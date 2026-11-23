import { afterAll, beforeAll, describe, expect, it } from "bun:test";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/**
 * The environment boundary is EXCLUSIVE, not merely additive — proven against the product.
 *
 * `brand-env.test.ts` is a statement about a function. This file is a statement about the CLI:
 * it spawns the real entrypoint and reads its stdout, which is the only form of evidence that
 * can close BRAND-02's acceptance clause ("`IFORGE_*` is the only spelling a user may set",
 * D-05a). Phase 03.1 verification falsified that clause by running exactly the command in
 * test 1 below and watching the internal register name be honored.
 *
 * The contract-wide cases read `config/rebrand-env-contract.txt` and iterate every pair in it,
 * rather than naming a hand-picked subset: a check narrower than the requirement does not
 * establish the requirement.
 */

const CLI_ENTRYPOINT = join(import.meta.dir, "..", "..", "src", "index.ts");
const CONTRACT_PATH = join(
	import.meta.dir,
	"..",
	"..",
	"..",
	"..",
	"config",
	"rebrand-env-contract.txt",
);

/** Every `IFORGE_<NAME>=CLAWDI_<NAME>` pair in the frozen contract file, in file order. */
function parseContractPairs(): [string, string][] {
	const pairs: [string, string][] = [];
	for (const raw of readFileSync(CONTRACT_PATH, "utf-8").split("\n")) {
		const line = raw.trim();
		if (!line || line.startsWith("#")) continue;
		const separator = line.indexOf("=");
		if (separator === -1) throw new Error(`malformed contract line: ${line}`);
		pairs.push([line.slice(0, separator), line.slice(separator + 1)]);
	}
	return pairs;
}

const CONTRACT_PAIRS = parseContractPairs();

let fakeHome: string;

beforeAll(() => {
	fakeHome = mkdtempSync(join(tmpdir(), "iforge-exclusive-boundary-"));
});

afterAll(() => {
	rmSync(fakeHome, { recursive: true, force: true });
});

/**
 * Spawn the real CLI with an environment scrubbed of BOTH spellings of every contract member,
 * so a name leaking in from the developer's own shell cannot decide the result.
 *
 * The two update-suppression keys are set in their PUBLIC spelling deliberately: they are
 * contract members, so after this plan lands the internal spelling would be refused — and this
 * helper would then silently re-enable a network update check inside the test suite.
 */
async function runCli(
	args: string[],
	overrides: Record<string, string>,
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
	const env: Record<string, string> = {};
	for (const [key, value] of Object.entries(process.env)) {
		if (value !== undefined) env[key] = value;
	}
	for (const [publicName, internalName] of CONTRACT_PAIRS) {
		delete env[publicName];
		delete env[internalName];
	}
	env.HOME = fakeHome;
	env.IFORGE_NO_UPDATE_CHECK = "1";
	env.IFORGE_NO_AUTO_UPDATE = "1";
	Object.assign(env, overrides);

	const child = Bun.spawn([process.execPath, CLI_ENTRYPOINT, ...args], {
		env,
		stdout: "pipe",
		stderr: "pipe",
	});
	const [exitCode, stdout, stderr] = await Promise.all([
		child.exited,
		new Response(child.stdout).text(),
		new Response(child.stderr).text(),
	]);
	return { exitCode, stdout, stderr };
}

describe("the CLI refuses a user-supplied internal register name", () => {
	// This is verbatim the command phase 03.1 verification used to falsify BRAND-02.
	it("does not honor a bare CLAWDI_API_URL typed in the user's shell", async () => {
		const { exitCode, stdout } = await runCli(["config", "list"], {
			CLAWDI_API_URL: "http://internal-accepted.example",
		});
		expect(exitCode).toBe(0);
		expect(stdout).not.toContain("overrides apiUrl");
		expect(stdout).not.toContain("internal-accepted.example");
	});

	it("honors the public spelling and names it in the override note the user reads", async () => {
		const { exitCode, stdout } = await runCli(["config", "list"], {
			IFORGE_API_URL: "http://public.example",
		});
		expect(exitCode).toBe(0);
		expect(stdout).toContain("overrides apiUrl");
		expect(stdout).toContain("IFORGE_API_URL");
		expect(stdout).toContain("http://public.example");
		// The note must not quote the private register the user never typed.
		expect(stdout).not.toContain("CLAWDI_API_URL");
	});
});

