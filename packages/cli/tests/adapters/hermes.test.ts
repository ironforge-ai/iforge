import { Database } from "bun:sqlite";
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { scanSessionModule } from "../../src/adapters/base";
import { HermesAdapter } from "../../src/adapters/hermes";
import { tarSkillDir } from "../../src/lib/tar";
import { reserveManagedSkill } from "../../src/runtime/managed-skill-reservation";
import {
	type AgentHomeOverrideSnapshot,
	restoreAgentHomeOverrides,
	snapshotAndClearAgentHomeOverrides,
} from "../commands/helpers";
import { addSkillDirectorySymlinkCases, cleanupTmp, copyFixtureToTmp } from "./helpers";

let tmpHome: string;
let origHome: string | undefined;
let origHomeOverrides: AgentHomeOverrideSnapshot = {};

beforeEach(() => {
	origHome = process.env.HOME;
	origHomeOverrides = snapshotAndClearAgentHomeOverrides();
	tmpHome = copyFixtureToTmp("hermes");
	process.env.HOME = tmpHome;
});

afterEach(() => {
	if (origHome) process.env.HOME = origHome;
	else delete process.env.HOME;
	restoreAgentHomeOverrides(origHomeOverrides);
	origHomeOverrides = {};
	cleanupTmp(tmpHome);
});

describe("HermesAdapter.detect", () => {
	it("returns true when $HOME/.hermes exists", async () => {
		const a = new HermesAdapter();
		expect(await a.detect()).toBe(true);
	});

	it("returns false when $HOME/.hermes is absent", async () => {
		process.env.HOME = `/tmp/clawdi-nowhere-${Date.now()}`;
		const a = new HermesAdapter();
		expect(await a.detect()).toBe(false);
	});
});

