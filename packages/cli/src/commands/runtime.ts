import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { join } from "node:path";
import chalk from "chalk";
import { getCliVersion } from "../lib/version";
import {
	type RuntimeAppliedContentIdentity,
	readRuntimeAppliedState,
	runtimeAppliedApplyIdentity,
	runtimeAppliedStateSchema,
	runtimeContentSha256,
	runtimeProviderConflicts,
	runtimeServiceWithdrawals,
	writeRuntimeAppliedState,
} from "../runtime/applied-state";
import {
	type RuntimeApplyContext,
	type RuntimeApplyIdentity,
	readRuntimeApplyContext,
	resolveRuntimeApplyGeneration,
	runtimeApplyIdentitiesEqual,
} from "../runtime/apply-identity";
import { readRuntimeAuthToken } from "../runtime/auth-token";
import { applyRuntimeBundleChannelsToManifestLoad } from "../runtime/channels";
import {
	applyRuntimeCliDesiredState,
	completePendingRuntimeCliUpgrade,
	type RuntimeCliReconciliationResult,
	type RuntimeCliRollbackResult,
	type RuntimeCliUpdateResult,
	reconcilePendingRuntimeCliUpgrade,
	rollbackPendingRuntimeCliUpgrade,
} from "../runtime/cli-update";
import { runCompanionExecutor } from "../runtime/companion-executor";
import { persistComponentActivations } from "../runtime/component-observation";
import {
	applyPostizUpdate,
	CONTAINER_COMPANION_KIND,
	type ContainerCompanionMaintenanceOutcome,
	containerCompanionPaths,
	convergePostizReadiness,
	deliverPostizExportRequests,
	reportContainerCompanionApplyFailure,
	reportContainerCompanionConditions,
	reportPostizRemoved,
	runContainerCompanionMaintenance,
} from "../runtime/container-companion";
import { withRuntimeConvergeLockAsync } from "../runtime/converge-lock";
import { readHostPolicy } from "../runtime/host-policy";
import { failedHostedAgentPluginsObservation } from "../runtime/hosted-agent-plugin-observation";
import {
	cleanupHostedAgentPluginTransientArchives,
	gcHostedAgentPluginArchives,
	type PreparedHostedAgentPlugins,
	prepareHostedAgentPluginPackages,
	readHostedAgentPluginReceipt,
} from "../runtime/hosted-agent-plugin-package";
import type { HostedAgentPluginCommandRunner } from "../runtime/hosted-agent-plugin-runtime";
import { resolveHostedOpenClawWorkspace } from "../runtime/hosted-openclaw-context";
import {
	assertHostedRuntimeContract,
	type HostedRuntimeContractOptions,
} from "../runtime/hosted-runtime-contract";
import type { HostedSkillEvidence } from "../runtime/hosted-skill-evidence";
import {
	gcHostedSkillArchives,
	type PreparedHostedSkill,
	prepareHostedSkillArchives,
} from "../runtime/hosted-sourced-skill-archive";
import {
	cacheRuntimeLastGoodManifest,
	convergeRuntimeManifest,
	loadRuntimeManifest,
	type RuntimePrivateAppliedAuthority,
	type RuntimeResourcePreparationFailures,
	runtimeRecoverableSecretValues,
} from "../runtime/manifest";
import { runtimeConvergenceWithoutApply, runtimeWorkspaceRoot } from "../runtime/manifest-planning";
import {
	loadCommittedRuntimeManifest,
	loadRemoteRuntimeManifest,
	migrateCommittedRuntimeSnapshot,
	pruneRuntimeSnapshots,
	type RuntimeManifestFailure,
	type RuntimeManifestLoad,
	runtimeSnapshotPath,
} from "../runtime/manifest-source";
import { readComponentServiceState } from "../runtime/observed";
import { detectRuntimeMode, getRuntimePaths, type RuntimePaths } from "../runtime/paths";
import { captureRuntimeRunConfigs } from "../runtime/run-config";
import {
	buildRuntimeBootStatus,
	ensureRuntimeStateDirs,
	hostPolicySummary,
	type RuntimeBootStage,
	type RuntimeBootStatus,
	writeRuntimeBootStatus,
	writeRuntimeWatchStatus,
} from "../runtime/state";
import {
	applySystemdRuntimeUpdate,
	assertRuntimeUserCanRead,
	assertSystemdRuntimeIdle,
	RUNTIME_SIDECAR_SYSTEM_UNIT,
	readSystemdUnitSnapshot,
	SystemdReobservationRequiredError,
	withoutStaleSystemdUnits,
} from "../runtime/systemd-transaction";
import { syncRuntimeVaultFiles } from "../runtime/vault-files";
import { log, toErrorMessage } from "../serve/log";
import { consumeSse } from "../serve/sse-client";

interface RuntimeInitOptions {
	nonInteractive?: boolean;
	json?: boolean;
	applyContext?: RuntimeApplyContext;
	hostedRuntimeContract?: HostedRuntimeContractOptions;
}

interface RuntimeWatchOptions {
	intervalMs?: number | string;
	selfHealMs?: number | string;
	once?: boolean;
	json?: boolean;
	abort?: AbortSignal;
	notifications?: boolean;
	notificationConsumer?: typeof consumeSse;
	applyContext?: RuntimeApplyContext;
	hostedRuntimeContract?: HostedRuntimeContractOptions;
	/**
	 * The companion dispatch claimer. Defaults to the shipped
	 * `runCompanionExecutor`; a test substitutes it to assert that the watch
	 * starts one at all, which is the property that was missing rather than the
	 * executor's own behaviour.
	 *
	 * `false` disables it outright, for a watch that must not claim.
	 */
	companionExecutor?: typeof runCompanionExecutor | false;
	/**
	 * The container companion's per-iteration maintenance (plan 06-15: scheduled backups with
	 * rehearsal markers). Defaults to `runWatchContainerCompanionMaintenance`; a test substitutes
	 * it to assert the loop survives it throwing. `false` disables it.
	 */
	containerCompanionMaintenance?: typeof runWatchContainerCompanionMaintenance | false;
}

// The image bootstrap is a RemainAfterExit oneshot with Restart=on-failure.
// A dedicated temporary-failure exit makes it start the newly activated CLI;
// runtime watch instead exits cleanly because its unit uses Restart=always.
const RUNTIME_INIT_CLI_HANDOFF_EXIT_CODE = 75;
const ACTIVE_CLI_VERSION = getCliVersion();
const RUNTIME_WATCH_INTERVAL_MS = 15_000;
const RUNTIME_WATCH_SELF_HEAL_MS = 300_000;
const RUNTIME_WATCH_INITIAL_BACKOFF_MS = 60_000;
const RUNTIME_WATCH_MAX_BACKOFF_MS = 300_000;

export function runtimeWatchPollDelayMs(
	intervalMs: number,
	random: () => number = Math.random,
): number {
	const offset = (random() - 0.5) * intervalMs;
	return Math.round(intervalMs + offset);
}

type RuntimeApplyResult =
	| RuntimeApplyConvergedResult
	| RuntimeApplyDeferredResult
	| RuntimeApplyCliHandoffResult
	| RuntimeApplyCliUpdateFailedResult;

interface RuntimeApplyConvergedResult {
	kind: "converged";
	convergence: ReturnType<typeof convergeRuntimeManifest>;
	cliUpdate: RuntimeCliUpdateResult;
	systemdApply: Omit<ReturnType<typeof applySystemdRuntimeUpdate>, "activated">;
}

interface RuntimeApplyDeferredResult {
	kind: "deferred";
	reason: NonNullable<RuntimeApplyConvergedResult["convergence"]["deferredReason"]>;
	convergence: RuntimeApplyConvergedResult["convergence"];
	cliUpdate: RuntimeCliUpdateResult;
}

interface RuntimeApplyCliHandoffResult {
	kind: "cli_handoff";
	cliUpdate: RuntimeCliUpdateResult;
}

interface RuntimeApplyCliUpdateFailedResult {
	kind: "cli_update_failed";
	cliUpdate: RuntimeCliUpdateResult;
}

interface RuntimeApplyOptions {
	retainedRunConfigs?: ReadonlyMap<string, string>;
	authorityCommit?: (
		convergence: ReturnType<typeof convergeRuntimeManifest>,
		authority: RuntimePrivateAppliedAuthority,
	) => void;
	continueOnCliUpdateError?: boolean;
	recoverFailedSystemdUnits?: boolean;
	requireSystemdApplied?: boolean;
	preparedCliUpdate?: RuntimeCliUpdateResult;
	preparedHostedSourcedSkills?: ReadonlyMap<string, PreparedHostedSkill>;
	preparedHostedAgentPlugins?: PreparedHostedAgentPlugins | null;
	hostedAgentPluginCommandRunner?: HostedAgentPluginCommandRunner;
	hostedRuntimeContract?: HostedRuntimeContractOptions;
	refreshCachedRuntimeProbes?: boolean;
}

export type RuntimeManifestApplyOptions = Omit<
	RuntimeApplyOptions,
	"authorityCommit" | "preparedCliUpdate" | "requireSystemdApplied" | "retainedRunConfigs"
>;

interface RuntimeWatchFailureBackoff {
	backoffMs: number;
	etag: string | null;
	nextRetryAt: number;
}

interface RuntimeWatchTickOptions {
	forceRefresh: boolean;
	recoverFailedSystemdUnits?: boolean;
	failureBackoff?: RuntimeWatchFailureBackoff;
	now: number;
	applyContext?: RuntimeApplyContext;
	hostedRuntimeContract?: HostedRuntimeContractOptions;
}

type ConvergeLoadResult =
	| { kind: "ready"; load: RuntimeManifestLoad }
	| {
			kind: "not_modified";
			sourcePath: string;
			etag: string | null;
			applied: NonNullable<ReturnType<typeof readRuntimeAppliedState>>;
	  }
	| { kind: "failed"; failure: RuntimeManifestFailure }
	| { kind: "error"; error: string; etag?: string }
	| { kind: "idle" };