describe("refuseUserSuppliedInternalNames", () => {
	it("refuses a bare internal name for EVERY pair in the contract file", async () => {
		const { refuseUserSuppliedInternalNames } = await import("../../src/lib/brand-env");

		expect(CONTRACT_PAIRS.length).toBeGreaterThan(40);
		let checked = 0;
		for (const [publicName, internalName] of CONTRACT_PAIRS) {
			const bare: NodeJS.ProcessEnv = { [internalName]: "smuggled" };
			expect(refuseUserSuppliedInternalNames(bare)).toEqual([internalName]);
			expect(internalName in bare).toBe(false);

			const paired: NodeJS.ProcessEnv = {
				[publicName]: "public",
				[internalName]: "smuggled",
			};
			expect(refuseUserSuppliedInternalNames(paired)).toEqual([]);
			expect(paired[internalName]).toBe("smuggled");
			checked += 1;
		}
		// A truncated parse would make the loop above vacuous, so pin the case count.
		expect(checked).toBe(CONTRACT_PAIRS.length);
	});

	it("treats an empty public value as unset, so it cannot rescue an internal name", async () => {
		const { refuseUserSuppliedInternalNames } = await import("../../src/lib/brand-env");
		const env: NodeJS.ProcessEnv = { IFORGE_API_URL: "", CLAWDI_API_URL: "smuggled" };
		expect(refuseUserSuppliedInternalNames(env)).toEqual(["CLAWDI_API_URL"]);
		expect("CLAWDI_API_URL" in env).toBe(false);
	});

	it("leaves a CLAWDI_ name that is not in the contract alone", async () => {
		const { refuseUserSuppliedInternalNames } = await import("../../src/lib/brand-env");
		// Driven by the contract, not by a prefix: `CLAWDI_DEFAULT_API_URL` is a build-time
		// define and `CLAWDI_HOME` is deferred state-root machinery (D-07/X-1). Neither has a
		// public spelling, so neither can be "supplied under the wrong name".
		const env: NodeJS.ProcessEnv = {
			CLAWDI_DEFAULT_API_URL: "http://built-in",
			CLAWDI_HOME: "/tmp/state-root",
		};
		expect(refuseUserSuppliedInternalNames(env)).toEqual([]);
		expect(env).toEqual({
			CLAWDI_DEFAULT_API_URL: "http://built-in",
			CLAWDI_HOME: "/tmp/state-root",
		});
	});
});

describe("the boundary as the entrypoint runs it (refuse, then apply)", () => {
	async function boundary(env: NodeJS.ProcessEnv): Promise<NodeJS.ProcessEnv> {
		const { applyBrandEnvAliases, refuseUserSuppliedInternalNames } = await import(
			"../../src/lib/brand-env"
		);
		refuseUserSuppliedInternalNames(env);
		applyBrandEnvAliases(env);
		return env;
	}

	it("is idempotent in effect: running it twice produces the same environment", async () => {
		const env: NodeJS.ProcessEnv = { IFORGE_API_URL: "http://x" };
		await boundary(env);
		const afterFirst = { ...env };
		await boundary(env);
		expect(env).toEqual(afterFirst);
	});

	it("lets the public spelling win when both are set, discarding the smuggled value", async () => {
		const env: NodeJS.ProcessEnv = {
			IFORGE_API_URL: "http://public",
			CLAWDI_API_URL: "http://smuggled",
		};
		await boundary(env);
		expect(env.CLAWDI_API_URL).toBe("http://public");
		expect(env.IFORGE_API_URL).toBe("http://public");
	});

	it("keeps the public spelling so a child process can re-derive it", async () => {
		const env: NodeJS.ProcessEnv = { IFORGE_API_URL: "http://x" };
		await boundary(env);
		expect(env.IFORGE_API_URL).toBe("http://x");
		expect(env.CLAWDI_API_URL).toBe("http://x");
	});
});

/**
 * Propagation. The refusal above is only safe if every environment the CLI hands to a child
 * carries the PUBLIC spelling of each contract member it carries — the child's own entrypoint
 * strips an internal name that arrives without its public counterpart.
 *
 * The mirror image is the credential scrub: because the public spelling now survives in
 * `process.env`, a scrub that deletes only the internal spelling lets the child re-derive the
 * very credential the scrub exists to remove (T-03.1G-01).
 */

