import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { adapterModuleNames, scanSessionModule } from "../../src/adapters/base";
import { IronHermesAdapter, sanitizeIronHermesSessionId } from "../../src/adapters/ironhermes";
import { getIronHermesHome } from "../../src/adapters/paths";
import { adapterRegistry } from "../../src/adapters/registry";
import {
	type AgentHomeOverrideSnapshot,
	restoreAgentHomeOverrides,
	snapshotAndClearAgentHomeOverrides,
} from "../commands/helpers";
import { makeIronHermesInstall, twoMessages } from "./ironhermes-fixture";

/**
 * The server-side control every emitted `local_session_id` must satisfy.
 *
 * Mirrors `_LOCAL_SESSION_ID_RE` in `backend/app/schemas/session.py`; the `-`
 * is unescaped here only because it is already in trailing position, so the
 * accepted character set is identical. If these two ever diverge, `clawdi push`
 * fails as an opaque 422 with no client-side signal — which is exactly the
 * failure this suite exists to make impossible.
 */
const SERVER_LOCAL_SESSION_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/;

const COMPOSITE_ID = "agent:main:web:dm:0088bd11-4d8a-ae96-d1500ed6cabe";
const COMPOSITE_SANITIZED = "agent.main.web.dm.0088bd11-4d8a-ae96-d1500ed6cabe";
/** Near-collision twin: same 53 leading characters, last hex digit differs. */
const COMPOSITE_TWIN_ID = "agent:main:web:dm:0088bd11-4d8a-ae96-d1500ed6cabf";
const COMPOSITE_TWIN_SANITIZED = "agent.main.web.dm.0088bd11-4d8a-ae96-d1500ed6cabf";
const HOME_PATH = "/Users/twilson";
const NESTED_ROOT = "/Users/twilson/code/ironhermes";
const NESTED_CHILD = "/Users/twilson/code/ironhermes/tmp";
const SIBLING_PATH = "/Users/twilson/tmp/acp-uat-project";

const T0 = 1_770_000_000;

let tmpHome: string;
let origHome: string | undefined;
let origHomeOverrides: AgentHomeOverrideSnapshot = {};

function seedDefaultInstall(): void {
	makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
		stores: [
			{
				relativePath: "state.db",
				sessions: [
					{
						// The composite-id shape that 65% of live rows carry.
						id: COMPOSITE_ID,
						model: "claude-opus-4-7",
						workspaceRoot: HOME_PATH,
						startedAt: T0 + 500,
						endedAt: null,
						messageCount: 2,
						inputTokens: 10,
						outputTokens: 5,
						cacheReadTokens: 2,
						messages: twoMessages(T0 + 500),
					},
					{
						// Deliberate near-collision: identical for 53 characters, differing
						// only in the final hex digit. If the transform ever truncated or
						// collapsed a trailing segment, these two would merge into one
						// server-side row and the later one would silently overwrite the
						// earlier one.
						id: COMPOSITE_TWIN_ID,
						model: "claude-opus-4-7",
						workspaceRoot: NESTED_ROOT,
						startedAt: T0 + 400,
						endedAt: T0 + 460,
						messages: twoMessages(T0 + 400),
					},
					{
						id: "agent:cli:0088bd11",
						model: "claude-opus-4-7",
						workspaceRoot: NESTED_CHILD,
						startedAt: T0 + 300,
						endedAt: null,
						messages: twoMessages(T0 + 300),
					},
					{
						// A sibling of the filter root — must NOT survive a prefix match.
						id: "agent:cli:sibling",
						workspaceRoot: SIBLING_PATH,
						startedAt: T0 + 200,
						messages: twoMessages(T0 + 200),
					},
					{
						// Unattributed. `source` is a channel tag, never a project path.
						id: "agent:telegram:dm:unattributed",
						source: "telegram",
						workspaceRoot: null,
						startedAt: T0 + 100,
						messages: twoMessages(T0 + 100),
					},
				],
			},
		],
	});
}

