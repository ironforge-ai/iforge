import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { ClaudeCodeAdapter } from "../../src/adapters/claude-code";
import { CodexAdapter } from "../../src/adapters/codex";
import { IronHermesAdapter } from "../../src/adapters/ironhermes";
import { OpenClawAdapter } from "../../src/adapters/openclaw";
import {
	type AgentHomeOverrideSnapshot,
	restoreAgentHomeOverrides,
	snapshotAndClearAgentHomeOverrides,
} from "../commands/helpers";
import { cleanupTmp, copyFixtureToTmp } from "./helpers";
import { type FixtureUsageEvent, makeIronHermesInstall, twoMessages } from "./ironhermes-fixture";

/**
 * The two failure modes this suite exists for are both SILENT.
 *
 * 1. Applying the seconds→milliseconds reflex to `usage_events.ts` puts the row
 *    tens of thousands of years in the future and nothing errors. The adjacent
 *    session and message reads DO multiply by 1000 — correctly, their columns
 *    are float epoch seconds — so the event read looks like it forgot to.
 * 2. Keying dedupe on the source table's own row id double-counts recorded
 *    spend after a store vacuum renumbers it, and nothing errors.
 *
 * Both get an assertion rather than a careful comment.
 */

/** Event timestamps are epoch MILLISECONDS. This value is mid-August 2026. */
const EVT_MS = 1_786_898_459_566;
/** Session/message timestamps are epoch SECONDS. Window straddles `EVT_MS`. */
const SESSION_START_S = 1_786_898_400;
const SESSION_END_S = 1_786_898_520;

/**
 * What the seconds reflex produces if it is ever applied to an event.
 * `new Date(1786898459566 * 1000)` is the year 58600.
 */
const YEAR_CEILING = 3000;

const MULTI_ID = "agent:main:web:dm:multimodel";
const AWKWARD_ID = "agent:main:cli:awkward";
const MSGLESS_ID = "agent:main:telegram:dm:msgless";
const GHOST_ID = "agent:main:cli:ghost-never-emitted";

let tmpHome: string;
let origHome: string | undefined;
let origHomeOverrides: AgentHomeOverrideSnapshot = {};

function evt(over: Partial<FixtureUsageEvent> = {}): FixtureUsageEvent {
	return {
		ts: EVT_MS,
		provider: "anthropic",
		model: "claude-opus-4-7",
		inTok: 100,
		outTok: 20,
		cacheRead: 5,
		cacheCreate: 7,
		costUsdMicros: 123_456,
		errorKind: null,
		...over,
	};
}

/** The install every test in the main describe reads. */
function seedDefaultInstall(root = join(tmpHome, ".ironhermes")): void {
	makeIronHermesInstall(root, {
		stores: [
			{
				relativePath: "state.db",
				sessions: [
					{
						// Genuinely multi-model: three distinct event models under one
						// session whose own `model` column names only the current one.
						id: MULTI_ID,
						model: "claude-opus-4-7",
						workspaceRoot: "/Users/t/code/a",
						startedAt: SESSION_START_S,
						endedAt: SESSION_END_S,
						messageCount: 2,
						toolCallCount: 137,
						inputTokens: 100,
						outputTokens: 20,
						cacheReadTokens: 5,
						messages: twoMessages(SESSION_START_S),
						usageEvents: [
							evt({ ts: EVT_MS, model: "claude-opus-4-7" }),
							evt({ ts: EVT_MS + 1000, model: "gpt-5.3-codex", provider: "openai-codex" }),
							evt({ ts: EVT_MS + 2000, model: "gemini-3-pro", provider: "google" }),
							// A repeat of the first model — `modelsUsed` is a SET, not a list.
							evt({ ts: EVT_MS + 3000, model: "claude-opus-4-7" }),
						],
					},
					{
						// The rows that fail-open rules exist for (D-18).
						id: AWKWARD_ID,
						model: "claude-opus-4-7",
						workspaceRoot: "/Users/t/code/b",
						startedAt: SESSION_START_S,
						endedAt: SESSION_END_S,
						toolCallCount: 4,
						messages: twoMessages(SESSION_START_S),
						usageEvents: [
							// Burned real tokens and then failed. Excluding it under-reports
							// spend, which is the error that is harder to notice.
							evt({ ts: EVT_MS, errorKind: "ContextLength", costUsdMicros: 9_000 }),
							// 56 live rows hold the empty string here.
							evt({ ts: EVT_MS + 500, provider: "", costUsdMicros: 11_000 }),
						],
					},
					{
						// A meaningful share of recorded spend sits on sessions with no
						// extractable messages at all (D-19).
						id: MSGLESS_ID,
						model: "claude-opus-4-7",
						workspaceRoot: "/Users/t/code/c",
						startedAt: SESSION_START_S,
						endedAt: SESSION_END_S,
						toolCallCount: 2,
						messages: [],
						usageEvents: [evt({ ts: EVT_MS }), evt({ ts: EVT_MS + 100 })],
					},
				],
				// No foreign key on the source table; real orphan rows exist.
				orphanUsageEvents: [{ ...evt({ costUsdMicros: 777_777 }), sessionId: GHOST_ID }],
			},
		],
	});
}

