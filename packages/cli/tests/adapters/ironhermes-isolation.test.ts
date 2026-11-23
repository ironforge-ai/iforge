import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { createHash } from "node:crypto";
import {
	existsSync,
	mkdirSync,
	mkdtempSync,
	readdirSync,
	readFileSync,
	rmSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
	assertAgentRootContains,
	assertAgentRootIsolated,
	isAgentRootIsolated,
} from "../../src/adapters/agent-root-guard";
import { HermesAdapter } from "../../src/adapters/hermes";
import { IronHermesAdapter } from "../../src/adapters/ironhermes";
import { builtinSkillTargetDir } from "../../src/adapters/registry";
import { tarSkillDir } from "../../src/lib/tar";
import {
	type AgentHomeOverrideSnapshot,
	restoreAgentHomeOverrides,
	snapshotAndClearAgentHomeOverrides,
} from "../commands/helpers";
import { makeIronHermesInstall, twoMessages } from "./ironhermes-fixture";

/**
 * D-21 / Success Criterion 1: IronHermes and upstream Hermes are two live
 * runtimes that must never write into each other's tree. This is the reason
 * repointing `$HERMES_HOME` at `~/.ironhermes` was rejected outright.
 *
 * Phase 1's D-17 established an assertion-only boundary proof. D-21
 * deliberately goes beyond it: the surface is six IronHermes stores plus the
 * Hermes install, with two overridable home variables, and upstream supports
 * its variable pointed directly at a profile directory. A test can prove the
 * code is correct today; only a RUNTIME guard catches the environment-variable
 * misconfiguration that makes correct code write to the wrong place.
 *
 * The two install roots are separate `mkdtemp` roots on purpose — reusing one
 * temp root with two subdirectories would make the path-resolution checks
 * vacuous.
 */

let tmpHome: string;
let ironRoot: string;
let hermesRoot: string;
let scratch: string;
/**
 * `<hermesRoot>-old`: a sibling whose name merely SHARES A PREFIX with the
 * Hermes root. The positive control for every read-path case.
 *
 * Only the tests that need it materialize it; `afterEach` removes it either
 * way. Kept out of `beforeEach` on purpose — creating it for every test would
 * silently change the conditions of the existing write-path prefix case, which
 * deliberately runs against a path that does not exist.
 */
let siblingRoot: string;
let origHome: string | undefined;
let origHomeOverrides: AgentHomeOverrideSnapshot = {};

/** relpath -> sha256 of content (directories recorded as a `dir` marker). */
function snapshotTree(root: string): Record<string, string> {
	const out: Record<string, string> = {};
	const walk = (dir: string, prefix: string): void => {
		const entries = readdirSync(dir, { withFileTypes: true }).sort((a, b) =>
			a.name.localeCompare(b.name),
		);
		for (const entry of entries) {
			const full = join(dir, entry.name);
			const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
			if (entry.isDirectory()) {
				out[`${rel}/`] = "dir";
				walk(full, rel);
			} else {
				out[rel] = createHash("sha256").update(readFileSync(full)).digest("hex");
			}
		}
	};
	if (existsSync(root)) walk(root, "");
	return out;
}

function plantSkill(installRoot: string, key: string, body: string): string {
	const dir = join(installRoot, "skills", ...key.split("/"));
	mkdirSync(dir, { recursive: true });
	writeFileSync(join(dir, "SKILL.md"), body);
	return dir;
}

/** A tarball whose single root entry is `key`. */
async function makeArchive(key: string, body: string): Promise<Buffer> {
	const root = join(scratch, `src-${Math.random().toString(36).slice(2)}`);
	const dir = join(root, ...key.split("/"));
	mkdirSync(dir, { recursive: true });
	writeFileSync(join(dir, "SKILL.md"), body);
	return await tarSkillDir(dir, root, key);
}

