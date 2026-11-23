import type { AgentType } from "./agent-types";

export interface SessionMessage {
	role: "user" | "assistant";
	content: string;
	model?: string;
	timestamp?: string;
}

/**
 * One recorded model call, as IronHermes's `usage_events` table stores it.
 *
 * Cost and cache-creation live HERE, at per-model grain, and deliberately not
 * on `RawSession` (D-15). Live sessions are genuinely multi-model — 38 of them
 * span 27 models across 10 providers — so a session-level scalar for either
 * would flatten them wrong. A session-level rollup is an aggregate query over
 * these rows and is the only sanctioned source for the answer; the source
 * store's own `sessions.cost_usd_micros` column provably disagrees with the sum
 * of its own events on live data and is never read.
 *
 * `provider` and `errorKind` are open vocabularies carried verbatim (D-18). A
 * blank provider is a real recorded value, not a defect, and an `errorKind`
 * appearing in no enumeration in either codebase is emitted unchanged — those
 * calls burned real tokens and real money, and dropping them under-reports what
 * the user actually spent.
 */
export interface RawUsageEvent {
	/**
	 * Source column `usage_events.ts` is epoch MILLISECONDS, unlike
	 * `sessions.started_at` and `messages.timestamp` which are epoch seconds.
	 * The two time bases genuinely differ inside one schema.
	 */
	ts: Date;
	provider: string | null;
	model: string;
	inTok: number;
	outTok: number;
	cacheRead: number;
	cacheCreate: number;
	costUsdMicros: number;
	errorKind: string | null;
}
export interface SessionEventSource {
	adapter: AgentType;
	session_key: string;
	record_id: string;
	record_seq?: number;
	part_index?: number;
}

export interface SessionEventReaction {
	emoji: string;
	author: string;
	at?: string;
	seen?: boolean;
}

export interface SessionEventDisplayMetadata {
	task_count?: number;
	attempt?: number;
	reactions?: SessionEventReaction[];
}

export interface SessionEventSemantics {
	lifecycle: "active" | "compacted" | "inactive";
	display: "message" | "event" | "hidden";
	compressed_summary: boolean;
	display_kind?: string;
	display_metadata?: SessionEventDisplayMetadata;
}

export type SessionContentPart =
	| { type: "text"; text: string }
	| {
			type: "attachment";
			attachment_id: string;
			availability: "external" | "metadata_only";
			uri?: string;
			name?: string;
			media_type?: string;
			size_bytes?: number;
			sha256?: string;
	  };

interface SessionEventBase {
	seq: number;
	event_id: string;
	source: SessionEventSource;
	timestamp?: string;
	semantics?: SessionEventSemantics;
}

export interface SessionMessageEvent extends SessionEventBase {
	type: "message";
	role: "user" | "assistant" | "system" | "developer";
	parts: SessionContentPart[];
	model?: string;
}

export interface SessionToolCallEvent extends SessionEventBase {
	type: "tool_call";
	call_id: string;
	name: string;
	arguments_json?: string;
	model?: string;
}

export interface SessionToolResultEvent extends SessionEventBase {
	type: "tool_result";
	call_id: string;
	name?: string;
	status: "completed" | "error";
	parts: SessionContentPart[];
	result_json?: string;
}

/** Owner-private model reasoning. Display/search projections never consume this event. */
export interface SessionReasoningEvent extends SessionEventBase {
	type: "reasoning";
	kind: "thinking" | "reasoning" | "redacted";
	parts: Array<{ type: "text"; text: string }>;
	/** Canonical JSON containing only non-display provider state such as signatures. */
	payload_json?: string;
	model?: string;
}

export type SessionEvent =
	| SessionMessageEvent
	| SessionToolCallEvent
	| SessionToolResultEvent
	| SessionReasoningEvent;

export interface RawSession {
	localSessionId: string;
	projectPath: string | null;
	startedAt: Date;
	endedAt: Date | null;
	messageCount: number;
	inputTokens: number;
	outputTokens: number;
	cacheReadTokens: number;
	model: string | null;
	modelsUsed: string[];
	durationSeconds: number | null;
	summary: string | null;
	messages: SessionMessage[];
	/** Present only when the source supports strict, stable events-v1. */
	events?: SessionEvent[];
	rawFilePath: string;
	/** Opaque adapter revision used to avoid materializing unchanged backing content. */
	sourceRevision?: string;
	/**
	 * Adapter-classified timestamp of the latest real user input in this
	 * session. `null` means the complete materialized session contains no real
	 * user input; `undefined` means the adapter does not provide this signal.
	 */
	realUserInputAt?: string | null;
	// Set by `pushOneAgent` after collection — sha256 hex of the JSON
	// the CLI is about to upload. Adapters do not populate this.
	contentHash?: string;

	// The three telemetry fields below are populated by `IronHermesAdapter`
	// only. ALL THREE ARE OPTIONAL AND MUST STAY THAT WAY: every adapter builds
	// a `RawSession` object literal, so making one required means editing
	// `claude-code.ts`, `codex.ts`, `openclaw.ts` and `hermes.ts` too.

	/**
	 * The ONE session-level telemetry scalar (D-16).
	 *
	 * It earns a session field because it is the single recorded value with no
	 * per-call analogue — there is no event row to attribute it to. Do NOT add a
	 * sibling `cacheCreationTokens` or `costUsdMicros` here: D-15 puts both on
	 * the event row at per-model grain, `RawUsageEvent` already carries them,
	 * and a session-level duplicate would give one question two answers. The
	 * absence is deliberate, not an oversight.
	 */
	toolCallCount?: number;
	/** This session's whole recorded call set. Orphan rows — events naming a
	 * session the adapter is not emitting — are filtered out at collection, so
	 * the server-side foreign key can stay real. */
	usageEvents?: RawUsageEvent[];
	/**
	 * sha256 hex over a canonical serialization of `usageEvents`, mirroring how
	 * sessions already fingerprint their message content.
	 *
	 * Ingest is replace-by-session and never append (D-22), and this is what
	 * decides whether a session's set needs re-sending at all. The source
	 * table's own row id is a LOCAL autoincrement that a store rebuild or vacuum
	 * can renumber, so it is never a dedupe key — appending against it would
	 * silently double-count recorded spend. Undefined when the session has no
	 * events, so an empty set is distinguishable from a hash of nothing.
	 */
	usageEventSetHash?: string;
}

