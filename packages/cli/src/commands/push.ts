import { homedir } from "node:os";
import { resolve as resolvePath } from "node:path";
import * as p from "@clack/prompts";
import chalk from "chalk";
import type { AgentAdapter, RawSession, RawSkill } from "../adapters/base";
import { type AgentType, adapterRegistry } from "../adapters/registry";
import { ApiClient, ApiError, unwrap } from "../lib/api-client";
import { isLoggedIn } from "../lib/config";
import { errMessage } from "../lib/errors";
import { parseModules } from "../lib/prompts";
import {
	adapterForType,
	fetchProjectIdForEnv,
	getEnvIdByAgent,
	resolveTargetAgentTypes,
} from "../lib/select-adapter";
import { computeLastActivityIso } from "../lib/session-activity";
import {
	negotiateSessionProtocol,
	persistSuppressedSession,
	planSessionUpload,
	type SelectedSessionProtocol,
	type SessionUploadPlan,
	sessionFence,
	sessionPlanIsDurablyBlocked,
	syncSessionContent,
} from "../lib/session-upload";
import {
	readFencedSessionEntry,
	readSessionsLock,
	recordFencedUsageEventSetHash,
} from "../lib/sessions-lock";
import { isValidSkillKey } from "../lib/skill-key";
import {
	computeSkillFolderHash,
	readProjectSkillMaterialization,
	readSkillProjectionState,
	recordSkillProjectionClaim,
} from "../lib/skills-lock";
import { type ModuleState, readModuleState, writeModuleState } from "../lib/state";
import { snapshotSkillArchive } from "../lib/tar";

const RESETUP_HINT =
	"This machine's environment is no longer registered. Run `iforge setup` again.";

const UP_MODULES = ["sessions", "skills"] as const;

interface PushOpts {
	modules?: string;
	project?: string;
	excludeProject?: string[];
	all?: boolean;
	allAgents?: boolean;
	dryRun?: boolean;
	agent?: string;
}

/**
 * Most usage events the CLI will put in one `/v1/sessions/usage-events` body.
 *
 * Re-derived from THIS route's row, not copied from the session loop's 500.
 * A `usage_events` row binds ~12 parameters (id, session_id, ts, provider,
 * model, in_tok, out_tok, cache_read, cache_create, cost_usd_micros,
 * error_kind, event_set_hash) against PostgreSQL's 32767 bound-parameter wire
 * ceiling → 32767 / 12 ≈ 2730 rows. 2000 leaves ~8700 parameters of headroom
 * and is an order of magnitude above the heaviest observed session (a couple
 * of hundred events).
 *
 * Deliberately equal to `USAGE_EVENT_BATCH_MAX` in
 * `backend/app/schemas/usage_event.py`: that value is a hard `max_length` on
 * the request, so a client chunking any higher 422s the whole body. The
 * session route's 500 was calibrated against a ~17-column session row after a
 * production incident, and reusing it here would couple two independent
 * chunking rules. Re-derive when the column count changes.
 */
export const USAGE_EVENT_BATCH_CHUNK = 2000;

/** What `uploadOneAgent` actually pushed for a single agent. */
interface AgentUploadResult {
	sessionsCreated: number;
	sessionsUpdated: number;
	sessionsUnchanged: number;
	sessionsSuppressed: number;
	contentUploaded: number;
	skillsPushed: number;
}

/**
 * What `scanOneAgent` found for a single agent: the sessions and skills
 * staged for upload. Splitting scan from upload lets `push` show a
 * combined, per-agent summary across every target agent before uploading,
 * instead of interleaving scan and upload per agent (which made a multi-agent
 * `iforge push` look like it had only picked the first agent).
 */
interface AgentScanResult {
	agentType: AgentType;
	modules: (typeof UP_MODULES)[number][];
	envId: string | null;
	sessions: RawSession[];
	sessionPlans: Map<string, SessionUploadPlan>;
	sessionProtocol: SelectedSessionProtocol | null;
	skills: RawSkill[];
	sessionsCacheSkipped: number;
	sessionsBlocked: number;
	skillsCacheSkipped: number;
	/** Per-agent advisories (hermes filter notice, first-run hint,
	 * exclusion summary) — collected during the scan and rendered
	 * under the agent in the unified summary instead of being printed
	 * mid-scan, so the scan reads as one operation across all agents. */
	notes: string[];
}

/** Whether a scan turned up anything to actually upload. */
function scanHasWork(scan: AgentScanResult): boolean {
	return scan.sessions.length > 0 || scan.skills.length > 0;
}

function supportsPushModule(
	adapter: AgentAdapter,
	module: string,
): module is (typeof UP_MODULES)[number] {
	if (module === "sessions") return adapter.sessions !== undefined;
	if (module === "skills") return adapter.skills !== undefined;
	return false;
}