beforeEach(() => {
	origHome = process.env.HOME;
	origHomeOverrides = snapshotAndClearAgentHomeOverrides();
	tmpHome = mkdtempSync(join(tmpdir(), "clawdi-ironhermes-"));
	process.env.HOME = tmpHome;
});

afterEach(() => {
	if (origHome) process.env.HOME = origHome;
	else delete process.env.HOME;
	restoreAgentHomeOverrides(origHomeOverrides);
	origHomeOverrides = {};
	rmSync(tmpHome, { recursive: true, force: true });
});

describe("getIronHermesHome", () => {
	it("defaults to ~/.ironhermes", () => {
		expect(getIronHermesHome()).toBe(join(tmpHome, ".ironhermes"));
	});

	it("honors $IRONHERMES_HOME exactly as $HERMES_HOME overrides ~/.hermes", () => {
		const override = join(tmpHome, "elsewhere", "ironhermes-root");
		process.env.IRONHERMES_HOME = override;
		try {
			expect(getIronHermesHome()).toBe(override);
		} finally {
			delete process.env.IRONHERMES_HOME;
		}
	});

	it("resolves lazily, so a HOME change between calls is observed", () => {
		const first = getIronHermesHome();
		process.env.HOME = join(tmpHome, "second-home");
		expect(getIronHermesHome()).not.toBe(first);
	});
});

describe("IronHermesAdapter.detect", () => {
	it("returns true for an install whose root state.db has a sessions table", async () => {
		seedDefaultInstall();
		expect(await new IronHermesAdapter().detect()).toBe(true);
	});

	it("returns false for a bare ~/.ironhermes directory with no store", async () => {
		mkdirSync(join(tmpHome, ".ironhermes"), { recursive: true });
		expect(await new IronHermesAdapter().detect()).toBe(false);
	});

	it("returns false for a 0-byte state.db rather than throwing", async () => {
		// `sqlite3 <path>` creates this by merely opening the path once.
		// `existsSync` passes; `prepare()` would throw `no such table: sessions`.
		makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
			stores: [{ relativePath: "state.db", sessions: [], empty: true }],
		});
		expect(await new IronHermesAdapter().detect()).toBe(false);
	});

	it("returns false for a real SQLite file with no sessions table", async () => {
		makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
			stores: [{ relativePath: "state.db", sessions: [], omitSessionsTable: true }],
		});
		expect(await new IronHermesAdapter().detect()).toBe(false);
	});

	it("returns false when ~/.ironhermes does not exist at all", async () => {
		expect(await new IronHermesAdapter().detect()).toBe(false);
	});
});

describe("sanitizeIronHermesSessionId", () => {
	it("substitutes every colon with a period, globally", () => {
		expect(sanitizeIronHermesSessionId(COMPOSITE_ID)).toBe(COMPOSITE_SANITIZED);
	});

	it("leaves an id with no colon untouched", () => {
		expect(sanitizeIronHermesSessionId("agent.main")).toBe("agent.main");
	});

	it("keeps the near-collision twin distinct", () => {
		expect(sanitizeIronHermesSessionId(COMPOSITE_ID)).not.toBe(
			sanitizeIronHermesSessionId(COMPOSITE_TWIN_ID),
		);
	});

	it("documents the one input class the transform cannot separate", () => {
		// Colon → period is injective only over ids that contain no literal
		// period. Two raw ids differing ONLY at a colon-versus-period position
		// map to the same output, and the later one would overwrite the earlier
		// one server-side.
		//
		// This is asserted rather than hidden. It is reachable only if
		// IronHermes starts emitting periods inside session ids: every id in
		// every live store is `<segment>:<segment>…:<uuid>`, where the segments
		// are lowercase words and the uuid is hex plus hyphens. The raw id is
		// preserved verbatim on `rawFilePath`, so the mapping stays diagnosable
		// if that assumption ever breaks.
		expect(sanitizeIronHermesSessionId("agent:main")).toBe(
			sanitizeIronHermesSessionId("agent.main"),
		);
	});
});

