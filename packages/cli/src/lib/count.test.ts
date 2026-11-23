import { describe, expect, it } from "bun:test";

import { formatCount } from "./count";

/**
 * UI-SPEC rule N8: a count is formatted ONCE, and is singular at exactly 1.
 *
 * The rule is shared rather than per-row precisely so that no printed row spells its own plural.
 * `1 files` is out of contract wherever it appears, and the only way to keep that true across
 * five `{count} files` rows in the S1 table is for all five to route through this one function.
 */
describe("formatCount", () => {
	it("is singular at exactly 1", () => {
		expect(formatCount(1, "file")).toBe("1 file");
	});

	it("is plural at 0", () => {
		// Zero takes the plural in English, which is the half of the rule a naive
		// `count === 1 ? singular : plural` gets right and a `count > 1` guard gets wrong.
		expect(formatCount(0, "file")).toBe("0 files");
	});

	it("is plural above 1", () => {
		expect(formatCount(2, "file")).toBe("2 files");
		expect(formatCount(365, "file")).toBe("365 files");
	});

	it("defaults the plural to the singular plus s", () => {
		expect(formatCount(3, "unit")).toBe("3 units");
	});

	it("takes an explicit plural when the default would be wrong", () => {
		expect(formatCount(1, "entry", "entries")).toBe("1 entry");
		expect(formatCount(4, "entry", "entries")).toBe("4 entries");
	});
});
