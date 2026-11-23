import type { components } from "@clawdi/shared/api";
import type { CapUnit } from "./tool-policy.logic";

/**
 * Blast-radius cap parsing, dirty tracking and payload assembly.
 *
 * Two properties are load-bearing and both live here rather than in the
 * component, because both are the kind of thing a render accidentally undoes:
 *
 * 1. **A cap of zero is a VALUE, not an absence.** Zero means "refuse
 *    everything", which is the tightest policy a tenant can set.
 *    `backend/app/core/companion_caps.py` names reading zero as falsy and
 *    substituting a default as the single most likely way this guarantee is
 *    lost — it turns the tightest possible policy into no policy at all. So
 *    parsing distinguishes `0` from unset, and rendering never treats `0` as
 *    empty.
 * 2. **Caps save as one unit.** All six ceilings are required with no defaults
 *    on the wire, so a payload is assembled only when all six parse. There is
 *    no partially-saved state to render because there is no partial payload.
 */

type Schemas = components["schemas"];

export type CapVector = Schemas["CompanionCapVectorResponse"];
export type CapVectorRequest = Schemas["CompanionCapVectorRequest"];
export type CapList = Schemas["CompanionCapListResponse"];
export type ConsumptionResponse = Schemas["CompanionConsumptionResponse"];
export type ConsumptionItem = Schemas["CompanionConsumptionItemResponse"];

/** The six cap scopes, in the order the form lays them out. */
export const CAP_FIELDS = [
	"per_call_calls",
	"per_call_records",
	"per_call_bytes",
	"per_session_calls",
	"per_session_records",
	"per_session_bytes",
] as const;

export type CapField = (typeof CAP_FIELDS)[number];

/** The form's working values: raw strings, so an in-progress edit survives. */
export type CapDraft = Record<CapField, string>;

const MEBIBYTE = 1024 * 1024;

/**
 * A MIRROR of `DEFAULT_CAPS` in `backend/app/core/companion_caps.py`, used
 * ONLY when the caps list did not cover a companion at all.
 *
 * It is not the authority and must never be used to decide whether a tenant
 * chose their numbers: `GET /v1/companions/caps` returns one vector per
 * deployed companion — the shipped defaults included, marked `is_default` —
 * so in normal operation this constant is never reached. `is_default` comes
 * from ROW EXISTENCE on the server and is never re-derived by comparing
 * values to these.
 */
export const SHIPPED_DEFAULT_CAPS = {
	per_call_calls: 1,
	per_call_records: 50,
	per_call_bytes: 10 * MEBIBYTE,
	per_session_calls: 200,
	per_session_records: 500,
	per_session_bytes: 200 * MEBIBYTE,
} as const satisfies Record<CapField, number>;

export const CAPS_COPY = {
	heading: "Blast-radius caps",
	helper:
		"Caps are enforced by the façade before a call is authorized. An agent cannot raise them, and asking it to will not change the answer.",
	perCallLabel: "Per call",
	perCallHelper: "The most one call may touch.",
	perSessionLabel: "Per session",
	perSessionHelper:
		"The budget a single agent session may spend before every further call is refused.",
	save: "Save caps",
	savedToast: "Caps saved",
	defaultsMarker: "Shipped defaults",
	savedMarker: "Saved by you",
} as const;

export const CAPS_SAVE_ERROR_COPY = "The caps were not saved. The old limits are still in force.";

export const CAP_FIELD_ERROR_COPY = "Enter a whole number of zero or more.";

/** Unit nouns, never enum keys. */
export const CAP_UNIT_LABELS: Record<CapUnit, string> = {
	calls: "Calls",
	records: "Records",
	bytes: "Bytes",
};

export interface CapFieldDefinition {
	field: CapField;
	scope: "per_call" | "per_session";
	unit: CapUnit;
	label: string;
}

export const CAP_FIELD_DEFINITIONS: readonly CapFieldDefinition[] = CAP_FIELDS.map((field) => {
	const scope: CapFieldDefinition["scope"] = field.startsWith("per_call")
		? "per_call"
		: "per_session";
	const unit: CapUnit = field.endsWith("calls")
		? "calls"
		: field.endsWith("records")
			? "records"
			: "bytes";
	return { field, scope, unit, label: CAP_UNIT_LABELS[unit] };
});

export interface ParsedCapField {
	/** The parsed ceiling. `0` is a real value; `null` means the field is unset. */
	value: number | null;
	/** True when the field carried no characters at all. */
	unset: boolean;
	error: string | null;
}

