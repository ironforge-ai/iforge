import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * The defect this closes is not "healthcheck.ts forgot the boundary" -- it is "the entrypoint
 * inventory was incomplete". This container ships two independent processes from one image
 * (`CMD` and `HEALTHCHECK`), and only the first one's module graph reached `applyBrandEnvAliases`.
 * A per-file assertion would have been written against the entrypoint someone remembered.
 *
 * So the inventory is derived from the Dockerfile rather than hand-listed here: adding a third
 * `CMD`/`ENTRYPOINT`/`HEALTHCHECK` target to the image makes this test claim it, and the new
 * process must apply the boundary (or the assertion has to be argued down deliberately).
 */

const SRC = dirname(fileURLToPath(import.meta.url));
const PACKAGE_ROOT = join(SRC, "..");
const REPO_ROOT = join(PACKAGE_ROOT, "..", "..");
const DOCKERFILE = join(PACKAGE_ROOT, "Dockerfile");
const DEPLOY_CONFIG = join(REPO_ROOT, "config", "deploy.yml");
const CONTRACT = join(REPO_ROOT, "config", "rebrand-env-contract.txt");
const HEALTHCHECK = join(SRC, "healthcheck.ts");

/** The three names `healthEndpoint()` selects a transport from. */
const ENDPOINT_KNOBS = ["WA_SIDECAR_SOCKET_PATH", "WA_SIDECAR_HOST", "WA_SIDECAR_PORT"] as const;
/**
 * A *call* to the boundary, not its declaration. Matching the bare identifier would credit
 * `brand-env.ts` itself -- every entrypoint imports it transitively, so the guard would have
 * passed over an entrypoint that merely names the boundary without invoking it.
 */
const BOUNDARY_CALL = /(?<!function\s)\bapplyBrandEnvAliases\s*\(/;
/**
 * A *call* to the refusal, same shape and same reason as {@link BOUNDARY_CALL}.
 *
 * `BOUNDARY_CALL` matches the apply and says nothing about the refusal, so the per-entrypoint
 * case above it passes identically whether or not the refusal is wired — which is precisely the
 * hollow-guard shape this repository's verification calls out. A refusal that is declared,
 * exported and unit-tested but never invoked leaves BRAND-02 false at this boundary under a
 * fully green board. The ordering case below is the control that makes the wiring real.
 */
const REFUSAL_CALL = /(?<!function\s)\brefuseUserSuppliedInternalNames\s*\(/;

/**
 * Line and block comments removed before searching for a call.
 *
 * Both wired call sites carry a comment explaining why the refusal precedes the apply, and a
 * comment naming either function would otherwise be indistinguishable from an invocation — and
 * could invert the two offsets, failing correct wiring.
 */
function withoutComments(source: string): string {
	return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
}

/** Every `dist/<name>.js` this image launches as a process, mapped back to `src/<name>.ts`. */
function dockerfileEntrypointSources(): string[] {
	const dockerfile = readFileSync(DOCKERFILE, "utf-8");
	const sources = new Set<string>();
	for (const match of dockerfile.matchAll(/dist\/([A-Za-z0-9._-]+)\.js/g)) {
		const name = match[1];
		if (name === undefined) continue;
		sources.add(join(SRC, `${name}.ts`));
	}
	return [...sources].sort();
}

/**
 * Does this module, or anything in its relative static-import closure, apply the boundary?
 *
 * Static imports are the only edge that matters: they are hoisted and evaluated before the
 * importing module's body, so a boundary reached only through a *dynamic* import would run too
 * late to be worth crediting here.
 */
function closureAppliesBoundary(entry: string): boolean {
	const seen = new Set<string>();
	const queue = [entry];
	while (queue.length > 0) {
		const current = queue.pop();
		if (current === undefined || seen.has(current)) continue;
		seen.add(current);

		let source: string;
		try {
			source = readFileSync(current, "utf-8");
		} catch {
			continue; // a bare-specifier dependency, not package source
		}
		if (BOUNDARY_CALL.test(source)) return true;

		for (const match of source.matchAll(/^\s*import\s[^;]*?from\s+"(\.\.?\/[^"]+)"/gm)) {
			const specifier = match[1];
			if (specifier === undefined) continue;
			queue.push(join(dirname(current), specifier.replace(/\.js$/, ".ts")));
		}
	}
	return false;
}

/**
 * Does this module, or anything in its relative static-import closure, call the refusal STRICTLY
 * BEFORE the apply, within one module?
 *
 * Both calls must live in the same module for the ordering to mean anything: a refusal in one
 * file and an apply in another says nothing about which runs first. Same traversal rules as
 * {@link closureAppliesBoundary} — static imports only.
 */
function closureRefusesBeforeApplying(entry: string): boolean {
	const seen = new Set<string>();
	const queue = [entry];
	while (queue.length > 0) {
		const current = queue.pop();
		if (current === undefined || seen.has(current)) continue;
		seen.add(current);

		let source: string;
		try {
			source = readFileSync(current, "utf-8");
		} catch {
			continue; // a bare-specifier dependency, not package source
		}
		const code = withoutComments(source);
		const refuseAt = code.search(REFUSAL_CALL);
		const applyAt = code.search(BOUNDARY_CALL);
		if (refuseAt !== -1 && applyAt !== -1 && refuseAt < applyAt) return true;

		for (const match of source.matchAll(/^\s*import\s[^;]*?from\s+"(\.\.?\/[^"]+)"/gm)) {
			const specifier = match[1];
			if (specifier === undefined) continue;
			queue.push(join(dirname(current), specifier.replace(/\.js$/, ".ts")));
		}
	}
	return false;
}

