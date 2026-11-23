"use client";

import { toast } from "sonner";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { Separator } from "@/components/ui/separator";
import {
	Sheet,
	SheetContent,
	SheetDescription,
	SheetHeader,
	SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge } from "@/components/ui/status-badge";
import { useOpenApi } from "@/lib/api";
import { ApiError } from "@/lib/api-errors";
import {
	CALL_DETAIL_COPY,
	type CompanionToolCallDetail,
	confirmsApproval,
	decisionReasonSentence,
	dryRunSectionState,
	provenanceRows,
} from "./call-detail.logic";
import { DryRunDiff } from "./dry-run-diff";
import { ProvenanceChain } from "./provenance-chain";
import {
	type CompanionToolCall,
	capDebitLabel,
	decisionLabel,
	decisionTone,
} from "./tool-activity.logic";
import { toolClassTone } from "./tool-policy.logic";
import { UntrustedContentWell } from "./untrusted-content-well";

/**
 * The provenance drawer: one row becomes a readable chain.
 *
 * It opens with the table row ALREADY IN HAND and fetches the detail behind it,
 * so the session, the agent and the prompting reference are on screen
 * immediately and only the two detail-only rows, the dry-run diff and the
 * companion content arrive later. That ordering is the reason
 * `provenanceRows` distinguishes `pending` from `not-recorded`.
 *
 * The same drawer opens from a pending approval, which is the point: a reviewer
 * about to authorize a write should see the whole chain and the untrusted bytes
 * FIRST. When it is opened that way the caller passes `expectedApprovalId`, and
 * a detail whose `approval_id` does not match is not rendered at all — showing
 * somebody else's chain to a reviewer at that moment would be worse than
 * showing none.
 */
export function CallDetailSheet({
	callId,
	row,
	open,
	onOpenChange,
	agentId,
	agentLabel,
	expectedApprovalId,
	unlinked = false,
	onOpenCall,
}: {
	callId: string | null;
	/** The list row, when the drawer was opened from the table. */
	row: CompanionToolCall | null;
	open: boolean;
	onOpenChange: (open: boolean) => void;
	agentId?: string;
	agentLabel?: string;
	/** Set when opened from a pending approval; the detail must confirm it. */
	expectedApprovalId?: string;
	/** The approval could not be matched to any recorded call at all. */
	unlinked?: boolean;
	onOpenCall?: (callId: string) => void;
}) {
	const $api = useOpenApi();
	const detail = $api.useQuery(
		"get",
		"/v1/companions/activity/{call_id}",
		{ params: { path: { call_id: callId ?? "" } } },
		{ enabled: open && callId !== null },
	);

	function copy(value: string) {
		void navigator.clipboard?.writeText(value).then(
			() => toast.success("Copied"),
			() => toast.error("That value could not be copied."),
		);
	}

	const subject = row ?? detail.data ?? null;

	return (
		<Sheet open={open} onOpenChange={onOpenChange}>
			<SheetContent side="right" className="sm:max-w-xl">
				{/* The header lives HERE rather than in the body: `SheetTitle` and
				    `SheetDescription` are base-ui Dialog parts and read the Dialog
				    context, so a body that rendered them could not be rendered
				    standalone by a held-out test. The body owns the sections. */}
				<SheetHeader>
					<SheetTitle>{CALL_DETAIL_COPY.title}</SheetTitle>
					{subject ? (
						<SheetDescription className="font-mono text-xs">{subject.tool}</SheetDescription>
					) : null}
				</SheetHeader>
				<CallDetailBody
					row={row ?? detail.data ?? null}
					detail={detail.data ?? null}
					loading={detail.isLoading}
					error={detail.error}
					onRetry={() => void detail.refetch()}
					agentId={agentId}
					agentLabel={agentLabel}
					expectedApprovalId={expectedApprovalId}
					unlinked={unlinked}
					onCopy={copy}
					onOpenCall={onOpenCall}
					companionContent={
						detail.data?.companion_content ? (
							<UntrustedContentWell
								content={detail.data.companion_content}
								companionKey={detail.data.companion_key}
								verb={detail.data.verb}
							/>
						) : null
					}
				/>
			</SheetContent>
		</Sheet>
	);
}

/**
 * The drawer's body, as a pure function of its props.
 *
 * `Sheet` portals its content, so `renderToStaticMarkup` cannot see inside one.
 * Exporting the body separately is what puts every S5 state — loading, error,
 * partial, populated — inside the reach of a held-out test.
 */