type ConvergeOutcome =
	| { kind: "idle" }
	| { kind: "reconciliation_error"; error: string }
	| {
			kind: "cli_handoff";
			detail:
				| { kind: "reconciliation"; reconciliation: RuntimeCliReconciliationResult }
				| { kind: "update"; load: RuntimeManifestLoad; cliUpdate: RuntimeCliUpdateResult };
	  }
	| { kind: "load_failed"; failure: RuntimeManifestFailure }
	| ({ kind: "not_modified"; selfReexec: boolean } & Extract<
			ConvergeLoadResult,
			{ kind: "not_modified" }
	  >)
	| {
			kind: "apply_error";
			error: string;
			errors?: string[];
			load?: RuntimeManifestLoad;
			etag?: string;
			cliRollback?: RuntimeCliRollbackResult;
			selfReexec?: boolean;
	  }
	| { kind: "cli_update_failed"; load: RuntimeManifestLoad; cliUpdate: RuntimeCliUpdateResult }
	| {
			kind: "deferred";
			load: RuntimeManifestLoad;
			reason: RuntimeApplyDeferredResult["reason"];
			convergence: RuntimeApplyDeferredResult["convergence"];
	  }
	| {
			kind: "converged";
			load: RuntimeManifestLoad;
			convergence: RuntimeApplyConvergedResult["convergence"];
			cliUpdate: RuntimeCliUpdateResult;
			systemdApply: RuntimeApplyConvergedResult["systemdApply"];
			runtimeErrors: string[];
			resourceProjectionErrors: string[];
			cliRollback: RuntimeCliRollbackResult | null;
			cliRollbackErrors: string[];
			selfReexec: boolean;
	  };

type ConvergePolicy = RuntimeManifestApplyOptions & {
	requireAppliedAuthority?: boolean;
};

/** The watch's own stages, plus the fork's companion loops that run inside it (phase 05/06). */
type RuntimeWatchStage =
	| RuntimeBootStage
	| "cli-update"
	| "watch"
	| "companion-executor"
	| "companion-maintenance";

type RuntimeWatchEventPayload = {
	cliUpdate?: RuntimeCliUpdateResult;
	etag?: string | null;
	selfReexec?: boolean;
	stage?: RuntimeWatchStage;
} & (
	| { status: "applied"; generation: number }
	| { status: "not_modified" }
	| { status: "cli_handoff"; stage: "cli-update"; selfReexec: true }
	| {
			status: "error";
			stage: RuntimeWatchStage;
			errors: string[];
			error: string | undefined;
	  }
);

function cacheRuntimeSourceManifest(load: RuntimeManifestLoad, paths: RuntimePaths): string | null {
	if (load.sourceBundle === undefined) return null;
	return cacheRuntimeLastGoodManifest(load.sourceBundle, paths, load.secretValues, load.manifest);
}

export function runtimeAppliedContentIdentity(
	load: RuntimeManifestLoad,
): RuntimeAppliedContentIdentity {
	return {
		sourcePath: load.sourcePath,
		sha256: runtimeContentSha256({
			manifest: load.sourceBundle ?? load.manifest,
			secretValues: runtimeRecoverableSecretValues(load.manifest, load.secretValues),
		}),
	};
}

// This revision may be surfaced through status/observation fallback fields.
// Keep secret-dependent recoverability verification in the root-only applied state.
export function runtimePublicContentRevision(load: RuntimeManifestLoad): string {
	return runtimeContentSha256({ manifest: load.manifest });
}

// Public boot/watch files must not expose the secret-dependent snapshot SHA.
// Root-only verification and applied provenance retain the actual selected path.
export function runtimePublicSourcePath(load: RuntimeManifestLoad, paths: RuntimePaths): string {
	return paths.mode === "hosted" &&
		load.source === "last-good-cache" &&
		load.sourcePath === runtimeSnapshotPath(paths, runtimeAppliedContentIdentity(load).sha256)
		? paths.manifestLastGood
		: load.sourcePath;
}

function runtimeAppliedStatus(paths: RuntimePaths): {
	activeGeneration: number | null;
	instanceId: string | null;
} {
	const applied = readRuntimeAppliedState(paths);
	return {
		activeGeneration: applied?.generation ?? null,
		instanceId: applied?.instanceId ?? null,
	};
}

export function commitRuntimeAppliedState(input: {
	skillEvidence?: HostedSkillEvidence[];
	load: RuntimeManifestLoad;
	paths: RuntimePaths;
	etag: string;
	sourceRevision: string;
	convergence: ReturnType<typeof convergeRuntimeManifest>;
	applyIdentity: RuntimeApplyIdentity | null;
	activated?: Record<string, string>;
	officialServiceCommandRevisions?: Record<string, string>;
}): void {
	if (
		input.applyIdentity &&
		input.applyIdentity.generation !== resolveRuntimeApplyGeneration(input.convergence.manifest)
	) {
		throw new Error(
			`runtime apply identity generation ${input.applyIdentity.generation} does not match resolved manifest apply generation ${resolveRuntimeApplyGeneration(input.convergence.manifest)}`,
		);
	}
	// The apply identity names the Hosted control-plane snapshot; `etag` names
	// the independently rendered runtime bundle. Persist both authorities.
	const providerIds = runtimeSourceProviderIds(input.load.manifest);
	const providerConflicts = runtimeProviderConflicts(input.convergence.providerConflicts ?? []);
	const serviceWithdrawals = runtimeServiceWithdrawals(input.convergence.serviceWithdrawals ?? []);
	const applied = runtimeAppliedStateSchema.parse({
		schemaVersion: "clawdi.runtimeAppliedState.v2",
		appliedAt: new Date().toISOString(),
		instanceId: input.convergence.manifest.instanceId,
		etag: input.etag,
		sourceRevision: input.sourceRevision,
		generation: input.convergence.manifest.generation,
		...(input.convergence.manifest.applyGeneration === undefined
			? {}
			: { applyGeneration: input.convergence.manifest.applyGeneration }),
		...(input.applyIdentity
			? {
					manifestETag: input.applyIdentity.manifestETag,
					applyReceiptId: input.applyIdentity.applyReceiptId,
					bootNonce: input.applyIdentity.bootNonce,
				}
			: {}),
		contentIdentity: runtimeAppliedContentIdentity(input.load),
		activated: input.activated ?? {},
		officialServiceCommandRevisions: input.officialServiceCommandRevisions ?? {},
		providerIds,
		projectedProviderIds: input.convergence.projectedProviderIds,
		...(input.skillEvidence ? { skillEvidence: input.skillEvidence } : {}),
		nativeCredentialProviderIds: input.convergence.nativeCredentialProviderIds,
		...(providerConflicts.length > 0 ? { providerConflicts } : {}),
		...(serviceWithdrawals.length > 0 ? { serviceWithdrawals } : {}),
		// Omitted when empty, for the same reason the observation omits its field: an unset
		// optional keeps an already-deployed applied-state record byte-identical for every
		// converge that meets no unrecognised kind. The value carried here is the one the
		// collect-set exemption computed, never a second derivation — the skip and the
		// observation must not be able to disagree about which kinds were skipped.
		...(input.convergence.unsupportedCompanionKinds.length > 0
			? { unsupportedCompanionKinds: input.convergence.unsupportedCompanionKinds }
			: {}),
	});
	if (input.load.manifest.recovery.cacheManifest !== false) {
		input.convergence.outputs.manifestLastGood = cacheRuntimeSourceManifest(
			input.load,
			input.paths,
		);
	}
	input.convergence.outputs.appliedState = writeRuntimeAppliedState(applied, input.paths);
	if (input.load.manifest.recovery.cacheManifest === false) {
		input.convergence.outputs.manifestLastGood = cacheRuntimeSourceManifest(
			input.load,
			input.paths,
		);
	} else {
		pruneRuntimeSnapshots(input.paths, [runtimeAppliedContentIdentity(input.load).sha256]);
	}
	persistComponentActivations(input.load, input.paths, (scope, unit) =>
		readComponentServiceState(input.paths, scope, unit),
	);
}

function runtimeSourceProviderIds(manifest: RuntimeManifestLoad["manifest"]): string[] {
	const selectedRuntime = manifest.runtime;
	if (!selectedRuntime) return [];
	return [...new Set(manifest.runtimes[selectedRuntime]?.provider_ids ?? [])].sort();
}

function parsePositiveMs(
	value: number | string | undefined,
	fallback: number,
	name: string,
): number {
	if (value === undefined) return fallback;
	const parsed = typeof value === "number" ? value : Number.parseInt(value, 10);
	if (!Number.isFinite(parsed) || parsed <= 0) {
		throw new Error(`${name} must be a positive integer number of milliseconds`);
	}
	return parsed;
}

interface RuntimeWatchWakeSignal {
	signal: () => void;
	wait: (ms: number, abort?: AbortSignal) => Promise<"notification" | "poll" | "aborted">;
}

function createRuntimeWatchWakeSignal(): RuntimeWatchWakeSignal {
	let queued = false;
	let wake: (() => void) | null = null;
	return {
		signal: () => {
			queued = true;
			wake?.();
		},
		wait: async (ms, abort) => {
			if (abort?.aborted) return "aborted";
			if (queued) {
				queued = false;
				return "notification";
			}
			return new Promise((resolveWait) => {
				let settled = false;
				const finish = (reason: "notification" | "poll" | "aborted") => {
					if (settled) return;
					settled = true;
					clearTimeout(timer);
					abort?.removeEventListener("abort", onAbort);
					wake = null;
					if (reason === "notification") queued = false;
					resolveWait(reason);
				};
				const timer = setTimeout(() => finish("poll"), ms);
				const onAbort = () => finish("aborted");
				wake = () => finish("notification");
				abort?.addEventListener("abort", onAbort, { once: true });
			});
		},
	};
}

interface RuntimeWatchNotificationConfig {
	apiUrl: string;
	apiKey: string;
	environmentId: string;
}

interface RuntimeWatchNotificationSubscription {
	config: RuntimeWatchNotificationConfig;
	abort: AbortController;
	task: Promise<void>;
	settled: boolean;
}

function sameRuntimeWatchNotificationConfig(
	left: RuntimeWatchNotificationConfig,
	right: RuntimeWatchNotificationConfig,
): boolean {
	return (
		left.apiUrl === right.apiUrl &&
		left.apiKey === right.apiKey &&
		left.environmentId === right.environmentId
	);
}

function readRuntimeWatchNotificationConfig(
	paths: ReturnType<typeof getRuntimePaths>,
): RuntimeWatchNotificationConfig | null {
	const apiKey = readRuntimeAuthToken(paths);
	if (!apiKey) return null;
	try {
		const parsed = loadCommittedRuntimeManifest(paths);
		if (!("manifest" in parsed)) return null;
		return {
			apiUrl: parsed.manifest.controlPlane.apiUrl,
			apiKey,
			environmentId: parsed.manifest.environmentId,
		};
	} catch {
		return null;
	}
}

