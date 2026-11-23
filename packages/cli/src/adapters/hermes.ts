import { createHash } from "node:crypto";
import { type Dirent, existsSync, readdirSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { join, relative } from "node:path";
import { setImmediate } from "node:timers/promises";
import { safeTruncate } from "../lib/sanitize";
import { durationSecondsBetween } from "../lib/session-duration";
import {
	projectEventsToMessages,
	type SessionEventDraft,
	sequenceSessionEvents,
} from "../lib/session-events";
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
	SessionBatchScan,
	SessionContentPart,
	SessionEventDisplayMetadata,
	SessionEventSemantics,
	SessionMessage,
	SessionScanRequest,
	SessionScanResult,
	SessionUserActivity,
	SyncReadContext,
} from "./base";
import { listAgentStoreCandidatePaths, listAgentStorePaths } from "./hermes-profiles";
import { getHermesHome, getIronHermesHome, SKIP_DIRS, safeSkillDirectoryPath } from "./paths";
import {
	canonicalStructuredString,
	jsonObject,
	jsonString,
	reasoningContent,
	toolResultContent,
	visibleContentParts,
} from "./rich-event-mapping";
import { openReadonlySqlite, type ReadonlySqliteDatabase } from "./sqlite";
import { readCommandVersion } from "./version";

/** Bracketed prefix every store-walk warning carries for this runtime. */
const STORE_OPTS = { label: "hermes" } as const;

interface SessionRow {
	id: string;
	source: string | null;
	model: string | null;
	title: string | null;
	started_at: number;
	ended_at: number | null;
	message_count: number | null;
	input_tokens: number | null;
	output_tokens: number | null;
	cache_read_tokens: number | null;
}

interface MessageRow {
	role: string;
	content: string | null;
	timestamp: number;
}

interface ModernMessageRow extends MessageRow {
	id: number;
	tool_call_id: string | null;
	tool_calls: string | null;
	tool_name: string | null;
	_compressed_summary: number;
	active: number;
	compacted: number;
	display_kind: string | null;
	display_metadata: string | null;
	reasoning: string | null;
	reasoning_content: string | null;
	reasoning_details: string | null;
	codex_reasoning_items: string | null;
}

interface TableInfoRow {
	name: string;
	type: string;
	pk: number;
}

interface MessageRevisionRow {
	presentation_state: string;
}

interface UserActivityRow {
	last_user_input_at: number | string | null;
}

const MODERN_MESSAGE_CORE_COLUMNS = ["session_id", "role", "content", "timestamp"] as const;

const MODERN_MESSAGE_OPTIONAL_COLUMNS = [
	["tool_call_id", "NULL"],
	["tool_calls", "NULL"],
	["tool_name", "NULL"],
	["_compressed_summary", "0"],
	["active", "1"],
	["compacted", "0"],
	["display_kind", "NULL"],
	["display_metadata", "NULL"],
	["reasoning", "NULL"],
	["reasoning_content", "NULL"],
	["reasoning_details", "NULL"],
	["codex_reasoning_items", "NULL"],
] as const;

const HERMES_CONTENT_JSON_PREFIX = "\0json:";
const HERMES_SESSION_SCAN_BATCH_SIZE = 32;
// Bump when persisted Hermes rows map to different Session/Event bytes.
const HERMES_SESSION_PROJECTION_REVISION = 2;

function messageTableInfo(db: ReadonlySqliteDatabase): TableInfoRow[] {
	return db.prepare("PRAGMA table_info(messages)").all() as TableInfoRow[];
}

function hasStableModernMessageIds(columns: readonly TableInfoRow[]): boolean {
	const id = columns.find((column) => column.name === "id");
	return (
		id?.pk === 1 &&
		id.type.trim().toUpperCase() === "INTEGER" &&
		MODERN_MESSAGE_CORE_COLUMNS.every((name) => columns.some((column) => column.name === name))
	);
}

function modernMessageSelectColumns(columns: readonly TableInfoRow[]): string {
	const names = new Set(columns.map((column) => column.name));
	return [
		"id",
		"role",
		"content",
		...MODERN_MESSAGE_OPTIONAL_COLUMNS.map(([name, fallback]) =>
			names.has(name) ? name : `${fallback} AS ${name}`,
		),
		"timestamp",
	].join(", ");
}

function messageRevisionQuery(columns: readonly TableInfoRow[]): string {
	const names = new Set(columns.map((column) => column.name));
	const columnOr = (name: string, fallback: string) =>
		names.has(name) ? name : `${fallback} AS ${name}`;
	// Hermes appends and replaces rows with stable IDs, toggles lifecycle flags,
	// updates presentation metadata in place, and may clear stale content. Keep
	// that state exact while reducing large immutable payloads to byte lengths.
	// SQLite can answer octet_length from the record header without reading the
	// content payload from overflow pages.
	return `
		SELECT json_group_array(
		         json_array(
		           id, timestamp, active, compacted, display_kind, display_metadata,
		           octet_length(content)
		         ) ORDER BY id
		       ) AS presentation_state
		FROM (
			SELECT id, timestamp, content,
			       ${columnOr("active", "1")},
			       ${columnOr("compacted", "0")},
			       ${columnOr("display_kind", "NULL")},
			       ${columnOr("display_metadata", "NULL")}
			FROM messages
			WHERE session_id = ?
		)
	`;
}