async function collect() {
	return (await new IronHermesAdapter().sessions.collect({ kind: "complete" })).sessions;
}

function bySessionId(sessions: Awaited<ReturnType<typeof collect>>, id: string) {
	const found = sessions.find((s) => s.localSessionId === id.replaceAll(":", "."));
	if (!found) throw new Error(`session ${id} absent from output`);
	return found;
}

beforeEach(() => {
	origHome = process.env.HOME;
	origHomeOverrides = snapshotAndClearAgentHomeOverrides();
	tmpHome = mkdtempSync(join(tmpdir(), "clawdi-ih-usage-"));
	process.env.HOME = tmpHome;
});

afterEach(() => {
	if (origHome) process.env.HOME = origHome;
	else delete process.env.HOME;
	restoreAgentHomeOverrides(origHomeOverrides);
	origHomeOverrides = {};
	if (tmpHome) rmSync(tmpHome, { recursive: true, force: true });
});

describe("IronHermes usage events — the millisecond time base (Pitfall 6)", () => {
	it("reads an event timestamp as milliseconds, landing inside the session window", async () => {
		seedDefaultInstall();
		const s = bySessionId(await collect(), MULTI_ID);
		const first = s.usageEvents?.[0];
		expect(first).toBeDefined();
		if (!first) return;

		// The whole point: `new Date(ts)`, NOT `new Date(ts * 1000)`.
		expect(first.ts.getTime()).toBe(EVT_MS);
		expect(first.ts.getUTCFullYear()).toBeLessThan(YEAR_CEILING);
		expect(first.ts.getTime()).toBeGreaterThanOrEqual(s.startedAt.getTime());
		expect(s.endedAt).not.toBeNull();
		expect(first.ts.getTime()).toBeLessThanOrEqual(s.endedAt?.getTime() ?? 0);
	});

	it("puts every event of a session with a recorded end inside [startedAt, endedAt]", async () => {
		seedDefaultInstall();
		const sessions = await collect();
		let asserted = 0;
		for (const s of sessions) {
			if (!s.endedAt || !s.usageEvents?.length) continue;
			for (const e of s.usageEvents) {
				expect(e.ts.getTime()).toBeGreaterThanOrEqual(s.startedAt.getTime());
				expect(e.ts.getTime()).toBeLessThanOrEqual(s.endedAt.getTime());
				expect(e.ts.getUTCFullYear()).toBeLessThan(YEAR_CEILING);
				asserted++;
			}
		}
		// Guard against the assertion loop vacuously passing over nothing.
		expect(asserted).toBeGreaterThanOrEqual(8);
	});

	it("still multiplies the session and message columns by 1000 — they are seconds", async () => {
		seedDefaultInstall();
		const s = bySessionId(await collect(), MULTI_ID);
		expect(s.startedAt.getTime()).toBe(SESSION_START_S * 1000);
		expect(s.messages[0]?.timestamp).toBe(new Date(SESSION_START_S * 1000).toISOString());
	});
});

