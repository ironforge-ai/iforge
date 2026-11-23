import type { components } from "@clawdi/shared/api";
import type { StatusTone } from "@/components/ui/status-badge";

/**
 * The activity log's rules, kept out of the component so they can be tested
 * without rendering a table.
 *
 * **The log records ATTEMPTS, not executions.** Plan 05-01 established that
 * every attempt writes exactly one row, including attempts refused before a
 * companion was ever reached. That is why a refused call here is a NORMAL ROW
 * with a destructive-tone decision chip and never an error state: the log
 * exists to answer "why not", and `decision` + `error_text` together are that
 * answer. `ApiErrorPanel` is reserved for a failed QUERY.
 *
 * **Null is not zero and not empty.** `session_id` is nullable by design — an
 * MCP session identifier is an opaque protocol value the transport may simply
 * not carry — so it is RENDERED as "Not recorded" rather than omitted. A blank
 * cell reads as "no session"; a fabricated identifier reads as evidence. Both
 * are lies on an audit surface.
 *
 * **No request this module builds carries a tenant or user identifier.**
 * Scoping is server-side and unconditional (`require_web_auth` plus a
 * `user_id` predicate in `backend/app/routes/companions.py`), so there is no
 * tenant selector on this surface and no filter it sends can widen the scope.
 * `activityQueryParams` is the single place a request's shape is decided, and
 * its test asserts the key set exhaustively.
 */

type Schemas = components["schemas"];

export type CompanionToolCall = Schemas["CompanionToolCallResponse"];
export type CompanionToolCallDetail = Schemas["CompanionToolCallDetailResponse"];
export type CallDecision = Schemas["CallDecision"];

/**
 * The thirteen decisions `CallDecision` can carry, in the order the backend's own
 * enum declares them.
 *
 * Listed here as a literal so the presentation table below can be asserted to
 * cover the vocabulary exactly — same size AND same set — rather than relying
 * on a default branch to absorb a value nobody mapped. A default fallthrough
 * on an audit surface renders an unmapped refusal as something benign.
 */
export const CALL_DECISIONS = [
	"allowed",
	"refused_policy",
	"refused_cap",
	"refused_budget_ceiling",
	"refused_repeated_call",
	"pending_approval",
	"refused_destroy_disabled",
	"refused_group_disabled",
	"refused_ratelimit",
	"refused_unknown_tool",
	"refused_approval",
	"runtime_unreachable",
	"dry_run_completed",
] as const satisfies readonly CallDecision[];

export interface DecisionPresentation {
	tone: StatusTone;
	label: string;
}

/**
 * Decision chip tone and label, from `05-UI-SPEC.md` §Color, plus the
 * spend-ceiling refusal and the repeated-call breaker from `06-UI-SPEC.md` S7,
 * and the group block (06-24), labelled to match S4's "Tools blocked" chip.
 *
 * Typed as a total `Record` over the vocabulary, so a decision added to the
 * backend enum and regenerated into the client fails `tsc` here rather than
 * rendering as a blank chip. The three decisions the spec's table does not name
 * verbatim are mapped by the same rule it states: a refusal is `destructive`,
 * something that merely waited or could not be reached is `warning`, and a
 * completed preview is `info` because nothing was written.
 */
