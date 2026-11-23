/**
 * The drawer's three load-bearing rules, asserted without opening a portal.
 *
 * `provenanceRows` decides what a reviewer is told about each link; the whole
 * point of the tests below is that "unrecorded", "still loading" and "there was
 * none" stay three different answers. `dryRunSectionState` decides whether a
 * preview is shown as clean, and the case that matters is the one where it must
 * NOT be. `truncateReason` is where the table's convention and the Sheet's
 * wrapping part company.
 */

import { describe, expect, test } from "bun:test";
import { fileURLToPath } from "node:url";
import { uiSpecCopy } from "@/lib/ui-spec-contract";
import {
	CALL_DETAIL_COPY,
	type CompanionToolCallDetail,
	confirmsApproval,
	DECISION_REASON_COPY,
	decisionReasonSentence,
	diffEntries,
	dryRunFailureCopy,
	dryRunFailureReason,
	dryRunSectionState,
	matchApprovalToCall,
	PROMPTED_BY_DISCLOSURE,
	PROVENANCE_ROW_KINDS,
	promptedByDisclosure,
	provenanceRows,
	REASON_DISPLAY_MAX,
	shortIdentifier,
	truncateReason,
} from "./call-detail.logic";
import {
	CALL_DECISIONS,
	type CallDecision,
	type CompanionToolCall,
	NOT_RECORDED,
} from "./tool-activity.logic";

const AGENT_ID = "6c3b1a2e-0000-4000-8000-000000000001";

function row(overrides: Partial<CompanionToolCall> = {}): CompanionToolCall {
	return {
		id: "0b7f0f1e-0000-4000-8000-000000000001",
		created_at: "2026-09-17T09:00:00Z",
		session_id: "mcp-session-1",
		environment_id: AGENT_ID,
		tool: "filebrowser__delete_path",
		tool_class: "destroy",
		companion_key: "filebrowser",
		verb: "delete_path",
		decision: "dry_run_completed",
		dry_run: true,
		cap_unit: "calls",
		cap_consumed: 1,
		cap_remaining_after: 199,
		error_text: null,
		parent_call_id: null,
		source_content_ref: "call-jti-abcdef",
		...overrides,
	};
}

function detail(overrides: Partial<CompanionToolCallDetail> = {}): CompanionToolCallDetail {
	return {
		...row(),
		arguments_digest: "sha256:deadbeef",
		arguments_preview: { path: "/photos/2026" },
		call_jti: "01JC0000000000000000000001",
		approval_id: null,
		approved_by_user_id: null,
		source_content_disclosure: null,
		dry_run_diff: null,
		companion_content: null,
		repeat_count: null,
		...overrides,
	};
}

const CLEAN_DIFF = {
	tool: "filebrowser__delete_path",
	toolClass: "destroy",
	target: "/photos/2026",
	targetCount: 0,
	before: [],
	after: [],
	undetermined: [],
	guarantee: "This is a point-in-time read of the companion, not a guarantee.",
};

describe("provenanceRows", () => {
	test("returns the six rows in the fixed order, top-down as one path", () => {
		const rows = provenanceRows(row({ parent_call_id: "parent" }), detail({ parent_call_id: "p" }));
		expect(rows.map((r) => r.kind)).toEqual([...PROVENANCE_ROW_KINDS]);
		expect(rows.map((r) => r.label)).toEqual([
			"Session",
			"Agent",
			"Prompted by",
			"Arguments",
			"Call identifier",
			"Parent call",
		]);
	});

	test("omits the parent row entirely when the call is not a retry after approval", () => {
		const rows = provenanceRows(row(), detail({ parent_call_id: null }));
		expect(rows.map((r) => r.kind)).not.toContain("parent");
		expect(rows).toHaveLength(5);
	});

	test("the parent row carries a link only when there is a parent to link to", () => {
		const withParent = provenanceRows(row(), detail({ parent_call_id: "call-2" }));
		expect(withParent.at(-1)?.linkCallId).toBe("call-2");
		for (const r of provenanceRows(row(), detail())) {
			expect(r.linkCallId).toBeUndefined();
		}
	});

	test("every unrecorded link renders the muted Not recorded — never blank, never invented", () => {
		const rows = provenanceRows(
			row({ session_id: null, environment_id: null, source_content_ref: null }),
			detail({ arguments_preview: {}, call_jti: null }),
		);
		expect(rows).toHaveLength(5);
		for (const r of rows) {
			expect(r.state).toBe("not-recorded");
			expect(r.value).toBeNull();
			// Never blank, and never a value the row did not carry.
			expect(r.display).toBe(NOT_RECORDED);
		}
	});

	test("no code path produces a blank display or a fabricated identifier", () => {
		const cases: CompanionToolCall[] = [
			row(),
			row({ session_id: null }),
			row({ environment_id: null }),
			row({ source_content_ref: null }),
			row({ session_id: null, environment_id: null, source_content_ref: null }),
		];
		for (const candidate of cases) {
			for (const r of provenanceRows(candidate, detail())) {
				if (r.state === "recorded") {
					// A displayed value is always one the row or the detail carried.
					expect(r.display).toBe(r.value as string);
					expect(r.display.trim()).not.toBe("");
				} else {
					expect(r.display).toBe(NOT_RECORDED);
				}
			}
		}
	});

	test("pending is not unrecorded: the detail-only rows wait rather than claiming absence", () => {
		const rows = provenanceRows(row(), null);
		const argumentsRow = rows.find((r) => r.kind === "arguments");
		const callIdRow = rows.find((r) => r.kind === "call-id");
		expect(argumentsRow?.state).toBe("pending");
		expect(callIdRow?.state).toBe("pending");
		expect(argumentsRow?.display).not.toBe(NOT_RECORDED);
		// The rows the table already has stay recorded while the detail is in flight.
		expect(rows.find((r) => r.kind === "session")?.state).toBe("recorded");
	});

	test("the Agent row names this page's agent and falls back to the raw id otherwise", () => {
		const named = provenanceRows(row(), detail(), {
			agentId: AGENT_ID,
			agentLabel: "Research Agent",
		});
		expect(named.find((r) => r.kind === "agent")?.display).toBe("Research Agent");

		const other = provenanceRows(row({ environment_id: "another-agent" }), detail(), {
			agentId: AGENT_ID,
			agentLabel: "Research Agent",
		});
		expect(other.find((r) => r.kind === "agent")?.display).toBe("another-agent");
	});
});

