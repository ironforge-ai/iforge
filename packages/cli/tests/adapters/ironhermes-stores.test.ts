import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { IronHermesAdapter } from "../../src/adapters/ironhermes";
import {
	type AgentHomeOverrideSnapshot,
	restoreAgentHomeOverrides,
	snapshotAndClearAgentHomeOverrides,
} from "../commands/helpers";
import { type FixtureSession, makeIronHermesInstall, twoMessages } from "./ironhermes-fixture";

/**
 * Multi-store behaviour for IronHermes: every store in ONE install root — the
 * root `state.db` plus each `profiles/<name>/state.db` — pools into a single
 * Clawdi agent (D-11). Reading only the root drops 37% of recorded activity on
 * the live install, and the profile stores are the better-attributed ones.
 */

const T0 = 1_770_000_000;

let tmpHome: string;
let origHome: string | undefined;
let origHomeOverrides: AgentHomeOverrideSnapshot = {};

function installRoot(): string {
	return join(tmpHome, ".ironhermes");
}

function session(id: string, startedAt: number, workspaceRoot?: string | null): FixtureSession {
	return {
		id,
		startedAt,
		workspaceRoot: workspaceRoot ?? null,
		messages: twoMessages(startedAt),
	};
}

/** Root store (3 sessions) + `profiles/alpha` (2) + `profiles/beta` (1). */
function seedPooledInstall(): void {
	makeIronHermesInstall(installRoot(), {
		stores: [
			{
				relativePath: "state.db",
				sessions: [
					session("agent:root:1", T0 + 900, "/w/root-a"),
					session("agent:root:2", T0 + 800, "/w/root-b"),
					session("agent:root:3", T0 + 700, null),
				],
			},
			{
				relativePath: "profiles/alpha/state.db",
				sessions: [
					session("agent:alpha:1", T0 + 600, "/w/alpha-a"),
					session("agent:alpha:2", T0 + 500, "/w/root-a/nested"),
				],
			},
			{
				relativePath: "profiles/beta/state.db",
				sessions: [session("agent:beta:1", T0 + 400, "/w/beta-a")],
			},
		],
		// Present on the live install; a walk that assumes a store here throws.
		emptyDirs: ["profiles/gamma"],
	});
}

function captureWarnings(): { warnings: string[]; restore: () => void } {
	const warnings: string[] = [];
	const origWarn = console.warn;
	console.warn = (...args: unknown[]) => {
		warnings.push(args.map(String).join(" "));
	};
	return { warnings, restore: () => (console.warn = origWarn) };
}

beforeEach(() => {
	origHome = process.env.HOME;
	origHomeOverrides = snapshotAndClearAgentHomeOverrides();
	tmpHome = mkdtempSync(join(tmpdir(), "clawdi-ironhermes-stores-"));
	process.env.HOME = tmpHome;
});

afterEach(() => {
	if (origHome) process.env.HOME = origHome;
	else delete process.env.HOME;
	restoreAgentHomeOverrides(origHomeOverrides);
	origHomeOverrides = {};
	rmSync(tmpHome, { recursive: true, force: true });
});

describe("IronHermesAdapter.detect — across stores", () => {
	it("is true when only a profile store exists and the root store is absent", async () => {
		makeIronHermesInstall(installRoot(), {
			stores: [{ relativePath: "profiles/dev/state.db", sessions: [session("agent:dev:1", T0)] }],
		});
		expect(await new IronHermesAdapter().detect()).toBe(true);
	});

	it("is false for an install root with no store anywhere", async () => {
		mkdirSync(join(installRoot(), "profiles", "dev"), { recursive: true });
		expect(await new IronHermesAdapter().detect()).toBe(false);
	});
});

