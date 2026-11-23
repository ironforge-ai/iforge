import { describe, expect, test } from "bun:test";
import {
	buildCapsPayload,
	byteUnitsFor,
	CAP_FIELD_DEFINITIONS,
	CAP_FIELDS,
	CAPS_SAVE_ERROR_COPY,
	type CapDraft,
	type CapVector,
	type ConsumptionItem,
	capDraftFrom,
	capFieldErrors,
	capsAreDirty,
	capsReadoutLine,
	capsReadoutRows,
	capVectorFor,
	formatByteBudget,
	formatWindowStart,
	parseCapField,
	RAISE_BUDGET_COPY,
	raisesSessionBudget,
	SHIPPED_DEFAULT_CAPS,
	windowHasConsumption,
} from "./caps-form.logic";

const MEBIBYTE = 1024 * 1024;

function vector(overrides: Partial<CapVector> = {}): CapVector {
	return {
		companion_key: "filebrowser",
		...SHIPPED_DEFAULT_CAPS,
		is_default: true,
		updated_at: null,
		...overrides,
	};
}

function consumption(overrides: Partial<ConsumptionItem> = {}): ConsumptionItem {
	return {
		companion_key: "filebrowser",
		environment_id: "6c3b1a2e-0000-4000-8000-000000000001",
		session_id: null,
		calls_consumed: 0,
		records_consumed: 0,
		bytes_consumed: 0,
		per_session_calls: SHIPPED_DEFAULT_CAPS.per_session_calls,
		per_session_records: SHIPPED_DEFAULT_CAPS.per_session_records,
		per_session_bytes: SHIPPED_DEFAULT_CAPS.per_session_bytes,
		...overrides,
	};
}

describe("the six cap fields", () => {
	test("cover per-call and per-session across calls, records and bytes", () => {
		expect(CAP_FIELDS).toHaveLength(6);
		expect(CAP_FIELD_DEFINITIONS.filter((d) => d.scope === "per_call")).toHaveLength(3);
		expect(CAP_FIELD_DEFINITIONS.filter((d) => d.scope === "per_session")).toHaveLength(3);
		expect(CAP_FIELD_DEFINITIONS.map((d) => d.unit)).toEqual([
			"calls",
			"records",
			"bytes",
			"calls",
			"records",
			"bytes",
		]);
		// Units are nouns, not enum keys.
		expect(CAP_FIELD_DEFINITIONS.map((d) => d.label)).toEqual([
			"Calls",
			"Records",
			"Bytes",
			"Calls",
			"Records",
			"Bytes",
		]);
	});
});

describe("parseCapField", () => {
	test("zero and unset are DIFFERENT results", () => {
		const zero = parseCapField("0");
		const unset = parseCapField("");
		expect(zero.value).toBe(0);
		expect(zero.unset).toBe(false);
		expect(zero.error).toBeNull();
		expect(unset.value).toBeNull();
		expect(unset.unset).toBe(true);
		// The whole point: they must not collapse into each other, because
		// zero is the tightest policy a tenant can set and unset is no policy.
		expect(zero).not.toEqual(unset);
	});

	test("a negative or fractional value is refused rather than coerced", () => {
		expect(parseCapField("-1").error).not.toBeNull();
		expect(parseCapField("1.5").error).not.toBeNull();
		expect(parseCapField("many").error).not.toBeNull();
		expect(parseCapField("  12  ").value).toBe(12);
	});
});