beforeEach(() => {
	origHome = process.env.HOME;
	origHomeOverrides = snapshotAndClearAgentHomeOverrides();
	tmpHome = mkdtempSync(join(tmpdir(), "clawdi-iso-home-"));
	ironRoot = mkdtempSync(join(tmpdir(), "clawdi-iso-ironhermes-"));
	hermesRoot = mkdtempSync(join(tmpdir(), "clawdi-iso-hermes-"));
	scratch = mkdtempSync(join(tmpdir(), "clawdi-iso-scratch-"));
	siblingRoot = `${hermesRoot}-old`;
	process.env.HOME = tmpHome;
	process.env.IRONHERMES_HOME = ironRoot;
	process.env.HERMES_HOME = hermesRoot;
	mkdirSync(join(ironRoot, "skills"), { recursive: true });
	mkdirSync(join(hermesRoot, "skills"), { recursive: true });
});

afterEach(() => {
	if (origHome) process.env.HOME = origHome;
	else delete process.env.HOME;
	restoreAgentHomeOverrides(origHomeOverrides);
	origHomeOverrides = {};
	for (const dir of [tmpHome, ironRoot, hermesRoot, scratch, siblingRoot]) {
		rmSync(dir, { recursive: true, force: true });
	}
});

describe("cross-tree isolation with BOTH home variables set (D-21)", () => {
	it("IronHermes writeSkillArchive leaves the Hermes tree byte-identical", async () => {
		plantSkill(hermesRoot, "git/commit-helper", "---\nname: hermes original\n---\n");
		const before = snapshotTree(hermesRoot);
		const archive = await makeArchive("git/commit-helper", "---\nname: ironhermes copy\n---\n");

		await new IronHermesAdapter().skills.writeArchive("git/commit-helper", archive);

		expect(
			readFileSync(join(ironRoot, "skills", "git", "commit-helper", "SKILL.md"), "utf-8"),
		).toContain("ironhermes copy");
		expect(snapshotTree(hermesRoot)).toEqual(before);
	});

	it("IronHermes writeSharedSkillArchive leaves the Hermes tree byte-identical", async () => {
		plantSkill(hermesRoot, "shared/demo__owner", "---\nname: hermes original\n---\n");
		const before = snapshotTree(hermesRoot);
		const adapter = new IronHermesAdapter();
		const archive = await makeArchive("demo", "---\nname: ironhermes shared\n---\n");

		await adapter.skills.writeSharedArchive("demo", "owner", archive);

		expect(
			readFileSync(join(adapter.skills.sharedPath("demo", "owner"), "SKILL.md"), "utf-8"),
		).toContain("ironhermes shared");
		expect(snapshotTree(hermesRoot)).toEqual(before);
	});

	it("IronHermes removeLocalSkill leaves a matching key in the Hermes tree untouched", async () => {
		plantSkill(ironRoot, "git/commit-helper", "---\nname: ironhermes\n---\n");
		plantSkill(hermesRoot, "git/commit-helper", "---\nname: hermes\n---\n");
		const before = snapshotTree(hermesRoot);

		await new IronHermesAdapter().skills.remove("git/commit-helper");

		expect(existsSync(join(ironRoot, "skills", "git", "commit-helper"))).toBe(false);
		expect(snapshotTree(hermesRoot)).toEqual(before);
	});

	it("the bundled clawdi target resolves under the IronHermes tree only", () => {
		const before = snapshotTree(hermesRoot);

		expect(builtinSkillTargetDir("ironhermes")).toBe(join(ironRoot, "skills", "iforge"));
		expect(builtinSkillTargetDir("hermes")).toBe(join(hermesRoot, "skills", "iforge"));
		expect(snapshotTree(hermesRoot)).toEqual(before);
	});

	it("swapped: Hermes writeSkillArchive leaves the IronHermes tree byte-identical", async () => {
		plantSkill(ironRoot, "git/commit-helper", "---\nname: ironhermes original\n---\n");
		const before = snapshotTree(ironRoot);
		const archive = await makeArchive("git/commit-helper", "---\nname: hermes copy\n---\n");

		await new HermesAdapter().skills.writeArchive("git/commit-helper", archive);

		expect(
			readFileSync(join(hermesRoot, "skills", "git", "commit-helper", "SKILL.md"), "utf-8"),
		).toContain("hermes copy");
		expect(snapshotTree(ironRoot)).toEqual(before);
	});

	it("swapped: Hermes writeSharedSkillArchive leaves the IronHermes tree byte-identical", async () => {
		plantSkill(ironRoot, "shared/demo__owner", "---\nname: ironhermes original\n---\n");
		const before = snapshotTree(ironRoot);
		const adapter = new HermesAdapter();
		const archive = await makeArchive("demo", "---\nname: hermes shared\n---\n");

		await adapter.skills.writeSharedArchive("demo", "owner", archive);

		expect(
			readFileSync(join(adapter.skills.sharedPath("demo", "owner"), "SKILL.md"), "utf-8"),
		).toContain("hermes shared");
		expect(snapshotTree(ironRoot)).toEqual(before);
	});

	it("swapped: Hermes removeLocalSkill leaves a matching key in the IronHermes tree untouched", async () => {
		plantSkill(ironRoot, "git/commit-helper", "---\nname: ironhermes\n---\n");
		plantSkill(hermesRoot, "git/commit-helper", "---\nname: hermes\n---\n");
		const before = snapshotTree(ironRoot);

		await new HermesAdapter().skills.remove("git/commit-helper");

		expect(existsSync(join(hermesRoot, "skills", "git", "commit-helper"))).toBe(false);
		expect(snapshotTree(ironRoot)).toEqual(before);
	});
});