describe("IronHermesAdapter.collectSessions — pooling", () => {
	it("returns the union of every store's sessions in one flat array", async () => {
		seedPooledInstall();
		const { sessions, coverage } = await new IronHermesAdapter().sessions.collect({
			kind: "complete",
		});
		expect(coverage).toBe("complete");
		expect(sessions).toHaveLength(6);
		expect(sessions.map((s) => s.localSessionId).sort()).toEqual([
			"agent.alpha.1",
			"agent.alpha.2",
			"agent.beta.1",
			"agent.root.1",
			"agent.root.2",
			"agent.root.3",
		]);
	});

	it("anchors rawFilePath to the store the session actually came from", async () => {
		seedPooledInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });

		const fromBeta = sessions.find((s) => s.localSessionId === "agent.beta.1");
		expect(fromBeta?.rawFilePath).toBe(
			`${join(installRoot(), "profiles", "beta", "state.db")}#agent:beta:1`,
		);
		expect(fromBeta?.rawFilePath).toContain(join("profiles", "beta", "state.db#"));

		const fromRoot = sessions.find((s) => s.localSessionId === "agent.root.1");
		expect(fromRoot?.rawFilePath).toBe(`${join(installRoot(), "state.db")}#agent:root:1`);
		expect(fromRoot?.rawFilePath).not.toContain("profiles");
	});

	it("contributes nothing for a profile directory with no store, and does not throw", async () => {
		seedPooledInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		expect(sessions.every((s) => !s.rawFilePath.includes("gamma"))).toBe(true);
	});

	it("warns and keeps the remaining stores when one store fails mid-pool", async () => {
		makeIronHermesInstall(installRoot(), {
			stores: [
				{ relativePath: "state.db", sessions: [session("agent:root:1", T0 + 900)] },
				// Passes the enumeration probe (it HAS a sessions table) and then
				// throws when the messages statement is prepared.
				{
					relativePath: "profiles/broken/state.db",
					sessions: [session("agent:broken:1", T0 + 800)],
					omitMessagesTable: true,
				},
				{ relativePath: "profiles/good/state.db", sessions: [session("agent:good:1", T0 + 700)] },
			],
		});

		const cap = captureWarnings();
		let sessions: Awaited<ReturnType<IronHermesAdapter["collectSessions"]>>["sessions"];
		let coverage: Awaited<ReturnType<IronHermesAdapter["collectSessions"]>>["coverage"];
		try {
			({ sessions, coverage } = await new IronHermesAdapter().sessions.collect({
				kind: "complete",
			}));
		} finally {
			cap.restore();
		}

		expect(sessions.map((s) => s.localSessionId).sort()).toEqual(["agent.good.1", "agent.root.1"]);
		// #1114: a skipped store makes the scan partial, so the daemon never
		// treats the broken store's sessions as absent.
		expect(coverage).toBe("partial");
		const broken = join(installRoot(), "profiles", "broken", "state.db");
		expect(cap.warnings.some((w) => w.includes(broken))).toBe(true);
		expect(cap.warnings.some((w) => w.includes("[ironhermes]"))).toBe(true);
	});

	it("applies projectFilter across the pooled result, not per store", async () => {
		seedPooledInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({
			kind: "complete",
			projectFilter: "/w/root-a",
		});
		// `/w/root-a` (root store) and `/w/root-a/nested` (alpha profile store) —
		// a per-store filter would have returned only one of them.
		expect(sessions.map((s) => s.projectPath).sort()).toEqual(["/w/root-a", "/w/root-a/nested"]);
		expect(sessions.map((s) => s.localSessionId).sort()).toEqual(["agent.alpha.2", "agent.root.1"]);
	});

	it("returns an empty result when the install has no stores at all", async () => {
		mkdirSync(installRoot(), { recursive: true });
		const { sessions, dedupedCount } = await new IronHermesAdapter().sessions.collect({
			kind: "complete",
		});
		expect(sessions).toEqual([]);
		expect(dedupedCount).toBe(0);
	});
});

