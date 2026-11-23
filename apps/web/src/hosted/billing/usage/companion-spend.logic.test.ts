/**
 * S6's money, ceiling and presentation rules (06-UI-SPEC §S6), as pure
 * functions: integer micros in, display strings out, with no floating point
 * between them.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { uiSpecCopy, uiSpecRowQuotes } from "@/lib/ui-spec-contract";
import {
	COMPANION_SPEND_COPY,
	canSaveCeiling,
	ceilingInputValue,
	ceilingReadOut,
	dimensionCell,
	microsToDollarString,
	parseCeilingInput,
	rowLabel,
	runSaveCeiling,
} from "./companion-spend.logic";

describe("microsToDollarString", () => {
	test("integer micros become an exact decimal dollar string", () => {
		expect(microsToDollarString(1_234_567)).toBe("1.234567");
	});

	test("zero is 0", () => {
		expect(microsToDollarString(0)).toBe("0");
	});

	test("whole dollars carry no fraction", () => {
		expect(microsToDollarString(5_000_000)).toBe("5");
	});

	test("sub-dollar amounts keep their leading zero and drop trailing zeros", () => {
		expect(microsToDollarString(450_000)).toBe("0.45");
		expect(microsToDollarString(1)).toBe("0.000001");
	});

	test("a value that is not a safe integer produces no digits at all", () => {
		expect(microsToDollarString(1.5)).toBe("");
		expect(microsToDollarString(Number.POSITIVE_INFINITY)).toBe("");
	});
});

describe("parseCeilingInput", () => {
	test("whole dollars parse to micros", () => {
		expect(parseCeilingInput("25")).toBe(25_000_000);
	});

	test("zero is a valid refuse-everything value", () => {
		expect(parseCeilingInput("0")).toBe(0);
	});

	test("cents parse exactly", () => {
		expect(parseCeilingInput("12.50")).toBe(12_500_000);
	});

	test("surrounding whitespace is ignored", () => {
		expect(parseCeilingInput(" 25 ")).toBe(25_000_000);
	});

	test("six fractional digits are the finest precision accepted", () => {
		expect(parseCeilingInput("1.000001")).toBe(1_000_001);
	});

	test.each([
		["-1", "a negative amount"],
		["abc", "not a number"],
		["1.0000001", "sub-micro precision"],
		["", "an empty field"],
		["1e3", "exponent notation"],
		["1,000", "a grouping separator"],
		[".5", "a missing whole part"],
		["99999999999", "more than a safe integer of micros"],
	])("%p is invalid (%s)", (raw) => {
		expect(parseCeilingInput(raw)).toBeNull();
	});
});

describe("ceiling input and save gate", () => {
	test("the stored ceiling fills the input as exact dollars, and 0 renders as 0", () => {
		expect(ceilingInputValue(25_000_000)).toBe("25");
		expect(ceilingInputValue(0)).toBe("0");
		expect(ceilingInputValue(null)).toBe("");
	});

	test("Save is disabled until the parsed value differs from the stored one", () => {
		expect(canSaveCeiling("25", 25_000_000)).toBe(false);
		expect(canSaveCeiling("25.00", 25_000_000)).toBe(false);
		expect(canSaveCeiling("30", 25_000_000)).toBe(true);
		expect(canSaveCeiling("0", null)).toBe(true);
		expect(canSaveCeiling("0", 25_000_000)).toBe(true);
	});

	test("Save stays disabled for invalid input or an unloaded ceiling", () => {
		expect(canSaveCeiling("-1", null)).toBe(false);
		expect(canSaveCeiling("", null)).toBe(false);
		expect(canSaveCeiling("25", undefined)).toBe(false);
	});
});

describe("dimensionCell", () => {
	test("an unmeasured dimension is an em dash with the not-yet-attributed tooltip", () => {
		expect(dimensionCell(null)).toEqual({
			text: "—",
			attributed: false,
			tooltip: COMPANION_SPEND_COPY.notAttributed,
		});
	});

	test("a measured zero is a fact and renders as formatted zero", () => {
		expect(dimensionCell(0)).toEqual({ text: "$0.00", attributed: true, tooltip: null });
	});

	test("a measured amount renders through formatUsdExact", () => {
		expect(dimensionCell(3_000_000).text).toBe("$3.00");
	});
});

describe("ceilingReadOut", () => {
	test("both amounts are formatted into the contract sentence", () => {
		expect(ceilingReadOut(1_200_000, 25_000_000)).toBe("$1.20 of $25.00 used this month");
	});

	test("a ceiling of 0 reads as zero, not blank", () => {
		expect(ceilingReadOut(0, 0)).toBe("$0.00 of $0.00 used this month");
	});
});

describe("rowLabel", () => {
	test("companion name and the sentence-case tier the install dialog uses", () => {
		expect(rowLabel("Postiz", "standard")).toBe("Postiz — Standard");
		expect(rowLabel("Postiz", "dedicated")).toBe("Postiz — Dedicated");
	});
});

describe("runSaveCeiling", () => {
	test("a rejected save resolves to the inline sentence and never runs onSaved", async () => {
		let saved = 0;
		const outcome = await runSaveCeiling({
			input: "25",
			requestSave: async () => {
				throw new Error("422");
			},
			onSaved: () => {
				saved += 1;
			},
		});
		expect(outcome).toEqual({ ok: false, message: COMPANION_SPEND_COPY.saveRejected });
		expect(saved).toBe(0);
	});

	test("invalid input sends nothing", async () => {
		let requested = 0;
		const outcome = await runSaveCeiling({
			input: "-1",
			requestSave: async () => {
				requested += 1;
				throw new Error("unreachable");
			},
			onSaved: () => undefined,
		});
		expect(outcome.ok).toBe(false);
		expect(requested).toBe(0);
	});
});

/** The phase-06 UI contract, resolved from this module rather than the cwd. */
const SPEC = fileURLToPath(
	new URL(
		"../../../../../../.planning/phases/06-first-companion-end-to-end/06-UI-SPEC.md",
		import.meta.url,
	),
);
const copy = (element: string) => uiSpecCopy(element, SPEC);
const quotes = (element: string) => uiSpecRowQuotes(element, SPEC);
/** Widen a literal-typed constant so it compares with a string read from the spec. */
const shipped = (value: string): string => value;