export const DECISION_PRESENTATION: Record<CallDecision, DecisionPresentation> = {
	allowed: { tone: "success", label: "Allowed" },
	pending_approval: { tone: "warning", label: "Waiting for you" },
	refused_policy: { tone: "destructive", label: "Refused — policy" },
	refused_cap: { tone: "destructive", label: "Refused — over cap" },
	// The monthly money ceiling (06-22). A different ledger from the caps, so
	// its own label: "over cap" would send the reader to the wrong setting.
	refused_budget_ceiling: { tone: "destructive", label: "Refused — over budget" },
	// The repeated-call breaker (06-23). A LOOP was stopped, which is neither a
	// cap nor a rate limit, so it has its own chip. `warning`: the agent was
	// paused on repetition, not refused on policy or money.
	refused_repeated_call: { tone: "warning", label: "Stopped — repeating call" },
	refused_destroy_disabled: { tone: "destructive", label: "Refused — destroy off" },
	// The group block (06-24, D-30). The owner blocked this companion's whole
	// tool group, so a call to it by name is refused. Its own chip because
	// blocking is a security control, not a policy preference; the label matches
	// S4's "Tools blocked". Plan 06-32 records it as a UI-SPEC addition.
	refused_group_disabled: { tone: "destructive", label: "Refused — tools blocked" },
	refused_ratelimit: { tone: "warning", label: "Slowed — rate limit" },
	refused_unknown_tool: { tone: "destructive", label: "Refused — unknown tool" },
	refused_approval: { tone: "destructive", label: "Refused — approval" },
	runtime_unreachable: { tone: "warning", label: "No runtime" },
	dry_run_completed: { tone: "info", label: "Dry run completed" },
};

/** The chip tone for one decision. Total over the vocabulary — no fallthrough. */
export function decisionTone(decision: CallDecision): StatusTone {
	return DECISION_PRESENTATION[decision].tone;
}

/** The chip label for one decision. Total over the vocabulary — no fallthrough. */
export function decisionLabel(decision: CallDecision): string {
	return DECISION_PRESENTATION[decision].label;
}

/**
 * The backend bounds `limit` at 200, mirroring the audit route's precedent.
 *
 * Clamped here rather than trusted, because a page size that exceeds the bound
 * comes back as a 422 rather than as a shorter page — a control that can put
 * the table into an error state is a control that should not be able to.
 */
export const ACTIVITY_PAGE_SIZE_CAP = 200;
export const DEFAULT_ACTIVITY_PAGE_SIZE = 50;
export const ACTIVITY_PAGE_SIZE_OPTIONS = [25, 50, 100, 200] as const;

/** A page size inside the backend's bound: at least one row, never above 200. */
export function clampPageSize(requested: number): number {
	if (!Number.isFinite(requested)) return DEFAULT_ACTIVITY_PAGE_SIZE;
	const whole = Math.floor(requested);
	if (whole < 1) return 1;
	return Math.min(whole, ACTIVITY_PAGE_SIZE_CAP);
}

/** The four facets the backend's listing accepts, and nothing else. */
export interface ActivityFilters {
	toolClass: string | null;
	decision: CallDecision | null;
	companionKey: string | null;
	sessionId: string | null;
	/** Client-side only — see `matchesSearch`. Never sent to the server. */
	search: string;
}

export const EMPTY_ACTIVITY_FILTERS: ActivityFilters = {
	toolClass: null,
	decision: null,
	companionKey: null,
	sessionId: null,
	search: "",
};

/**
 * How many facets are narrowing the log right now.
 *
 * Zero means "the whole log", which is what decides between the two empty
 * states and whether the "Clear filters" action exists at all. The search box
 * counts: a search that matches nothing is a filtered-to-zero result, not an
 * empty log.
 */
export function activeFacetCount(filters: ActivityFilters): number {
	let count = 0;
	if (filters.toolClass) count += 1;
	if (filters.decision) count += 1;
	if (filters.companionKey) count += 1;
	if (filters.sessionId) count += 1;
	if (filters.search.trim() !== "") count += 1;
	return count;
}

/** The request this surface sends. Four facets, a bound and an offset. Nothing else. */
export interface ActivityQuery {
	session_id?: string;
	tool_class?: string;
	decision?: CallDecision;
	companion_key?: string;
	limit: number;
	offset: number;
}

/**
 * Build the listing request for a page.
 *
 * `page` is 1-based to match `DataTablePagination`'s convention. The absence of
 * a tenant or user key here is the mitigation for T-5-59 and is asserted by
 * this module's test over the exact key set rather than by reading the code.
 */