describe("a mis-set home variable is refused at RUNTIME, not just in a test (T-02-11)", () => {
	it("IRONHERMES_HOME pointed AT the Hermes tree refuses writeSkillArchive", async () => {
		const before = snapshotTree(hermesRoot);
		process.env.IRONHERMES_HOME = hermesRoot;
		const archive = await makeArchive("git/commit-helper", "---\nname: leak\n---\n");

		await expect(
			new IronHermesAdapter().skills.writeArchive("git/commit-helper", archive),
		).rejects.toThrow(hermesRoot);

		// A refused write leaves NO partial artifact — not even a parent directory.
		expect(snapshotTree(hermesRoot)).toEqual(before);
		expect(existsSync(join(hermesRoot, "skills", "git", "commit-helper"))).toBe(false);
	});

	it("IRONHERMES_HOME pointed AT the Hermes tree refuses writeSharedSkillArchive", async () => {
		const before = snapshotTree(hermesRoot);
		process.env.IRONHERMES_HOME = hermesRoot;
		const archive = await makeArchive("demo", "---\nname: leak\n---\n");

		await expect(
			new IronHermesAdapter().skills.writeSharedArchive("demo", "owner", archive),
		).rejects.toThrow(hermesRoot);
		expect(snapshotTree(hermesRoot)).toEqual(before);
	});

	it("IRONHERMES_HOME pointed AT the Hermes tree refuses removeLocalSkill", async () => {
		plantSkill(hermesRoot, "git/commit-helper", "---\nname: hermes\n---\n");
		const before = snapshotTree(hermesRoot);
		process.env.IRONHERMES_HOME = hermesRoot;

		await expect(new IronHermesAdapter().skills.remove("git/commit-helper")).rejects.toThrow(
			hermesRoot,
		);
		expect(snapshotTree(hermesRoot)).toEqual(before);
	});

	it("IRONHERMES_HOME pointed AT the Hermes tree refuses the bundled clawdi target", () => {
		process.env.IRONHERMES_HOME = hermesRoot;
		expect(() => builtinSkillTargetDir("ironhermes")).toThrow(hermesRoot);
	});

	it("IRONHERMES_HOME pointed at a directory INSIDE the Hermes tree is refused too", async () => {
		const nested = join(hermesRoot, "profiles", "dev");
		mkdirSync(nested, { recursive: true });
		const before = snapshotTree(hermesRoot);
		process.env.IRONHERMES_HOME = nested;
		const archive = await makeArchive("demo", "---\nname: leak\n---\n");

		await expect(new IronHermesAdapter().skills.writeArchive("demo", archive)).rejects.toThrow(
			/ironhermes/i,
		);
		expect(snapshotTree(hermesRoot)).toEqual(before);
	});

	it("swapped: HERMES_HOME pointed AT the IronHermes tree refuses every write path", async () => {
		const before = snapshotTree(ironRoot);
		process.env.HERMES_HOME = ironRoot;
		const adapter = new HermesAdapter();
		const archive = await makeArchive("demo", "---\nname: leak\n---\n");

		await expect(adapter.skills.writeArchive("demo", archive)).rejects.toThrow(ironRoot);
		await expect(adapter.skills.writeSharedArchive("demo", "owner", archive)).rejects.toThrow(
			ironRoot,
		);
		await expect(adapter.skills.remove("demo")).rejects.toThrow(ironRoot);
		expect(() => builtinSkillTargetDir("hermes")).toThrow(ironRoot);
		expect(snapshotTree(ironRoot)).toEqual(before);
	});

	it("a legitimate, correctly-configured write still succeeds", async () => {
		const archive = await makeArchive("demo", "---\nname: legitimate\n---\n");

		await new IronHermesAdapter().skills.writeArchive("demo", archive);

		expect(readFileSync(join(ironRoot, "skills", "demo", "SKILL.md"), "utf-8")).toContain(
			"legitimate",
		);
	});
});