describe("HermesAdapter.collectSessions", () => {
	it("selects events-v1 and maps every safe modern row in stable source order", async () => {
		const a = new HermesAdapter();
		expect(await a.sessions.contentProtocol()).toBe("events-v1");
		const { sessions } = await a.sessions.collect({ kind: "complete" });
		expect(sessions).toHaveLength(1);
		const session = sessions[0];
		expect(session).toMatchObject({
			localSessionId: "s-modern",
			projectPath: null,
			model: "gpt-5.3-codex",
			modelsUsed: ["gpt-5.3-codex"],
			messageCount: 12,
			inputTokens: 120,
			outputTokens: 45,
			cacheReadTokens: 8,
			summary: "Inspect this report",
		});
		expect(session?.rawFilePath).toContain("state.db#s-modern");
		expect((await a.sessions.resolve("s-modern"))?.events).toEqual(session?.events);
		expect(await a.sessions.resolve("missing-session")).toBeNull();

		const events = session?.events ?? [];
		expect(events.map((event) => event.seq)).toEqual(events.map((_, index) => index));
		expect(events.map((event) => event.source.record_id)).toEqual([
			"1",
			"2",
			"3",
			"3",
			"3",
			"4",
			"5",
			"6",
			"7",
			"8",
			"8",
			"9",
			"10",
			"10",
			"11",
			"12",
		]);
		expect(events[0]).toMatchObject({ type: "message", role: "system" });
		expect(events[1]).toMatchObject({
			type: "message",
			role: "user",
			parts: [
				{ type: "text", text: "Inspect this report" },
				{
					type: "attachment",
					availability: "external",
					uri: "https://cdn.example.com/report.png",
					media_type: "image/png",
				},
			],
			semantics: {
				lifecycle: "active",
				display: "message",
				display_metadata: {
					reactions: [{ emoji: "thumbs-up", author: "user" }],
				},
			},
		});
		expect(events[2]).toMatchObject({
			type: "reasoning",
			parts: [{ type: "text", text: "hidden row reasoning" }],
			payload_json: '{"items":[{"text":"hidden codex reasoning","type":"reasoning"}]}',
			semantics: { lifecycle: "compacted" },
		});
		expect(events[3]).toMatchObject({
			type: "tool_call",
			call_id: "call-search",
			name: "search",
			arguments_json: '{"api_key":"sk-tool-secret","query":"Hermes"}',
			semantics: { lifecycle: "compacted" },
		});
		expect(events[4]).toMatchObject({
			type: "tool_call",
			call_id: "call-read",
			name: "read_file",
		});
		expect(events[5]).toMatchObject({
			type: "tool_result",
			call_id: "call-search",
			name: "search",
			parts: [{ type: "text", text: "Found one result" }],
			result_json: '[{"items":[{"id":1}],"ok":true,"password":"result-secret"}]',
			semantics: { lifecycle: "compacted" },
		});
		expect(events[8]).toMatchObject({
			type: "message",
			role: "user",
			semantics: {
				lifecycle: "inactive",
				display: "event",
				display_kind: "auto_continue",
				display_metadata: { attempt: 2 },
			},
		});
		expect(events[7]).toMatchObject({
			type: "message",
			parts: [{ type: "text", text: "Summary of earlier turns" }],
			semantics: { compressed_summary: true },
		});
		expect(events[9]).toMatchObject({
			type: "reasoning",
			parts: [
				{ type: "text", text: "hidden reasoning" },
				{ type: "text", text: "hidden reasoning content" },
				{ type: "text", text: "hidden inline reasoning" },
			],
			payload_json: '{"details":{"encrypted_content":"opaque reasoning"}}',
		});
		expect(events[10]).toMatchObject({
			type: "message",
			parts: [{ type: "text", text: "Public answer" }],
		});
		expect(events[11]).toMatchObject({
			semantics: {
				display: "event",
				display_kind: "async_delegation_complete",
				display_metadata: { task_count: 2 },
			},
		});
		// Assistant rows with visible content + tool_calls produce one message and one call.
		expect(events.slice(12, 14).map((event) => event.type)).toEqual(["message", "tool_call"]);
		// Raw audit rows are retained: the compaction copy remains distinct and marked compacted.
		expect(events[6]).toMatchObject({ source: { record_id: "5" } });
		expect(events[15]).toMatchObject({
			source: { record_id: "12" },
			semantics: { lifecycle: "compacted" },
		});
		expect(events[14]).toMatchObject({
			type: "tool_result",
			result_json: '{"authorization":"Bearer secret","lines":42,"ok":true}',
		});

		const serialized = JSON.stringify(session);
		for (const reasoning of [
			"hidden row reasoning",
			"hidden codex reasoning",
			"hidden inline reasoning",
			"hidden reasoning content",
			"opaque reasoning",
		]) {
			expect(serialized).toContain(reasoning);
		}
		for (const hidden of [
			"sk-model-secret",
			"sk-config-secret",
			"provider envelope secret",
			"display-secret",
			"metadata-secret",
		]) {
			expect(serialized).not.toContain(hidden);
		}
	});

	it("keeps prior identities as an append-only prefix when a row is added", async () => {
		const adapter = new HermesAdapter();
		const before = (await adapter.sessions.resolve("s-modern"))?.events ?? [];
		const db = new Database(join(tmpHome, ".hermes", "state.db"));
		db.run(
			"INSERT INTO messages (session_id, role, content, timestamp, active, compacted) VALUES (?, ?, ?, ?, 1, 0)",
			"s-modern",
			"user",
			"Appended turn",
			1776247261,
		);
		db.close();

		const after = (await adapter.sessions.resolve("s-modern"))?.events ?? [];
		expect(after.slice(0, before.length).map((event) => event.event_id)).toEqual(
			before.map((event) => event.event_id),
		);
		expect(after.at(-1)).toMatchObject({
			seq: before.length,
			type: "message",
			source: { adapter: "hermes", record_id: "13", record_seq: 13 },
		});
	});

	it("scans large stores in bounded batches and expands only revised sessions", async () => {
		const db = new Database(join(tmpHome, ".hermes", "state.db"));
		for (let index = 0; index < 40; index++) {
			const id = `bulk-${String(index).padStart(2, "0")}`;
			db.run(
				"INSERT INTO sessions (id, source, title, started_at, message_count) VALUES (?, 'cron', ?, ?, 1)",
				id,
				`Bulk ${index}`,
				1776247000,
			);
			db.run(
				"INSERT INTO messages (session_id, role, content, timestamp, active, compacted) VALUES (?, 'user', ?, ?, 1, 0)",
				id,
				`Message ${index}`,
				1776247000,
			);
		}
		db.run(
			"INSERT INTO messages (session_id, role, content, timestamp, active, compacted) VALUES ('bulk-04', 'assistant', 'Reply', 1776247001, 1, 0)",
		);
		db.close();

		const adapter = new HermesAdapter();
		const initial = await scanSessionModule(adapter.sessions, { kind: "complete" });
		const revisions = new Map<string, string>();
		const initialBatchSizes: number[] = [];
		for await (const batch of initial.batches) {
			initialBatchSizes.push(batch.observedLocalSessionIds.length);
			for (const session of batch.sessions) {
				if (!session.sourceRevision) throw new Error("expected Hermes source revision");
				revisions.set(session.localSessionId, session.sourceRevision);
			}
		}
		expect(initialBatchSizes).toEqual([32, 9]);
		expect(revisions.size).toBe(41);

		const unchanged = await scanSessionModule(adapter.sessions, { kind: "complete" }, revisions);
		let observed = 0;
		let expanded = 0;
		for await (const batch of unchanged.batches) {
			observed += batch.observedLocalSessionIds.length;
			expanded += batch.sessions.length;
		}
		expect({ observed, expanded }).toEqual({ observed: 41, expanded: 0 });

		const changedDb = new Database(join(tmpHome, ".hermes", "state.db"));
		changedDb.run(
			"INSERT INTO messages (session_id, role, content, timestamp, active, compacted) VALUES ('bulk-00', 'assistant', 'Changed', 1776247001, 1, 0)",
		);
		changedDb.run("UPDATE sessions SET title = 'Renamed' WHERE id = 'bulk-01'");
		changedDb.run(
			`UPDATE messages SET display_kind = 'auto_continue', display_metadata = '{"attempt":2}'
			 WHERE session_id = 'bulk-02'`,
		);
		changedDb.run("UPDATE messages SET content = 'M' WHERE session_id = 'bulk-03'");
		changedDb.run(
			`UPDATE messages
			 SET content = CASE role WHEN 'user' THEN 'Short' ELSE 'Message 4' END
			 WHERE session_id = 'bulk-04'`,
		);
		changedDb.close();
		const changed = await scanSessionModule(adapter.sessions, { kind: "complete" }, revisions);
		const changedIds: string[] = [];
		for await (const batch of changed.batches) {
			changedIds.push(...batch.sessions.map((session) => session.localSessionId));
		}
		expect(changedIds.sort()).toEqual(["bulk-00", "bulk-01", "bulk-02", "bulk-03", "bulk-04"]);
	});

	it("classifies user rows from Hermes conversations, including compression splits", async () => {
		const db = new Database(join(tmpHome, ".hermes", "state.db"));
		for (const [id, source, parentSessionId, modelConfig, timestamp] of [
			["activity-root", "telegram", null, null, 2_000_000_000],
			["activity-cron", "cron", null, null, 2_000_000_600],
			["activity-subagent", "subagent", null, null, 2_000_000_700],
			["activity-curator", "curator", null, null, 2_000_000_800],
			["activity-kanban", "kanban", null, null, 2_000_000_900],
			[
				"activity-delegate",
				"telegram",
				"activity-root",
				'{"_delegate_from":"activity-root"}',
				2_000_001_000,
			],
			// A compression split continues the same conversation under a new session row.
			["activity-split", "telegram", "activity-root", null, 2_000_000_400],
		] as const) {
			db.run(
				"INSERT INTO sessions (id, source, parent_session_id, model_config, title, started_at, message_count) VALUES (?, ?, ?, ?, ?, ?, 1)",
				id,
				source,
				parentSessionId,
				modelConfig,
				id,
				timestamp,
			);
			db.run(
				"INSERT INTO messages (session_id, role, content, timestamp, active, compacted) VALUES (?, 'user', 'input', ?, 1, 0)",
				id,
				timestamp,
			);
		}
		db.close();

		const scan = await scanSessionModule(new HermesAdapter().sessions, { kind: "complete" });

		expect(scan.userActivity).toEqual({
			lastUserInputAt: new Date(2_000_000_400 * 1000).toISOString(),
			complete: true,
		});
	});

	it("fails Hermes activity closed when the session source is unavailable", async () => {
		const db = new Database(join(tmpHome, ".hermes", "state.db"));
		db.exec(`
			ALTER TABLE sessions DROP COLUMN source;
		`);
		db.close();

		const scan = await scanSessionModule(new HermesAdapter().sessions, { kind: "complete" });
		expect(scan.userActivity).toEqual({ lastUserInputAt: null, complete: false });
		// Session batches need the same column. Upstream surfaces that schema error from the drain;
		// the fork pools every profile store (D-12), and its per-store guard skips a store it cannot
		// read and reports the scan `partial` instead, so its sessions are never treated as absent.
		for await (const _batch of scan.batches) {
			// Drain so the read-only SQLite handle closes.
		}
		expect(scan.coverage).toBe("partial");
	});

	it("uses events-v1 with stable ids when newer optional message columns are absent", async () => {
		const db = new Database(join(tmpHome, ".hermes", "state.db"));
		db.exec(`
			DROP TABLE messages;
			CREATE TABLE messages (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				session_id TEXT NOT NULL,
				role TEXT NOT NULL,
				content TEXT,
				timestamp REAL NOT NULL
			);
			INSERT INTO messages (session_id, role, content, timestamp) VALUES
				('s-modern', 'developer', '<think>literal developer content', 1776247201),
				('s-modern', 'assistant', 'Visible answer', 1776247202);
		`);
		db.close();

		const adapter = new HermesAdapter();
		expect(await adapter.sessions.contentProtocol()).toBe("events-v1");
		const session = await adapter.sessions.resolve("s-modern");
		expect(session?.events).toMatchObject([
			{
				type: "message",
				role: "developer",
				parts: [{ type: "text", text: "<think>literal developer content" }],
				source: { record_id: "1", record_seq: 1 },
				semantics: {
					lifecycle: "active",
					display: "message",
					compressed_summary: false,
				},
			},
			{
				type: "message",
				role: "assistant",
				source: { record_id: "2", record_seq: 2 },
			},
		]);
	});

	it("scrubs upstream reasoning tags only from assistant model output", async () => {
		const db = new Database(join(tmpHome, ".hermes", "state.db"));
		const insert = db.prepare(
			"INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, timestamp, active, compacted) VALUES (?, ?, ?, ?, ?, ?, 1, 0)",
		);
		insert.run("s-modern", "user", "<thinking>literal user content", null, null, 1776247261);
		insert.run("s-modern", "system", "<thought>literal system content", null, null, 1776247262);
		insert.run(
			"s-modern",
			"tool",
			"<REASONING_SCRATCHPAD>literal tool content",
			"call-literal",
			"literal_tool",
			1776247263,
		);
		insert.run(
			"s-modern",
			"assistant",
			"<think>hidden think</think><thinking>hidden thinking</thinking><reasoning>hidden reasoning</reasoning><thought>hidden thought</thought><REASONING_SCRATCHPAD>hidden scratchpad</REASONING_SCRATCHPAD>Visible assistant",
			null,
			null,
			1776247264,
		);
		insert.run(
			"s-modern",
			"assistant",
			'Use the <think> element in prose.\nconst tag = "<reasoning>";\n  <thought>hidden tail',
			null,
			null,
			1776247265,
		);
		db.close();

		const session = await new HermesAdapter().sessions.resolve("s-modern");
		const events = session?.events ?? [];
		const added = events.filter((event) => event.source.record_seq > 12);
		expect(added).toMatchObject([
			{
				type: "message",
				role: "user",
				parts: [{ type: "text", text: "<thinking>literal user content" }],
			},
			{
				type: "message",
				role: "system",
				parts: [{ type: "text", text: "<thought>literal system content" }],
			},
			{
				type: "tool_result",
				parts: [{ type: "text", text: "<REASONING_SCRATCHPAD>literal tool content" }],
			},
			{
				type: "reasoning",
				kind: "reasoning",
				parts: [
					{ type: "text", text: "hidden think" },
					{ type: "text", text: "hidden thinking" },
					{ type: "text", text: "hidden reasoning" },
					{ type: "text", text: "hidden thought" },
					{ type: "text", text: "hidden scratchpad" },
				],
			},
			{
				type: "message",
				role: "assistant",
				parts: [{ type: "text", text: "Visible assistant" }],
			},
			{
				type: "reasoning",
				kind: "reasoning",
				parts: [{ type: "text", text: "hidden tail" }],
			},
			{
				type: "message",
				role: "assistant",
				parts: [
					{
						type: "text",
						text: 'Use the <think> element in prose.\nconst tag = "<reasoning>";\n  ',
					},
				],
			},
		]);
		for (const hidden of [
			"hidden think",
			"hidden thinking",
			"hidden reasoning",
			"hidden thought",
			"hidden scratchpad",
			"hidden tail",
		]) {
			expect(JSON.stringify(added)).toContain(hidden);
			expect(JSON.stringify(session?.messages)).not.toContain(hidden);
		}
	});

	it("falls back to snapshot-v1 for a legacy messages table without stable ids", async () => {
		const db = new Database(join(tmpHome, ".hermes", "state.db"));
		db.exec(`
			DROP TABLE messages;
			CREATE TABLE messages (
				session_id TEXT NOT NULL,
				role TEXT NOT NULL,
				content TEXT,
				timestamp REAL NOT NULL
			);
			INSERT INTO messages VALUES
				('s-modern', 'user', 'legacy question', 1776247201),
				('s-modern', 'assistant', 'legacy answer', 1776247202);
		`);
		db.close();

		const adapter = new HermesAdapter();
		expect(await adapter.sessions.contentProtocol()).toBe("snapshot-v1");
		const session = await adapter.sessions.resolve("s-modern");
		expect(session?.events).toBeUndefined();
		expect(session?.messages).toEqual([
			{
				role: "user",
				content: "legacy question",
				timestamp: "2026-04-15T10:00:01.000Z",
			},
			{
				role: "assistant",
				content: "legacy answer",
				model: "gpt-5.3-codex",
				timestamp: "2026-04-15T10:00:02.000Z",
			},
		]);
	});
});

