"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ChevronRight, TriangleAlert } from "lucide-react";
import { useId, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { ApiErrorPanel } from "@/components/api-error-panel";
import {
	type CompanionCatalogItem,
	OVERVIEW_ERROR_NORMALIZER,
} from "@/components/companions/companions-overview.logic";
import { EmptyState } from "@/components/empty-state";
import { SettingsSection } from "@/components/settings-section";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
	InputGroup,
	InputGroupAddon,
	InputGroupInput,
	InputGroupText,
} from "@/components/ui/input-group";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useApi, useOpenApi } from "@/lib/api";
import { shouldBlockQueryError } from "@/lib/query-state";
import { cn } from "@/lib/utils";
import {
	COMPANION_SPEND_CEILING_QUERY_KEY,
	COMPANION_SPEND_COPY,
	type CompanionSpend,
	type CompanionSpendCeiling,
	type CompanionSpendLine,
	canSaveCeiling,
	ceilingInputValue,
	ceilingReadOut,
	companionDisplayName,
	companionSpendRequests,
	dimensionCell,
	parseCeilingInput,
	rowLabel,
	runSaveCeiling,
	SPEND_DIMENSIONS,
} from "./companion-spend.logic";

export type CompanionSpendSectionViewProps = {
	/** The ceiling route's answer; undefined until it has answered. */
	ceiling: CompanionSpendCeiling | undefined;
	ceilingLoading: boolean;
	/** A BLOCKING ceiling-load error — one with no cached ceiling left to render. */
	ceilingError: unknown;
	onRetryCeiling: () => void;
	ceilingInput: string;
	onCeilingInputChange: (value: string) => void;
	onSaveCeiling: () => void;
	savePending: boolean;
	saveError: string | null;
	spend: CompanionSpend | undefined;
	spendLoading: boolean;
	/** A BLOCKING spend-load error — one with no cached spend left to render. */
	spendError: unknown;
	onRetrySpend: () => void;
	catalog: readonly CompanionCatalogItem[] | undefined;
	expanded: ReadonlySet<string>;
	onToggleRow: (installationId: string) => void;
};

/**
 * S6 — Companion spend and ceiling, the last section of Settings → Usage
 * (hosted-only).
 *
 * The ceiling control reads and writes ONLY the ceiling route, so a slow or
 * failed spend read never stops a tenant from saving a ceiling. Both routes
 * are month-scoped to the current UTC month server-side; the section adds no
 * period control of its own, and the panel's day-range control keeps scoping
 * the model breakdown above it.
 */
export function CompanionSpendSection() {
	const $api = useOpenApi();
	const api = useApi();
	const queryClient = useQueryClient();
	const requests = useMemo(() => companionSpendRequests(api), [api]);
	const ceilingQuery = $api.useQuery("get", "/v1/companions/spend-ceiling", {});
	const spendQuery = $api.useQuery("get", "/v1/companions/spend", {});
	const catalogQuery = $api.useQuery("get", "/v1/companions/catalog", {});

	// null = the input shows the stored ceiling; a string = what the tenant typed.
	const [draft, setDraft] = useState<string | null>(null);
	const [savePending, setSavePending] = useState(false);
	const [saveError, setSaveError] = useState<string | null>(null);
	// Synchronous lock: `disabled` lands only on the next render, which leaves
	// a double click room to send two writes.
	const saveLock = useRef(false);
	const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());

	const ceiling = ceilingQuery.data;
	const ceilingInput = draft ?? (ceiling ? ceilingInputValue(ceiling.ceiling_usd_micros) : "");

	async function handleSave() {
		if (saveLock.current) return;
		if (!canSaveCeiling(ceilingInput, ceiling?.ceiling_usd_micros)) return;
		saveLock.current = true;
		setSavePending(true);
		setSaveError(null);
		try {
			const outcome = await runSaveCeiling({
				input: ceilingInput,
				requestSave: requests.saveCeiling,
				onSaved: async () => {
					toast.success(COMPANION_SPEND_COPY.savedToast);
					await queryClient.invalidateQueries({ queryKey: COMPANION_SPEND_CEILING_QUERY_KEY });
					setDraft(null);
				},
			});
			setSaveError(outcome.ok ? null : outcome.message);
		} finally {
			saveLock.current = false;
			setSavePending(false);
		}
	}

	function toggleRow(installationId: string) {
		setExpanded((current) => {
			const next = new Set(current);
			if (next.has(installationId)) next.delete(installationId);
			else next.add(installationId);
			return next;
		});
	}

	return (
		<CompanionSpendSectionView
			ceiling={ceiling}
			ceilingLoading={ceilingQuery.isLoading}
			ceilingError={
				shouldBlockQueryError(ceilingQuery.error, ceilingQuery.data) ? ceilingQuery.error : null
			}
			onRetryCeiling={() => void ceilingQuery.refetch()}
			ceilingInput={ceilingInput}
			onCeilingInputChange={(value) => {
				setDraft(value);
				setSaveError(null);
			}}
			onSaveCeiling={() => void handleSave()}
			savePending={savePending}
			saveError={saveError}
			spend={spendQuery.data}
			// The row label needs the catalog's display name, so rows wait for it;
			// a failed catalog read falls back to the companion key.
			spendLoading={spendQuery.isLoading || catalogQuery.isLoading}
			spendError={
				shouldBlockQueryError(spendQuery.error, spendQuery.data) ? spendQuery.error : null
			}
			onRetrySpend={() => void spendQuery.refetch()}
			catalog={catalogQuery.data?.items}
			expanded={expanded}
			onToggleRow={toggleRow}
		/>
	);
}

