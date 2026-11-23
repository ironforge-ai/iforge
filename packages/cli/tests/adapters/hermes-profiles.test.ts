import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { listAgentStorePaths } from "../../src/adapters/hermes-profiles";
import { storeHasSessionsTable } from "../../src/adapters/sqlite-store";
import {
	type AgentHomeOverrideSnapshot,
	restoreAgentHomeOverrides,
	snapshotAndClearAgentHomeOverrides,
} from "../commands/helpers";
import { makeIronHermesInstall, twoMessages } from "./ironhermes-fixture";

/**
 * Enumeration coverage for the one store walk both Hermes-lineage adapters use.
 *
 * The walk answers exactly one question: which `state.db` files inside ONE
 * install root carry a `sessions` table. Profiles live inside that root; a
 * second install would sit beside it and is explicitly out of scope.
 */

const T0 = 1_770_000_000;

let tmpHome: string;
let origHome: string | undefined;
let origHomeOverrides: AgentHomeOverrideSnapshot = {};
/** Directories chmod'd unreadable by a test, restored before cleanup. */
let lockedDirs: string[] = [];
/** Files chmod'd unreadable by a test, restored before cleanup. */
let lockedFiles: string[] = [];

function installRoot(): string {
	return join(tmpHome, ".ironhermes");
}

function sessionsFor(id: string) {
	return [{ id, startedAt: T0, messages: twoMessages(T0) }];
}

beforeEach(() => {
	origHome = process.env.HOME;
	origHomeOverrides = snapshotAndClearAgentHomeOverrides();
	tmpHome = mkdtempSync(join(tmpdir(), "clawdi-hermes-profiles-"));
	process.env.HOME = tmpHome;
	lockedDirs = [];
	lockedFiles = [];
});

afterEach(() => {
	for (const dir of lockedDirs) {
		try {
			chmodSync(dir, 0o755);
		} catch {
			// Best effort: the test may already have restored it.
		}
	}
	for (const file of lockedFiles) {
		try {
			chmodSync(file, 0o644);
		} catch {
			// Best effort: the test may already have restored it.
		}
	}
	lockedDirs = [];
	lockedFiles = [];
	if (origHome) process.env.HOME = origHome;
	else delete process.env.HOME;
	restoreAgentHomeOverrides(origHomeOverrides);
	origHomeOverrides = {};
	rmSync(tmpHome, { recursive: true, force: true });
});

