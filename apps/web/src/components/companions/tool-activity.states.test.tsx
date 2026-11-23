/**
 * Held-out UI-state tests for S4 — the activity DataTable, the ListToolbar
 * filter row and the tool name column. One assertion per `05-UI-SPEC.md`
 * §UI Considerations row for those three elements.
 *
 * **SCOPE LIMIT, stated so a later reader does not mistake this for a layout
 * measurement.** There is no DOM library and no viewport harness in this
 * workspace, so every assertion here reads `renderToStaticMarkup` output: which
 * component rendered, which copy is present, which classes and `title`
 * attributes are present, and in which ORDER elements were emitted. It does not
 * measure pixels and cannot observe a rendered width. The `overflow` and
 * `long-text` rows are therefore asserted as their markup-level
 * PRECONDITIONS — the truncation affordance is present, the full value is in a
 * `title`, the container carries the scroll class, the trailing cells are still
 * emitted after a maximum-length name — never as a measured result. Whether the
 * result actually holds at the narrowest supported width is the `ui.safety-gate`'s
 * question.
 *
 * Two shapes are invisible to static markup and are asserted through the logic
 * module instead: `DataTableFacetedFilter` portals its option list (only the
 * trigger is in the markup), and the `Rows` `Select` portals its options too.
 */

import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { ApiError } from "@/lib/api-errors";
import { renderedText } from "@/lib/rendered-text";
import {
	ACTIVITY_EMPTY_COPY,
	ACTIVITY_FILTER_LABELS,
	ACTIVITY_FILTERED_EMPTY_COPY,
	ACTIVITY_PAGE_COPY,
	type ActivityFilters,
	CALL_DECISIONS,
	type CallDecision,
	type CompanionToolCall,
	decisionLabel,
	EMPTY_ACTIVITY_FILTERS,
	NOT_RECORDED,
} from "./tool-activity.logic";
import { ToolActivityRegion } from "./tool-activity-table";

const AGENT_ID = "6c3b1a2e-0000-4000-8000-000000000001";
/** The registry bounds a composite tool name at 94 characters. */
const MAX_LENGTH_TOOL_NAME = `filebrowser__${"x".repeat(94 - "filebrowser__".length)}`;

function call(overrides: Partial<CompanionToolCall> = {}): CompanionToolCall {
	return {
		id: "0b7f0f1e-0000-4000-8000-000000000001",
		created_at: "2026-09-17T09:00:00Z",
		session_id: "mcp-session-1",
		environment_id: AGENT_ID,
		tool: "filebrowser__list_dir",
		tool_class: "read",
		companion_key: "filebrowser",
		verb: "list_dir",
		decision: "allowed",
		dry_run: false,
		cap_unit: "calls",
		cap_consumed: 1,
		cap_remaining_after: 199,
		error_text: null,
		parent_call_id: null,
		source_content_ref: null,
		...overrides,
	};
}

const COMPANION_OPTIONS = [{ label: "filebrowser", value: "filebrowser" }];

function render(
	props: Partial<Parameters<typeof ToolActivityRegion>[0]> = {},
	filters: Partial<ActivityFilters> = {},
) {
	return renderToStaticMarkup(
		<ToolActivityRegion
			agentId={AGENT_ID}
			items={[call()]}
			filters={{ ...EMPTY_ACTIVITY_FILTERS, ...filters }}
			onFiltersChange={() => {}}
			companionOptions={COMPANION_OPTIONS}
			page={1}
			pageSize={50}
			onPageChange={() => {}}
			onPageSizeChange={() => {}}
			onRetry={() => {}}
			{...props}
		/>,
	);
}

