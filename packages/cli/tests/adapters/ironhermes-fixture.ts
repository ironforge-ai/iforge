import { Database } from "bun:sqlite";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

/**
 * Programmatic IronHermes `state.db` generator.
 *
 * Built in `beforeEach` rather than checked in as a binary. The Hermes fixture
 * checks a `state.db` into the tree, which hides the schema from review and does
 * not scale to profile stores plus a `usage_events` table. The DDL below is the
 * live IronHermes schema verbatim, so a schema drift shows up as a readable diff.
 *
 * Time bases inside this schema differ and the difference is load-bearing:
 *   `sessions.started_at` / `messages.timestamp` — REAL epoch SECONDS
 *   `usage_events.ts`                           — INTEGER epoch MILLISECONDS
 */

const SESSIONS_DDL = `
CREATE TABLE sessions (
    id                  TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    user_id             TEXT,
    model               TEXT,
    system_prompt       TEXT,
    parent_session_id   TEXT,
    started_at          REAL NOT NULL,
    ended_at            REAL,
    end_reason          TEXT,
    message_count       INTEGER DEFAULT 0,
    tool_call_count     INTEGER DEFAULT 0,
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    title               TEXT,
    workspace_root      TEXT,
    cache_read_tokens   INTEGER DEFAULT 0,
    cache_creation_tokens INTEGER DEFAULT 0,
    cost_usd_micros     INTEGER DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
)`;

const MESSAGES_DDL = `
CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    role            TEXT NOT NULL,
    content         TEXT,
    tool_call_id    TEXT,
    tool_calls      TEXT,
    tool_name       TEXT,
    timestamp       REAL NOT NULL,
    token_count     INTEGER,
    finish_reason   TEXT
)`;

const USAGE_EVENTS_DDL = `
CREATE TABLE usage_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    ts               INTEGER NOT NULL,
    provider         TEXT NOT NULL,
    model            TEXT NOT NULL,
    in_tok           INTEGER NOT NULL,
    out_tok          INTEGER NOT NULL,
    cache_read       INTEGER NOT NULL DEFAULT 0,
    cache_create     INTEGER NOT NULL DEFAULT 0,
    cost_usd_micros  INTEGER NOT NULL DEFAULT 0,
    error_kind       TEXT
)`;

export interface FixtureMessage {
	role: "user" | "assistant" | "tool";
	content: string | null;
	/** Epoch SECONDS, matching the live schema's REAL column. */
	timestamp: number;
}

export interface FixtureUsageEvent {
	/** Epoch MILLISECONDS — this column's base differs from the others. */
	ts: number;
	provider: string;
	model: string;
	inTok: number;
	outTok: number;
	cacheRead?: number;
	cacheCreate?: number;
	costUsdMicros?: number;
	errorKind?: string | null;
}

export interface FixtureSession {
	/** Raw id exactly as IronHermes stores it, colons included. */
	id: string;
	source?: string;
	model?: string | null;
	title?: string | null;
	workspaceRoot?: string | null;
	/** Epoch SECONDS. */
	startedAt: number;
	/** Epoch SECONDS, or null for the (common) unrecorded-end case. */
	endedAt?: number | null;
	messageCount?: number | null;
	toolCallCount?: number;
	inputTokens?: number | null;
	outputTokens?: number | null;
	cacheReadTokens?: number | null;
	messages?: FixtureMessage[];
	usageEvents?: FixtureUsageEvent[];
}

export interface FixtureStoreSpec {
	/** Store path relative to the install root, e.g. `state.db` or `profiles/x/state.db`. */
	relativePath: string;
	sessions: FixtureSession[];
	/**
	 * Usage-event rows whose `session_id` names NO session row in this store.
	 *
	 * The source table carries no foreign key and real orphan rows exist in the
	 * live store (Pitfall 8). They cannot be expressed through `FixtureSession`
	 * because those events are always tied to a session that was inserted, so
	 * the orphan case needs its own door.
	 */
	orphanUsageEvents?: Array<FixtureUsageEvent & { sessionId: string }>;
	/** Create the file but leave it at 0 bytes — the Pitfall 1 case. */
	empty?: boolean;
	/** Create the SQLite file but omit the `sessions` table entirely. */
	omitSessionsTable?: boolean;
	/**
	 * Create the SQLite file WITH `sessions` and `messages` but WITHOUT
	 * `usage_events`.
	 *
	 * An IronHermes install predating the telemetry table is still a valid
	 * store full of real sessions. Reading events must not be able to abort
	 * the session read that shares its per-store try block.
	 */
	omitUsageEventsTable?: boolean;
	/**
	 * Create the SQLite file WITH a `sessions` table but WITHOUT `messages`.
	 *
	 * This is the store that passes the enumeration probe and then fails mid-read
	 * — the case a per-store guard has to survive so one bad store cannot abort
	 * the whole pool.
	 */
	omitMessagesTable?: boolean;
}