function ensureRuntimeWatchNotificationSubscription(
	current: RuntimeWatchNotificationSubscription | null,
	paths: ReturnType<typeof getRuntimePaths>,
	wakeSignal: RuntimeWatchWakeSignal,
	opts: RuntimeWatchOptions,
): RuntimeWatchNotificationSubscription | null {
	if (opts.once || opts.notifications === false) return current;
	const config = readRuntimeWatchNotificationConfig(paths);
	if (!config) {
		current?.abort.abort();
		return null;
	}
	if (current && !current.settled && sameRuntimeWatchNotificationConfig(current.config, config)) {
		return current;
	}
	current?.abort.abort();
	const abort = new AbortController();
	const consumer = opts.notificationConsumer ?? consumeSse;
	const subscription: RuntimeWatchNotificationSubscription = {
		config,
		abort,
		task: Promise.resolve(),
		settled: false,
	};
	let lastVaultWakeAt = 0;
	let vaultWakeTimer: ReturnType<typeof setTimeout> | null = null;
	const wakeVaults = () => {
		vaultWakeTimer = null;
		lastVaultWakeAt = Date.now();
		wakeSignal.signal();
	};
	abort.signal.addEventListener(
		"abort",
		() => {
			if (vaultWakeTimer) clearTimeout(vaultWakeTimer);
		},
		{ once: true },
	);
	subscription.task = consumer({
		apiUrl: config.apiUrl,
		apiKey: config.apiKey,
		abort: abort.signal,
		onEvent: (event) => {
			if (
				(event.type === "runtime_manifest_changed" || event.type === "runtime_vaults_changed") &&
				event.environment_id === config.environmentId
			) {
				if (event.type === "runtime_manifest_changed") wakeSignal.signal();
				else if (vaultWakeTimer === null) {
					const delay = Math.max(0, 250 - (Date.now() - lastVaultWakeAt));
					if (delay === 0) wakeVaults();
					else vaultWakeTimer = setTimeout(wakeVaults, delay);
				}
			}
		},
		// Runtime-watch keeps ETag polling alive after auth failure. The settled
		// subscription becomes eligible for replacement on the next watch pass.
		onAuthFailure: () => {},
	}).finally(() => {
		subscription.settled = true;
	});
	void subscription.task.catch(() => {
		// The shared SSE consumer already handles transient reconnects. An
		// unexpected terminal failure must not take down the polling fallback.
	});
	return subscription;
}

function runtimeWatchEvent<T extends RuntimeWatchEventPayload>(
	payload: T,
): { schemaVersion: "clawdi.runtimeWatchEvent.v1" } & T {
	return { schemaVersion: "clawdi.runtimeWatchEvent.v1", ...payload };
}

type RuntimeWatchEvent = ReturnType<typeof runtimeWatchEvent>;

function runtimeWatchError<T extends object>(
	stage: Extract<RuntimeWatchEventPayload, { status: "error" }>["stage"],
	errors: string[],
	detail?: T,
): RuntimeWatchEvent {
	return runtimeWatchEvent({ ...detail, status: "error", stage, errors, error: errors[0] });
}

const NO_SYSTEMD_APPLY = {
	applied: false,
	systemUnitsChanged: [],
	userUnitsChanged: [],
} as const;

function emitRuntimeWatchEvent(event: RuntimeWatchEvent, json: boolean | undefined): void {
	if (json) {
		console.log(JSON.stringify(event));
		return;
	}
	if (event.status === "applied") {
		console.log(`runtime watch applied generation ${event.generation ?? "unknown"}`);
		return;
	}
	if (event.status === "cli_handoff") {
		console.log("runtime watch handed off to the managed CLI");
		return;
	}
	if (event.status === "error") {
		console.error(`runtime watch error: ${event.error ?? event.errors?.[0] ?? "unknown error"}`);
	}
}

function emitRuntimeInitStatus(input: {
	opts: RuntimeInitOptions;
	paths: RuntimePaths;
	status: Parameters<typeof buildRuntimeBootStatus>[0];
	persist?: boolean;
	jsonExtras?: Record<string, unknown>;
	render?: (status: RuntimeBootStatus) => void;
}): void {
	const status = buildRuntimeBootStatus(input.status, input.paths);
	if (input.persist !== false) writeRuntimeBootStatus(status, input.paths);
	if (input.opts.json || !process.stdout.isTTY) {
		console.log(
			JSON.stringify(input.jsonExtras ? { ...status, ...input.jsonExtras } : status, null, 2),
		);
	} else {
		input.render?.(status);
	}
	process.exitCode = status.exitCode;
}

function renderRuntimeInit(
	paths: RuntimePaths,
	message: string,
	color: (value: string) => string,
): () => void {
	return () => {
		console.log(chalk.bold("iforge runtime init"));
		console.log(color(`  ${message}`));
		console.log(chalk.gray(`  status: ${paths.bootStatus}`));
	};
}

function emitRuntimeInitRepair(
	input: Pick<RuntimeBootStatus, "bootId" | "stage" | "runtimeMode" | "errors" | "exitCode"> & {
		opts: RuntimeInitOptions;
		paths: RuntimePaths;
		persist?: boolean;
		render?: (status: RuntimeBootStatus) => void;
		active?: ReturnType<typeof runtimeAppliedStatus>;
		rejectedGeneration?: number | null;
		manifestLoad?: RuntimeManifestLoad;
		jsonExtras?: Record<string, unknown>;
	},
): void {
	const {
		opts,
		paths,
		persist,
		render,
		active,
		rejectedGeneration,
		manifestLoad,
		jsonExtras,
		...repair
	} = input;
	emitRuntimeInitStatus({
		opts,
		paths,
		status: {
			...repair,
			mode: "repair",
			status: "error",
			activeGeneration: active?.activeGeneration ?? null,
			...(rejectedGeneration === undefined ? {} : { rejectedGeneration }),
			...(active ? { instanceId: active.instanceId } : {}),
			enabledRuntimes: [],
			error: repair.errors[0],
			datasource: "RuntimeSource",
			hostPolicy: hostPolicySummary(readHostPolicy(paths.hostPolicy)),
			...(manifestLoad
				? {
						manifestSource: {
							type: manifestLoad.source,
							path: manifestLoad.sourcePath,
							offline: manifestLoad.offline,
						},
					}
				: {}),
		},
		persist,
		render,
		jsonExtras,
	});
}

function finishRuntimeInitCliHandoff(input: {
	opts: RuntimeInitOptions;
	paths: ReturnType<typeof getRuntimePaths>;
	mode: "hosted";
	bootId: string;
	hostPolicy: ReturnType<typeof readHostPolicy>;
	manifestLoad?: RuntimeManifestLoad;
	detail:
		| { cliUpdate: RuntimeCliUpdateResult }
		| { reconciliation: RuntimeCliReconciliationResult };
}): void {
	const applied = runtimeAppliedStatus(input.paths);
	emitRuntimeInitStatus({
		opts: input.opts,
		paths: input.paths,
		status: {
			mode: input.manifestLoad?.offline ? "degraded-offline" : "normal",
			status: "ok",
			stage: "config",
			bootId: input.bootId,
			runtimeMode: input.mode,
			activeGeneration: applied.activeGeneration,
			rejectedGeneration: null,
			instanceId: applied.instanceId,
			enabledRuntimes: [],
			errors: [],
			exitCode: RUNTIME_INIT_CLI_HANDOFF_EXIT_CODE,
			datasource: "RuntimeSource",
			hostPolicy: hostPolicySummary(input.hostPolicy),
			...(input.manifestLoad
				? {
						manifestSource: {
							type: input.manifestLoad.source,
							path: input.manifestLoad.sourcePath,
							offline: input.manifestLoad.offline,
						},
					}
				: {}),
		},
		jsonExtras: {
			handoff: "cli_reexec",
			...input.detail,
			selfReexec: true,
		},
		render: renderRuntimeInit(
			input.paths,
			"CLI activated; restarting under the managed binary",
			chalk.green,
		),
	});
}

export async function runtimeInit(opts: RuntimeInitOptions = {}) {
	const paths = getRuntimePaths();
	const mode = detectRuntimeMode();
	const bootId = randomUUID();

	if (mode !== "hosted") {
		emitRuntimeInitRepair({
			opts,
			paths,
			bootId,
			runtimeMode: mode,
			stage: "detect",
			exitCode: 2,
			errors: [
				"runtime init requires IFORGE_RUNTIME_MODE=hosted explicitly; host policy files do not select runtime mode",
			],
			persist: false,
			render: () =>
				console.log(chalk.red("runtime init is only available in hosted runtime mode.")),
		});
		return;
	}
	let applyContext: RuntimeApplyContext;
	try {
		applyContext = opts.applyContext ?? readRuntimeApplyContext();
		assertHostedRuntimeContract(paths, applyContext, {
			...opts.hostedRuntimeContract,
			platformRoots: "deferred",
		});
	} catch (error) {
		const message = toErrorMessage(error);
		emitRuntimeInitRepair({
			opts,
			paths,
			bootId,
			runtimeMode: mode,
			stage: "detect",
			exitCode: 20,
			errors: [message],
			persist: false,
			render: () => console.log(chalk.red(message)),
		});
		return;
	}
	try {
		ensureRuntimeStateDirs(paths);
	} catch (error) {
		emitRuntimeInitRepair({
			opts,
			paths,
			bootId,
			runtimeMode: mode,
			stage: "detect",
			exitCode: 20,
			errors: [`could not create runtime state directories: ${toErrorMessage(error)}`],
			persist: false,
			render: (status) => console.log(chalk.red(status.error)),
		});
		return;
	}
	return withRuntimeConvergeLockAsync(paths, () =>
		runtimeInitLocked({ ...opts, applyContext }, paths, mode, bootId),
	);
}