/** Props-only render of every S6 state (component-testable without a network). */
export function CompanionSpendSectionView({
	ceiling,
	ceilingLoading,
	ceilingError,
	onRetryCeiling,
	ceilingInput,
	onCeilingInputChange,
	onSaveCeiling,
	savePending,
	saveError,
	spend,
	spendLoading,
	spendError,
	onRetrySpend,
	catalog,
	expanded,
	onToggleRow,
}: CompanionSpendSectionViewProps) {
	const ceilingMicros = ceiling?.ceiling_usd_micros ?? null;
	return (
		<SettingsSection
			data-hosted="true"
			data-slot="companion-spend"
			headingLevel={3}
			title={COMPANION_SPEND_COPY.sectionTitle}
		>
			<div className="flex flex-col gap-6">
				{ceiling?.reached ? (
					// Warning, never destructive: at the ceiling the companions keep
					// running (D-21), and a red banner would say they had stopped.
					<Alert variant="warning" data-ceiling-reached="true">
						<TriangleAlert aria-hidden="true" />
						<AlertDescription>{COMPANION_SPEND_COPY.reachedBanner}</AlertDescription>
					</Alert>
				) : null}

				{ceilingError ? (
					<ApiErrorPanel error={ceilingError} onRetry={onRetryCeiling} />
				) : (
					<CeilingControl
						ceiling={ceiling}
						loading={ceilingLoading}
						input={ceilingInput}
						onInputChange={onCeilingInputChange}
						onSave={onSaveCeiling}
						pending={savePending}
						error={saveError}
					/>
				)}

				{ceiling && ceilingMicros !== null ? (
					<p data-slot="ceiling-read-out" className="text-sm tabular-nums">
						{ceilingReadOut(ceiling.spent_usd_micros, ceilingMicros)}
					</p>
				) : null}

				<SpendLines
					spend={spend}
					loading={spendLoading}
					error={spendError}
					onRetry={onRetrySpend}
					catalog={catalog}
					expanded={expanded}
					onToggleRow={onToggleRow}
				/>
			</div>
		</SettingsSection>
	);
}

function CeilingControl({
	ceiling,
	loading,
	input,
	onInputChange,
	onSave,
	pending,
	error,
}: {
	ceiling: CompanionSpendCeiling | undefined;
	loading: boolean;
	input: string;
	onInputChange: (value: string) => void;
	onSave: () => void;
	pending: boolean;
	error: string | null;
}) {
	const inputId = useId();
	const helperId = useId();
	const stored = ceiling ? ceiling.ceiling_usd_micros : undefined;
	const invalid = input.trim() !== "" && parseCeilingInput(input) === null;
	return (
		<div data-slot="ceiling-control" className="flex flex-col gap-3">
			<div className="space-y-1.5">
				<Label htmlFor={inputId}>{COMPANION_SPEND_COPY.ceilingLabel}</Label>
				<InputGroup className="max-w-48">
					<InputGroupAddon>
						<InputGroupText>$</InputGroupText>
					</InputGroupAddon>
					<InputGroupInput
						id={inputId}
						inputMode="decimal"
						autoComplete="off"
						className="tabular-nums"
						value={input}
						onChange={(event) => onInputChange(event.target.value)}
						onKeyDown={(event) => {
							if (event.key === "Enter") onSave();
						}}
						disabled={!ceiling || loading || pending}
						aria-invalid={invalid || undefined}
						aria-describedby={helperId}
					/>
				</InputGroup>
			</div>
			<p id={helperId} className="max-w-2xl text-xs text-muted-foreground">
				{COMPANION_SPEND_COPY.ceilingHelper}
			</p>
			<div>
				<Button
					type="button"
					size="sm"
					onClick={onSave}
					disabled={pending || !canSaveCeiling(input, stored)}
				>
					{pending ? <Spinner /> : null}
					{COMPANION_SPEND_COPY.saveCta}
				</Button>
			</div>
			{/* A rejected save renders inline beneath the control, never as a toast. */}
			{error ? (
				<p role="alert" className="text-sm text-destructive-muted-foreground">
					{error}
				</p>
			) : null}
		</div>
	);
}

