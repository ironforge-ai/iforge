import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
	applyBrandEnvAliases,
	BRAND_ENV_ALIASES,
	publicNameFor,
	resolveBrandEnv,
} from "./brand-env";

const REPO_ROOT = join(import.meta.dir, "..", "..", "..", "..");
const CONTRACT_PATH = join(REPO_ROOT, "config", "rebrand-env-contract.txt");

function parseContractFile(): Record<string, string> {
	const pairs: Record<string, string> = {};
	for (const raw of readFileSync(CONTRACT_PATH, "utf-8").split("\n")) {
		const line = raw.trim();
		if (!line || line.startsWith("#")) continue;
		const separator = line.indexOf("=");
		if (separator === -1) throw new Error(`malformed contract line: ${line}`);
		pairs[line.slice(0, separator)] = line.slice(separator + 1);
	}
	return pairs;
}

describe("resolveBrandEnv", () => {
	it("maps a public IFORGE_ name onto its internal register name", () => {
		expect(resolveBrandEnv({ IFORGE_API_URL: "https://x" })).toEqual({
			CLAWDI_API_URL: "https://x",
		});
	});

	it("refuses a bare internal name as user configuration", () => {
		expect(resolveBrandEnv({ CLAWDI_API_URL: "https://x" })).toEqual({});
	});

	it("treats an empty string as unset so it cannot overwrite a resolved default", () => {
		expect(resolveBrandEnv({ IFORGE_API_URL: "" })).toEqual({});
	});

	it("does not mutate the environment it was handed", () => {
		const env = { IFORGE_API_URL: "https://x" };
		resolveBrandEnv(env);
		expect(env).toEqual({ IFORGE_API_URL: "https://x" });
	});
});

describe("applyBrandEnvAliases", () => {
	// INVERTED deliberately (plan 03.1-09, BRAND-02). This used to assert that the public key
	// was removed once consumed. Keeping it is now load-bearing: the public spelling is what a
	// child process inherits and re-derives its own register from, which is what lets
	// `refuseUserSuppliedInternalNames` run unconditionally at every entrypoint.
	it("writes the internal name, KEEPS the public key so a child can re-derive it, and returns the count", () => {
		const env: NodeJS.ProcessEnv = { IFORGE_API_URL: "https://x" };
		expect(applyBrandEnvAliases(env)).toBe(1);
		expect(env.CLAWDI_API_URL).toBe("https://x");
		expect(env.IFORGE_API_URL).toBe("https://x");
	});

	// ALSO INVERTED, in ownership rather than in outcome. Applying still never deletes an
	// internal name it did not set — but it is no longer the whole story, because the deletion
	// now belongs to `refuseUserSuppliedInternalNames`, which runs first at the entrypoint. The
	// old reason (the CLI writes mapped names into its own child processes) is preserved by the
	// new design: those children now receive the PUBLIC spelling. See
	// `tests/lib/brand-env-exclusive-boundary.test.ts` for the composed behaviour.
	it("on its own never deletes an internal name it did not set — the refusal function owns deletion", () => {
		const env: NodeJS.ProcessEnv = { CLAWDI_API_URL: "https://parent" };
		expect(applyBrandEnvAliases(env)).toBe(0);
		expect(env.CLAWDI_API_URL).toBe("https://parent");
	});

	it("is idempotent in effect: a second call re-applies the same value and changes nothing", () => {
		const env: NodeJS.ProcessEnv = { IFORGE_API_URL: "https://x" };
		applyBrandEnvAliases(env);
		const after = { ...env };
		expect(applyBrandEnvAliases(env)).toBe(1);
		expect(env).toEqual(after);
	});
});

describe("publicNameFor", () => {
	it("maps an internal register name back to the spelling the user types", () => {
		expect(publicNameFor("CLAWDI_API_URL")).toBe("IFORGE_API_URL");
	});

	it("returns undefined for a CLAWDI_ name that is not a contract member", () => {
		// `CLAWDI_HOME` is deferred state-root machinery (D-07/X-1) and has no public spelling.
		expect(publicNameFor("CLAWDI_HOME")).toBeUndefined();
	});

	it("is the exact inverse of the alias map, for every pair", () => {
		for (const [publicName, internalName] of Object.entries(BRAND_ENV_ALIASES)) {
			expect(publicNameFor(internalName)).toBe(publicName);
		}
	});
});

describe("BRAND_ENV_ALIASES", () => {
	it("is a non-empty IFORGE_ to CLAWDI_ map with no self-referential entry", () => {
		const entries = Object.entries(BRAND_ENV_ALIASES);
		expect(entries.length).toBeGreaterThan(0);
		for (const [publicName, internalName] of entries) {
			expect(publicName.startsWith("IFORGE_")).toBe(true);
			expect(internalName.startsWith("CLAWDI_")).toBe(true);
			expect(publicName).not.toBe(internalName);
		}
	});

	it("equals config/rebrand-env-contract.txt exactly, so the code cannot drift from the contract", () => {
		expect(BRAND_ENV_ALIASES).toEqual(parseContractFile());
	});
});