async function runtimeInitLocked(
	opts: RuntimeInitOptions,
	paths: ReturnType<typeof getRuntimePaths>,
	mode: "hosted",
	bootId: string,
): Promise<void> {
	const hostPolicy = readHostPolicy(paths.hostPolicy);
	const repair = (stage: RuntimeBootStage, errors: string[], exitCode = 23, prefix = ""): void =>
		emitRuntimeInitRepair({
			opts,
			paths,
			bootId,
			runtimeMode: mode,
			stage,
			exitCode,
			errors,
			render: (status) =>
				console.log((exitCode === 20 ? chalk.yellow : chalk.red)(`${prefix}${status.error}`)),
		});
	if (opts.nonInteractive !== true) {
		repair("detect", ["runtime init requires --non-interactive in hosted mode"], 20, "  repair: ");
		return;
	}

	const outcome = await convergeOnce(
		async () => {
			const loaded = await loadRuntimeManifest(paths, { applyContext: opts.applyContext });
			return "errors" in loaded
				? { kind: "failed" as const, failure: loaded }
				: { kind: "ready" as const, load: loaded };
		},
		paths,
		{ hostedRuntimeContract: opts.hostedRuntimeContract },
	);

	if (outcome.kind === "reconciliation_error") {
		repair("local", [outcome.error]);
		return;
	}
	if (outcome.kind === "cli_handoff") {
		finishRuntimeInitCliHandoff({
			opts,
			paths,
			mode,
			bootId,
			hostPolicy,
			...(outcome.detail.kind === "update" ? { manifestLoad: outcome.detail.load } : {}),
			detail:
				outcome.detail.kind === "update"
					? { cliUpdate: outcome.detail.cliUpdate }
					: { reconciliation: outcome.detail.reconciliation },
		});
		return;
	}
	if (outcome.kind === "load_failed") {
		const { failure } = outcome;
		emitRuntimeInitStatus({
			opts,
			paths,
			status: {
				mode: failure.mode,
				status: "error",
				stage: failure.stage,
				bootId,
				runtimeMode: mode,
				activeGeneration: failure.activeGeneration ?? null,
				rejectedGeneration: failure.rejectedGeneration ?? null,
				enabledRuntimes: [],
				error: failure.errors[0],
				errors: failure.errors,
				exitCode: failure.mode === "manifest-rejected" ? 22 : 21,
				datasource: "RuntimeSource",
				hostPolicy: hostPolicySummary(hostPolicy),
			},
			render: renderRuntimeInit(paths, `${failure.mode}: ${failure.errors[0]}`, chalk.yellow),
		});
		return;
	}
	if (outcome.kind === "deferred") {
		const message =
			outcome.reason === "systemd_reobservation_required"
				? "Systemd job outcome is unknown; retry with fresh observation on the next poll"
				: "Hermes config changed during runtime convergence; retry on the next poll";
		const applied = runtimeAppliedStatus(paths);
		const errors = [
			...outcome.convergence.installErrors,
			...outcome.convergence.resourceProjectionErrors,
			message,
		];
		emitRuntimeInitRepair({
			opts,
			paths,
			stage: "final",
			bootId,
			runtimeMode: mode,
			errors,
			exitCode: 23,
			active: applied,
			rejectedGeneration: outcome.load.manifest.generation,
			manifestLoad: outcome.load,
			render: renderRuntimeInit(paths, `repair: ${errors[0]}`, chalk.red),
		});
		return;
	}
	if (outcome.kind === "apply_error" || outcome.kind === "cli_update_failed") {
		const message =
			outcome.kind === "apply_error"
				? outcome.error
				: (outcome.cliUpdate.error ?? "CLI update failed");
		const load = outcome.load;
		const selfReexec = outcome.kind === "apply_error" && outcome.selfReexec === true;
		const applied = runtimeAppliedStatus(paths);
		emitRuntimeInitRepair({
			opts,
			paths,
			stage: outcome.kind === "cli_update_failed" ? "config" : "final",
			bootId,
			runtimeMode: mode,
			errors: outcome.kind === "apply_error" ? (outcome.errors ?? [message]) : [message],
			exitCode: selfReexec ? RUNTIME_INIT_CLI_HANDOFF_EXIT_CODE : 23,
			active: applied,
			rejectedGeneration: load?.manifest.generation ?? null,
			...(outcome.kind === "apply_error" && load ? { manifestLoad: load } : {}),
			jsonExtras: {
				...(outcome.kind === "apply_error" && outcome.cliRollback
					? { cliRollback: outcome.cliRollback }
					: {}),
				...(selfReexec ? { selfReexec: true, handoff: "cli_reexec" } : {}),
			},
			render: renderRuntimeInit(paths, `repair: ${message}`, chalk.red),
		});
		return;
	}
	if (outcome.kind === "converged") {
		const runtimeErrors = [...outcome.runtimeErrors, ...outcome.cliRollbackErrors];
		const runtimeReady = runtimeErrors.length === 0;
		const applied = runtimeAppliedStatus(paths);
		emitRuntimeInitStatus({
			opts,
			paths,
			status: {
				mode: outcome.convergence.mode,
				status: runtimeReady ? "ok" : "error",
				stage: "final",
				bootId,
				runtimeMode: mode,
				activeGeneration: applied.activeGeneration,
				rejectedGeneration: runtimeReady ? null : outcome.convergence.manifest.generation,
				instanceId: applied.instanceId,
				enabledRuntimes: outcome.convergence.enabledRuntimes,
				error: runtimeErrors[0],
				errors: runtimeErrors,
				...(outcome.resourceProjectionErrors.length > 0
					? { resourceProjectionErrors: outcome.resourceProjectionErrors }
					: {}),
				exitCode: outcome.selfReexec ? RUNTIME_INIT_CLI_HANDOFF_EXIT_CODE : runtimeReady ? 0 : 23,
				datasource: "RuntimeSource",
				hostPolicy: hostPolicySummary(hostPolicy),
				manifestSource: {
					type: outcome.convergence.source,
					path: runtimePublicSourcePath(outcome.load, paths),
					offline: outcome.convergence.offline,
				},
				convergence: outcome.convergence.outputs,
			},
			jsonExtras: {
				...(outcome.cliRollback ? { cliRollback: outcome.cliRollback } : {}),
				...(outcome.selfReexec ? { selfReexec: true, handoff: "cli_reexec" } : {}),
			},
			render: renderRuntimeInit(
				paths,
				`${outcome.convergence.mode}: generation ${outcome.convergence.manifest.generation}`,
				chalk.green,
			),
		});
		return;
	}

	const message = "runtime init manifest loader returned no desired state";
	repair("local", [message]);
}

async function runtimeWatchTick(
	paths: ReturnType<typeof getRuntimePaths>,
	opts: RuntimeWatchTickOptions,
): Promise<RuntimeWatchEvent | null> {
	return withRuntimeConvergeLockAsync(paths, async () =>
		runtimeWatchEventForOutcome(
			await convergeOnce(() => loadRuntimeManifestForWatch(paths, opts), paths, {
				continueOnCliUpdateError: true,
				recoverFailedSystemdUnits: opts.recoverFailedSystemdUnits,
				refreshCachedRuntimeProbes: opts.forceRefresh,
				hostedRuntimeContract: opts.hostedRuntimeContract,
				requireAppliedAuthority: true,
			}),
			paths,
		),
	);
}

async function convergeOnce(
	load: () => Promise<ConvergeLoadResult>,
	paths: RuntimePaths,
	policy: ConvergePolicy,
): Promise<ConvergeOutcome> {
	const { requireAppliedAuthority, ...apply } = policy;
	let reconciliation: RuntimeCliReconciliationResult;
	try {
		reconciliation = reconcilePendingRuntimeCliUpgrade(paths, ACTIVE_CLI_VERSION);
	} catch (error) {
		return { kind: "reconciliation_error", error: toErrorMessage(error) };
	}
	if (reconciliation.selfReexec) {
		return { kind: "cli_handoff", detail: { kind: "reconciliation", reconciliation } };
	}

	const requested = await load();
	if (requested.kind === "idle") return requested;
	if (requested.kind === "failed") return { kind: "load_failed", failure: requested.failure };
	if (requested.kind === "error") return { ...requested, kind: "apply_error" };
	if (requested.kind === "not_modified") {
		const completion = completePendingRuntimeCliUpgrade(paths, ACTIVE_CLI_VERSION);
		return { ...requested, selfReexec: completion.selfReexec };
	}

	let convergenceLoad = requested.load;
	let applyResult: RuntimeApplyResult;
	try {
		convergenceLoad = applyRuntimeBundleChannelsToManifestLoad(convergenceLoad, paths);
		if (requireAppliedAuthority && (!convergenceLoad.etag || !convergenceLoad.sourceRevision)) {
			throw new Error("runtime bundle is missing applied authority identity");
		}
		applyResult = await applyRuntimeManifestLoad(convergenceLoad, paths, apply);
	} catch (error) {
		const message = toErrorMessage(error);
		if (error instanceof SystemdReobservationRequiredError) {
			return {
				kind: "deferred",
				load: convergenceLoad,
				reason: "systemd_reobservation_required",
				convergence: runtimeConvergenceWithoutApply({
					load: convergenceLoad,
					paths,
					workspaceRoot: runtimeWorkspaceRoot(convergenceLoad.manifest, paths),
					enabledRuntimes: Object.entries(convergenceLoad.manifest.runtimes)
						.filter(([, runtime]) => runtime.enabled)
						.map(([name]) => name),
					installErrors: [message],
					projectedProviderIds: {},
				}),
			};
		}
		const errors = [message];
		const cliRollback = maybeRollbackFailedCliUpgrade(paths, errors);
		return {
			kind: "apply_error",
			error: message,
			errors,
			load: convergenceLoad,
			...(convergenceLoad.etag ? { etag: convergenceLoad.etag } : {}),
			...(cliRollback.status !== "not_pending"
				? {
						cliRollback,
						selfReexec: cliRollback.status === "rolled_back",
					}
				: {}),
		};
	}
	if (applyResult.kind === "cli_handoff") {
		return {
			kind: "cli_handoff",
			detail: { kind: "update", load: convergenceLoad, cliUpdate: applyResult.cliUpdate },
		};
	}
	if (applyResult.kind === "cli_update_failed") {
		return { kind: "cli_update_failed", load: convergenceLoad, cliUpdate: applyResult.cliUpdate };
	}
	if (applyResult.kind === "deferred") {
		return {
			kind: "deferred",
			load: convergenceLoad,
			reason: applyResult.reason,
			convergence: applyResult.convergence,
		};
	}

	const cliUpdateError =
		applyResult.cliUpdate.status === "error"
			? (applyResult.cliUpdate.error ?? "CLI update failed")
			: null;
	const runtimeErrors = [
		...(cliUpdateError ? [cliUpdateError] : []),
		...applyResult.convergence.installErrors,
	];
	const resourceProjectionErrors = [...applyResult.convergence.resourceProjectionErrors];
	let selfReexec = applyResult.cliUpdate.selfReexec;
	let cliRollback: RuntimeCliRollbackResult | null = null;
	let cliRollbackErrors: string[] = [];
	if (runtimeErrors.length > 0) {
		const rollbackErrors = [...runtimeErrors, ...resourceProjectionErrors];
		const errorCount = rollbackErrors.length;
		cliRollback = maybeRollbackFailedCliUpgrade(paths, rollbackErrors);
		cliRollbackErrors = rollbackErrors.slice(errorCount);
		if (cliRollback.status === "rolled_back") selfReexec = true;
	} else {
		const completion = completePendingRuntimeCliUpgrade(paths, ACTIVE_CLI_VERSION);
		selfReexec = selfReexec || completion.selfReexec;
	}
	return {
		kind: "converged",
		load: convergenceLoad,
		convergence: applyResult.convergence,
		cliUpdate: applyResult.cliUpdate,
		systemdApply: applyResult.systemdApply,
		runtimeErrors,
		resourceProjectionErrors,
		cliRollback,
		cliRollbackErrors,
		selfReexec,
	};
}

