import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
	applyBrandEnvAliases,
	BRAND_ENV_ALIASES,
	refuseUserSuppliedInternalNames,
	resolveBrandEnv,
} from "./brand-env.js";

const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..");
const CONTRACT_PATH = join(REPO_ROOT, "config", "rebrand-env-contract.txt");
const CLI_BOUNDARY_PATH = join(REPO_ROOT, "packages", "cli", "src", "lib", "brand-env.ts");

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

function parseCliAliasMap(): Record<string, string> {
	const source = readFileSync(CLI_BOUNDARY_PATH, "utf-8");
	const body = source.split("export const BRAND_ENV_ALIASES")[1]?.split("};")[0];
	if (!body) throw new Error("could not locate BRAND_ENV_ALIASES in the CLI boundary");
	const pairs: Record<string, string> = {};
	for (const [, publicName, internalName] of body.matchAll(
		/^\t(IFORGE_[A-Z0-9_]+): "(CLAWDI_[A-Z0-9_]+)",$/gm,
	)) {
		pairs[publicName] = internalName;
	}
	return pairs;
}

describe("sidecar brand-env boundary", () => {
	it("is identical to the CLI's map, which is why the local copy is safe", () => {
		expect(BRAND_ENV_ALIASES).toEqual(parseCliAliasMap());
	});

	it("equals config/rebrand-env-contract.txt, the single authority both copies answer to", () => {
		expect(BRAND_ENV_ALIASES).toEqual(parseContractFile());
	});

	it("maps a public name onto the internal register and refuses a bare internal name", () => {
		expect(resolveBrandEnv({ IFORGE_WA_SIDECAR_LOG_LEVEL: "debug" })).toEqual({
			CLAWDI_WA_SIDECAR_LOG_LEVEL: "debug",
		});
		expect(resolveBrandEnv({ CLAWDI_WA_SIDECAR_LOG_LEVEL: "debug" })).toEqual({});
	});

	// RESTATED, not deleted (plan 03.1-16): applying on its own still never removes an internal
	// name it did not set, but that is no longer the whole story — deletion now belongs to
	// `refuseUserSuppliedInternalNames`, which runs first at both entrypoints.
	it("on its own never deletes an internal name it did not set — the refusal owns deletion", () => {
		const env: NodeJS.ProcessEnv = { CLAWDI_WA_SIDECAR_STATE_ROOT: "/data" };
		expect(applyBrandEnvAliases(env)).toBe(0);
		expect(env.CLAWDI_WA_SIDECAR_STATE_ROOT).toBe("/data");
	});

	// INVERTED deliberately (plan 03.1-16). Applying used to consume the public key. Keeping it
	// is what lets the refusal run unconditionally at every entrypoint with no provenance marker.
	it("KEEPS the public key it consumed so a child process can re-derive the register", () => {
		const env: NodeJS.ProcessEnv = { IFORGE_WA_SIDECAR_STATE_ROOT: "/data" };
		expect(applyBrandEnvAliases(env)).toBe(1);
		expect(env.CLAWDI_WA_SIDECAR_STATE_ROOT).toBe("/data");
		expect(env.IFORGE_WA_SIDECAR_STATE_ROOT).toBe("/data");
	});
});

describe("refuseUserSuppliedInternalNames", () => {
	it("refuses a bare internal name and reports it", () => {
		const env: NodeJS.ProcessEnv = { CLAWDI_WA_SIDECAR_STATE_ROOT: "/from-operator" };
		expect(refuseUserSuppliedInternalNames(env)).toContain("CLAWDI_WA_SIDECAR_STATE_ROOT");
		expect("CLAWDI_WA_SIDECAR_STATE_ROOT" in env).toBe(false);
	});

	it("leaves a paired internal name alone — that value is this boundary's own work", () => {
		const env: NodeJS.ProcessEnv = {
			IFORGE_WA_SIDECAR_STATE_ROOT: "/data",
			CLAWDI_WA_SIDECAR_STATE_ROOT: "/data",
		};
		expect(refuseUserSuppliedInternalNames(env)).toEqual([]);
		expect(env.CLAWDI_WA_SIDECAR_STATE_ROOT).toBe("/data");
	});

	it("follows the contract and not the CLAWDI_ prefix, so a non-member is untouched", () => {
		// `CLAWDI_WA_EGRESS` is an iptables chain name and `CLAWDI_HOME` is the deferred state
		// root (D-07/X-1). Neither has a public spelling, so neither could have been "supplied
		// under the wrong one".
		const env: NodeJS.ProcessEnv = { CLAWDI_WA_EGRESS: "chain", CLAWDI_HOME: "/var/lib/clawdi" };
		expect(refuseUserSuppliedInternalNames(env)).toEqual([]);
		expect(env).toEqual({ CLAWDI_WA_EGRESS: "chain", CLAWDI_HOME: "/var/lib/clawdi" });
	});

	it("does not let an empty public value rescue a user-supplied internal name", () => {
		const env: NodeJS.ProcessEnv = {
			IFORGE_WA_SIDECAR_STATE_ROOT: "",
			CLAWDI_WA_SIDECAR_STATE_ROOT: "/from-operator",
		};
		expect(refuseUserSuppliedInternalNames(env)).toContain("CLAWDI_WA_SIDECAR_STATE_ROOT");
		expect("CLAWDI_WA_SIDECAR_STATE_ROOT" in env).toBe(false);
	});
});