describe("assertAgentRootContains", () => {
	it("refuses a target whose `..` segments resolve outside the root, after resolution", () => {
		const root = join(ironRoot, "skills");
		const escapingTarget = join(root, "git", "..", "..", "..", "elsewhere", "evil");

		expect(() => assertAgentRootContains(root, escapingTarget, "unit")).toThrow();
		// The message must name BOTH the offending target and the root it escaped.
		expect(() => assertAgentRootContains(root, escapingTarget, "unit")).toThrow(root);
	});

	it("refuses a sibling whose name merely starts with the root's name", () => {
		// A raw string-prefix comparison would accept this. Segment comparison
		// must not.
		expect(() =>
			assertAgentRootContains(`${ironRoot}/skills`, `${ironRoot}/skills-evil/x`, "unit"),
		).toThrow();
	});

	it("accepts the root itself and any descendant", () => {
		const root = join(ironRoot, "skills");
		expect(() => assertAgentRootContains(root, root, "unit")).not.toThrow();
		expect(() =>
			assertAgentRootContains(root, join(root, "git", "commit-helper"), "unit"),
		).not.toThrow();
	});

	it("does not false-positive on a symlinked but genuinely contained path", () => {
		const realRoot = join(scratch, "real-install");
		mkdirSync(join(realRoot, "skills", "git", "commit-helper"), { recursive: true });
		const linkedRoot = join(scratch, "linked-install");
		symlinkSync(realRoot, linkedRoot);

		// Root given through the symlink, target given canonically, and the
		// reverse. Both are genuinely the same tree.
		expect(() =>
			assertAgentRootContains(
				join(linkedRoot, "skills"),
				join(realRoot, "skills", "git", "commit-helper"),
				"unit",
			),
		).not.toThrow();
		expect(() =>
			assertAgentRootContains(
				join(realRoot, "skills"),
				join(linkedRoot, "skills", "git", "commit-helper"),
				"unit",
			),
		).not.toThrow();
	});

	it("names the context so the caller can tell which write was refused", () => {
		expect(() =>
			assertAgentRootContains(join(ironRoot, "skills"), join(hermesRoot, "skills"), "demo write"),
		).toThrow("demo write");
	});
});