describe("IronHermesAdapter.collectSessions — session id conformance", () => {
	it("emits ids that all match the server's local_session_id pattern", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		expect(sessions.length).toBeGreaterThan(0);
		for (const s of sessions) {
			expect(s.localSessionId).toMatch(SERVER_LOCAL_SESSION_ID_RE);
		}
	});

	it("sanitizes the composite agent:main:web:dm: id to its dotted form", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		const ids = sessions.map((s) => s.localSessionId);
		expect(ids).toContain(COMPOSITE_SANITIZED);
		// The raw form would have been rejected outright by the server.
		expect(COMPOSITE_ID).not.toMatch(SERVER_LOCAL_SESSION_ID_RE);
	});

	it("is collision-free: N distinct raw ids yield N distinct sanitized ids", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		const sanitized = new Set(sessions.map((s) => s.localSessionId));
		// `rawFilePath` carries the untransformed id, so the raw count is derivable
		// from the same result rather than from the fixture spec.
		const raw = new Set(sessions.map((s) => s.rawFilePath));
		expect(sanitized.size).toBe(raw.size);
		expect(sanitized.size).toBe(sessions.length);
	});

	it("keeps the raw id on rawFilePath so the sanitized id stays reversible", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		const composite = sessions.find((s) => s.rawFilePath.endsWith(`#${COMPOSITE_ID}`));
		expect(composite).toBeDefined();
		expect(composite?.rawFilePath).toBe(
			`${join(tmpHome, ".ironhermes", "state.db")}#${COMPOSITE_ID}`,
		);
		// The pointer is anchored at the row, not at the whole store.
		expect(composite?.rawFilePath).toContain("state.db#");
	});
});

describe("IronHermesAdapter.collectSessions — project attribution", () => {
	it("passes workspace_root through verbatim, bare home directory included", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		const composite = sessions.find((s) => s.localSessionId === COMPOSITE_SANITIZED);
		expect(composite?.projectPath).toBe(HOME_PATH);
	});

	it("yields projectPath null for a NULL workspace_root, never the source tag", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		const unattributed = sessions.find(
			(s) => s.localSessionId === "agent.telegram.dm.unattributed",
		);
		expect(unattributed).toBeDefined();
		expect(unattributed?.projectPath).toBeNull();
		// The row's `source` is "telegram"; it must not have leaked into the path.
		expect(unattributed?.projectPath).not.toBe("telegram");
	});

	it("never rewrites the home directory into a `~` or filters it out", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		for (const s of sessions) {
			if (s.projectPath !== null) expect(s.projectPath.startsWith("~")).toBe(false);
		}
	});
});

describe("IronHermesAdapter.collectSessions — projectFilter", () => {
	it("keeps the exact match and the nested child, drops sibling and null paths", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({
			kind: "complete",
			projectFilter: NESTED_ROOT,
		});
		const paths = sessions.map((s) => s.projectPath).sort();
		expect(paths).toEqual([NESTED_ROOT, NESTED_CHILD].sort());
		expect(paths).not.toContain(SIBLING_PATH);
		expect(paths).not.toContain(null);
	});

	it("drops every null-projectPath session when a filter is supplied", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({
			kind: "complete",
			projectFilter: HOME_PATH,
		});
		for (const s of sessions) expect(s.projectPath).not.toBeNull();
	});

	it("returns every session, attributed or not, when no filter is supplied", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		expect(sessions).toHaveLength(5);
		expect(sessions.filter((s) => s.projectPath === null)).toHaveLength(1);
	});
});

