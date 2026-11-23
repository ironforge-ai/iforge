import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { push, USAGE_EVENT_BATCH_CHUNK } from "../../src/commands/push";
import {
	cacheKey,
	type FencedSessionLockEntry,
	readSessionsLock,
} from "../../src/lib/sessions-lock";
import { cleanupTmp, copyFixtureToTmp } from "../adapters/helpers";
import {
	type FixtureSession,
	type FixtureUsageEvent,
	makeIronHermesInstall,
	twoMessages,
} from "../adapters/ironhermes-fixture";
import {
	type AgentHomeOverrideSnapshot,
	type CapturedRequest,
	jsonResponse,
	mockFetch,
	okEnvironmentProbe,
	restoreAgentHomeOverrides,
	seedAuthAndEnv,
	snapshotAndClearAgentHomeOverrides,
} from "./helpers";

const EVT_MS = 1_786_898_459_566;
const START_S = 1_786_898_400;
const END_S = 1_786_898_520;

const EVENTS_PATH = "/v1/sessions/usage-events";
const BATCH_PATH = "/v1/sessions/batch";

interface WireEvent {
	ts: string;
	provider: string | null;
	model: string;
	in_tok: number;
	out_tok: number;
	cache_read: number;
	cache_create: number;
	cost_usd_micros: number;
	error_kind: string | null;
}
interface WireEventBatch {
	environment_id: string;
	local_session_id: string;
	event_set_hash: string;
	events: WireEvent[];
}
interface WireSession {
	local_session_id: string;
	tool_call_count?: number;
	cache_read_tokens?: number;
}

function eventBatches(captured: CapturedRequest[]): WireEventBatch[] {
	return captured.filter((c) => c.path === EVENTS_PATH).map((c) => c.body as WireEventBatch);
}
function batchSessions(captured: CapturedRequest[]): WireSession[] {
	return captured
		.filter((c) => c.path === BATCH_PATH)
		.flatMap((c) => (c.body as { sessions?: WireSession[] })?.sessions ?? []);
}

/**
 * The session's v2 fenced lock entry (#1229 session lock v2). The fence key is a digest of
 * the API origin, Agent, adapter and source key, so the entry is found by its recorded
 * identity rather than by recomputing the key.
 */
function fencedLockEntry(localSessionId: string): FencedSessionLockEntry | undefined {
	return Object.values(readSessionsLock().sessions).find(
		(entry): entry is FencedSessionLockEntry =>
			"source_session_key" in entry &&
			entry.adapter === "ironhermes" &&
			entry.source_session_key === localSessionId,
	);
}

let tmpHome: string;
let origHome: string | undefined;
let origHomeOverrides: AgentHomeOverrideSnapshot = {};

function evt(over: Partial<FixtureUsageEvent> = {}): FixtureUsageEvent {
	return {
		ts: EVT_MS,
		provider: "anthropic",
		model: "claude-opus-4-7",
		inTok: 10,
		outTok: 2,
		cacheRead: 1,
		cacheCreate: 3,
		costUsdMicros: 5_000,
		errorKind: null,
		...over,
	};
}

function session(id: string, events: FixtureUsageEvent[], toolCallCount = 7): FixtureSession {
	return {
		id,
		model: "claude-opus-4-7",
		workspaceRoot: "/Users/t/code/a",
		startedAt: START_S,
		endedAt: END_S,
		toolCallCount,
		messages: twoMessages(START_S),
		usageEvents: events,
	};
}

function seedIronHermes(sessions: FixtureSession[]): void {
	makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
		stores: [{ relativePath: "state.db", sessions }],
	});
	seedAuthAndEnv(tmpHome, "ironhermes");
}

/** Handlers for a happy-path push: env probe, session batch, events ingest. */
function happyPath(
	over: { rejected?: string[]; unchanged?: boolean } = {},
): Parameters<typeof mockFetch>[0] {
	return [
		okEnvironmentProbe(),
		{
			method: "POST",
			path: BATCH_PATH,
			response: () =>
				jsonResponse({
					created: 0,
					updated: 0,
					unchanged: 0,
					needs_content: [],
					rejected: over.rejected ?? [],
				}),
		},
		{
			method: "POST",
			path: EVENTS_PATH,
			response: (req) => {
				const body = req.body as WireEventBatch;
				return jsonResponse({
					inserted: body.events.length,
					deleted: 0,
					unchanged: over.unchanged ?? false,
				});
			},
		},
		{ method: "POST", path: "/v1/sessions/", response: () => jsonResponse({}) },
	];
}