/**
 * Upstream Hermes has the same profile-scoped layout IronHermes does:
 * `<HERMES_HOME>/state.db` plus `<HERMES_HOME>/profiles/<name>/state.db`. The
 * adapter read only the root, so Hermes users with profiles have been losing
 * those sessions. D-12 closes that with the SAME shared walk, and pins that
 * nothing else about this shipped adapter moved with it.
 *
 * Built programmatically rather than as a second checked-in binary: the schema
 * stays readable, and the existing suite already opens a database mid-test.
 */
const HERMES_SESSIONS_DDL = `
CREATE TABLE sessions (
	id TEXT PRIMARY KEY,
	source TEXT,
	model TEXT,
	title TEXT,
	started_at REAL NOT NULL,
	ended_at REAL,
	message_count INTEGER DEFAULT 0,
	input_tokens INTEGER DEFAULT 0,
	output_tokens INTEGER DEFAULT 0,
	cache_read_tokens INTEGER DEFAULT 0
)`;
const HERMES_MESSAGES_DDL = `
CREATE TABLE messages (
	session_id TEXT NOT NULL,
	role TEXT NOT NULL,
	content TEXT,
	timestamp REAL NOT NULL,
	FOREIGN KEY(session_id) REFERENCES sessions(id)
)`;

interface HermesFixtureSession {
	id: string;
	model?: string | null;
	title?: string | null;
	startedAt: number;
	withMessages?: boolean;
}