describe("IronHermesAdapter.collectSessions — duration", () => {
	it("leaves endedAt and durationSeconds null when ended_at is NULL", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		const unrecorded = sessions.find(
			(s) => s.localSessionId === "agent.main.web.dm.0088bd11-4d8a-ae96-d1500ed6cabe",
		);
		expect(unrecorded?.endedAt).toBeNull();
		// No derivation from the maximum message timestamp: an unrecorded
		// duration is omitted, never inferred.
		expect(unrecorded?.durationSeconds).toBeNull();
	});

	it("reports a real duration when ended_at IS recorded", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		const recorded = sessions.find((s) => s.localSessionId === COMPOSITE_TWIN_SANITIZED);
		expect(recorded?.endedAt).toEqual(new Date((T0 + 460) * 1000));
		expect(recorded?.durationSeconds).toBe(60);
	});
});

describe("IronHermesAdapter.collectSessions — carried-through observables", () => {
	it("carries model, token counters and messages from the row", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		const composite = sessions.find((s) => s.localSessionId === COMPOSITE_SANITIZED);
		expect(composite).toMatchObject({
			model: "claude-opus-4-7",
			modelsUsed: ["claude-opus-4-7"],
			messageCount: 2,
			inputTokens: 10,
			outputTokens: 5,
			cacheReadTokens: 2,
		});
		expect(composite?.startedAt).toEqual(new Date((T0 + 500) * 1000));
		expect(composite?.messages).toHaveLength(2);
		expect(composite?.messages[1]?.model).toBe("claude-opus-4-7");
	});

	it("extracts a plain model name from a JSON model blob carrying provider secrets", async () => {
		makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
			stores: [
				{
					relativePath: "state.db",
					sessions: [
						{
							id: "agent:blob",
							model:
								'{"default": "gpt-5.3-codex", "provider": "openai-codex",' +
								' "headers": {"x-api-key": "sk-redacted"}}',
							startedAt: T0,
							messages: twoMessages(T0),
						},
					],
				},
			],
		});
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		expect(sessions[0]?.model).toBe("gpt-5.3-codex");
		expect(sessions[0]?.modelsUsed).toEqual(["gpt-5.3-codex"]);
		expect(JSON.stringify(sessions[0])).not.toContain("sk-redacted");
	});

	it("falls back to the first user message for summary when title is NULL", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		for (const s of sessions) expect(s.summary).toBe("hello");
	});

	it("orders sessions by started_at DESC", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		expect(sessions.map((s) => s.localSessionId)).toEqual([
			COMPOSITE_SANITIZED,
			COMPOSITE_TWIN_SANITIZED,
			"agent.cli.0088bd11",
			"agent.cli.sibling",
			"agent.telegram.dm.unattributed",
		]);
	});

	it("returns an empty result when the store is absent", async () => {
		const { sessions, dedupedCount } = await new IronHermesAdapter().sessions.collect({
			kind: "complete",
		});
		expect(sessions).toEqual([]);
		expect(dedupedCount).toBe(0);
	});
});