export interface IronHermesInstallSpec {
	stores: FixtureStoreSpec[];
	/**
	 * Directories to create under the install root that carry no `state.db`.
	 *
	 * The live install has 8 profile directories and only 5 of them hold a
	 * store, so "profile directory without a store" is the majority-adjacent
	 * case, not an exotic one. A walk that assumes every profile directory has
	 * a store throws on the real machine.
	 */
	emptyDirs?: string[];
}

/**
 * Materialize an IronHermes install rooted at `root`, returning the root.
 *
 * `root` is the install directory itself (`<tmpHome>/.ironhermes`), not `$HOME`.
 */
export function makeIronHermesInstall(root: string, spec: IronHermesInstallSpec): string {
	mkdirSync(root, { recursive: true });
	for (const dir of spec.emptyDirs ?? []) {
		mkdirSync(join(root, dir), { recursive: true });
	}
	for (const store of spec.stores) {
		const dbPath = join(root, store.relativePath);
		mkdirSync(dirname(dbPath), { recursive: true });

		if (store.empty) {
			// A genuinely 0-byte file: what `sqlite3 <path>` leaves behind when a
			// session is opened and closed without writing. `existsSync` says yes.
			writeFileSync(dbPath, "");
			continue;
		}

		const db = new Database(dbPath, { create: true });
		try {
			if (!store.omitMessagesTable) db.run(MESSAGES_DDL);
			if (!store.omitUsageEventsTable) db.run(USAGE_EVENTS_DDL);
			if (store.omitSessionsTable) continue;
			db.run(SESSIONS_DDL);

			const insertSession = db.prepare(`
				INSERT INTO sessions (
					id, source, model, title, workspace_root, started_at, ended_at,
					message_count, tool_call_count, input_tokens, output_tokens, cache_read_tokens
				) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			`);
			const insertMessage = store.omitMessagesTable
				? null
				: db.prepare(`
				INSERT INTO messages (session_id, role, content, timestamp)
				VALUES (?, ?, ?, ?)
			`);
			const insertEvent = store.omitUsageEventsTable
				? null
				: db.prepare(`
				INSERT INTO usage_events (
					session_id, ts, provider, model, in_tok, out_tok,
					cache_read, cache_create, cost_usd_micros, error_kind
				) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			`);

			for (const s of store.sessions) {
				insertSession.run(
					s.id,
					s.source ?? "cli",
					s.model ?? null,
					s.title ?? null,
					s.workspaceRoot ?? null,
					s.startedAt,
					s.endedAt ?? null,
					s.messageCount ?? null,
					s.toolCallCount ?? 0,
					s.inputTokens ?? 0,
					s.outputTokens ?? 0,
					s.cacheReadTokens ?? 0,
				);
				for (const m of s.messages ?? []) {
					insertMessage?.run(s.id, m.role, m.content, m.timestamp);
				}
				for (const e of s.usageEvents ?? []) {
					insertEvent?.run(
						s.id,
						e.ts,
						e.provider,
						e.model,
						e.inTok,
						e.outTok,
						e.cacheRead ?? 0,
						e.cacheCreate ?? 0,
						e.costUsdMicros ?? 0,
						e.errorKind ?? null,
					);
				}
			}
			for (const e of store.orphanUsageEvents ?? []) {
				insertEvent?.run(
					e.sessionId,
					e.ts,
					e.provider,
					e.model,
					e.inTok,
					e.outTok,
					e.cacheRead ?? 0,
					e.cacheCreate ?? 0,
					e.costUsdMicros ?? 0,
					e.errorKind ?? null,
				);
			}
		} finally {
			db.close();
		}
	}
	return root;
}

/** Two messages (one user, one assistant) anchored at `startedAt`. */
export function twoMessages(startedAt: number): FixtureMessage[] {
	return [
		{ role: "user", content: "hello", timestamp: startedAt },
		{ role: "assistant", content: "hi there", timestamp: startedAt + 1 },
	];
}