async function loadRuntimeManifestForWatch(
	paths: RuntimePaths,
	opts: RuntimeWatchTickOptions,
): Promise<ConvergeLoadResult> {
	if (opts.failureBackoff?.etag === null && opts.now < opts.failureBackoff.nextRetryAt) {
		return { kind: "idle" };
	}
	const active = readRuntimeAppliedState(paths);
	const retryDeferred =
		opts.failureBackoff !== undefined && opts.now < opts.failureBackoff.nextRetryAt;
	const manifestEtag =
		retryDeferred && opts.failureBackoff?.etag
			? opts.failureBackoff.etag
			: opts.forceRefresh
				? undefined
				: (active?.etag ?? undefined);
	const conditional = await loadRemoteRuntimeManifest(paths, {
		ifNoneMatch: manifestEtag,
		applyContext: opts.applyContext,
	});
	if ("errors" in conditional) {
		return retryDeferred &&
			opts.failureBackoff &&
			(!conditional.etag || conditional.etag === opts.failureBackoff.etag)
			? { kind: "idle" }
			: { kind: "failed", failure: conditional };
	}
	const responseEtag = conditional.etag ?? manifestEtag ?? null;
	if (retryDeferred && opts.failureBackoff?.etag === responseEtag) return { kind: "idle" };
	if (
		"notModified" in conditional &&
		active !== null &&
		active.etag === responseEtag &&
		runtimeApplyIdentitiesEqual(
			conditional.applyContext?.identity ?? null,
			runtimeAppliedApplyIdentity(active),
		)
	) {
		// Missing history requires an unconditional fetch to resolve cache policy.
		let snapshotReady = paths.mode !== "hosted";
		if (paths.mode === "hosted") {
			try {
				snapshotReady = migrateCommittedRuntimeSnapshot(paths, conditional.applyContext);
			} catch {
				return {
					kind: "error",
					error: "could not persist verified committed runtime snapshot",
					etag: responseEtag,
				};
			}
		}
		if (snapshotReady)
			return {
				kind: "not_modified",
				sourcePath: conditional.sourcePath,
				etag: responseEtag,
				applied: active,
			};
	}

	try {
		const fresh =
			"notModified" in conditional
				? await loadRemoteRuntimeManifest(paths, { applyContext: opts.applyContext })
				: conditional;
		if ("notModified" in fresh) {
			throw new Error("runtime manifest datasource returned 304 without If-None-Match");
		}
		if ("errors" in fresh) return { kind: "failed", failure: fresh };
		if (retryDeferred && opts.failureBackoff?.etag === fresh.etag) return { kind: "idle" };
		return { kind: "ready", load: fresh };
	} catch (error) {
		return {
			kind: "error",
			error: toErrorMessage(error),
			...(responseEtag ? { etag: responseEtag } : {}),
		};
	}
}

export function runtimeWatchEventForOutcome(
	outcome: ConvergeOutcome,
	paths: RuntimePaths,
): RuntimeWatchEvent | null {
	if (outcome.kind === "idle") return null;
	if (outcome.kind === "deferred") {
		const errors = [
			...outcome.convergence.installErrors,
			...outcome.convergence.resourceProjectionErrors,
		];
		return errors.length > 0
			? runtimeWatchError("final", errors, { etag: outcome.load.etag })
			: null;
	}
	if (outcome.kind === "reconciliation_error") {
		return runtimeWatchError("cli-update", [outcome.error], { selfReexec: false });
	}
	if (outcome.kind === "cli_handoff") {
		if (outcome.detail.kind === "reconciliation") {
			return runtimeWatchEvent({
				status: "cli_handoff",
				stage: "cli-update",
				handoff: "cli_reexec",
				reconciliation: outcome.detail.reconciliation,
				selfReexec: true,
			});
		}
		const applied = runtimeAppliedStatus(paths);
		return runtimeWatchEvent({
			status: "cli_handoff",
			stage: "cli-update",
			handoff: "cli_reexec",
			activeGeneration: applied.activeGeneration,
			desiredGeneration: outcome.detail.load.manifest.generation,
			instanceId: applied.instanceId,
			cliUpdate: outcome.detail.cliUpdate,
			selfReexec: true,
			systemdUnitsChanged: false,
			systemdApply: NO_SYSTEMD_APPLY,
		});
	}
	if (outcome.kind === "load_failed") {
		return runtimeWatchError(outcome.failure.stage, outcome.failure.errors, {
			mode: outcome.failure.mode,
			activeGeneration: outcome.failure.activeGeneration ?? null,
			rejectedGeneration: outcome.failure.rejectedGeneration ?? null,
			...(outcome.failure.etag ? { etag: outcome.failure.etag } : {}),
		});
	}
	if (outcome.kind === "not_modified") {
		return runtimeWatchEvent({
			status: "not_modified",
			sourcePath: outcome.sourcePath,
			etag: outcome.etag,
			sourceRevision: outcome.applied.sourceRevision,
			generation: outcome.applied.generation,
			instanceId: outcome.applied.instanceId,
			selfReexec: outcome.selfReexec,
		});
	}
	if (outcome.kind === "apply_error") {
		return runtimeWatchError("final", outcome.errors ?? [outcome.error], {
			...(outcome.etag ? { etag: outcome.etag } : {}),
			...(outcome.load
				? {
						...runtimeAppliedStatus(paths),
						rejectedGeneration: outcome.load.manifest.generation,
					}
				: {}),
			...(outcome.cliRollback
				? { cliRollback: outcome.cliRollback, selfReexec: outcome.selfReexec }
				: {}),
		});
	}
	if (outcome.kind === "cli_update_failed") {
		const error = outcome.cliUpdate.error ?? "CLI update failed";
		const applied = runtimeAppliedStatus(paths);
		return runtimeWatchError("cli-update", [error], {
			activeGeneration: applied.activeGeneration,
			rejectedGeneration: outcome.load.manifest.generation,
			instanceId: applied.instanceId,
			etag: outcome.load.etag,
			cliUpdate: outcome.cliUpdate,
			selfReexec: outcome.cliUpdate.selfReexec,
			systemdUnitsChanged: false,
			systemdApply: NO_SYSTEMD_APPLY,
		});
	}

	const resourceProjectionOnly =
		outcome.runtimeErrors.length === 0 && outcome.resourceProjectionErrors.length > 0;
	const errors = [
		...outcome.runtimeErrors,
		...outcome.resourceProjectionErrors,
		...outcome.cliRollbackErrors,
	];
	const systemdUnitsChanged =
		outcome.systemdApply.systemUnitsChanged.length > 0 ||
		outcome.systemdApply.userUnitsChanged.length > 0;
	if (errors.length > 0) {
		const agentPlugins = outcome.load.sourceRevision
			? failedHostedAgentPluginsObservation(
					outcome.load.manifest,
					outcome.load.sourceRevision,
					outcome.convergence.agentPluginFailedNames,
				)
			: null;
		const applied = runtimeAppliedStatus(paths);
		return runtimeWatchError(
			outcome.cliUpdate.status === "error" ? "cli-update" : "final",
			errors,
			{
				activeGeneration: applied.activeGeneration,
				rejectedGeneration: resourceProjectionOnly ? null : outcome.convergence.manifest.generation,
				instanceId: applied.instanceId,
				etag: outcome.load.etag,
				cliUpdate: outcome.cliUpdate,
				...(outcome.cliRollback ? { cliRollback: outcome.cliRollback } : {}),
				selfReexec: outcome.selfReexec,
				systemdUnitsChanged,
				systemdApply: outcome.systemdApply,
				convergence: outcome.convergence.outputs,
				...(resourceProjectionOnly ? { healthImpact: "resource_projection" } : {}),
				...(agentPlugins ? { agentPlugins } : {}),
			},
		);
	}
	return runtimeWatchEvent({
		status: "applied",
		sourcePath: runtimePublicSourcePath(outcome.load, paths),
		etag: outcome.load.etag,
		sourceRevision: outcome.load.sourceRevision,
		generation: outcome.convergence.manifest.generation,
		instanceId: outcome.convergence.manifest.instanceId,
		enabledRuntimes: outcome.convergence.enabledRuntimes,
		cliUpdate: outcome.cliUpdate,
		selfReexec: outcome.selfReexec,
		systemdUnitsChanged,
		systemdApply: outcome.systemdApply,
		convergence: outcome.convergence.outputs,
	});
}

function runtimeWatchFailureBackoff(
	previous: RuntimeWatchFailureBackoff | null,
	event: RuntimeWatchEvent,
	now: number,
): RuntimeWatchFailureBackoff {
	const etag = event.etag ?? null;
	const backoffMs = nextBoundedBackoffMs(previous?.etag === etag ? previous.backoffMs : 0);
	return {
		backoffMs,
		etag,
		nextRetryAt: now + backoffMs,
	};
}

export async function applyRuntimeManifestLoad(
	load: RuntimeManifestLoad,
	paths: RuntimePaths,
	opts: RuntimeManifestApplyOptions = {},
): Promise<RuntimeApplyResult> {
	const contentRevision = runtimePublicContentRevision(load);
	const applyIdentity = load.applyContext?.identity ?? null;
	return applyRuntimeDesiredState(load, paths, {
		...opts,
		authorityCommit: (convergence, authority) =>
			commitRuntimeAppliedState({
				load,
				paths,
				etag: load.etag ?? `"sha256:${contentRevision}"`,
				sourceRevision: load.sourceRevision ?? contentRevision,
				convergence,
				applyIdentity,
				activated: authority.activated,
				officialServiceCommandRevisions: authority.officialServiceCommandRevisions,
				skillEvidence: authority.skillEvidence,
			}),
		requireSystemdApplied: applyIdentity !== null,
	});
}

