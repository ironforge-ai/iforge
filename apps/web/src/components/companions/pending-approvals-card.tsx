"use client";

import { useRef, useState } from "react";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { EmptyState } from "@/components/empty-state";
import { SectionLabel } from "@/components/section-label";
import { TimeTooltip } from "@/components/time-tooltip";
import { TruncatedText } from "@/components/truncated-text";
import { Button } from "@/components/ui/button";
import { ConfirmAction } from "@/components/ui/confirm-action";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import {
	APPROVALS_COPY,
	APPROVALS_EMPTY_COPY,
	type ApprovalDecision,
	approvalCardState,
	type CompanionApproval,
	capDebitLabel,
	confirmCopyFor,
	decideApproval,
	formatArgumentsPreview,
	initialDecisionState,
	sortApprovalsNewestFirst,
} from "./approval-decision.logic";
import { toolClassTone } from "./tool-policy.logic";

/**
 * The pending-approvals region: stacked cards, newest first, pinned to the top
 * of the surface because approvals are the only region with a deadline.
 *
 * With nothing pending there is no approve or deny control anywhere — the
 * buttons exist on a card or not at all, never as a disabled standing
 * affordance that implies something is waiting.
 */
export function PendingApprovalsRegion({
	approvals,
	agentLabel,
	capUnitFor,
	loading = false,
	error,
	onRetry,
	onDecide,
	onReviewDetails,
	now = new Date(),
}: {
	approvals: readonly CompanionApproval[];
	agentLabel: string;
	/** The verb's cap unit, joined from the tool catalog. Null when unknown. */
	capUnitFor: (tool: string) => string | null;
	loading?: boolean;
	error?: unknown;
	onRetry?: () => void;
	/** Resolves when the server recorded the decision; rejects otherwise. */
	onDecide: (approval: CompanionApproval, decision: ApprovalDecision) => Promise<unknown>;
	onReviewDetails?: (approval: CompanionApproval) => void;
	now?: Date;
}) {
	if (error) {
		return (
			<section data-slot="pending-approvals" className="space-y-3">
				<SectionLabel>{APPROVALS_COPY.heading}</SectionLabel>
				<ApiErrorPanel error={error} onRetry={onRetry} />
			</section>
		);
	}

	if (loading) {
		return (
			<section data-slot="pending-approvals" className="space-y-3">
				<SectionLabel>{APPROVALS_COPY.heading}</SectionLabel>
				<div className="space-y-3">
					{Array.from({ length: 2 }).map((_, index) => (
						<div
							key={index}
							data-slot="pending-approval-skeleton"
							aria-hidden="true"
							className="space-y-3 rounded-lg border border-border p-4"
						>
							<Skeleton className="h-5 w-56 max-w-full" />
							<Skeleton className="h-4 w-72 max-w-full" />
							<Skeleton className="h-16 w-full" />
							<Skeleton className="h-9 w-48" />
						</div>
					))}
				</div>
			</section>
		);
	}

	const ordered = sortApprovalsNewestFirst(approvals);

	return (
		<section data-slot="pending-approvals" className="space-y-3">
			<SectionLabel count={ordered.length}>{APPROVALS_COPY.heading}</SectionLabel>
			{ordered.length === 0 ? (
				<EmptyState
					variant="inset"
					title={APPROVALS_EMPTY_COPY.title}
					description={APPROVALS_EMPTY_COPY.description}
				/>
			) : (
				<div className="space-y-3">
					{ordered.map((approval) => (
						<PendingApprovalCard
							key={approval.id}
							approval={approval}
							agentLabel={agentLabel}
							capUnit={capUnitFor(approval.tool)}
							onDecide={(decision) => onDecide(approval, decision)}
							onReviewDetails={onReviewDetails ? () => onReviewDetails(approval) : undefined}
							now={now}
						/>
					))}
				</div>
			)}
		</section>
	);
}