export async function push(opts: PushOpts) {
	p.intro(chalk.bold("iforge push"));

	if (!opts.dryRun && !isLoggedIn()) {
		p.log.error("Not logged in. Run `iforge auth login` first.");
		p.outro(chalk.red("Aborted."));
		process.exitCode = 1;
		return;
	}

	if (opts.project && opts.excludeProject && opts.excludeProject.length > 0) {
		p.log.error(
			"--project and --exclude-project cannot be combined (--project is positive selection, --exclude-project is subtractive).",
		);
		p.outro(chalk.red("Aborted."));
		process.exitCode = 1;
		return;
	}

	// `--all` widens every axis it can. Explicit narrowing flags
	// (--agent, --modules, --project, --exclude-project) still win
	// per-axis — `--all --agent codex` means "all modules, all
	// projects, but only codex".
	if (opts.all && !opts.agent && !opts.allAgents) {
		opts.allAgents = true;
	}

	const targetTypes = await resolveTargetAgentTypes(opts.agent, !!opts.allAgents);
	if (targetTypes.length === 0) {
		p.outro(chalk.red("Aborted."));
		process.exitCode = 1;
		return;
	}

	// Module default: when --modules is omitted, take every module.
	// `parseModules(undefined, …)` already returns the full list — no
	// multi-select prompt to block an agent harness on.
	const modules = parseModules(opts.modules, UP_MODULES);
	if (!modules) return;
	const explicitlySelectedModules = opts.modules !== undefined;
	const strictModuleSelection = explicitlySelectedModules && targetTypes.length === 1;

	const moduleState = readModuleState();

	// Project selection is agent-independent (derived only from flags), so
	// resolve it once and report it once — not per agent.
	const projectFilter = opts.project ?? (opts.all ? undefined : process.cwd());
	if (modules.includes("sessions")) {
		const target = projectFilter ? `project ${projectFilter}` : "all projects";
		p.log.info(chalk.gray(`Scanning ${target}`));
	}

	// Scan every agent first — one spinner, one combined summary — so a
	// multi-agent push reads as a single scan, not a per-agent block
	// sequence that looks like only the first agent was picked.
	const scanSpinner = p.spinner();
	scanSpinner.start(
		`Scanning ${targetTypes.length} agent${targetTypes.length === 1 ? "" : "s"}...`,
	);
	const scans: AgentScanResult[] = [];
	const moduleSkips: Array<{ agentType: AgentType; modules: string[] }> = [];
	let scanError: string | null = null;
	try {
		for (const agentType of targetTypes) {
			const adapter = adapterForType(agentType);
			if (!adapter) continue;
			const availableModules = modules.filter((module) => supportsPushModule(adapter, module));
			const missing = modules.filter((module) => !supportsPushModule(adapter, module));
			if (strictModuleSelection && missing.length > 0) {
				scanError = `${adapterRegistry[agentType].displayName} does not support ${missing.join(", ")}.`;
				break;
			}
			if (explicitlySelectedModules && missing.length > 0) {
				moduleSkips.push({ agentType, modules: missing });
			}
			if (availableModules.length === 0) continue;
			const scan = await scanOneAgent(adapter, availableModules, opts, projectFilter);
			if ("error" in scan) {
				scanError = scan.error;
				break;
			}
			scans.push(scan);
		}
	} catch (e) {
		// A genuine throw (adapter scan / skill hashing failed) — stop the
		// spinner before the error bubbles to `handleError`.
		scanSpinner.stop("Scan failed.");
		throw e;
	}
	scanSpinner.stop(scanError ? "Scan failed." : "Scan complete.");

	if (scanError) {
		// Scan phase mutates no caches — nothing to persist.
		p.log.error(scanError);
		p.outro(chalk.red("Aborted."));
		process.exitCode = 1;
		return;
	}
	for (const skip of moduleSkips) {
		p.log.warn(
			`${adapterRegistry[skip.agentType].displayName} skipped unsupported ${skip.modules.join(", ")}.`,
		);
	}

	// Combined per-agent summary: every target agent, its counts, and
	// any advisories — visible in full before uploads begin.
	for (const scan of scans) {
		const name = adapterRegistry[scan.agentType].displayName;
		const bits: string[] = [];
		if (scan.modules.includes("sessions")) {
			const sync = scan.sessionsCacheSkipped > 0 ? ` (${scan.sessionsCacheSkipped} in sync)` : "";
			bits.push(`${scan.sessions.length} session${scan.sessions.length === 1 ? "" : "s"}${sync}`);
			if (scan.sessionsBlocked > 0) {
				bits.push(
					`${scan.sessionsBlocked} session${scan.sessionsBlocked === 1 ? "" : "s"} blocked`,
				);
			}
		}
		if (scan.modules.includes("skills")) {
			const sync = scan.skillsCacheSkipped > 0 ? ` (${scan.skillsCacheSkipped} in sync)` : "";
			bits.push(`${scan.skills.length} skill${scan.skills.length === 1 ? "" : "s"}${sync}`);
		}
		p.log.message(`${chalk.bold(name)} — ${bits.join(", ")} to upload`);
		for (const note of scan.notes) {
			p.log.message(chalk.gray(`  ${note}`));
		}
	}

	const toUpload = scans.filter(scanHasWork);

	if (opts.dryRun) {
		p.outro(
			chalk.gray(
				toUpload.length > 0
					? "Dry run complete."
					: "Dry run — nothing to push, everything already in sync.",
			),
		);
		return;
	}

	const totals = {
		cacheSkipped: 0,
		created: 0,
		updated: 0,
		unchanged: 0,
		suppressed: 0,
		content: 0,
		skillsCacheSkipped: 0,
		skills: 0,
	};
	let aborted = false;
	for (const scan of scans) {
		// Cache-skip counts are scan facts — fold them in regardless of
		// whether this agent goes on to upload anything.
		totals.cacheSkipped += scan.sessionsCacheSkipped;
		totals.skillsCacheSkipped += scan.skillsCacheSkipped;
		if (!scanHasWork(scan)) continue;
		// Header only when more than one agent actually uploads — a
		// lone upload block needs no disambiguating label.
		if (toUpload.length > 1) {
			p.log.step(chalk.bold(`▶ ${adapterRegistry[scan.agentType].displayName}`));
		}
		const result = await uploadOneAgent(scan, moduleState);
		if (result === "aborted") {
			aborted = true;
			break;
		}
		totals.created += result.sessionsCreated;
		totals.updated += result.sessionsUpdated;
		totals.unchanged += result.sessionsUnchanged;
		totals.suppressed += result.sessionsSuppressed;
		totals.content += result.contentUploaded;
		totals.skills += result.skillsPushed;
	}

	writeModuleState(moduleState);
	if (aborted) {
		p.outro(chalk.red("Aborted."));
		process.exitCode = 1;
		return;
	}

	const parts: string[] = [];
	if (modules.includes("sessions")) {
		// Merge `cacheSkipped` into `unchanged` for display — they mean the
		// same thing to the user ("this session didn't need any work"). The
		// distinction (client cache hit vs. server hash match) is purely an
		// internal perf metric and only confuses non-technical users.
		const unchangedTotal = totals.cacheSkipped + totals.unchanged;
		parts.push(`${totals.created} new, ${totals.updated} updated, ${unchangedTotal} unchanged`);
		if (totals.suppressed > 0) {
			parts.push(
				`${totals.suppressed} cloud Session${totals.suppressed === 1 ? "" : "s"} kept deleted`,
			);
		}
		parts.push(`${totals.content} content upload${totals.content === 1 ? "" : "s"}`);
	}
	if (modules.includes("skills")) {
		const skillsLabel =
			totals.skillsCacheSkipped > 0
				? `${totals.skills} skill${totals.skills === 1 ? "" : "s"} uploaded, ${totals.skillsCacheSkipped} already in sync`
				: `${totals.skills} skill${totals.skills === 1 ? "" : "s"}`;
		parts.push(skillsLabel);
	}
	parts.push(`across ${targetTypes.length} agent${targetTypes.length === 1 ? "" : "s"}`);
	p.outro(chalk.green(`✓ Push complete — ${parts.join(", ")}`));
}

