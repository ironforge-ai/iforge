"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Database, type LucideIcon, Server, Users } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { EntityChoiceCard } from "@/components/entity-card";
import { IconChip } from "@/components/icon-chip";
import { Button } from "@/components/ui/button";
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { StatusBadge } from "@/components/ui/status-badge";
import { unwrap, useApi, useOpenApi } from "@/lib/api";
import {
	COMPANION_INSTALLATIONS_QUERY_KEY,
	type CompanionCatalogItem,
	type CompanionInstallation,
	canConfirmInstall,
	defaultTier,
	deriveQuoteState,
	formatUsdMicros,
	INSTALL_DIALOG_COPY,
	type IsolationTier,
	installCredentialNote,
	installDialogDescription,
	installDialogTitle,
	OVERVIEW_ERROR_NORMALIZER,
	PRICE_PLACEHOLDER,
	type QuoteState,
	runInstall,
	type TierOption,
	tierOptions,
} from "./companions-overview.logic";

/** Floor for re-quoting, so a quote that expires "now" cannot drive a tight refetch loop. */
const MIN_REQUOTE_MS = 1_000;

/** 06.1-UI-SPEC §T2: one lucide glyph per tier, in an `IconChip`. */
const TIER_ICONS: Readonly<Record<IsolationTier, LucideIcon>> = {
	standard: Database,
	dedicated: Server,
	shared: Users,
};

/**
 * S2 — install one companion on one agent, after its price is on screen.
 *
 * APP-08 is literal here: the quote is requested when the dialog opens and
 * whenever the tier changes, the confirm button stays disabled until a CURRENT
 * quote for the selected tier is rendered, and the install request carries
 * that rendered quote's id (and reads its companion and tier from it). The
 * backend verifies the quote (plan 06-17); this dialog makes it impossible to
 * confirm one the tenant has not seen.
 *
 * S2 is a form, so it is a `Dialog` rather than an `AlertDialog`. Its submit
 * follows `ConfirmAction`'s contract — the dialog holds open with a `Spinner`
 * until the job is accepted, a double click cannot submit twice, dismissal is
 * refused while the request is in flight, and a rejection renders inline
 * beneath the confirm button — without stacking a second modal whose body
 * would only repeat this one (see the 06-26 SUMMARY).
 */
export function InstallCompanionDialog({
	agentId,
	item,
	open,
	onOpenChange,
	onInstalled,
}: {
	agentId: string;
	item: CompanionCatalogItem;
	open: boolean;
	onOpenChange: (open: boolean) => void;
	onInstalled?: (installation: CompanionInstallation) => unknown;
}) {
	const $api = useOpenApi();
	const api = useApi();
	const queryClient = useQueryClient();
	const [tier, setTier] = useState<IsolationTier>(() => defaultTier(item));
	const [pending, setPending] = useState(false);
	const [rejection, setRejection] = useState<string | null>(null);
	// Synchronous lock: `disabled` only lands on the next render, which leaves
	// a sub-frame window for a second click to submit the same install twice.
	const locked = useRef(false);

	const quote = $api.useQuery(
		"post",
		"/v1/companions/quotes",
		{ body: { companion_key: item.companion_key, isolation_tier: tier } },
		{
			enabled: open,
			gcTime: 0,
			refetchOnWindowFocus: false,
			// Re-quote as the quote expires, so the price on screen is always one
			// the backend will still accept.
			refetchInterval: (query) => {
				const expiresAt = query.state.data?.expires_at;
				if (!expiresAt) return false;
				return Math.max(Date.parse(expiresAt) - Date.now(), MIN_REQUOTE_MS);
			},
		},
	);

	const state = deriveQuoteState({
		data: quote.data,
		error: quote.error,
		companionKey: item.companion_key,
		tier,
	});

	function handleOpenChange(next: boolean) {
		// Held open until the job is accepted or refused. Each open mounts a
		// fresh dialog (the overview keys it per open), so there is nothing to reset.
		if (pending) return;
		onOpenChange(next);
	}

	async function confirm() {
		if (locked.current || !canConfirmInstall(state, false)) return;
		locked.current = true;
		setPending(true);
		setRejection(null);
		const outcome = await runInstall({
			agentId,
			state,
			requestInstall: async (body) =>
				unwrap(await api.POST("/v1/companions/installations", { body })),
			onAccepted: async (installation) => {
				toast.success(INSTALL_DIALOG_COPY.startedToast);
				await queryClient.invalidateQueries({ queryKey: COMPANION_INSTALLATIONS_QUERY_KEY });
				await onInstalled?.(installation);
			},
		});
		locked.current = false;
		setPending(false);
		if (outcome.ok) {
			onOpenChange(false);
		} else {
			// The dialog stays open with the tier the tenant chose.
			setRejection(outcome.message);
		}
	}

	return (
		<Dialog open={open} onOpenChange={handleOpenChange}>
			<DialogContent className="rounded-xl sm:max-w-lg" showCloseButton={!pending}>
				<DialogHeader>
					<DialogTitle>{installDialogTitle(item.display_name)}</DialogTitle>
					<DialogDescription>{installDialogDescription(item.display_name)}</DialogDescription>
				</DialogHeader>
				<InstallCompanionDialogBody
					companionName={item.display_name}
					options={tierOptions(item)}
					tier={tier}
					onTierChange={(next) => {
						setTier(next);
						setRejection(null);
					}}
					quote={state}
					onRetryQuote={() => {
						void quote.refetch();
					}}
					pending={pending}
					rejection={rejection}
					onConfirm={() => {
						void confirm();
					}}
					onCancel={() => handleOpenChange(false)}
				/>
			</DialogContent>
		</Dialog>
	);
}