describe("listAgentStorePaths", () => {
	it("returns the root store first, then profile stores in stable sorted order", async () => {
		makeIronHermesInstall(installRoot(), {
			stores: [
				{ relativePath: "state.db", sessions: sessionsFor("root:1") },
				{ relativePath: "profiles/zeta/state.db", sessions: sessionsFor("zeta:1") },
				{ relativePath: "profiles/alpha/state.db", sessions: sessionsFor("alpha:1") },
			],
		});

		expect(await listAgentStorePaths(installRoot())).toEqual([
			join(installRoot(), "state.db"),
			join(installRoot(), "profiles", "alpha", "state.db"),
			join(installRoot(), "profiles", "zeta", "state.db"),
		]);
	});

	it("skips a profile directory that exists but carries no state.db", async () => {
		makeIronHermesInstall(installRoot(), {
			stores: [
				{ relativePath: "state.db", sessions: sessionsFor("root:1") },
				{ relativePath: "profiles/alpha/state.db", sessions: sessionsFor("alpha:1") },
			],
			// 3 of the 8 live profile directories are exactly this case.
			emptyDirs: ["profiles/gamma", "profiles/epsilon"],
		});

		const paths = await listAgentStorePaths(installRoot());
		expect(paths).toEqual([
			join(installRoot(), "state.db"),
			join(installRoot(), "profiles", "alpha", "state.db"),
		]);
		expect(paths.some((p) => p.includes("gamma"))).toBe(false);
	});

	it("excludes a zero-byte profile store — the probe decides, not the filesystem", async () => {
		makeIronHermesInstall(installRoot(), {
			stores: [
				{ relativePath: "state.db", sessions: sessionsFor("root:1") },
				{ relativePath: "profiles/delta/state.db", sessions: [], empty: true },
			],
		});

		expect(await listAgentStorePaths(installRoot())).toEqual([join(installRoot(), "state.db")]);
	});

	it("returns an empty list for an install root that does not exist", async () => {
		expect(await listAgentStorePaths(join(tmpHome, "nowhere"))).toEqual([]);
	});

	it("returns exactly one path when the install has no profiles/ directory", async () => {
		// Assumption-delta invariant: the single-store case is the ONE-ELEMENT
		// case of the general form, not a separate branch.
		makeIronHermesInstall(installRoot(), {
			stores: [{ relativePath: "state.db", sessions: sessionsFor("root:1") }],
		});

		expect(await listAgentStorePaths(installRoot())).toEqual([join(installRoot(), "state.db")]);
	});

	it("warns on stderr and still returns the root store when profiles/ cannot be enumerated", async () => {
		makeIronHermesInstall(installRoot(), {
			stores: [
				{ relativePath: "state.db", sessions: sessionsFor("root:1") },
				{ relativePath: "profiles/alpha/state.db", sessions: sessionsFor("alpha:1") },
			],
		});
		const profilesDir = join(installRoot(), "profiles");
		chmodSync(profilesDir, 0o000);
		lockedDirs.push(profilesDir);

		const warnings: string[] = [];
		const origWarn = console.warn;
		console.warn = (...args: unknown[]) => {
			warnings.push(args.map(String).join(" "));
		};
		let paths: string[];
		try {
			paths = await listAgentStorePaths(installRoot(), { label: "ironhermes" });
		} finally {
			console.warn = origWarn;
		}

		// Never an empty list: that is how a push reports success with 0 sessions
		// while silently swallowing the user's data.
		expect(paths).toEqual([join(installRoot(), "state.db")]);
		expect(warnings).toHaveLength(1);
		expect(warnings[0]).toContain("[ironhermes]");
		expect(warnings[0]).toContain(profilesDir);
	});

	it("warns when the INSTALL ROOT itself cannot be enumerated (T-02-07)", async () => {
		// The sibling case above covers an unreadable `profiles/`. The root itself
		// was the silent one: `existsSync(installRoot)` is true for a chmod-000
		// directory (it tests F_OK, not readability), every child probe beneath it
		// then fails, and the walk returns [] having warned nothing.
		//
		// That is the shape the whole module exists to prevent — `clawdi push`
		// reporting success with 0 sessions while the user's stores sit right
		// there, unread. Two of the three declared cases warned; this was the third.
		makeIronHermesInstall(installRoot(), {
			stores: [
				{ relativePath: "state.db", sessions: sessionsFor("root:1") },
				{ relativePath: "profiles/alpha/state.db", sessions: sessionsFor("alpha:1") },
			],
		});
		const root = installRoot();
		chmodSync(root, 0o000);
		lockedDirs.push(root);

		const warnings: string[] = [];
		const origWarn = console.warn;
		console.warn = (...args: unknown[]) => {
			warnings.push(args.map(String).join(" "));
		};
		let paths: string[];
		try {
			paths = await listAgentStorePaths(root, { label: "ironhermes" });
		} finally {
			console.warn = origWarn;
		}

		// The stores are genuinely unreachable, so an empty list is the honest
		// answer here — what must NOT happen is returning it silently.
		expect(paths).toEqual([]);
		expect(warnings).toHaveLength(1);
		expect(warnings[0]).toContain("[ironhermes]");
		expect(warnings[0]).toContain(root);
	});

	it("warns and skips a store that exists but cannot be opened", async () => {
		// A store the process cannot read must never be silently folded into a
		// successful zero-session push (T-02-07). The probe alone would answer
		// "no sessions table" and say nothing.
		makeIronHermesInstall(installRoot(), {
			stores: [
				{ relativePath: "state.db", sessions: sessionsFor("root:1") },
				{ relativePath: "profiles/locked/state.db", sessions: sessionsFor("locked:1") },
			],
		});
		const locked = join(installRoot(), "profiles", "locked", "state.db");
		chmodSync(locked, 0o000);
		lockedFiles.push(locked);

		const warnings: string[] = [];
		const origWarn = console.warn;
		console.warn = (...args: unknown[]) => {
			warnings.push(args.map(String).join(" "));
		};
		let paths: string[];
		try {
			paths = await listAgentStorePaths(installRoot(), { label: "ironhermes" });
		} finally {
			console.warn = origWarn;
		}

		expect(paths).toEqual([join(installRoot(), "state.db")]);
		expect(warnings.some((w) => w.includes(locked))).toBe(true);
		expect(warnings.some((w) => w.includes("[ironhermes]"))).toBe(true);
	});

	it("ignores dot-directories under profiles/", async () => {
		makeIronHermesInstall(installRoot(), {
			stores: [
				{ relativePath: "state.db", sessions: sessionsFor("root:1") },
				{ relativePath: "profiles/.hidden/state.db", sessions: sessionsFor("hidden:1") },
				{ relativePath: "profiles/alpha/state.db", sessions: sessionsFor("alpha:1") },
			],
		});

		const paths = await listAgentStorePaths(installRoot());
		expect(paths).toEqual([
			join(installRoot(), "state.db"),
			join(installRoot(), "profiles", "alpha", "state.db"),
		]);
	});

	it("finds a profile store when the install root carries no store of its own", async () => {
		makeIronHermesInstall(installRoot(), {
			stores: [{ relativePath: "profiles/dev/state.db", sessions: sessionsFor("dev:1") }],
		});

		expect(await listAgentStorePaths(installRoot())).toEqual([
			join(installRoot(), "profiles", "dev", "state.db"),
		]);
	});

	it("labels the warning with the install root basename when no label is supplied", async () => {
		makeIronHermesInstall(installRoot(), {
			stores: [{ relativePath: "state.db", sessions: sessionsFor("root:1") }],
			emptyDirs: ["profiles"],
		});
		const profilesDir = join(installRoot(), "profiles");
		chmodSync(profilesDir, 0o000);
		lockedDirs.push(profilesDir);

		const warnings: string[] = [];
		const origWarn = console.warn;
		console.warn = (...args: unknown[]) => {
			warnings.push(args.map(String).join(" "));
		};
		try {
			await listAgentStorePaths(installRoot());
		} finally {
			console.warn = origWarn;
		}

		expect(warnings[0]).toContain("[.ironhermes]");
	});
});