describe("IronHermesAdapter — #1114 session-scan contract", () => {
	it("reports complete coverage when every store reads", async () => {
		seedDefaultInstall();
		const result = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		expect(result.coverage).toBe("complete");
		expect(result.dedupedCount).toBe(0);
	});

	it("scans every store for a paths request, since a SQLite store has no per-row paths", async () => {
		seedDefaultInstall();
		const statePath = join(tmpHome, ".ironhermes", "state.db");
		const byPaths = await new IronHermesAdapter().sessions.collect({
			kind: "paths",
			paths: [`${statePath}-wal`],
		});
		const complete = await new IronHermesAdapter().sessions.collect({ kind: "complete" });
		expect(byPaths.coverage).toBe("complete");
		expect(byPaths.sessions.map((s) => s.localSessionId)).toEqual(
			complete.sessions.map((s) => s.localSessionId),
		);
	});

	it("honors projectFilter on a paths request too", async () => {
		seedDefaultInstall();
		const { sessions } = await new IronHermesAdapter().sessions.collect({
			kind: "paths",
			paths: [join(tmpHome, ".ironhermes", "state.db")],
			projectFilter: NESTED_ROOT,
		});
		expect(sessions.map((s) => s.projectPath).sort()).toEqual([NESTED_ROOT, NESTED_CHILD].sort());
	});

	it("resolves a session by its SANITIZED wire id to the same session a scan emits", async () => {
		seedDefaultInstall();
		const adapter = new IronHermesAdapter();
		const resolved = await adapter.sessions.resolve(COMPOSITE_SANITIZED);
		const { sessions } = await adapter.sessions.collect({ kind: "complete" });
		expect(resolved).not.toBeNull();
		expect(resolved).toEqual(sessions.find((s) => s.localSessionId === COMPOSITE_SANITIZED));
		// The raw id stays on the pointer back into the store.
		expect(resolved?.rawFilePath.endsWith(`#${COMPOSITE_ID}`)).toBe(true);
	});

	it("resolves the near-collision twin to itself, not to its neighbour", async () => {
		seedDefaultInstall();
		const twin = await new IronHermesAdapter().sessions.resolve(COMPOSITE_TWIN_SANITIZED);
		expect(twin?.localSessionId).toBe(COMPOSITE_TWIN_SANITIZED);
		expect(twin?.rawFilePath.endsWith(`#${COMPOSITE_TWIN_ID}`)).toBe(true);
	});

	it("returns null for an id no store holds, and for an install with no store", async () => {
		expect(await new IronHermesAdapter().sessions.resolve(COMPOSITE_SANITIZED)).toBeNull();
		seedDefaultInstall();
		expect(await new IronHermesAdapter().sessions.resolve("agent.main.absent")).toBeNull();
	});

	it("keeps the read-root isolation guard on resolveSession", async () => {
		seedDefaultInstall();
		// Point IronHermes' root inside the Hermes home: the read must refuse.
		const hermesHome = join(tmpHome, ".hermes");
		mkdirSync(join(hermesHome, "nested"), { recursive: true });
		const origIron = process.env.IRONHERMES_HOME;
		const origHermes = process.env.HERMES_HOME;
		process.env.HERMES_HOME = hermesHome;
		process.env.IRONHERMES_HOME = join(hermesHome, "nested");
		try {
			await expect(new IronHermesAdapter().sessions.resolve(COMPOSITE_SANITIZED)).rejects.toThrow(
				hermesHome,
			);
		} finally {
			if (origIron === undefined) delete process.env.IRONHERMES_HOME;
			else process.env.IRONHERMES_HOME = origIron;
			if (origHermes === undefined) delete process.env.HERMES_HOME;
			else process.env.HERMES_HOME = origHermes;
		}
	});
});

describe("IronHermesAdapter — identity and isolation", () => {
	it("declares the ironhermes agent type", () => {
		expect(new IronHermesAdapter().agentType).toBe("ironhermes");
	});

	it("roots every path at the IronHermes home, never at the Hermes home", () => {
		const a = new IronHermesAdapter();
		expect(a.skills.rootDir()).toBe(join(tmpHome, ".ironhermes", "skills"));
		expect(a.skills.path("foo")).toBe(join(tmpHome, ".ironhermes", "skills", "foo", "SKILL.md"));
		for (const p of a.sessions.watchPaths()) {
			expect(p).toContain(join(tmpHome, ".ironhermes"));
			expect(p).not.toContain(join(tmpHome, ".hermes"));
		}
	});

	it("builds run commands with the ironhermes binary", () => {
		expect(new IronHermesAdapter().buildRunCommand(["hello"], {})).toEqual(["ironhermes", "hello"]);
	});
});

