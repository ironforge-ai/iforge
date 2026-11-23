import { createHash } from "node:crypto";
import { existsSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { safeTruncate } from "../lib/sanitize";
import { durationSecondsBetween } from "../lib/session-duration";
import { isValidSkillKey } from "../lib/skill-key";
import { replaceSkillArchiveTarGz } from "../lib/tar";
import { managedSkillDirectoryDigest } from "../runtime/hosted-bundled-skill";
import {
	migrateLegacyLocalSetupSkill,
	mutateUserSkillTarget,
	shouldIgnoreUserSkill,
} from "../runtime/managed-skill-reservation";
import {
	assertAgentRootContains,
	assertAgentRootIsolated,
	isAgentRootIsolated,
} from "./agent-root-guard";
import type {
	AgentAdapterCore,
	RawSession,
	RawSkill,
	RawUsageEvent,
	SessionBatchScan,
	SessionMessage,
	SessionModule,
	SessionScanBatch,
	SessionScanRequest,
	SessionScanResult,
	SkillModule,
} from "./base";
import { listAgentStoreCandidatePaths, listAgentStorePaths } from "./hermes-profiles";
import { getHermesHome, getIronHermesHome, SKIP_DIRS } from "./paths";
import { openReadonlySqlite, type ReadonlySqliteDatabase, type SqliteStatement } from "./sqlite";
import { readCommandVersion } from "./version";

/** Bracketed prefix every store-walk warning carries for this runtime. */
const STORE_OPTS = { label: "ironhermes" } as const;

function ironHermesDir() {
	return getIronHermesHome();
}
function stateDbPath() {
	return join(ironHermesDir(), "state.db");
}
/**
 * The ONE skills root, for reads and for writes.
 *
 * D-13 (read-many/write-one, the OpenClaw precedent): sessions pool across
 * every store in the install, but skills resolve a single workspace. This
 * deliberately never consults the store enumeration — `profiles/<name>/skills/`
 * holds a small fixed overlay that already exists at root, so it is not
 * user-authored content and is neither collected nor written.
 *
 * D-09: two sibling trees are excluded by never being a root here, rather than
 * by a special-case branch. The catalog directory upstream ships beside this
 * one holds skills that are NOT active until installed explicitly — reporting
 * them would tell the user their agent has capabilities it does not have
 * (T-02-14). `sessions/` holds trajectory files (tool-call traces); the
 * conversation surface is the store's `messages` table, not a directory.
 * Both are pinned by test in `tests/adapters/ironhermes-skills.test.ts`.
 */
function skillsDir() {
	return join(ironHermesDir(), "skills");
}

function shouldSkipIronHermesSkillDir(entryName: string): boolean {
	return entryName.startsWith(".") || SKIP_DIRS.has(entryName);
}

/**
 * D-21: every IronHermes mutation target, checked at RUNTIME.
 *
 * Two assertions, because they catch different failures. Containment catches a
 * target that escapes this install; isolation catches the install root ITSELF
 * having been pointed at upstream Hermes, which containment cannot see —
 * the target is faithfully inside the root, the root is just the wrong tree.
 * `$IRONHERMES_HOME` and `$HERMES_HOME` are user-controlled, and upstream
 * supports its variable pointed directly at a profile directory, so this is a
 * configuration a user can reach without doing anything obviously wrong.
 */
function assertIronHermesWriteTarget(targetPath: string): void {
	const root = ironHermesDir();
	assertAgentRootIsolated(root, foreignRoots(), "ironhermes Skill write");
	assertAgentRootContains(root, targetPath, "ironhermes Skill write");
}

/** The live runtimes this one must never resolve inside. */
function foreignRoots(): string[] {
	return [getHermesHome()];
}

/**
 * T-02-05: the READ half of D-21.
 *
 * The isolation guard was wired into every mutation and no read, which left
 * `$IRONHERMES_HOME` pointed at a Hermes install free to report IronHermes
 * present, collect upstream Hermes's sessions, and upload them under the
 * IronHermes identity. Nothing is written into the wrong tree — it is read out
 * of one. That is conflation of the user's own data rather than cross-tenant
 * disclosure, but conflation is exactly what the phase goal exists to prevent.
 *
 * Every collect path THROWS, and that asymmetry against `detect()` is the whole
 * design: a silent skip uploads nothing while a silent pass uploads another
 * runtime's data, so this fails closed. Callers already contain it —
 * `sync-engine.ts:onSessionsStable` catches and records sync health, and
 * `commands/push.ts` surfaces it through `handleError`.
 *
 * Returns the root so callers resolve it exactly once, rather than checking one
 * value and then reading another.
 */
function assertIronHermesReadRoot(context: string): string {
	const root = ironHermesDir();
	assertAgentRootIsolated(root, foreignRoots(), context);
	return root;
}

function ironHermesSkillKeyFromPath(fullPath: string): string | null {
	// The key is the exact byte sequence the directory entry gave us. No
	// Unicode normalization and no case folding are applied here, and that is
	// deliberate — do not "fix" it.
	//
	// A macOS filesystem can hand back a decomposed form (e + U+0301) where the
	// same name typed elsewhere composes (U+00E9), so two logically identical
	// names can arrive as different byte sequences. Normalizing at the adapter
	// would silently merge two real directories into one key and drop one
	// skill; not normalizing surfaces what is actually on disk. `isValidSkillKey`
	// is the only filter — its pattern is ASCII-only, so in practice both
	// normalization forms are rejected identically rather than one being folded
	// into the other. Case is preserved because the pattern admits `A-Z`.
	const skillKey = relative(skillsDir(), fullPath).replaceAll("\\", "/");
	return isValidSkillKey(skillKey) ? skillKey : null;
}

/**
 * Extract a plain model name string from the IronHermes model field.
 * The field can be a plain string ("claude-opus-4.6") or a JSON object
 * ({"default": "gpt-5.3-codex", "provider": "openai-codex", ...}).
 *
 * Never return the raw blob: it can carry provider headers including API keys,
 * and the model value is rendered in the dashboard.
 *
 * TRIM BEFORE the brace test, and accept `[` as well (T-02-26). This gate used
 * to run on untrimmed input, so a blob with one leading space, newline or tab
 * was not recognised as a blob and was returned whole — secret included. The
 * server's `_clean_model_value` has always stripped first, which is why the
 * two controls were never redundant: this was the shape only one of them
 * caught, and for a while the other one was missing entirely.
 *
 * Keep this function and `clean_model_value` in `backend/app/schemas/session.py`
 * in agreement. They are two implementations of one policy in two languages,
 * and the whitespace bug is what disagreement looks like.
 */
function parseModelField(raw: string | null): string | null {
	if (!raw) return null;
	const value = raw.trim();
	if (!value) return null;
	if (value.startsWith("{") || value.startsWith("[")) {
		try {
			const parsed = JSON.parse(value);
			// A nested object under `default` is NOT a model name. Returning it
			// used to hand an object to `safeTruncate`, which threw
			// `str is not iterable` and took down the whole store read rather
			// than just this row.
			const candidate = parsed?.default ?? parsed?.model;
			return typeof candidate === "string" && candidate.trim() ? candidate.trim() : null;
		} catch {
			return /['"](?:default|model)['"]\s*:\s*['"]([^'"]+)['"]/.exec(value)?.[1]?.trim() ?? null;
		}
	}
	return value;
}

/**
 * The DISTINCT set of models this session's recorded calls used (D-20).
 *
 * Insertion-ordered so the emitted array is stable for an unchanged store.
 * With no events, falls back to the session's own model as a one-element set
 * rather than an empty one — the field must never regress below what the
 * tracer already produced for a session that simply has no telemetry.
 */
function distinctModels(events: RawUsageEvent[], sessionModel: string | null): string[] {
	const seen = new Set<string>();
	for (const e of events) if (e.model) seen.add(e.model);
	if (seen.size > 0) return [...seen];
	return sessionModel ? [sessionModel] : [];
}

/**
 * Fingerprint a session's whole event set, mirroring how sessions already
 * fingerprint their message content.
 *
 * Rows are serialized field-by-field and then SORTED BY CONTENT before
 * hashing, so an unstable row order out of SQLite cannot flap the hash and
 * cause a spurious re-push. The source table's `id` is deliberately excluded:
 * it is a local autoincrement a store rebuild or vacuum can renumber, so
 * including it would make the hash move when nothing the user cares about
 * changed — and keying dedupe on it directly would silently double-count
 * recorded spend (D-22).
 *
 * Undefined for an empty set, so "this session has no events" stays
 * distinguishable from "this session's events hash to X".
 */
function usageEventSetHash(events: RawUsageEvent[]): string | undefined {
	if (events.length === 0) return undefined;
	const rows = events
		.map((e) =>
			JSON.stringify([
				e.ts.getTime(),
				e.provider,
				e.model,
				e.inTok,
				e.outTok,
				e.cacheRead,
				e.cacheCreate,
				e.costUsdMicros,
				e.errorKind,
			]),
		)
		.sort();
	return createHash("sha256").update(JSON.stringify(rows)).digest("hex");
}

/**
 * Make an IronHermes session id safe for the server's `local_session_id`
 * contract, `^[A-Za-z0-9][A-Za-z0-9._\-]{0,199}$`.
 *
 * IronHermes ids are composite — `agent:main:web:dm:<uuid>` — and the colon is
 * outside the permitted charset. That pattern is a security control: the value
 * becomes a path component of the object-store key
 * `sessions/{user_id}/{local_session_id}.json`, so it is NOT relaxed
 * server-side. `POST /v1/sessions/batch` validates a *list*, so one rejected id
 * 422s the whole 500-session chunk — the transform must happen here.
 *
 * Colon → period, globally. Chosen over stripping the composite prefix because
 * it is total (assumes nothing about id shape), lossless and reversible. The
 * untransformed id survives on `rawFilePath` as `<storePath>#<rawId>`.
 * Observed max id length is 54, well inside the 200-char cap.
 */
export function sanitizeIronHermesSessionId(rawId: string): string {
	return rawId.replaceAll(":", ".");
}

interface SessionRow {
	id: string;
	source: string | null;
	model: string | null;
	title: string | null;
	workspace_root: string | null;
	started_at: number;
	ended_at: number | null;
	message_count: number | null;
	tool_call_count: number | null;
	input_tokens: number | null;
	output_tokens: number | null;
	cache_read_tokens: number | null;
}

interface MessageRow {
	role: string;
	content: string;
	timestamp: number;
}

interface UsageEventRow {
	ts: number;
	provider: string | null;
	model: string | null;
	in_tok: number | null;
	out_tok: number | null;
	cache_read: number | null;
	cache_create: number | null;
	cost_usd_micros: number | null;
	error_kind: string | null;
}

interface StateRow {
	state: string | null;
}

const SESSION_COLUMNS = `
	id, source, model, title, workspace_root, started_at, ended_at,
	message_count, tool_call_count, input_tokens, output_tokens, cache_read_tokens
`;

/** Rows per `SessionScanBatch`, as upstream Hermes pages its store (#1229). */
const IRONHERMES_SESSION_SCAN_BATCH_SIZE = 32;
/** Bump when persisted IronHermes rows map to different Session bytes. */
const IRONHERMES_SESSION_PROJECTION_REVISION = 1;

/**
 * The statements one store needs to materialize and fingerprint its sessions,
 * prepared once per store.
 */
interface StoreReaders {
	messages: SqliteStatement;
	messageState: SqliteStatement;
	/**
	 * Null when the store predates the telemetry table. A store with no
	 * `usage_events` is still a store full of real sessions, so a missing table
	 * must not throw into the per-store catch, which would drop every session in
	 * the store rather than just its (nonexistent) events.
	 */
	usageEvents: SqliteStatement | null;
	usageEventState: SqliteStatement | null;
}

function storeReaders(db: ReadonlySqliteDatabase): StoreReaders {
	// The same filter the materializer applies, so the fingerprint covers exactly
	// the rows a session's snapshot is built from.
	const messageFilter = "session_id = ? AND role IN ('user', 'assistant') AND content IS NOT NULL";
	let usageEvents: SqliteStatement | null = null;
	let usageEventState: SqliteStatement | null = null;
	try {
		// Prepared per store and per session, NOT as one flat query joined
		// afterwards (Pitfall 8). Keying on the raw session id inside the session
		// loop is what naturally emits events only for sessions this adapter is
		// actually emitting — real orphan rows exist in the live store (the source
		// table carries no foreign key), and the server-side foreign key is real,
		// so an unfiltered orphan would 404 a whole batch.
		usageEvents = db.prepare(`
			SELECT ts, provider, model, in_tok, out_tok,
			       cache_read, cache_create, cost_usd_micros, error_kind
			FROM usage_events
			WHERE session_id = ?
			ORDER BY ts ASC, id ASC
		`);
		// Every value the event set hash covers, never the local autoincrement id
		// (D-22): a vacuum that renumbers rows must not look like a change.
		usageEventState = db.prepare(`
			SELECT json_group_array(
			         json_array(ts, provider, model, in_tok, out_tok,
			                    cache_read, cache_create, cost_usd_micros, error_kind)
			         ORDER BY ts, id
			       ) AS state
			FROM usage_events
			WHERE session_id = ?
		`);
	} catch {
		// No `usage_events` table in this store. Sessions still collect.
	}
	return {
		messages: db.prepare(`
			SELECT role, content, timestamp
			FROM messages
			WHERE ${messageFilter}
			ORDER BY timestamp ASC
		`),
		// IronHermes appends message rows under a stable INTEGER id, as upstream
		// Hermes does, so the fingerprint keeps ids, roles and timestamps exact and
		// reduces each payload to its byte length. SQLite answers octet_length from
		// the record header without reading overflow pages.
		messageState: db.prepare(`
			SELECT json_group_array(
			         json_array(id, role, timestamp, octet_length(content)) ORDER BY id
			       ) AS state
			FROM messages
			WHERE ${messageFilter}
		`),
		usageEvents,
		usageEventState,
	};
}

/**
 * Opaque per-session revision for the #1229 incremental scan.
 *
 * A session whose revision matches the one the daemon last confirmed is not
 * materialized again. The usage-event state is part of it, so an event-only
 * change (phase 02 D-22: messages untouched, recorded calls moved) still
 * produces a new revision and is never skipped as unchanged.
 */
function sessionSourceRevision(row: SessionRow, readers: StoreReaders): string {
	const messages = (readers.messageState.get(row.id) as StateRow | undefined)?.state ?? "[]";
	const events = readers.usageEventState
		? ((readers.usageEventState.get(row.id) as StateRow | undefined)?.state ?? "[]")
		: null;
	return createHash("sha256")
		.update(
			JSON.stringify([
				IRONHERMES_SESSION_PROJECTION_REVISION,
				row.id,
				row.source,
				row.model,
				row.title,
				row.workspace_root,
				row.started_at,
				row.ended_at,
				row.message_count,
				row.tool_call_count,
				row.input_tokens,
				row.output_tokens,
				row.cache_read_tokens,
				messages,
				events,
			]),
		)
		.digest("hex");
}

/**
 * D-03: prefix-match project filtering, same form as Codex/OpenClaw. Resolved
 * once per scan — the filter applies to the pooled result, not per store.
 */
function projectFilterMatcher(projectFilter: string | undefined): (row: SessionRow) => boolean {
	const absFilter = projectFilter ? resolve(projectFilter) : null;
	return (row) => {
		if (!absFilter) return true;
		const projectPath = row.workspace_root;
		if (!projectPath) return false;
		return projectPath === absFilter || projectPath.startsWith(`${absFilter}/`);
	};
}

/**
 * Build one session's snapshot from its store row, or null when it has nothing
 * to carry (no extractable message and no recorded call).
 */
function materializeSession(
	row: SessionRow,
	readers: StoreReaders,
	storePath: string,
	sourceRevision: string,
): RawSession | null {
	// D-02 / IRON-04: whatever IronHermes recorded, verbatim. The column is
	// nullable TEXT with no default and no empty-string rows, so `??` is exact.
	// No filtering, no `~` substitution, no home-directory special case — a bare
	// home path IS the recorded attribution, and second-guessing it would make
	// this the only adapter that rewrites its own source data. D-01: `source` is
	// a channel/origin tag ("telegram"), never a path — it is never consulted here.
	const projectPath = row.workspace_root ?? null;

	const model = parseModelField(row.model);
	// `sessions.started_at` / `messages.timestamp` are REAL epoch SECONDS, hence
	// the ×1000 here and below. `usage_events.ts` is INTEGER epoch MILLISECONDS
	// and is NOT multiplied — see the event read further down.
	const startedAt = new Date(row.started_at * 1000);
	// Absence stays absence. Most IronHermes rows have no recorded end; deriving
	// one from the last message timestamp would present an estimate as a
	// measurement, so `durationSeconds` stays null too.
	const endedAt = row.ended_at ? new Date(row.ended_at * 1000) : null;
	const durationSeconds = durationSecondsBetween(startedAt, endedAt);

	const msgRows = readers.messages.all(row.id) as MessageRow[];
	const messages: SessionMessage[] = msgRows.map((m) => ({
		role: m.role as "user" | "assistant",
		content: m.content,
		model: m.role === "assistant" ? (model ?? undefined) : undefined,
		timestamp: new Date(m.timestamp * 1000).toISOString(),
	}));

	// Summary: use title or first user message. `title` is NULL for every live
	// IronHermes row today, so the fallback is the usual path; the "New Chat"
	// handling is kept because it is free and stays correct if IronHermes starts
	// writing titles.
	let summary = row.title;
	if (!summary || summary === "New Chat" || summary.startsWith("New Chat #")) {
		const firstUser = messages.find((m) => m.role === "user");
		summary = firstUser ? safeTruncate(firstUser.content, 200) : null;
	}

	// Keyed on the RAW id: `usage_events.session_id` holds the composite form,
	// and the sanitized id only exists on the wire.
	//
	// FAIL OPEN on the row (D-18). Every event is emitted — including ones
	// carrying an `error_kind` and ones whose `provider` is the empty string.
	// Failed calls still burned tokens and cost, so excluding them makes the
	// totals wrong in the user's favour, which is the harder error to notice. No
	// validation enum for `error_kind`: the live vocabulary already exceeds the
	// recorded design and IronHermes owns it. No placeholder for a blank provider
	// either — carry the blank through and let the display layer decide.
	//
	// `sessions.cost_usd_micros` is never read (D-15, Pitfall 9). Cost lives only
	// on event rows; the session column provably disagrees with the sum of its
	// own events on live data, and ingesting both would give one question two
	// answers.
	const evtRows = (readers.usageEvents?.all(row.id) ?? []) as UsageEventRow[];
	const usageEvents: RawUsageEvent[] = evtRows.map((e) => ({
		// NOT multiplied by 1000. This column is epoch MILLISECONDS while the
		// session and message columns above are epoch seconds — the two tables
		// genuinely differ inside one schema. Applying the seconds reflex here
		// yields a date in the year 58600 and nothing errors, which is why the
		// adjacent code's ×1000 is not a template.
		ts: new Date(e.ts),
		// Truncated to the server's own caps (02-REVIEW WR-10). These are free text
		// from a store this CLI does not own, and the whole batch is one request —
		// one over-long value 422s every event beside it, and push.ts records no
		// event-set hash on failure, so the push retries and fails identically
		// forever. `safeTruncate` cuts on a codepoint boundary, which matches how
		// Pydantic counts length.
		provider: e.provider == null ? e.provider : safeTruncate(e.provider, 100),
		// Parsed BEFORE truncation (T-02-26). `usage_events.model` is the same
		// free-text column shape as `sessions.model`, which is already parsed at
		// the session read above — a config blob there can carry provider headers
		// including API keys, and this value is rendered in the dashboard.
		// Truncating a blob would ship a *prefix* of the secret rather than none of
		// it, so the parse has to come first. `parseModelField` returns null for a
		// blob with no extractable model; "" is the correct wire value for that,
		// and the server now accepts it (CR-01).
		model: safeTruncate(parseModelField(e.model ?? null) ?? "", 200),
		inTok: e.in_tok ?? 0,
		outTok: e.out_tok ?? 0,
		cacheRead: e.cache_read ?? 0,
		cacheCreate: e.cache_create ?? 0,
		costUsdMicros: e.cost_usd_micros ?? 0,
		errorKind: e.error_kind == null ? e.error_kind : safeTruncate(e.error_kind, 200),
	}));

	// D-19, IronHermes ONLY: a session with no extractable messages but at least
	// one recorded call is kept. A meaningful share of recorded spend sits on
	// message-less sessions, and dropping them would drop that spend. `hermes.ts`
	// keeps its unconditional skip — the two runtimes have diverged and this
	// relaxation is not shared.
	if (messages.length === 0 && usageEvents.length === 0) return null;

	return {
		localSessionId: sanitizeIronHermesSessionId(row.id),
		projectPath,
		startedAt,
		endedAt,
		messageCount: row.message_count ?? messages.length,
		toolCallCount: row.tool_call_count ?? 0,
		inputTokens: row.input_tokens ?? 0,
		outputTokens: row.output_tokens ?? 0,
		cacheReadTokens: row.cache_read_tokens ?? 0,
		model,
		// D-20: the DISTINCT set of this session's event models, not the
		// single-element construction copied from Hermes. `model` stays the session
		// row's own value — that column is the primary/current model and the events
		// give the set. With no events, fall back to the session model so the field
		// never regresses below what the tracer already produced.
		modelsUsed: distinctModels(usageEvents, model),
		usageEvents,
		usageEventSetHash: usageEventSetHash(usageEvents),
		durationSeconds,
		summary,
		messages,
		// The DB is shared across sessions — anchor to the row id so the pointer
		// identifies the specific session rather than the whole store, and to THIS
		// store rather than the root, so a pooled session still says which database
		// produced it. Uses the RAW id: this is where the sanitized id stays a
		// reversible pointer back into the store.
		rawFilePath: `${storePath}#${row.id}`,
		sourceRevision,
	};
}

/**
 * IronHermes on upstream's #1229 modular adapter contract: adapter core plus a
 * session module and a skill module.
 *
 * Sessions stay on the `snapshot-v1` content protocol. The events-v1 wire
 * vocabulary (`SessionEventSource.adapter`, server-side) does not name this
 * runtime, and the phase 02 projection (user and assistant rows with content,
 * the D-19 message-less sessions that carry spend) is what the snapshot keeps.
 * The #1229 scan contract is adopted in full: pooled stores are paged into
 * `SessionScanBatch`es, and each session carries a `sourceRevision` so the
 * daemon's incremental scan skips sessions it has already confirmed.
 */
export class IronHermesAdapter implements AgentAdapterCore {
	readonly agentType = "ironhermes" as const;
	readonly sessions: SessionModule = {
		contentProtocol: async () => "snapshot-v1",
		collect: (request: SessionScanRequest) => this.collectSessions(request),
		scan: (request: SessionScanRequest, knownSourceRevisions: ReadonlyMap<string, string>) =>
			this.scanSessions(request, knownSourceRevisions),
		resolve: (localSessionId: string) => this.resolveSession(localSessionId),
		watchPaths: () => this.getSessionsWatchPaths(),
	};
	readonly skills: SkillModule = {
		collect: () => this.collectSkills(),
		listKeys: () => this.listSkillKeys(),
		path: (key: string) => this.getSkillPath(key),
		rootDir: () => this.getSkillsRootDir(),
		sharedPath: (skillKey: string, ownerHandle: string) =>
			this.getSharedSkillPath(skillKey, ownerHandle),
		writeArchive: (key: string, tarGzBytes: Buffer) => this.writeSkillArchive(key, tarGzBytes),
		writeSharedArchive: (key: string, ownerHandle: string, tarGzBytes: Buffer) =>
			this.writeSharedSkillArchive(key, ownerHandle, tarGzBytes),
		remove: (key: string) => this.removeLocalSkill(key),
	};

	async detect(): Promise<boolean> {
		// One expression carrying three requirements, because the enumerator
		// already applies the `sessions`-table probe to every candidate:
		//   D-06      — probe the store, not the directory. `existsSync` alone
		//               passes for a 0-byte `state.db` (which `sqlite3 <path>`
		//               creates just by opening it) and the read then throws
		//               `no such table: sessions` mid-push.
		//   D-11      — ANY store in the install is enough, including the
		//               profile-only case where the root store never existed.
		//   Pitfall 1 — a zero-byte store is not a detection.
		//
		// The isolation check is the ONE place in this adapter that answers rather
		// than throws (T-02-05). `lib/select-adapter.ts` maps every adapter's
		// detect() into a single `Promise.all`, and none of its three call sites
		// (select-adapter.ts, doctor.ts, setup.ts) wraps it in try/catch — so a
		// throw here would reject the whole batch and stop `iforge push` working
		// for Claude Code, Codex and OpenClaw too. `false` is also simply the
		// truthful answer: a root that fails containment is not a correctly
		// installed IronHermes. Pinned by test; do not "simplify" it to a throw.
		const root = ironHermesDir();
		if (!isAgentRootIsolated(root, foreignRoots())) {
			console.warn(
				`[ironhermes] not detected: the install root ${root} resolves inside another runtime's install root (${foreignRoots().join(", ")}); check $IRONHERMES_HOME and $HERMES_HOME`,
			);
			return false;
		}
		return (await listAgentStorePaths(root, STORE_OPTS)).length > 0;
	}

	async getVersion(): Promise<string | null> {
		return readCommandVersion("ironhermes", ["--version"]);
	}

	private async collectSessions(request: SessionScanRequest): Promise<SessionScanResult> {
		const scan = await this.scanSessions(request, new Map());
		const sessions: RawSession[] = [];
		let dedupedCount = 0;
		for await (const batch of scan.batches) {
			sessions.push(...batch.sessions);
			dedupedCount += batch.dedupedCount;
		}
		// One row per session with a stable id — no resume-chain duplication.
		return { sessions, dedupedCount, coverage: scan.coverage };
	}

	private async scanSessions(
		request: SessionScanRequest,
		knownSourceRevisions: ReadonlyMap<string, string>,
	): Promise<SessionBatchScan> {
		// IronHermes state is profile-scoped: `<root>/state.db` plus
		// `<root>/profiles/<name>/state.db`. Every store pools into ONE iforge
		// agent (D-11) — no per-profile agent, and no namespacing scheme on
		// `localSessionId`: session ids were checked for collisions root-to-profile
		// and profile-to-profile with zero overlap in every pair, so pooling is
		// identity-safe as-is. Reading only the root drops 37% of recorded
		// activity, and the profile stores are the better-attributed ones.
		//
		// A `paths` request names watcher paths, which for this runtime are SQLite
		// stores and their sidecars: a store has no per-row stat info, so every
		// request scans every store's whole `sessions` table (cost is negligible,
		// dozens to hundreds of rows), exactly as upstream Hermes does. Unlike
		// Hermes, `projectFilter` is honored (D-03).
		const root = assertIronHermesReadRoot("ironhermes session read");
		const storePaths = await listAgentStorePaths(root, STORE_OPTS);
		if (storePaths.length === 0) {
			return { coverage: "complete", batches: (async function* () {})() };
		}
		const matchesProject = projectFilterMatcher(request.projectFilter);
		// Per-store guard: one corrupt, locked or mid-migration store must not
		// abort the pool. Reading a live WAL database read-only can also fail
		// transiently — the reader needs the `-shm`/`-wal` sidecars readable, and
		// can still see a busy error during WAL coordination — so the guard is the
		// answer to both. A skipped store makes the scan partial, so its sessions
		// are never treated as absent by the daemon's health bookkeeping.
		const stores: Array<{ path: string; db: ReadonlySqliteDatabase }> = [];
		let complete = true;
		for (const storePath of storePaths) {
			try {
				stores.push({ path: storePath, db: await openReadonlySqlite(storePath) });
			} catch (e) {
				complete = false;
				console.warn(
					`[ironhermes] could not open ${storePath}: ${e instanceof Error ? e.message : String(e)}`,
				);
			}
		}
		const scan: SessionBatchScan = {
			coverage: complete ? "complete" : "partial",
			batches: this.readPooledSessionBatches(stores, knownSourceRevisions, matchesProject, () => {
				scan.coverage = "partial";
			}),
		};
		return scan;
	}

	private async *readPooledSessionBatches(
		stores: Array<{ path: string; db: ReadonlySqliteDatabase }>,
		knownSourceRevisions: ReadonlyMap<string, string>,
		matchesProject: (row: SessionRow) => boolean,
		markPartial: () => void,
	): AsyncGenerator<SessionScanBatch> {
		let index = 0;
		try {
			for (; index < stores.length; index++) {
				const store = stores[index] as { path: string; db: ReadonlySqliteDatabase };
				try {
					yield* this.readSessionBatches(
						store.db,
						store.path,
						knownSourceRevisions,
						matchesProject,
					);
				} catch (e) {
					// The store passed the enumeration probe and still failed to read —
					// a missing `messages` table, a mid-migration schema, a busy WAL.
					// Surface it and keep pooling the remaining stores; a silent skip
					// here is a push that reports success while dropping a whole store.
					markPartial();
					console.warn(
						`[ironhermes] could not read ${store.path}: ${e instanceof Error ? e.message : String(e)}`,
					);
				}
			}
		} finally {
			// A consumer that stops early leaves later stores unread; close them here
			// (readSessionBatches closes each store it ran).
			for (let rest = index + 1; rest < stores.length; rest++) stores[rest]?.db.close();
		}
	}

	private async *readSessionBatches(
		db: ReadonlySqliteDatabase,
		storePath: string,
		knownSourceRevisions: ReadonlyMap<string, string>,
		matchesProject: (row: SessionRow) => boolean,
	): AsyncGenerator<SessionScanBatch> {
		try {
			const readers = storeReaders(db);
			let cursor: Pick<SessionRow, "started_at" | "id"> | null = null;
			while (true) {
				const rows = db
					.prepare(`
						SELECT ${SESSION_COLUMNS}
						FROM sessions
						${cursor ? "WHERE started_at < ? OR (started_at = ? AND id < ?)" : ""}
						ORDER BY started_at DESC, id DESC
						LIMIT ?
					`)
					.all(
						...(cursor ? [cursor.started_at, cursor.started_at, cursor.id] : []),
						IRONHERMES_SESSION_SCAN_BATCH_SIZE,
					) as SessionRow[];
				if (rows.length === 0) return;

				const observedLocalSessionIds: string[] = [];
				const sessions: RawSession[] = [];
				for (const row of rows) {
					if (!matchesProject(row)) continue;
					const localSessionId = sanitizeIronHermesSessionId(row.id);
					observedLocalSessionIds.push(localSessionId);
					const sourceRevision = sessionSourceRevision(row, readers);
					if (knownSourceRevisions.get(localSessionId) === sourceRevision) continue;
					const session = materializeSession(row, readers, storePath, sourceRevision);
					if (session) sessions.push(session);
				}
				yield { sessions, observedLocalSessionIds, dedupedCount: 0 };
				const last = rows.at(-1);
				if (!last || rows.length < IRONHERMES_SESSION_SCAN_BATCH_SIZE) return;
				cursor = last;
			}
		} finally {
			db.close();
		}
	}

	/**
	 * Re-read one session from its current store (#1114).
	 *
	 * `localSessionId` is the id on the wire, i.e. the SANITIZED form
	 * (`sanitizeIronHermesSessionId`), while the store keys rows by the raw
	 * composite id. Colon → period is not invertible in general, so the lookup
	 * applies the same total transform to the stored id in SQL rather than
	 * guessing a raw id from the sanitized one. The id may live in any profile
	 * store; the first store holding it wins, in the order the scan pools them.
	 */
	private async resolveSession(localSessionId: string): Promise<RawSession | null> {
		const root = assertIronHermesReadRoot("ironhermes session read");
		for (const storePath of await listAgentStorePaths(root, STORE_OPTS)) {
			let db: ReadonlySqliteDatabase;
			try {
				db = await openReadonlySqlite(storePath);
			} catch (e) {
				console.warn(
					`[ironhermes] could not open ${storePath}: ${e instanceof Error ? e.message : String(e)}`,
				);
				continue;
			}
			try {
				// `replace(id, ':', '.')` is `sanitizeIronHermesSessionId` in SQL:
				// SQLite's replace() substitutes every occurrence, as replaceAll does.
				const row = db
					.prepare(`
						SELECT ${SESSION_COLUMNS}
						FROM sessions
						WHERE replace(id, ':', '.') = ?
						ORDER BY started_at DESC
						LIMIT 1
					`)
					.get(localSessionId) as SessionRow | undefined;
				if (!row) continue;
				const readers = storeReaders(db);
				const session = materializeSession(
					row,
					readers,
					storePath,
					sessionSourceRevision(row, readers),
				);
				if (session) return session;
			} catch (e) {
				console.warn(
					`[ironhermes] could not read ${storePath}: ${e instanceof Error ? e.message : String(e)}`,
				);
			} finally {
				db.close();
			}
		}
		return null;
	}

	private async collectSkills(): Promise<RawSkill[]> {
		// Before `migrateLegacyLocalSetupSkill`, not after: that call takes the
		// ledger write lock and can write, so a guard placed below it would let a
		// read path mutate the foreign tree on its way to refusing (WR-05).
		assertIronHermesReadRoot("ironhermes Skill read");
		// Upstream's own local-setup-skill migration: `migrateLegacyLocalSetupSkill` pins the
		// `clawdi` directory and id.
		migrateLegacyLocalSetupSkill({
			targetDir: join(skillsDir(), "clawdi"),
			id: "clawdi",
			version: 1,
			digest: managedSkillDirectoryDigest,
		});
		if (!existsSync(skillsDir())) return [];

		const skills: RawSkill[] = [];
		this._scanSkillsDir(skillsDir(), skills);
		return skills;
	}

	/**
	 * Recursively scan for directories containing SKILL.md.
	 * IronHermes skills are nested: skills/category/skill-name/SKILL.md
	 */
	private _scanSkillsDir(dir: string, results: RawSkill[]): void {
		for (const entry of readdirSync(dir, { withFileTypes: true })) {
			if (!entry.isDirectory()) continue;
			if (shouldSkipIronHermesSkillDir(entry.name)) continue;
			const fullPath = join(dir, entry.name);
			const skillMd = join(fullPath, "SKILL.md");

			if (existsSync(skillMd)) {
				const content = readFileSync(skillMd, "utf-8");
				const skillKey = ironHermesSkillKeyFromPath(fullPath);
				if (!skillKey) continue;
				if (shouldIgnoreUserSkill(fullPath, skillKey)) continue;
				const fileCount = readdirSync(fullPath, { recursive: true }).length;

				results.push({
					skillKey,
					name: entry.name,
					content,
					filePath: skillMd,
					directoryPath: fullPath,
					isDirectory: fileCount > 1,
				});
			} else {
				// Might be a category directory, recurse
				this._scanSkillsDir(fullPath, results);
			}
		}
	}

	private getSkillPath(key: string): string {
		return join(skillsDir(), key, "SKILL.md");
	}

	private getSkillsRootDir(): string {
		return skillsDir();
	}

	private getSharedSkillPath(skillKey: string, ownerHandle: string): string {
		// IronHermes nests skills under category dirs; route shared project
		// content into a dedicated `shared/` category so it doesn't intermix
		// with user-authored categories.
		return join(skillsDir(), "shared", `${skillKey}__${ownerHandle}`);
	}

	private async listSkillKeys(): Promise<string[]> {
		// IronHermes nests skills under category dirs:
		//   `~/.ironhermes/skills/category/foo/SKILL.md`
		// Recurse — same logic `_scanSkillsDir` uses for the fully-loaded
		// `collectSkills`, just without reading SKILL.md content. Returns
		// relative paths so the daemon's hash + watch + push paths land at the
		// right place under `getSkillsRootDir()`. The generic flat-walk would
		// silently drop every nested skill from sync.
		//
		// Guarded before the migration for the same reason as `collectSkills`
		// (WR-05): the migration can write, so the check has to precede it.
		assertIronHermesReadRoot("ironhermes Skill read");
		// Upstream's own local-setup-skill migration: `migrateLegacyLocalSetupSkill` pins the
		// `clawdi` directory and id.
		migrateLegacyLocalSetupSkill({
			targetDir: join(skillsDir(), "clawdi"),
			id: "clawdi",
			version: 1,
			digest: managedSkillDirectoryDigest,
		});
		if (!existsSync(skillsDir())) return [];
		const out: string[] = [];
		const walk = (dir: string): void => {
			for (const entry of readdirSync(dir, { withFileTypes: true })) {
				if (!entry.isDirectory()) continue;
				if (shouldSkipIronHermesSkillDir(entry.name)) continue;
				const fullPath = join(dir, entry.name);
				if (existsSync(join(fullPath, "SKILL.md"))) {
					const skillKey = ironHermesSkillKeyFromPath(fullPath);
					if (skillKey && !shouldIgnoreUserSkill(fullPath, skillKey)) out.push(skillKey);
				} else {
					walk(fullPath);
				}
			}
		};
		walk(skillsDir());
		return out;
	}

	private getSessionsWatchPaths(): string[] {
		// SQLite may keep committed session rows in WAL or rollback-journal
		// sidecars while `state.db` itself remains unchanged. All four paths
		// therefore belong to one global quiescence window; missing sidecars have
		// an empty poll signature and become observable when created.
		//
		// Pitfall 7: every IronHermes store is `journal_mode=wal`, so `-journal`
		// never exists while `-shm` does — and `-shm` was unwatched by the Hermes
		// shape this adapter was copied from. `manifest.ts` already gets this pair
		// right for OpenClaw's database. `-journal` is kept anyway: watching a path
		// that does not exist is explicitly sanctioned by the `AgentAdapter`
		// contract, and dropping it would silently break a non-WAL install.
		//
		// Fan-out is per store. The root store is always included even when nothing
		// exists yet, so the daemon can notice a first-ever install appearing.
		// `getSessionsWatchPaths` is synchronous by contract, so this consumes the
		// enumerator's synchronous candidate form rather than the probe-filtered
		// one — the watcher tolerates paths that are not (yet) valid stores.
		const stores = new Set<string>([
			stateDbPath(),
			...listAgentStoreCandidatePaths(ironHermesDir(), STORE_OPTS),
		]);
		const paths: string[] = [];
		for (const database of stores) {
			paths.push(database, `${database}-wal`, `${database}-shm`, `${database}-journal`);
		}
		return paths;
	}

	private async removeLocalSkill(key: string): Promise<void> {
		const dir = join(skillsDir(), key);
		assertIronHermesWriteTarget(dir);
		mutateUserSkillTarget(
			dir,
			key,
			() => {
				if (existsSync(dir)) rmSync(dir, { recursive: true, force: true });
			},
			ironHermesDir(),
		);
	}

	private async writeSkillArchive(key: string, tarGzBytes: Buffer): Promise<void> {
		const root = skillsDir();
		const targetDir = join(root, key);
		// Before `replaceSkillArchiveTarGz`, not only inside the commit closure:
		// the helper creates the skills root and the target's parent directories
		// while staging, so a check deferred to commit time would already have
		// left empty directories in the wrong tree.
		assertIronHermesWriteTarget(targetDir);
		await replaceSkillArchiveTarGz(key, root, targetDir, tarGzBytes, undefined, (mutation) =>
			mutateUserSkillTarget(targetDir, key, mutation, ironHermesDir()),
		);
	}

	private async writeSharedSkillArchive(
		key: string,
		ownerHandle: string,
		tarGzBytes: Buffer,
	): Promise<void> {
		// The root-isolation guard runs before the archive replace, as on the
		// other write paths, because the shared-root mutation hook below checks
		// the reservation ledger but not which runtime's tree it is in. The hook
		// is upstream's (#1114 era) and matches `hermes.ts`.
		const root = this.getSkillsRootDir();
		const sharedRoot = join(root, "shared");
		const targetDir = this.getSharedSkillPath(key, ownerHandle);
		assertIronHermesWriteTarget(targetDir);
		await replaceSkillArchiveTarGz(key, root, targetDir, tarGzBytes, undefined, (mutation) =>
			mutateUserSkillTarget(sharedRoot, "shared", mutation),
		);
	}

	buildRunCommand(args: string[], _env: Record<string, string>): string[] {
		return ["ironhermes", ...args];
	}
}