/**
 * The quoted strings of the S6 row in a `## UI Considerations` table, whose
 * first cell (`` `partial` `` and so on) repeats once per surface, so the
 * generic first-match readers would land on S1's row.
 */
function s6ConsiderationQuotes(category: string): string[] {
	const line = readFileSync(SPEC, "utf8")
		.split("\n")
		.find((row) => row.startsWith(`| \`${category}\` | S6 `));
	if (!line) throw new Error(`no S6 \`${category}\` row in ${SPEC}`);
	return [...line.matchAll(/"([^"]*)"/g)].map((match) => match[1] as string);
}

describe("S6 copy parity with 06-UI-SPEC.md", () => {
	test("section heading", () => {
		expect(shipped(COMPANION_SPEND_COPY.sectionTitle)).toBe(copy("S6 section heading"));
	});

	test("line-item row label", () => {
		expect(shipped(COMPANION_SPEND_COPY.rowLabel)).toBe(copy("S6 line-item row"));
	});

	test("cost dimension labels, in order", () => {
		expect([
			shipped(COMPANION_SPEND_COPY.compute),
			shipped(COMPANION_SPEND_COPY.storage),
			shipped(COMPANION_SPEND_COPY.aiUsage),
		]).toEqual(quotes("S6 cost dimension labels").slice(0, 3));
	});

	test("ceiling label", () => {
		expect(shipped(COMPANION_SPEND_COPY.ceilingLabel)).toBe(copy("S6 ceiling label"));
	});

	test("ceiling helper", () => {
		expect(shipped(COMPANION_SPEND_COPY.ceilingHelper)).toBe(copy("S6 ceiling helper"));
	});

	test("ceiling read-out", () => {
		expect(shipped(COMPANION_SPEND_COPY.readOut)).toBe(copy("S6 ceiling read-out"));
	});

	test("ceiling CTA", () => {
		expect(shipped(COMPANION_SPEND_COPY.saveCta)).toBe(copy("S6 ceiling CTA"));
	});

	test("ceiling-reached banner", () => {
		expect(shipped(COMPANION_SPEND_COPY.reachedBanner)).toBe(copy("S6 ceiling-reached banner"));
	});

	test("empty heading and body", () => {
		expect([
			shipped(COMPANION_SPEND_COPY.emptyTitle),
			shipped(COMPANION_SPEND_COPY.emptyBody),
		]).toEqual(quotes("S6, no companions installed"));
	});

	test("spend load error", () => {
		expect(shipped(COMPANION_SPEND_COPY.spendLoadFailed)).toBe(
			copy("Companion spend could not be loaded"),
		);
	});

	test("rejected save error", () => {
		expect(shipped(COMPANION_SPEND_COPY.saveRejected)).toBe(copy("Spend ceiling rejected"));
	});

	test("the saved toast is on the contract's toast list", () => {
		const toastList = readFileSync(SPEC, "utf8").split("### Toasts")[1]?.split("\n## ")[0] ?? "";
		const toasts = [...toastList.matchAll(/"([^"]*)"/g)].map((match) => match[1]);
		expect(toasts).toContain(shipped(COMPANION_SPEND_COPY.savedToast));
	});

	test("the not-yet-attributed tooltip is the partial row's words, sentence-cased", () => {
		// The row quotes the phrase mid-sentence in lower case; the tooltip is its whole text.
		expect(s6ConsiderationQuotes("partial")).toContain(
			COMPANION_SPEND_COPY.notAttributed.toLowerCase(),
		);
		expect(COMPANION_SPEND_COPY.notAttributed).toBe("Not yet attributed");
	});

	test("the internal term for nested LLM spend is in no shipped string", () => {
		for (const value of Object.values(COMPANION_SPEND_COPY)) {
			expect(value.toLowerCase()).not.toContain("nested");
			expect(value).not.toContain("LLM");
		}
	});
});