describe("IronHermesAdapter — #1229 modular contract", () => {
	/** `count` sessions, each with two messages and one recorded call. */
	function seedMany(count: number, costFor: (index: number) => number = () => 1_000): void {
		makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
			stores: [
				{
					relativePath: "state.db",
					sessions: Array.from({ length: count }, (_, index) => ({
						id: `agent:cli:many-${String(index).padStart(3, "0")}`,
						model: "claude-opus-4-7",
						workspaceRoot: HOME_PATH,
						startedAt: T0 + index,
						messages: twoMessages(T0 + index),
						usageEvents: [
							{
								ts: (T0 + index) * 1000,
								provider: "anthropic",
								model: "claude-opus-4-7",
								inTok: 10,
								outTok: 2,
								cacheRead: 0,
								cacheCreate: 0,
								costUsdMicros: costFor(index),
								errorKind: null,
							},
						],
					})),
				},
			],
		});
	}

	async function drain(scan: Awaited<ReturnType<typeof scanSessionModule>>) {
		const batches = [];
		for await (const batch of scan.batches) batches.push(batch);
		return batches;
	}

	it("registers a session module and a skill module on the snapshot protocol", async () => {
		const adapter = new IronHermesAdapter();
		expect(adapterModuleNames(adapter)).toEqual(["sessions", "skills"]);
		expect(await adapter.sessions.contentProtocol()).toBe("snapshot-v1");
		expect(adapterRegistry.ironhermes.create().agentType).toBe("ironhermes");
	});

	it("pages the pooled store into batches that together equal collect()", async () => {
		seedMany(40);
		const adapter = new IronHermesAdapter();
		const collected = await adapter.sessions.collect({ kind: "complete" });
		const scan = await scanSessionModule(adapter.sessions, { kind: "complete" });
		const batches = await drain(scan);

		expect(scan.coverage).toBe("complete");
		expect(batches.length).toBeGreaterThan(1);
		for (const batch of batches) expect(batch.sessions.length).toBeLessThanOrEqual(32);
		const scanned = batches.flatMap((batch) => batch.sessions.map((s) => s.localSessionId));
		expect(scanned).toHaveLength(40);
		expect(scanned).toEqual(collected.sessions.map((s) => s.localSessionId));
		// Observed ids are the wire ids, so the daemon's health bookkeeping and
		// its known-revision map key on the same value the server stores.
		const observed = batches.flatMap((batch) => [...batch.observedLocalSessionIds]);
		expect(observed).toEqual(scanned);
		for (const id of observed) expect(id).toMatch(SERVER_LOCAL_SESSION_ID_RE);
		for (const session of collected.sessions)
			expect(session.sourceRevision).toMatch(/^[0-9a-f]{64}$/);
	});

	it("skips sessions whose confirmed revision is unchanged, and still observes them", async () => {
		seedMany(3);
		const adapter = new IronHermesAdapter();
		const first = await adapter.sessions.collect({ kind: "complete" });
		const known = new Map(
			first.sessions.map((s) => [s.localSessionId, s.sourceRevision as string] as const),
		);

		const batches = await drain(
			await scanSessionModule(adapter.sessions, { kind: "complete" }, known),
		);
		expect(batches.flatMap((batch) => batch.sessions)).toHaveLength(0);
		expect(batches.flatMap((batch) => [...batch.observedLocalSessionIds]).sort()).toEqual(
			[...known.keys()].sort(),
		);
	});

	it("gives an event-only change a new revision, so the incremental scan never skips it", async () => {
		seedMany(3);
		const adapter = new IronHermesAdapter();
		const first = await adapter.sessions.collect({ kind: "complete" });
		const known = new Map(
			first.sessions.map((s) => [s.localSessionId, s.sourceRevision as string] as const),
		);

		// Identical messages; only session 1's recorded cost moved (phase 02 D-22).
		rmSync(join(tmpHome, ".ironhermes"), { recursive: true, force: true });
		seedMany(3, (index) => (index === 1 ? 999_999 : 1_000));

		const changed = (
			await drain(await scanSessionModule(adapter.sessions, { kind: "complete" }, known))
		).flatMap((batch) => batch.sessions);
		expect(changed.map((s) => s.localSessionId)).toEqual(["agent.cli.many-001"]);
		expect(changed[0]?.usageEvents?.[0]?.costUsdMicros).toBe(999_999);
		expect(changed[0]?.sourceRevision).not.toBe(known.get("agent.cli.many-001"));
	});
});