describe("IronHermes usage events — orphans and fail-open rows", () => {
	it("emits no event whose session_id matches no emitted session (Pitfall 8)", async () => {
		seedDefaultInstall();
		const sessions = await collect();
		const all = sessions.flatMap((s) => s.usageEvents ?? []);
		expect(all.length).toBeGreaterThan(0);
		// The orphan is identifiable by its unique cost.
		expect(all.some((e) => e.costUsdMicros === 777_777)).toBe(false);
		expect(sessions.some((s) => s.localSessionId === GHOST_ID.replaceAll(":", "."))).toBe(false);
	});

	it("emits an error_kind present in no enumeration in either codebase, unchanged (D-18)", async () => {
		seedDefaultInstall();
		const s = bySessionId(await collect(), AWKWARD_ID);
		const failed = s.usageEvents?.find((e) => e.errorKind !== null);
		expect(failed).toBeDefined();
		expect(failed?.errorKind).toBe("ContextLength");
		// The tokens and cost of a failed call are still real spend.
		expect(failed?.costUsdMicros).toBe(9_000);
	});

	it("emits a blank-provider row rather than dropping it or inventing a substitute", async () => {
		seedDefaultInstall();
		const s = bySessionId(await collect(), AWKWARD_ID);
		const blank = s.usageEvents?.find((e) => e.costUsdMicros === 11_000);
		expect(blank).toBeDefined();
		expect(blank?.provider).toBe("");
		expect(s.usageEvents).toHaveLength(2);
	});
});