const CONTRACT_INTERNAL_NAMES = new Set(CONTRACT_PAIRS.map(([, internal]) => internal));
const CONTRACT_PUBLIC_NAMES = new Set(CONTRACT_PAIRS.map(([publicName]) => publicName));

describe("the credential scrub covers both spellings", () => {
	async function childEnvFor(env: NodeJS.ProcessEnv): Promise<NodeJS.ProcessEnv> {
		const { buildRuntimeChildSpawn } = await import("../../src/commands/run");
		return buildRuntimeChildSpawn({
			runtime: "codex",
			service: null,
			command: "/bin/true",
			args: [],
			env,
			configPath: "/nonexistent/run-config.json",
		}).env;
	}

	it("removes BOTH spellings of the bearer token from the child environment", async () => {
		const childEnv = await childEnvFor({
			IFORGE_AUTH_TOKEN: "public-secret",
			CLAWDI_AUTH_TOKEN: "internal-secret",
			IFORGE_API_URL: "http://api.example",
		});
		expect("CLAWDI_AUTH_TOKEN" in childEnv).toBe(false);
		// Without this one, the child's own boundary copies the public spelling straight back
		// into the internal register — reintroducing the credential the scrub just removed.
		expect("IFORGE_AUTH_TOKEN" in childEnv).toBe(false);
	});

	it("removes the egress secret file, which has no public spelling to also remove", async () => {
		// Measured against the contract, not assumed: `CLAWDI_EGRESS_SECRET_FILE` is NOT a
		// member, so there is no public name that could alias back into it. Inventing one here
		// would widen the contract by accident.
		expect(CONTRACT_INTERNAL_NAMES.has("CLAWDI_EGRESS_SECRET_FILE")).toBe(false);
		const childEnv = await childEnvFor({
			CLAWDI_EGRESS_SECRET_FILE: "/run/egress/secret",
		});
		expect("CLAWDI_EGRESS_SECRET_FILE" in childEnv).toBe(false);
	});

	it("keeps a non-credential contract member in its public spelling so the child re-derives it", async () => {
		const childEnv = await childEnvFor({
			IFORGE_API_URL: "http://api.example",
			CLAWDI_API_URL: "http://api.example",
		});
		expect(childEnv.IFORGE_API_URL).toBe("http://api.example");
	});
});

