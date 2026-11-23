import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { BRAND } from "./brand";

const REPO_ROOT = join(import.meta.dir, "..", "..", "..", "..");
const AUTHORITY_PATH = join(REPO_ROOT, "config", "brand-strings.txt");
const MODULE_PATH = join(import.meta.dir, "brand.ts");

/**
 * Parse the authority file the same way `brand-env.test.ts` parses the environment contract:
 * skip blanks and comments, split on the FIRST separator, throw on a malformed line.
 *
 * It takes text rather than reading the file itself so the malformed-line behaviour can be
 * proven with an injected string instead of by writing a corrupted authority to disk.
 */
function parseAuthority(text: string): Record<string, string> {
	const pairs: Record<string, string> = {};
	for (const raw of text.split("\n")) {
		const line = raw.trim();
		if (!line || line.startsWith("#")) continue;
		const separator = line.indexOf("=");
		if (separator === -1) throw new Error(`malformed brand-strings line: ${line}`);
		pairs[line.slice(0, separator)] = line.slice(separator + 1);
	}
	return pairs;
}

/** The one legal spelling of the product name in prose, UI and printed output (N1 / D-04). */
const PRODUCT_NAME = "iforge";
/** The register exception (N5 / D-05a): SCREAMING_SNAKE is legal only inside a register name. */
const REGISTER_NAME = /^[A-Z0-9_]+$/;
/** Any casing of the product name, so a member can be judged on the spelling it actually used. */
const ANY_CASING = /[Ii][Ff][Oo][Rr][Gg][Ee]/g;

describe("BRAND", () => {
	it("is a frozen record of non-empty strings", () => {
		expect(Object.isFrozen(BRAND)).toBe(true);
		const entries = Object.entries(BRAND);
		expect(entries.length).toBeGreaterThan(0);
		for (const [key, value] of entries) {
			expect(typeof value).toBe("string");
			expect(value, `${key} is empty`).not.toBe("");
		}
	});

	it("names the product, the invocation prefix and the public register prefix as members", () => {
		expect(BRAND.productName).toBe(PRODUCT_NAME);
		expect(BRAND.invocation).toBe(PRODUCT_NAME);
		expect(BRAND.environmentPrefix).toBe("IFORGE_");
	});

	it("carries every capitalised compound the naming rules enumerate, qualifier case intact", () => {
		// N2: the compound keeps the QUALIFIER's case and lowercases only the product name.
		// Measured over packages/cli/src, apps/web/src and packages/shared/src: these eleven
		// qualifiers are the whole set the tree uses today.
		const qualifiers = [
			"AI",
			"API",
			"CLI",
			"Channels",
			"Cloud",
			"MCP",
			"OAuth",
			"Project",
			"Vault",
			"Wallet",
			"WhatsApp",
		];
		for (const qualifier of qualifiers) {
			expect(BRAND[`compound${qualifier}`]).toBe(`${PRODUCT_NAME} ${qualifier}`);
		}
	});

	it("spells the product name lowercase, and equal to itself lowercased", () => {
		expect(BRAND.productName).toBe(BRAND.productName.toLowerCase());
		expect(BRAND.invocation).toBe(BRAND.invocation.toLowerCase());
	});

	it("spells the product name in no capitalisation the naming rules disallow", () => {
		// N1 fixes prose, UI and printed output as lowercase. The single exception is the
		// environment register, whose names are SCREAMING_SNAKE by the convention every shell
		// shares -- so `IFORGE_` is legal in a member whose whole value IS a register name, and
		// nowhere else. This is the same allow rule `check_rebrand_residual.py` applies.
		for (const [key, value] of Object.entries(BRAND)) {
			for (const spelling of value.match(ANY_CASING) ?? []) {
				if (spelling === PRODUCT_NAME) continue;
				if (spelling === "IFORGE" && REGISTER_NAME.test(value)) continue;
				throw new Error(`${key} spells the product name as ${spelling}: ${value}`);
			}
		}
	});

	it("equals config/brand-strings.txt exactly, in both directions", () => {
		const authority = parseAuthority(readFileSync(AUTHORITY_PATH, "utf-8"));
		// `toEqual` is bidirectional by construction: an extra key on either side fails it.
		// Asserted per-key first so a failure names the member rather than dumping both records.
		for (const [key, value] of Object.entries(BRAND)) {
			expect(authority[key], `${key} is missing from the authority file`).toBe(value);
		}
		for (const [key, value] of Object.entries(authority)) {
			expect(BRAND[key], `${key} is in the authority file but not in the record`).toBe(value);
		}
		expect(BRAND).toEqual(authority);
	});

	it("throws on a malformed authority line rather than silently skipping it", () => {
		expect(() => parseAuthority("productName=iforge\nthis line has no separator\n")).toThrow(
			"malformed brand-strings line",
		);
	});

	it("reads no file at import time, because it is bundled into a native binary", () => {
		const source = readFileSync(MODULE_PATH, "utf-8");
		expect(source).not.toContain("readFileSync");
		expect(source).not.toContain("node:fs");
		expect(source).not.toContain("import.meta.dir");
	});

	it("holds brand strings only -- no URL and no email address", () => {
		// The destinations authority in `packages/shared` owns those. The rewrite script
		// consumes BRAND as its substitution source, so a destination in here would become a
		// destination the sweep rewrites.
		const source = readFileSync(MODULE_PATH, "utf-8");
		expect(source).not.toMatch(/https?:\/\//);
		expect(source).not.toMatch(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
		for (const value of Object.values(BRAND)) {
			expect(value).not.toMatch(/https?:\/\//);
			expect(value).not.toMatch(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/);
		}
	});

	it("states in its module doc why it is verbatim and why destinations are separate", () => {
		const source = readFileSync(MODULE_PATH, "utf-8");
		expect(source).toContain("native binary");
		expect(source).toContain("packages/shared");
	});
});