function sessionSourceRevision(row: SessionRow, messages: MessageRevisionRow): string {
	return createHash("sha256")
		.update(
			JSON.stringify([
				HERMES_SESSION_PROJECTION_REVISION,
				row.id,
				row.source,
				row.model,
				row.title,
				row.started_at,
				row.ended_at,
				row.message_count,
				row.input_tokens,
				row.output_tokens,
				row.cache_read_tokens,
				messages.presentation_state,
			]),
		)
		.digest("hex");
}

function hermesUserActivity(db: ReadonlySqliteDatabase): SessionUserActivity {
	const sessionColumns = new Set(
		(db.prepare("PRAGMA table_info(sessions)").all() as TableInfoRow[]).map(
			(column) => column.name,
		),
	);
	const messageColumns = new Set(messageTableInfo(db).map((column) => column.name));
	if (
		!["id", "source"].every((column) => sessionColumns.has(column)) ||
		!["session_id", "role", "timestamp"].every((column) => messageColumns.has(column))
	) {
		return { lastUserInputAt: null, complete: false };
	}
	// parent_session_id chains are compression splits of the same conversation, so they
	// count. Delegated subagents are tagged by source or by model_config._delegate_from.
	const delegated = sessionColumns.has("model_config")
		? "AND NOT (json_valid(s.model_config) AND json_extract(s.model_config, '$._delegate_from') IS NOT NULL)"
		: "";
	try {
		const row = db
			.prepare(`
				SELECT MAX(m.timestamp) AS last_user_input_at
				FROM messages AS m
				JOIN sessions AS s ON s.id = m.session_id
				WHERE lower(m.role) = 'user'
				  AND lower(coalesce(s.source, '')) NOT IN ('cron', 'subagent', 'curator', 'kanban')
				  ${delegated}
			`)
			.get() as UserActivityRow | undefined;
		const timestamp = row?.last_user_input_at ?? null;
		if (timestamp === null) return { lastUserInputAt: null, complete: true };
		const lastUserInputAt = timestampIso(Number(timestamp));
		return lastUserInputAt
			? { lastUserInputAt, complete: true }
			: { lastUserInputAt: null, complete: false };
	} catch {
		return { lastUserInputAt: null, complete: false };
	}
}

function decodeHermesContent(content: string | null): unknown {
	if (!content?.startsWith(HERMES_CONTENT_JSON_PREFIX)) return content;
	try {
		return JSON.parse(content.slice(HERMES_CONTENT_JSON_PREFIX.length));
	} catch {
		return content;
	}
}