async function applyRuntimeDesiredState(
	load: RuntimeManifestLoad,
	paths: ReturnType<typeof getRuntimePaths>,
	opts: RuntimeApplyOptions = {},
): Promise<RuntimeApplyResult> {
	let preparedHostedAgentPlugins = opts.preparedHostedAgentPlugins;
	const resourcePreparationFailures: RuntimeResourcePreparationFailures = {};
	let preservePreparedAgentPluginArchives = false;
	try {
		let cliUpdate = opts.preparedCliUpdate;
		if (!cliUpdate) {
			try {
				cliUpdate = applyRuntimeCliDesiredState(load.manifest, paths, {
					runningVersion: ACTIVE_CLI_VERSION,
				});
			} catch (error) {
				if (!opts.continueOnCliUpdateError) throw error;
				return {
					kind: "cli_update_failed",
					cliUpdate: runtimeCliUpdateError(load.manifest, paths, error),
				};
			}
		}
		if (cliUpdate.status === "error") {
			if (!opts.continueOnCliUpdateError) {
				throw new Error(cliUpdate.error ?? "CLI update failed");
			}
			return { kind: "cli_update_failed", cliUpdate };
		}
		if (cliUpdate.selfReexec) {
			return { kind: "cli_handoff", cliUpdate };
		}
		if (preparedHostedAgentPlugins === undefined) {
			try {
				preparedHostedAgentPlugins = await prepareHostedAgentPluginPackages(load.manifest, paths, {
					offline: load.offline,
				});
			} catch (error) {
				resourcePreparationFailures.agentPlugins = {
					error: `runtime Agent Plugin package preparation failed: ${toErrorMessage(error)}`,
					installationNames: Object.keys(
						load.manifest.projection?.agentPlugins?.installations ?? {},
					).sort(),
				};
			}
		}
		let preparedHostedSourcedSkills = opts.preparedHostedSourcedSkills;
		if (preparedHostedSourcedSkills === undefined) {
			try {
				preparedHostedSourcedSkills = await prepareHostedSkillArchives(load.manifest, paths, {
					authToken: load.applyContext?.manifestSource.auth.token,
				});
			} catch (error) {
				resourcePreparationFailures.sourcedSkills = `runtime sourced Skill archive preparation failed: ${toErrorMessage(error)}`;
			}
		}
		const previousSystemdUnits = readSystemdUnitSnapshot(paths);
		let systemdApply = {
			applied: false,
			systemUnitsChanged: [] as string[],
			userUnitsChanged: [] as string[],
		};
		let egressPrerequisiteApply: typeof systemdApply | null = null;
		let egressPrerequisiteActivated = false;
		const previousCommitted = load.applyContext
			? loadCommittedRuntimeManifest(paths, load.applyContext)
			: null;
		const previousRunConfigs =
			previousCommitted && "manifest" in previousCommitted
				? captureRuntimeRunConfigs(previousCommitted.manifest, paths)
				: undefined;
		const convergence = convergeRuntimeManifest(load, paths, {
			cacheLastGood: false,
			retainedRunConfigs: opts.retainedRunConfigs,
			hostedRuntimeContract: opts.hostedRuntimeContract,
			refreshCachedRuntimeProbes: opts.refreshCachedRuntimeProbes,
			preparedHostedSourcedSkills,
			...(preparedHostedAgentPlugins ? { preparedHostedAgentPlugins } : {}),
			...(Object.keys(resourcePreparationFailures).length > 0
				? { resourcePreparationFailures }
				: {}),
			...(opts.hostedAgentPluginCommandRunner
				? { hostedAgentPluginCommandRunner: opts.hostedAgentPluginCommandRunner }
				: {}),
			commitAuthority: (committedConvergence, authority) => {
				if (opts.requireSystemdApplied && !systemdApply.applied) {
					throw new Error("systemd apply did not activate the rendered runtime manifest");
				}
				opts.authorityCommit?.(committedConvergence, authority);
			},
			systemdApply: {
				assertIdle: () => assertSystemdRuntimeIdle(paths, previousSystemdUnits),
				activateEgressPrerequisite: () => {
					const candidateSystemdUnits = readSystemdUnitSnapshot(paths);
					try {
						const prerequisite = applySystemdRuntimeUpdate(
							paths,
							previousSystemdUnits,
							candidateSystemdUnits,
							{
								activationScope: {
									systemUnits: [RUNTIME_SIDECAR_SYSTEM_UNIT],
									userUnits: [],
								},
								recoverFailedUnits: opts.recoverFailedSystemdUnits,
								restartChangedUnits: load.source === "last-good-cache",
							},
						);
						if (prerequisite.applied) {
							assertRuntimeUserCanRead(paths.egressSystemCaFile, paths.userHome);
							egressPrerequisiteActivated = true;
						}
						egressPrerequisiteApply = prerequisite;
						return prerequisite;
					} catch (error) {
						if (error instanceof SystemdReobservationRequiredError) throw error;
						throw new Error(
							`transparent-egress prerequisite activation failed: ${toErrorMessage(error)}`,
						);
					}
				},
				activate: ({ staleSystemUnits, staleUserUnits, invalidatedUserUnits }) => {
					// Official installers run after the prerequisite phase and add their
					// base units, so final reconciliation must observe a fresh rendered state.
					const candidateSystemdUnits = readSystemdUnitSnapshot(paths);
					try {
						const activationTarget = withoutStaleSystemdUnits(
							candidateSystemdUnits,
							staleSystemUnits,
							staleUserUnits,
						);
						const activation = applySystemdRuntimeUpdate(
							paths,
							previousSystemdUnits,
							activationTarget,
							{
								recoverFailedUnits: opts.recoverFailedSystemdUnits,
								restartChangedUnits: load.source === "last-good-cache",
								invalidatedUserUnits,
								skipActivatedSystemUnits: egressPrerequisiteActivated
									? [RUNTIME_SIDECAR_SYSTEM_UNIT]
									: [],
							},
						);
						systemdApply = {
							applied: activation.applied && (egressPrerequisiteApply?.applied ?? true),
							systemUnitsChanged: [
								...new Set([
									...(egressPrerequisiteApply?.systemUnitsChanged ?? []),
									...activation.systemUnitsChanged,
								]),
							].sort(),
							userUnitsChanged: [
								...new Set([
									...(egressPrerequisiteApply?.userUnitsChanged ?? []),
									...activation.userUnitsChanged,
								]),
							].sort(),
						};
						return { ...systemdApply, activated: activation.activated };
					} catch (error) {
						if (error instanceof SystemdReobservationRequiredError) throw error;
						throw new Error(`systemd apply failed: ${toErrorMessage(error)}`);
					}
				},
			},
		});
		if (convergence.deferredReason) {
			// Native plugin mutations/receipts may already exist. Keep their
			// archives available for the next observation and reconciliation.
			preservePreparedAgentPluginArchives =
				convergence.deferredReason === "systemd_reobservation_required";
			return {
				kind: "deferred",
				reason: convergence.deferredReason,
				convergence,
				cliUpdate,
			};
		}
		if (
			convergence.installErrors.length > 0 &&
			load.source !== "last-good-cache" &&
			load.applyContext
		) {
			const committed = loadCommittedRuntimeManifest(paths, load.applyContext);
			if ("errors" in committed) {
				convergence.installErrors.push(`last-good replay failed: ${committed.errors.join("; ")}`);
			} else {
				const replayOptions = {
					...opts,
					preparedCliUpdate: cliUpdate,
					retainedRunConfigs: previousRunConfigs,
				};
				delete replayOptions.authorityCommit;
				delete replayOptions.preparedHostedAgentPlugins;
				delete replayOptions.preparedHostedSourcedSkills;
				const replay = await applyRuntimeDesiredState(committed, paths, replayOptions);
				if (replay.kind === "deferred") {
					preservePreparedAgentPluginArchives = true;
					return {
						...replay,
						convergence: {
							...convergence,
							deferredReason: replay.reason,
							installErrors: [...convergence.installErrors, ...replay.convergence.installErrors],
							resourceProjectionErrors: [
								...convergence.resourceProjectionErrors,
								...replay.convergence.resourceProjectionErrors,
							],
						},
					};
				}
				if (replay.kind !== "converged") {
					convergence.installErrors.push(
						`last-good replay failed: runtime ${replay.kind.replaceAll("_", " ")}`,
					);
				} else {
					const replayErrors = [
						...replay.convergence.installErrors,
						...replay.convergence.resourceProjectionErrors,
					];
					if (replayErrors.length > 0) {
						convergence.installErrors.push(`last-good replay failed: ${replayErrors.join("; ")}`);
					} else {
						systemdApply = replay.systemdApply;
						// Replay has validated the committed services again. Its run files
						// may have been restored after a late failure or an interrupted apply.
						// Record that real activation without advancing the applied receipt.
						const applied = readRuntimeAppliedState(paths);
						if (
							systemdApply.applied &&
							applied?.contentIdentity.sha256 === runtimeAppliedContentIdentity(committed).sha256
						) {
							persistComponentActivations(committed, paths, (scope, unit) =>
								readComponentServiceState(paths, scope, unit),
							);
						}
					}
				}
			}
		}
		if (convergence.installErrors.length === 0) {
			preservePreparedAgentPluginArchives = true;
			try {
				gcHostedSkillArchives(load.manifest, paths);
			} catch (error) {
				console.warn(`post-commit Skill archive cleanup deferred: ${toErrorMessage(error)}`);
			}
			try {
				gcHostedAgentPluginArchives(
					readHostedAgentPluginReceipt(paths),
					paths,
					[...(preparedHostedAgentPlugins?.desired.values() ?? [])].map(
						(plugin) => plugin.installation.ownershipIdentity,
					),
				);
			} catch (error) {
				console.warn(`post-commit Agent Plugin archive cleanup deferred: ${toErrorMessage(error)}`);
			}
		}
		await reportContainerCompanionReadiness(load, paths);
		return { kind: "converged", cliUpdate, convergence, systemdApply };
	} finally {
		if (!preservePreparedAgentPluginArchives) {
			cleanupHostedAgentPluginTransientArchives(preparedHostedAgentPlugins ?? null, paths);
		}
	}
}