/**
 * Scan one agent's local data and stage what would be uploaded. Prints
 * nothing and mutates nothing (no cache writes, no network writes —
 * only reads the sessions-lock to diff). Advisories go into the
 * returned `notes` so the caller can render one combined summary
 * across every agent. Returns `{ error }` on an unrecoverable env
 * problem so the caller can surface it after stopping the scan spinner.
 */
async function scanOneAgent(
	adapter: AgentAdapter,
	modules: string[],
	opts: PushOpts,
	projectFilter: string | undefined,
): Promise<AgentScanResult | { error: string }> {
	const agentType = adapter.agentType;
	const envId = getEnvIdByAgent(agentType);
	// True when `projectFilter` came from the cwd default rather than an
	// explicit --project — drives whether "--all" is a useful hint.
	const usedCwdDefault = !opts.all && !opts.project;

	if (!opts.dryRun && !envId) {
		return {
			error: `No environment registered for ${adapterRegistry[agentType].displayName}. Run \`iforge setup\` first.`,
		};
	}

	// Probe the cached env_id before doing any local work. The CLI keeps a
	// per-agent file under ~/.iforge/environments/, but the corresponding row
	// can disappear server-side (account switch, prod reset, env teardown).
	// Catching that here means the user runs `iforge setup` once and is back
	// in business — instead of pushing 60 sessions that all show up as
	// "Unknown" in the dashboard.
	if (!opts.dryRun && envId) {
		const probe = new ApiClient();
		try {
			const res = await probe.GET("/v1/agents/{agent_id}", {
				params: { path: { agent_id: envId } },
			});
			if (res.error || !res.data) {
				const status = res.response?.status ?? 0;
				if (status === 404) return { error: RESETUP_HINT };
				// Anything else (401, network, 5xx) — let the actual upload bubble
				// up the proper error; don't double-report here.
			}
		} catch (e) {
			if (e instanceof ApiError && e.status === 404) return { error: RESETUP_HINT };
			// Same reasoning as above — fall through and let upload surface it.
		}
	}

	const notes: string[] = [];
	const sessionPlans = new Map<string, SessionUploadPlan>();
	let sessionProtocol: SelectedSessionProtocol | null = null;
	const sessionApi = new ApiClient({ requireAuth: !opts.dryRun });
	const excludeSet = new Set<string>(
		(opts.excludeProject ?? []).map((path) => normalizeProject(path)),
	);

	if (agentType === "hermes" && modules.includes("sessions") && projectFilter !== undefined) {
		// `--all` (alone) clears projectFilter, so suggesting it only
		// helps when the filter came from the cwd default.
		notes.push(
			usedCwdDefault
				? "Hermes ignores project filters — pushing all sessions. Use --all to suppress."
				: "Hermes ignores project filters — pushing all sessions.",
		);
	}

	let sessions: RawSession[] = [];
	const skills: RawSkill[] = [];
	let skillsCacheSkipped = 0;
	const sessionsModule = modules.includes("sessions") ? adapter.sessions : undefined;
	if (sessionsModule) {
		// `collectSessions` also reports a `dedupedCount` (resume chains it
		// collapsed). That's internal housekeeping — not actionable and not
		// perceptible to the user — so it isn't surfaced.
		sessionProtocol = opts.dryRun
			? await sessionsModule.contentProtocol()
			: await negotiateSessionProtocol(sessionApi, sessionsModule);
		sessions = (await sessionsModule.collect({ kind: "complete", projectFilter })).sessions;
	}
	if (modules.includes("skills") && adapter.skills) {
		// Always route local Skills through the authenticated Agent claim
		// boundary. Legacy hash-only lock entries are not ownership evidence;
		// skipping on them would leave same-byte Cloud rows unclaimed.
		let invalidSkillCount = 0;
		let projectMaterializationCount = 0;
		let exactClaims = new Map<string, string>();
		if (!opts.dryRun && envId) {
			const skillProjectId = await fetchProjectIdForEnv(new ApiClient(), envId);
			exactClaims = readSkillProjectionState(agentType, envId, skillProjectId).claims;
		}
		for (const skill of await adapter.skills.collect()) {
			if (!isValidSkillKey(skill.skillKey)) {
				invalidSkillCount++;
				continue;
			}
			if (readProjectSkillMaterialization({ agentType, localSkillKey: skill.skillKey })) {
				projectMaterializationCount++;
				continue;
			}
			skill.contentHash = await computeSkillFolderHash(
				skill.directoryPath,
				undefined,
				skill.skillKey,
			);
			if (exactClaims.get(skill.skillKey) === skill.contentHash) skillsCacheSkipped++;
			else skills.push(skill);
		}
		if (invalidSkillCount > 0) {
			notes.push(
				`Skipped ${invalidSkillCount} skill ${invalidSkillCount === 1 ? "directory" : "directories"} with invalid names. Rename local skill directories to letters, numbers, dot, underscore, hyphen, or up to 4 slash-separated components.`,
			);
		}
		if (projectMaterializationCount > 0) {
			notes.push(
				`Skipped ${projectMaterializationCount} Project-owned skill ${projectMaterializationCount === 1 ? "reference" : "references"}; update them from their source Project.`,
			);
		}
	}

	// Apply --exclude-project after scan. Exact-equality match on normalized
	// absolute paths — `~/work` does NOT exclude `~/work/foo` (users say what
	// they mean; prefix-match would silently drop sibling repos).
	if (excludeSet.size > 0 && sessions.length > 0) {
		const before = sessions.length;
		const matchedExcludes = new Set<string>();
		sessions = sessions.filter((s) => {
			if (!s.projectPath) return true;
			const normalized = normalizeProject(s.projectPath);
			if (excludeSet.has(normalized)) {
				matchedExcludes.add(normalized);
				return false;
			}
			return true;
		});
		const removed = before - sessions.length;
		if (removed > 0) {
			notes.push(
				`Excluded ${removed} session${removed === 1 ? "" : "s"} from ${matchedExcludes.size} project${matchedExcludes.size === 1 ? "" : "s"}.`,
			);
		}
		for (const requested of excludeSet) {
			if (!matchedExcludes.has(requested)) {
				notes.push(`--exclude-project ${requested} matched no local sessions; ignored.`);
			}
		}
	}

	// Filter against the sessions-lock cache: any session whose hash matches
	// the stored value can be skipped — the server already has it. This is
	// the per-entity diff that replaces the old global mtime cursor; project
	// filters can't pollute it because each session has its own entry.
	let sessionsCacheSkipped = 0;
	let sessionsBlocked = 0;
	// This snapshot belongs only to the synchronous eligibility pass after collection.
	const sessionsLock = modules.includes("sessions") ? readSessionsLock() : null;
	if (sessionsModule && sessionProtocol && sessionsLock) {
		const before = sessions.length;
		sessions = sessions.filter((s) => {
			const plan = planSessionUpload(s, sessionProtocol);
			sessionPlans.set(s.localSessionId, plan);
			if (!envId) return true;
			const fence = sessionFence(sessionApi, {
				environmentId: envId,
				adapter: agentType,
				sourceSessionKey: s.localSessionId,
			});
			const blocked = sessionPlanIsDurablyBlocked(fence, plan, sessionsLock);
			if (blocked) {
				sessionsBlocked += 1;
				notes.push(blocked);
				return false;
			}
			const cached = readFencedSessionEntry(sessionsLock, fence);
			// Two independent fingerprints, either of which is reason enough to keep the session
			// (phase 02 D-22). Filtering on content alone would drop a session whose messages are
			// untouched but whose recorded calls moved, so its changed event set would never reach
			// the upload step. Runtimes with no telemetry leave `usageEventSetHash` undefined, so
			// this is exactly upstream's predicate for them.
			if (
				s.usageEventSetHash !== undefined &&
				cached?.usage_event_set_hash !== s.usageEventSetHash
			) {
				return true;
			}
			return !(
				cached?.protocol === plan.protocol &&
				cached.local_hash === plan.localHash &&
				cached.pending === undefined
			);
		});
		sessionsCacheSkipped = before - sessions.length - sessionsBlocked;
		const retainedSessionIds = new Set(sessions.map((session) => session.localSessionId));
		for (const id of [...sessionPlans.keys()]) {
			if (!retainedSessionIds.has(id)) sessionPlans.delete(id);
		}
	}

	// Guidance when nothing matched at all.
	if (sessionsLock && sessions.length === 0 && sessionsCacheSkipped === 0) {
		const isFirstRun = !Object.keys(sessionsLock.sessions).some((k) =>
			k.startsWith(`${agentType}:`),
		);
		if (excludeSet.size > 0) {
			notes.push("Nothing left to push after exclusions.");
		} else if (usedCwdDefault && isFirstRun) {
			notes.push(
				`No sessions in ${process.cwd()} — looks like a first run. Use --all to scan every project.`,
			);
		} else if (projectFilter) {
			// Don't suggest --all if the user already passed it (explicit
			// --project alongside --all): --all wouldn't widen anything,
			// the project just didn't match.
			notes.push(
				opts.all
					? "No sessions matched that project — check the --project path."
					: "No sessions matched. Use --all to scan every project, or --project <abs-path>.",
			);
		}
	}

	return {
		agentType,
		modules: modules as (typeof UP_MODULES)[number][],
		envId,
		sessions,
		sessionPlans,
		sessionProtocol,
		skills,
		sessionsCacheSkipped,
		sessionsBlocked,
		skillsCacheSkipped,
		notes,
	};
}