export function activityQueryParams(
	filters: ActivityFilters,
	page: number,
	pageSize: number,
): ActivityQuery {
	const limit = clampPageSize(pageSize);
	const offset = Math.max(0, (Math.max(1, Math.floor(page)) - 1) * limit);
	return {
		...(filters.sessionId ? { session_id: filters.sessionId } : {}),
		...(filters.toolClass ? { tool_class: filters.toolClass } : {}),
		...(filters.decision ? { decision: filters.decision } : {}),
		...(filters.companionKey ? { companion_key: filters.companionKey } : {}),
		limit,
		offset,
	};
}

/**
 * Client-side search over the page in hand.
 *
 * **Stated plainly because the scope is narrower than a search box implies.**
 * `GET /v1/companions/activity` has no free-text parameter (plan 05-07 exposed
 * four facets and a bound), and the list row carries no arguments at all — the
 * redacted preview lives on the DETAIL response. So this matches the tool name
 * and the session identifier, on the current page only, and the placeholder
 * copy says exactly that rather than promising an argument search the API
 * cannot perform.
 */
export function matchesSearch(row: CompanionToolCall, search: string): boolean {
	const needle = search.trim().toLowerCase();
	if (needle === "") return true;
	if (row.tool.toLowerCase().includes(needle)) return true;
	if (row.session_id?.toLowerCase().includes(needle)) return true;
	return false;
}

/**
 * True when the page came back full, which is the only "is there more" signal
 * the response supports.
 *
 * The list response is `{items: [...]}` with no total — the audit precedent it
 * copies has none either — so nothing here can compute "page 3 of 12". Next is
 * enabled when the page came back full and disabled otherwise, and the read-out
 * names the rows on screen rather than inventing a count the server never sent.
 */
export function hasFullPage(items: readonly unknown[], pageSize: number): boolean {
	return items.length >= clampPageSize(pageSize);
}

/**
 * Whether one row belongs on THIS agent's page.
 *
 * `GET /v1/companions/activity` is tenant-scoped and accepts no agent
 * parameter — deliberately, for the same reason the approvals route does not —
 * so the page's own agent scope is the filter, exactly as plan 05-08 filtered
 * approvals. `environment_id` IS the stable agent id, so the match is exact
 * rather than a join.
 *
 * **A null `environment_id` is KEPT.** Plan 05-07 made that column nullable
 * precisely so an attempt refused because no runtime of the tenant's has the
 * companion deployed is a ROW rather than nothing — the dispatch queue's own
 * `environment_id` is NOT NULL and structurally cannot hold it. Dropping those
 * rows here would undo that on the only surface that renders the log. Such a
 * row explains itself: its decision chip reads "No runtime".
 */
export function belongsToAgent(row: CompanionToolCall, agentId: string): boolean {
	return row.environment_id === null || row.environment_id === agentId;
}

/** The rows this page shows: agent-scoped, then narrowed by the client-side search. */
export function visibleActivityRows(
	items: readonly CompanionToolCall[],
	agentId: string,
	filters: ActivityFilters,
): CompanionToolCall[] {
	return items.filter((row) => belongsToAgent(row, agentId) && matchesSearch(row, filters.search));
}

/**
 * The distinct session identifiers on the page in hand, for the Session facet.
 *
 * Derived from the current page because plan 05-07 exposed no facet-options
 * route: there is no "what session values exist for this tenant" endpoint to
 * ask. The facet therefore offers what the log currently shows rather than
 * claiming to enumerate every session the tenant ever had.
 */
export function sessionFacetOptions(items: readonly CompanionToolCall[]): {
	label: string;
	value: string;
}[] {
	const seen = new Set<string>();
	for (const row of items) {
		if (row.session_id) seen.add(row.session_id);
	}
	return Array.from(seen)
		.sort()
		.map((value) => ({ label: value, value }));
}

/** The muted value every unrecorded link renders, in the table and the drawer alike. */
export const NOT_RECORDED = "Not recorded";