describe("assertAgentRootIsolated", () => {
	it("refuses an agent root equal to a foreign runtime's root", () => {
		expect(() => assertAgentRootIsolated(hermesRoot, [hermesRoot], "unit")).toThrow(hermesRoot);
	});

	it("refuses an agent root nested inside a foreign runtime's root", () => {
		expect(() =>
			assertAgentRootIsolated(join(hermesRoot, "profiles", "dev"), [hermesRoot], "unit"),
		).toThrow();
	});

	it("permits a foreign root nested inside the agent's own root", () => {
		// This direction is NOT a violation: the agent writing inside its own
		// tree is legitimate. The foreign runtime detects the collision from its
		// own side, where its root IS nested inside another's.
		expect(() =>
			assertAgentRootIsolated(ironRoot, [join(ironRoot, "nested-foreign")], "unit"),
		).not.toThrow();
	});

	it("permits two genuinely separate roots", () => {
		expect(() => assertAgentRootIsolated(ironRoot, [hermesRoot], "unit")).not.toThrow();
	});

	it("is not fooled by a sibling whose name starts with the foreign root's name", () => {
		expect(() =>
			assertAgentRootIsolated(`${hermesRoot}-other`, [hermesRoot], "unit"),
		).not.toThrow();
	});
});

/**
 * ---------------------------------------------------------------------------
 * READ paths (T-02-05 / WR-04 / WR-05)
 * ---------------------------------------------------------------------------
 *
 * Everything above this line is a WRITE. The guard shipped wired into writes
 * only, which left the read half open: with `$IRONHERMES_HOME` pointed at a
 * Hermes install, `detect()` reported IronHermes present and `collectSessions()`
 * handed back upstream Hermes's sessions to be uploaded under the IronHermes
 * identity. Nothing was written into the wrong tree — it was read out of it.
 *
 * The two halves answer differently on purpose, and the split is by what the
 * caller can do about the failure:
 *
 *   detect()      warn + return false. `src/lib/select-adapter.ts` fans every
 *                 adapter's detect() into one `Promise.all` with no try/catch
 *                 at any of its three call sites, so a throw here would take
 *                 Claude Code, Codex and OpenClaw down with it.
 *   collect*()    throw. A silent skip uploads nothing; a silent pass uploads
 *                 the wrong runtime's data. Fail closed.
 *
 * Every store below is planted deliberately: without one, `detect()` answers
 * `false` because the install is empty, and the test would pass against
 * completely unguarded code.
 */

function captureWarnings(): { warnings: string[]; restore: () => void } {
	const warnings: string[] = [];
	const origWarn = console.warn;
	console.warn = (...args: unknown[]) => {
		warnings.push(args.map(String).join(" "));
	};
	return { warnings, restore: () => (console.warn = origWarn) };
}

/**
 * A real, readable store holding one readable session.
 *
 * Messages included deliberately: a session row with none is dropped from
 * `collectSessions`, which would make the positive control below pass against a
 * guard that wrongly refused it.
 */
function plantStore(installRoot: string, sessionId: string): void {
	const startedAt = 1_700_000_000;
	makeIronHermesInstall(installRoot, {
		stores: [
			{
				relativePath: "state.db",
				sessions: [
					{ id: sessionId, startedAt, workspaceRoot: null, messages: twoMessages(startedAt) },
				],
			},
		],
	});
}

describe("isAgentRootIsolated is the predicate form of assertAgentRootIsolated (T-02-05)", () => {
	// The read paths need a boolean and the write paths need a throw, but the two
	// forms must never disagree about WHAT is isolated. Driving both over the same
	// inputs is what stops the read half growing a re-derived rule of its own.

	it("returns false for exactly the inputs the assertion throws on", () => {
		const refused: Array<[string, string[]]> = [
			[hermesRoot, [hermesRoot]],
			[join(hermesRoot, "profiles", "dev"), [hermesRoot]],
		];
		for (const [root, foreign] of refused) {
			expect(() => assertAgentRootIsolated(root, foreign, "unit")).toThrow();
			expect(isAgentRootIsolated(root, foreign)).toBe(false);
		}
	});

	it("returns true for exactly the inputs the assertion accepts", () => {
		const permitted: Array<[string, string[]]> = [
			[ironRoot, [hermesRoot]],
			// A foreign root nested inside our own is NOT a violation — see the
			// directionality note on `assertAgentRootIsolated`.
			[ironRoot, [join(ironRoot, "nested-foreign")]],
			[siblingRoot, [hermesRoot]],
		];
		for (const [root, foreign] of permitted) {
			expect(() => assertAgentRootIsolated(root, foreign, "unit")).not.toThrow();
			expect(isAgentRootIsolated(root, foreign)).toBe(true);
		}
	});
});

