import { describe, expect, test } from "bun:test";
import {
	APPROVAL_DECISION_ERROR_COPY,
	APPROVAL_EXPIRED_COPY,
	APPROVALS_COPY,
	approvalCardState,
	type CompanionApproval,
	capDebitLabel,
	confirmCopyFor,
	decideApproval,
	formatArgumentsPreview,
	initialDecisionState,
	sortApprovalsNewestFirst,
	UNPREVIEWED_TARGET_COPY,
} from "./approval-decision.logic";

const NOW = new Date("2026-09-16T08:30:00Z");
const AGENT_LABEL = "Research Agent";

function approval(overrides: Partial<CompanionApproval> = {}): CompanionApproval {
	return {
		id: "b1d0c4f6-0000-4000-8000-000000000001",
		tool: "filebrowser__move_path",
		tool_class: "mutate",
		companion_key: "filebrowser",
		verb: "move_path",
		arguments_preview: { from: "/a", to: "/b" },
		resolved_target_count: 12,
		remaining_budget: 188,
		status: "issued",
		expires_at: "2026-09-16T08:35:00Z",
		decided_at: null,
		created_at: "2026-09-16T08:29:00Z",
		...overrides,
	};
}

describe("approvalCardState", () => {
	test("an unexpired issued approval can be approved and denied", () => {
		const state = approvalCardState(approval(), NOW);
		expect(state.kind).toBe("pending");
		expect(state.canApprove).toBe(true);
		expect(state.canDeny).toBe(true);
		expect(state.notice).toBeNull();
	});

	test("an expired-but-unswept approval disables BOTH buttons and says why", () => {
		const byClock = approvalCardState(approval({ expires_at: "2026-09-16T08:29:30Z" }), NOW);
		expect(byClock.kind).toBe("expired");
		expect(byClock.canApprove).toBe(false);
		expect(byClock.canDeny).toBe(false);
		expect(byClock.notice).toBe(APPROVAL_EXPIRED_COPY);
		expect(APPROVAL_EXPIRED_COPY).toBe("This approval expired. The agent has to ask again.");

		// The server's own status says the same thing; either is enough.
		const byStatus = approvalCardState(approval({ status: "expired" }), NOW);
		expect(byStatus.kind).toBe("expired");
	});

	test("an already-decided approval offers no action at all", () => {
		for (const status of ["committed", "denied"] as const) {
			const state = approvalCardState(approval({ status }), NOW);
			expect(state.kind).toBe("decided");
			expect(state.canApprove).toBe(false);
			expect(state.canDeny).toBe(false);
		}
	});
});

describe("decideApproval", () => {
	test("a conflict does NOT flip the card to approved", () => {
		const submitted = decideApproval(initialDecisionState(), {
			type: "submit",
			decision: "approve",
		});
		const conflicted = decideApproval(submitted, { type: "conflict" });
		expect(conflicted.pending).toBeNull();
		expect(conflicted.conflicted).toBe(true);
		expect(conflicted.error).toBe(APPROVAL_DECISION_ERROR_COPY);
		// There is no "approved" in this state at all — the card's status comes
		// from the refetch, which is what stops two tabs approving twice.
		expect(Object.keys(conflicted)).toEqual(["pending", "error", "conflicted"]);
	});

	test("a second submit inside one frame is blocked", () => {
		const first = decideApproval(initialDecisionState(), { type: "submit", decision: "approve" });
		const second = decideApproval(first, { type: "submit", decision: "deny" });
		expect(second).toBe(first);
		expect(second.pending).toBe("approve");
	});

	test("a settled decision clears the lock and any previous error", () => {
		const failed = decideApproval(
			decideApproval(initialDecisionState(), { type: "submit", decision: "deny" }),
			{ type: "failed" },
		);
		expect(failed.error).toBe(APPROVAL_DECISION_ERROR_COPY);
		const retried = decideApproval(failed, { type: "submit", decision: "deny" });
		const settled = decideApproval(retried, { type: "settled" });
		expect(settled.pending).toBeNull();
		expect(settled.error).toBeNull();
		expect(settled.conflicted).toBe(false);
	});

	test("the rejection sentence is the spec's, with no exclamation and no toast wording", () => {
		expect(APPROVAL_DECISION_ERROR_COPY).toBe(
			"That approval was not recorded. It may have expired or already been decided — reload to see where it stands.",
		);
		expect(APPROVAL_DECISION_ERROR_COPY).not.toContain("!");
	});
});