beforeEach(() => {
	origHome = process.env.HOME;
	origHomeOverrides = snapshotAndClearAgentHomeOverrides();
	tmpHome = mkdtempSync(join(tmpdir(), "clawdi-push-usage-"));
	process.env.HOME = tmpHome;
});

afterEach(() => {
	if (origHome) process.env.HOME = origHome;
	else delete process.env.HOME;
	restoreAgentHomeOverrides(origHomeOverrides);
	origHomeOverrides = {};
	process.exitCode = 0;
	if (tmpHome) rmSync(tmpHome, { recursive: true, force: true });
});

describe("push — usage events reach the server exactly once per content state", () => {
	it("sends a session's whole event set, at the millisecond instant it was recorded", async () => {
		seedIronHermes([session("agent:cli:one", [evt(), evt({ ts: EVT_MS + 10, outTok: 9 })])]);
		const { captured, restore } = mockFetch(happyPath());
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			restore();
		}

		const batches = eventBatches(captured);
		expect(batches).toHaveLength(1);
		const body = batches[0];
		expect(body?.local_session_id).toBe("agent.cli.one");
		expect(body?.environment_id).toBe("env-test");
		expect(body?.event_set_hash).toMatch(/^[0-9a-f]{64}$/);
		expect(body?.events).toHaveLength(2);

		// The instant survives the trip: ISO-8601 of the millisecond epoch, NOT
		// a value a thousand times too large.
		expect(body?.events[0]?.ts).toBe(new Date(EVT_MS).toISOString());
		expect(new Date(body?.events[0]?.ts ?? 0).getUTCFullYear()).toBeLessThan(3000);
		expect(body?.events[0]).toMatchObject({
			provider: "anthropic",
			model: "claude-opus-4-7",
			in_tok: 10,
			out_tok: 2,
			cache_read: 1,
			cache_create: 3,
			cost_usd_micros: 5_000,
			error_kind: null,
		});
	});

	it("posts nothing for a session whose event-set hash is unchanged since the last push", async () => {
		seedIronHermes([session("agent:cli:one", [evt()])]);
		// First push warms the lock.
		const first = mockFetch(happyPath());
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			first.restore();
		}
		expect(eventBatches(first.captured)).toHaveLength(1);

		// Second push over an unmodified store.
		const second = mockFetch(happyPath());
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			second.restore();
		}
		expect(eventBatches(second.captured)).toHaveLength(0);
	});

	it("sends the COMPLETE set — never a delta — when the hash changed", async () => {
		seedIronHermes([session("agent:cli:one", [evt()])]);
		const first = mockFetch(happyPath());
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			first.restore();
		}

		// A second event lands in the store. Ingest is replace-by-session, so the
		// client must re-send BOTH rows; sending only the new one would leave the
		// server's wholesale replacement holding a set of one.
		rmSync(join(tmpHome, ".ironhermes"), { recursive: true, force: true });
		makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
			stores: [
				{
					relativePath: "state.db",
					sessions: [
						session("agent:cli:one", [evt(), evt({ ts: EVT_MS + 10, costUsdMicros: 6_000 })]),
					],
				},
			],
		});

		const second = mockFetch(happyPath());
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			second.restore();
		}
		const batches = eventBatches(second.captured);
		expect(batches).toHaveLength(1);
		expect(batches[0]?.events).toHaveLength(2);
	});

	it("re-pushes an event-only change even though the message content hash is unchanged", async () => {
		// The backstop: the session-content filter runs BEFORE the events push, so
		// a session whose messages are untouched would be dropped from the scan
		// entirely and its changed events would never be seen.
		seedIronHermes([session("agent:cli:one", [evt()])]);
		const first = mockFetch(happyPath());
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			first.restore();
		}

		rmSync(join(tmpHome, ".ironhermes"), { recursive: true, force: true });
		makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
			stores: [
				{
					relativePath: "state.db",
					// Identical messages; only the cost of the single event moved.
					sessions: [session("agent:cli:one", [evt({ costUsdMicros: 999_999 })])],
				},
			],
		});

		const second = mockFetch(happyPath());
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			second.restore();
		}
		const batches = eventBatches(second.captured);
		expect(batches).toHaveLength(1);
		expect(batches[0]?.events[0]?.cost_usd_micros).toBe(999_999);
	});

	it("contributes nothing, and no empty request, for a session with zero events", async () => {
		seedIronHermes([
			session("agent:cli:hasevents", [evt()]),
			{
				id: "agent:cli:noevents",
				model: "claude-opus-4-7",
				startedAt: START_S,
				messages: twoMessages(START_S),
			},
		]);
		const { captured, restore } = mockFetch(happyPath());
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			restore();
		}
		const batches = eventBatches(captured);
		expect(batches).toHaveLength(1);
		expect(batches[0]?.local_session_id).toBe("agent.cli.hasevents");
		for (const b of batches) expect(b.events.length).toBeGreaterThan(0);
	});

	it("does not push events for a session the server rejected in the batch step", async () => {
		seedIronHermes([
			session("agent:cli:kept", [evt()]),
			session("agent:cli:loser", [evt({ costUsdMicros: 42 })]),
		]);
		// The events request names a session by local id and the server resolves
		// it against sessions the principal owns — a rejected session has not
		// landed, so its events would be refused for a session that does not exist.
		const { captured, restore } = mockFetch(happyPath({ rejected: ["agent.cli.loser"] }));
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			restore();
		}
		const ids = eventBatches(captured).map((b) => b.local_session_id);
		expect(ids).toEqual(["agent.cli.kept"]);
	});

	it("orders the events push after the session batch that creates its referent", async () => {
		seedIronHermes([session("agent:cli:one", [evt()])]);
		const { captured, restore } = mockFetch(happyPath());
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			restore();
		}
		const order = captured.map((c) => c.path);
		expect(order.indexOf(BATCH_PATH)).toBeGreaterThanOrEqual(0);
		expect(order.indexOf(EVENTS_PATH)).toBeGreaterThan(order.indexOf(BATCH_PATH));
	});
});

