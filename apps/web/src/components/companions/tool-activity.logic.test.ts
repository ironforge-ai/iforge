/**
 * The activity log's rules, asserted without rendering a table.
 *
 * Four exports carry the surface: `decisionTone` (and the presentation table
 * behind it), `activityColumns`, `clampPageSize` and `activeFacetCount`. The
 * request shape is asserted here too, because "no filter this surface sends can
 * widen the tenant scope" is a claim about a key set and nothing else.
 */

import { describe, expect, test } from "bun:test";
import {
	ACTIVITY_PAGE_COPY,
	ACTIVITY_PAGE_SIZE_CAP,
	activeFacetCount,
	activityColumns,
	activityQueryParams,
	belongsToAgent,
	CALL_DECISIONS,
	type CallDecision,
	type CompanionToolCall,
	capDebitLabel,
	clampPageSize,
	DECISION_PRESENTATION,
	decisionLabel,
	decisionTone,
	EMPTY_ACTIVITY_FILTERS,
	hasFullPage,
	matchesSearch,
	NOT_RECORDED,
	sessionCellValue,
	sessionFacetOptions,
	visibleActivityRows,
} from "./tool-activity.logic";

const AGENT_ID = "6c3b1a2e-0000-4000-8000-000000000001";

function call(overrides: Partial<CompanionToolCall> = {}): CompanionToolCall {
	return {
		id: "0b7f0f1e-0000-4000-8000-000000000001",
		created_at: "2026-09-17T09:00:00Z",
		session_id: "mcp-session-1",
		environment_id: AGENT_ID,
		tool: "filebrowser__delete_path",
		tool_class: "destroy",
		companion_key: "filebrowser",
		verb: "delete_path",
		decision: "refused_policy",
		dry_run: false,
		cap_unit: "records",
		cap_consumed: 0,
		cap_remaining_after: 200,
		error_text: "destroy tools are off for this companion",
		parent_call_id: null,
		source_content_ref: null,
		...overrides,
	};
}

describe("decisionTone", () => {
	// One assertion per value in the backend vocabulary. Written as a literal
	// table rather than derived from the map under test, so a wrong tone is a
	// failure here rather than a tautology.
	const expected: Record<CallDecision, string> = {
		allowed: "success",
		pending_approval: "warning",
		refused_policy: "destructive",
		refused_cap: "destructive",
		refused_budget_ceiling: "destructive",
		refused_repeated_call: "warning",
		refused_destroy_disabled: "destructive",
		refused_group_disabled: "destructive",
		refused_ratelimit: "warning",
		refused_unknown_tool: "destructive",
		refused_approval: "destructive",
		runtime_unreachable: "warning",
		dry_run_completed: "info",
	};

	for (const decision of CALL_DECISIONS) {
		test(`${decision} renders its recorded tone`, () => {
			expect(decisionTone(decision)).toBe(expected[decision] as never);
		});
	}

	test("the mapping covers every member of the vocabulary with no default fallthrough", () => {
		// Same SIZE and same SET, in both directions: a fourteenth decision cannot
		// arrive unmapped and a mapping cannot survive for a decision that was
		// removed. A default branch would hide the first case by rendering an
		// unknown refusal as something benign.
		const mapped = Object.keys(DECISION_PRESENTATION).sort();
		const vocabulary = [...CALL_DECISIONS].sort();
		expect(mapped.length).toBe(vocabulary.length);
		expect(mapped).toEqual(vocabulary);
		for (const decision of CALL_DECISIONS) {
			expect(decisionLabel(decision).length).toBeGreaterThan(0);
		}
	});

	test("a spend-ceiling refusal has its own destructive chip, distinct from the cap's", () => {
		// 06-UI-SPEC S7: the money ceiling is a different ledger from the Phase 5
		// caps, so its chip must never read as "over cap".
		expect(CALL_DECISIONS).toContain("refused_budget_ceiling");
		expect(DECISION_PRESENTATION["refused_budget_ceiling" as CallDecision]).toEqual({
			tone: "destructive",
			label: "Refused — over budget",
		});
		expect(decisionLabel("refused_cap")).not.toBe("Refused — over budget");
	});

	test("a repeated-call stop has its own warning chip, distinct from the rate limit's", () => {
		// 06-UI-SPEC S7: the breaker stopped a LOOP, which is neither a cap nor a
		// rate limit, so it gets its own token and its own chip. `warning`, not
		// `destructive`: nothing was refused on policy, the agent was paused.
		expect(CALL_DECISIONS).toContain("refused_repeated_call");
		expect(DECISION_PRESENTATION["refused_repeated_call" as CallDecision]).toEqual({
			tone: "warning",
			label: "Stopped — repeating call",
		});
		expect(decisionLabel("refused_ratelimit")).not.toBe("Stopped — repeating call");
	});

	test("a blocked companion group has its own destructive chip, matching S4's", () => {
		// Plan 06-24 (D-30): blocking a companion's tools is a security control,
		// so a call to a blocked group is its own decision rather than a policy
		// refusal. The label matches S4's own "Tools blocked" chip; plan 06-32
		// records this third new decision as a UI-SPEC addition.
		expect(CALL_DECISIONS).toContain("refused_group_disabled");
		expect(DECISION_PRESENTATION["refused_group_disabled" as CallDecision]).toEqual({
			tone: "destructive",
			label: "Refused — tools blocked",
		});
		expect(decisionLabel("refused_policy")).not.toBe("Refused — tools blocked");
		expect(decisionLabel("refused_destroy_disabled")).not.toBe("Refused — tools blocked");
	});

	test("a refusal is a decision, never an error state", () => {
		// The chip tone is destructive; nothing here routes a refusal anywhere
		// near ApiErrorPanel. The rendered counterpart is in the states test.
		expect(decisionTone("refused_policy")).toBe("destructive");
		expect(decisionLabel("refused_policy")).toBe("Refused — policy");
	});
});