describe("IronHermes read paths refuse a cross-tree install root (T-02-05)", () => {
	it("detect() returns false and warns when $IRONHERMES_HOME IS the Hermes tree", async () => {
		plantStore(hermesRoot, "agent:main:hermes-1");
		process.env.IRONHERMES_HOME = hermesRoot;
		const cap = captureWarnings();
		try {
			expect(await new IronHermesAdapter().detect()).toBe(false);
			const joined = cap.warnings.join("\n");
			expect(joined).toContain("[ironhermes]");
			// The warning names BOTH roots — a refusal the operator cannot locate
			// is barely better than a silent one.
			expect(joined).toContain(hermesRoot);
		} finally {
			cap.restore();
		}
	});

	it("detect() returns false when $IRONHERMES_HOME is nested INSIDE the Hermes tree", async () => {
		// Upstream supports `$HERMES_HOME` pointed straight at a profile
		// directory, so this arrangement is reachable without doing anything
		// obviously wrong.
		const nested = join(hermesRoot, "profiles", "dev");
		plantStore(nested, "agent:main:hermes-profile-1");
		process.env.IRONHERMES_HOME = nested;
		const cap = captureWarnings();
		try {
			expect(await new IronHermesAdapter().detect()).toBe(false);
			expect(cap.warnings.join("\n")).toContain("[ironhermes]");
		} finally {
			cap.restore();
		}
	});

	it("detect() returns false when $IRONHERMES_HOME is a SYMLINK into the Hermes tree", async () => {
		// Containment is a resolution result, not a string comparison.
		plantStore(hermesRoot, "agent:main:hermes-2");
		const link = join(scratch, "iron-link");
		symlinkSync(hermesRoot, link);
		process.env.IRONHERMES_HOME = link;
		const cap = captureWarnings();
		try {
			expect(await new IronHermesAdapter().detect()).toBe(false);
		} finally {
			cap.restore();
		}
	});

	it("detect() does NOT throw — select-adapter.ts calls it inside an unguarded Promise.all", async () => {
		// `src/lib/select-adapter.ts` maps every adapter's detect() into one
		// `Promise.all`, and none of the three call sites (select-adapter.ts,
		// doctor.ts, setup.ts) wraps it in try/catch. A throw here rejects the
		// whole batch, so one mis-set env var would stop `clawdi push` working
		// for Claude Code, Codex and OpenClaw as well. Warn-and-return-false is
		// deliberate. Do not "simplify" this to a throw.
		plantStore(hermesRoot, "agent:main:hermes-3");
		process.env.IRONHERMES_HOME = hermesRoot;
		const cap = captureWarnings();
		try {
			await expect(new IronHermesAdapter().detect()).resolves.toBe(false);
		} finally {
			cap.restore();
		}
	});

	it("collectSessions() throws rather than returning the Hermes tree's sessions", async () => {
		plantStore(hermesRoot, "agent:main:hermes-4");
		process.env.IRONHERMES_HOME = hermesRoot;

		await expect(new IronHermesAdapter().sessions.collect({ kind: "complete" })).rejects.toThrow(
			hermesRoot,
		);
	});

	it("collectSkills() throws, and takes no ledger lock on the way (WR-05)", async () => {
		plantSkill(hermesRoot, "git/commit-helper", "---\nname: hermes original\n---\n");
		process.env.IRONHERMES_HOME = hermesRoot;
		const before = snapshotTree(hermesRoot);

		await expect(new IronHermesAdapter().skills.collect()).rejects.toThrow(hermesRoot);

		// `migrateLegacyLocalSetupSkill` runs first inside this method and takes
		// the ledger write lock. The guard has to land BEFORE it, or a read path
		// mutates the foreign tree on its way to refusing.
		expect(snapshotTree(hermesRoot)).toEqual(before);
		expect(existsSync(join(hermesRoot, "skills", "clawdi"))).toBe(false);
	});

	it("listSkillKeys() throws, and takes no ledger lock on the way (WR-05)", async () => {
		plantSkill(hermesRoot, "git/commit-helper", "---\nname: hermes original\n---\n");
		process.env.IRONHERMES_HOME = hermesRoot;
		const before = snapshotTree(hermesRoot);

		await expect(new IronHermesAdapter().skills.listKeys()).rejects.toThrow(hermesRoot);

		expect(snapshotTree(hermesRoot)).toEqual(before);
		expect(existsSync(join(hermesRoot, "skills", "clawdi"))).toBe(false);
	});
});

