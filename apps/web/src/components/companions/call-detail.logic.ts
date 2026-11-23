import type { components } from "@clawdi/shared/api";
import { truncateForDisplay } from "@/lib/format";
import { type CallDecision, type CompanionToolCall, NOT_RECORDED } from "./tool-activity.logic";

/**
 * The call-detail drawer's rules, kept out of the components so the ones that
 * matter most can be asserted without opening a portal.
 *
 * Three distinctions carry this module, and each of them is a place a renderer
 * could quietly tell a reviewer something false immediately before they
 * authorize a write.
 *
 * **1. Unrecorded is not blank and is not invented.** Every link in the chain is
 * nullable by design — `session_id` because an MCP session identifier is an
 * opaque protocol value the transport may not carry, `environment_id` because an
 * attempt refused before any runtime was chosen belongs to no agent. A blank row
 * reads as "there was none"; a fabricated identifier reads as evidence. Both are
 * lies, so an unrecorded link renders the explicit "Not recorded".
 *
 * **2. Pending is not unrecorded either.** The Sheet opens with the table row
 * already in hand and the detail still in flight, so the two rows that exist
 * only on the detail response — the arguments and the call identifier — are
 * `pending` until it lands. Rendering them as "Not recorded" for that second
 * would be a claim about the data rather than about the request.
 *
 * **3. A null diff is "nobody looked"; an empty diff is "nothing would
 * change".** `companion_tool_calls.dry_run_diff` is written only for a preview
 * that COMPLETED and passed `validate_dry_run_output`; a preview that errored
 * records null and puts its reason in `error_text`. Collapsing the two would
 * render "Nothing would change." over a probe that never read the target, which
 * is the exact failure D-11 exists to prevent.
 *
 * The diff is READ, never recomputed. Re-running the probe at render time would
 * read the target as it is NOW, so a reviewer opening a row after the write
 * landed would see a diff that never existed.
 */

type Schemas = components["schemas"];

export type CompanionToolCallDetail = Schemas["CompanionToolCallDetailResponse"];
export type CompanionApproval = Schemas["CompanionApprovalResponse"];
type JsonValue = Schemas["JsonValue"];

/** Section headings and the fixed copy the spec assigns to this drawer. */
export const CALL_DETAIL_COPY = {
	title: "Tool call",
	decisionHeading: "Decision",
	provenanceHeading: "Provenance",
	dryRunHeading: "What would change",
	companionContentHeading: "Companion content",
	dryRunClean: "Nothing would change.",
	/** `{reason}` is filled from `error_text`; see `dryRunFailureCopy`. */
	dryRunFailed: "This would fail before it changed anything: {reason}",
	/** The honest form when the preview failed and recorded no reason at all. */
	dryRunFailedWithoutReason: "This preview did not complete, and no reason was recorded.",
	/** Rendered beside the computed entries of a partial diff. */
	dryRunUndetermined: "The façade could not determine:",
	errorTitle: "Couldn't load this call",
	/**
	 * A row belonging to somebody else comes back as a 404, not a 403 — the
	 * route is deliberate about that, because a 403 would confirm the row exists
	 * and turn the detail endpoint into a membership oracle over a stranger's
	 * activity. So the copy says NOT FOUND and never says forbidden.
	 */
	notFound: "That call is not in this log. It may have been purged, or it may never have existed.",
	reviewNotFound:
		"This approval could not be linked to a recorded call, so the chain below is not shown.",
} as const;

/**
 * The best-effort disclosure the "Prompted by" row always carries.
 *
 * `05-UI-SPEC.md` assigns this sentence; the server owns a longer one
 * (`source_content_disclosure`) that travels WITH `source_content_ref` and is
 * present exactly when the reference is. `promptedByDisclosure` prefers the
 * server's wording verbatim when there is one — plan 05-07 shipped it precisely
 * so no renderer writes a second sentence — and falls back to this so the
 * disclosure is present even when there is no reference to disclose about.
 * Unconditional either way: the correlation is an ordering the server OBSERVED,
 * never a cause it established, and a disclosure each renderer had to remember
 * is one that eventually gets forgotten.
 */
export const PROMPTED_BY_DISCLOSURE =
	"Best-effort: attributed to the most recent companion content in this session.";

export function promptedByDisclosure(
	detail: Pick<CompanionToolCallDetail, "source_content_disclosure"> | null | undefined,
): string {
	const served = detail?.source_content_disclosure;
	return served && served.trim() !== "" ? served : PROMPTED_BY_DISCLOSURE;
}

