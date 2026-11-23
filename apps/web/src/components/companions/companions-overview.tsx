"use client";

import { useNavigate } from "@tanstack/react-router";
import { Boxes, PackageOpen } from "lucide-react";
import { type ReactNode, type Ref, useRef, useState } from "react";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { EmptyState } from "@/components/empty-state";
import {
	ENTITY_GRID_CLASS,
	EntityCardLink,
	type EntityCardLinkOptions,
	EntityCardSkeleton,
	EntityHeader,
	entityCardChassisClass,
} from "@/components/entity-card";
import { IconChip } from "@/components/icon-chip";
import { ListToolbar } from "@/components/list-toolbar";
import { PageHeader } from "@/components/page-header";
import { CENTERED_PAGE_WIDTH_CLASS } from "@/components/page-width";
import { SectionLabel } from "@/components/section-label";
import { Button } from "@/components/ui/button";
import { SearchInput } from "@/components/ui/search-input";
import { StatusBadge } from "@/components/ui/status-badge";
import { useOpenApi } from "@/lib/api";
import { identityFor } from "@/lib/identity";
import { cn, formatAbsoluteTooltip } from "@/lib/utils";
import {
	type AvailableCardModel,
	blockingFailure,
	type CompanionCatalogItem,
	type CompanionInstallation,
	deriveOverviewModel,
	type InstalledCardModel,
	OVERVIEW_COPY,
	OVERVIEW_EMPTY_COPY,
	OVERVIEW_ERROR_NORMALIZER,
	OVERVIEW_SEARCH_EMPTY_COPY,
} from "./companions-overview.logic";
import { InstallCompanionDialog } from "./install-companion-dialog";
import { CompanionSectionTabs } from "./tool-activity-table";

const SURFACE_CLASS = cn(CENTERED_PAGE_WIDTH_CLASS.page, "space-y-8 px-4 lg:px-6");

/** Skeleton cards shown while the catalog loads — one full row at xl. */
const SKELETON_CARD_COUNT = 3;

/**
 * S1 — the Companions overview, for one hosted agent.
 *
 * Owns the two reads and the search term; everything it renders is decided by
 * `deriveOverviewModel` and drawn by `CompanionsOverviewView`, which takes
 * props only so every S1 state is renderable in a test without a network.
 */
export function CompanionsOverview({ agentId }: { agentId: string }) {
	const $api = useOpenApi();
	const navigate = useNavigate();
	const [search, setSearch] = useState("");
	const availableRef = useRef<HTMLElement>(null);
	// The item stays set after close so the dialog can animate out with its copy.
	const [installItem, setInstallItem] = useState<CompanionCatalogItem | null>(null);
	const [installOpen, setInstallOpen] = useState(false);
	// Bumped on every open so each open starts fresh: default tier, no rejection.
	const [installSession, setInstallSession] = useState(0);

	const catalog = $api.useQuery("get", "/v1/companions/catalog", {});
	const installations = $api.useQuery("get", "/v1/companions/installations", {
		params: { query: { agent_id: agentId } },
	});

	const failure = blockingFailure([catalog, installations]);

	function showAvailable() {
		const group = availableRef.current;
		if (!group) return;
		group.scrollIntoView?.({ block: "start", behavior: "smooth" });
		group.focus({ preventScroll: true });
	}

	return (
		<>
			<CompanionsOverviewView
				tabs={<CompanionSectionTabs agentId={agentId} active="companions" />}
				catalog={catalog.data?.items}
				installations={installations.data?.items}
				loading={
					(catalog.isLoading && !catalog.data) || (installations.isLoading && !installations.data)
				}
				error={failure?.error}
				onRetry={failure?.retry}
				search={search}
				onSearchChange={setSearch}
				onInstall={(item) => {
					setInstallItem(item);
					setInstallSession((session) => session + 1);
					setInstallOpen(true);
				}}
				onShowAvailable={showAvailable}
				availableRef={availableRef}
				installedLink={installedCompanionLink}
			/>
			{installItem ? (
				<InstallCompanionDialog
					key={`${installItem.companion_key}:${installSession}`}
					agentId={agentId}
					item={installItem}
					open={installOpen}
					onOpenChange={setInstallOpen}
					// An accepted install routes to S3, where its state machine and log live.
					onInstalled={(installation) => navigate(installedCompanionLink(installation))}
				/>
			) : null}
		</>
	);
}

/** S3 for one installed companion — the Installed card's whole-card link, and where S2 lands. */
export function installedCompanionLink(
	installation: Pick<CompanionInstallation, "agent_id" | "companion_key">,
) {
	return {
		to: "/agents/$id/companions/$companionKey",
		params: { id: installation.agent_id, companionKey: installation.companion_key },
	} satisfies EntityCardLinkOptions;
}