describe("storeHasSessionsTable", () => {
	it("returns false for a zero-byte file without throwing", async () => {
		makeIronHermesInstall(installRoot(), {
			stores: [{ relativePath: "state.db", sessions: [], empty: true }],
		});

		expect(await storeHasSessionsTable(join(installRoot(), "state.db"))).toBe(false);
	});

	it("returns false for a file that is not a SQLite database at all", async () => {
		makeIronHermesInstall(installRoot(), { stores: [] });
		const junk = join(installRoot(), "state.db");
		writeFileSync(junk, "this is not a database, it is a text file\n");

		expect(await storeHasSessionsTable(junk)).toBe(false);
	});

	it("returns false for a real SQLite file with no sessions table", async () => {
		makeIronHermesInstall(installRoot(), {
			stores: [{ relativePath: "state.db", sessions: [], omitSessionsTable: true }],
		});

		expect(await storeHasSessionsTable(join(installRoot(), "state.db"))).toBe(false);
	});

	it("returns false for a path that does not exist", async () => {
		expect(await storeHasSessionsTable(join(tmpHome, "absent.db"))).toBe(false);
	});

	it("returns true for a store carrying a sessions table", async () => {
		makeIronHermesInstall(installRoot(), {
			stores: [{ relativePath: "state.db", sessions: sessionsFor("root:1") }],
		});

		expect(await storeHasSessionsTable(join(installRoot(), "state.db"))).toBe(true);
	});
});