describe("activityColumns", () => {
	test("declares exactly the seven columns the screen contract names, in order", () => {
		expect(activityColumns.map((column) => column.id)).toEqual([
			"created_at",
			"tool",
			"tool_class",
			"decision",
			"dry_run",
			"session_id",
			"cap_debit",
		]);
		expect(activityColumns.map((column) => column.header)).toEqual([
			"Time",
			"Tool",
			"Class",
			"Decision",
			"Dry run",
			"Session",
			"Cap debit",
		]);
	});

	test("no column may ever be hidden — a dropped column removes evidence", () => {
		expect(activityColumns.every((column) => column.hideable === false)).toBe(true);
	});
});

describe("clampPageSize", () => {
	test("caps at the backend's 200-row bound", () => {
		expect(clampPageSize(200)).toBe(200);
		expect(clampPageSize(201)).toBe(200);
		expect(clampPageSize(10_000)).toBe(ACTIVITY_PAGE_SIZE_CAP);
	});

	test("a negative or zero page size becomes one row, never a 422", () => {
		expect(clampPageSize(-5)).toBe(1);
		expect(clampPageSize(0)).toBe(1);
	});

	test("a non-finite page size falls back to the default rather than propagating NaN", () => {
		expect(clampPageSize(Number.NaN)).toBe(50);
	});
});

describe("activeFacetCount", () => {
	test("returns zero for an unset toolbar", () => {
		expect(activeFacetCount(EMPTY_ACTIVITY_FILTERS)).toBe(0);
	});

	test("counts each active facet, search included", () => {
		expect(activeFacetCount({ ...EMPTY_ACTIVITY_FILTERS, toolClass: "destroy" })).toBe(1);
		expect(
			activeFacetCount({
				toolClass: "destroy",
				decision: "refused_policy",
				companionKey: "filebrowser",
				sessionId: "mcp-session-1",
				search: "delete",
			}),
		).toBe(5);
	});

	test("whitespace alone is not a search", () => {
		expect(activeFacetCount({ ...EMPTY_ACTIVITY_FILTERS, search: "   " })).toBe(0);
	});
});