/**
 * Plan 06-12: once a converge has applied a container companion's units, finish its install over
 * loopback (first-registrant bootstrap, API key seed, data assertion) and report the outcome to
 * the control plane. Runs only after the converge succeeded, and never fails the apply: the
 * install's own success or failure travels as its report, and a report that could not be
 * delivered is retried by the next converge.
 *
 * Plan 06-18: an accepted update runs here too -- the converge's own apply never moves a
 * running installation to a new digest. `applyPostizUpdate` backs up, swaps and asserts, or aborts
 * or rolls back, and reports; it is a no-op when the manifest names the digests already running.
 *
 * Plan 06-37 (CR-04): an apply failure is reported first, from the record the converge wrote. The
 * converge isolates the container kind, so a refusal there no longer fails the agent's converge;
 * it fails the install by name here instead, once, with its redacted reason. A failure on an
 * installation that already reported ready is only logged. The update and readiness steps then
 * run unchanged: with nothing applied, each skips on its own.
 *
 * Plan 06-40 (CR-03): a report the control plane refuses (a 409 other than "already complete") is
 * its answer, never a delivery, and is logged by name at warn: an update whose ready was refused
 * was rolled back (`companion_update_rolled_back`), an install whose ready was refused is settled
 * failed on the host (`companion_install_report_refused`), and a refused apply-failure or
 * removal report is `companion_apply_failure_refused` / `companion_removal_report_refused`.
 *
 * Plan 06-41 (CR-03): an app-digest change the control plane did not announce -- from the digest
 * this host runs to the pin -- is refused before anything runs and logged at warn as
 * `companion_update_unrequested`.
 *
 * Plan 06-45 (G-06-2): the host conditions the converge and the update pass raised or cleared are
 * reported last, each state once (`companion_condition_reported`); a pass that could not deliver
 * them logs `companion_condition_undelivered` and the next one sends them again.
 *
 * Plan 06-46 (WR-07): a rollback that stopped part-way is logged at warn as
 * `companion_update_rollback_incomplete` and raises `update_rollback_incomplete`, which the
 * condition report below sends in the same pass; a later retry of that rollback that completes is
 * logged at info as `companion_update_rollback_repaired` and clears it.
 */
async function reportContainerCompanionReadiness(
	load: RuntimeManifestLoad,
	paths: ReturnType<typeof getRuntimePaths>,
): Promise<void> {
	if (load.manifest.companions?.postiz?.lifecycle === "remove") {
		// Plan 06-20: the converge just removed the companion and its data on the explicit
		// instruction; tell the control plane, which then tears down its platform resources. An
		// undelivered report is sent again by the next converge.
		try {
			const removal = await reportPostizRemoved({
				manifest: load.manifest,
				secretValues: load.secretValues,
				paths,
			});
			if (removal.outcome === "unreportable") {
				log.warn("runtime.companion_removal_report_undelivered", { reason: removal.reason });
			} else if (removal.outcome === "refused") {
				log.warn("runtime.companion_removal_report_refused", { code: removal.code });
			} else if (removal.outcome === "reported") {
				log.info("runtime.companion_removed_reported", { status: removal.status });
			}
		} catch (error) {
			log.warn("runtime.companion_removal_report_failed", { error: toErrorMessage(error) });
		}
		return;
	}
	if (load.manifest.companions?.postiz?.lifecycle !== "present") return;
	try {
		const failure = await reportContainerCompanionApplyFailure({
			manifest: load.manifest,
			secretValues: load.secretValues,
			paths,
		});
		if (failure.outcome === "reported") {
			log.warn("runtime.companion_apply_failure_reported", { status: failure.status });
		} else if (failure.outcome === "refused") {
			log.warn("runtime.companion_apply_failure_refused", {
				code: failure.code,
				terminalStatus: failure.terminalStatus,
			});
		} else if (failure.outcome === "unreportable") {
			log.warn("runtime.companion_apply_failure_undelivered", { reason: failure.reason });
		} else if (failure.outcome === "logged-only") {
			log.warn("runtime.companion_apply_failed", { error: failure.message });
		}
	} catch (error) {
		log.warn("runtime.companion_apply_failure_undelivered", { reason: toErrorMessage(error) });
	}
	try {
		const update = await applyPostizUpdate({
			manifest: load.manifest,
			secretValues: load.secretValues,
			paths,
		});
		if (update.outcome === "unreportable") {
			log.warn("runtime.companion_update_report_undelivered", { reason: update.reason });
		} else if (update.outcome === "unrequested") {
			// Plan 06-41 (CR-03): the entry names an app digest this host does not run and the control
			// plane did not announce that transition, so nothing was changed.
			log.warn("runtime.companion_update_unrequested", { reason: update.reason });
		} else if (update.outcome === "rolled-back") {
			// The control plane refused the update's ready report (plan 06-40, CR-03): the host went
			// back to the previous version from the pre-update backup.
			log.warn("runtime.companion_update_rolled_back", {
				code: update.code,
				label: update.label,
				rollback: update.rollback,
			});
		} else if (update.outcome === "rollback-incomplete") {
			// Plan 06-46 (WR-07): the rollback stopped part-way, so the host state is unknown. Nothing
			// runs over the installation, the condition is reported below, and the same rollback is
			// retried on a backoff. The error is already redacted.
			log.warn("runtime.companion_update_rollback_incomplete", {
				step: update.step,
				code: update.code,
				label: update.label,
				attempts: update.attempts,
				error: update.error,
			});
		} else if (update.outcome === "repaired") {
			log.info("runtime.companion_update_rollback_repaired", {
				label: update.label,
				rollback: update.rollback,
			});
		} else if (update.outcome !== "skipped") {
			log.info("runtime.companion_update_reported", {
				outcome: update.outcome,
				step: update.step,
				label: update.label,
			});
		}
	} catch (error) {
		// An interrupted update changed nothing it did not put back; the next converge starts over
		// from a fresh backup.
		log.warn("runtime.companion_update_failed", { error: toErrorMessage(error) });
	}
	try {
		const outcome = await convergePostizReadiness({
			manifest: load.manifest,
			secretValues: load.secretValues,
			paths,
		});
		if (outcome.outcome === "unreportable") {
			log.warn("runtime.companion_install_report_undelivered", { reason: outcome.reason });
		} else if (outcome.outcome === "refused") {
			log.warn("runtime.companion_install_report_refused", {
				code: outcome.code,
				terminalStatus: outcome.terminalStatus,
				step: outcome.step,
			});
		} else if (outcome.outcome !== "skipped") {
			log.info("runtime.companion_install_reported", {
				outcome: outcome.outcome,
				step: outcome.step,
			});
		}
	} catch (error) {
		log.warn("runtime.companion_install_report_failed", { error: toErrorMessage(error) });
	}
	// Plan 06-45 (G-06-2): after the update and readiness passes, so a host condition either of
	// them raised or cleared reaches the control plane in this same pass. Logs carry the codes and
	// states sent, never a condition's detail.
	try {
		const conditions = await reportContainerCompanionConditions({
			manifest: load.manifest,
			secretValues: load.secretValues,
			paths,
		});
		if (conditions.outcome === "reported") {
			log.info("runtime.companion_condition_reported", { sent: conditions.sent });
		} else if (conditions.outcome === "unreportable") {
			log.warn("runtime.companion_condition_undelivered", { reason: conditions.reason });
		}
	} catch (error) {
		log.warn("runtime.companion_condition_undelivered", { reason: toErrorMessage(error) });
	}
	await deliverContainerCompanionExports(load.manifest, load.secretValues, paths);
}

/**
 * Plan 06-19: answer the entry's `exportRequests` -- dump the database without the platform's API
 * key and upload it for each tenant export job this runtime has not delivered yet. Runs after the
 * converge and on every maintenance pass; never fails either, because a job this pass could not
 * deliver is tried again by the next one and fails by name on the control plane if none does.
 */
async function deliverContainerCompanionExports(
	manifest: RuntimeManifestLoad["manifest"],
	secretValues: Record<string, string> | undefined,
	paths: Pick<RuntimePaths, "serviceStateRoot" | "configurationRoot" | "runRoot">,
): Promise<void> {
	try {
		const outcome = await deliverPostizExportRequests({ manifest, secretValues, paths });
		if (outcome.outcome === "delivered") {
			if (outcome.delivered.length > 0) {
				log.info("runtime.companion_export_delivered", { jobs: outcome.delivered });
			}
			for (const failure of outcome.failed) {
				log.warn("runtime.companion_export_failed", failure);
			}
		}
	} catch (error) {
		log.warn("runtime.companion_export_failed", { error: toErrorMessage(error) });
	}
}

/**
 * Plan 06-15: one maintenance pass for the container companion -- a scheduled backup preceded by
 * a rehearsal marker when one is due -- against the manifest this runtime last COMMITTED.
 *
 * The committed manifest (with its cached secret values) is the authority the installation's
 * units were applied from; a not-modified watch iteration loads no fresh one. Hosts that never
 * applied a container companion stop at the receipt check and read nothing else.
 */
async function runWatchContainerCompanionMaintenance(input: {
	paths: RuntimePaths;
	applyContext: RuntimeApplyContext;
}): Promise<ContainerCompanionMaintenanceOutcome | null> {
	const receiptPath = containerCompanionPaths(input.paths, CONTAINER_COMPANION_KIND).receiptPath;
	if (!existsSync(receiptPath)) return null;
	const committed = loadCommittedRuntimeManifest(input.paths, input.applyContext);
	if ("errors" in committed) return null;
	if (committed.manifest.companions?.postiz?.lifecycle !== "present") return null;
	await deliverContainerCompanionExports(committed.manifest, committed.secretValues, input.paths);
	const outcome = await runContainerCompanionMaintenance({
		paths: input.paths,
		manifest: committed.manifest,
		secretValues: committed.secretValues,
	});
	if (outcome.outcome === "backed-up") {
		log.info("runtime.companion_backup", {
			type: outcome.type,
			label: outcome.label,
			marker: outcome.marker?.id ?? null,
			reported: outcome.reported,
		});
	} else if (outcome.outcome === "failed") {
		log.warn("runtime.companion_backup_failed", {
			type: outcome.type,
			error: outcome.error,
			reported: outcome.reported,
		});
	}
	return outcome;
}

function runtimeCliUpdateError(
	manifest: RuntimeManifestLoad["manifest"],
	paths: ReturnType<typeof getRuntimePaths>,
	error: unknown,
): RuntimeCliUpdateResult {
	const rawRegistry = manifest.clawdiCli?.registry;
	return {
		status: "error",
		packageSpec: manifest.clawdiCli?.packageSpec?.trim() || null,
		registry: typeof rawRegistry === "string" && rawRegistry.trim() ? rawRegistry.trim() : null,
		npmPrefix: paths.cliNpmPrefix,
		npmCache: paths.cliNpmCache,
		activePath: paths.cliManagedBin,
		activeTarget: null,
		version: null,
		retryAt: null,
		selfReexec: false,
		error: toErrorMessage(error),
	};
}