/** The six links, in the one order they read as a path rather than a property grid. */
export const PROVENANCE_ROW_KINDS = [
	"session",
	"agent",
	"prompted-by",
	"arguments",
	"call-id",
	"parent",
] as const;

export type ProvenanceRowKind = (typeof PROVENANCE_ROW_KINDS)[number];

export const PROVENANCE_LABELS: Record<ProvenanceRowKind, string> = {
	session: "Session",
	agent: "Agent",
	"prompted-by": "Prompted by",
	arguments: "Arguments",
	"call-id": "Call identifier",
	parent: "Parent call",
};

export interface ProvenanceRow {
	kind: ProvenanceRowKind;
	label: string;
	/**
	 * `recorded` carries a value; `not-recorded` renders the muted
	 * "Not recorded"; `pending` is still in flight on the detail request and
	 * renders a skeleton — a different claim from "there was none".
	 */
	state: "recorded" | "not-recorded" | "pending";
	/** The recorded value, or null. Never a placeholder standing in for one. */
	value: string | null;
	/** What the row displays. `NOT_RECORDED` when nothing was recorded. */
	display: string;
	/** Render in mono: identifiers and payloads, not prose. */
	mono: boolean;
	/** A line rendered beneath the row. Only the "Prompted by" row has one. */
	note?: string;
	/** The call this row links to. Only ever set on the parent row. */
	linkCallId?: string;
}

/** The redacted argument payload as one readable block. */
export function formatArgumentPayload(
	preview: Record<string, JsonValue> | null | undefined,
): string | null {
	if (!preview || Object.keys(preview).length === 0) return null;
	return JSON.stringify(preview, null, 2);
}

/**
 * The chain for one call, top-down, in a fixed order.
 *
 * The parent row is OMITTED when the call is not a retry after approval, rather
 * than rendered with a dead link. Every other row is always present: a chain
 * with every link unrecorded still renders five rows reading "Not recorded",
 * because a collapsed section reads as "there is no chain" when the truth is
 * "the chain was not recorded".
 *
 * `agentLabel` exists because `environment_id` is a raw identifier and plan
 * 05-07 exposed no name for it. The page already knows which agent it is, so it
 * supplies the name for its OWN id and the row falls back to the identifier for
 * anything else rather than inventing a label.
 */
export function provenanceRows(
	row: Pick<CompanionToolCall, "session_id" | "environment_id" | "source_content_ref">,
	detail: CompanionToolCallDetail | null | undefined,
	options: { agentId?: string; agentLabel?: string } = {},
): ProvenanceRow[] {
	const recorded = (
		kind: ProvenanceRowKind,
		value: string | null,
		mono: boolean,
		extra: Partial<ProvenanceRow> = {},
	): ProvenanceRow => ({
		kind,
		label: PROVENANCE_LABELS[kind],
		state: value === null || value === "" ? "not-recorded" : "recorded",
		value: value === null || value === "" ? null : value,
		display: value === null || value === "" ? NOT_RECORDED : value,
		mono,
		...extra,
	});

	const agentValue =
		row.environment_id === null
			? null
			: options.agentLabel && options.agentId === row.environment_id
				? options.agentLabel
				: row.environment_id;

	const rows: ProvenanceRow[] = [
		recorded("session", row.session_id, true),
		recorded("agent", agentValue, options.agentLabel !== agentValue),
		// The disclosure is attached unconditionally — with a resolved reference
		// and without one alike.
		recorded("prompted-by", row.source_content_ref, true, {
			note: promptedByDisclosure(detail),
		}),
	];

	if (detail) {
		rows.push(recorded("arguments", formatArgumentPayload(detail.arguments_preview), true));
		rows.push(recorded("call-id", detail.call_jti, true));
	} else {
		// In flight, not absent. A skeleton, never "Not recorded".
		rows.push(pendingRow("arguments"));
		rows.push(pendingRow("call-id"));
	}

	// Only when the call is a retry after approval. Absent, the row is omitted
	// rather than rendered with a link that goes nowhere.
	const parentId = detail?.parent_call_id ?? null;
	if (parentId) {
		rows.push(recorded("parent", parentId, true, { linkCallId: parentId }));
	}

	return rows;
}

function pendingRow(kind: ProvenanceRowKind): ProvenanceRow {
	return {
		kind,
		label: PROVENANCE_LABELS[kind],
		state: "pending",
		value: null,
		display: "",
		mono: true,
	};
}

/** One before-and-after pair. Either side may be absent — a create has no before. */
export interface DiffEntry {
	before: string | null;
	after: string | null;
}

