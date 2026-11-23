"use client";

import { EmptyState } from "@/components/empty-state";
import { SectionLabel } from "@/components/section-label";
import { ConfirmAction } from "@/components/ui/confirm-action";
import { Switch } from "@/components/ui/switch";
import { CompanionToolRow } from "./companion-tool-row";
import {
	type CompanionSummary,
	type CompanionToolSettings,
	DESTROY_GROUP_EMPTY_COPY,
	DESTROY_GROUP_TITLE,
	enableDestroyToolsCopy,
	type PolicyMode,
	type PolicyRowState,
} from "./tool-policy.logic";

/**
 * The destroy verbs, in their own group behind an enablement Switch.
 *
 * The group renders ONLY when the companion actually declares a destroy verb.
 * `has_destroy_verbs` and `destroy_enabled` are separate fields on the wire
 * because they are separate situations, and a Switch that governs nothing is
 * worse than no group at all.
 *
 * **The Switch never flips optimistically.** In the off position it IS the
 * `ConfirmAction` trigger: clicking it opens the dialog and carries no
 * `onCheckedChange`, so the control cannot move until the server has accepted
 * the position. `ConfirmAction` holds the dialog open with a spinner until the
 * promise settles and renders a rejection inline in the still-open dialog — so
 * a refused enablement leaves the Switch exactly where it was.
 */
export function DestroyToolsGroup({
	companion,
	tools,
	agentLabel,
	policyState,
	onModeChange,
	onSetEnabled,
	enablementError,
	onCopyToolName,
	blocked = false,
}: {
	companion: CompanionSummary;
	tools: readonly CompanionToolSettings[];
	agentLabel: string;
	policyState: (tool: string) => PolicyRowState;
	onModeChange: (tool: string, mode: PolicyMode) => Promise<unknown>;
	/** Resolves when the server has accepted the position; rejects otherwise. */
	onSetEnabled: (enabled: boolean) => Promise<unknown>;
	enablementError?: string | null;
	onCopyToolName?: (tool: string) => void;
	/** The companion's whole group is blocked (S4): its destroy rows dim and lock too. */
	blocked?: boolean;
}) {
	if (!companion.has_destroy_verbs) return null;
	const copy = enableDestroyToolsCopy(companion.companion_key, agentLabel);
	const enabled = companion.destroy_enabled;
	const switchLabel = `Destroy tools for ${companion.companion_key}`;

	return (
		<section
			data-slot="destroy-tools-group"
			data-destroy-enabled={enabled ? "true" : "false"}
			className="space-y-3"
		>
			<div className="flex items-center justify-between gap-2">
				<SectionLabel count={tools.length}>{DESTROY_GROUP_TITLE}</SectionLabel>
				{enabled ? (
					<Switch
						checked
						aria-label={switchLabel}
						onCheckedChange={() => {
							void onSetEnabled(false).catch(() => {});
						}}
					/>
				) : (
					<ConfirmAction
						title={copy.title}
						description={copy.description}
						confirmLabel={copy.confirmLabel}
						destructive={copy.destructive}
						onConfirm={() => onSetEnabled(true)}
					>
						<Switch checked={false} aria-label={switchLabel} />
					</ConfirmAction>
				)}
			</div>
			{enablementError ? (
				<p
					data-slot="destroy-enablement-error"
					className="text-destructive-muted-foreground text-xs"
				>
					{enablementError}
				</p>
			) : null}
			{enabled ? (
				<div className="rounded-lg border border-border">
					{tools.map((row) => (
						<CompanionToolRow
							key={row.tool}
							row={row}
							agentLabel={agentLabel}
							state={policyState(row.tool)}
							onModeChange={(mode) => onModeChange(row.tool, mode)}
							onCopyToolName={onCopyToolName}
							blocked={blocked}
						/>
					))}
				</div>
			) : (
				<EmptyState
					variant="inset"
					title={DESTROY_GROUP_EMPTY_COPY.title}
					description={DESTROY_GROUP_EMPTY_COPY.description}
				/>
			)}
		</section>
	);
}