function makeHermesStore(dbPath: string, sessions: HermesFixtureSession[]): string {
	mkdirSync(join(dbPath, ".."), { recursive: true });
	const db = new Database(dbPath, { create: true });
	try {
		db.run(HERMES_SESSIONS_DDL);
		db.run(HERMES_MESSAGES_DDL);
		const insertSession = db.prepare(
			"INSERT INTO sessions (id, source, model, title, started_at, ended_at, message_count," +
				" input_tokens, output_tokens, cache_read_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
		);
		const insertMessage = db.prepare(
			"INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
		);
		for (const s of sessions) {
			insertSession.run(
				s.id,
				"telegram",
				s.model ?? "claude-opus-4-7",
				s.title ?? null,
				s.startedAt,
				null,
				s.withMessages === false ? 0 : 2,
				0,
				0,
				0,
			);
			if (s.withMessages === false) continue;
			insertMessage.run(s.id, "user", "profile hello", s.startedAt);
			insertMessage.run(s.id, "assistant", "profile hi", s.startedAt + 1);
		}
	} finally {
		db.close();
	}
	return dbPath;
}

describe("HermesAdapter — profile stores (D-12)", () => {
	it("yields the union of the root store and a profile store", async () => {
		makeHermesStore(join(tmpHome, ".hermes", "profiles", "work", "state.db"), [
			{ id: "p-work-1", startedAt: 1776247400 },
		]);

		const { sessions } = await new HermesAdapter().sessions.collect({ kind: "complete" });
		// The root fixture (upstream's modern store since #1229) contributes s-modern.
		expect(sessions.map((s) => s.localSessionId).sort()).toEqual(["p-work-1", "s-modern"]);
	});

	it("skips a profile directory that carries no store", async () => {
		mkdirSync(join(tmpHome, ".hermes", "profiles", "abandoned"), { recursive: true });
		makeHermesStore(join(tmpHome, ".hermes", "profiles", "work", "state.db"), [
			{ id: "p-work-1", startedAt: 1776247400 },
		]);

		const { sessions } = await new HermesAdapter().sessions.collect({ kind: "complete" });
		expect(sessions.map((s) => s.localSessionId).sort()).toEqual(["p-work-1", "s-modern"]);
	});

	it("detect() is true when the only store lives under profiles/", async () => {
		rmSync(join(tmpHome, ".hermes", "state.db"), { force: true });
		makeHermesStore(join(tmpHome, ".hermes", "profiles", "work", "state.db"), [
			{ id: "p-work-1", startedAt: 1776247400 },
		]);

		expect(await new HermesAdapter().detect()).toBe(true);
	});

	it("anchors rawFilePath to the profile store that produced the row", async () => {
		const store = makeHermesStore(join(tmpHome, ".hermes", "profiles", "work", "state.db"), [
			{ id: "p-work-1", startedAt: 1776247400 },
		]);

		const { sessions } = await new HermesAdapter().sessions.collect({ kind: "complete" });
		const fromProfile = sessions.find((s) => s.localSessionId === "p-work-1");
		expect(fromProfile?.rawFilePath).toBe(`${store}#p-work-1`);

		const fromRoot = sessions.find((s) => s.localSessionId === "s-modern");
		expect(fromRoot?.rawFilePath).toBe(`${join(tmpHome, ".hermes", "state.db")}#s-modern`);
	});

	it("finds the store when HERMES_HOME points directly at a profiles/<name> dir", async () => {
		// Upstream supports this configuration. The enumerator's root-store branch
		// already produces the right answer — no walk-back branch needed.
		const profileDir = join(tmpHome, ".hermes", "profiles", "work");
		makeHermesStore(join(profileDir, "state.db"), [{ id: "p-work-1", startedAt: 1776247400 }]);

		process.env.HERMES_HOME = profileDir;
		try {
			const a = new HermesAdapter();
			expect(await a.detect()).toBe(true);
			const { sessions } = await a.sessions.collect({ kind: "complete" });
			expect(sessions.map((s) => s.localSessionId)).toEqual(["p-work-1"]);
		} finally {
			delete process.env.HERMES_HOME;
		}
	});

	it("still emits projectPath null for profile-store sessions — D-02 is IronHermes-only", async () => {
		makeHermesStore(join(tmpHome, ".hermes", "profiles", "work", "state.db"), [
			{ id: "p-work-1", startedAt: 1776247400 },
		]);

		const { sessions } = await new HermesAdapter().sessions.collect({ kind: "complete" });
		expect(sessions).not.toHaveLength(0);
		for (const s of sessions) expect(s.projectPath).toBeNull();
	});

	it("still skips message-less profile sessions — D-19 is IronHermes-only", async () => {
		makeHermesStore(join(tmpHome, ".hermes", "profiles", "work", "state.db"), [
			{ id: "p-work-1", startedAt: 1776247400 },
			{ id: "p-work-empty", startedAt: 1776247500, withMessages: false },
		]);

		const { sessions } = await new HermesAdapter().sessions.collect({ kind: "complete" });
		expect(sessions.find((s) => s.localSessionId === "p-work-empty")).toBeUndefined();
		expect(sessions.find((s) => s.localSessionId === "p-work-1")).toBeDefined();
	});

	it("does not sanitize colon-bearing ids — that transform is IronHermes-only", async () => {
		// Applying it here would change the object-store key of every already
		// synced Hermes session.
		makeHermesStore(join(tmpHome, ".hermes", "profiles", "work", "state.db"), [
			{ id: "agent:main:web", startedAt: 1776247400 },
		]);

		const { sessions } = await new HermesAdapter().sessions.collect({ kind: "complete" });
		expect(sessions.map((s) => s.localSessionId)).toContain("agent:main:web");
	});

	it("watches every store's WAL sidecars, root store included", () => {
		makeHermesStore(join(tmpHome, ".hermes", "profiles", "work", "state.db"), [
			{ id: "p-work-1", startedAt: 1776247400 },
		]);

		const paths = new HermesAdapter().getSessionsWatchPaths();
		const root = join(tmpHome, ".hermes", "state.db");
		const profile = join(tmpHome, ".hermes", "profiles", "work", "state.db");
		for (const store of [root, profile]) {
			expect(paths).toContain(store);
			// The sidecars Hermes already returned must not be dropped.
			expect(paths).toContain(`${store}-wal`);
			expect(paths).toContain(`${store}-journal`);
			expect(paths).toContain(`${store}-shm`);
		}
		expect(new Set(paths).size).toBe(paths.length);
	});

	it("returns every store's quartet for a large install, with nothing truncated", () => {
		// Same fd-budget property IronHermes is pinned on: the fan-out is exactly
		// 4 paths per store at any size, and a large install comes back whole.
		// Nothing between here and `watchSessions` caps the path count, so a cap
		// introduced later would silently stop watching the tail stores and
		// `clawdi push` would quietly miss their sessions.
		const profileNames = Array.from({ length: 9 }, (_, i) => `p${String(i).padStart(2, "0")}`);
		for (const name of profileNames) {
			makeHermesStore(join(tmpHome, ".hermes", "profiles", name, "state.db"), [
				{ id: `p-${name}-1`, startedAt: 1776247400 },
			]);
		}

		const paths = new HermesAdapter().getSessionsWatchPaths();
		// The fixture install already carries a root store, so 9 profiles = 10.
		const stores = [
			join(tmpHome, ".hermes", "state.db"),
			...profileNames.map((name) => join(tmpHome, ".hermes", "profiles", name, "state.db")),
		];
		const suffixes = ["", "-wal", "-shm", "-journal"];

		expect(paths).toHaveLength(stores.length * suffixes.length);
		expect(paths.sort()).toEqual(
			stores.flatMap((store) => suffixes.map((suffix) => `${store}${suffix}`)).sort(),
		);
	});
});