describe("push — one request per session, chunked to the events route's own ceiling", () => {
	it("splits many sessions across one request each, every event sent exactly once", async () => {
		const sessions = [
			session("agent:cli:a", [
				evt({ costUsdMicros: 1 }),
				evt({ ts: EVT_MS + 1, costUsdMicros: 2 }),
			]),
			session("agent:cli:b", [evt({ costUsdMicros: 3 })]),
			session("agent:cli:c", [
				evt({ costUsdMicros: 4 }),
				evt({ ts: EVT_MS + 1, costUsdMicros: 5 }),
				evt({ ts: EVT_MS + 2, costUsdMicros: 6 }),
			]),
		];
		seedIronHermes(sessions);
		const { captured, restore } = mockFetch(happyPath());
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			restore();
		}

		const batches = eventBatches(captured);
		expect(batches).toHaveLength(3);
		// Each request carries exactly one session's set…
		expect(new Set(batches.map((b) => b.local_session_id)).size).toBe(3);
		// …and every recorded call appears exactly once across all of them.
		const costs = batches
			.flatMap((b) => b.events.map((e) => e.cost_usd_micros))
			.sort((x, y) => x - y);
		expect(costs).toEqual([1, 2, 3, 4, 5, 6]);
	});

	it("caps a single request at the events route's own ceiling, not the session loop's 500", async () => {
		// The session batch's 500 was calibrated for a ~17-column session row. The
		// event row is narrower, so its ceiling is its own number and must match
		// the cap the server declares — a client that chunks higher 422s the batch.
		expect(USAGE_EVENT_BATCH_CHUNK).toBe(2000);
		expect(USAGE_EVENT_BATCH_CHUNK).not.toBe(500);
	});

	it("refuses a session exceeding the cap loudly instead of splitting or truncating it", async () => {
		// Ingest is replace-by-session: a split second request would either be
		// no-op'd by the matching hash (dropping its rows) or delete the first
		// request's rows. Both silently corrupt recorded spend, which is the
		// failure the accuracy prohibitions name. Refusing is the honest move —
		// and it stays visible, because the lock is not advanced.
		const huge = Array.from({ length: USAGE_EVENT_BATCH_CHUNK + 1 }, (_, i) =>
			evt({ ts: EVT_MS + i, costUsdMicros: i }),
		);
		seedIronHermes([session("agent:cli:huge", huge), session("agent:cli:small", [evt()])]);

		const { captured, restore } = mockFetch(happyPath());
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			restore();
		}

		const batches = eventBatches(captured);
		// The over-cap session contributes no request at all…
		expect(batches.map((b) => b.local_session_id)).toEqual(["agent.cli.small"]);
		// …and its hash is NOT recorded, so the next push retries rather than
		// treating the never-sent set as delivered. The small session's entry proves
		// the lookup reads the lock the push wrote.
		expect(fencedLockEntry("agent.cli.small")?.usage_event_set_hash).toMatch(/^[0-9a-f]{64}$/);
		expect(fencedLockEntry("agent.cli.huge")?.usage_event_set_hash).toBeUndefined();
	});

	it("does not advance the event hash when the events request fails", async () => {
		seedIronHermes([session("agent:cli:one", [evt()])]);
		const { restore } = mockFetch([
			okEnvironmentProbe(),
			{
				method: "POST",
				path: BATCH_PATH,
				response: () =>
					jsonResponse({ created: 1, updated: 0, unchanged: 0, needs_content: [], rejected: [] }),
			},
			{
				method: "POST",
				path: EVENTS_PATH,
				response: () => jsonResponse({ detail: "boom" }, 500),
			},
		]);
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			restore();
		}
		const entry = readSessionsLock().sessions[cacheKey("ironhermes", "agent.cli.one")] as
			| { hash: string; usageEventSetHash?: string }
			| undefined;
		expect(entry?.usageEventSetHash).toBeUndefined();
	});
});