describe("the shipped defaults", () => {
	test("mirror backend/app/core/companion_caps.py exactly", () => {
		expect(SHIPPED_DEFAULT_CAPS).toEqual({
			per_call_calls: 1,
			per_call_records: 50,
			per_call_bytes: 10 * MEBIBYTE,
			per_session_calls: 200,
			per_session_records: 500,
			per_session_bytes: 200 * MEBIBYTE,
		});
	});

	test("a companion the caps list did not cover renders them, never blank inputs", () => {
		const fallback = capVectorFor({ items: [] }, "filebrowser");
		expect(fallback).toBeDefined();
		expect(fallback?.per_call_records).toBe(SHIPPED_DEFAULT_CAPS.per_call_records);
		expect(fallback?.is_default).toBe(true);
		const draft = capDraftFrom(fallback as CapVector);
		for (const field of CAP_FIELDS) {
			expect(draft[field]).not.toBe("");
		}
	});

	test("is_default comes from the server's flag, never from comparing values", () => {
		// A tenant who saved the shipped numbers verbatim made a CHOICE. The
		// server clears the marker for them; nothing here re-derives it.
		const saved = vector({ is_default: false, updated_at: "2026-09-16T08:00:00Z" });
		expect(saved.per_call_records).toBe(SHIPPED_DEFAULT_CAPS.per_call_records);
		expect(saved.is_default).toBe(false);
		const fromList = capVectorFor({ items: [saved] }, "filebrowser");
		expect(fromList?.is_default).toBe(false);
	});
});

describe("formatByteBudget", () => {
	test("renders a unit suffix rather than a raw byte count", () => {
		expect(formatByteBudget(10 * MEBIBYTE)).toEqual({ value: 10, unit: "MiB", label: "10 MiB" });
		expect(formatByteBudget(1024)).toEqual({ value: 1, unit: "KiB", label: "1 KiB" });
	});

	test("a maximum-value budget still reads as a number plus a unit", () => {
		const maximum = 1024 ** 4 * 8;
		const budget = formatByteBudget(maximum);
		expect(budget.unit).toBe("TiB");
		expect(budget.label).toBe("8 TiB");
		expect(budget.label).not.toContain(String(maximum));
	});

	test("a value that divides nothing evenly stays in bytes, with a B suffix", () => {
		expect(formatByteBudget(1023).label).toBe("1023 B");
		expect(formatByteBudget(0).label).toBe("0 B");
	});
});

describe("capsAreDirty", () => {
	test("false for untouched values and true after any single field changes", () => {
		const saved = vector();
		const draft = capDraftFrom(saved);
		expect(capsAreDirty(draft, saved)).toBe(false);
		for (const field of CAP_FIELDS) {
			expect(capsAreDirty({ ...draft, [field]: "7" }, saved)).toBe(true);
		}
	});

	test("setting a field to zero is a change, not a clearing", () => {
		const saved = vector();
		const draft = capDraftFrom(saved);
		expect(capsAreDirty({ ...draft, per_session_calls: "0" }, saved)).toBe(true);
	});
});

describe("buildCapsPayload", () => {
	test("assembles all six, converting byte inputs back through their unit", () => {
		const saved = vector();
		const units = byteUnitsFor(saved);
		const payload = buildCapsPayload(capDraftFrom(saved), units);
		expect(payload).toEqual({
			per_call_calls: 1,
			per_call_records: 50,
			per_call_bytes: 10 * MEBIBYTE,
			per_session_calls: 200,
			per_session_records: 500,
			per_session_bytes: 200 * MEBIBYTE,
		});
	});

	test("returns null when any field does not parse — caps save as one unit", () => {
		const saved = vector();
		const units = byteUnitsFor(saved);
		const draft = capDraftFrom(saved);
		for (const field of CAP_FIELDS) {
			expect(buildCapsPayload({ ...draft, [field]: "" } as CapDraft, units)).toBeNull();
		}
	});

	test("a cap of zero survives assembly as zero", () => {
		const saved = vector();
		const units = byteUnitsFor(saved);
		const payload = buildCapsPayload({ ...capDraftFrom(saved), per_call_records: "0" }, units);
		expect(payload?.per_call_records).toBe(0);
	});

	test("every unset field is named as an error rather than silently defaulted", () => {
		const saved = vector();
		const draft = { ...capDraftFrom(saved), per_call_calls: "" };
		expect(capFieldErrors(draft).per_call_calls).toBeDefined();
		expect(capFieldErrors(capDraftFrom(saved))).toEqual({});
	});
});