describe("IronHermes usage events — the session-level surface", () => {
	it("carries tool_call_count from the session row (D-16)", async () => {
		seedDefaultInstall();
		const sessions = await collect();
		expect(bySessionId(sessions, MULTI_ID).toolCallCount).toBe(137);
		expect(bySessionId(sessions, AWKWARD_ID).toolCallCount).toBe(4);
	});

	it("declares NO session-level cache-creation or cost field — both live at event grain (D-15)", async () => {
		seedDefaultInstall();
		const s = bySessionId(await collect(), MULTI_ID);
		const sessionKeys = Object.keys(s);
		expect(sessionKeys.filter((k) => /cachecreat/i.test(k))).toEqual([]);
		expect(sessionKeys.filter((k) => /cost/i.test(k))).toEqual([]);
		// …and cache-creation IS present where D-15 puts it.
		expect(s.usageEvents?.[0]?.cacheCreate).toBe(7);
		expect(s.usageEvents?.[0]?.costUsdMicros).toBe(123_456);
	});

	it("makes modelsUsed the DISTINCT set of event models, with model still the session's own (D-20)", async () => {
		seedDefaultInstall();
		const s = bySessionId(await collect(), MULTI_ID);
		expect(s.model).toBe("claude-opus-4-7");
		expect([...s.modelsUsed].sort()).toEqual(["claude-opus-4-7", "gemini-3-pro", "gpt-5.3-codex"]);
	});

	it("never uploads a provider secret as an EVENT model (T-02-26)", async () => {
		// `parseModelField` exists in this very file (ironhermes.ts:105) and its
		// own doc says why: "Never return the raw blob: it can carry provider
		// headers including API keys, and the model value is rendered in the
		// dashboard." It was wired to `sessions.model` at :332 and NOT to the
		// event rows, which carry the same free-text column from the same store.
		//
		// The sibling assertion for the session field is hermes.test.ts:77.
		// Events reach the same dashboard, so they need the same control.
		const blob =
			'{"default": "gpt-5.3-codex", "provider": "openai-codex", "headers": {"x-api-key": "sk-live-must-not-ship"}}';
		makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
			stores: [
				{
					relativePath: "state.db",
					sessions: [
						{
							id: "agent:cli:blobmodel",
							model: "claude-opus-4-7",
							startedAt: SESSION_START_S,
							messages: twoMessages(SESSION_START_S),
							usageEvents: [evt({ ts: EVT_MS, model: blob })],
						},
					],
				},
			],
		});

		const s = bySessionId(await collect(), "agent:cli:blobmodel");
		const models = (s.usageEvents ?? []).map((e) => e.model);
		expect(models).toEqual(["gpt-5.3-codex"]);
		// Assert on the secret itself, not just the happy value — a future
		// refactor that extracts the model but also forwards the blob elsewhere
		// should still fail here.
		for (const m of models) expect(m).not.toContain("sk-live-must-not-ship");
		expect(s.modelsUsed).not.toContain(blob);
	});

	it("parses a blob the JSON gate would otherwise miss (T-02-26 bypass shapes)", async () => {
		// The case above passes with a `raw.startsWith("{")` gate. These do not.
		//
		// `parseModelField` tested `startsWith("{")` with NO `.strip()`, so any
		// leading whitespace returned the blob verbatim — while the server's
		// `_clean_model_value` (session.py:195-198) strips FIRST and only then
		// tests the brace. The two layers were never redundant: this is exactly
		// the shape only one of them catches, which is why the missing server
		// half was load-bearing rather than defence in depth.
		//
		// SQLite stores whatever IronHermes wrote, and a config serialiser that
		// pretty-prints or emits an array produces these without anything going
		// wrong upstream.
		const secret = "sk-live-bypass-must-not-ship";
		const cases: Array<[string, string, string]> = [
			[
				"leading space",
				` {"default": "gpt-5.3-codex", "headers": {"x-api-key": "${secret}"}}`,
				"gpt-5.3-codex",
			],
			[
				"leading newline",
				`\n{"default": "claude-opus-4-7", "headers": {"x-api-key": "${secret}"}}`,
				"claude-opus-4-7",
			],
			[
				"leading tab",
				`\t{"model": "gemini-3-pro", "headers": {"x-api-key": "${secret}"}}`,
				"gemini-3-pro",
			],
		];

		for (const [label, raw, expected] of cases) {
			const id = `agent:cli:bypass-${label.replace(/\s/g, "-")}`;
			makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
				stores: [
					{
						relativePath: "state.db",
						sessions: [
							{
								id,
								model: "claude-opus-4-7",
								startedAt: SESSION_START_S,
								messages: twoMessages(SESSION_START_S),
								usageEvents: [evt({ ts: EVT_MS, model: raw })],
							},
						],
					},
				],
			});

			const s = bySessionId(await collect(), id);
			const models = (s.usageEvents ?? []).map((e) => e.model);
			expect(models, label).toEqual([expected]);
			for (const m of models) expect(m, label).not.toContain(secret);
			rmSync(join(tmpHome, ".ironhermes"), { recursive: true, force: true });
		}
	});

	it("does not crash the collect path on a nested-object model field", async () => {
		// `parseModelField` returned `obj.default || obj.model` without checking
		// the type, so a nested object came back as an object and `safeTruncate`
		// then threw `str is not iterable` — taking down the whole store read,
		// not just this row. Availability rather than disclosure, found beside
		// T-02-26 and fixed with it.
		makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
			stores: [
				{
					relativePath: "state.db",
					sessions: [
						{
							id: "agent:cli:nestedmodel",
							model: "claude-opus-4-7",
							startedAt: SESSION_START_S,
							messages: twoMessages(SESSION_START_S),
							usageEvents: [
								evt({
									ts: EVT_MS,
									model: '{"default": {"name": "m", "api_key": "sk-live-nested-must-not-ship"}}',
								}),
							],
						},
					],
				},
			],
		});

		const s = bySessionId(await collect(), "agent:cli:nestedmodel");
		const models = (s.usageEvents ?? []).map((e) => e.model);
		// Fails open to blank rather than throwing, and carries no secret.
		expect(models).toEqual([""]);
		for (const m of models) expect(m).not.toContain("sk-live-nested-must-not-ship");
	});

	it("falls back to the session model when a session has no events at all", async () => {
		makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
			stores: [
				{
					relativePath: "state.db",
					sessions: [
						{
							id: "agent:cli:noevents",
							model: "claude-opus-4-7",
							startedAt: SESSION_START_S,
							messages: twoMessages(SESSION_START_S),
						},
					],
				},
			],
		});
		const s = bySessionId(await collect(), "agent:cli:noevents");
		// Never regress below what the tracer already produced.
		expect(s.modelsUsed).toEqual(["claude-opus-4-7"]);
		expect(s.usageEvents ?? []).toHaveLength(0);
	});

	it("retains a session with events but zero extractable messages (D-19, IronHermes only)", async () => {
		seedDefaultInstall();
		const s = bySessionId(await collect(), MSGLESS_ID);
		expect(s.messages).toHaveLength(0);
		expect(s.usageEvents).toHaveLength(2);
	});

	it("still skips a session with neither messages nor events", async () => {
		makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
			stores: [
				{
					relativePath: "state.db",
					sessions: [
						{ id: "agent:cli:barren", startedAt: SESSION_START_S, messages: [] },
						{
							id: "agent:cli:real",
							startedAt: SESSION_START_S,
							messages: twoMessages(SESSION_START_S),
						},
					],
				},
			],
		});
		const ids = (await collect()).map((s) => s.localSessionId);
		expect(ids).toEqual(["agent.cli.real"]);
	});
});