describe("S4 activity DataTable", () => {
	test("empty: zero rows renders the page-variant empty state naming refused calls", () => {
		const markup = render({ items: [] });
		const text = renderedText(markup);
		expect(text).toContain(ACTIVITY_EMPTY_COPY.title);
		expect(text).toContain("including the ones that were refused");
		// Distinct from the filtered-to-zero state, which must NOT appear here.
		expect(text).not.toContain(ACTIVITY_FILTERED_EMPTY_COPY.title);
	});

	test("empty: filters matching nothing renders the distinct inset state with Clear filters", () => {
		const markup = render({ items: [] }, { decision: "refused_cap" });
		const text = renderedText(markup);
		expect(text).toContain(ACTIVITY_FILTERED_EMPTY_COPY.title);
		expect(text).toContain(ACTIVITY_FILTERED_EMPTY_COPY.action);
		expect(text).not.toContain(ACTIVITY_EMPTY_COPY.title);
	});

	test("loading: skeleton table rows, with the toolbar still mounted and no page spinner", () => {
		const markup = render({ loading: true, items: [] });
		expect(markup).toContain('data-slot="skeleton"');
		// The toolbar mounts before the data and stays mounted, so applying a
		// filter never reflows the control row.
		expect(markup).toContain(ACTIVITY_FILTER_LABELS.decision);
		expect(markup).not.toContain('data-slot="spinner"');
	});

	test("error: a failed QUERY renders ApiErrorPanel, while a refused CALL is a normal row", () => {
		const failed = renderedText(render({ error: new ApiError(500, "boom"), items: [] }));
		expect(failed).toContain("Couldn't load tool activity");

		const refused = render({ items: [call({ decision: "refused_policy" })] });
		expect(refused).toContain('data-decision="refused_policy"');
		expect(refused).toContain('data-status="destructive"');
		// The refused row went through the ordinary row path: no error panel.
		expect(renderedText(refused)).not.toContain("Couldn't load tool activity");
	});

	test("populated: a dense table at the shadcn default row height, with no added row padding", () => {
		const markup = render();
		expect(markup).toContain("<table");
		expect(markup).toContain('data-slot="activity-time"');
		expect(markup).toContain('data-slot="activity-cap-debit"');
		// The cap debit is tabular so a counter cannot jiggle between refetches.
		expect(markup).toMatch(/data-slot="activity-cap-debit"[^>]*tabular-nums/);
		// No py-* override on the row: the chassis default is the density.
		expect(markup).not.toContain("py-4");
	});

	test("partial: a null session renders Not recorded in muted text, never blank", () => {
		const markup = render({ items: [call({ session_id: null })] });
		expect(renderedText(markup)).toContain(NOT_RECORDED);
		expect(markup).toMatch(/data-slot="activity-session"[^>]*text-muted-foreground/);
	});

	test("overflow: the table container scrolls horizontally rather than dropping a column", () => {
		// The recorded answer to the spec's unresolved narrow-width row. Asserted
		// as its precondition: the scroll container is present AND all seven
		// headings are still emitted.
		const markup = render();
		expect(markup).toContain("overflow-x-auto");
		const text = renderedText(markup);
		for (const heading of [
			"Time",
			"Tool",
			"Class",
			"Decision",
			"Dry run",
			"Session",
			"Cap debit",
		]) {
			expect(text).toContain(heading);
		}
	});

	test("zero-one-many: one call renders one row with the full chrome intact", () => {
		const markup = render({ items: [call()] });
		// Header, toolbar and pagination all stay mounted at a single row, so the
		// log never looks broken at one.
		expect(markup).toContain("<thead");
		expect(markup).toContain('data-slot="activity-pagination"');
		expect(markup).toContain(ACTIVITY_PAGE_COPY.searchPlaceholder);
		expect(markup.match(/<tbody[\s\S]*?<\/tbody>/)?.[0].match(/<tr/g)?.length).toBe(1);
	});
});