describe("swapped: upstream Hermes read paths refuse a cross-tree install root (T-02-05)", () => {
	// Upstream Hermes reading an IronHermes tree is the same defect with the
	// labels swapped, and the guard is directional — only "my root is inside
	// theirs" is a violation — so each nesting case has to be asserted against
	// the runtime whose root is the nested one.

	it("detect() returns false and warns when $HERMES_HOME IS the IronHermes tree", async () => {
		plantStore(ironRoot, "agent:main:iron-1");
		process.env.HERMES_HOME = ironRoot;
		const cap = captureWarnings();
		try {
			expect(await new HermesAdapter().detect()).toBe(false);
			const joined = cap.warnings.join("\n");
			expect(joined).toContain("[hermes]");
			expect(joined).toContain(ironRoot);
		} finally {
			cap.restore();
		}
	});

	it("detect() returns false when $HERMES_HOME is nested INSIDE the IronHermes tree", async () => {
		const nested = join(ironRoot, "profiles", "dev");
		plantStore(nested, "agent:main:iron-profile-1");
		process.env.HERMES_HOME = nested;
		const cap = captureWarnings();
		try {
			expect(await new HermesAdapter().detect()).toBe(false);
		} finally {
			cap.restore();
		}
	});

	it("every collect path throws", async () => {
		plantStore(ironRoot, "agent:main:iron-2");
		plantSkill(ironRoot, "git/commit-helper", "---\nname: ironhermes original\n---\n");
		process.env.HERMES_HOME = ironRoot;
		const adapter = new HermesAdapter();
		const before = snapshotTree(ironRoot);

		await expect(adapter.sessions.collect({ kind: "complete" })).rejects.toThrow(ironRoot);
		await expect(adapter.skills.collect()).rejects.toThrow(ironRoot);
		await expect(adapter.skills.listKeys()).rejects.toThrow(ironRoot);

		expect(snapshotTree(ironRoot)).toEqual(before);
	});
});

describe("the positive control: a prefix-sharing sibling is NOT refused on any read path", () => {
	// `~/.hermes-old` beside `~/.hermes`. This is the row that must never be
	// dropped: a prefix-string implementation of containment passes every
	// refusal case above and fails only here.

	it("detect() returns true and warns about nothing", async () => {
		plantStore(siblingRoot, "agent:main:old-1");
		process.env.IRONHERMES_HOME = siblingRoot;
		const cap = captureWarnings();
		try {
			expect(await new IronHermesAdapter().detect()).toBe(true);
			expect(cap.warnings).toEqual([]);
		} finally {
			cap.restore();
		}
	});

	it("collectSessions() reads it without throwing", async () => {
		plantStore(siblingRoot, "agent:main:old-2");
		process.env.IRONHERMES_HOME = siblingRoot;

		const result = await new IronHermesAdapter().sessions.collect({ kind: "complete" });

		expect(result.sessions.map((s) => s.localSessionId)).toContain("agent.main.old-2");
	});

	it("collectSkills() and listSkillKeys() read it without throwing", async () => {
		mkdirSync(join(siblingRoot, "skills"), { recursive: true });
		plantSkill(siblingRoot, "git/commit-helper", "---\nname: sibling\n---\n");
		process.env.IRONHERMES_HOME = siblingRoot;
		const adapter = new IronHermesAdapter();

		expect((await adapter.skills.collect()).map((s) => s.skillKey)).toContain("git/commit-helper");
		expect(await adapter.skills.listKeys()).toContain("git/commit-helper");
	});
});
