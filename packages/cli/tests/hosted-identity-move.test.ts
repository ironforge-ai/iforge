/**
 * The hosted identity — iforge names only.
 *
 * On 2026-09-11 the hosted runtime OS user, the Files identity, the six platform roots and the
 * three system-scope unit names moved from `clawdi` to `iforge`. On 2026-09-30 the user directed
 * that the fork keep NO Clawdi-name aliases beside them: there is no production deployment of
 * iforge, so there is no pre-rename host to recognise, migrate or clean up. This file guards both.
 *
 * Two upstream security fixes merged at sync step 1 changed what the identity contains. #939
 * (64916c0a) retired the Files service account: Files runs as the tenant runtime user, so the
 * current Files identity IS the hosted runtime account. #924 (4d37c946) moved hosted CLI state out
 * of the tenant home to `/var/lib/iforge-user`.
 *
 * Three contracts, in the order they can fail:
 *
 *   1. The CURRENT spelling is what the code defines.
 *   2. No Clawdi-name alias remains: no module exports a `LEGACY_*` reader for a pre-rename
 *      spelling, and no non-test CLI source line names a retired spelling outside a comment,
 *      except the enumerated upstream WIRE values below (never an alias of an iforge name).
 *   3. `paths.ts`'s inline `/home/iforge` literal equals `HOSTED_RUNTIME_HOME`.
 *
 * Contract 2 reads source text: the claim is about what the tree may CONTAIN. It is written as an
 * ENUMERATION, never a count, and every allowance is asserted to be live.
 */
import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import * as fileBrowserIsolation from "../src/runtime/file-browser-isolation";
import * as hostedRuntimeContract from "../src/runtime/hosted-runtime-contract";
import {
	HOSTED_RUNTIME_GID,
	HOSTED_RUNTIME_HOME,
	HOSTED_RUNTIME_UID,
	HOSTED_RUNTIME_USER,
} from "../src/runtime/hosted-runtime-contract";
import * as runtimePaths from "../src/runtime/paths";
import {
	DEFAULT_CACHE_ROOT,
	DEFAULT_CONFIGURATION_ROOT,
	DEFAULT_FILE_BROWSER_RUNTIME_ROOT,
	DEFAULT_FILE_BROWSER_STATE_ROOT,
	DEFAULT_RUN_ROOT,
	DEFAULT_RUNTIME_USER_CLI_STATE_ROOT,
	DEFAULT_SERVICE_STATE_ROOT,
	SYSTEMD_FILE_BROWSER_STATE_DIRECTORY,
	SYSTEMD_PLATFORM_DIRECTORY,
} from "../src/runtime/paths";
import * as systemdUser from "../src/runtime/systemd-user";
import {
	GENERATED_RUNTIME_SYSTEMD_FILE_HEADER,
	isGeneratedRuntimeSystemdFile,
	RUNTIME_SYSTEM_UNIT_DAEMON,
	RUNTIME_SYSTEM_UNIT_NAMES,
	RUNTIME_SYSTEM_UNIT_SIDECAR,
	RUNTIME_SYSTEM_UNIT_WATCH,
	RUNTIME_SYSTEMD_DROP_IN_FILE,
	RUNTIME_SYSTEMD_DROP_IN_FILE_NAMES,
	SERVE_DAEMON_SYSTEMD_UNIT_BASENAMES,
} from "../src/runtime/systemd-user";
import * as serveInstaller from "../src/serve/installer";

const cliRoot = resolve(import.meta.dir, "..");
const runtimeSrc = join(cliRoot, "src", "runtime");

function readSource(path: string): string {
	return readFileSync(path, "utf8");
}