describe("every name-selected child environment carries public spellings", () => {
	it("PERSISTED_ENV_KEYS names no internal contract register", async () => {
		const { PERSISTED_ENV_KEYS } = await import("../../src/serve/installer");
		expect(PERSISTED_ENV_KEYS.length).toBeGreaterThan(5);
		for (const key of PERSISTED_ENV_KEYS) {
			expect({ key, isInternalContractName: CONTRACT_INTERNAL_NAMES.has(key) }).toEqual({
				key,
				isInternalContractName: false,
			});
		}
		// Not vacuous: the list really does carry contract members, in their public spelling.
		expect(
			PERSISTED_ENV_KEYS.filter((key) => CONTRACT_PUBLIC_NAMES.has(key)).length,
		).toBeGreaterThan(5);
		// The ONE deliberate exception, pinned by name so a silent widening is visible.
		// `CLAWDI_HOME` is the deferred state root (D-07/X-1) and is not a contract member.
		expect(PERSISTED_ENV_KEYS).toContain("CLAWDI_HOME");
		// INVERTED deliberately by plan 03.1-16, the way this file inverted plan 06's. Plan 09
		// pinned `CLAWDI_STATE_DIR` here because the contract had no `IFORGE_STATE_DIR` pair;
		// plan 16 added the pair, so the internal spelling would now be REFUSED by the
		// supervised daemon's own boundary and the persisted key has to be the public one.
		expect(PERSISTED_ENV_KEYS).toContain("IFORGE_STATE_DIR");
		expect(PERSISTED_ENV_KEYS).not.toContain("CLAWDI_STATE_DIR");
	});

	it("the operation runner's nested-CLI allowlist names no internal contract register", async () => {
		const { NESTED_CLI_ENV_KEYS } = await import("../../src/serve/operation-runner");
		expect(NESTED_CLI_ENV_KEYS.length).toBeGreaterThan(10);
		for (const key of NESTED_CLI_ENV_KEYS) {
			expect({ key, isInternalContractName: CONTRACT_INTERNAL_NAMES.has(key) }).toEqual({
				key,
				isInternalContractName: false,
			});
		}
		expect(
			NESTED_CLI_ENV_KEYS.filter((key) => CONTRACT_PUBLIC_NAMES.has(key)).length,
		).toBeGreaterThan(3);
		expect(NESTED_CLI_ENV_KEYS).toContain("CLAWDI_HOME");
		// INVERTED deliberately by plan 03.1-16, for the same reason as `PERSISTED_ENV_KEYS`
		// above: the nested CLI runs the same boundary, which now refuses `CLAWDI_STATE_DIR`
		// arriving without `IFORGE_STATE_DIR`.
		expect(NESTED_CLI_ENV_KEYS).toContain("IFORGE_STATE_DIR");
		expect(NESTED_CLI_ENV_KEYS).not.toContain("CLAWDI_STATE_DIR");
	});

	it("the systemd unit's common environment names no internal contract register", async () => {
		const { runtimeSystemdCommonEnvironment } = await import(
			"../../src/runtime/runtime-systemd-reconciliation"
		);
		const { getRuntimePaths } = await import("../../src/runtime/paths");
		const environment = runtimeSystemdCommonEnvironment(getRuntimePaths({ mode: "hosted" }));
		for (const key of Object.keys(environment)) {
			expect({ key, isInternalContractName: CONTRACT_INTERNAL_NAMES.has(key) }).toEqual({
				key,
				isInternalContractName: false,
			});
		}
		// The units built on top of this run the iforge CLI itself, so hosted mode must survive
		// the child's own refusal.
		expect(environment.IFORGE_RUNTIME_MODE).toBe("hosted");
	});
});

/**
 * A source-level backstop for the child-environment constructions that are not reachable as
 * exported values — `cli-update.ts`'s two spawn sites and the individual systemd unit bodies.
 *
 * Stated rather than assumed (inherited anti-pattern 4): `packages/cli/src` sits OUTSIDE
 * `scripts/check_rebrand_residual.py`'s pathspec, so a green residual scan says nothing about
 * these files. This assertion is the only guard they have.
 */
describe("the child-environment construction sites carry no internal contract register", () => {
	const CHILD_ENV_SOURCES = [
		"serve/installer.ts",
		"serve/operation-runner.ts",
		"runtime/runtime-systemd-reconciliation.ts",
		"runtime/cli-update.ts",
	];

	/** Object-literal keys and quoted string entries, with comment lines removed. */
	function envNamesWritten(source: string): string[] {
		const names: string[] = [];
		for (const raw of source.split("\n")) {
			const line = raw.trim();
			if (line.startsWith("*") || line.startsWith("//") || line.startsWith("/*")) continue;
			for (const match of raw.matchAll(/^\s*(CLAWDI_[A-Z0-9_]+)\s*:/g)) {
				if (match[1] !== undefined) names.push(match[1]);
			}
			for (const match of raw.matchAll(/["'](CLAWDI_[A-Z0-9_]+)["']/g)) {
				if (match[1] !== undefined) names.push(match[1]);
			}
		}
		return names;
	}

	it("detects a written name, so a clean result is evidence rather than a formality", () => {
		expect(envNamesWritten('\tCLAWDI_API_URL: "x",')).toEqual(["CLAWDI_API_URL"]);
		expect(envNamesWritten('\t"CLAWDI_AUTH_TOKEN",')).toEqual(["CLAWDI_AUTH_TOKEN"]);
		expect(envNamesWritten(" *   - CLAWDI_AUTH_TOKEN / CLAWDI_API_URL: auth + endpoint")).toEqual(
			[],
		);
	});

	it.each(CHILD_ENV_SOURCES)("%s writes no internal contract register name", (relative) => {
		const source = readFileSync(join(import.meta.dir, "..", "..", "src", relative), "utf-8");
		const offenders = envNamesWritten(source).filter((name) => CONTRACT_INTERNAL_NAMES.has(name));
		expect([...new Set(offenders)].sort()).toEqual([]);
	});
});