/**
 * Parse one cap input.
 *
 * Zero and unset are DIFFERENT results — that distinction is the whole reason
 * this function exists rather than a `Number(raw) || default` at the call site.
 */
export function parseCapField(raw: string): ParsedCapField {
	const trimmed = raw.trim();
	if (trimmed === "") return { value: null, unset: true, error: null };
	if (!/^\d+$/.test(trimmed)) return { value: null, unset: false, error: CAP_FIELD_ERROR_COPY };
	const value = Number.parseInt(trimmed, 10);
	if (!Number.isSafeInteger(value)) {
		return { value: null, unset: false, error: CAP_FIELD_ERROR_COPY };
	}
	return { value, unset: false, error: null };
}

export interface ByteBudget {
	/** The number the input holds, in `unit`. */
	value: number;
	unit: ByteUnit;
	/** The number and its unit as one label, never a raw byte count. */
	label: string;
}

export type ByteUnit = "B" | "KiB" | "MiB" | "GiB" | "TiB";

const BYTE_UNITS: readonly { unit: ByteUnit; factor: number }[] = [
	{ unit: "TiB", factor: 1024 ** 4 },
	{ unit: "GiB", factor: 1024 ** 3 },
	{ unit: "MiB", factor: 1024 ** 2 },
	{ unit: "KiB", factor: 1024 },
	{ unit: "B", factor: 1 },
];

/**
 * Render a byte ceiling with a unit suffix.
 *
 * The largest unit that divides the value EXACTLY is chosen, so the number in
 * the input round-trips through `bytesFromBudget` without losing a byte. A
 * budget that divides nothing evenly stays in bytes — with a `B` suffix, so
 * even that case is a number plus a unit rather than a raw count.
 */
export function formatByteBudget(bytes: number): ByteBudget {
	for (const { unit, factor } of BYTE_UNITS) {
		if (bytes !== 0 && bytes % factor === 0) {
			return { value: bytes / factor, unit, label: `${bytes / factor} ${unit}` };
		}
	}
	return { value: bytes, unit: "B", label: `${bytes} B` };
}

/** The inverse of `formatByteBudget`, for turning an edited input back. */
export function bytesFromBudget(value: number, unit: ByteUnit): number {
	const entry = BYTE_UNITS.find((candidate) => candidate.unit === unit);
	return value * (entry?.factor ?? 1);
}

/** The vector the form starts from — the server's, or the shipped fallback. */
export function capVectorFor(
	list: CapList | undefined,
	companionKey: string,
): CapVector | undefined {
	const saved = list?.items.find((item) => item.companion_key === companionKey);
	if (saved) return saved;
	if (!list) return undefined;
	return {
		companion_key: companionKey,
		...SHIPPED_DEFAULT_CAPS,
		is_default: true,
		updated_at: null,
	};
}

/**
 * The draft the form renders from a saved vector.
 *
 * Every field is populated: an unsaved tenant sees the shipped numbers as
 * values, never blank inputs. Byte fields carry the unit-scaled number that
 * pairs with the `InputGroup` suffix.
 */
export function capDraftFrom(vector: CapVector): CapDraft {
	return {
		per_call_calls: String(vector.per_call_calls),
		per_call_records: String(vector.per_call_records),
		per_call_bytes: String(formatByteBudget(vector.per_call_bytes).value),
		per_session_calls: String(vector.per_session_calls),
		per_session_records: String(vector.per_session_records),
		per_session_bytes: String(formatByteBudget(vector.per_session_bytes).value),
	};
}

/** The unit each byte field's input is expressed in, from the saved vector. */
export function byteUnitsFor(
	vector: CapVector,
): Record<"per_call_bytes" | "per_session_bytes", ByteUnit> {
	return {
		per_call_bytes: formatByteBudget(vector.per_call_bytes).unit,
		per_session_bytes: formatByteBudget(vector.per_session_bytes).unit,
	};
}

/** True once any single field differs from what is saved. */
export function capsAreDirty(draft: CapDraft, vector: CapVector): boolean {
	const saved = capDraftFrom(vector);
	return CAP_FIELDS.some((field) => draft[field].trim() !== saved[field]);
}

/**
 * Assemble the request body, or `null` when any of the six does not parse.
 *
 * Returning `null` rather than a partial body is what makes "caps save as one
 * unit" true on this side of the wire as well: a client that filled five
 * fields cannot widen the sixth by omitting it.
 */