/** One held call, and the only place it can be approved or denied. */
export function PendingApprovalCard({
	approval,
	agentLabel,
	capUnit,
	onDecide,
	onReviewDetails,
	now = new Date(),
}: {
	approval: CompanionApproval;
	agentLabel: string;
	capUnit: string | null;
	onDecide: (decision: ApprovalDecision) => Promise<unknown>;
	onReviewDetails?: () => void;
	now?: Date;
}) {
	const [state, setState] = useState(initialDecisionState);
	// Synchronous lock: `disabled` only takes effect on the next render, which
	// leaves a sub-frame window where a fast double-click could submit twice.
	// `decideApproval`'s own pending guard is the same rule, stated where it
	// can be tested.
	const locked = useRef(false);
	const card = approvalCardState(approval, now);
	const confirm = confirmCopyFor(approval, agentLabel, capUnit);

	async function decide(decision: ApprovalDecision) {
		if (locked.current) return;
		locked.current = true;
		setState((previous) => decideApproval(previous, { type: "submit", decision }));
		try {
			await onDecide(decision);
			setState((previous) => decideApproval(previous, { type: "settled" }));
		} catch (cause) {
			// Server-owned: a conflict or an expiry is answered by the refetch,
			// never by flipping this card to approved.
			setState((previous) => decideApproval(previous, { type: "conflict" }));
			throw cause;
		} finally {
			locked.current = false;
		}
	}

	return (
		<article
			data-slot="pending-approval-card"
			data-approval={approval.id}
			data-state={card.kind}
			className="space-y-3 rounded-lg border border-border p-4"
		>
			<div className="flex min-w-0 flex-wrap items-center gap-2">
				<StatusBadge status={toolClassTone(approval.tool_class)} className="font-mono">
					{approval.tool_class}
				</StatusBadge>
				<TruncatedText className="min-w-0 max-w-full font-mono text-xs">
					{approval.tool}
				</TruncatedText>
				<StatusBadge status="warning">Waiting for you</StatusBadge>
			</div>
			<div className="flex flex-wrap items-center gap-2 text-muted-foreground text-xs">
				<span>{agentLabel}</span>
				<span>·</span>
				<span className="font-mono">{approval.companion_key}</span>
				<span>·</span>
				<span className="tabular-nums">{capDebitLabel(approval, capUnit)}</span>
				<span>·</span>
				<TimeTooltip value={approval.expires_at}>
					<span>Expires {new Date(approval.expires_at).toLocaleTimeString()}</span>
				</TimeTooltip>
			</div>
			{/* Companion-derived bytes as literal characters in a mono well —
			    never markdown, never HTML, never auto-linkified. */}
			<TruncatedText
				className="block w-full rounded-md bg-muted/30 p-4 font-mono text-xs"
				title={formatArgumentsPreview(approval.arguments_preview)}
			>
				{formatArgumentsPreview(approval.arguments_preview)}
			</TruncatedText>
			{card.notice ? (
				<p data-slot="approval-notice" className="text-muted-foreground text-xs">
					{card.notice}
				</p>
			) : null}
			{state.error ? (
				<p data-slot="approval-error" className="text-destructive-muted-foreground text-xs">
					{state.error}
				</p>
			) : null}
			<div className="flex flex-wrap items-center gap-2">
				{onReviewDetails ? (
					<Button type="button" variant="ghost" size="sm" onClick={onReviewDetails}>
						{APPROVALS_COPY.reviewDetails}
					</Button>
				) : null}
				<Button
					type="button"
					variant="ghost"
					size="sm"
					className="text-destructive"
					disabled={!card.canDeny || state.pending !== null}
					onClick={() => {
						void decide("deny").catch(() => {});
					}}
				>
					{APPROVALS_COPY.deny}
				</Button>
				{card.canApprove ? (
					<ConfirmAction
						title={confirm.title}
						description={confirm.description}
						confirmLabel={confirm.confirmLabel}
						destructive={confirm.destructive}
						onConfirm={() => decide("approve")}
					>
						<Button type="button" size="sm" disabled={state.pending !== null}>
							{APPROVALS_COPY.approve}
						</Button>
					</ConfirmAction>
				) : (
					<Button type="button" size="sm" disabled>
						{APPROVALS_COPY.approve}
					</Button>
				)}
			</div>
		</article>
	);
}