export type DryRunSectionState =
	/** Not a dry run: the whole section is omitted. */
	| { kind: "absent" }
	/** A dry run whose detail has not landed yet. The heading is already up. */
	| { kind: "loading" }
	/** The preview never completed. `reason` is verbatim from `error_text`. */
	| { kind: "failed"; reason: string | null }
	/** The preview ran and nothing would change. NOT the same as "nobody looked". */
	| { kind: "clean"; target: string | null }
	/** The preview ran and these entries would change. */
	| { kind: "populated"; target: string | null; entries: DiffEntry[] }
	/** The preview ran but could not determine everything. Never rendered as complete. */
	| {
			kind: "partial";
			target: string | null;
			entries: DiffEntry[];
			undetermined: string[];
	  };

/**
 * What the "What would change" section is.
 *
 * `absent` and `failed` are separated from `clean` on purpose: a preview that
 * errored before it read anything records `dry_run_diff: null` and its reason in
 * `error_text`, and rendering that as a clean diff is the failure D-11 exists to
 * prevent. `partial` takes precedence over both `clean` and `populated`, because
 * a diff with anything undetermined must never render as complete — the complete
 * LOOKING one is the shape an approver trusts most and should trust least.
 */
export function dryRunSectionState(
	row: Pick<CompanionToolCall, "dry_run">,
	detail: CompanionToolCallDetail | null | undefined,
): DryRunSectionState {
	if (!row.dry_run) return { kind: "absent" };
	if (!detail) return { kind: "loading" };

	const diff = detail.dry_run_diff;
	if (diff === null || diff === undefined) {
		return { kind: "failed", reason: dryRunFailureReason(detail.error_text) };
	}

	const target = typeof diff.target === "string" && diff.target !== "" ? diff.target : null;
	const entries = diffEntries(diff);
	const undetermined = Array.isArray(diff.undetermined)
		? diff.undetermined.filter((item): item is string => typeof item === "string")
		: [];

	if (undetermined.length > 0) return { kind: "partial", target, entries, undetermined };
	if (entries.length === 0) return { kind: "clean", target };
	return { kind: "populated", target, entries };
}

/** Pair `before` and `after` by index. A missing side stays null rather than blank. */
export function diffEntries(diff: Record<string, JsonValue>): DiffEntry[] {
	const before = Array.isArray(diff.before) ? diff.before : [];
	const after = Array.isArray(diff.after) ? diff.after : [];
	const length = Math.max(before.length, after.length);
	const entries: DiffEntry[] = [];
	for (let index = 0; index < length; index += 1) {
		entries.push({
			before: index < before.length ? stringifyDiffSide(before[index]) : null,
			after: index < after.length ? stringifyDiffSide(after[index]) : null,
		});
	}
	return entries;
}

function stringifyDiffSide(value: unknown): string {
	if (typeof value === "string") return value;
	return JSON.stringify(value, null, 2) ?? "";
}

/**
 * The reason a preview failed, pulled out of the prose the façade recorded.
 *
 * Plan 05-07 note 2: there is no structured `failure_reason` — `error_text`
 * carries `"{tool} could not be previewed: {reason}"`. The prefix is stripped so
 * the spec's sentence does not read "This would fail before it changed anything:
 * filebrowser__delete_path could not be previewed: …". When the shape is not the
 * one the façade writes, the whole recorded text is used rather than a guess.
 */
export function dryRunFailureReason(errorText: string | null | undefined): string | null {
	if (!errorText || errorText.trim() === "") return null;
	const marker = " could not be previewed: ";
	const at = errorText.indexOf(marker);
	if (at === -1) return errorText;
	const reason = errorText.slice(at + marker.length).trim();
	return reason === "" ? errorText : reason;
}

/** The failed-preview sentence, with the honest form when no reason was recorded. */
export function dryRunFailureCopy(reason: string | null): string {
	if (reason === null) return CALL_DETAIL_COPY.dryRunFailedWithoutReason;
	return CALL_DETAIL_COPY.dryRunFailed.replace("{reason}", reason);
}

/**
 * The fixed tenant-facing sentences for the Phase 6 refusal decisions
 * (`06-UI-SPEC.md` S7). `call-detail.states.test.tsx` holds them against the
 * contract rows.
 */