describe("IronHermes usage events — the per-session set hash (D-22)", () => {
	/** Build a one-session install in a fresh HOME and return its set hash. */
	async function hashFor(events: FixtureUsageEvent[]): Promise<string | undefined> {
		const home = mkdtempSync(join(tmpdir(), "clawdi-ih-hash-"));
		const prev = process.env.HOME;
		process.env.HOME = home;
		try {
			makeIronHermesInstall(join(home, ".ironhermes"), {
				stores: [
					{
						relativePath: "state.db",
						sessions: [
							{
								id: "agent:cli:hashme",
								model: "claude-opus-4-7",
								startedAt: SESSION_START_S,
								endedAt: SESSION_END_S,
								messages: twoMessages(SESSION_START_S),
								usageEvents: events,
							},
						],
					},
				],
			});
			const s = bySessionId(await collect(), "agent:cli:hashme");
			return s.usageEventSetHash;
		} finally {
			if (prev) process.env.HOME = prev;
			else delete process.env.HOME;
			rmSync(home, { recursive: true, force: true });
		}
	}

	const BASE: FixtureUsageEvent[] = [
		evt({ ts: EVT_MS }),
		evt({ ts: EVT_MS + 1000, model: "gpt-5.3-codex", provider: "openai-codex" }),
	];

	it("is byte-identical across two collections of an unmodified fixture", async () => {
		seedDefaultInstall();
		const a = bySessionId(await collect(), MULTI_ID).usageEventSetHash;
		const b = bySessionId(await collect(), MULTI_ID).usageEventSetHash;
		expect(a).toBeDefined();
		expect(a).toMatch(/^[0-9a-f]{64}$/);
		expect(b).toBe(a);
	});

	it("is stable across two independently built identical stores", async () => {
		expect(await hashFor(BASE)).toBe(await hashFor(BASE));
	});

	// One case per field. A hash that moves on cost but not on model would pass
	// a single-field test and still let a real edit go unpushed.
	const MUTATIONS: Array<[string, Partial<FixtureUsageEvent>]> = [
		["cost", { costUsdMicros: 999_999 }],
		["input token count", { inTok: 101 }],
		["output token count", { outTok: 21 }],
		["cache read", { cacheRead: 6 }],
		["cache create", { cacheCreate: 8 }],
		["model", { model: "claude-haiku-4-7" }],
		["provider", { provider: "bedrock" }],
		["timestamp", { ts: EVT_MS + 7 }],
		["error kind", { errorKind: "RateLimited" }],
	];

	for (const [label, patch] of MUTATIONS) {
		it(`changes when the ${label} of a single row changes`, async () => {
			const base = await hashFor(BASE);
			const mutated = await hashFor([evt({ ts: EVT_MS, ...patch }), BASE[1] as FixtureUsageEvent]);
			expect(base).toBeDefined();
			expect(mutated).toBeDefined();
			expect(mutated).not.toBe(base);
		});
	}

	it("changes when a row is added and when one is removed", async () => {
		const base = await hashFor(BASE);
		expect(await hashFor([...BASE, evt({ ts: EVT_MS + 5000 })])).not.toBe(base);
		expect(await hashFor([BASE[0] as FixtureUsageEvent])).not.toBe(base);
	});

	it("is absent for a session with no events rather than being a hash of nothing", async () => {
		expect(await hashFor([])).toBeUndefined();
	});
});