/**
 * The Session cell's text: the identifier, or the explicit not-recorded value.
 *
 * Never a blank cell (which reads as "no session") and never a fabricated
 * identifier (which reads as evidence).
 */
export function sessionCellValue(sessionId: string | null): string {
	return sessionId && sessionId.trim() !== "" ? sessionId : NOT_RECORDED;
}

/**
 * The cap debit as one phrase, or the not-recorded value.
 *
 * `cap_consumed: null` means the attempt never reached a debit — refused
 * upstream of the cap check — which is a different fact from a debit of zero.
 * A zero debit is a real measurement and renders as `0`.
 */
export function capDebitLabel(row: Pick<CompanionToolCall, "cap_unit" | "cap_consumed">): string {
	if (row.cap_consumed === null || row.cap_unit === null) return NOT_RECORDED;
	return `${row.cap_consumed} ${row.cap_unit}`;
}

/** Page and section copy, from `05-UI-SPEC.md` §Copywriting Contract. */
export const ACTIVITY_PAGE_COPY = {
	title: "Tool Activity",
	description:
		"Every call this agent made through the façade — allowed, refused, and dry runs alike.",
	toolsTab: "Tools",
	activityTab: "Activity",
	// Narrower than the spec's "Search tools, arguments or sessions…": the
	// listing carries no arguments and no free-text parameter, and a box that
	// says it searches arguments while silently not doing so is worse on an
	// audit surface than one that says what it does.
	searchPlaceholder: "Search tools or sessions…",
	clearFilters: "Clear filters",
} as const;

/** The four faceted-filter labels, in the fixed slot order the toolbar renders. */
export const ACTIVITY_FILTER_LABELS = {
	toolClass: "Class",
	decision: "Decision",
	companionKey: "Companion",
	sessionId: "Session",
} as const;

/** Zero rows and nothing filtering: the whole log is empty. */
export const ACTIVITY_EMPTY_COPY = {
	title: "No tool calls yet",
	description:
		"Every call this agent makes through the façade is recorded here, including the ones that were refused.",
} as const;

/** Zero rows because the filters excluded them. A different answer, and a different state. */
export const ACTIVITY_FILTERED_EMPTY_COPY = {
	title: "No calls match these filters",
	description: "Widen the filters or clear them to see the full log.",
	action: "Clear filters",
} as const;

/**
 * The seven columns, in render order, as data rather than as JSX.
 *
 * The cell renderers live in `tool-activity-table.tsx` because they render
 * components; what a column IS — its identifier, its heading, its width and
 * whether the table may hide it — is a decision, and decisions belong where
 * they can be asserted without a renderer. `hideable: false` on every entry is
 * the recorded answer to the spec's unresolved narrow-width question: the table
 * scrolls horizontally inside its own container and never drops a column,
 * because a dropped column silently removes evidence from an audit surface.
 */
export interface ActivityColumn {
	id: string;
	/** The column heading, as the user reads it. */
	header: string;
	/** Width hint handed to `@tanstack/react-table`, in pixels. */
	size: number;
	/** Whether the table may ever hide this column. Always false here. */
	hideable: boolean;
}

export const activityColumns: readonly ActivityColumn[] = [
	{ id: "created_at", header: "Time", size: 120, hideable: false },
	{ id: "tool", header: "Tool", size: 260, hideable: false },
	{ id: "tool_class", header: "Class", size: 100, hideable: false },
	{ id: "decision", header: "Decision", size: 180, hideable: false },
	{ id: "dry_run", header: "Dry run", size: 100, hideable: false },
	{ id: "session_id", header: "Session", size: 180, hideable: false },
	{ id: "cap_debit", header: "Cap debit", size: 120, hideable: false },
];

/** The minimum table width the seven columns need before the container scrolls. */
export const ACTIVITY_TABLE_MIN_WIDTH = activityColumns.reduce(
	(total, column) => total + column.size,
	0,
);