describe("the best-effort disclosure", () => {
	test("renders unconditionally — with a resolved prompting reference and without one", () => {
		const withRef = provenanceRows(row({ source_content_ref: "call-1" }), detail());
		const withoutRef = provenanceRows(row({ source_content_ref: null }), detail());
		expect(withRef.find((r) => r.kind === "prompted-by")?.note).toBe(PROMPTED_BY_DISCLOSURE);
		expect(withoutRef.find((r) => r.kind === "prompted-by")?.note).toBe(PROMPTED_BY_DISCLOSURE);
	});

	test("prefers the server's own sentence verbatim when it sent one", () => {
		// Plan 05-07 ships `source_content_disclosure` beside the reference for
		// exactly this reason: no second sentence is written in the web package.
		const served = "Best-effort correlation: the server observed the ordering only.";
		expect(promptedByDisclosure({ source_content_disclosure: served })).toBe(served);
		expect(promptedByDisclosure({ source_content_disclosure: null })).toBe(PROMPTED_BY_DISCLOSURE);
		expect(promptedByDisclosure(null)).toBe(PROMPTED_BY_DISCLOSURE);
	});

	test("the disclosure never asserts causation", () => {
		expect(PROMPTED_BY_DISCLOSURE.toLowerCase()).toContain("best-effort");
		expect(PROMPTED_BY_DISCLOSURE.toLowerCase()).not.toContain("caused");
	});
});