const CLOSED_REASONING_BLOCK =
	/<(think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>[\s\S]*?<\/\1>/gi;
const OPEN_REASONING_TAG = /<(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>/gi;
const ORPHAN_REASONING_CLOSE =
	/<\/(?:think|thinking|reasoning|thought|REASONING_SCRATCHPAD)>[ \t\r\n]*/gi;

function stripHiddenReasoning(text: string): string {
	let visible = text.replace(CLOSED_REASONING_BLOCK, "");
	for (const match of visible.matchAll(OPEN_REASONING_TAG)) {
		const index = match.index;
		const lineStart = visible.lastIndexOf("\n", index - 1) + 1;
		if (visible.slice(lineStart, index).trim().length === 0) {
			visible = visible.slice(0, index);
			break;
		}
	}
	return visible.replace(ORPHAN_REASONING_CLOSE, "");
}

function hiddenReasoningTexts(text: string): string[] {
	const texts: string[] = [];
	for (const match of text.matchAll(CLOSED_REASONING_BLOCK)) {
		const value = match[0].replace(/^<[^>]+>/, "").replace(/<\/[^>]+>$/, "");
		if (value) texts.push(value);
	}
	for (const match of text.matchAll(OPEN_REASONING_TAG)) {
		const index = match.index;
		const lineStart = text.lastIndexOf("\n", index - 1) + 1;
		if (text.slice(lineStart, index).trim().length > 0) continue;
		const tail = text.slice(index + match[0].length);
		if (!tail.includes("</") && tail) texts.push(tail);
		break;
	}
	return texts;
}

function decodeOptionalJson(value: string | null): unknown {
	if (value === null) return undefined;
	try {
		return JSON.parse(value);
	} catch {
		return value;
	}
}

function safeHermesContent(content: string | null, scrubReasoning: boolean): unknown {
	const decoded = decodeHermesContent(content);
	if (!scrubReasoning) return decoded;
	if (typeof decoded === "string") return stripHiddenReasoning(decoded);
	const scrubBlock = (item: unknown): unknown => {
		const block = jsonObject(item);
		if (!block || typeof block.text !== "string") return item;
		return { ...block, text: stripHiddenReasoning(block.text) };
	};
	return Array.isArray(decoded) ? decoded.map(scrubBlock) : scrubBlock(decoded);
}

function hermesContentParts(content: string | null, scrubReasoning: boolean): SessionContentPart[] {
	return visibleContentParts(safeHermesContent(content, scrubReasoning)).filter(
		(part) => part.type !== "text" || part.text.length > 0,
	);
}

function hermesReasoning(row: ModernMessageRow) {
	if (row.role !== "assistant") return null;
	const decoded = decodeHermesContent(row.content);
	const texts: string[] = [];
	if (row.reasoning) texts.push(row.reasoning);
	if (row.reasoning_content) texts.push(row.reasoning_content);
	const collect = (value: unknown) => {
		if (typeof value === "string") texts.push(...hiddenReasoningTexts(value));
		else {
			const block = jsonObject(value);
			if (typeof block?.text === "string") texts.push(...hiddenReasoningTexts(block.text));
		}
	};
	if (Array.isArray(decoded)) decoded.forEach(collect);
	else collect(decoded);
	const uniqueTexts = [...new Set(texts.filter(Boolean))];
	return reasoningContent({
		type: "reasoning",
		summary: uniqueTexts.map((text) => ({ type: "summary_text", text })),
		reasoning_details: decodeOptionalJson(row.reasoning_details),
		codex_reasoning_items: decodeOptionalJson(row.codex_reasoning_items),
	});
}

function timestampIso(value: number): string | undefined {
	if (!Number.isFinite(value)) return undefined;
	const date = new Date(value * 1000);
	return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

function nonNegativeInteger(value: unknown): number | undefined {
	return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}

function displayMetadata(raw: string | null): SessionEventDisplayMetadata | undefined {
	if (!raw) return undefined;
	let parsed: unknown;
	try {
		parsed = JSON.parse(raw);
	} catch {
		return undefined;
	}
	const metadata = jsonObject(parsed);
	if (!metadata) return undefined;
	const normalized: SessionEventDisplayMetadata = {};
	const taskCount = nonNegativeInteger(metadata.task_count);
	const attempt = nonNegativeInteger(metadata.attempt);
	if (taskCount !== undefined) normalized.task_count = taskCount;
	if (attempt !== undefined) normalized.attempt = attempt;
	if (Array.isArray(metadata.reactions)) {
		const reactions = metadata.reactions.flatMap((value) => {
			const reaction = jsonObject(value);
			const emoji = jsonString(reaction?.emoji);
			const author = jsonString(reaction?.author);
			if (!emoji || emoji.length > 64 || !author || author.length > 100) return [];
			const at = typeof reaction?.at === "number" ? timestampIso(reaction.at) : undefined;
			return [
				{
					emoji,
					author,
					...(at ? { at } : {}),
					...(typeof reaction?.seen === "boolean" ? { seen: reaction.seen } : {}),
				},
			];
		});
		if (reactions.length > 0) normalized.reactions = reactions;
	}
	return Object.keys(normalized).length > 0 ? normalized : undefined;
}

function rowSemantics(row: ModernMessageRow): SessionEventSemantics {
	const displayKind = row.display_kind?.trim() || undefined;
	const metadata = displayMetadata(row.display_metadata);
	return {
		lifecycle: row.active ? "active" : row.compacted ? "compacted" : "inactive",
		display: displayKind === "hidden" ? "hidden" : displayKind ? "event" : "message",
		compressed_summary: Boolean(row._compressed_summary),
		...(displayKind ? { display_kind: displayKind } : {}),
		...(metadata ? { display_metadata: metadata } : {}),
	};
}

function parseToolCalls(raw: string | null): Array<Record<string, unknown>> {
	if (!raw) return [];
	try {
		const parsed: unknown = JSON.parse(raw);
		return Array.isArray(parsed)
			? parsed.filter((value): value is Record<string, unknown> => jsonObject(value) !== null)
			: [];
	} catch {
		return [];
	}
}

function hermesEventDrafts(
	row: ModernMessageRow,
	sessionKey: string,
	model: string | null,
): SessionEventDraft[] {
	const semantics = rowSemantics(row);
	const timestamp = timestampIso(row.timestamp);
	const source = (partIndex: number) => ({
		adapter: "hermes" as const,
		session_key: sessionKey,
		record_id: String(row.id),
		record_seq: row.id,
		part_index: partIndex,
	});
	const drafts: SessionEventDraft[] = [];
	const calls = parseToolCalls(row.tool_calls);
	const reasoning = hermesReasoning(row);
	if (reasoning) {
		drafts.push({
			type: "reasoning",
			...reasoning,
			source: source(0),
			semantics,
			...(timestamp ? { timestamp } : {}),
			...(model ? { model } : {}),
		});
	}
	if (
		row.role === "user" ||
		row.role === "assistant" ||
		row.role === "system" ||
		row.role === "developer"
	) {
		const parts = hermesContentParts(row.content, row.role === "assistant");
		if (parts.length > 0 || calls.length === 0) {
			drafts.push({
				type: "message",
				role: row.role,
				parts,
				source: source(0),
				semantics,
				...(timestamp ? { timestamp } : {}),
				...(row.role === "assistant" && model ? { model } : {}),
			});
		}
	}
	if (row.role === "assistant") {
		for (let index = 0; index < calls.length; index++) {
			const call = calls[index];
			if (!call) continue;
			const fn = jsonObject(call.function);
			const name = jsonString(fn?.name) ?? jsonString(call.name);
			if (!name) continue;
			const callId = jsonString(call.id) ?? `hermes:${sessionKey}:${row.id}:tool-call:${index}`;
			const args = fn?.arguments ?? call.arguments;
			drafts.push({
				type: "tool_call",
				call_id: callId,
				name,
				arguments_json: canonicalStructuredString(args),
				source: source(index + 1),
				semantics,
				...(timestamp ? { timestamp } : {}),
				...(model ? { model } : {}),
			});
		}
	}
	if (row.role === "tool") {
		drafts.push({
			type: "tool_result",
			call_id: row.tool_call_id ?? `hermes:${sessionKey}:${row.id}:tool-result`,
			...(row.tool_name ? { name: row.tool_name } : {}),
			status: "completed",
			...toolResultContent(safeHermesContent(row.content, false)),
			source: source(0),
			semantics,
			...(timestamp ? { timestamp } : {}),
		});
	}
	return drafts;
}

function hermesDir() {
	return getHermesHome();
}
function stateDbPath() {
	return join(hermesDir(), "state.db");
}
function skillsDir() {
	return join(hermesDir(), "skills");
}

function shouldSkipHermesSkillDir(entryName: string): boolean {
	return entryName.startsWith(".") || SKIP_DIRS.has(entryName);
}

/**
 * D-21: every upstream-Hermes mutation target, checked at RUNTIME.
 *
 * The mirror of `assertIronHermesWriteTarget`. Isolation is the load-bearing
 * half here: upstream supports `$HERMES_HOME` pointed directly at a profile
 * directory, so a user who points it into the IronHermes tree is not doing
 * anything upstream considers wrong — and containment alone cannot see it,
 * because the target is faithfully inside the (wrong) root.
 */
function assertHermesWriteTarget(targetPath: string): void {
	const root = hermesDir();
	assertAgentRootIsolated(root, foreignRoots(), "hermes Skill write");
	assertAgentRootContains(root, targetPath, "hermes Skill write");
}

/** The live runtimes this one must never resolve inside. */
function foreignRoots(): string[] {
	return [getIronHermesHome()];
}

/**
 * T-02-05: the READ half, mirrored from `ironhermes.ts`.
 *
 * Upstream Hermes reading an IronHermes tree is the same defect with the labels
 * swapped, and the guard is directional by design — it reports only "my root is
 * inside theirs" — so neither side's check substitutes for the other's. Both
 * halves must exist for either nesting arrangement to be caught.
 *
 * Throws, for the same reason the IronHermes copy does: collecting from the
 * wrong tree uploads another runtime's data under this identity, which is the
 * harm itself.
 */
function assertHermesReadRoot(context: string): string {
	const root = hermesDir();
	assertAgentRootIsolated(root, foreignRoots(), context);
	return root;
}

function hermesSkillKeyFromPath(fullPath: string): string | null {
	const skillKey = relative(skillsDir(), fullPath).replaceAll("\\", "/");
	return isValidSkillKey(skillKey) ? skillKey : null;
}

/**
 * Extract a plain model name string from Hermes model field.
 * The field can be a plain string ("claude-opus-4.6") or a JSON object
 * ({"default": "gpt-5.3-codex", "provider": "openai-codex", ...}).
 *
 * Never return the raw blob: it can carry provider headers including API keys,
 * and the model value is rendered in the dashboard.
 *
 * Mirror of the IronHermes copy — see the longer note there for why the trim
 * precedes the brace test and why `[` counts as a blob (T-02-26). Both must
 * stay in agreement with `clean_model_value` in
 * `backend/app/schemas/session.py`.
 */
function parseModelField(raw: string | null): string | null {
	if (!raw) return null;
	const value = raw.trim();
	if (!value) return null;
	if (value.startsWith("{") || value.startsWith("[")) {
		try {
			const parsed = JSON.parse(value);
			const candidate = parsed?.default ?? parsed?.model;
			return typeof candidate === "string" && candidate.trim() ? candidate.trim() : null;
		} catch {
			return /['"](?:default|model)['"]\s*:\s*['"]([^'"]+)['"]/.exec(value)?.[1]?.trim() ?? null;
		}
	}
	return value;
}

export class HermesAdapter implements AgentAdapterCore {
	readonly agentType = "hermes" as const;
	readonly sessions = {
		contentProtocol: (context?: SyncReadContext) => this.getContentProtocol(context),
		collect: (request: SessionScanRequest, context?: SyncReadContext) =>
			this.collectSessions(request, context),
		scan: (
			request: SessionScanRequest,
			knownSourceRevisions: ReadonlyMap<string, string>,
			context?: SyncReadContext,
		) => this.scanSessions(request, knownSourceRevisions, context),
		resolve: (localSessionId: string, context?: SyncReadContext) =>
			this.resolveSession(localSessionId, context),
		watchPaths: () => this.getSessionsWatchPaths(),
	};
	readonly skills = {
		collect: (context?: SyncReadContext) => this.collectSkills(context),
		listKeys: (context?: SyncReadContext) => this.listSkillKeys(context),
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
		// Hermes stores state in SQLite. The dir alone may exist as a leftover; a
		// store is the only file every Hermes install creates. Any store in the
		// install counts, including the profile-only case where `HERMES_HOME`
		// points at a root that never ran with the default profile.
		//
		// Warns and answers `false` rather than throwing (T-02-05) — see the
		// matching note in `ironhermes.ts`: `lib/select-adapter.ts` fans every
		// adapter's detect() into one unguarded `Promise.all`, so a throw here
		// would break adapter selection for every other runtime as well.
		const root = hermesDir();
		if (!isAgentRootIsolated(root, foreignRoots())) {
			console.warn(
				`[hermes] not detected: the install root ${root} resolves inside another runtime's install root (${foreignRoots().join(", ")}); check $HERMES_HOME and $IRONHERMES_HOME`,
			);
			return false;
		}
		return (await listAgentStorePaths(root, STORE_OPTS)).length > 0;
	}

	async getVersion(): Promise<string | null> {
		return readCommandVersion("hermes", ["--version"]);
	}

	private async getContentProtocol(
		context?: SyncReadContext,
	): Promise<"events-v1" | "snapshot-v1"> {
		context?.signal.throwIfAborted();
		// D-12: the protocol covers every profile store the scan pools, so events
		// are offered only when EVERY store carries stable modern message ids. One
		// legacy or unreadable store keeps the whole adapter on snapshots.
		const storePaths = await listAgentStorePaths(
			assertHermesReadRoot("hermes session read"),
			STORE_OPTS,
		);
		if (storePaths.length === 0) return "snapshot-v1";
		for (const storePath of storePaths) {
			context?.signal.throwIfAborted();
			let db: ReadonlySqliteDatabase;
			try {
				db = await openReadonlySqlite(storePath);
			} catch {
				return "snapshot-v1";
			}
			try {
				context?.signal.throwIfAborted();
				if (!hasStableModernMessageIds(messageTableInfo(db))) return "snapshot-v1";
			} catch (error) {
				if (context?.signal.aborted) throw error;
				return "snapshot-v1";
			} finally {
				db.close();
			}
		}
		return "events-v1";
	}

	private async collectSessions(
		request: SessionScanRequest,
		context?: SyncReadContext,
	): Promise<SessionScanResult> {
		context?.signal.throwIfAborted();
		const scan = await this.scanSessions(request, new Map(), context);
		const sessions: RawSession[] = [];
		let dedupedCount = 0;
		for await (const batch of scan.batches) {
			sessions.push(...batch.sessions);
			dedupedCount += batch.dedupedCount;
		}
		return { sessions, dedupedCount, coverage: scan.coverage };
	}

	private async scanSessions(
		_request: SessionScanRequest,
		knownSourceRevisions: ReadonlyMap<string, string>,
		context?: SyncReadContext,
	): Promise<SessionBatchScan> {
		// D-12: Hermes is profile-scoped exactly as IronHermes is —
		// `<HERMES_HOME>/state.db` plus `<HERMES_HOME>/profiles/<name>/state.db`.
		// Upstream reads only the root, so Hermes users with profiles would lose
		// those sessions. The shared walk closes the gap. `projectFilter` has no
		// analogue in Hermes' data model and is silently ignored.
		const root = assertHermesReadRoot("hermes session read");
		const storePaths = await listAgentStorePaths(root, STORE_OPTS);
		if (storePaths.length === 0) {
			return {
				coverage: "complete",
				userActivity: { lastUserInputAt: null, complete: false },
				batches: (async function* () {})(),
			};
		}
		// Per-store guard: one corrupt, locked or mid-migration store must not
		// abort the pool, and a skipped store is always surfaced (coverage
		// `partial`) rather than folded into a successful zero-session push, so
		// its sessions are never treated as absent. A cancelled sync (upstream's
		// SyncReadContext) is not a store fault: it closes what was opened and
		// propagates.
		const stores: Array<{ path: string; db: ReadonlySqliteDatabase }> = [];
		let complete = true;
		let lastUserInputAt: string | null = null;
		let activityComplete = true;
		try {
			for (const storePath of storePaths) {
				context?.signal.throwIfAborted();
				let db: ReadonlySqliteDatabase;
				try {
					db = await openReadonlySqlite(storePath);
				} catch (e) {
					complete = false;
					activityComplete = false;
					console.warn(
						`[hermes] could not open ${storePath}: ${e instanceof Error ? e.message : String(e)}`,
					);
					continue;
				}
				stores.push({ path: storePath, db });
				const activity = hermesUserActivity(db);
				if (!activity.complete) activityComplete = false;
				if (
					activity.lastUserInputAt !== null &&
					(lastUserInputAt === null || activity.lastUserInputAt > lastUserInputAt)
				) {
					lastUserInputAt = activity.lastUserInputAt;
				}
			}
		} catch (error) {
			for (const store of stores) store.db.close();
			throw error;
		}
		const scan: SessionBatchScan = {
			coverage: complete ? "complete" : "partial",
			userActivity: { lastUserInputAt, complete: activityComplete },
			batches: this.readPooledSessionBatches(
				stores,
				knownSourceRevisions,
				() => {
					scan.coverage = "partial";
				},
				context,
			),
		};
		return scan;
	}

	private async *readPooledSessionBatches(
		stores: Array<{ path: string; db: ReadonlySqliteDatabase }>,
		knownSourceRevisions: ReadonlyMap<string, string>,
		markPartial: () => void,
		context?: SyncReadContext,
	): AsyncGenerator<{
		sessions: RawSession[];
		observedLocalSessionIds: readonly string[];
		dedupedCount: number;
	}> {
		let index = 0;
		try {
			for (; index < stores.length; index++) {
				const store = stores[index] as { path: string; db: ReadonlySqliteDatabase };
				try {
					yield* this.readSessionBatches(store.db, store.path, knownSourceRevisions, context);
				} catch (e) {
					if (context?.signal.aborted) throw e;
					markPartial();
					console.warn(
						`[hermes] could not read ${store.path}: ${e instanceof Error ? e.message : String(e)}`,
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
		context?: SyncReadContext,
	): AsyncGenerator<{
		sessions: RawSession[];
		observedLocalSessionIds: readonly string[];
		dedupedCount: number;
	}> {
		try {
			const readers = this.sessionReaders(db);
			let cursor: Pick<SessionRow, "started_at" | "id"> | null = null;
			while (true) {
				if (context) await setImmediate(undefined, { signal: context.signal });
				context?.signal.throwIfAborted();
				const rows = db
					.prepare(`
						SELECT id, source, model, title, started_at, ended_at,
						       message_count, input_tokens, output_tokens, cache_read_tokens
						FROM sessions
						${cursor ? "WHERE started_at < ? OR (started_at = ? AND id < ?)" : ""}
						ORDER BY started_at DESC, id DESC
						LIMIT ?
					`)
					.all(
						...(cursor ? [cursor.started_at, cursor.started_at, cursor.id] : []),
						HERMES_SESSION_SCAN_BATCH_SIZE,
					) as SessionRow[];
				if (rows.length === 0) return;

				const observedLocalSessionIds = rows.map((row) => row.id);
				const sessions: RawSession[] = [];
				for (const row of rows) {
					const sourceRevision = readers.revision
						? sessionSourceRevision(row, readers.revision.get(row.id) as MessageRevisionRow)
						: undefined;
					if (sourceRevision && knownSourceRevisions.get(row.id) === sourceRevision) continue;
					const session = this.materializeSession(
						row,
						sourceRevision,
						readers.modern,
						readers.messages,
						storePath,
					);
					if (session) sessions.push(session);
				}
				yield { sessions, observedLocalSessionIds, dedupedCount: 0 };
				const last = rows.at(-1);
				if (!last || rows.length < HERMES_SESSION_SCAN_BATCH_SIZE) return;
				cursor = last;
			}
		} finally {
			db.close();
		}
	}

	private async resolveSession(
		localSessionId: string,
		context?: SyncReadContext,
	): Promise<RawSession | null> {
		context?.signal.throwIfAborted();
		// D-12: the id may live in any profile store; the first store holding it wins, in the
		// same order the scan pools them. An unreadable store is skipped with a warning.
		const root = assertHermesReadRoot("hermes session read");
		for (const storePath of await listAgentStorePaths(root, STORE_OPTS)) {
			context?.signal.throwIfAborted();
			let db: ReadonlySqliteDatabase;
			try {
				db = await openReadonlySqlite(storePath);
			} catch (e) {
				console.warn(
					`[hermes] could not open ${storePath}: ${e instanceof Error ? e.message : String(e)}`,
				);
				continue;
			}
			try {
				context?.signal.throwIfAborted();
				const row = db
					.prepare(`
						SELECT id, source, model, title, started_at, ended_at,
						       message_count, input_tokens, output_tokens, cache_read_tokens
						FROM sessions
						WHERE id = ?
					`)
					.get(localSessionId) as SessionRow | undefined;
				if (!row) continue;
				const readers = this.sessionReaders(db);
				return this.materializeSession(
					row,
					readers.revision
						? sessionSourceRevision(row, readers.revision.get(row.id) as MessageRevisionRow)
						: undefined,
					readers.modern,
					readers.messages,
					storePath,
				);
			} catch (e) {
				if (context?.signal.aborted) throw e;
				console.warn(
					`[hermes] could not read ${storePath}: ${e instanceof Error ? e.message : String(e)}`,
				);
			} finally {
				db.close();
			}
		}
		return null;
	}

	private sessionReaders(db: ReadonlySqliteDatabase) {
		const messageColumns = messageTableInfo(db);
		const modern = hasStableModernMessageIds(messageColumns);
		return {
			modern,
			revision: modern ? db.prepare(messageRevisionQuery(messageColumns)) : null,
			messages: db.prepare(
				modern
					? `
						SELECT ${modernMessageSelectColumns(messageColumns)}
						FROM messages
						WHERE session_id = ?
						ORDER BY id ASC
					`
					: `
						SELECT role, content, timestamp
						FROM messages
						WHERE session_id = ? AND role IN ('user', 'assistant') AND content IS NOT NULL
						ORDER BY timestamp ASC
					`,
			),
		};
	}

	private materializeSession(
		row: SessionRow,
		sourceRevision: string | undefined,
		modern: boolean,
		messagesStatement: ReturnType<ReadonlySqliteDatabase["prepare"]>,
		storePath: string,
	): RawSession | null {
		const model = parseModelField(row.model);
		const startedAt = new Date(row.started_at * 1000);
		const endedAt = row.ended_at ? new Date(row.ended_at * 1000) : null;
		const durationSeconds = durationSecondsBetween(startedAt, endedAt);
		const messageRows = messagesStatement.all(row.id) as Array<MessageRow | ModernMessageRow>;
		const events = modern
			? sequenceSessionEvents(
					(messageRows as ModernMessageRow[]).flatMap((message) =>
						hermesEventDrafts(message, row.id, model),
					),
				)
			: undefined;
		const messages: SessionMessage[] = events
			? projectEventsToMessages(events)
			: (messageRows as MessageRow[]).map((message) => ({
					role: message.role as "user" | "assistant",
					content: message.content ?? "",
					model: message.role === "assistant" ? (model ?? undefined) : undefined,
					...(timestampIso(message.timestamp)
						? { timestamp: timestampIso(message.timestamp) }
						: {}),
				}));
		if (modern ? events?.length === 0 : messages.length === 0) return null;

		let summary = row.title;
		if (!summary || summary === "New Chat" || summary.startsWith("New Chat #")) {
			const firstUser = messages.find((message) => message.role === "user");
			summary = firstUser ? safeTruncate(firstUser.content, 200) : null;
		}
		return {
			// NOT sanitized. The colon→period transform exists for IronHermes'
			// composite web/DM routing keys; applying it here would change the
			// object-store key of every already synced Hermes session.
			localSessionId: row.id,
			// Hermes sessions have no filesystem cwd — `row.source` is a channel/origin
			// tag (e.g. "telegram"), not a path. Upstream Hermes has no
			// `workspace_root` column, so D-02's verbatim pass-through is
			// IronHermes-only.
			projectPath: null,
			startedAt,
			endedAt,
			messageCount: row.message_count ?? messages.length,
			inputTokens: row.input_tokens ?? 0,
			outputTokens: row.output_tokens ?? 0,
			cacheReadTokens: row.cache_read_tokens ?? 0,
			model,
			modelsUsed: model ? [model] : [],
			durationSeconds,
			summary,
			messages,
			...(events ? { events } : {}),
			// Anchored to THIS store rather than the root, so a pooled session still
			// says which database produced it.
			rawFilePath: `${storePath}#${row.id}`,
			...(sourceRevision ? { sourceRevision } : {}),
		};
	}

	private async collectSkills(context?: SyncReadContext): Promise<RawSkill[]> {
		context?.signal.throwIfAborted();
		// Before the migration, which takes the ledger write lock and can write
		// (WR-05). A guard below it would let a read path mutate the foreign tree
		// on its way to refusing.
		assertHermesReadRoot("hermes Skill read");
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
		this._scanSkillsDir(skillsDir(), skills, new Set());
		return skills;
	}

	/**
	 * Recursively scan for directories containing SKILL.md.
	 * Hermes skills can be nested: skills/category/skill-name/SKILL.md
	 */
	private _scanSkillsDir(dir: string, results: RawSkill[], visited: Set<string>): void {
		let canonicalDir: string;
		try {
			canonicalDir = realpathSync(dir);
		} catch {
			return;
		}
		if (visited.has(canonicalDir)) return;
		visited.add(canonicalDir);
		let entries: Dirent[];
		try {
			entries = readdirSync(dir, { withFileTypes: true });
		} catch {
			return;
		}
		for (const entry of entries) {
			if (shouldSkipHermesSkillDir(entry.name)) continue;
			const fullPath = safeSkillDirectoryPath(skillsDir(), entry, dir);
			if (!fullPath) continue;
			try {
				const skillMd = join(fullPath, "SKILL.md");
				if (existsSync(skillMd)) {
					const skillKey = hermesSkillKeyFromPath(fullPath);
					if (!skillKey || shouldIgnoreUserSkill(fullPath, skillKey)) continue;
					const content = readFileSync(skillMd, "utf-8");
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
					this._scanSkillsDir(fullPath, results, visited);
				}
			} catch {}
		}
	}

	private getSkillPath(key: string): string {
		return join(skillsDir(), key, "SKILL.md");
	}

	private getSkillsRootDir(): string {
		return skillsDir();
	}

	private getSharedSkillPath(skillKey: string, ownerHandle: string): string {
		// Hermes nests skills under category dirs; route shared
		// project content into a dedicated `shared/` category so it
		// doesn't intermix with user-authored categories.
		return join(skillsDir(), "shared", `${skillKey}__${ownerHandle}`);
	}

	private async listSkillKeys(context?: SyncReadContext): Promise<string[]> {
		context?.signal.throwIfAborted();
		// Hermes nests skills under category dirs:
		//   `~/.hermes/skills/category/foo/SKILL.md`
		// Recurse — same logic `_scanSkillsDir` uses for the
		// fully-loaded `collectSkills`, just without reading
		// SKILL.md content. Returns relative paths so the
		// daemon's hash + watch + push paths land at the right
		// place under `getSkillsRootDir()`. Without this method,
		// the generic flat-walk used to silently drop nested
		// Hermes skills from sync.
		//
		// Guarded before the migration for the same reason as `collectSkills`
		// (WR-05): the migration can write, so the check has to precede it.
		assertHermesReadRoot("hermes Skill read");
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
		const visited = new Set<string>();
		const walk = (dir: string): void => {
			let canonicalDir: string;
			try {
				canonicalDir = realpathSync(dir);
			} catch {
				return;
			}
			if (visited.has(canonicalDir)) return;
			visited.add(canonicalDir);
			let entries: Dirent[];
			try {
				entries = readdirSync(dir, { withFileTypes: true });
			} catch {
				return;
			}
			for (const entry of entries) {
				if (shouldSkipHermesSkillDir(entry.name)) continue;
				const fullPath = safeSkillDirectoryPath(skillsDir(), entry, dir);
				if (!fullPath) continue;
				try {
					if (existsSync(join(fullPath, "SKILL.md"))) {
						const skillKey = hermesSkillKeyFromPath(fullPath);
						if (skillKey && !shouldIgnoreUserSkill(fullPath, skillKey)) out.push(skillKey);
					} else {
						walk(fullPath);
					}
				} catch {}
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
		// `-wal` and `-journal` are the pair this adapter already returned and are
		// kept as-is — a Hermes install may legitimately be in rollback-journal
		// mode, and dropping a sidecar someone's setup depends on would be a silent
		// regression. `-shm` is ADDED: WAL-mode stores create it, and it was
		// unwatched (`manifest.ts` already gets this right for OpenClaw's database).
		//
		// Fan-out is per store, with the root store always present so the daemon can
		// notice a first-ever install appearing. `getSessionsWatchPaths` is
		// synchronous by contract, so this consumes the enumerator's synchronous
		// candidate form; the watcher tolerates paths that are not (yet) valid.
		const stores = new Set<string>([
			stateDbPath(),
			...listAgentStoreCandidatePaths(hermesDir(), STORE_OPTS),
		]);
		const paths: string[] = [];
		for (const database of stores) {
			paths.push(database, `${database}-wal`, `${database}-shm`, `${database}-journal`);
		}
		return paths;
	}

	private async removeLocalSkill(key: string): Promise<void> {
		const dir = join(skillsDir(), key);
		assertHermesWriteTarget(dir);
		mutateUserSkillTarget(
			dir,
			key,
			() => {
				if (existsSync(dir)) rmSync(dir, { recursive: true, force: true });
			},
			hermesDir(),
		);
	}

	private async writeSkillArchive(key: string, tarGzBytes: Buffer): Promise<void> {
		const root = skillsDir();
		const targetDir = join(root, key);
		// Before `replaceSkillArchiveTarGz`, not only inside the commit closure:
		// the helper creates the skills root and the target's parent directories
		// while staging, so a check deferred to commit time would already have
		// left empty directories in the wrong tree.
		assertHermesWriteTarget(targetDir);
		await replaceSkillArchiveTarGz(key, root, targetDir, tarGzBytes, undefined, (mutation) =>
			mutateUserSkillTarget(targetDir, key, mutation, hermesDir()),
		);
	}

	private async writeSharedSkillArchive(
		key: string,
		ownerHandle: string,
		tarGzBytes: Buffer,
	): Promise<void> {
		// The root-isolation guard runs before the archive replace, as on the
		// other write paths; the shared-root mutation hook is upstream's (#1114
		// era). Mirrored in `ironhermes.ts`.
		const root = this.getSkillsRootDir();
		const sharedRoot = join(root, "shared");
		const targetDir = this.getSharedSkillPath(key, ownerHandle);
		assertHermesWriteTarget(targetDir);
		await replaceSkillArchiveTarGz(key, root, targetDir, tarGzBytes, undefined, (mutation) =>
			mutateUserSkillTarget(sharedRoot, "shared", mutation),
		);
	}
}