describe("HermesAdapter.collectSkills", () => {
	it("finds a nested skill at skills/core/demo/SKILL.md and skips SKIP_DIRS at every depth", async () => {
		const a = new HermesAdapter();
		const skills = await a.skills.collect();
		// `core/demo` is the real nested skill. The fixture also plants
		// `skills/node_modules/bad/SKILL.md` — Hermes' scanner recurses, so
		// SKIP_DIRS must apply at the root level AND block the recursion.
		expect(skills).toHaveLength(1);
		expect(skills[0]).toMatchObject({
			skillKey: "core/demo",
			name: "demo",
		});
		expect(skills[0]?.content).toContain("description: A nested demo skill");
	});

	it("discovers safe top-level directory symlinks and isolates unsafe ones", async () => {
		const root = join(tmpHome, ".hermes", "skills");
		const linked = addSkillDirectorySymlinkCases(root, join(tmpHome, "outside-hermes-skill"));
		const adapter = new HermesAdapter();
		const skills = await adapter.skills.collect();
		const keys = skills.map((skill) => skill.skillKey).sort();
		expect(keys).toEqual(["core/demo", "linked", "source/linked-source"]);
		expect(skills.find((skill) => skill.skillKey === "linked")?.directoryPath).toBe(linked);
		expect((await adapter.skills.listKeys()).sort()).toEqual(keys);
	});

	it("skips archived dot-directories and invalid skill keys at every depth", async () => {
		const skillsRoot = join(tmpHome, ".hermes", "skills");
		mkdirSync(join(skillsRoot, ".archive", "old-skill"), { recursive: true });
		writeFileSync(join(skillsRoot, ".archive", "old-skill", "SKILL.md"), "---\nname: old\n---\n");
		mkdirSync(join(skillsRoot, "apple", ".archive", "old-reminders"), { recursive: true });
		writeFileSync(
			join(skillsRoot, "apple", ".archive", "old-reminders", "SKILL.md"),
			"---\nname: old reminders\n---\n",
		);
		mkdirSync(join(skillsRoot, "bad key"), { recursive: true });
		writeFileSync(join(skillsRoot, "bad key", "SKILL.md"), "---\nname: bad key\n---\n");
		mkdirSync(join(skillsRoot, "apple", "_private"), { recursive: true });
		writeFileSync(join(skillsRoot, "apple", "_private", "SKILL.md"), "---\nname: private\n---\n");

		const a = new HermesAdapter();
		const skills = await a.skills.collect();
		const keys = skills.map((s) => s.skillKey).sort();

		expect(keys).toEqual(["core/demo"]);
		expect(await a.skills.listKeys()).toEqual(["core/demo"]);
	});

	it("returns empty when skills dir is missing", async () => {
		// Point HOME at a fresh tmpdir with no .hermes/
		process.env.HOME = `/tmp/clawdi-empty-${Date.now()}`;
		const a = new HermesAdapter();
		expect(await a.skills.collect()).toEqual([]);
	});
});