describe("dryRunSectionState", () => {
	test("absent: a non-dry-run call omits the whole section", () => {
		expect(dryRunSectionState(row({ dry_run: false }), detail())).toEqual({ kind: "absent" });
	});

	test("clean: an empty diff is 'nothing would change', with the exact sentence", () => {
		const state = dryRunSectionState(row(), detail({ dry_run_diff: CLEAN_DIFF }));
		expect(state.kind).toBe("clean");
		expect(CALL_DETAIL_COPY.dryRunClean).toBe("Nothing would change.");
	});

	test("partial: undetermined entries are stated, and the diff is never shown as complete", () => {
		const state = dryRunSectionState(
			row(),
			detail({
				dry_run_diff: {
					...CLEAN_DIFF,
					before: [{ name: "a.jpg" }],
					after: [],
					undetermined: ["the contents of /photos/2026/raw"],
				},
			}),
		);
		expect(state.kind).toBe("partial");
		if (state.kind !== "partial") throw new Error("unreachable");
		expect(state.undetermined).toEqual(["the contents of /photos/2026/raw"]);
		expect(state.entries).toHaveLength(1);
	});

	test("failed: a null diff is 'nobody looked', and renders the failure copy, not a diff", () => {
		const state = dryRunSectionState(
			row({ decision: "refused_policy" }),
			detail({
				dry_run_diff: null,
				error_text: "filebrowser__delete_path could not be previewed: the probe did not answer",
			}),
		);
		expect(state.kind).toBe("failed");
		if (state.kind !== "failed") throw new Error("unreachable");
		expect(state.reason).toBe("the probe did not answer");
		expect(dryRunFailureCopy(state.reason)).toBe(
			"This would fail before it changed anything: the probe did not answer",
		);
		// The failed branch carries no entries at all — there is nothing for a
		// renderer to mistake for a clean preview.
		expect("entries" in state).toBe(false);
	});

	test("a null diff and an EMPTY diff are different answers", () => {
		const nobodyLooked = dryRunSectionState(row(), detail({ dry_run_diff: null }));
		const nothingChanges = dryRunSectionState(row(), detail({ dry_run_diff: CLEAN_DIFF }));
		expect(nobodyLooked.kind).toBe("failed");
		expect(nothingChanges.kind).toBe("clean");
		expect(nobodyLooked.kind).not.toBe(nothingChanges.kind);
	});

	test("loading: a dry run whose detail has not landed is not yet any of the other four", () => {
		expect(dryRunSectionState(row(), null)).toEqual({ kind: "loading" });
	});

	test("populated: entries pair before against after, and a missing side stays null", () => {
		const state = dryRunSectionState(
			row(),
			detail({
				dry_run_diff: { ...CLEAN_DIFF, before: [{ name: "a.jpg" }, { name: "b.jpg" }], after: [] },
			}),
		);
		expect(state.kind).toBe("populated");
		if (state.kind !== "populated") throw new Error("unreachable");
		expect(state.entries).toHaveLength(2);
		expect(state.entries[0]?.after).toBeNull();
		expect(state.entries[0]?.before).toContain("a.jpg");
	});

	test("one entry and many entries use the same shape — no single-entry special case", () => {
		const one = diffEntries({ ...CLEAN_DIFF, before: [{ n: 1 }], after: [{ n: 2 }] });
		const many = diffEntries({
			...CLEAN_DIFF,
			before: [{ n: 1 }, { n: 3 }],
			after: [{ n: 2 }, { n: 4 }],
		});
		expect(Object.keys(one[0] ?? {})).toEqual(Object.keys(many[0] ?? {}));
	});

	test("a failed preview with no recorded reason says so instead of inventing one", () => {
		expect(dryRunFailureReason(null)).toBeNull();
		expect(dryRunFailureCopy(null)).toBe(CALL_DETAIL_COPY.dryRunFailedWithoutReason);
	});

	test("a reason that does not match the façade's prose is used whole rather than guessed at", () => {
		expect(dryRunFailureReason("something else entirely")).toBe("something else entirely");
	});
});

describe("truncateReason", () => {
	test("leaves a 159- and a 160-character reason whole", () => {
		expect(truncateReason("x".repeat(159))).toHaveLength(159);
		expect(truncateReason("x".repeat(160))).toHaveLength(160);
		expect(truncateReason("x".repeat(160))).not.toContain("…");
	});

	test("truncates at 161 characters, per the project's existing convention", () => {
		const truncated = truncateReason("x".repeat(161));
		expect(truncated).toHaveLength(REASON_DISPLAY_MAX);
		expect(truncated.endsWith("…")).toBe(true);
	});

	test("the Sheet copy is NOT routed through it — the reviewer needs the sentence", () => {
		// `dryRunFailureCopy` is the Sheet's sentence and applies no bound.
		const long = "y".repeat(400);
		expect(dryRunFailureCopy(long)).toContain(long);
	});
});

describe("shortIdentifier", () => {
	test("truncates to a twelve-character prefix, leaving short values alone", () => {
		expect(shortIdentifier("01JC0000000000000000000001")).toBe("01JC00000000…");
		expect(shortIdentifier("short")).toBe("short");
	});
});

describe("linking a pending approval to its recorded call", () => {
	const approval = { tool: "filebrowser__delete_path", created_at: "2026-09-17T09:00:00Z" };

	test("picks the pending-approval row for the same tool recorded closest in time", () => {
		const id = matchApprovalToCall(
			[
				row({ id: "far", created_at: "2026-09-17T08:00:00Z" }),
				row({ id: "near", created_at: "2026-09-17T09:00:01Z" }),
			].map((r) => ({ ...r, decision: "pending_approval" as const })),
			approval,
		);
		expect(id).toBe("near");
	});

	test("ignores rows with a different decision or a different tool", () => {
		expect(
			matchApprovalToCall(
				[row({ id: "a", decision: "allowed" }), row({ id: "b", tool: "filebrowser__list_dir" })],
				approval,
			),
		).toBeNull();
	});

	test("a candidate is only a candidate — the detail's approval_id is what confirms it", () => {
		expect(confirmsApproval({ approval_id: "approval-1" }, "approval-1")).toBe(true);
		expect(confirmsApproval({ approval_id: "approval-2" }, "approval-1")).toBe(false);
		expect(confirmsApproval({ approval_id: null }, "approval-1")).toBe(false);
		expect(confirmsApproval(null, "approval-1")).toBe(false);
	});
});