describe("S4 ListToolbar filter row", () => {
	test("empty: search plus four unset facet triggers, and no Clear filters action", () => {
		const text = renderedText(render());
		for (const label of Object.values(ACTIVITY_FILTER_LABELS)) {
			expect(text).toContain(label);
		}
		expect(text).not.toContain(ACTIVITY_PAGE_COPY.clearFilters);
	});

	test("loading: the toolbar is mounted before the table data and survives a refetch", () => {
		const loading = render({ loading: true, items: [] });
		const settled = render();
		for (const markup of [loading, settled]) {
			expect(markup).toContain(ACTIVITY_PAGE_COPY.searchPlaceholder);
			expect(markup).toContain(ACTIVITY_FILTER_LABELS.companionKey);
		}
	});

	test("error: a failed facet-options query disables that trigger with its label intact", () => {
		const markup = render({ companionOptionsError: true });
		expect(markup).toContain('data-slot="activity-facet-unavailable"');
		expect(markup).toContain('data-facet="Companion"');
		expect(markup).toMatch(/data-slot="activity-facet-unavailable"[\s\S]{0,400}?disabled/);
		// The trigger is disabled, never removed — the control row keeps its geometry.
		expect(renderedText(markup)).toContain(ACTIVITY_FILTER_LABELS.companionKey);
	});

	test("error: the failed facet does NOT take the other three with it", () => {
		const text = renderedText(render({ companionOptionsError: true }));
		expect(text).toContain(ACTIVITY_FILTER_LABELS.toolClass);
		expect(text).toContain(ACTIVITY_FILTER_LABELS.decision);
		expect(text).toContain(ACTIVITY_FILTER_LABELS.sessionId);
	});

	test("populated: fixed slot order — search, then the four facets, then actions", () => {
		const markup = render({}, { decision: "refused_cap" });
		const search = markup.indexOf(ACTIVITY_PAGE_COPY.searchPlaceholder);
		const classFacet = markup.indexOf('data-facet="Class"');
		const decisionFacet = markup.indexOf('data-facet="Decision"');
		const companionFacet = markup.indexOf('data-facet="Companion"');
		const sessionFacet = markup.indexOf('data-facet="Session"');
		const clear = markup.indexOf(ACTIVITY_PAGE_COPY.clearFilters);
		expect(search).toBeGreaterThanOrEqual(0);
		expect(classFacet).toBeGreaterThan(search);
		expect(decisionFacet).toBeGreaterThan(classFacet);
		expect(companionFacet).toBeGreaterThan(decisionFacet);
		expect(sessionFacet).toBeGreaterThan(companionFacet);
		expect(clear).toBeGreaterThan(sessionFacet);
		// Filters live in the toolbar, never in PageHeader actions.
		expect(markup).not.toContain('data-slot="page-header"');
	});

	test("partial: a facet whose options have not loaded keeps its label and shows no count badge", () => {
		const markup = render({ companionOptions: [] });
		expect(markup).toContain('data-slot="activity-facet-unavailable"');
		expect(renderedText(markup)).toContain(ACTIVITY_FILTER_LABELS.companionKey);
		// No empty dropdown, and no count badge on an unloaded facet.
		expect(markup).not.toMatch(/data-facet="Companion"[\s\S]{0,300}?data-slot="badge"/);
	});

	test("overflow: the toolbar wraps and a long facet value is bounded rather than widening it", () => {
		// The recorded answer to the spec's second unresolved row, asserted as its
		// markup precondition: ListToolbar is a wrapping flex row, and the facet
		// trigger is width-capped with its selected-value badge truncated.
		const markup = render({}, { sessionId: "s".repeat(180) });
		expect(markup).toContain("flex-wrap");
		expect(markup).toMatch(/data-facet="Session"[^>]*max-w-\[12rem\]/);
		expect(markup).toMatch(/data-facet="Session"[^>]*truncate/);
	});

	test("zero-one-many: an active facet renders the Clear filters action; an unset one does not", () => {
		expect(renderedText(render({}, { toolClass: "destroy" }))).toContain(
			ACTIVITY_PAGE_COPY.clearFilters,
		);
		expect(renderedText(render())).not.toContain(ACTIVITY_PAGE_COPY.clearFilters);
	});

	test("long-text: a long selected session value truncates rather than widening the toolbar", () => {
		const markup = render({}, { sessionId: "s".repeat(180) });
		expect(markup).toMatch(/data-facet="Session"[^>]*truncate/);
	});
});