describe("IronHermesAdapter.getSessionsWatchPaths — WAL-aware fan-out", () => {
	it("returns the database plus -wal, -shm and -journal for every store, deduplicated", () => {
		seedPooledInstall();
		const paths = new IronHermesAdapter().sessions.watchPaths();

		const stores = [
			join(installRoot(), "state.db"),
			join(installRoot(), "profiles", "alpha", "state.db"),
			join(installRoot(), "profiles", "beta", "state.db"),
		];
		expect(paths).toHaveLength(stores.length * 4);
		expect(new Set(paths).size).toBe(paths.length);
		for (const store of stores) {
			expect(paths).toContain(store);
			expect(paths).toContain(`${store}-wal`);
			// These stores are journal_mode=wal, so `-shm` exists and `-journal`
			// never does. `-shm` was unwatched by the Hermes shape this adapter was
			// copied from; watching a path that does not exist is contract-safe.
			expect(paths).toContain(`${store}-shm`);
			expect(paths).toContain(`${store}-journal`);
		}
	});

	/**
	 * The fan-out is the daemon's file-descriptor bill: `watchSessions` attaches
	 * one `fs.watch` per returned path. Research assumption A6 guessed 6 stores ×
	 * 4 = 24; the live host measures 40 for IronHermes because it carries 10
	 * stores. There is deliberately NO cap anywhere on this path — not in the
	 * adapter, not in `sessions-watcher.ts` — because a cap would silently stop
	 * watching some stores and `clawdi push` would quietly miss their sessions.
	 *
	 * These cases pin that: the count is exactly 4 × store-count at every size,
	 * and a large install is returned whole rather than as a truncated prefix.
	 * A future cap, a fifth sidecar suffix, or a dropped `-shm` all turn one red.
	 */
	const SIDECAR_SUFFIXES = ["", "-wal", "-shm", "-journal"];

	it("returns every store's quartet for a large install, with nothing truncated", () => {
		// 12 profiles + the root store = 13 stores = 52 watch paths, comfortably
		// past the 24 the research assumption budgeted for.
		const profileNames = Array.from({ length: 12 }, (_, i) => `p${String(i).padStart(2, "0")}`);
		makeIronHermesInstall(installRoot(), {
			stores: [
				{ relativePath: "state.db", sessions: [session("agent:root:1", T0)] },
				...profileNames.map((name) => ({
					relativePath: `profiles/${name}/state.db`,
					sessions: [session(`agent:${name}:1`, T0)],
				})),
			],
		});

		const paths = new IronHermesAdapter().sessions.watchPaths();
		const stores = [
			join(installRoot(), "state.db"),
			...profileNames.map((name) => join(installRoot(), "profiles", name, "state.db")),
		];

		expect(paths).toHaveLength(stores.length * SIDECAR_SUFFIXES.length);
		expect(new Set(paths).size).toBe(paths.length);
		// Every store, not just the first N: a truncating cap would keep the
		// prefix and drop `p11`, which is exactly the silent data loss the
		// AgentAdapter contract forbids.
		expect(paths.sort()).toEqual(
			stores.flatMap((store) => SIDECAR_SUFFIXES.map((suffix) => `${store}${suffix}`)).sort(),
		);
	});

	it("grows the fan-out linearly as stores appear, with no cap in between", () => {
		// Re-query after each new store rather than asserting one size, so a cap
		// placed at ANY threshold between 1 and 8 stores shows up as a plateau.
		const observed: number[] = [];
		makeIronHermesInstall(installRoot(), {
			stores: [{ relativePath: "state.db", sessions: [session("agent:root:1", T0)] }],
		});
		observed.push(new IronHermesAdapter().sessions.watchPaths().length);

		for (let i = 1; i < 8; i += 1) {
			makeIronHermesInstall(installRoot(), {
				stores: [
					{
						relativePath: `profiles/p${i}/state.db`,
						sessions: [session(`agent:p${i}:1`, T0)],
					},
				],
			});
			observed.push(new IronHermesAdapter().sessions.watchPaths().length);
		}

		expect(observed).toEqual([4, 8, 12, 16, 20, 24, 28, 32]);
	});

	it("still returns the root store quartet when the agent has never run", () => {
		// The AgentAdapter contract sanctions paths that do not exist yet: the
		// daemon must be able to notice a first-ever `state.db` appearing.
		const root = join(installRoot(), "state.db");
		expect(new IronHermesAdapter().sessions.watchPaths()).toEqual([
			root,
			`${root}-wal`,
			`${root}-shm`,
			`${root}-journal`,
		]);
	});
});