export function CallDetailBody({
	row,
	detail,
	loading = false,
	error,
	onRetry,
	agentId,
	agentLabel,
	expectedApprovalId,
	onCopy,
	onOpenCall,
	companionContent,
	unlinked = false,
}: {
	row: Pick<
		CompanionToolCall,
		| "tool"
		| "tool_class"
		| "companion_key"
		| "verb"
		| "decision"
		| "dry_run"
		| "cap_unit"
		| "cap_consumed"
		| "cap_remaining_after"
		| "error_text"
		| "session_id"
		| "environment_id"
		| "source_content_ref"
	> | null;
	detail: CompanionToolCallDetail | null;
	loading?: boolean;
	error?: unknown;
	onRetry?: () => void;
	agentId?: string;
	agentLabel?: string;
	expectedApprovalId?: string;
	onCopy?: (value: string) => void;
	onOpenCall?: (callId: string) => void;
	/** The untrusted-content section. Supplied by the drawer; see plan 05-09 Task 3. */
	companionContent?: React.ReactNode;
	/**
	 * The approval this drawer was opened from could not be matched to any
	 * recorded call. Rendered as that sentence rather than as an empty chain.
	 */
	unlinked?: boolean;
}) {
	if (!row) {
		return (
			<div data-slot="call-detail-body" className="flex min-h-0 flex-col">
				<div className="space-y-2 px-4 pb-4">
					{error ? (
						<CallDetailError error={error} onRetry={onRetry} />
					) : unlinked ? (
						<p data-slot="call-detail-unconfirmed" className="text-muted-foreground text-sm">
							{CALL_DETAIL_COPY.reviewNotFound}
						</p>
					) : (
						<>
							<Skeleton className="h-4 w-56 max-w-full" />
							<Skeleton className="h-4 w-40 max-w-full" />
						</>
					)}
				</div>
			</div>
		);
	}

	const rows = provenanceRows(row, detail, { agentId, agentLabel });
	const dryRun = dryRunSectionState(row, loading || error ? null : detail);
	const guarantee =
		typeof detail?.dry_run_diff?.guarantee === "string" ? detail.dry_run_diff.guarantee : null;
	// `repeat_count` exists only on the detail response. Until it lands a
	// breaker row reads the count-less sentence, which is true, never an
	// unsubstituted count.
	const reasonSentence = decisionReasonSentence(row.decision, detail?.repeat_count ?? null);
	// Opened from an approval: render the chain only once the detail CONFIRMS it
	// is the call that approval names.
	const unconfirmed =
		unlinked ||
		(expectedApprovalId !== undefined &&
			detail !== null &&
			!confirmsApproval(detail, expectedApprovalId));

	return (
		<div data-slot="call-detail-body" className="flex min-h-0 flex-col">
			<div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 pb-4">
				{/* The confirmation gate covers the WHOLE body, not one section of it.
				    Every field below comes from the CANDIDATE row, and the candidate
				    was chosen by a timestamp heuristic (`matchApprovalToCall`,
				    WINDOWS #146) rather than by a recorded link — there is no
				    approval→call route and the listing row carries no `approval_id`.
				    The person reading this drawer is about to authorize a write, so
				    they must not be shown ANY of another call's data: not its
				    preview, not its bytes, not its decision and not its reason.
				    Showing them one sentence costs a trip to the activity log;
				    showing them the wrong call costs them the write. */}
				{unconfirmed ? (
					<p data-slot="call-detail-unconfirmed" className="text-muted-foreground text-sm">
						{CALL_DETAIL_COPY.reviewNotFound}
					</p>
				) : (
					<>
						{/* The tool name, so the body is readable on its own as well as
						    beneath the Sheet header that also carries it. */}
						<p data-slot="call-detail-tool" className="font-mono text-xs" title={row.tool}>
							{row.tool}
						</p>
						{/* 1 — Decision. */}
						<section data-slot="call-decision" className="space-y-2">
							<h3 className="font-semibold text-sm">{CALL_DETAIL_COPY.decisionHeading}</h3>
							<div className="flex flex-wrap items-center gap-2">
								<StatusBadge status={toolClassTone(row.tool_class)} className="font-mono">
									{row.tool_class}
								</StatusBadge>
								<StatusBadge status={decisionTone(row.decision)} data-decision={row.decision}>
									{decisionLabel(row.decision)}
								</StatusBadge>
								{row.dry_run ? <StatusBadge status="info">Dry run</StatusBadge> : null}
							</div>
							{/* The reason wraps here rather than truncating: the table cell is
							    where the 160-character convention applies, and the reviewer in
							    this drawer is the one about to authorize the write. A Phase 6
							    refusal (S7) renders its fixed sentence INSTEAD of the recorded
							    reason, which was written for the agent. */}
							{reasonSentence !== null || row.error_text ? (
								<p data-slot="call-decision-reason" className="text-sm break-words">
									{reasonSentence ?? row.error_text}
								</p>
							) : null}
							<p data-slot="call-cap-debit" className="text-muted-foreground text-xs tabular-nums">
								{capDebitLabel(row)}
								{row.cap_remaining_after !== null ? `, ${row.cap_remaining_after} left` : ""}
							</p>
						</section>

						{error ? (
							<>
								<Separator />
								<CallDetailError error={error} onRetry={onRetry} />
							</>
						) : null}

						{/* 2 — Provenance. */}
						<Separator />
						<ProvenanceChain rows={rows} onCopy={onCopy} onOpenCall={onOpenCall} />

						{/* 3 — What would change. Omitted entirely on a non-dry-run call. */}
						{dryRun.kind === "absent" ? null : (
							<>
								<Separator />
								<DryRunDiff state={dryRun} guarantee={guarantee} />
							</>
						)}

						{/* 4 — Companion content, inside the untrusted-content well. */}
						{companionContent ? (
							<>
								<Separator />
								{companionContent}
							</>
						) : null}
					</>
				)}
			</div>
		</div>
	);
}

/**
 * A failed detail fetch, with the 404 called what it is.
 *
 * The route answers a row belonging to somebody else with 404 rather than 403 on
 * purpose — a 403 would confirm the row exists — so this renders "not found" and
 * never anything that reads as "forbidden".
 */
export function CallDetailError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
	if (error instanceof ApiError && error.status === 404) {
		return (
			<p data-slot="call-detail-not-found" className="text-muted-foreground text-sm">
				{CALL_DETAIL_COPY.notFound}
			</p>
		);
	}
	return <ApiErrorPanel error={error} onRetry={onRetry} title={CALL_DETAIL_COPY.errorTitle} />;
}