export function buildCapsPayload(
	draft: CapDraft,
	byteUnits: Record<"per_call_bytes" | "per_session_bytes", ByteUnit>,
): CapVectorRequest | null {
	const parsed = {} as Record<CapField, number>;
	for (const field of CAP_FIELDS) {
		const result = parseCapField(draft[field]);
		if (result.value === null) return null;
		parsed[field] = result.value;
	}
	return {
		...parsed,
		per_call_bytes: bytesFromBudget(parsed.per_call_bytes, byteUnits.per_call_bytes),
		per_session_bytes: bytesFromBudget(parsed.per_session_bytes, byteUnits.per_session_bytes),
	};
}

/** Per-field validation messages for the current draft. */
export function capFieldErrors(draft: CapDraft): Partial<Record<CapField, string>> {
	const errors: Partial<Record<CapField, string>> = {};
	for (const field of CAP_FIELDS) {
		const result = parseCapField(draft[field]);
		if (result.error) errors[field] = result.error;
		else if (result.unset) errors[field] = CAP_FIELD_ERROR_COPY;
	}
	return errors;
}

/**
 * One line of the live read-out.
 *
 * **The copy says "used since {time}", not "used this session".** The MCP
 * protocol as this backend speaks it delivers no session identifier to the
 * façade, so `GET /v1/companions/consumption` meters a rolling window and
 * reports an explicit null session id. A read-out labelled "this session"
 * would name a scope the server does not meter — the same class of defect as
 * displaying a policy mode the server does not gate on.
 */
export function capsReadoutLine(
	used: string,
	budget: string,
	unit: string,
	sinceLabel: string,
): string {
	// A byte value already carries its unit inside the formatted label, so it
	// passes an empty unit rather than naming one twice.
	const measured = unit ? `${used} of ${budget} ${unit}` : `${used} of ${budget}`;
	return `${measured} used since ${sinceLabel}`;
}

/**
 * The short clock label the read-out sentence interpolates.
 *
 * Short on purpose: the metered window is one hour wide, so the hour and
 * minute answer "since when" without a date. The full timestamp stays one
 * hover away through `TimeTooltip`, which is the project's existing idiom for
 * every time value.
 */
export function formatWindowStart(value: string | null | undefined): string {
	if (!value) return "—";
	const moment = new Date(value);
	if (Number.isNaN(moment.getTime())) return "—";
	return moment.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

export interface CapsReadoutRow {
	unit: CapUnit;
	used: string;
	budget: string;
	sentence: string;
}

/** The three read-out rows for one companion's current window. */
export function capsReadoutRows(
	item: ConsumptionItem | undefined,
	vector: CapVector,
	sinceLabel: string,
): readonly CapsReadoutRow[] {
	const usedCalls = item?.calls_consumed ?? 0;
	const usedRecords = item?.records_consumed ?? 0;
	const usedBytes = item?.bytes_consumed ?? 0;
	return [
		row("calls", String(usedCalls), String(vector.per_session_calls), sinceLabel),
		row("records", String(usedRecords), String(vector.per_session_records), sinceLabel),
		row(
			"bytes",
			formatByteBudget(usedBytes).label,
			formatByteBudget(vector.per_session_bytes).label,
			sinceLabel,
		),
	];
}

function row(unit: CapUnit, used: string, budget: string, sinceLabel: string): CapsReadoutRow {
	return {
		unit,
		used,
		budget,
		sentence: capsReadoutLine(used, budget, unit === "bytes" ? "" : unit, sinceLabel),
	};
}

/** True when this window has already spent something against a ceiling. */
export function windowHasConsumption(item: ConsumptionItem | undefined): boolean {
	if (!item) return false;
	return item.calls_consumed > 0 || item.records_consumed > 0 || item.bytes_consumed > 0;
}

/**
 * True when the draft raises a per-session ceiling above what is saved.
 *
 * Raising one while the metered window already has spend routes through a
 * confirmation, because the new budget takes effect on the NEXT call and calls
 * already refused are not retried. The UI spec words this as "while a session
 * is live"; there is no session, so the live window is the honest stand-in.
 */
export function raisesSessionBudget(
	draft: CapDraft,
	vector: CapVector,
	byteUnits: Record<"per_call_bytes" | "per_session_bytes", ByteUnit>,
): boolean {
	const payload = buildCapsPayload(draft, byteUnits);
	if (!payload) return false;
	return (
		payload.per_session_calls > vector.per_session_calls ||
		payload.per_session_records > vector.per_session_records ||
		payload.per_session_bytes > vector.per_session_bytes
	);
}

/** The "Raise this budget" confirmation, from the UI spec's table. */
export const RAISE_BUDGET_COPY = {
	title: "Raise this budget",
	description:
		"The new budget takes effect on the next call. Calls already refused in this session are not retried automatically.",
	confirmLabel: "Raise budget",
	destructive: false,
} as const;