function requireSessionPlan(
	plans: ReadonlyMap<string, SessionUploadPlan>,
	localSessionId: string,
): SessionUploadPlan {
	const plan = plans.get(localSessionId);
	if (!plan) throw new Error(`internal error: missing upload plan for ${localSessionId}`);
	return plan;
}

/**
 * Upload one agent's scanned data. Mutates `moduleState` and both lock
 * caches as uploads land. Returns "aborted" if the server reports the
 * environment vanished mid-batch.
 */
async function uploadOneAgent(
	scan: AgentScanResult,
	moduleState: ModuleState,
): Promise<AgentUploadResult | "aborted"> {
	const { agentType, envId, sessions, sessionPlans, skills } = scan;

	if (!envId) {
		p.log.error("Environment id missing — rerun `iforge setup`.");
		return "aborted";
	}

	const api = new ApiClient();
	let sessionsCreated = 0;
	let sessionsUpdated = 0;
	let sessionsUnchanged = 0;
	let sessionsSuppressed = 0;
	let contentUploaded = 0;
	let skillsPushed = 0;

	if (sessions.length > 0) {
		const sessionSpinner = p.spinner();
		sessionSpinner.start(
			`Uploading metadata for ${sessions.length} session${sessions.length === 1 ? "" : "s"}...`,
		);
		const needsContent: Set<string> = new Set();
		const rejectedIds: Set<string> = new Set();
		const suppressedIds: Set<string> = new Set();
		// Chunk sessions into batches that fit under PostgreSQL's
		// 32767 bound-parameters-per-query limit. The server upsert
		// builds a single multi-VALUES INSERT with ~17 columns per
		// row; in prod we observed
		// `sqlalchemy.exc.InterfaceError: ... query arguments cannot
		// exceed 32767` 500-ing this endpoint when a heavy user's
		// initial backfill shipped 1900+ sessions in one body.
		// Schema-side cap (max_length=500 on SessionBatchRequest)
		// gives the same protection as defense-in-depth.
		const SESSION_BATCH_CHUNK = 500;
		try {
			for (let offset = 0; offset < sessions.length; offset += SESSION_BATCH_CHUNK) {
				const chunk = sessions.slice(offset, offset + SESSION_BATCH_CHUNK);
				const result = unwrap(
					await api.POST("/v1/sessions/batch", {
						body: {
							sessions: chunk.map((s) => {
								const plan = requireSessionPlan(sessionPlans, s.localSessionId);
								return {
									environment_id: envId,
									local_session_id: s.localSessionId,
									project_path: s.projectPath,
									started_at: s.startedAt.toISOString(),
									ended_at: s.endedAt?.toISOString() ?? null,
									last_activity_at: computeLastActivityIso(s),
									duration_seconds: s.durationSeconds,
									message_count: s.messageCount,
									input_tokens: s.inputTokens,
									output_tokens: s.outputTokens,
									cache_read_tokens: s.cacheReadTokens,
									// The ONE session-level telemetry scalar (D-16). `?? 0` because the
									// runtimes with no such counter leave the field undefined, which
									// serializes to an absent key rather than the zero they mean.
									// `serve/sync-engine.ts` builds this same body independently and must
									// carry this field too.
									tool_call_count: s.toolCallCount ?? 0,
									model: s.model,
									models_used: s.modelsUsed,
									summary: s.summary,
									status: "completed",
									content_protocol: plan.protocol,
									content_hash: plan.protocol === "snapshot-v1" ? plan.localHash : null,
								};
							}),
						},
					}),
				);
				for (const id of result.needs_content) needsContent.add(id);
				for (const id of result.suppressed ?? []) suppressedIds.add(id);
				sessionsCreated += result.created;
				sessionsUpdated += result.updated;
				sessionsUnchanged += result.unchanged;
				sessionsSuppressed += result.suppressed?.length ?? 0;
				// Server flagged these ids as cross-env race casualties
				// (see SessionBatchResponse.rejected). They are NOT
				// synced; the caller must skip the lock-write step
				// below so the next push retries. Pre-fix the absence
				// from `needs_content` looked like success and we
				// wrote a stale lock.
				for (const id of result.rejected ?? []) rejectedIds.add(id);
			}
			for (const id of suppressedIds) needsContent.delete(id);
			if (rejectedIds.size > 0) {
				p.log.warn(
					`${rejectedIds.size} session${rejectedIds.size === 1 ? "" : "s"} rejected by server (cross-env race) — will retry on next push`,
				);
			}
			const suppressedSummary =
				sessionsSuppressed > 0 ? `, ${sessionsSuppressed} kept deleted` : "";
			sessionSpinner.stop(
				`Metadata: ${sessionsCreated} new, ${sessionsUpdated} updated, ${sessionsUnchanged} unchanged${suppressedSummary}`,
			);
		} catch (e) {
			sessionSpinner.stop("Session metadata upload failed.");
			// Translate the backend's "unknown_environment" 400 into the same
			// re-setup hint the up-front probe uses. The probe catches the
			// common case; this catches a race where the env was deleted
			// between probe and batch.
			if (e instanceof ApiError && e.status === 400 && e.body.includes("unknown_environment")) {
				p.log.error(RESETUP_HINT);
				return "aborted";
			}
			throw e;
		}

		if (sessions.some((session) => !rejectedIds.has(session.localSessionId))) {
			const contentSpinner = p.spinner();
			contentSpinner.start(
				`Syncing content for ${sessions.length} session${sessions.length === 1 ? "" : "s"}...`,
			);
			for (const s of sessions) {
				const id = s.localSessionId;
				if (rejectedIds.has(id)) continue;
				const plan = requireSessionPlan(sessionPlans, id);
				const fence = sessionFence(api, {
					environmentId: envId,
					adapter: agentType,
					sourceSessionKey: id,
				});
				if (suppressedIds.has(id)) {
					persistSuppressedSession(fence, s, plan);
					continue;
				}
				try {
					const result = await syncSessionContent({
						api,
						fence,
						session: s,
						plan,
						needsSnapshotContent: needsContent.has(id),
					});
					if (result.status === "blocked") {
						p.log.warn(result.message);
					} else if (result.uploaded) {
						contentUploaded += 1;
						contentSpinner.message(
							`Synced content for ${contentUploaded} session${contentUploaded === 1 ? "" : "s"}...`,
						);
					}
				} catch (e) {
					p.log.warn(`Content sync failed for ${s.localSessionId}: ${errMessage(e)}`);
				}
			}
			contentSpinner.stop(
				`Synced ${contentUploaded} changed content session${contentUploaded === 1 ? "" : "s"}`,
			);
		}

		// Per-call usage telemetry, for the runtimes that record any. Runs
		// AFTER the session batch above and only for sessions the server did
		// not reject: the request references a session by its local id and the
		// server resolves that against sessions the principal owns, so an
		// events push that raced ahead of the session upsert would be refused
		// for a session that simply had not landed yet.
		const eventful = sessions.filter(
			(s) => !rejectedIds.has(s.localSessionId) && s.usageEvents?.length && s.usageEventSetHash,
		);
		if (eventful.length > 0) {
			const eventSpinner = p.spinner();
			eventSpinner.start(`Uploading usage events for ${eventful.length} session...`);
			let sent = 0;
			let skippedUnchanged = 0;
			for (const s of eventful) {
				const id = s.localSessionId;
				const events = s.usageEvents ?? [];
				// Unchanged set → post nothing at all for this session. The server
				// would answer `unchanged` anyway, but the cheapest request is the
				// one not made, and this is the common case on every re-push.
				const fence = sessionFence(api, {
					environmentId: envId,
					adapter: agentType,
					sourceSessionKey: id,
				});
				// Re-read: the content sync above rewrote this session's fenced entry.
				const cached = readFencedSessionEntry(readSessionsLock(), fence);
				if (cached?.usage_event_set_hash === s.usageEventSetHash) {
					skippedUnchanged++;
					continue;
				}
				// Ingest is replace-by-session (D-22): the server deletes this
				// session's rows and inserts the incoming body wholesale. A set
				// split across two requests would therefore either be no-op'd by
				// the second request's matching hash — silently dropping its rows —
				// or delete the first request's rows outright. Both corrupt
				// recorded spend, so an over-cap set is refused LOUDLY and its
				// hash is left unrecorded, which keeps it retrying and visible
				// rather than quietly half-ingested or truncated.
				if (events.length > USAGE_EVENT_BATCH_CHUNK) {
					p.log.warn(
						`Session ${id} records ${events.length} usage events, above the ${USAGE_EVENT_BATCH_CHUNK}-event limit for one request — skipped rather than split, which would drop or duplicate recorded spend.`,
					);
					continue;
				}
				try {
					unwrap(
						await api.POST("/v1/sessions/usage-events", {
							body: {
								environment_id: envId,
								local_session_id: id,
								event_set_hash: s.usageEventSetHash ?? "",
								events: events.map((e) => ({
									// The adapter already read this column at its own
									// millisecond base; this is a straight serialization.
									ts: e.ts.toISOString(),
									provider: e.provider,
									model: e.model,
									in_tok: e.inTok,
									out_tok: e.outTok,
									cache_read: e.cacheRead,
									cache_create: e.cacheCreate,
									cost_usd_micros: e.costUsdMicros,
									error_kind: e.errorKind,
								})),
							},
						}),
					);
					// Recorded ONLY once the set landed: recording it beside a failed or
					// refused push would mark never-delivered spend as delivered.
					recordFencedUsageEventSetHash(fence, s.usageEventSetHash ?? "");
					sent++;
				} catch (e) {
					// Best-effort, exactly like the content upload above: the session
					// metadata is already committed. The hash stays unrecorded so the
					// next push retries this session's set.
					p.log.warn(`Usage events skipped for ${id}: ${errMessage(e)}`);
				}
			}
			eventSpinner.stop(
				`Usage events: ${sent} session${sent === 1 ? "" : "s"} uploaded, ${skippedUnchanged} unchanged`,
			);
		}
		moduleState[`sessions:${agentType}`] = { lastActivityAt: new Date().toISOString() };
	}

	if (skills.length > 0) {
		// Skill upload uses project-explicit URLs. Resolve
		// THIS agent's env's default_project_id directly — not the
		// auth key's "most recently active env" heuristic. With a
		// multi-agent setup on an unbound CLI key, the latter would
		// route a `claude_code` push under whichever env was
		// touched last (often `codex` from the previous push),
		// while sessions still wrote correctly to `envId`. The
		// `claude_code` daemon would never see these skills because
		// its reconcile listing is tied to its own project.
		if (!envId) {
			throw new Error(
				`internal error: skill push without envId for ${agentType}; the early-return guard above should have caught this`,
			);
		}
		const skillProjectId = await fetchProjectIdForEnv(api, envId);

		// `skills` is already the to-upload set — the scan phase hashed
		// every skill and dropped the ones already in sync.
		const skillSpinner = p.spinner();
		skillSpinner.start(`Uploading ${skills.length} skill${skills.length === 1 ? "" : "s"}...`);
		let pushed = 0;
		const skipped: { key: string; reason: string }[] = [];
		try {
			for (const skill of skills) {
				// Pass skill_key so nested Hermes layouts archive
				// entries under `category/foo/...` (matching the
				// cloud key), not just `foo/...` which would violate the
				// projection archive-root contract.
				const snapshot = await snapshotSkillArchive(skill.directoryPath, undefined, skill.skillKey);
				try {
					await api.uploadAgentSkill(
						envId,
						skillProjectId,
						skill.skillKey,
						snapshot.archive,
						`${skill.skillKey}.tar.gz`,
						snapshot.hash,
					);
					pushed++;
					// Cache key is partitioned by `(agentType, skillKey)`: in
					// multi-agent push, agent A and agent B can have the same
					// `foo` content under different projects; a flat skill_key
					// cache would say "B already in sync" the moment A pushed,
					// leaving B's project missing the skill.
					recordSkillProjectionClaim({
						agentType,
						agentId: envId,
						projectId: skillProjectId,
						skillKey: skill.skillKey,
						hash: snapshot.hash,
					});
				} catch (e) {
					// 413 = upstream (Cloudflare / nginx) refused the body. Almost
					// always a single oversized skill; skip it and keep going so
					// one fat tarball doesn't kill the whole batch. Other errors
					// (auth, 5xx, network) still bubble out and abort.
					//
					// Prefer the status code; fall back to a body match only when the
					// edge masks the status (some Cloudflare error pages serve 502
					// with "413 Request Entity Too Large" in the HTML body). Body
					// regex is anchored to a word boundary so an unrelated 4XX whose
					// body happens to contain "413" doesn't get silently skipped.
					const is413 =
						e instanceof ApiError &&
						(e.status === 413 ||
							(typeof e.body === "string" &&
								/(?:^|[^0-9])413(?:[^0-9]|$)|payload too large/i.test(e.body)));
					if (!is413) throw e;
					const mb = (snapshot.archive.length / 1024 / 1024).toFixed(1);
					skipped.push({ key: skill.skillKey, reason: `${mb} MB exceeds upload limit` });
				}
				skillSpinner.message(`Uploading skills (${pushed + skipped.length}/${skills.length})...`);
			}
			const summary = [`Pushed ${pushed} skill${pushed === 1 ? "" : "s"}`];
			if (skipped.length > 0) {
				summary.push(`skipped ${skipped.length} (too large)`);
			}
			skillSpinner.stop(summary.join(", "));
			for (const s of skipped) {
				p.log.warn(`Skipped ${s.key} — ${s.reason}`);
			}
			skillsPushed = pushed;
		} catch (e) {
			skillSpinner.stop(`Failed after ${pushed} skill${pushed === 1 ? "" : "s"}.`);
			throw e;
		}
		moduleState.skills = { lastActivityAt: new Date().toISOString() };
	}

	return {
		sessionsCreated,
		sessionsUpdated,
		sessionsUnchanged,
		sessionsSuppressed,
		contentUploaded,
		skillsPushed,
	};
}

function normalizeProject(input: string): string {
	// Expand `~` ourselves — `path.resolve` doesn't do tilde expansion, so a
	// shell-less caller (e.g. an agent invoking the CLI directly) that passes
	// `~/scratch` would otherwise get `<cwd>/~/scratch`, which never matches.
	let expanded = input;
	if (expanded === "~") expanded = homedir();
	else if (expanded.startsWith("~/")) expanded = `${homedir()}${expanded.slice(1)}`;
	return resolvePath(expanded);
}