describe("sidecar process entrypoints apply the environment boundary", () => {
	const entrypoints = dockerfileEntrypointSources();

	// Fail closed. If the Dockerfile is reformatted so the regex above matches nothing, the
	// per-entrypoint assertions would all pass vacuously over an empty list.
	it("derives a non-empty inventory containing both known image processes", () => {
		expect(entrypoints).toEqual([join(SRC, "healthcheck.ts"), join(SRC, "index.ts")]);
	});

	it.each(entrypoints)("%s reaches applyBrandEnvAliases before reading configuration", (entry) => {
		expect(closureAppliesBoundary(entry)).toBe(true);
	});

	// The traversal must be capable of returning false, or the assertions above prove nothing.
	it("reports a module whose static-import closure never applies the boundary", () => {
		expect(closureAppliesBoundary(join(SRC, "json-bytes.ts"))).toBe(false);
	});

	// The sharper vacuity check: `brand-env.ts` declares the boundary and calls it nowhere. If this
	// were credited, every entrypoint would pass for free, because they all import it.
	it("does not credit the module that merely declares the boundary", () => {
		expect(closureAppliesBoundary(join(SRC, "brand-env.ts"))).toBe(false);
	});

	// The case the assertion above cannot make. `BOUNDARY_CALL` matches `applyBrandEnvAliases`
	// only, so every case above this one passes with the refusal entirely absent. Asserting the
	// ORDER of two calls in one module is what makes the wiring at `config.ts` and
	// `healthcheck.ts` a property this suite defends rather than a line someone once wrote.
	it.each(entrypoints)("%s refuses user-supplied internal names before it applies", (entry) => {
		expect(closureRefusesBeforeApplying(entry)).toBe(true);
	});

	// Mirrors of the two vacuity cases above, for the ordering traversal: it must be able to
	// return false, or the two assertions immediately above prove nothing.
	it("reports a module whose closure never refuses before applying", () => {
		expect(closureRefusesBeforeApplying(join(SRC, "json-bytes.ts"))).toBe(false);
	});

	it("does not credit the module that declares BOTH functions and calls neither", () => {
		expect(closureRefusesBeforeApplying(join(SRC, "brand-env.ts"))).toBe(false);
	});
});

/** `IFORGE_X -> CLAWDI_X` exactly as the frozen contract file declares it. */
function contractAliases(): Record<string, string> {
	const pairs: Record<string, string> = {};
	for (const raw of readFileSync(CONTRACT, "utf-8").split("\n")) {
		const line = raw.trim();
		if (!line || line.startsWith("#")) continue;
		const separator = line.indexOf("=");
		if (separator === -1) continue;
		pairs[line.slice(0, separator)] = line.slice(separator + 1);
	}
	return pairs;
}

/** The env keys `config/deploy.yml` puts in the sidecar container, from its `clear:` block. */
function deployedSidecarEnvKeys(): string[] {
	const deploy = readFileSync(DEPLOY_CONFIG, "utf-8");
	const accessory = deploy.slice(deploy.indexOf("\n  whatsapp-baileys:\n"));
	const clearBlock = accessory.slice(
		accessory.indexOf("      clear:"),
		accessory.indexOf("      secret:"),
	);
	return [...clearBlock.matchAll(/^\s{8}([A-Z0-9_]+):/gm)].flatMap((match) =>
		match[1] === undefined ? [] : [match[1]],
	);
}

/**
 * WR-09: the Kamal readiness gate runs `dist/healthcheck.js` inside the container whose
 * environment `config/deploy.yml` populates. If the deploy contract sets a spelling the
 * healthcheck cannot resolve, the gate fails the deploy *after* the accessory has been rebooted.
 * Pin the intersection rather than either side's literals.
 */
describe("the deployed sidecar environment and the healthcheck read set intersect", () => {
	const aliases = contractAliases();
	const healthcheck = readFileSync(HEALTHCHECK, "utf-8");
	const deployed = deployedSidecarEnvKeys();

	// Fail closed: an empty parse would make every assertion below vacuous.
	it("parses a non-empty deployed environment that configures the transport", () => {
		expect(deployed.length).toBeGreaterThan(0);
		expect(deployed.some((key) => ENDPOINT_KNOBS.some((knob) => key.endsWith(knob)))).toBe(true);
	});

	it("resolves every transport knob deploy.yml sets to a name the healthcheck reads", () => {
		const transportKeys = deployed.filter((key) =>
			ENDPOINT_KNOBS.some((knob) => key.endsWith(knob)),
		);
		for (const key of transportKeys) {
			// Either the healthcheck reads this exact spelling, or the boundary maps it to one it does.
			const internal = aliases[key] ?? key;
			expect(healthcheck.includes(`env.${internal}`)).toBe(true);
			if (internal !== key) expect(BOUNDARY_CALL.test(healthcheck)).toBe(true);
		}
	});
});