describe("S4 tool name column", () => {
	test("empty: a name the façade could not resolve renders its raw recorded value in mono", () => {
		const markup = render({ items: [call({ tool: "unresolved-protocol-value" })] });
		expect(renderedText(markup)).toContain("unresolved-protocol-value");
		expect(markup).toMatch(/data-slot="activity-tool"[^>]*font-mono/);
	});

	test("loading: the tool cell skeletons in at mono width rather than resizing on arrival", () => {
		const markup = render({ loading: true, items: [] });
		expect(markup).toContain('data-slot="skeleton"');
		// The header row is emitted with the loading body, so the column is sized
		// by the table layout before any row lands.
		expect(renderedText(markup)).toContain("Tool");
	});

	test("error: a malformed tool name renders verbatim — the recorded value is the evidence", () => {
		const malformed = "filebrowser__:::not a verb:::";
		const markup = render({ items: [call({ tool: malformed })] });
		expect(renderedText(markup)).toContain(malformed);
		expect(markup).not.toContain("Invalid tool");
	});

	test("populated: mono at the small type size so the double underscore stays intact", () => {
		const markup = render({ items: [call({ tool: "filebrowser__delete_path" })] });
		expect(markup).toMatch(/data-slot="activity-tool"[^>]*font-mono text-xs/);
		expect(renderedText(markup)).toContain("filebrowser__delete_path");
	});

	test("partial: a name missing its kind prefix renders exactly as recorded", () => {
		const markup = render({ items: [call({ tool: "delete_path" })] });
		expect(renderedText(markup)).toContain("delete_path");
		expect(renderedText(markup)).not.toContain("filebrowser__delete_path");
	});

	test("overflow: a maximum-length name still leaves the decision chip on the row", () => {
		const markup = render({
			items: [call({ tool: MAX_LENGTH_TOOL_NAME, decision: "refused_cap" })],
		});
		const name = markup.indexOf('data-slot="activity-tool"');
		const chip = markup.indexOf('data-decision="refused_cap"');
		expect(name).toBeGreaterThanOrEqual(0);
		expect(chip).toBeGreaterThan(name);
	});

	test("long-text: the cell truncates with the full value in a title, never wrapping to two lines", () => {
		const markup = render({ items: [call({ tool: MAX_LENGTH_TOOL_NAME })] });
		expect(markup).toContain(`title="${MAX_LENGTH_TOOL_NAME}"`);
		expect(markup).toMatch(/data-slot="activity-tool"[^>]*truncate/);
	});

	test("zero-one-many: one row and many rows use the same cell treatment", () => {
		const one = render({ items: [call({ id: "a" })] });
		const many = render({
			items: [call({ id: "a" }), call({ id: "b" }), call({ id: "c" })],
		});
		const treatment = /data-slot="activity-tool" class="([^"]+)"/;
		expect(one.match(treatment)?.[1]).toBe(many.match(treatment)?.[1] as string);
	});
});

/** The three Phase 6 refusals and the chip each one renders (06-UI-SPEC S7). */
const PHASE_6_REFUSAL_CHIPS = [
	{ decision: "refused_budget_ceiling", chip: "Refused — over budget" },
	{ decision: "refused_repeated_call", chip: "Stopped — repeating call" },
	// A UI-SPEC addition: plan 06-24 added the decision, plan 06-32 records the chip.
	{ decision: "refused_group_disabled", chip: "Refused — tools blocked" },
] as const satisfies readonly { decision: CallDecision; chip: string }[];

describe("S7 refusal decisions in the activity table", () => {
	test("populated: each refused row is a normal row carrying its own distinct chip", () => {
		const items = PHASE_6_REFUSAL_CHIPS.map(({ decision }, index) =>
			call({ id: `row-${index}`, decision, error_text: `[${decision}] agent-facing text` }),
		);
		const markup = render({ items });
		const text = renderedText(markup);
		expect(markup.match(/<tbody[\s\S]*?<\/tbody>/)?.[0].match(/<tr/g)?.length).toBe(3);
		for (const { decision, chip } of PHASE_6_REFUSAL_CHIPS) {
			expect(markup).toContain(`data-decision="${decision}"`);
			expect(text).toContain(chip);
		}
		// Three distinct chips, not one shared refusal token.
		expect(new Set(PHASE_6_REFUSAL_CHIPS.map(({ chip }) => chip)).size).toBe(3);
	});

	test("error: a refused row never routes to ApiErrorPanel — that is for a failed query", () => {
		for (const { decision } of PHASE_6_REFUSAL_CHIPS) {
			const text = renderedText(render({ items: [call({ decision })] }));
			expect(text).not.toContain("Couldn't load tool activity");
		}
	});

	test("the decision facet offers all three new values, labelled with their chips", () => {
		// The facet's option list portals, so it is asserted through the exact
		// mapping the table hands `ActivityFacet`: every decision, by its label.
		const options = CALL_DECISIONS.map((value) => ({ label: decisionLabel(value), value }));
		for (const { decision, chip } of PHASE_6_REFUSAL_CHIPS) {
			expect(options).toContainEqual({ label: chip, value: decision });
		}
		// The trigger itself is in the markup.
		expect(render()).toContain('data-facet="Decision"');
	});

	test("empty and filtered: filtering to a new decision with no rows is Phase 5's filtered state", () => {
		for (const { decision } of PHASE_6_REFUSAL_CHIPS) {
			const text = renderedText(render({ items: [] }, { decision }));
			expect(text).toContain(ACTIVITY_FILTERED_EMPTY_COPY.title);
			expect(text).not.toContain(ACTIVITY_EMPTY_COPY.title);
		}
	});
});