describe("activityQueryParams", () => {
	test("carries no tenant or user identifier — scoping is server-side", () => {
		const keys = Object.keys(
			activityQueryParams(
				{
					toolClass: "destroy",
					decision: "refused_cap",
					companionKey: "filebrowser",
					sessionId: "mcp-session-1",
					search: "anything",
				},
				3,
				50,
			),
		).sort();
		expect(keys).toEqual([
			"companion_key",
			"decision",
			"limit",
			"offset",
			"session_id",
			"tool_class",
		]);
		for (const key of keys) {
			expect(/tenant|user|owner|org/i.test(key)).toBe(false);
		}
	});

	test("omits an unset facet rather than sending a null", () => {
		expect(activityQueryParams(EMPTY_ACTIVITY_FILTERS, 1, 50)).toEqual({
			limit: 50,
			offset: 0,
		});
	});

	test("the client-side search never reaches the server", () => {
		const params = activityQueryParams({ ...EMPTY_ACTIVITY_FILTERS, search: "delete" }, 1, 50);
		expect(JSON.stringify(params)).not.toContain("delete");
	});

	test("paging is limit plus a bounded offset, clamped to the 200-row cap", () => {
		expect(activityQueryParams(EMPTY_ACTIVITY_FILTERS, 3, 25)).toEqual({ limit: 25, offset: 50 });
		expect(activityQueryParams(EMPTY_ACTIVITY_FILTERS, 2, 500)).toEqual({
			limit: 200,
			offset: 200,
		});
		expect(activityQueryParams(EMPTY_ACTIVITY_FILTERS, 0, 50)).toEqual({ limit: 50, offset: 0 });
	});
});

describe("null is not zero and not empty", () => {
	test("a null session renders as not recorded, never blank and never fabricated", () => {
		expect(sessionCellValue(null)).toBe(NOT_RECORDED);
		expect(sessionCellValue("   ")).toBe(NOT_RECORDED);
		expect(sessionCellValue("mcp-session-1")).toBe("mcp-session-1");
	});

	test("a null cap debit is not recorded, while a debit of zero is a measurement", () => {
		expect(capDebitLabel({ cap_unit: "records", cap_consumed: null })).toBe(NOT_RECORDED);
		expect(capDebitLabel({ cap_unit: null, cap_consumed: 4 })).toBe(NOT_RECORDED);
		expect(capDebitLabel({ cap_unit: "records", cap_consumed: 0 })).toBe("0 records");
	});
});

describe("agent scope", () => {
	test("a row from another agent is excluded", () => {
		expect(belongsToAgent(call({ environment_id: "another-agent" }), AGENT_ID)).toBe(false);
	});

	test("a row with NO environment is kept — that row is why the column is nullable", () => {
		// The attempt refused because no runtime of the tenant's has the companion
		// deployed. `companion_dispatch_calls.environment_id` is NOT NULL and
		// structurally cannot hold it; dropping it here would lose it entirely.
		expect(belongsToAgent(call({ environment_id: null }), AGENT_ID)).toBe(true);
	});

	test("visible rows are agent-scoped and then narrowed by the search", () => {
		const rows = visibleActivityRows(
			[
				call({ id: "a", tool: "filebrowser__delete_path" }),
				call({ id: "b", tool: "filebrowser__list_dir" }),
				call({ id: "c", environment_id: "another-agent" }),
			],
			AGENT_ID,
			{ ...EMPTY_ACTIVITY_FILTERS, search: "delete" },
		);
		expect(rows.map((row) => row.id)).toEqual(["a"]);
	});
});

describe("search and facet options", () => {
	test("matches the tool name and the session identifier, case-insensitively", () => {
		expect(matchesSearch(call(), "DELETE")).toBe(true);
		expect(matchesSearch(call(), "session-1")).toBe(true);
		expect(matchesSearch(call({ session_id: null }), "session-1")).toBe(false);
		expect(matchesSearch(call(), "")).toBe(true);
	});

	test("the copy promises only what the listing can do", () => {
		// The route has no free-text parameter and the list row carries no
		// arguments, so the placeholder must not offer an argument search.
		expect(ACTIVITY_PAGE_COPY.searchPlaceholder).not.toContain("argument");
	});

	test("session facet options are the distinct sessions on the page, sorted", () => {
		expect(
			sessionFacetOptions([
				call({ session_id: "b" }),
				call({ session_id: "a" }),
				call({ session_id: "a" }),
				call({ session_id: null }),
			]).map((option) => option.value),
		).toEqual(["a", "b"]);
	});
});

describe("hasFullPage", () => {
	test("next is available exactly when the page came back full", () => {
		expect(hasFullPage(new Array(50), 50)).toBe(true);
		expect(hasFullPage(new Array(49), 50)).toBe(false);
		expect(hasFullPage([], 50)).toBe(false);
	});
});