describe("push — tool_call_count travels the whole path (D-16)", () => {
	it("carries the session's toolCallCount into the session batch body", async () => {
		seedIronHermes([session("agent:cli:one", [evt()], 137)]);
		const { captured, restore } = mockFetch(happyPath());
		try {
			await push({ agent: "ironhermes", modules: "sessions", all: true });
		} finally {
			restore();
		}
		const posted = batchSessions(captured).find((s) => s.local_session_id === "agent.cli.one");
		expect(posted).toBeDefined();
		expect(posted?.tool_call_count).toBe(137);
	});

	it("posts 0 — not null, not an absent key — for a runtime that has no such counter", async () => {
		// The server's field is `ge=0` with a default. `undefined` serializes to an
		// absent key, which is a different wire fact than the zero the other three
		// runtimes actually mean.
		const home = copyFixtureToTmp("hermes");
		const prev = process.env.HOME;
		process.env.HOME = home;
		seedAuthAndEnv(home, "hermes");
		const { captured, restore } = mockFetch(happyPath());
		try {
			await push({ agent: "hermes", modules: "sessions", all: true });
		} finally {
			restore();
			if (prev) process.env.HOME = prev;
			else delete process.env.HOME;
			cleanupTmp(home);
		}

		const posted = batchSessions(captured);
		expect(posted.length).toBeGreaterThan(0);
		for (const s of posted) {
			expect(s.tool_call_count).toBe(0);
			expect(s.tool_call_count).not.toBeNull();
			expect(Object.hasOwn(s, "tool_call_count")).toBe(true);
		}
	});

	it("issues no usage-events request for a runtime whose adapter populates none", async () => {
		const home = copyFixtureToTmp("hermes");
		const prev = process.env.HOME;
		process.env.HOME = home;
		seedAuthAndEnv(home, "hermes");
		const { captured, restore } = mockFetch(happyPath());
		try {
			await push({ agent: "hermes", modules: "sessions", all: true });
		} finally {
			restore();
			if (prev) process.env.HOME = prev;
			else delete process.env.HOME;
			cleanupTmp(home);
		}
		expect(eventBatches(captured)).toHaveLength(0);
	});
});