describe("confirmCopyFor", () => {
	test("a destroy-class call swaps the label and the variant", () => {
		const copy = confirmCopyFor(approval({ tool_class: "destroy" }), AGENT_LABEL, "records");
		expect(copy.title).toBe("Approve this delete");
		expect(copy.confirmLabel).toBe("Approve and run");
		expect(copy.destructive).toBe(true);
		expect(copy.description).toContain("Review the arguments below before you decide.");
	});

	test("a mutate or create call names the cap debit it will take", () => {
		const copy = confirmCopyFor(approval({ resolved_target_count: 12 }), AGENT_LABEL, "records");
		expect(copy.title).toBe("Approve this call");
		expect(copy.confirmLabel).toBe(APPROVALS_COPY.approve);
		expect(copy.destructive).toBe(false);
		expect(copy.description).toContain("debits 12 records from the current budget window");
		// The debit this sentence names lands on the WINDOW ledger, keyed by (environment_id,
		// companion_key, window_started_at), which every call debits first. Since plan 05-10 the
		// MCP session id DOES reach the facade, so a call that carries one also debits a
		// session-keyed row (WINDOWS #195 corrected the older claim that `session_id` is always
		// null). The window is still the ceiling every call meets, and `CompanionSessionBudget` is
		// the model's name, not the scope it meters -- copy that said "this session's budget" would
		// name the narrower of the two ledgers, in the one sentence telling a reviewer what
		// approving costs.
		expect(copy.description).not.toContain("this session");
		expect(copy.description).toContain(AGENT_LABEL);
		expect(copy.description).toContain("filebrowser__move_path");
	});

	test("an UN-PREVIEWED call says so rather than interpolating a zero", () => {
		// `resolved_target_count: null` means no dry run happened. Collapsing it
		// to 0 would tell a reviewer a write touches nothing.
		const copy = confirmCopyFor(approval({ resolved_target_count: null }), AGENT_LABEL, "records");
		expect(copy.description).toContain("was not previewed");
		expect(copy.description).not.toContain("debits 0");
		expect(copy.description).not.toContain("0 records");
	});

	test("an unknown cap unit never invents one", () => {
		const copy = confirmCopyFor(approval(), AGENT_LABEL, null);
		expect(copy.description).toContain("was not previewed");
		expect(copy.description).not.toContain("undefined");
		expect(copy.description).not.toContain("null");
	});
});

describe("capDebitLabel", () => {
	test("a previewed call names the target count and the remaining budget", () => {
		expect(capDebitLabel(approval(), "records")).toBe("12 records · 188 records left");
	});

	test("an un-previewed call reads 'Not previewed', never 0", () => {
		const label = capDebitLabel(approval({ resolved_target_count: null }), "records");
		expect(label).toContain(UNPREVIEWED_TARGET_COPY);
		expect(label).toContain("188 records left");
		expect(label).not.toMatch(/(^|\s)0 records(\s|$)/);
	});

	test("a call whose remaining budget is zero still shows it, never hides the refusal", () => {
		expect(capDebitLabel(approval({ remaining_budget: 0 }), "records")).toContain("0 records left");
	});
});

describe("argument previews", () => {
	test("render as literal characters, never as markup", () => {
		const preview = formatArgumentsPreview({ path: "<script>alert(1)</script>" });
		expect(preview).toContain("<script>alert(1)</script>");
		expect(preview).toContain('"path"');
	});

	test("an unserializable preview degrades to an empty object rather than throwing", () => {
		const circular: Record<string, unknown> = {};
		circular.self = circular;
		expect(formatArgumentsPreview(circular as CompanionApproval["arguments_preview"])).toBe("{}");
	});
});

describe("ordering", () => {
	test("newest first", () => {
		const older = approval({ id: "older", created_at: "2026-09-16T08:00:00Z" });
		const newer = approval({ id: "newer", created_at: "2026-09-16T08:29:00Z" });
		expect(sortApprovalsNewestFirst([older, newer]).map((a) => a.id)).toEqual(["newer", "older"]);
		// The input is not mutated — the query cache owns that array.
		const input = [older, newer];
		sortApprovalsNewestFirst(input);
		expect(input.map((a) => a.id)).toEqual(["older", "newer"]);
	});
});
