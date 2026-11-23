import type { components } from "@clawdi/shared/api";

/**
 * Approve and deny transitions for a held call.
 *
 * **Approval state is SERVER-OWNED.** Nothing here flips a card to approved:
 * a decision either settles and the list refetches, or it comes back as a
 * conflict and the card re-renders from the server's answer. An approval
 * raced between two browser tabs therefore never shows as approved twice —
 * the second tab sees the conflict, not a second success.
 *
 * **`resolved_target_count: null` means UN-PREVIEWED, not zero.** A dry run
 * that never happened has no target count, and collapsing that to `0` would
 * tell a reviewer that a delete touches nothing at the moment they authorize
 * it. Every path here keeps the null distinguishable.
 */

type Schemas = components["schemas"];

export type CompanionApproval = Schemas["CompanionApprovalResponse"];
export type CompanionApprovalList = Schemas["CompanionApprovalListResponse"];

export type ApprovalDecision = "approve" | "deny";

export const APPROVALS_COPY = {
	heading: "Waiting for you",
	approve: "Approve call",
	deny: "Deny call",
	reviewDetails: "Review details",
	approvedToast: "Call approved",
	deniedToast: "Call denied",
} as const;

export const APPROVALS_EMPTY_COPY = {
	title: "No calls waiting",
	description: "Calls that need your approval land here. Nothing runs while one waits.",
} as const;

export const APPROVAL_EXPIRED_COPY = "This approval expired. The agent has to ask again.";

export const APPROVAL_DECISION_ERROR_COPY =
	"That approval was not recorded. It may have expired or already been decided — reload to see where it stands.";

/** What a reviewer is told when the call was never previewed. */
export const UNPREVIEWED_TARGET_COPY = "Not previewed";

export interface ApprovalCardState {
	kind: "pending" | "expired" | "decided";
	canApprove: boolean;
	canDeny: boolean;
	/** The sentence shown in place of the buttons' usual affordance. */
	notice: string | null;
}

/**
 * Whether this card can still be decided.
 *
 * An expired-but-unswept approval disables BOTH buttons rather than offering
 * an action the server would answer with a conflict. The server refuses an
 * expired approval at decision time and at redemption alike, so a live button
 * would be a mode the server does not gate on.
 */
export function approvalCardState(approval: CompanionApproval, now: Date): ApprovalCardState {
	const expiresAt = new Date(approval.expires_at).getTime();
	const expired =
		approval.status === "expired" || (Number.isFinite(expiresAt) && expiresAt <= now.getTime());
	if (approval.status === "committed" || approval.status === "denied") {
		return { kind: "decided", canApprove: false, canDeny: false, notice: null };
	}
	if (expired) {
		return { kind: "expired", canApprove: false, canDeny: false, notice: APPROVAL_EXPIRED_COPY };
	}
	return { kind: "pending", canApprove: true, canDeny: true, notice: null };
}

export interface ApprovalDecisionState {
	/** The decision in flight, if any. Doubles as the synchronous lock. */
	pending: ApprovalDecision | null;
	error: string | null;
	/** Set once a conflict has been seen, so the card waits for the refetch. */
	conflicted: boolean;
}

export type ApprovalDecisionEvent =
	| { type: "submit"; decision: ApprovalDecision }
	| { type: "settled" }
	| { type: "conflict" }
	| { type: "failed" };

export function initialDecisionState(): ApprovalDecisionState {
	return { pending: null, error: null, conflicted: false };
}

/**
 * The decision transition machine.
 *
 * A second `submit` while one is in flight returns the SAME state object — the
 * double-submit lock, expressed where it can be tested rather than as a ref
 * nobody can observe. `ConfirmAction` holds an equivalent synchronous lock of
 * its own; this one covers the deny button, which has no dialog.
 *
 * Neither `settled` nor `conflict` marks the card approved. The list refetch
 * is the only thing that changes a card's status.
 */
export function decideApproval(
	state: ApprovalDecisionState,
	event: ApprovalDecisionEvent,
): ApprovalDecisionState {
	if (event.type === "submit") {
		if (state.pending !== null) return state;
		return { pending: event.decision, error: null, conflicted: false };
	}
	if (event.type === "settled") {
		return { pending: null, error: null, conflicted: false };
	}
	if (event.type === "conflict") {
		return { pending: null, error: APPROVAL_DECISION_ERROR_COPY, conflicted: true };
	}
	return { pending: null, error: APPROVAL_DECISION_ERROR_COPY, conflicted: false };
}

export interface ApprovalConfirmCopy {
	title: string;
	description: string;
	confirmLabel: string;
	destructive: boolean;
}

/**
 * The approve confirmation, from the UI spec's destructive-confirmation table.
 *
 * A `destroy`-class call swaps the confirm label to "Approve and run" with the
 * destructive variant. A `mutate` or `create` call names the cap debit — but
 * only when the call was previewed. An un-previewed call says so rather than
 * interpolating a zero it does not have.
 */
export function confirmCopyFor(
	approval: CompanionApproval,
	agentLabel: string,
	capUnit: string | null,
): ApprovalConfirmCopy {
	if (approval.tool_class === "destroy") {
		return {
			title: "Approve this delete",
			description: `${agentLabel} is asking to run ${approval.tool}. Approving runs it once, now. Review the arguments below before you decide.`,
			confirmLabel: "Approve and run",
			destructive: true,
		};
	}
	const debit =
		approval.resolved_target_count === null || !capUnit
			? "Approving runs it once, now. This call was not previewed, so how much of the budget it debits is not known yet."
			: `Approving runs it once, now, and debits ${approval.resolved_target_count} ${capUnit} from the current budget window.`;
	return {
		title: "Approve this call",
		description: `${agentLabel} is asking to run ${approval.tool}. ${debit}`,
		confirmLabel: APPROVALS_COPY.approve,
		destructive: false,
	};
}

/**
 * The cap-debit meta line on a card.
 *
 * Null is rendered as "Not previewed", never as `0`. The remaining budget
 * always travels with it, so a call the caps would refuse is visible as such
 * before anybody authorizes it.
 */
export function capDebitLabel(approval: CompanionApproval, capUnit: string | null): string {
	const unit = capUnit ?? "";
	const remaining = unit
		? `${approval.remaining_budget} ${unit} left`
		: `${approval.remaining_budget} left`;
	if (approval.resolved_target_count === null) {
		return `${UNPREVIEWED_TARGET_COPY} · ${remaining}`;
	}
	const target = unit
		? `${approval.resolved_target_count} ${unit}`
		: String(approval.resolved_target_count);
	return `${target} · ${remaining}`;
}

/**
 * The argument preview, as plain text.
 *
 * Companion-derived bytes are rendered as literal characters in a mono well —
 * never through a markdown renderer and never through React's raw-HTML escape
 * hatch. The reviewer is about to authorize a write, which makes them exactly
 * the wrong person to hand a rendering surface to. A committed grep gate in
 * `pending-approvals.states.test.tsx` asserts the absence of both by name; the
 * names are deliberately not spelled here so the gate stays greppable.
 */
export function formatArgumentsPreview(preview: CompanionApproval["arguments_preview"]): string {
	try {
		return JSON.stringify(preview, null, 2) ?? "{}";
	} catch {
		return "{}";
	}
}

/** Newest first — the order the region stacks its cards in. */
export function sortApprovalsNewestFirst(
	approvals: readonly CompanionApproval[],
): readonly CompanionApproval[] {
	return [...approvals].sort(
		(left, right) => new Date(right.created_at).getTime() - new Date(left.created_at).getTime(),
	);
}