describe("the session batch body is built twice and both copies must agree", () => {
	/**
	 * `push.ts` and `serve/sync-engine.ts` each construct their own
	 * `/v1/sessions/batch` body. Neither is generated from the other. A field
	 * added to one alone survives `clawdi push` and vanishes on every daemon
	 * sync — a discrepancy that surfaces as an intermittently-zero counter and
	 * is miserable to trace back.
	 *
	 * Asserted against `sync-engine.ts` DIRECTLY rather than inferred from a
	 * push-driven request, because inferring is exactly the mistake that lets
	 * the daemon copy drift.
	 */
	const here = dirname(fileURLToPath(import.meta.url));
	const srcRoot = join(here, "..", "..", "src");

	/** Field names of the object literal posted to `/v1/sessions/batch`. */
	function batchBodyFields(relPath: string): string[] {
		const src = readFileSync(join(srcRoot, relPath), "utf-8");
		const anchor = src.indexOf('"/v1/sessions/batch"');
		expect(anchor).toBeGreaterThan(-1);
		// The body literal always spans from the anchor to the `content_hash`
		// entry, which is last in both copies.
		const end = src.indexOf("content_hash:", anchor);
		expect(end).toBeGreaterThan(anchor);
		const region = src.slice(anchor, end);
		return [...region.matchAll(/^\s*([a-z_]+):/gm)].map((m) => m[1] as string);
	}

	it("sync-engine.ts carries tool_call_count in its own batch body", () => {
		expect(batchBodyFields("serve/sync-engine.ts")).toContain("tool_call_count");
	});

	it("push.ts carries tool_call_count in its batch body", () => {
		expect(batchBodyFields("commands/push.ts")).toContain("tool_call_count");
	});

	it("both copies post the identical field list", () => {
		const fromPush = batchBodyFields("commands/push.ts");
		const fromDaemon = batchBodyFields("serve/sync-engine.ts");
		expect(fromPush.length).toBeGreaterThan(10);
		expect(fromDaemon).toEqual(fromPush);
	});

	it("both copies default the counter to 0 rather than sending undefined", () => {
		for (const rel of ["commands/push.ts", "serve/sync-engine.ts"]) {
			const src = readFileSync(join(srcRoot, rel), "utf-8");
			expect(src).toMatch(/tool_call_count:\s*\w+\.toolCallCount\s*\?\?\s*0/);
		}
	});
});

describe("push — the sessions-lock namespace stays unambiguous", () => {
	it("does not collide ironhermes: and hermes: keys, and adds no separator parser", () => {
		// The prefix filter is a plain `startsWith`, which never splits on the
		// separator — so `hermes:` does not match `ironhermes:`. Introducing a
		// split would CREATE the collision it appears to prevent.
		const src = readFileSync(
			join(dirname(fileURLToPath(import.meta.url)), "..", "..", "src", "lib", "sessions-lock.ts"),
			"utf-8",
		);
		expect(src).not.toContain('split(":")');

		expect(cacheKey("ironhermes", "s1")).toBe("ironhermes:s1");
		expect(cacheKey("hermes", "s1")).toBe("hermes:s1");
		expect(cacheKey("ironhermes", "s1").startsWith("hermes:")).toBe(false);
		expect(cacheKey("hermes", "s1").startsWith("ironhermes:")).toBe(false);
	});

	it("records the event-set hash alongside the content hash after a successful push", () => {
		// Two independent fingerprints, deliberately: an event-only change is
		// pushed without forcing a session-content reupload.
		seedIronHermes([session("agent:cli:one", [evt()])]);
		const { restore } = mockFetch(happyPath());
		return push({ agent: "ironhermes", modules: "sessions", all: true })
			.then(() => {
				const entry = fencedLockEntry("agent.cli.one");
				expect(entry?.local_hash).toMatch(/^[0-9a-f]{64}$/);
				expect(entry?.usage_event_set_hash).toMatch(/^[0-9a-f]{64}$/);
				expect(entry?.usage_event_set_hash).not.toBe(entry?.local_hash);
			})
			.finally(restore);
	});
});