describe("hosted identity move: the current spelling is what the code defines", () => {
	test("the hosted runtime account is iforge, and its numeric identity did not move", () => {
		expect(HOSTED_RUNTIME_HOME).toBe("/home/iforge");
		expect(HOSTED_RUNTIME_USER).toBe("iforge");
		// The uid/gid deliberately have no legacy twin. Keeping 10001 across the move is what
		// makes the runbook a RENAME of the account rather than a re-chown of every file the
		// runtime owns; if these ever move, the runbook's `usermod`/`groupmod` steps are wrong.
		expect(HOSTED_RUNTIME_UID).toBe(10_001);
		expect(HOSTED_RUNTIME_GID).toBe(10_001);
	});

	test("Files runs as the tenant runtime account, which is iforge (#939)", () => {
		// Upstream #939 retired the Files service account: the file browser runs as the tenant
		// runtime user that owns the home it serves. The identity resolver and the unit writer must
		// both fall back to HOSTED_RUNTIME_USER (asserted `iforge` above), never to a literal —
		// a literal is how the pre-rename `clawdi` spelling would come back unnoticed.
		const fallback = "process.env.CLAWDI_RUNTIME_USER?.trim() || HOSTED_RUNTIME_USER";
		const isolation = readSource(join(runtimeSrc, "file-browser-isolation.ts"));
		const entry = isolation.indexOf("export function ensureFileBrowserServiceIsolation(");
		// Found before sliced, so a rename fails here rather than asserting over an empty body.
		expect(entry).toBeGreaterThan(0);
		const entryBody = isolation.slice(entry);
		expect(entryBody).toContain(fallback);
		expect(entryBody).not.toContain("companionServiceNames(");
		const reconciliation = readSource(join(runtimeSrc, "runtime-systemd-reconciliation.ts"));
		const writer = reconciliation.indexOf("function writeFileBrowserSystemdUnit(");
		expect(writer).toBeGreaterThan(0);
		const writerBody = reconciliation.slice(writer, reconciliation.indexOf("\n}\n", writer));
		expect(writerBody).toContain(fallback);
		// biome-ignore lint/suspicious/noTemplateCurlyInString: matches source text, not a template.
		expect(writerBody).toContain("`User=${runtimeUser}`");
		expect(writerBody).not.toContain("User=iforge-files");
	});

	test("all six platform roots and both systemd directory names carry the new spelling", () => {
		expect({
			configuration: DEFAULT_CONFIGURATION_ROOT,
			serviceState: DEFAULT_SERVICE_STATE_ROOT,
			cache: DEFAULT_CACHE_ROOT,
			run: DEFAULT_RUN_ROOT,
			fileBrowserState: DEFAULT_FILE_BROWSER_STATE_ROOT,
			fileBrowserRuntime: DEFAULT_FILE_BROWSER_RUNTIME_ROOT,
		}).toEqual({
			configuration: "/etc/iforge",
			serviceState: "/var/lib/iforge",
			cache: "/var/cache/iforge",
			run: "/run/iforge",
			fileBrowserState: "/var/lib/iforge-files",
			fileBrowserRuntime: "/run/iforge-files",
		});
		expect(SYSTEMD_PLATFORM_DIRECTORY).toBe("iforge");
		expect(SYSTEMD_FILE_BROWSER_STATE_DIRECTORY).toBe("iforge-files");
	});

	test("the three system-scope unit names moved, and their filenames are derived, not restated", () => {
		expect(RUNTIME_SYSTEM_UNIT_WATCH).toBe("iforge-runtime-watch");
		expect(RUNTIME_SYSTEM_UNIT_DAEMON).toBe("iforge-daemon");
		expect(RUNTIME_SYSTEM_UNIT_SIDECAR).toBe("iforge-runtime-sidecar");
		// Derived, so a future rename cannot move the unit this product WRITES while leaving the
		// set that decides what it is willing to COLLECT pointing at the old name.
		expect([...RUNTIME_SYSTEM_UNIT_NAMES]).toEqual([
			`${RUNTIME_SYSTEM_UNIT_WATCH}.service`,
			`${RUNTIME_SYSTEM_UNIT_DAEMON}.service`,
			`${RUNTIME_SYSTEM_UNIT_SIDECAR}.service`,
		]);
	});

	test("the runtime command's unit triple is the same triple, not a fourth copy", () => {
		// The systemd transaction decides which units to restart and stop (upstream #1141 moved it
		// out of `commands/runtime.ts` into `runtime/systemd-transaction.ts`). It named these
		// literally until the move; a stale spelling here asks systemd to restart a unit the
		// reconciler no longer writes, and is silent on a host and invisible in a diff.
		// Since upstream #1165/#1194 the transaction names only the watch and sidecar units (the
		// daemon is activated through the generic unit set), so those two must be derived and no
		// unit of the triple may appear as a literal.
		const source = readSource(join(runtimeSrc, "systemd-transaction.ts"));
		for (const base of [RUNTIME_SYSTEM_UNIT_WATCH, RUNTIME_SYSTEM_UNIT_SIDECAR]) {
			expect(source).toContain(`\`\${${sourceConstantName(base)}}.service\``);
		}
		for (const base of [
			RUNTIME_SYSTEM_UNIT_WATCH,
			RUNTIME_SYSTEM_UNIT_DAEMON,
			RUNTIME_SYSTEM_UNIT_SIDECAR,
		]) {
			expect(source).not.toContain(`"${base}.service"`);
		}
	});
});