/**
 * Request for `AgentAdapter.collectSessions`.
 *
 * `projectFilter` restricts to sessions whose stored `cwd` / project path
 * equals or is under the given absolute path. Hermes ignores this — its
 * data model has no project linkage. IronHermes does NOT ignore it: the two
 * runtimes have diverged, and IronHermes records a `workspace_root` per
 * session that the filter matches against by prefix.
 */
export type SessionScanRequest =
	| { kind: "complete"; projectFilter?: string }
	| { kind: "paths"; paths: readonly string[]; projectFilter?: string };

/**
 * Return shape of `AgentAdapter.collectSessions`.
 *
 * `dedupedCount` is non-zero only for `ClaudeCodeAdapter`, which dedupes
 * resume chains: when a newer session's message-uuid set strictly contains
 * an older one's, the older one is recognized as a resume predecessor and
 * dropped from the result. Other adapters return `dedupedCount: 0` because
 * their storage formats don't produce cross-file duplication (e.g. Codex
 * keeps long-conversation history in-file via `compacted` entries).
 */
export interface SessionScanResult {
	sessions: RawSession[];
	dedupedCount: number;
	coverage: "complete" | "partial";
}

export interface SessionScanBatch {
	sessions: RawSession[];
	/** Every local session observed in this batch, including revision-matched sessions. */
	observedLocalSessionIds: readonly string[];
	dedupedCount: number;
}

export interface SessionUserActivity {
	lastUserInputAt: string | null;
	complete: boolean;
}

export interface SessionBatchScan {
	coverage: SessionScanResult["coverage"];
	/** Runtime-wide aggregate from the canonical local inventory. */
	userActivity?: SessionUserActivity;
	batches: AsyncIterable<SessionScanBatch>;
}

export interface SyncReadContext {
	signal: AbortSignal;
}

export interface SessionModule {
	contentProtocol(context?: SyncReadContext): Promise<"events-v1" | "snapshot-v1">;
	collect(request: SessionScanRequest, context?: SyncReadContext): Promise<SessionScanResult>;
	/**
	 * Bounded scan for large or monolithic stores. Implementations may omit it;
	 * callers then treat `collect` as one batch.
	 */
	scan?(
		request: SessionScanRequest,
		knownSourceRevisions: ReadonlyMap<string, string>,
		context?: SyncReadContext,
	): Promise<SessionBatchScan>;
	resolve(localSessionId: string, context?: SyncReadContext): Promise<RawSession | null>;
	/** Paths watched as one backing-store stability group. */
	watchPaths(): string[];
}

export async function scanSessionModule(
	module: SessionModule,
	request: SessionScanRequest,
	knownSourceRevisions: ReadonlyMap<string, string> = new Map(),
	context?: SyncReadContext,
): Promise<SessionBatchScan> {
	context?.signal.throwIfAborted();
	if (module.scan) return module.scan(request, knownSourceRevisions, context);
	const result = await module.collect(request, context);
	context?.signal.throwIfAborted();
	return {
		coverage: result.coverage,
		batches: (async function* () {
			yield {
				sessions: result.sessions,
				observedLocalSessionIds: result.sessions.map((session) => session.localSessionId),
				dedupedCount: result.dedupedCount,
			};
		})(),
	};
}

export interface RawSkill {
	skillKey: string;
	name: string;
	content: string;
	filePath: string;
	directoryPath: string;
	isDirectory: boolean;
	// Set by `push` during the scan phase — the skill folder hash used to
	// diff against the skills-lock. Adapters do not populate this.
	contentHash?: string;
}

export interface SkillModule {
	collect(context?: SyncReadContext): Promise<RawSkill[]>;
	listKeys(context?: SyncReadContext): Promise<string[]>;
	path(key: string): string;
	rootDir(): string;
	sharedPath(skillKey: string, ownerHandle: string): string;
	writeArchive(key: string, tarGzBytes: Buffer): Promise<void>;
	writeSharedArchive(key: string, ownerHandle: string, tarGzBytes: Buffer): Promise<void>;
	remove(key: string): Promise<void>;
}

export interface AgentAdapterCore {
	readonly agentType: AgentType;
	detect(): Promise<boolean>;
	getVersion(): Promise<string | null>;
}

type AtLeastOne<T> = {
	[K in keyof T]-?: Required<Pick<T, K>> & Partial<Omit<T, K>>;
}[keyof T];

/** An adapter is core identity plus at least one complete data module. */
export type AgentAdapter = AgentAdapterCore &
	AtLeastOne<{
		sessions: SessionModule;
		skills: SkillModule;
	}>;

export type AdapterModuleName = "sessions" | "skills";

/** Derive the registration contract from complete modules actually present. */
export function adapterModuleNames(
	adapter: AgentAdapter,
): [AdapterModuleName, ...AdapterModuleName[]] {
	const modules: AdapterModuleName[] = [];
	if (adapter.sessions) modules.push("sessions");
	if (adapter.skills) modules.push("skills");
	if (modules.length === 0) throw new Error(`${adapter.agentType} has no data modules`);
	return modules as [AdapterModuleName, ...AdapterModuleName[]];
}