describe("HermesAdapter.writeSkillArchive + getSkillPath", () => {
	it("extracts a tar.gz round-trip (key matches archive root dir)", async () => {
		// In production, skill.ts derives skillKey from basename(path) and then
		// tars that dir — so key always matches the archive's internal top-level
		// dirname. Test preserves that invariant.
		const srcDir = join(tmpHome, ".hermes", "skills", "core", "demo");
		const tarBytes = await tarSkillDir(srcDir);

		// Remove source first so we can tell it was re-extracted.
		const a = new HermesAdapter();
		await a.skills.writeArchive("demo", tarBytes);

		const extracted = join(tmpHome, ".hermes", "skills", "demo", "SKILL.md");
		expect(existsSync(extracted)).toBe(true);
		expect(readFileSync(extracted, "utf-8")).toContain("description: A nested demo skill");
	});

	it("refuses to write shared content through a managed shared namespace", async () => {
		const skillsRoot = join(tmpHome, ".hermes", "skills");
		const sharedRoot = join(skillsRoot, "shared");
		mkdirSync(sharedRoot, { recursive: true });
		writeFileSync(join(sharedRoot, "SKILL.md"), "# Managed shared namespace\n");
		reserveManagedSkill({
			targetDir: sharedRoot,
			id: "shared",
			version: 1,
			digest: "a".repeat(64),
			manager: "local-setup",
		});
		const tarBytes = await tarSkillDir(join(skillsRoot, "core", "demo"));

		const adapter = new HermesAdapter();
		await expect(adapter.skills.writeSharedArchive("demo", "owner", tarBytes)).rejects.toThrow(
			"Skill shared is reserved by a managed Skill owner",
		);
		expect(readFileSync(join(sharedRoot, "SKILL.md"), "utf8")).toBe("# Managed shared namespace\n");
		expect(existsSync(join(sharedRoot, "demo__owner"))).toBe(false);
	});

	it("getSkillPath returns the canonical SKILL.md anchor under skills/", () => {
		const a = new HermesAdapter();
		const p = a.skills.path("foo");
		expect(p).toBe(join(tmpHome, ".hermes", "skills", "foo", "SKILL.md"));
	});
});