function maybeRollbackFailedCliUpgrade(
	paths: RuntimePaths,
	errors: string[],
): RuntimeCliRollbackResult {
	const rollback = rollbackPendingRuntimeCliUpgrade(
		paths,
		`first converge after CLI upgrade failed: ${errors[0] ?? "unknown error"}`,
	);
	if (rollback.status === "rolled_back") {
		errors.push(
			`rolled back iforge CLI ${rollback.version} to previous version ${rollback.previousVersion ?? "unknown"}`,
		);
	} else if (rollback.status === "error") {
		errors.push(`failed to roll back iforge CLI ${rollback.version}: ${rollback.error}`);
	}
	return rollback;
}

export async function runtimeWatch(opts: RuntimeWatchOptions = {}) {
	const paths = getRuntimePaths();
	const mode = detectRuntimeMode();
	const intervalMs = parsePositiveMs(opts.intervalMs, RUNTIME_WATCH_INTERVAL_MS, "--interval-ms");
	const selfHealMs = parsePositiveMs(opts.selfHealMs, RUNTIME_WATCH_SELF_HEAL_MS, "--self-heal-ms");
	let nextCliInstallRetryAt = 0;
	let vaultRetryAt = 0;
	let vaultBackoffMs = 0;
	let failureBackoff: RuntimeWatchFailureBackoff | null = null;
	const wakeSignal = createRuntimeWatchWakeSignal();
	let notificationSubscription: RuntimeWatchNotificationSubscription | null = null;

	if (mode !== "hosted") {
		const event = runtimeWatchError("detect", ["runtime watch requires hosted runtime mode"]);
		emitRuntimeWatchEvent(event, opts.json);
		process.exitCode = 2;
		return;
	}
	let applyContext: RuntimeApplyContext;
	try {
		applyContext = opts.applyContext ?? readRuntimeApplyContext();
		assertHostedRuntimeContract(paths, applyContext, {
			...opts.hostedRuntimeContract,
			platformRoots: "deferred",
		});
	} catch (error) {
		const message = toErrorMessage(error);
		const event = runtimeWatchError("detect", [message]);
		emitRuntimeWatchEvent(event, opts.json);
		process.exitCode = 20;
		return;
	}

	try {
		ensureRuntimeStateDirs(paths);
	} catch (error) {
		const message = `could not create runtime state directories: ${toErrorMessage(error)}`;
		const event = runtimeWatchError("detect", [message]);
		emitRuntimeWatchEvent(event, opts.json);
		process.exitCode = 20;
		return;
	}

	const companionExecutorAbort = new AbortController();
	const companionAbort = opts.abort
		? AbortSignal.any([opts.abort, companionExecutorAbort.signal])
		: companionExecutorAbort.signal;
	const runExecutor =
		opts.companionExecutor === false ? null : (opts.companionExecutor ?? runCompanionExecutor);
	// Not in `--once` mode: a one-shot tick is the boot-time converge, and a daily backup belongs to
	// the long-running watch, not to the boot path.
	const runMaintenance =
		opts.containerCompanionMaintenance === false || opts.once
			? null
			: (opts.containerCompanionMaintenance ?? runWatchContainerCompanionMaintenance);
	// A claimer that throws must not take the watch down with it. The watch is
	// what keeps the runtime converged; losing convergence because a companion
	// poll failed would trade a working host for a broken one.
	const companionExecutorTask =
		runExecutor === null || opts.once
			? null
			: Promise.resolve(runExecutor({ abort: companionAbort, paths })).catch((error: unknown) => {
					const message = error instanceof Error ? error.message : String(error);
					emitRuntimeWatchEvent(
						{
							schemaVersion: "clawdi.runtimeWatchEvent.v1",
							status: "error",
							stage: "companion-executor",
							error: message,
							errors: [message],
						},
						opts.json,
					);
				});

	try {
		notificationSubscription = ensureRuntimeWatchNotificationSubscription(
			notificationSubscription,
			paths,
			wakeSignal,
			opts,
		);
		// The companion dispatch claimer, started ONCE for the life of the watch.
		//
		// Without it the queue has no claimer: a tenant's agent mints a dispatch
		// row, nothing ever claims it, and the call answers `runtime_unreachable`
		// after the proxy budget. The executor was implemented and tested against
		// a stand-in, but no shipped code path constructed one — so "the executor
		// claims and calls" and "the product runs an executor" were separate
		// claims and only the first was true.
		//
		// It belongs here rather than in its own unit because it needs exactly
		// what the watch already establishes: hosted mode, a verified runtime
		// contract, and a process supervised with Restart=always. A second unit
		// would need all three again.
		//
		// Not started in `--once` mode: a one-shot tick that never returns is not
		// one-shot, and the same reasoning already excludes the notification
		// subscription.
		// Startup work must not consume the self-heal interval. The first watch
		// tick should remain conditional, with full refresh timing measured from
		// the point at which the polling loop is ready.
		let lastFullFetchAt = Date.now();
		for (;;) {
			if (opts.abort?.aborted) return;
			const tickNow = Date.now();
			const cliInstallRetryDue = nextCliInstallRetryAt > 0 && tickNow >= nextCliInstallRetryAt;
			const failureRetryDue = failureBackoff !== null && tickNow >= failureBackoff.nextRetryAt;
			// Full refreshes also re-resolve floating CLI channels when manifest ETags are unchanged.
			const forceRefresh =
				tickNow - lastFullFetchAt >= selfHealMs || cliInstallRetryDue || failureRetryDue;
			const fullFetchAttempted =
				forceRefresh && (!failureBackoff || tickNow >= failureBackoff.nextRetryAt);
			let event: RuntimeWatchEvent | null;
			try {
				event = await runtimeWatchTick(paths, {
					forceRefresh,
					applyContext,
					hostedRuntimeContract: opts.hostedRuntimeContract,
					failureBackoff: failureBackoff ?? undefined,
					now: tickNow,
					// Conditional retries run every 15 seconds. Recover failed units
					// only on the five-minute full refresh, or once in one-shot mode.
					recoverFailedSystemdUnits: forceRefresh || opts.once === true,
				});
			} catch (error) {
				const message = toErrorMessage(error);
				event = runtimeWatchError("watch", [message]);
			}
			if (!event?.selfReexec && Date.now() >= vaultRetryAt) {
				try {
					const config = readRuntimeWatchNotificationConfig(paths);
					if (config) {
						const bundle = loadCommittedRuntimeManifest(paths);
						if (!("manifest" in bundle))
							throw new Error("committed runtime snapshot is unavailable");
						const workspace = bundle.manifest.runtimes.openclaw?.enabled
							? resolveHostedOpenClawWorkspace(paths.userHome)
							: runtimeWorkspaceRoot(bundle.manifest, paths);
						await syncRuntimeVaultFiles({
							...config,
							agentId: config.environmentId,
							home: paths.userHome,
							workspace,
							paths,
							receiptPath: join(paths.serviceStateRoot, "vault-files.json"),
						});
					}
					vaultBackoffMs = 0;
					vaultRetryAt = 0;
				} catch {
					vaultBackoffMs = nextBoundedBackoffMs(vaultBackoffMs);
					vaultRetryAt = Date.now() + vaultBackoffMs;
					console.error(
						"Runtime Vault file sync deferred; retrying without restarting native agents.",
					);
				}
			}
			if (event !== null) {
				const cliUpdateStatus = event.cliUpdate?.status;
				const retryAt = event.cliUpdate?.retryAt;
				const parsedRetryAt = retryAt === null || retryAt === undefined ? NaN : Date.parse(retryAt);
				const cliRetryAt = Number.isFinite(parsedRetryAt) ? parsedRetryAt : null;
				if (cliRetryAt !== null) {
					nextCliInstallRetryAt = cliRetryAt;
				} else if (
					cliUpdateStatus === "installed" ||
					cliUpdateStatus === "current" ||
					cliUpdateStatus === "not_requested"
				) {
					nextCliInstallRetryAt = 0;
				}
				if (event.status === "error" && event.stage !== "cli-update") {
					failureBackoff = runtimeWatchFailureBackoff(failureBackoff, event, Date.now());
				} else if (event.status !== "error" || event.stage === "cli-update") {
					failureBackoff = null;
				}
				if (event.status === "applied" || fullFetchAttempted) lastFullFetchAt = Date.now();
				writeRuntimeWatchStatus(event, paths);
				emitRuntimeWatchEvent(event, opts.json);
			}
			if (runMaintenance !== null) {
				// After the manifest converge, once per iteration. A failing backup is reported and
				// the loop goes on: losing convergence to a backup error would trade a working host
				// for a broken one (T-06-15-06).
				try {
					await runMaintenance({ paths, applyContext });
				} catch (error) {
					const message = error instanceof Error ? error.message : String(error);
					emitRuntimeWatchEvent(
						{
							schemaVersion: "clawdi.runtimeWatchEvent.v1",
							status: "error",
							stage: "companion-maintenance",
							error: message,
							errors: [message],
						},
						opts.json,
					);
				}
			}
			notificationSubscription = ensureRuntimeWatchNotificationSubscription(
				notificationSubscription,
				paths,
				wakeSignal,
				opts,
			);
			if (event !== null && opts.once) {
				if (event.status === "error") process.exitCode = 1;
				else process.exitCode = 0;
				return;
			}
			if (event?.selfReexec === true || opts.abort?.aborted) {
				return;
			}
			// Per-cycle jitter keeps hosts started by the same rollout from
			// synchronizing fallback polls. SSE notifications still wake immediately.
			if ((await wakeSignal.wait(runtimeWatchPollDelayMs(intervalMs), opts.abort)) === "aborted") {
				return;
			}
		}
	} finally {
		notificationSubscription?.abort.abort();
		await notificationSubscription?.task.catch(() => {});
		// Abort BEFORE awaiting: the executor's loop only checks its signal
		// between claims, so awaiting first would block on a long poll that has
		// not been told to stop.
		companionExecutorAbort.abort();
		await companionExecutorTask?.catch(() => {});
	}
}

function nextBoundedBackoffMs(previousMs: number): number {
	if (previousMs <= 0) return RUNTIME_WATCH_INITIAL_BACKOFF_MS;
	return Math.min(previousMs * 2, RUNTIME_WATCH_MAX_BACKOFF_MS);
}