export interface CompanionsOverviewViewProps {
	/** The section tab strip. Router-bound, so the container supplies it. */
	tabs?: ReactNode;
	catalog: readonly CompanionCatalogItem[] | undefined;
	installations: readonly CompanionInstallation[] | undefined;
	loading?: boolean;
	error?: unknown;
	onRetry?: () => void;
	search?: string;
	onSearchChange?: (next: string) => void;
	/** Opens S2 for a catalog item. */
	onInstall?: (item: CompanionCatalogItem) => void;
	/** The page empty state's action: bring the Available group into view. */
	onShowAvailable?: () => void;
	availableRef?: Ref<HTMLElement>;
	/** Whole-card link target for an Installed card (S3). Omitted → the card is not a link. */
	installedLink?: (installation: CompanionInstallation) => EntityCardLinkOptions | undefined;
}

export function CompanionsOverviewView({
	tabs,
	catalog,
	installations,
	loading = false,
	error,
	onRetry,
	search = "",
	onSearchChange,
	onInstall,
	onShowAvailable,
	availableRef,
	installedLink,
}: CompanionsOverviewViewProps) {
	return (
		<div className={SURFACE_CLASS} data-testid="companions-overview">
			<div className="space-y-4">
				<PageHeader title={OVERVIEW_COPY.title} description={OVERVIEW_COPY.description} />
				{tabs}
			</div>
			<ListToolbar
				search={
					<SearchInput
						value={search}
						onChange={(next) => onSearchChange?.(next)}
						placeholder={OVERVIEW_COPY.searchPlaceholder}
						ariaLabel={OVERVIEW_COPY.searchLabel}
					/>
				}
			/>
			<OverviewBody
				catalog={catalog}
				installations={installations}
				loading={loading}
				error={error}
				onRetry={onRetry}
				search={search}
				onClearSearch={() => onSearchChange?.("")}
				onInstall={onInstall}
				onShowAvailable={onShowAvailable}
				availableRef={availableRef}
				installedLink={installedLink}
			/>
		</div>
	);
}

function OverviewBody({
	catalog,
	installations,
	loading,
	error,
	onRetry,
	search,
	onClearSearch,
	onInstall,
	onShowAvailable,
	availableRef,
	installedLink,
}: Omit<CompanionsOverviewViewProps, "tabs" | "onSearchChange"> & {
	loading: boolean;
	search: string;
	onClearSearch: () => void;
}) {
	if (error !== undefined && error !== null) {
		return <ApiErrorPanel error={error} onRetry={onRetry} normalizer={OVERVIEW_ERROR_NORMALIZER} />;
	}

	if (loading || catalog === undefined || installations === undefined) {
		return (
			<section className="space-y-3" aria-busy="true" data-slot="companions-loading">
				<SectionLabel leading={<PackageOpen className="size-3.5" aria-hidden />}>
					{OVERVIEW_COPY.availableGroup}
				</SectionLabel>
				<div className={ENTITY_GRID_CLASS}>
					{Array.from({ length: SKELETON_CARD_COUNT }, (_, index) => (
						<EntityCardSkeleton key={index} metaLines={2} />
					))}
				</div>
			</section>
		);
	}

	const model = deriveOverviewModel({ catalog, installations, search });

	return (
		<div className="space-y-6">
			{model.showPageEmpty ? (
				<EmptyState
					variant="page"
					title={OVERVIEW_EMPTY_COPY.title}
					description={OVERVIEW_EMPTY_COPY.body}
					action={
						model.availableTotal > 0 ? (
							<Button variant="outline" onClick={() => onShowAvailable?.()}>
								{OVERVIEW_EMPTY_COPY.action}
							</Button>
						) : undefined
					}
				/>
			) : null}

			{model.installed.length > 0 ? (
				<section className="space-y-3" data-slot="companions-installed">
					<SectionLabel
						leading={<Boxes className="size-3.5" aria-hidden />}
						count={model.installed.length}
					>
						{OVERVIEW_COPY.installedGroup}
					</SectionLabel>
					<div className={ENTITY_GRID_CLASS}>
						{model.installed.map((card) => (
							<InstalledCompanionCard
								key={card.installation.id}
								card={card}
								link={installedLink?.(card.installation)}
							/>
						))}
					</div>
				</section>
			) : null}

			{model.availableTotal > 0 ? (
				<section
					ref={availableRef}
					tabIndex={-1}
					className="space-y-3 outline-none"
					data-slot="companions-available"
					aria-label={OVERVIEW_COPY.availableGroup}
				>
					<SectionLabel
						leading={<PackageOpen className="size-3.5" aria-hidden />}
						count={model.available.length}
					>
						{OVERVIEW_COPY.availableGroup}
					</SectionLabel>
					{model.showSearchEmpty ? (
						<EmptyState
							variant="inset"
							title={OVERVIEW_SEARCH_EMPTY_COPY.title}
							description={OVERVIEW_SEARCH_EMPTY_COPY.body}
							action={
								<Button variant="outline" size="sm" onClick={onClearSearch}>
									{OVERVIEW_SEARCH_EMPTY_COPY.action}
								</Button>
							}
						/>
					) : (
						<div className={ENTITY_GRID_CLASS}>
							{model.available.map((card) => (
								<AvailableCompanionCard
									key={card.item.companion_key}
									card={card}
									onInstall={onInstall}
								/>
							))}
						</div>
					)}
				</section>
			) : null}
		</div>
	);
}

