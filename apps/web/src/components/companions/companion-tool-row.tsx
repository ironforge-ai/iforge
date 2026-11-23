"use client";

import { MoreHorizontal } from "lucide-react";
import { useState } from "react";
import { TruncatedText } from "@/components/truncated-text";
import { Button } from "@/components/ui/button";
import { ConfirmAction } from "@/components/ui/confirm-action";
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { StatusBadge } from "@/components/ui/status-badge";
import { cn } from "@/lib/utils";
import {
	allowWithoutAskingCopy,
	type CompanionToolSettings,
	capUnitMeta,
	type PolicyMode,
	type PolicyRowState,
	policyIsOverridden,
	policyOptions,
	policySelectDisabled,
	policySelectValue,
	requiresConfirmation,
	resolvedPolicyOption,
	resolvedReasonSentence,
	toolClassTone,
} from "./tool-policy.logic";

/**
 * One configuration row. Rows, not cards — DESIGN.md principle 2 reserves
 * cards for objects, and the tool catalog is settings.
 *
 * The Select carries the tenant's SETTING and the chip beside it carries what
 * the server would actually resolve right now. When the two differ the row
 * says why, because a UI that showed only one of them would either hide the
 * tenant's choice or display a guarantee the next call will refuse.
 */
export function CompanionToolRow({
	row,
	agentLabel,
	state,
	onModeChange,
	onCopyToolName,
	blocked = false,
}: {
	row: CompanionToolSettings;
	agentLabel: string;
	state: PolicyRowState;
	/** Resolves when the server has accepted the change; rejects otherwise. */
	onModeChange: (mode: PolicyMode) => Promise<unknown>;
	onCopyToolName?: (tool: string) => void;
	/**
	 * The companion's whole group is blocked (S4). The row stays visible and
	 * readable, so the tenant keeps track of what they blocked, but it is
	 * dimmed and every control on it is disabled.
	 */
	blocked?: boolean;
}) {
	// The mode the user picked that still needs an explicit confirmation.
	// `ConfirmAction` opens from a trigger click, so a safety-weakening choice
	// is STAGED here and applied only from the dialog — which also means the
	// Select never flips to a mode the server has not accepted.
	const [stagedMode, setStagedMode] = useState<PolicyMode | null>(null);
	const disabled = blocked || policySelectDisabled(row, state.pending);
	const resolved = resolvedPolicyOption(row);
	const overridden = policyIsOverridden(row);
	const reason = resolvedReasonSentence(row.resolved_reason);
	const staged = stagedMode ? allowWithoutAskingCopy(row.tool, agentLabel) : null;

	function apply(mode: PolicyMode) {
		return onModeChange(mode).then(
			(result) => {
				setStagedMode(null);
				return result;
			},
			(error: unknown) => {
				throw error;
			},
		);
	}

	return (
		<div
			data-slot="companion-tool-row"
			data-tool={row.tool}
			data-tool-class={row.tool_class}
			data-blocked={blocked ? "true" : undefined}
			className={cn(
				"flex flex-col gap-2 border-border border-b p-2 last:border-b-0 sm:flex-row sm:items-center",
				blocked ? "text-muted-foreground" : "hover:bg-muted/50",
			)}
		>
			<div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
				<StatusBadge status={toolClassTone(row.tool_class)} className="font-mono">
					{row.tool_class}
				</StatusBadge>
				<TruncatedText className="min-w-0 max-w-full font-mono text-xs">{row.tool}</TruncatedText>
				{row.dry_run_available ? <StatusBadge status="info">Dry run</StatusBadge> : null}
				<span className="text-muted-foreground text-xs">·</span>
				<span className="text-muted-foreground text-xs">{capUnitMeta(row.cap_unit)}</span>
			</div>
			<div className="flex shrink-0 items-center gap-2">
				<StatusBadge status={resolved.tone}>{resolved.chipLabel}</StatusBadge>
				{state.pending ? <Spinner className="size-4 text-muted-foreground" /> : null}
				<Select
					value={policySelectValue(row)}
					items={policyOptions.map((option) => ({ value: option.value, label: option.label }))}
					disabled={disabled}
					onValueChange={(value) => {
						const mode = value as PolicyMode;
						if (mode === policySelectValue(row)) return;
						if (requiresConfirmation(row.tool_class, mode)) {
							setStagedMode(mode);
							return;
						}
						void apply(mode).catch(() => {});
					}}
				>
					<SelectTrigger
						size="sm"
						aria-label={`Approval policy for ${row.tool}`}
						className={cn("w-28", disabled && "opacity-50")}
					>
						<SelectValue />
					</SelectTrigger>
					<SelectContent>
						{policyOptions.map((option) => (
							<SelectItem key={option.value} value={option.value}>
								{option.label}
							</SelectItem>
						))}
					</SelectContent>
				</Select>
				<DropdownMenu>
					<DropdownMenuTrigger
						render={
							<Button
								type="button"
								variant="ghost"
								size="icon-sm"
								aria-label={`Actions for ${row.tool}`}
								disabled={blocked}
							/>
						}
					>
						<MoreHorizontal />
					</DropdownMenuTrigger>
					<DropdownMenuContent align="end">
						<DropdownMenuItem onClick={() => onCopyToolName?.(row.tool)}>
							Copy tool name
						</DropdownMenuItem>
					</DropdownMenuContent>
				</DropdownMenu>
			</div>
			{staged && !blocked ? (
				<div className="flex w-full items-center gap-2 sm:w-auto">
					<ConfirmAction
						title={staged.title}
						description={staged.description}
						confirmLabel={staged.confirmLabel}
						destructive={staged.destructive}
						onOpenChange={(open) => {
							if (!open) setStagedMode(null);
						}}
						onConfirm={() => apply(stagedMode as PolicyMode)}
					>
						<Button type="button" size="xs">
							{staged.confirmLabel}
						</Button>
					</ConfirmAction>
				</div>
			) : null}
			{reason ? (
				<p
					data-slot="companion-tool-row-reason"
					data-overridden={overridden ? "true" : undefined}
					className="w-full text-muted-foreground text-xs sm:w-auto"
				>
					{reason}
				</p>
			) : null}
			{state.error ? (
				<p className="w-full text-destructive-muted-foreground text-xs sm:w-auto">{state.error}</p>
			) : null}
		</div>
	);
}

/** Loading shape at the real row geometry — chip, mono name, meta, Select. */
export function CompanionToolRowSkeleton() {
	return (
		<div
			data-slot="companion-tool-row-skeleton"
			aria-hidden="true"
			className="flex items-center gap-2 border-border border-b p-2 last:border-b-0"
		>
			<Skeleton className="h-5 w-14 rounded-sm" />
			<Skeleton className="h-4 w-48 max-w-full" />
			<Skeleton className="h-4 w-24" />
			<div className="flex-1" />
			<Skeleton className="h-8 w-28 rounded-md" />
			<Skeleton className="size-8 rounded-md" />
		</div>
	);
}