function SpendLines({
	spend,
	loading,
	error,
	onRetry,
	catalog,
	expanded,
	onToggleRow,
}: {
	spend: CompanionSpend | undefined;
	loading: boolean;
	error: unknown;
	onRetry: () => void;
	catalog: readonly CompanionCatalogItem[] | undefined;
	expanded: ReadonlySet<string>;
	onToggleRow: (installationId: string) => void;
}) {
	if (error) {
		return (
			<ApiErrorPanel
				error={error}
				onRetry={onRetry}
				title={COMPANION_SPEND_COPY.spendLoadFailed}
				normalizer={OVERVIEW_ERROR_NORMALIZER}
			/>
		);
	}
	if (loading) {
		return (
			<div data-slot="companion-spend-loading" role="status" className="flex flex-col gap-2">
				<span className="sr-only">{COMPANION_SPEND_COPY.loadingSpend}</span>
				<Skeleton className="h-10 w-full" />
				<Skeleton className="h-10 w-full" />
			</div>
		);
	}
	if (!spend || spend.companions.length === 0) {
		return (
			<EmptyState
				variant="inset"
				title={COMPANION_SPEND_COPY.emptyTitle}
				description={COMPANION_SPEND_COPY.emptyBody}
			/>
		);
	}
	// Rows, not cards (DESIGN.md principle 2: this is settings). One scroll
	// region only: the settings panel scrolls, so the list grows in place.
	return (
		<ul
			data-slot="companion-spend-list"
			className="divide-y divide-border rounded-lg border border-border bg-card"
		>
			{spend.companions.map((line) => (
				<SpendRow
					key={line.installation_id}
					line={line}
					label={rowLabel(companionDisplayName(line.companion_key, catalog), line.isolation_tier)}
					open={expanded.has(line.installation_id)}
					onToggle={() => onToggleRow(line.installation_id)}
				/>
			))}
		</ul>
	);
}

function SpendRow({
	line,
	label,
	open,
	onToggle,
}: {
	line: CompanionSpendLine;
	label: string;
	open: boolean;
	onToggle: () => void;
}) {
	const detailId = useId();
	return (
		<li
			data-slot="companion-spend-row"
			data-installation-id={line.installation_id}
			className="px-3 py-2"
		>
			<div className="flex items-center gap-3">
				<Button
					type="button"
					variant="ghost"
					size="sm"
					className="min-w-0 flex-1 justify-start px-1"
					aria-expanded={open}
					aria-controls={open ? detailId : undefined}
					onClick={onToggle}
				>
					<ChevronRight
						aria-hidden="true"
						className={cn("text-muted-foreground transition-transform", open && "rotate-90")}
					/>
					<span className="truncate" title={line.isolation_tier}>
						{label}
					</span>
				</Button>
				<Amount micros={line.total_usd_micros} className="font-medium" />
			</div>
			{open ? (
				<dl
					id={detailId}
					data-slot="companion-spend-dimensions"
					className="flex flex-col gap-1 pt-2 pb-1 pl-8"
				>
					{SPEND_DIMENSIONS.map((dimension) => (
						<div key={dimension.field} className="flex items-center justify-between gap-3 text-xs">
							<dt className="min-w-0 truncate text-muted-foreground">{dimension.label}</dt>
							<dd>
								<Amount micros={line[dimension.field]} className="text-xs" />
							</dd>
						</div>
					))}
				</dl>
			) : null}
		</li>
	);
}

/**
 * One money figure: right-aligned `tabular-nums` that never wraps. An
 * unmeasured figure is an em dash with the not-yet-attributed tooltip, and
 * the same words for a screen reader, never "$0.00".
 */
function Amount({ micros, className }: { micros: number | null; className?: string }) {
	const cell = dimensionCell(micros);
	const classes = cn("shrink-0 text-right text-sm whitespace-nowrap tabular-nums", className);
	if (cell.attributed) {
		return (
			<span data-slot="spend-amount" className={classes}>
				{cell.text}
			</span>
		);
	}
	return (
		<Tooltip>
			<TooltipTrigger
				render={
					<span data-slot="spend-unattributed" className={cn(classes, "text-muted-foreground")}>
						<span aria-hidden="true">{cell.text}</span>
						<span className="sr-only">{cell.tooltip}</span>
					</span>
				}
			/>
			<TooltipContent>{cell.tooltip}</TooltipContent>
		</Tooltip>
	);
}