/** The companion's identity tile — S1's cards and S3's header share it. */
export function CompanionIdentityTile({ seed }: { seed: string }) {
	const identity = identityFor(seed);
	return (
		<IconChip tint={identity.colorClasses} className="text-xl">
			{identity.emoji}
		</IconChip>
	);
}

/**
 * An Installed card. When `link` is supplied the whole card is the link
 * target (a stretched link) and nothing inside it is interactive: chips and
 * the hostname are text, not controls.
 */
export function InstalledCompanionCard({
	card,
	link,
}: {
	card: InstalledCardModel;
	link?: EntityCardLinkOptions;
}) {
	const { installation } = card;
	return (
		<article
			className={entityCardChassisClass({
				variant: "compact",
				interactive: Boolean(link),
				className: "flex flex-col gap-3",
			})}
			data-slot="installed-companion-card"
			data-state={installation.status}
			data-operation={installation.operation}
		>
			<EntityHeader
				icon={<CompanionIdentityTile seed={installation.companion_key} />}
				title={card.name}
			/>
			<div className="flex min-w-0 flex-wrap items-center gap-2">
				<StatusBadge
					status={card.stateChip.tone}
					withDot
					title={installation.status}
					data-state={installation.status}
				>
					{card.stateChip.label}
				</StatusBadge>
				{card.reachability ? (
					<StatusBadge
						status={card.reachability.tone}
						title={installation.reachability.state}
						data-reachability={installation.reachability.state}
					>
						{card.reachability.label}
					</StatusBadge>
				) : null}
			</div>
			{card.hostname ? (
				<p
					className="truncate font-mono text-xs text-muted-foreground"
					title={card.hostname}
					data-slot="companion-hostname"
				>
					{card.hostname}
				</p>
			) : null}
			<p className="truncate text-xs text-muted-foreground" data-slot="companion-meta">
				{card.installedLabel ? (
					<span title={formatAbsoluteTooltip(card.installedAt)}>{card.installedLabel} · </span>
				) : null}
				<span title={installation.isolation_tier}>{card.tierLabel}</span>
			</p>
			{link ? <EntityCardLink variant="compact" {...link} ariaLabel={card.name} /> : null}
		</article>
	);
}

/**
 * An Available card. Not a link: its trailing Install button is the only
 * interactive element, and it opens S2 rather than installing anything.
 */
export function AvailableCompanionCard({
	card,
	onInstall,
}: {
	card: AvailableCardModel;
	onInstall?: (item: CompanionCatalogItem) => void;
}) {
	return (
		<article
			className={entityCardChassisClass({ variant: "compact", className: "flex flex-col gap-3" })}
			data-slot="available-companion-card"
		>
			<EntityHeader
				icon={<CompanionIdentityTile seed={card.item.companion_key} />}
				title={card.name}
			/>
			{card.description ? (
				<p className="truncate text-sm text-muted-foreground" title={card.description}>
					{card.description}
				</p>
			) : null}
			<div className="flex min-w-0 items-center gap-2">
				{card.priceLabel ? (
					<p
						className="min-w-0 flex-1 truncate text-xs text-muted-foreground tabular-nums"
						title={card.priceLabel}
						data-slot="companion-price"
					>
						{card.priceLabel}
					</p>
				) : (
					<span className="flex-1" />
				)}
				<Button
					variant="outline"
					size="sm"
					disabled={!card.installable}
					onClick={() => onInstall?.(card.item)}
				>
					{OVERVIEW_COPY.installButton}
				</Button>
			</div>
		</article>
	);
}