describe("the live read-out", () => {
	test("says 'used since {time}', never 'used this session'", () => {
		const sentence = capsReadoutLine("48", "200", "calls", "08:14");
		expect(sentence).toBe("48 of 200 calls used since 08:14");
		expect(sentence).not.toContain("this session");
	});

	test("an unsaved tenant with no spend reads zero of the shipped budget", () => {
		const rows = capsReadoutRows(undefined, vector(), "08:14");
		expect(rows.map((row) => row.unit)).toEqual(["calls", "records", "bytes"]);
		expect(rows[0]?.sentence).toBe("0 of 200 calls used since 08:14");
		expect(rows[1]?.sentence).toBe("0 of 500 records used since 08:14");
		// Bytes name their unit through the formatted value, never twice.
		expect(rows[2]?.sentence).toBe("0 B of 200 MiB used since 08:14");
	});

	test("a cap of zero renders as zero rather than as empty or absent", () => {
		const rows = capsReadoutRows(consumption(), vector({ per_session_calls: 0 }), "08:14");
		expect(rows[0]?.sentence).toBe("0 of 0 calls used since 08:14");
		expect(rows[0]?.budget).toBe("0");
	});

	test("no unit is omitted from the read-out", () => {
		const rows = capsReadoutRows(
			consumption({ calls_consumed: 3, records_consumed: 9, bytes_consumed: 2 * MEBIBYTE }),
			vector(),
			"08:14",
		);
		expect(rows).toHaveLength(3);
		expect(rows[2]?.used).toBe("2 MiB");
		for (const row of rows) {
			expect(row.sentence).toContain("used since 08:14");
		}
	});

	test("an unknown window start renders an em dash rather than a fabricated time", () => {
		expect(formatWindowStart(null)).toBe("—");
		expect(formatWindowStart("not a time")).toBe("—");
		expect(formatWindowStart("2026-09-16T08:14:00Z")).not.toBe("—");
	});
});

describe("raising a session budget", () => {
	test("only confirms when the metered window has already spent something", () => {
		expect(windowHasConsumption(undefined)).toBe(false);
		expect(windowHasConsumption(consumption())).toBe(false);
		expect(windowHasConsumption(consumption({ calls_consumed: 1 }))).toBe(true);
		expect(windowHasConsumption(consumption({ bytes_consumed: 1 }))).toBe(true);
	});

	test("detects a raise on any of the three per-session ceilings, and not a lowering", () => {
		const saved = vector();
		const units = byteUnitsFor(saved);
		const draft = capDraftFrom(saved);
		expect(raisesSessionBudget(draft, saved, units)).toBe(false);
		expect(raisesSessionBudget({ ...draft, per_session_calls: "500" }, saved, units)).toBe(true);
		expect(raisesSessionBudget({ ...draft, per_session_records: "900" }, saved, units)).toBe(true);
		expect(raisesSessionBudget({ ...draft, per_session_bytes: "400" }, saved, units)).toBe(true);
		expect(raisesSessionBudget({ ...draft, per_session_calls: "10" }, saved, units)).toBe(false);
		// Tightening a PER-CALL ceiling is not a session raise.
		expect(raisesSessionBudget({ ...draft, per_call_calls: "99" }, saved, units)).toBe(false);
	});

	test("carries the UI spec's copy verbatim, with no exclamation", () => {
		expect(RAISE_BUDGET_COPY.title).toBe("Raise this budget");
		expect(RAISE_BUDGET_COPY.confirmLabel).toBe("Raise budget");
		expect(RAISE_BUDGET_COPY.description).toContain("takes effect on the next call");
		expect(RAISE_BUDGET_COPY.description).toContain("not retried automatically");
		expect(CAPS_SAVE_ERROR_COPY).toBe(
			"The caps were not saved. The old limits are still in force.",
		);
		expect(CAPS_SAVE_ERROR_COPY).not.toContain("!");
	});
});