export interface InstallCompanionDialogBodyProps {
	companionName: string;
	options: readonly TierOption[];
	tier: IsolationTier;
	onTierChange?: (tier: IsolationTier) => void;
	quote: QuoteState;
	onRetryQuote?: () => void;
	pending?: boolean;
	/** The inline rejection sentence, or null. */
	rejection?: string | null;
	onConfirm?: () => void;
	onCancel?: () => void;
}

/**
 * S2's body in its fixed order — Isolation → You will be charged → credential
 * note — then the footer. Props only, so every S2 state renders in a test
 * without a portal or a network.
 */
export function InstallCompanionDialogBody({
	companionName,
	options,
	tier,
	onTierChange,
	quote,
	onRetryQuote,
	pending = false,
	rejection = null,
	onConfirm,
	onCancel,
}: InstallCompanionDialogBodyProps) {
	const confirmEnabled = canConfirmInstall(quote, pending);
	return (
		<div className="space-y-6" data-slot="install-companion-body">
			<fieldset data-slot="install-tier" disabled={pending}>
				<legend className="mb-2 text-sm font-semibold">{INSTALL_DIALOG_COPY.tierGroupLabel}</legend>
				{/*
				 * 06.1-UI-SPEC §T2: EntityChoiceCard replaces the RadioGroup upstream
				 * deleted. `onClick` is passed to every option, disabled ones included,
				 * so each renders as `<button type="button" aria-pressed>` with native
				 * `disabled`. Clicking the selected card re-selects it, so exactly one
				 * option is always pressed.
				 */}
				<div className="grid gap-2">
					{options.map((option) => {
						const Icon = TIER_ICONS[option.tier];
						return (
							<div key={option.tier} data-slot="install-tier-option" data-tier={option.tier}>
								<EntityChoiceCard
									icon={
										<IconChip size="sm" tint="bg-muted text-muted-foreground">
											<Icon />
										</IconChip>
									}
									title={option.title}
									description={option.description}
									selected={tier === option.tier}
									badge={
										option.offered ? undefined : (
											<StatusBadge status="neutral">
												{INSTALL_DIALOG_COPY.unavailableNote}
											</StatusBadge>
										)
									}
									disabled={!option.offered}
									onClick={() => onTierChange?.(option.tier)}
								/>
							</div>
						);
					})}
				</div>
			</fieldset>

			<section
				className="space-y-1 rounded-lg bg-muted/30 p-4"
				data-slot="install-price"
				data-quote-id={quote.status === "ready" ? quote.quote.quote_id : undefined}
				aria-live="polite"
				aria-busy={quote.status === "loading"}
			>
				<h3 className="text-sm font-semibold">{INSTALL_DIALOG_COPY.priceLabel}</h3>
				{quote.status === "ready" ? (
					<PriceBody price={formatUsdMicros(quote.quote.monthly_price_usd_micros)} />
				) : quote.status === "error" ? (
					<ApiErrorPanel
						error={quote.error}
						onRetry={onRetryQuote}
						normalizer={OVERVIEW_ERROR_NORMALIZER}
					/>
				) : (
					<Skeleton className="h-11 w-full" data-slot="install-price-skeleton" />
				)}
			</section>

			<p className="text-sm text-muted-foreground" data-slot="install-credential-note">
				{installCredentialNote(companionName)}
			</p>

			<div className="space-y-2">
				<DialogFooter>
					<Button variant="outline" onClick={onCancel} disabled={pending}>
						{INSTALL_DIALOG_COPY.cancel}
					</Button>
					<Button
						onClick={onConfirm}
						disabled={!confirmEnabled}
						data-slot="install-confirm"
						aria-busy={pending}
					>
						{pending ? <Spinner /> : null}
						{INSTALL_DIALOG_COPY.confirm}
					</Button>
				</DialogFooter>
				{rejection ? (
					<p
						role="alert"
						className="text-sm text-destructive-muted-foreground sm:text-right"
						data-slot="install-rejection"
					>
						{rejection}
					</p>
				) : null}
			</div>
		</div>
	);
}

/** "{price} per month, …" with the amount in `tabular-nums`. */
function PriceBody({ price }: { price: string }) {
	const [before, after] = INSTALL_DIALOG_COPY.priceBodyTemplate.split(PRICE_PLACEHOLDER);
	return (
		<p className="min-h-11 text-sm leading-relaxed" data-slot="install-price-body">
			{before}
			<span className="font-medium tabular-nums">{price}</span>
			{after}
		</p>
	);
}