export const DECISION_REASON_COPY = {
	/** S7 over-budget refusal reason (detail sheet). */
	budgetCeiling:
		"Refused: this month's companion spend ceiling is reached. The ceiling is an account setting — it cannot be raised from a tool argument.",
	/** S7 breaker refusal reason (detail sheet). `{n}` is the repetition count. */
	repeatedCall:
		"Stopped: this call repeated {n} times with the same arguments. The loop was broken before it spent more of your budget.",
	/**
	 * The S7 "partial" form: a breaker row that carries no count drops the
	 * count clause rather than rendering an unsubstituted or undefined `n`.
	 */
	repeatedCallWithoutCount:
		"Stopped: this call repeated with the same arguments. The loop was broken before it spent more of your budget.",
	/**
	 * A UI-SPEC addition: plan 06-24 added `refused_group_disabled` after the
	 * spec was approved, so this sentence has no contract row yet. It follows
	 * the over-budget sentence's voice — blocking is an account setting, not
	 * something a tool argument can change.
	 */
	groupDisabled:
		"Refused: this companion's tools are blocked. Blocking is an account setting — it cannot be changed from a tool argument.",
} as const;

/**
 * The breaker fires only above its threshold, so a count it recorded is an
 * integer of at least 2 and "repeated {n} times" is always plural. Anything
 * else is not a count the breaker could have written, and is dropped.
 */
function breakerCount(repeatCount: number | null): number | null {
	if (repeatCount === null || !Number.isInteger(repeatCount) || repeatCount < 2) return null;
	return repeatCount;
}

/**
 * The sentence the Decision section renders for a Phase 6 refusal, or null.
 *
 * The three Phase 6 guards — the spend ceiling, the loop breaker and a tool
 * block — record an `error_text` written for the AGENT, carrying a refusal
 * token the model reads. The tenant gets a fixed sentence INSTEAD of that text:
 * it interpolates at most one integer and never a tool argument, a tool name or
 * anything the agent supplied. Every Phase 5 decision returns null, so those
 * rows keep rendering the reason they recorded.
 */
export function decisionReasonSentence(
	decision: CallDecision,
	repeatCount: number | null,
): string | null {
	switch (decision) {
		case "refused_budget_ceiling":
			return DECISION_REASON_COPY.budgetCeiling;
		case "refused_repeated_call": {
			const count = breakerCount(repeatCount);
			return count === null
				? DECISION_REASON_COPY.repeatedCallWithoutCount
				: DECISION_REASON_COPY.repeatedCall.replace("{n}", String(count));
		}
		case "refused_group_disabled":
			return DECISION_REASON_COPY.groupDisabled;
		default:
			return null;
	}
}

/**
 * The project's 160-character error truncation, for the TABLE cell only.
 *
 * The Sheet leaves the reason whole and wraps it: the reviewer there is about to
 * authorize a write and needs the sentence, not its first line.
 */
export const REASON_DISPLAY_MAX = 160;

export function truncateReason(reason: string): string {
	return truncateForDisplay(reason, REASON_DISPLAY_MAX);
}

/**
 * The 12-character mono prefix an identifier renders as, with the full value
 * kept in a `title` by the component.
 */
export const IDENTIFIER_PREFIX_LENGTH = 12;

export function shortIdentifier(value: string): string {
	return value.length <= IDENTIFIER_PREFIX_LENGTH
		? value
		: `${value.slice(0, IDENTIFIER_PREFIX_LENGTH)}…`;
}

/**
 * Which recorded call a pending approval belongs to — a CANDIDATE, not an answer.
 *
 * There is no approval→call route and no `approval_id` on the listing row, so
 * the candidate is chosen by the only signal the listing carries: among the
 * tenant's `pending_approval` rows for the same tool, the one recorded closest
 * to the approval's own timestamp. The façade writes both in the same request,
 * so that is usually exact — but "usually" is not a claim to render.
 *
 * The caller therefore CONFIRMS the candidate by fetching its detail and
 * checking `approval_id`. An unconfirmed candidate is never shown: the drawer
 * says it could not link the approval rather than displaying somebody else's
 * chain to a reviewer who is about to approve a write.
 */
export function matchApprovalToCall(
	items: readonly CompanionToolCall[],
	approval: Pick<CompanionApproval, "tool" | "created_at">,
): string | null {
	const at = Date.parse(approval.created_at);
	let best: { id: string; distance: number } | null = null;
	for (const row of items) {
		if (row.decision !== "pending_approval") continue;
		if (row.tool !== approval.tool) continue;
		const distance = Math.abs(Date.parse(row.created_at) - at);
		if (!Number.isFinite(distance)) continue;
		if (best === null || distance < best.distance) best = { id: row.id, distance };
	}
	return best?.id ?? null;
}

/** True when the fetched detail really is the call this approval names. */
export function confirmsApproval(
	detail: Pick<CompanionToolCallDetail, "approval_id"> | null | undefined,
	approvalId: string,
): boolean {
	return Boolean(detail && detail.approval_id === approvalId);
}