describe("IronHermes usage events — per-store collection", () => {
	it("gives a profile-store session that profile's events, not the root store's", async () => {
		makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
			stores: [
				{
					relativePath: "state.db",
					sessions: [
						{
							id: "agent:cli:root",
							model: "root-model",
							startedAt: SESSION_START_S,
							endedAt: SESSION_END_S,
							messages: twoMessages(SESSION_START_S),
							usageEvents: [evt({ model: "root-model", costUsdMicros: 1_000 })],
						},
					],
				},
				{
					relativePath: "profiles/work/state.db",
					sessions: [
						{
							id: "agent:cli:profile",
							model: "profile-model",
							startedAt: SESSION_START_S,
							endedAt: SESSION_END_S,
							messages: twoMessages(SESSION_START_S),
							usageEvents: [evt({ model: "profile-model", costUsdMicros: 2_000 })],
						},
					],
				},
			],
		});
		const sessions = await collect();
		const root = bySessionId(sessions, "agent:cli:root");
		const profile = bySessionId(sessions, "agent:cli:profile");

		expect(root.usageEvents?.map((e) => e.costUsdMicros)).toEqual([1_000]);
		expect(profile.usageEvents?.map((e) => e.costUsdMicros)).toEqual([2_000]);
		expect(root.modelsUsed).toEqual(["root-model"]);
		expect(profile.modelsUsed).toEqual(["profile-model"]);
	});

	it("still returns a store's sessions when that store has no usage_events table", async () => {
		makeIronHermesInstall(join(tmpHome, ".ironhermes"), {
			stores: [
				{
					relativePath: "state.db",
					omitUsageEventsTable: true,
					sessions: [
						{
							id: "agent:cli:pre-telemetry",
							model: "claude-opus-4-7",
							startedAt: SESSION_START_S,
							messages: twoMessages(SESSION_START_S),
						},
					],
				},
			],
		});
		// An install predating the telemetry table is still full of real sessions;
		// the event read must not be able to abort the session read beside it.
		const s = bySessionId(await collect(), "agent:cli:pre-telemetry");
		expect(s.messages).toHaveLength(2);
		expect(s.usageEvents ?? []).toHaveLength(0);
		expect(s.usageEventSetHash).toBeUndefined();
	});
});

describe("the three new RawSession fields are optional", () => {
	/**
	 * Every adapter builds a `RawSession` object literal, so a REQUIRED field
	 * means editing all four adapter files. These three are optional precisely
	 * so a phase meant to touch Hermes minimally stays that way — and the other
	 * three runtimes leave them undefined rather than fabricating a zero.
	 */
	const OTHERS = [
		["claude-code", () => new ClaudeCodeAdapter()],
		["codex", () => new CodexAdapter()],
		["openclaw", () => new OpenClawAdapter()],
	] as const;

	for (const [fixture, make] of OTHERS) {
		it(`${fixture} still collects sessions with all three fields undefined`, async () => {
			const home = copyFixtureToTmp(fixture);
			const prev = process.env.HOME;
			process.env.HOME = home;
			try {
				const { sessions } = await make().sessions.collect({ kind: "complete" });
				expect(sessions.length).toBeGreaterThan(0);
				for (const s of sessions) {
					expect(s.toolCallCount).toBeUndefined();
					expect(s.usageEvents).toBeUndefined();
					expect(s.usageEventSetHash).toBeUndefined();
				}
			} finally {
				if (prev) process.env.HOME = prev;
				else delete process.env.HOME;
				cleanupTmp(home);
			}
		});
	}
});