/** The exported identifier a base unit name is declared under, for the derivation assertion. */
function sourceConstantName(base: string): string {
	switch (base) {
		case RUNTIME_SYSTEM_UNIT_WATCH:
			return "RUNTIME_SYSTEM_UNIT_WATCH";
		case RUNTIME_SYSTEM_UNIT_DAEMON:
			return "RUNTIME_SYSTEM_UNIT_DAEMON";
		case RUNTIME_SYSTEM_UNIT_SIDECAR:
			return "RUNTIME_SYSTEM_UNIT_SIDECAR";
		default:
			throw new Error(`no constant name recorded for unit base ${base}`);
	}
}

describe("iforge names only: no Clawdi-name alias remains (user directive 2026-09-30)", () => {
	test("no identity module exports a legacy reader", () => {
		const modules = {
			"runtime/paths": runtimePaths,
			"runtime/hosted-runtime-contract": hostedRuntimeContract,
			"runtime/file-browser-isolation": fileBrowserIsolation,
			"runtime/systemd-user": systemdUser,
			"serve/installer": serveInstaller,
		};
		const legacy = Object.entries(modules).flatMap(([name, module]) =>
			Object.keys(module)
				.filter((key) => /^legacy/i.test(key) || key.includes("Legacy"))
				.map((key) => `${name}: ${key}`),
		);
		// No exception. Upstream #1520's `legacyRuntimeManifestPaths` (the pre-0.14.83 committed
		// pair under `cacheRoot`) was removed too (user decision 2026-09-30): no iforge host ever
		// ran a CLI that wrote there, so there is no deployment for it to stay compatible with.
		expect(legacy).toEqual([]);
	});

	test("the one generated header, drop-in and serve unit are this product's spelling alone", () => {
		expect([...RUNTIME_SYSTEMD_DROP_IN_FILE_NAMES]).toEqual([RUNTIME_SYSTEMD_DROP_IN_FILE]);
		expect(RUNTIME_SYSTEMD_DROP_IN_FILE).toBe("10-iforge-hosted.conf");
		expect([...SERVE_DAEMON_SYSTEMD_UNIT_BASENAMES]).toEqual(["iforge-serve"]);
		expect(isGeneratedRuntimeSystemdFile(`${GENERATED_RUNTIME_SYSTEMD_FILE_HEADER}\n`)).toBe(true);
		// Upstream clawdi's own header marks ITS units, never this product's.
		expect(
			isGeneratedRuntimeSystemdFile(
				"# Generated by clawdi runtime init. Do not edit inside hosted runtime.\n",
			),
		).toBe(false);
	});

	/**
	 * Upstream WIRE values that carry a retired spelling but are not an alias of any iforge name.
	 * Each is a value the other side of a protocol emits; renaming it here alone would break it.
	 */
	const WIRE_ALLOWANCES: ReadonlyArray<{ file: string; fragment: string }> = [
		{
			// The backend mints the Files JWT with this audience prefix
			// (`backend/app/schemas/runtime.py` `audience_prefix`), and this schema validates it.
			file: join(runtimeSrc, "manifest-contract.ts"),
			fragment: 'const FILE_BROWSER_AUDIENCE_PREFIX = "clawdi-files:";',
		},
	];

	/** The retired spellings, longest first so a match reports the most specific one. */
	const RETIRED_SPELLINGS = [
		"# Generated by clawdi runtime init",
		"clawdi runtime init",
		"10-clawdi-hosted.conf",
		"/var/lib/clawdi-files",
		"/run/clawdi-files",
		"clawdi-runtime-watch",
		"clawdi-runtime-sidecar",
		"ai.clawdi.serve",
		"/var/cache/clawdi",
		"/etc/clawdi",
		"/var/lib/clawdi",
		"/home/clawdi",
		"/run/clawdi",
		"clawdi-daemon",
		"clawdi-serve",
		"clawdi-files",
		'".clawdi"',
	] as const;

	/** Comment-only lines carry rationale, not behaviour. */
	function isComment(line: string): boolean {
		const trimmed = line.trim();
		return trimmed.startsWith("//") || trimmed.startsWith("*") || trimmed.startsWith("/*");
	}

	function nonTestSources(root: string): string[] {
		const files: string[] = [];
		for (const entry of readdirSync(root)) {
			const path = join(root, entry);
			if (statSync(path).isDirectory()) {
				files.push(...nonTestSources(path));
			} else if (entry.endsWith(".ts") && !entry.endsWith(".test.ts")) {
				files.push(path);
			}
		}
		return files;
	}

	test("no non-test CLI source names a retired spelling outside a comment", () => {
		const sources = nonTestSources(join(cliRoot, "src"));
		// A walk that read nothing would pass for the wrong reason.
		expect(sources.length).toBeGreaterThan(100);
		const findings: string[] = [];
		for (const file of sources) {
			readSource(file)
				.split("\n")
				.forEach((line, index) => {
					if (isComment(line)) return;
					const spelling = RETIRED_SPELLINGS.find((candidate) => line.includes(candidate));
					if (spelling === undefined) return;
					if (WIRE_ALLOWANCES.some((allow) => allow.file === file && line.includes(allow.fragment)))
						return;
					findings.push(`${file}:${index + 1}: retired spelling ${spelling}: ${line.trim()}`);
				});
		}
		// The enumeration, not a count — the failure names every survivor.
		expect(findings).toEqual([]);
	});

	test("every wire allowance is live, so a stale one cannot widen the gate silently", () => {
		const dead = WIRE_ALLOWANCES.filter(
			(allow) => !readSource(allow.file).includes(allow.fragment),
		);
		expect(dead).toEqual([]);
	});

	test("the units this product writes carry only the current spelling", () => {
		// These are the `writeSystemdSystemUnit({ name: ... })` call sites.
		const source = readSource(join(runtimeSrc, "runtime-systemd-reconciliation.ts"));
		for (const base of [
			RUNTIME_SYSTEM_UNIT_WATCH,
			RUNTIME_SYSTEM_UNIT_DAEMON,
			RUNTIME_SYSTEM_UNIT_SIDECAR,
		]) {
			expect(source).toContain(`name: ${sourceConstantName(base)},`);
		}
		expect(source).not.toContain('name: "clawdi-');
	});
});