describe("a foreign row is not found, never forbidden", () => {
	test("the copy says not found and never says forbidden", () => {
		expect(CALL_DETAIL_COPY.notFound.toLowerCase()).toContain("not in this log");
		expect(CALL_DETAIL_COPY.notFound.toLowerCase()).not.toContain("forbidden");
		expect(CALL_DETAIL_COPY.notFound.toLowerCase()).not.toContain("permission");
	});
});

/** The phase-06 UI contract, resolved from this module rather than the cwd. */
const PHASE_06_SPEC = fileURLToPath(
	new URL(
		"../../../../../.planning/phases/06-first-companion-end-to-end/06-UI-SPEC.md",
		import.meta.url,
	),
);

/** Widen a literal-typed constant so it compares with a string read from the spec. */
const shipped = (value: string): string => value;

const OVER_BUDGET_ROW = "S7 over-budget refusal reason (detail sheet)";
const BREAKER_ROW = "S7 breaker refusal reason (detail sheet)";

/** The three decisions Phase 6 added, each with its own fixed sentence. */
const PHASE_6_REFUSALS: readonly CallDecision[] = [
	"refused_budget_ceiling",
	"refused_repeated_call",
	"refused_group_disabled",
];

describe("decisionReasonSentence (06-UI-SPEC S7)", () => {
	test("the budget refusal reads the contract's over-budget sentence", () => {
		expect(decisionReasonSentence("refused_budget_ceiling", null)).toBe(
			uiSpecCopy(OVER_BUDGET_ROW, PHASE_06_SPEC),
		);
		expect(shipped(DECISION_REASON_COPY.budgetCeiling)).toBe(
			uiSpecCopy(OVER_BUDGET_ROW, PHASE_06_SPEC),
		);
	});

	test("the breaker names its repetition count, in the contract's words", () => {
		expect(decisionReasonSentence("refused_repeated_call", 6)).toBe(
			uiSpecCopy(BREAKER_ROW, PHASE_06_SPEC).replace("{n}", "6"),
		);
		expect(decisionReasonSentence("refused_repeated_call", 6)).toBe(
			"Stopped: this call repeated 6 times with the same arguments. The loop was broken before it spent more of your budget.",
		);
	});

	test("partial: a breaker with no count drops the count clause — no undefined, no brace", () => {
		const sentence = decisionReasonSentence("refused_repeated_call", null);
		expect(sentence).toBe(
			"Stopped: this call repeated with the same arguments. The loop was broken before it spent more of your budget.",
		);
		// The count-less form is the contract sentence with only the count clause removed.
		expect(sentence).toBe(uiSpecCopy(BREAKER_ROW, PHASE_06_SPEC).replace(" {n} times", ""));
		expect(sentence).not.toContain("undefined");
		expect(sentence).not.toContain("{n}");
		expect(sentence).not.toContain("null");
	});

	test("zero-one-many: a count the breaker could not have recorded is dropped, never pluralized wrong", () => {
		// The breaker fires only above its threshold, so n >= 2. A 0, a 1 or a
		// non-integer is bad data, and "repeated 1 times" would be a false sentence.
		const withoutCount = decisionReasonSentence("refused_repeated_call", null);
		for (const bad of [0, 1, -3, 2.5, Number.NaN]) {
			expect(decisionReasonSentence("refused_repeated_call", bad)).toBe(withoutCount);
		}
		expect(decisionReasonSentence("refused_repeated_call", 2)).toContain("repeated 2 times");
	});

	test("a blocked group reads the tools-blocked sentence (UI-SPEC addition, plan 06-24)", () => {
		expect(decisionReasonSentence("refused_group_disabled", null)).toBe(
			"Refused: this companion's tools are blocked. Blocking is an account setting — it cannot be changed from a tool argument.",
		);
	});

	test("every Phase 5 decision returns null, so its recorded reason keeps rendering", () => {
		const phase5 = CALL_DECISIONS.filter((decision) => !PHASE_6_REFUSALS.includes(decision));
		expect(phase5.length).toBe(CALL_DECISIONS.length - PHASE_6_REFUSALS.length);
		for (const decision of phase5) {
			expect(decisionReasonSentence(decision, null)).toBeNull();
			expect(decisionReasonSentence(decision, 6)).toBeNull();
		}
	});

	test("long-text: a sentence interpolates at most one integer and never agent-supplied text", () => {
		for (const decision of PHASE_6_REFUSALS) {
			const sentence = decisionReasonSentence(decision, 6);
			expect(sentence).not.toBeNull();
			// No tool name, no refusal token, no argument-shaped text.
			expect(sentence).not.toContain("__");
			expect(sentence).not.toMatch(/\[[a-z_]+\]/);
			expect(sentence?.match(/\d+/g)?.length ?? 0).toBeLessThanOrEqual(1);
		}
	});
});