describe("hosted identity move: the restated runtime-user fallbacks agree", () => {
	test("every hosted runtime-user fallback is the iforge account", () => {
		// `systemd-user.ts` and `runtime-user-command.ts` sit below `hosted-runtime-contract.ts`
		// in the import graph, so they restate its value. A `clawdi` fallback here wrote
		// `CLAWDI_RUNTIME_USER=clawdi` into every hosted unit and failed the hosted contract on
		// a real host (06.1-04).
		expect(systemdUser.runtimeUserName.toString()).toContain(`|| "${HOSTED_RUNTIME_USER}"`);
		const userCommand = readSource(join(runtimeSrc, "runtime-user-command.ts"));
		expect(userCommand).toContain(`const HOSTED_RUNTIME_USER_NAME = "${HOSTED_RUNTIME_USER}";`);
		const reconciliation = readSource(join(runtimeSrc, "runtime-systemd-reconciliation.ts"));
		expect(reconciliation).toContain("CLAWDI_RUNTIME_USER: HOSTED_RUNTIME_USER,");
	});
});

describe("hosted identity move: the two uncoupled home literals agree", () => {
	test("paths.ts's inline hosted-home fallback equals HOSTED_RUNTIME_HOME", () => {
		// `hosted-runtime-contract.ts` type-imports `paths.ts`, so `paths.ts` cannot import the
		// constant back without a cycle. The literal is therefore duplicated ON PURPOSE, and this
		// assertion is the only thing standing between that duplication and a host whose resolved
		// HOME fails the contract check it is supposed to satisfy.
		const source = readSource(join(runtimeSrc, "paths.ts"));
		const match = source.match(
			/return envPath\("CLAWDI_RUNTIME_HOME"\) \?\? process\.env\.HOME \?\? "([^"]+)";/,
		);
		// Assert the SITE was found before asserting its value: a regex that stops matching after
		// a refactor would otherwise make this test pass by comparing nothing to nothing.
		expect(match).not.toBeNull();
		expect(match?.[1]).toBe(HOSTED_RUNTIME_HOME);
	});

	test("hosted user state lives outside the hosted home, under the current spelling (#924)", () => {
		// Upstream #924 took hosted CLI state out of the tenant home, where the tenant could read
		// and replace it. The fork's root carries the iforge spelling; the old in-home `~/.iforge`
		// tree is cleaned up by the converge, never resolved to.
		expect(DEFAULT_RUNTIME_USER_CLI_STATE_ROOT).toBe("/var/lib/iforge-user");
		expect(DEFAULT_RUNTIME_USER_CLI_STATE_ROOT.startsWith(`${HOSTED_RUNTIME_HOME}/`)).toBe(false);
		const source = readSource(join(runtimeSrc, "paths.ts"));
		expect(source).not.toContain('join(userHome, ".iforge")');
		expect(source).not.toContain('join(userHome, ".clawdi")');
	});
});
