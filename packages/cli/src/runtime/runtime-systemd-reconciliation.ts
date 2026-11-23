import { createHash } from "node:crypto";
import {
	chmodSync,
	chownSync,
	existsSync,
	lstatSync,
	mkdirSync,
	readdirSync,
	readFileSync,
	rmdirSync,
	rmSync,
} from "node:fs";
import { dirname, isAbsolute, join } from "node:path";
import { BRAND_ENV_ALIASES } from "../lib/brand-env";
import { writePrivateFileAtomic } from "../lib/private-file";
import { ensureDirectoryWithinTrustedRoot } from "../lib/trusted-directory";
import { log } from "../serve/log";
// `FILE_BROWSER_COMPANION_KIND` was a module-local literal in this file until 04-08. It now comes
// from the registry, typed `KnownCompanionKind`, so a registry that stopped carrying the key fails
// `tsc --noEmit` here rather than leaving the unit writer naming an account no row owns.
import {
	FILE_BROWSER_COMPANION_KIND,
	KNOWN_COMPANION_KINDS,
	UNSUPPORTED_COMPANIONS_MAX,
} from "./companion-registry";
import {
	applyContainerCompanion,
	CONTAINER_COMPANION_KIND,
	type ContainerCompanionApplyFailure,
	type ContainerCompanionApplyResult,
	ContainerCompanionConditionError,
	type ContainerCompanionPaths,
	type ContainerCompanionRemoveResult,
	type ContainerCompanionWithdrawResult,
	type ContainerHost,
	clearContainerCompanionApplyFailure,
	containerCompanionApplyFailure,
	containerCompanionPaths,
	containerCompanionServices,
	nodeContainerHost,
	planContainerCompanion,
	probePostizRestore,
	raiseContainerCompanionCondition,
	readContainerCompanionFacts,
	recordContainerCompanionApplyFailure,
	removeContainerCompanion,
	withdrawContainerCompanion,
} from "./container-companion";
import { applyEgressTransparentRuntimeEnv } from "./egress-env";
import { companionServiceNames } from "./file-browser-isolation";
import { HOSTED_RUNTIME_USER } from "./hosted-runtime-contract";
import type { PostizCompanionManifest, RuntimeManifest } from "./manifest-contract";
import {
	runtimeCommandCurrentRevision,
	runtimeCommandPath,
	runtimeFileCurrentRevision,
	writeRuntimeInstallerLog,
} from "./manifest-install";
import { runtimeRecoverableSecretValues } from "./manifest-secrets";
import type { RuntimeMitmproxyEnsureResult } from "./mitmproxy-fetch";
import {
	DEFAULT_RUN_ROOT,
	DEFAULT_SERVICE_STATE_ROOT,
	type RuntimePaths,
	SYSTEMD_PLATFORM_DIRECTORY,
} from "./paths";
import {
	type RuntimeName,
	type RuntimeRunConfig,
	type RuntimeServiceName,
	withoutPathEntry,
} from "./run-config";
import {
	daemonProgramRevision,
	runtimeImpactRevision,
	runtimeServiceProgramRevision,
	runtimeSidecarProgramRevision,
} from "./runtime-impact-revision";
import {
	commandResolvable,
	executableExists,
	makeRuntimeUserOwned,
	runningAsRoot,
	runRuntimeUserCommand,
	runtimeEgressGid,
	runtimeEgressUid,
	spawnRuntimeUserCommand,
	withRuntimeUserFileAccess,
} from "./runtime-user-command";
import { runtimeSecretValue } from "./secret-values";
import { isGeneratedRuntimeSystemdPath, managedRuntimeSystemdUnitEntries } from "./systemd";
import {
	GENERATED_RUNTIME_SYSTEMD_FILE_HEADER,
	isGeneratedRuntimeSystemdFile,
	isOwnedRuntimeSystemdEntry,
	RUNTIME_SYSTEM_UNIT_DAEMON,
	RUNTIME_SYSTEM_UNIT_NAMES,
	RUNTIME_SYSTEM_UNIT_SIDECAR,
	RUNTIME_SYSTEM_UNIT_WATCH,
	RUNTIME_SYSTEMD_DROP_IN_FILE,
	RUNTIME_SYSTEMD_DROP_IN_FILE_NAMES,
	RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME,
	RUNTIME_SYSTEMD_OWNED_NAME_PREFIX,
} from "./systemd-user";
import { TRANSPARENT_EGRESS_PORT } from "./transparent-egress";

/**
 * The Cloud bearer token in both spellings: the internal register and the public `IFORGE_*` name a
 * child `iforge` process re-derives it from. Native services unset both (T-03.1G-01).
 *
 * The internal name is derived from the alias map, never typed as a literal: this file builds
 * child environments, and `brand-env-exclusive-boundary.test.ts` forbids an internal contract
 * register name written here.
 */
const RUNTIME_AUTH_TOKEN_ENV_NAMES: readonly string[] = runtimeAuthTokenEnvNames();

function runtimeAuthTokenEnvNames(): readonly string[] {
	const publicName = "IFORGE_AUTH_TOKEN";
	const internalName = BRAND_ENV_ALIASES[publicName];
	if (internalName === undefined) throw new Error(`${publicName} has no internal register name`);
	return [internalName, publicName];
}

export interface RuntimeSystemdUserProgram {
	programKind: "runtime" | "file-browser";
	runtime: RuntimeName;
	service: RuntimeServiceName | null;
	command: string;
	args: string[];
	cwd: string;
	env: Record<string, string>;
	resolvedSecretEnv: Record<string, string>;
}

export interface RuntimeEgressSystemdProgram {
	profileBundlePath: string;
	envFilePath: string;
	transparentPort: number;
	addonPath: string;
	addonSha256: string;
	engine: Extract<RuntimeMitmproxyEnsureResult, { status: "ready" }>;
	systemCaBundle: string;
	secretFilePath: string | null;
}

export interface OfficialRuntimeServicePlan {
	pending: Array<{
		unitName: string;
		program: RuntimeSystemdUserProgram;
		serviceRevision: string | null;
	}>;
	serviceRevisions: Record<string, string>;
}

export interface RuntimeSystemdStaleFilePlan {
	platformFiles: string[];
	userFiles: string[];
	systemUnits: string[];
	userUnits: string[];
	/**
	 * The companion registry keys this converge declined to reconcile because this build does not
	 * recognise the kind, sorted. Reported (COMP-02/D-06), never withdrawn (D-14).
	 *
	 * Produced by the SAME derivation that drives the collect-set exemption below, so the skip and
	 * the observation cannot disagree about which kinds were skipped.
	 */
	unsupportedCompanionKinds: string[];
}

interface RuntimeEgressIdentity {
	runtimeUid: number;
	runtimeGid: number;
	egressUid: number;
	egressGid: number;
}

function runtimeEgressSystemdProgram(
	paths: RuntimePaths,
	profileBundlePath: string | null,
	secretFilePath: string | null,
	engine: RuntimeMitmproxyEnsureResult | null,
	addon: { path: string; sha256: string } | null,
): RuntimeEgressSystemdProgram | null {
	if (!profileBundlePath) return null;
	if (engine?.status !== "ready") return null;
	if (!addon) return null;
	return {
		profileBundlePath,
		envFilePath: paths.egressTransparentEnv,
		transparentPort: TRANSPARENT_EGRESS_PORT,
		addonPath: addon.path,
		addonSha256: addon.sha256,
		engine,
		systemCaBundle: paths.egressSystemCaFile,
		secretFilePath,
	};
}

export function resolveRuntimeSystemdIdentity(input: {
	paths: RuntimePaths;
	profileBundlePath: string | null;
	secretFilePath: string | null;
	engine: RuntimeMitmproxyEnsureResult | null;
	addon: { path: string; sha256: string } | null;
	runtimeIdentity: { uid: number; gid: number };
}): {
	egressProgram: RuntimeEgressSystemdProgram | null;
	identity: RuntimeEgressIdentity | null;
} {
	const egressProgram = runtimeEgressSystemdProgram(
		input.paths,
		input.profileBundlePath,
		input.secretFilePath,
		input.engine,
		input.addon,
	);
	if (!egressProgram) return { egressProgram: null, identity: null };
	return {
		egressProgram,
		identity: {
			runtimeUid: input.runtimeIdentity.uid,
			runtimeGid: input.runtimeIdentity.gid,
			egressUid: runtimeEgressUid(),
			egressGid: runtimeEgressGid(),
		},
	};
}

export function buildRuntimeSystemdUserProgram(input: {
	config: RuntimeRunConfig;
	paths: RuntimePaths;
	secretValues: Record<string, string> | undefined;
	egress: Pick<RuntimeEgressSystemdProgram, "systemCaBundle"> | null;
}): RuntimeSystemdUserProgram | null {
	if (!input.config.enabled) return null;

	const currentPath = withoutPathEntry(
		runtimeSystemdPath(input.paths),
		dirname(input.paths.cliManagedBin),
	);
	const pathPrefix = input.config.prependPath.join(":");
	const env: Record<string, string> = {
		...input.config.env,
		PATH: pathPrefix ? [pathPrefix, currentPath].filter(Boolean).join(":") : currentPath,
	};
	const resolvedSecretEnv: Record<string, string> = {};
	for (const [envName, ref] of Object.entries(input.config.secretEnv)) {
		const value = runtimeSecretValue(input.secretValues ?? {}, ref);
		if (!value) {
			throw new Error(`Runtime secret ${ref} for ${envName} is unavailable.`);
		}
		env[envName] = value;
		resolvedSecretEnv[envName] = value;
	}
	if (input.egress) {
		applyEgressTransparentRuntimeEnv(env, { caFile: input.egress.systemCaBundle });
	}

	const command =
		input.config.commandPath && existsSync(input.config.commandPath)
			? input.config.commandPath
			: input.config.command;

	return {
		programKind: "runtime",
		runtime: input.config.runtime,
		service: input.config.service,
		command,
		args: input.config.defaultArgs,
		cwd: input.config.cwd ?? input.paths.workspaceRoot,
		env,
		resolvedSecretEnv,
	};
}

/** The Hermes dashboard's unit name -- a special case with no official descriptor behind it. */
export const RUNTIME_SYSTEMD_HERMES_DASHBOARD_UNIT_NAME = `${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}hermes-dashboard`;

function runtimeSystemdProgramName(program: RuntimeSystemdUserProgram): string {
	if (program.programKind === "file-browser") return RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME;
	const officialName = officialRuntimeSystemdProgramName(program);
	if (officialName) return officialName;
	if (!program.service) {
		return `${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}${systemdUnitNameSegment(program.runtime)}`;
	}
	return runtimeServiceProgramName(program.runtime, program.service);
}

function officialRuntimeSystemdProgramName(program: RuntimeSystemdUserProgram): string | null {
	return officialRuntimeServiceDescriptorForProgram(program)?.programName ?? null;
}

function runtimeSystemdProgramRevision(
	manifest: RuntimeManifest,
	program: RuntimeSystemdUserProgram,
	secretValues: Record<string, string> | undefined,
	providerProjectionRevisions: Partial<Record<string, string | null>> = {},
	runtimeRevision: (
		manifest: RuntimeManifest,
		runtime: string,
		secretValues: Record<string, string> | undefined,
		providerProjectionRevision: string | null,
	) => string,
): string {
	if (program.programKind === "file-browser") {
		return runtimeImpactRevision({
			companion: manifest.companions?.filebrowser ?? null,
			providerProjectionRevision: null,
		});
	}
	if (program.service) return runtimeServiceProgramRevision(program);
	return runtimeRevision(
		manifest,
		program.runtime,
		secretValues,
		providerProjectionRevisions[program.runtime] ?? null,
	);
}

export function runtimeServiceProgramName(runtime: string, service: string): string {
	const official = OFFICIAL_RUNTIME_SERVICE_DESCRIPTORS.find(
		(descriptor) => descriptor.runtime === runtime && descriptor.service === service,
	);
	if (official) return official.programName;
	if (runtime === "hermes" && service === "dashboard") {
		return RUNTIME_SYSTEMD_HERMES_DASHBOARD_UNIT_NAME;
	}
	return `${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}${systemdUnitNameSegment(runtime)}-${systemdUnitNameSegment(service)}`;
}

function systemdUnitNameSegment(value: string): string {
	return value.replace(/[^A-Za-z0-9_-]+/g, "-");
}

function runtimeSystemdPath(paths: RuntimePaths): string {
	const inheritedPath =
		process.env.PATH || "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";
	return [
		...new Set([
			paths.userLocalBin,
			join(paths.userHome, ".openclaw", "bin"),
			...inheritedPath.split(":"),
		]),
	].join(":");
}

function systemdUnitFileName(name: string): string {
	return `${systemdUnitNameSegment(name)}.service`;
}

export function runtimeSystemdUserUnitName(program: RuntimeSystemdUserProgram): string {
	return systemdUnitFileName(runtimeSystemdProgramName(program));
}

function systemdDropInFilePath(paths: RuntimePaths, unitName: string): string {
	return join(
		paths.systemdUserRoot,
		`${systemdUnitFileName(unitName)}.d`,
		RUNTIME_SYSTEMD_DROP_IN_FILE,
	);
}

function systemdQuote(value: string): string {
	if (/[\r\n]/.test(value)) {
		throw new Error("systemd unit values must be single-line strings");
	}
	return `"${value
		.replace(/\\/g, "\\\\")
		.replace(/"/g, '\\"')
		.replace(/%/g, "%%")
		.replace(/\$/g, "$$")}"`;
}

function systemdExec(command: string, args: string[]): string {
	return [command, ...args].map(systemdQuote).join(" ");
}

function fileBrowserSystemdExec(command: string, config: string): string {
	return systemdExec(command, ["-c", config]);
}

function fileBrowserVersionProbeExec(command: string, version: string, commit: string): string {
	const script =
		'output=$("$1" version 2>&1) || exit $?; case "$output" in *"$2"*) ;; *) exit 65 ;; esac; case "$output" in *"$3"*) ;; *) exit 65 ;; esac';
	return systemdExec("/bin/sh", ["-c", script, "sh", command, version, commit.slice(0, 7)]);
}

function systemdPath(value: string): string {
	if (!isAbsolute(value)) {
		throw new Error(`systemd unit paths must be absolute: ${value}`);
	}
	if (/[\r\n]/.test(value)) {
		throw new Error("systemd unit paths must be single-line strings");
	}
	return value
		.replace(/\\/g, "\\\\")
		.replace(/%/g, "%%")
		.replace(/ /g, "\\x20")
		.replace(/\t/g, "\\x09");
}

function systemdUnitEnvironmentLines(values: Record<string, string>): string[] {
	return Object.entries(values).map(
		([key, value]) => `Environment=${systemdQuote(`${key}=${value}`)}`,
	);
}

export function systemdEnvironmentFilePath(paths: RuntimePaths, unitName: string): string {
	return join(paths.systemdEnvRoot, `${systemdUnitFileName(unitName)}.env`);
}

function systemdEnvironmentFileQuote(value: string): string {
	if (/[\r\n]/.test(value)) {
		throw new Error("systemd environment files only support single-line values");
	}
	return `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

export type OfficialRuntimeServiceDescriptor = {
	runtime: RuntimeName;
	programName: string;
	command: string;
	installArgs: string[];
	uninstallArgs: string[];
	// Resolved in memory for the installer subprocess; strict native auth may omit it from the unit.
	installSecretEnv?: readonly string[];
	// Manifest `services` key the official unit corresponds to; used for
	// program naming even when such an entry is not official for the runtime.
	service: string;
	// Which desired programs the official unit covers. Deliberately
	// asymmetric: openclaw's default program is its gateway, while hermes may
	// express the gateway as the default program or an explicit
	// `services.gateway` entry.
	matchesProgram: (program: RuntimeSystemdUserProgram) => boolean;
};

export const OFFICIAL_RUNTIME_SERVICE_DESCRIPTORS: OfficialRuntimeServiceDescriptor[] = [
	{
		runtime: "openclaw",
		programName: "openclaw-gateway",
		command: "openclaw",
		installArgs: ["gateway", "install", "--force", "--json"],
		uninstallArgs: ["gateway", "uninstall"],
		installSecretEnv: ["OPENCLAW_GATEWAY_TOKEN"],
		service: "gateway",
		matchesProgram: (program) => !program.service,
	},
	{
		runtime: "hermes",
		programName: "hermes-gateway",
		command: "hermes",
		installArgs: ["gateway", "install", "--force", "--no-start-now"],
		uninstallArgs: ["gateway", "uninstall"],
		service: "gateway",
		matchesProgram: (program) => (program.service ?? program.args[0] ?? "") === "gateway",
	},
];

function officialRuntimeServiceDescriptorForProgram(
	program: RuntimeSystemdUserProgram,
): OfficialRuntimeServiceDescriptor | null {
	return (
		OFFICIAL_RUNTIME_SERVICE_DESCRIPTORS.find(
			(descriptor) => descriptor.runtime === program.runtime && descriptor.matchesProgram(program),
		) ?? null
	);
}

function officialRuntimeServiceDescriptorForUnit(
	unitName: string,
): OfficialRuntimeServiceDescriptor | null {
	return (
		OFFICIAL_RUNTIME_SERVICE_DESCRIPTORS.find(
			(descriptor) => systemdUnitFileName(descriptor.programName) === unitName,
		) ?? null
	);
}

function officialRuntimeServiceCommand(
	descriptor: OfficialRuntimeServiceDescriptor,
	paths: RuntimePaths,
): string {
	const commandPath = runtimeCommandPath(descriptor.runtime, paths.userHome);
	return commandPath && executableExists(commandPath) ? commandPath : descriptor.command;
}

function officialRuntimeServiceRevision(
	program: RuntimeSystemdUserProgram,
	paths: RuntimePaths,
): string | null {
	const descriptor = officialRuntimeServiceDescriptorForProgram(program);
	if (!descriptor) return null;
	const unitName = systemdUnitFileName(descriptor.programName);
	const unitPath = join(paths.systemdUserRoot, unitName);
	let contents: Buffer;
	try {
		const unitStat = lstatSync(unitPath);
		if (!unitStat.isFile()) throw new Error(`official ${unitName} unit is not a regular file`);
		contents = readFileSync(unitPath);
	} catch (error) {
		if (error instanceof Error && "code" in error && error.code === "ENOENT") return null;
		throw error;
	}
	if (isGeneratedRuntimeSystemdFile(contents.toString("utf8"))) return null;
	const command = officialRuntimeServiceCommand(descriptor, paths);
	const commandRevision =
		descriptor.runtime === "hermes"
			? runtimeFileCurrentRevision(command)
			: runtimeCommandCurrentRevision(command, paths.userHome, paths.userHome);
	// An unavailable observation is not evidence that a valid service needs reinstalling.
	if (!commandRevision) {
		throw new Error(
			`official ${unitName} command revision is unavailable; refusing service reinstall`,
		);
	}
	return createHash("sha256").update(commandRevision).update("\0").update(contents).digest("hex");
}

export function planOfficialRuntimeServices(
	programs: RuntimeSystemdUserProgram[],
	paths: RuntimePaths,
	executeInstallers: boolean,
): OfficialRuntimeServicePlan {
	const pending: OfficialRuntimeServicePlan["pending"] = [];
	const serviceRevisions: Record<string, string> = {};
	if (!executeInstallers) return { pending, serviceRevisions };
	for (const program of officialRuntimeSystemdPrograms(programs)) {
		const unitName = systemdUnitFileName(runtimeSystemdProgramName(program));
		const serviceRevision = officialRuntimeServiceRevision(program, paths);
		if (serviceRevision) serviceRevisions[unitName] = serviceRevision;
		// Native updaters own service refresh. Fingerprint drift alone is not a repair request.
		if (!serviceRevision) {
			pending.push({ unitName, program, serviceRevision });
		}
	}
	return { pending, serviceRevisions };
}

const OFFICIAL_SERVICE_INSTALL_TIMEOUT_MS = 600_000;
const OFFICIAL_SERVICE_UNINSTALL_TIMEOUT_MS = 120_000;
const HERMES_DASHBOARD_INSTALL_TIMEOUT_MS = 600_000;
const HERMES_DASHBOARD_BUILD_TIMEOUT_MS = 900_000;
export const HERMES_DASHBOARD_BUILD_REVISION_FILE = ".clawdi-runtime-revision";

function writeSystemdEnvironmentFile(input: {
	paths: RuntimePaths;
	name: string;
	owner: "root" | "runtime-user";
	env: Record<string, string>;
}): string {
	ensureDirectoryWithinTrustedRoot(input.paths.runRoot, input.paths.systemdRuntimeRoot, {
		mode: 0o711,
	});
	chmodSync(input.paths.systemdRuntimeRoot, 0o711);
	ensureDirectoryWithinTrustedRoot(input.paths.runRoot, input.paths.systemdEnvRoot, {
		mode: 0o711,
	});
	// This is a deliberate handoff directory: tenant-owned 0600 environment
	// files must be traversable without making sibling platform files readable.
	chmodSync(input.paths.systemdEnvRoot, 0o711);
	const path = systemdEnvironmentFilePath(input.paths, input.name);
	const lines = Object.entries(input.env)
		.sort(([a], [b]) => a.localeCompare(b))
		.map(([key, value]) => {
			if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
				throw new Error(`invalid systemd environment key: ${key}`);
			}
			return `${key}=${systemdEnvironmentFileQuote(value)}`;
		});
	const content = `${GENERATED_RUNTIME_SYSTEMD_FILE_HEADER}\n${lines.join("\n")}\n`;
	writeSystemdManagedFile({
		path,
		content,
		mode: 0o600,
		dirMode: 0o711,
		trustedRoot: input.paths.runRoot,
		owner: input.owner,
	});
	return path;
}

function writeSystemdManagedFile(input: {
	path: string;
	content: string;
	mode: number;
	dirMode: number;
	trustedRoot: string;
	owner: "root" | "runtime-user";
}): void {
	let unchanged = false;
	try {
		const current = lstatSync(input.path);
		unchanged =
			current.isFile() && current.nlink === 1 && readFileSync(input.path, "utf8") === input.content;
	} catch {
		// A missing or unreadable target is replaced by the trusted atomic writer below.
	}
	if (unchanged) chmodSync(input.path, input.mode);
	else {
		writePrivateFileAtomic(input.path, input.content, {
			mode: input.mode,
			dirMode: input.dirMode,
			trustedRoot: input.trustedRoot,
		});
	}
	if (input.owner === "runtime-user") makeRuntimeUserOwned(input.path);
	else if (runningAsRoot()) chownSync(input.path, 0, 0);
}

function writeSystemdProgramEnvironment(input: {
	paths: RuntimePaths;
	name: string;
	owner: "root" | "runtime-user";
	env: Record<string, string>;
}): string {
	return writeSystemdEnvironmentFile(input);
}

function withRuntimeUserSystemdFiles<T>(
	operation: () => T & (T extends PromiseLike<unknown> ? never : unknown),
): T {
	return withRuntimeUserFileAccess(operation);
}

function writeSystemdUnit(input: {
	root: string;
	owner: "root" | "runtime-user";
	paths: RuntimePaths;
	name: string;
	description: string;
	command: string;
	args: string[];
	cwd: string;
	env: Record<string, string>;
	unitEnv?: Record<string, string>;
	execStart?: string;
	serviceType?: "simple" | "oneshot" | "notify";
	restart?: boolean;
	directoryKind?: "platform" | { companionKind: string };
	extraUnitLines?: string[];
	extraServiceLines?: string[];
	unsetEnvironment?: readonly string[];
	wantedBy: "multi-user.target" | "default.target";
}): string {
	const path = join(input.root, systemdUnitFileName(input.name));
	const envFile = writeSystemdProgramEnvironment({
		paths: input.paths,
		name: input.name,
		owner: input.owner,
		env: input.env,
	});
	const lines = [
		GENERATED_RUNTIME_SYSTEMD_FILE_HEADER,
		"[Unit]",
		`Description=${input.description}`,
		...(input.owner === "runtime-user"
			? [
					"# The environment file is regenerated by convergence each boot; this unit must not start before it exists.",
					`ConditionPathExists=${systemdPath(envFile)}`,
				]
			: []),
		...(input.extraUnitLines ?? []),
		"",
		"[Service]",
		`Type=${input.serviceType ?? "simple"}`,
		`WorkingDirectory=${systemdPath(input.cwd)}`,
		...(input.directoryKind === "platform"
			? [
					`ConfigurationDirectory=${SYSTEMD_PLATFORM_DIRECTORY}`,
					"ConfigurationDirectoryMode=0700",
					`StateDirectory=${SYSTEMD_PLATFORM_DIRECTORY}`,
					"StateDirectoryMode=0700",
					`CacheDirectory=${SYSTEMD_PLATFORM_DIRECTORY}`,
					"CacheDirectoryMode=0700",
					// Runtime state is prepared before convergence: the boot prep unit owns
					// the production root for the entire boot, while ensureRuntimeStateDirs()
					// creates non-default roots. Do not bind the shared root to this service.
				]
			: typeof input.directoryKind === "object"
				? // PER KIND, through the single registry-key naming helper, so two companion kinds
					// cannot land on one state directory (T-4-06). Mode stays 0700 and is not a
					// per-kind parameter.
					[
						`StateDirectory=${companionServiceNames(input.directoryKind.companionKind).stateDirectory}`,
						"StateDirectoryMode=0700",
						`RuntimeDirectory=${companionServiceNames(input.directoryKind.companionKind).stateDirectory}`,
						"RuntimeDirectoryMode=0700",
					]
				: []),
		...(input.unitEnv ? systemdUnitEnvironmentLines(input.unitEnv) : []),
		...(input.unsetEnvironment?.length
			? [`UnsetEnvironment=${input.unsetEnvironment.join(" ")}`]
			: []),
		...(input.extraServiceLines ?? []),
		`EnvironmentFile=${systemdPath(envFile)}`,
		`ExecStart=${input.execStart ?? systemdExec(input.command, input.args)}`,
		...(input.restart === false
			? []
			: ["Restart=always", "RestartSec=2", "KillMode=mixed", "TimeoutStopSec=30"]),
		"",
		"[Install]",
		`WantedBy=${input.wantedBy}`,
		"",
	];
	const writeUnitFile = (): string => {
		if (input.owner === "runtime-user") mkdirSync(input.root, { recursive: true });
		ensureDirectoryWithinTrustedRoot(input.root, input.root);
		writeSystemdManagedFile({
			path,
			content: lines.join("\n"),
			mode: 0o644,
			dirMode: 0o755,
			trustedRoot: input.root,
			owner: input.owner,
		});
		return path;
	};
	return input.owner === "runtime-user"
		? withRuntimeUserSystemdFiles(writeUnitFile)
		: writeUnitFile();
}

function writeSystemdSystemUnit(
	input: Omit<Parameters<typeof writeSystemdUnit>[0], "root" | "owner" | "wantedBy">,
): string {
	return writeSystemdUnit({
		...input,
		root: input.paths.systemdSystemRoot,
		owner: "root",
		wantedBy: "multi-user.target",
	});
}

function writeSystemdUserUnit(
	input: Omit<Parameters<typeof writeSystemdUnit>[0], "root" | "owner" | "wantedBy">,
): string {
	return writeSystemdUnit({
		...input,
		root: input.paths.systemdUserRoot,
		owner: "runtime-user",
		wantedBy: "default.target",
	});
}

function writeSystemdUserEnvironmentDropIn(input: {
	paths: RuntimePaths;
	name: string;
	env: Record<string, string>;
	unsetEnvironment?: readonly string[];
}): string {
	const unitName = systemdUnitFileName(input.name);
	const envFile = writeSystemdProgramEnvironment({
		paths: input.paths,
		name: input.name,
		owner: "runtime-user",
		env: input.env,
	});
	const path = systemdDropInFilePath(input.paths, input.name);
	const lines = [
		GENERATED_RUNTIME_SYSTEMD_FILE_HEADER,
		"# IforgeHostedRuntimeDropIn=v1",
		"# The base unit is generated by the runtime's official service installer.",
		"[Unit]",
		"# The environment file is regenerated by convergence each boot; this unit must not start before it exists.",
		`ConditionPathExists=${systemdPath(envFile)}`,
		"",
		"[Service]",
		...(input.unsetEnvironment?.length
			? [`UnsetEnvironment=${input.unsetEnvironment.join(" ")}`]
			: []),
		`EnvironmentFile=${systemdPath(envFile)}`,
		"",
	];
	withRuntimeUserSystemdFiles(() => {
		removeGeneratedRuntimeBaseUnit(input.paths, unitName);
		mkdirSync(input.paths.systemdUserRoot, { recursive: true });
		ensureDirectoryWithinTrustedRoot(input.paths.systemdUserRoot, dirname(path));
		writeSystemdManagedFile({
			path,
			content: lines.join("\n"),
			mode: 0o644,
			dirMode: 0o755,
			trustedRoot: input.paths.systemdUserRoot,
			owner: "runtime-user",
		});
	});
	return join(input.paths.systemdUserRoot, unitName);
}

function removeSystemdUserEnvironmentDropIn(paths: RuntimePaths, name: string): void {
	const path = systemdDropInFilePath(paths, name);
	withRuntimeUserSystemdFiles(() => {
		rmSync(path, { force: true });
		if (existsSync(dirname(path)) && readdirSync(dirname(path)).length === 0) {
			rmdirSync(dirname(path));
		}
	});
}

function removeGeneratedRuntimeBaseUnit(paths: RuntimePaths, unitName: string): void {
	const path = join(paths.systemdUserRoot, unitName);
	if (!isGeneratedRuntimeSystemdPath(path)) return;
	rmSync(path, { force: true });
}

function officialRuntimeServiceInstallArgs(program: RuntimeSystemdUserProgram): string[] | null {
	return officialRuntimeServiceDescriptorForProgram(program)?.installArgs ?? null;
}

const OFFICIAL_INSTALLER_MAX_BUFFER_BYTES = 64 * 1024;

export function prepareOfficialRuntimeServiceDependencies(
	programs: RuntimeSystemdUserProgram[],
	plan: OfficialRuntimeServicePlan,
	paths: RuntimePaths,
	egressSystemCaFile?: string,
): string | null {
	// Hermes includes the dashboard's node_modules/.bin in its gateway unit.
	// Finish the cold build first so gateway startup cannot rewrite the installed unit.
	const preparesHermesGateway = plan.pending.some((item) => item.program.runtime === "hermes");
	const hasHermesDashboard = programs.some(
		(program) => program.runtime === "hermes" && program.service === "dashboard",
	);
	if (!preparesHermesGateway || !hasHermesDashboard) return null;

	const appRoot = join(paths.userHome, ".hermes", "hermes-agent");
	const index = join(appRoot, "hermes_cli", "web_dist", "index.html");
	const revisionFile = join(dirname(index), HERMES_DASHBOARD_BUILD_REVISION_FILE);
	const descriptor = OFFICIAL_RUNTIME_SERVICE_DESCRIPTORS.find(
		(candidate) => candidate.runtime === "hermes" && candidate.service === "gateway",
	);
	const commandRevision = descriptor
		? runtimeCommandCurrentRevision(
				officialRuntimeServiceCommand(descriptor, paths),
				paths.userHome,
				paths.userHome,
			)
		: null;
	if (
		commandRevision &&
		existsSync(index) &&
		existsSync(revisionFile) &&
		readFileSync(revisionFile, "utf8").trim() === commandRevision
	) {
		return null;
	}
	const commands = [
		{
			args: ["ci", "--include=dev", "--workspace", "web"],
			cwd: appRoot,
			timeoutMs: HERMES_DASHBOARD_INSTALL_TIMEOUT_MS,
		},
		{
			args: ["run", "build"],
			cwd: join(appRoot, "web"),
			timeoutMs: HERMES_DASHBOARD_BUILD_TIMEOUT_MS,
		},
	] as const;
	for (const command of commands) {
		let result: ReturnType<typeof spawnRuntimeUserCommand>;
		try {
			result = spawnRuntimeUserCommand("npm", [...command.args], paths.userHome, command.cwd, {
				egressSystemCaFile,
				maxBufferBytes: OFFICIAL_INSTALLER_MAX_BUFFER_BYTES,
				timeoutMs: command.timeoutMs,
			});
		} catch (error) {
			const logPath = writeRuntimeInstallerLog(paths, "hermes-dashboard-prerequisite", { error });
			return `Hermes dashboard prerequisite failed; see ${logPath}`;
		}
		if (result.status !== 0 || result.error) {
			const logPath = writeRuntimeInstallerLog(paths, "hermes-dashboard-prerequisite", result);
			return `Hermes dashboard prerequisite failed; see ${logPath}`;
		}
	}
	if (!existsSync(index)) return `Hermes dashboard prerequisite did not produce ${index}`;
	if (commandRevision) {
		withRuntimeUserFileAccess(() =>
			writePrivateFileAtomic(revisionFile, `${commandRevision}\n`, { mode: 0o600 }),
		);
	}
	return null;
}

function installOfficialRuntimeUserService(
	program: RuntimeSystemdUserProgram,
	paths: RuntimePaths,
	runtimeIdentity: { uid: number; gid: number },
): string | null {
	const descriptor = officialRuntimeServiceDescriptorForProgram(program);
	if (!descriptor) return null;
	const args = descriptor.installArgs;
	if (!commandResolvable(program.command)) {
		return `official ${runtimeSystemdProgramName(program)} service installer command is unavailable: ${program.command}`;
	}
	try {
		// The installer validates auth for the unit it is about to write, so it
		// needs the same secret environment that unit will run with. openclaw
		// resolves gateway.auth.token with an OPENCLAW_GATEWAY_TOKEN fallback
		// (`readFallback` in its auth-token-resolution); without it the install
		// decides no token exists, generates one and persists it to config
		// mid-publication, which trips its own artifact-fingerprint guard and
		// aborts. Only the descriptor's `installSecretEnv` names are passed, and they travel
		// in the process environment, never in argv.
		const environment = Object.fromEntries(
			(descriptor.installSecretEnv ?? []).flatMap((name) => {
				const value = program.resolvedSecretEnv[name];
				return value ? [[name, value]] : [];
			}),
		);
		const result = spawnRuntimeUserCommand(program.command, args, paths.userHome, program.cwd, {
			environment,
			environmentOverrides:
				descriptor.runtime === "openclaw"
					? {
							OPENCLAW_HOME: undefined,
							OPENCLAW_STATE_DIR: undefined,
							OPENCLAW_CONFIG_PATH: undefined,
						}
					: undefined,
			maxBufferBytes: OFFICIAL_INSTALLER_MAX_BUFFER_BYTES,
			runtimeGid: runtimeIdentity.gid,
			runtimeUid: runtimeIdentity.uid,
			timeoutMs: OFFICIAL_SERVICE_INSTALL_TIMEOUT_MS,
		});
		const logPath = writeRuntimeInstallerLog(
			paths,
			`${runtimeSystemdProgramName(program)}-service`,
			result,
		);
		return result.status === 0 && !result.error
			? null
			: `official ${runtimeSystemdProgramName(program)} service install failed; see ${logPath}`;
	} catch (error) {
		const logPath = writeRuntimeInstallerLog(
			paths,
			`${runtimeSystemdProgramName(program)}-service`,
			{ error },
		);
		return `official ${runtimeSystemdProgramName(program)} service install failed; see ${logPath}`;
	}
}

export function installOfficialRuntimeService(
	item: OfficialRuntimeServicePlan["pending"][number],
	paths: RuntimePaths,
	runtimeIdentity: { uid: number; gid: number },
): string | null {
	const error = installOfficialRuntimeUserService(
		{ ...item.program, cwd: paths.userHome },
		paths,
		runtimeIdentity,
	);
	if (error) return error;
	item.serviceRevision = officialRuntimeServiceRevision(item.program, paths);
	if (!item.serviceRevision) {
		return `official ${runtimeSystemdProgramName(item.program)} service install could not be verified`;
	}
	return null;
}

function uninstallOfficialRuntimeUserService(input: {
	unitName: string;
	paths: RuntimePaths;
	workspaceRoot: string;
}): string | null {
	const descriptor = officialRuntimeServiceDescriptorForUnit(input.unitName);
	if (!descriptor) return null;
	const command = officialRuntimeServiceCommand(descriptor, input.paths);
	if (!commandResolvable(command)) {
		return `official ${input.unitName} uninstaller command is unavailable: ${command}`;
	}
	try {
		runRuntimeUserCommand(
			command,
			descriptor.uninstallArgs,
			"",
			input.paths.userHome,
			input.workspaceRoot,
			{ timeoutMs: OFFICIAL_SERVICE_UNINSTALL_TIMEOUT_MS },
		);
		return null;
	} catch (error) {
		return `official ${input.unitName} uninstall failed: ${
			error instanceof Error ? error.message : String(error)
		}`;
	}
}

export function uninstallStaleOfficialRuntimeServices(input: {
	paths: RuntimePaths;
	unitNames: readonly string[];
	workspaceRoot: string;
}): string[] {
	const errors: string[] = [];
	for (const unitName of input.unitNames) {
		const error = uninstallOfficialRuntimeUserService({
			unitName,
			paths: input.paths,
			workspaceRoot: input.workspaceRoot,
		});
		if (error) errors.push(error);
	}
	return errors;
}

/** True for a path whose basename is a generated drop-in in any spelling. */
function isRuntimeSystemdDropInPath(path: string): boolean {
	return RUNTIME_SYSTEMD_DROP_IN_FILE_NAMES.some((name) => path.endsWith(`/${name}`));
}

/**
 * The companion registry KEYS the current manifest declares that this build does not recognise.
 *
 * Recognition is decided by `KNOWN_COMPANION_KINDS` — the registry — rather than by a literal
 * restated here, so adding a kind stays a one-place edit and this file cannot drift from the
 * envelope parser that bounded the same key at the trust boundary (`COMPANION_KEY_PATTERN`,
 * `manifest-contract.ts`).
 *
 * KEYS ONLY. The entry VALUES are never read: the filebrowser entry embeds a live HS256 secret at
 * `auth.secret`, and everything downstream of this function (the exemption, the persisted
 * applied-state set, the observation POST) is therefore structurally unable to carry it (T-4-07).
 *
 * Sorted and capped at the observation's own bound so two converges over one manifest produce
 * identical output and a persisted set can never exceed what the wire will accept.
 */
function unrecognisedCompanionKinds(manifest: RuntimeManifest): string[] {
	const known = new Set<string>(KNOWN_COMPANION_KINDS);
	return Object.keys(manifest.companions ?? {})
		.filter((key) => !known.has(key))
		.sort()
		.slice(0, UNSUPPORTED_COMPANIONS_MAX);
}

/**
 * Every systemd filename that belongs to an unrecognised companion kind and is therefore EXEMPT
 * from collection — D-14.
 *
 * A CLI that cannot PARSE a kind also cannot safely judge that the kind should be REMOVED. A
 * downgrade is not a deprovision, and treating it as one turns a client rollback into an
 * availability outage for a companion that is working correctly. So the unit is left running and
 * reported unmanaged in the observation instead of stopped.
 *
 * DELIBERATE ASYMMETRY: a RECOGNISED kind that becomes ineligible is still withdrawn on the normal
 * path — there, the CLI does understand the kind and has decided not to run it. The exemption is
 * scoped to what this build cannot parse, never to what it understands.
 *
 * BOUNDARY CONDITION: the exemption is keyed on the kind being DECLARED IN THE CURRENT MANIFEST.
 * A unit for a key the manifest no longer mentions has nothing left to protect and falls back to
 * the normal collect rules.
 *
 * Names are DERIVED through `systemdUnitFileName` and the owned-name prefix, never concatenated
 * from a manifest value (T-4-01) — the key arrives from the provisioning image and is untrusted
 * input, bounded by `COMPANION_KEY_PATTERN` at the parse boundary.
 *
 * EXPLICIT rather than implicit. The system-scope `managedSystem` set happens to be a literal list
 * today, so an unrecognised kind's unit is already skipped there by accident; `04-06-PLAN.md`
 * generalises per-kind unit naming, and the moment that set becomes prefix- or registry-derived an
 * implicit exemption evaporates silently. The user-scope branch needs no such argument — its
 * ownership-prefix rule reclaims anything it owns that it did not plan, and would take the unit
 * today.
 */
function exemptUnrecognisedCompanionFileNames(kinds: readonly string[]): Set<string> {
	const exempt = new Set<string>();
	for (const kind of kinds) {
		const unit = systemdUnitFileName(`${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}${kind}`);
		exempt.add(unit);
		// The unit's `EnvironmentFile` and its drop-in directory are part of the same unit's
		// footprint. Deleting either while leaving the unit running breaks it on the next
		// restart, which is the outage this exemption exists to prevent.
		exempt.add(`${unit}.env`);
		exempt.add(`${unit}.d`);
	}
	return exempt;
}

function planStaleRuntimeSystemdFiles(
	paths: RuntimePaths,
	desiredSystemUnits: readonly string[],
	desiredUserUnits: readonly string[],
	unsupportedCompanionKinds: readonly string[] = [],
): RuntimeSystemdStaleFilePlan {
	const exemptCompanionFiles = exemptUnrecognisedCompanionFileNames(unsupportedCompanionKinds);
	const platformFiles = new Set<string>();
	const userFiles = new Set<string>();
	const systemUnits = new Set<string>();
	const userUnits = new Set<string>();
	const desiredSystem = new Set(desiredSystemUnits);
	const desiredUser = new Set(desiredUserUnits);
	// The system-scope units this product is allowed to collect: the three hosted units and the
	// Files unit, so a converge that stops running one removes what it wrote.
	const managedSystem = new Set([
		...RUNTIME_SYSTEM_UNIT_NAMES,
		systemdUnitFileName(RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME),
	]);
	if (existsSync(paths.systemdSystemRoot)) {
		for (const entry of readdirSync(paths.systemdSystemRoot)) {
			// D-14 exemption; see `exemptUnrecognisedCompanionFileNames`.
			if (exemptCompanionFiles.has(entry)) continue;
			if (!managedSystem.has(entry) || desiredSystem.has(entry)) continue;
			platformFiles.add(join(paths.systemdSystemRoot, entry));
			systemUnits.add(entry);
		}
	}
	for (const entry of managedRuntimeSystemdUnitEntries(paths.systemdUserRoot)) {
		// D-14 exemption, applied BEFORE the ownership rule rather than after: that rule reclaims
		// anything it owns that it did not plan, so an unrecognised kind's unit (or its drop-in)
		// reaches it looking exactly like a stale one of ours.
		if (exemptCompanionFiles.has(entry.unitName)) continue;
		if (desiredUser.has(entry.unitName)) continue;
		userFiles.add(entry.path);
		userUnits.add(entry.unitName);
	}
	const desiredEnvironmentFiles = new Set(
		[...desiredSystem, ...desiredUser].map((unit) => `${unit}.env`),
	);
	if (existsSync(paths.systemdEnvRoot)) {
		for (const entry of readdirSync(paths.systemdEnvRoot)) {
			// D-14 exemption. An exempt unit's `EnvironmentFile` must survive with it — collecting
			// it alone leaves the unit running now and broken at its next restart.
			if (exemptCompanionFiles.has(entry)) continue;
			if (!entry.endsWith(".service.env") || desiredEnvironmentFiles.has(entry)) continue;
			const path = join(paths.systemdEnvRoot, entry);
			// Negated continue-guard, same polarity note as the unit scan above.
			if (!isOwnedRuntimeSystemdEntry(entry, () => isGeneratedRuntimeSystemdPath(path))) continue;
			platformFiles.add(path);
		}
	}
	return {
		platformFiles: [...platformFiles].sort(),
		userFiles: [...userFiles].sort(),
		systemUnits: [...systemUnits].sort(),
		userUnits: [...userUnits].sort(),
		unsupportedCompanionKinds: [...unsupportedCompanionKinds],
	};
}

export function removeStaleRuntimeSystemdFiles(plan: RuntimeSystemdStaleFilePlan): string[] {
	const errors: string[] = [];
	const removeFiles = (paths: readonly string[]) => {
		for (const path of paths) {
			try {
				rmSync(path, { force: true });
				if (isRuntimeSystemdDropInPath(path) && existsSync(dirname(path))) {
					if (readdirSync(dirname(path)).length === 0) rmdirSync(dirname(path));
				}
			} catch (error) {
				errors.push(error instanceof Error ? error.message : String(error));
			}
		}
	};
	removeFiles(plan.platformFiles);
	withRuntimeUserSystemdFiles(() => removeFiles(plan.userFiles));
	return errors;
}

/**
 * The environment every generated unit starts from.
 *
 * Two of these units (`iforge-runtime-watch`, `iforge-daemon`, and the sidecar) run the `iforge`
 * CLI itself, whose entrypoint refuses an internal register name that arrives without its public
 * counterpart. Contract members therefore MUST be written in their public spelling here or the
 * hosted daemon loses hosted mode and its run directory at start.
 *
 * `CLAWDI_RUNTIME_USER` (D-07 / X-4) and `CLAWDI_SERVICE_STATE_DIR` are NOT contract members —
 * nothing can alias into them, so they keep the internal spelling.
 */
export function runtimeSystemdCommonEnvironment(paths: RuntimePaths): Record<string, string> {
	const environment: Record<string, string> = {
		HOME: paths.userHome,
		CLAWDI_HOME: paths.clawdiHome,
		IFORGE_RUNTIME_MODE: "hosted",
		CLAWDI_RUNTIME_USER: HOSTED_RUNTIME_USER,
		PATH: runtimeSystemdPath(paths),
		...(paths.serviceStateRoot === DEFAULT_SERVICE_STATE_ROOT
			? {}
			: { CLAWDI_SERVICE_STATE_DIR: paths.serviceStateRoot }),
		...(paths.runRoot === DEFAULT_RUN_ROOT ? {} : { IFORGE_RUN_DIR: paths.runRoot }),
	};
	return environment;
}

interface RuntimeSystemdUserProgramEnvironmentInput {
	program: RuntimeSystemdUserProgram;
	commonEnvironment: Record<string, string>;
	manifest: RuntimeManifest;
	paths: RuntimePaths;
	secretValues: Record<string, string> | undefined;
	providerProjectionRevisions: Partial<Record<string, string | null>>;
	runtimeRevision: Parameters<typeof runtimeSystemdProgramRevision>[4];
}

function runtimeSystemdUserProgramEnvironment(
	input: RuntimeSystemdUserProgramEnvironmentInput,
): Record<string, string> {
	const { program } = input;
	const runtimeEnv = { ...program.env };
	const descriptor = officialRuntimeServiceDescriptorForProgram(program);
	const isHermesDashboard = program.runtime === "hermes" && program.service === "dashboard";
	const installerOnlySecretEnv =
		descriptor?.runtime === "openclaw" &&
		input.manifest.openclawGatewayAuth?.activation.enabled === true
			? (descriptor.installSecretEnv ?? [])
			: [];
	for (const envName of installerOnlySecretEnv) {
		delete runtimeEnv[envName];
	}
	if (descriptor) delete runtimeEnv.PATH;
	// Both spellings: a native service that spawns `iforge` (its MCP server) would re-derive the
	// internal register from the public name (T-03.1G-01).
	if (descriptor || isHermesDashboard) {
		for (const name of RUNTIME_AUTH_TOKEN_ENV_NAMES) delete runtimeEnv[name];
	}
	const revision = runtimeSystemdProgramRevision(
		input.manifest,
		program,
		input.secretValues,
		input.providerProjectionRevisions,
		input.runtimeRevision,
	);
	const isNativeRuntimeService = descriptor !== null || isHermesDashboard;
	const env: Record<string, string> = isNativeRuntimeService
		? {
				...(isHermesDashboard ? { HOME: input.paths.userHome } : {}),
				...runtimeEnv,
				CLAWDI_MANAGED_CONTENT_DIGEST: revision,
			}
		: {
				...input.commonEnvironment,
				...runtimeEnv,
				IFORGE_AUTH_TOKEN: "",
				CLAWDI_HOME: input.paths.clawdiHome,
				CLAWDI_MANAGED_CONTENT_DIGEST: revision,
			};
	return env;
}

export function publishRetainedOpenClawEnvironment(
	input: RuntimeSystemdUserProgramEnvironmentInput,
): void {
	const { program, paths, manifest } = input;
	if (
		program.runtime !== "openclaw" ||
		program.service ||
		manifest.runtimes.openclaw?.enabled !== true
	)
		return;
	const name = runtimeSystemdProgramName(program);
	if (!isGeneratedRuntimeSystemdPath(systemdDropInFilePath(paths, name))) return;
	// Warm updates keep their environment publication in the final apply phase.
	if (existsSync(systemdEnvironmentFilePath(paths, name))) return;
	// New/legacy installs retain the normal installer and drop-in publication order.
	if (planOfficialRuntimeServices([program], paths, true).pending.length !== 0) return;
	runtimeRecoverableSecretValues(manifest, input.secretValues);
	writeSystemdProgramEnvironment({
		paths,
		name,
		owner: "runtime-user",
		env: runtimeSystemdUserProgramEnvironment(input),
	});
}

function writeRuntimeSystemdUserProgram(input: RuntimeSystemdUserProgramEnvironmentInput): string {
	const { program } = input;
	const name = runtimeSystemdProgramName(program);
	const env = runtimeSystemdUserProgramEnvironment(input);
	const isHermesDashboard = program.runtime === "hermes" && program.service === "dashboard";
	if (officialRuntimeServiceInstallArgs(program)) {
		return writeSystemdUserEnvironmentDropIn({
			paths: input.paths,
			name,
			env,
			unsetEnvironment: RUNTIME_AUTH_TOKEN_ENV_NAMES,
		});
	}
	return writeSystemdUserUnit({
		paths: input.paths,
		name,
		description: `Clawdi hosted ${program.runtime}${program.service ? ` ${program.service}` : ""}`,
		command: program.command,
		args: program.args,
		cwd: program.cwd,
		env,
		unsetEnvironment: isHermesDashboard ? RUNTIME_AUTH_TOKEN_ENV_NAMES : undefined,
	});
}

/**
 * The sandbox every companion unit carries, extracted VERBATIM from the shipped Files unit.
 *
 * This is the shared per-kind DEFAULT, and there is deliberately no mechanism by which a kind can
 * omit or soften a line of it. The per-kind variation is a `CompanionBindAllowList` — a list of
 * paths, defaulting to empty — and never a flag. A baseline any one kind can opt out of is a
 * baseline every future kind can cite, and the one kind that needs home access is precisely the
 * precedent that would be cited.
 *
 * WHY THE HOME DIRECTIVE IS `tmpfs` AND NOT THE COARSER VALUE. `systemd.exec(5)` on `ProtectHome=`:
 * *"The value "tmpfs" is useful to hide home directories not relevant to the processes invoked by
 * the unit, while still allowing necessary directories to be made visible when listed in
 * `BindPaths=` or `BindReadOnlyPaths=`."* And of the coarser value: *"Setting this to "yes" is
 * mostly equivalent to setting the three directories in `InaccessiblePaths=`"* — which `BindPaths=`
 * CANNOT re-open. So `tmpfs` plus an empty allow-list is a read-only empty home, i.e. at least as
 * strong as the coarser value, while `tmpfs` plus one path expresses the file browser's need as
 * DATA rather than as an exception to the policy. Adopting the coarser-value-with-opt-out shape
 * would be a weakening of what already ships, not an implementation of it.
 *
 * WHAT WOULD HAVE TO BE TRUE TO CHANGE A LINE HERE: a per-kind justification recorded in
 * `config/companion-registry.txt` AND in `docs/managed-runtime.md`, reviewed as the security change
 * it is. Not a parameter, not an option bag, not a default someone can override at a call site.
 *
 * Split around the allow-list only because the path directives render between
 * `ProtectHome=tmpfs` and `ProtectKernelTunables=` in the shipped unit, and this extraction changes
 * nothing about the rendered bytes.
 */
export const COMPANION_SANDBOX_BASELINE = {
	beforeAllowList: [
		"UMask=0077",
		"NoNewPrivileges=true",
		"PrivateTmp=true",
		"PrivateDevices=true",
		"ProtectSystem=strict",
		"ProtectHome=tmpfs",
	],
	afterAllowList: [
		"ProtectKernelTunables=true",
		"ProtectKernelModules=true",
		"ProtectKernelLogs=true",
		"ProtectControlGroups=true",
		"ProtectClock=true",
		"ProtectHostname=true",
		"ProtectProc=invisible",
		"ProcSubset=pid",
		"LockPersonality=true",
		"RestrictSUIDSGID=true",
		"RestrictRealtime=true",
		"RestrictNamespaces=true",
		"KeyringMode=private",
		"RemoveIPC=true",
		"RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
		"CapabilityBoundingSet=",
		"AmbientCapabilities=",
		"SystemCallArchitectures=native",
		"TasksMax=128",
	],
} as const satisfies { beforeAllowList: readonly string[]; afterAllowList: readonly string[] };

/**
 * The paths one companion kind needs visible, writable and non-executable — ALL DEFAULTING TO EMPTY.
 *
 * Empty is the interesting default. Under the baseline's home directive an empty list yields a
 * read-only empty home, so a kind that declares nothing is fully protected without anyone having
 * written a rule for it. Only a kind with a genuine need adds a path, and the need is then visible
 * as one line of data in one place.
 */
export interface CompanionBindAllowList {
	/** Paths made visible inside the unit's mount namespace. */
	bind?: readonly string[];
	/** Paths the unit may write. */
	readWrite?: readonly string[];
	/** Paths mounted non-executable. */
	noExec?: readonly string[];
}

/**
 * The injectable seam an at-rest credential encryption step would be reached through.
 *
 * Takes the plaintext source and the credential id the unit will load it under, and returns the
 * path of the ciphertext it produced. It is a SEAM rather than a direct call for the reason
 * `FileBrowserCompanionInstallOptions` is: the real implementation is root-only and machine-local,
 * and the 200+ unprivileged reconciliation tests must never reach it.
 *
 * NOTHING SUPPLIES ONE TODAY — see `companionCredentialDirective`. The type ships so Phase 6
 * inherits the contract rather than inventing it, and so the id it is handed is provably the same
 * id the unit loads.
 */
export type CompanionCredentialEncrypt = (input: {
	/** The plaintext credential file. */
	source: string;
	/**
	 * The credential id, from `companionServiceNames`. Under the encrypted form this id is EMBEDDED
	 * in the ciphertext and verified on decrypt — systemd does that specifically to stop a credential
	 * being reused for a purpose it was not intended for — so an id that disagreed with the unit's
	 * directive would be a service START FAILURE, not a warning.
	 */
	credentialId: string;
}) => string;

export interface CompanionCredentialOptions {
	/** The registry key; the credential id is derived from it through the single naming helper. */
	kind: string;
	/** The plaintext credential file the unit loads. */
	source: string;
	/** See `CompanionCredentialEncrypt`. Supplying one currently throws — D-11 is deferred. */
	encrypt?: CompanionCredentialEncrypt;
}

/**
 * Render one companion's credential directive. THE SINGLE SITE that decides the delivery form.
 *
 * WHAT SHIPS TODAY, AND WHY IT IS A DELIBERATE DEFERRAL RATHER THAN AN OVERSIGHT.
 *
 * Locked decision D-11 says a companion's credential should live in the ENCRYPTED per-unit form.
 * At execution time the user was shown the evidence and chose to DEFER that increment to Phase 6.
 * Two research assumptions could not be verified from the planning host and still cannot:
 *
 *   A1 — the hosted image's systemd must be 247 or newer for the encrypted directive to exist.
 *        Local systemd is 255; the IMAGE's version has never been observed.
 *   A2 — `systemd-creds encrypt` needs either a usable TPM2 device or a writable root-only local
 *        secret file, inside an Incus CVM, which is a different machine class from any host this
 *        repository has measured.
 *
 * A directive the host cannot support fails the service AT START on a real host while every local
 * test stays green — the entire fleet's companions stop starting and CI never sees it.
 *
 * CRUCIALLY, COMP-05 IS ALREADY SATISFIED BY WHAT SHIPS HERE. State the two properties separately,
 * because conflating them overstates what this phase proved:
 *
 *   1. PER-UID CREDENTIAL ISOLATION — what COMP-05 actually names. Already true, and true through
 *      the plain form: `systemd.exec(5)` says the data is "accessible from the unit's processes via
 *      the file system, at a read-only location that (if possible and permitted) is backed by
 *      non-swappable memory. The data is only accessible to the user associated with the unit, via
 *      the `User=`/`DynamicUser=` settings (as well as the superuser)." The `User=` half comes from
 *      `companionSandboxServiceLines` above. COMP-05 is provable against that DIRECTIVE PAIR.
 *   2. AT-REST ENCRYPTION OF THE CREDENTIAL FILE — what D-11 adds, and what is deferred. It is not
 *      what COMP-05 asks for.
 *
 * WHAT MUST BE OBSERVED BEFORE SWITCHING (Phase 6 owns this): A1 and A2 confirmed against a real
 * provisioned host, not inferred. Then this function is the one line that changes — the credential
 * id already comes from the per-kind naming helper, so encrypt-time and load-time ids cannot drift.
 * Note the cost that comes with the switch: under `PrivateDevices=` the encrypted directive adds
 * the TPM device node with rw mode to `DeviceAllow=`, re-opening a node the sandbox baseline
 * currently closes.
 *
 * The inline-credential form stays prohibited under BOTH forms, and for a stronger reason than
 * D-11 gives: it is not merely visible in unit-inspection output, it is, per `systemd.exec(5)`,
 * "accessible to unprivileged processes via IPC".
 */
export function companionCredentialDirective(options: CompanionCredentialOptions): string {
	const names = companionServiceNames(options.kind);
	if (options.encrypt !== undefined) {
		// FAIL CLOSED. Silently ignoring an injected seam would let a caller believe credentials are
		// encrypted at rest when they are not; silently honouring it would ship a directive the host
		// may not support. Both are worse than refusing until A1 and A2 are observations.
		throw new Error(
			`Companion ${options.kind}: at-rest credential encryption (D-11) is DEFERRED to Phase 6 pending confirmation of the hosted image's systemd version (A1) and credential key availability (A2); the plain per-unit credential directive already satisfies COMP-05`,
		);
	}
	return `LoadCredential=${names.credentialId}:${systemdPath(options.source)}`;
}

export interface CompanionSandboxOptions {
	/** The registry key. Every per-kind name is derived from it through one helper. */
	kind: string;
	/**
	 * Lines specific to this kind, rendered between the identity and the baseline — the credential
	 * directive, a verified-binary publish, a version probe. Never a sandbox directive: anything
	 * that belongs to every companion belongs in the baseline above.
	 */
	perKindLines?: readonly string[];
	allowList?: CompanionBindAllowList;
}

/**
 * Build one companion unit's `[Service]` lines: its identity, its own lines, then the baseline with
 * its path allow-list spliced in.
 *
 * `User=` / `Group=` come from `companionServiceNames`, the same single derivation
 * `ensureCompanionServiceIdentity` allocates and re-asserts against, so the account the unit names
 * and the account reconciliation created cannot disagree.
 *
 * EGRESS RESIDUAL, recorded here because this is where the next kind's author will be looking: the
 * nftables redirect in `transparent-egress.ts` keys on `runtimeUid` ONLY, so every per-companion
 * UID is OUTSIDE the transparent-egress redirect and its outbound traffic is neither redirected nor
 * explicitly accepted by policy. That is why `config/companion-registry.txt` carries an `egress`
 * column, and why `filebrowser`'s value is `bypass` — a description of the shipped state, recorded
 * as an accepted residual (T-4-11) rather than left unstated. Bringing companion UIDs into the
 * redirect is Phase 6 OPS work; do not change the nftables rules from here.
 */
export function companionSandboxServiceLines(options: CompanionSandboxOptions): string[] {
	const names = companionServiceNames(options.kind);
	const allowList = options.allowList ?? {};
	const allowListLines: string[] = [];
	const bind = allowList.bind ?? [];
	const readWrite = allowList.readWrite ?? [];
	const noExec = allowList.noExec ?? [];
	if (bind.length > 0) allowListLines.push(`BindPaths=${bind.map(systemdPath).join(" ")}`);
	if (readWrite.length > 0) {
		allowListLines.push(`ReadWritePaths=${readWrite.map(systemdPath).join(" ")}`);
	}
	if (noExec.length > 0) allowListLines.push(`NoExecPaths=${noExec.map(systemdPath).join(" ")}`);
	return [
		`User=${names.user}`,
		`Group=${names.group}`,
		...(options.perKindLines ?? []),
		...COMPANION_SANDBOX_BASELINE.beforeAllowList,
		...allowListLines,
		...COMPANION_SANDBOX_BASELINE.afterAllowList,
	];
}

function writeFileBrowserSystemdUnit(input: {
	program: RuntimeSystemdUserProgram;
	manifest: RuntimeManifest;
	paths: RuntimePaths;
	runtimeIdentity: { uid: number; gid: number };
}): string {
	const companion = input.manifest.companions?.filebrowser;
	if (!companion) throw new Error("Files systemd unit requires a companion manifest");
	// The same resolution `ensureFileBrowserServiceIsolation` uses, so the unit's `User=` is the
	// identity that owns the home and the config group (#939).
	const runtimeUser = process.env.CLAWDI_RUNTIME_USER?.trim() || HOSTED_RUNTIME_USER;
	if (runtimeUser === "root" || runtimeUser === "0") {
		throw new Error("Files systemd unit requires a non-root tenant runtime user");
	}
	const serviceConfig = join(dirname(input.paths.fileBrowserServiceBinary), "filebrowser.yaml");
	return writeSystemdSystemUnit({
		paths: input.paths,
		// One derivation with `runtimeSystemdProgramName`'s file-browser branch. The key and the
		// file it names must not be able to disagree.
		name: RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME,
		description: "iforge hosted Files companion",
		command: input.program.command,
		args: input.program.args,
		execStart: fileBrowserSystemdExec(input.paths.fileBrowserServiceBinary, serviceConfig),
		cwd: input.program.cwd,
		directoryKind: { companionKind: FILE_BROWSER_COMPANION_KIND },
		env: {
			HOME: "/nonexistent",
			// FileBrowser checks this path before loading server.database from YAML.
			FILEBROWSER_DATABASE: join(input.paths.fileBrowserStateRoot, "filebrowser.db"),
			CLAWDI_MANAGED_CONTENT_DIGEST: runtimeImpactRevision({
				companion: input.manifest.companions?.filebrowser ?? null,
			}),
		},
		extraUnitLines: ["After=network-online.target", "Wants=network-online.target"],
		extraServiceLines: [
			`User=${runtimeUser}`,
			`Group=${input.runtimeIdentity.gid}`,
			// Publish only this verified executable into the component service's
			// private runtime directory; the platform state root stays untraversable.
			`BindReadOnlyPaths=${systemdPath(input.program.command)}:${systemdPath(input.paths.fileBrowserServiceBinary)}:norbind`,
			`BindReadOnlyPaths=${systemdPath(input.paths.fileBrowserConfig)}:${systemdPath(serviceConfig)}:norbind`,
			`ExecStartPre=${fileBrowserVersionProbeExec(
				input.paths.fileBrowserServiceBinary,
				companion.version,
				companion.commit,
			)}`,
			"UMask=0077",
			"NoNewPrivileges=true",
			"PrivateTmp=true",
			"PrivateDevices=true",
			"ProtectSystem=strict",
			"ProtectHome=tmpfs",
			`BindPaths=${systemdPath(input.paths.userHome)}`,
			`ReadWritePaths=${systemdPath(input.paths.userHome)}`,
			`NoExecPaths=${systemdPath(input.paths.userHome)} ${systemdPath(input.paths.fileBrowserStateRoot)}`,
			"ProtectKernelTunables=true",
			"ProtectKernelModules=true",
			"ProtectKernelLogs=true",
			"ProtectControlGroups=true",
			"ProtectClock=true",
			"ProtectHostname=true",
			"ProtectProc=invisible",
			"ProcSubset=pid",
			"PrivatePIDs=true",
			"LockPersonality=true",
			"RestrictSUIDSGID=true",
			"RestrictRealtime=true",
			"RestrictNamespaces=true",
			"KeyringMode=private",
			"RemoveIPC=true",
			"RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
			"CapabilityBoundingSet=",
			"AmbientCapabilities=",
			"SystemCallArchitectures=native",
			"TasksMax=128",
		],
	});
}

export interface ContainerCompanionConvergeResult {
	/** The Quadlet-generated services this converge keeps, counted as desired system units. */
	desiredServices: string[];
	applied: ContainerCompanionApplyResult | null;
	withdrawn: ContainerCompanionWithdrawResult | null;
	removed: ContainerCompanionRemoveResult | null;
	/**
	 * The retired installation whose retained data this converge removed before applying the
	 * present entry (plan 06-42, CR-04), or `null` when none was.
	 */
	retiredRemoved: string | null;
	/**
	 * The kind's refusal on this converge (plan 06-37, CR-04), already recorded for the
	 * post-converge reporter; `null` when the kind converged.
	 */
	failure: ContainerCompanionApplyFailure | null;
}

/**
 * Converge the container companion kind: plan and apply a present entry, withdraw an absent one,
 * and remove -- data included -- only an entry that says `lifecycle: "remove"`.
 *
 * "Absent" covers two wire shapes, both withdrawn identically: no `postiz` key, and no
 * `companions` object at all (research Pitfall 12 — the control plane omits the whole object when
 * nothing survives, because an empty `{}` means "converge to zero"). Withdrawal takes the units
 * down and never the data. Destroying data is the explicit `lifecycle: "remove"` instruction the
 * control plane sends for a confirmed uninstall (plan 06-20, `removeContainerCompanion`), which
 * the runtime then reports `removed`. A recognised kind that becomes ineligible is withdrawn on
 * the normal path — the D-14 exemption is for kinds this build cannot parse, and it can parse
 * this one.
 *
 * Isolated (plan 06-37, CR-04): this never throws. A refusal on any branch -- a failed pull, a
 * host probe, a plan refusal, a withdrawal or removal that would not complete -- is recorded
 * root-only, redacted, and returned as `failure`, so the rest of the agent's converge proceeds and
 * the post-converge reporter fails the install by name. The refusals themselves are unchanged;
 * only their blast radius is. On failure the services the previous applied receipt describes are
 * still counted as desired: the apply rolled back to them.
 *
 * Retired installations (plan 06-42, CR-04): when the host holds a receipt -- withdrawn or
 * applied -- for an installation OTHER than the present entry's, and the entry names that
 * installation in `retiredInstallations`, its retained data is removed through
 * `removeContainerCompanion` (path guard, symlink refusal, guard-table removal) before the entry
 * is planned against freshly read facts. The control plane names an installation there only for a
 * tenant-confirmed uninstall the host never confirmed, so this deletes nothing the tenant did not
 * confirm deleting. A receipt for any installation the entry does NOT name is left alone, and
 * `planContainerCompanion` still refuses the new installation by name.
 *
 * Files is not a container companion: since #939/#915 it runs as the tenant runtime user and reads
 * its config through `BindReadOnlyPaths=`, so nothing here decides its delivery.
 */
export function reconcileContainerCompanions(input: {
	manifest: RuntimeManifest;
	paths: RuntimePaths;
	secretValues: Record<string, string> | undefined;
	host: ContainerHost;
}): ContainerCompanionConvergeResult {
	const paths = containerCompanionPaths(input.paths, CONTAINER_COMPANION_KIND);
	try {
		return convergeContainerCompanionKind(input, paths);
	} catch (error) {
		const failure = containerCompanionApplyFailure({
			manifest: input.manifest,
			secretValues: input.secretValues,
			error,
			now: input.host.now(),
		});
		try {
			recordContainerCompanionApplyFailure(input.host, paths, failure);
		} catch {
			// An unwritable record costs the report, never the agent's converge: the failure still
			// travels in the result, and the install's deadline fails it on the control plane.
		}
		// Plan 06-49 (G-06-2 WR-06): a failure that is also a host condition -- a companion network
		// that could not be moved onto the pinned subnet, its units left stopped -- is raised on the
		// installation's condition record with the already-redacted message. The post-converge pass
		// reports it through the condition phase even for an installation that reported ready,
		// whose apply failure is only logged.
		const entry = input.manifest.companions?.postiz;
		if (error instanceof ContainerCompanionConditionError && entry?.lifecycle === "present") {
			try {
				raiseContainerCompanionCondition(input.host, paths, {
					installationId: entry.installationId,
					code: error.code,
					detail: failure.message,
				});
			} catch {
				// Same as the failure record: an unwritable record never fails the agent's converge.
			}
		}
		return {
			desiredServices: previouslyAppliedServices(input.host, paths),
			applied: null,
			withdrawn: null,
			removed: null,
			retiredRemoved: null,
			failure,
		};
	}
}

/**
 * Remove the retained data of the installation the host's receipt names, if and only if the
 * present entry names it as retired (plan 06-42, CR-04). Returns that installation's id, or `null`
 * when nothing was removed. A refusal from the guarded remove path throws, and the caller's
 * isolation records it like any other refusal.
 */
function removeRetiredInstallation(
	entry: PostizCompanionManifest,
	receiptInstallationId: string | undefined,
	host: ContainerHost,
	paths: ContainerCompanionPaths,
): string | null {
	if (receiptInstallationId === undefined || receiptInstallationId === entry.installationId) {
		return null;
	}
	const retired = entry.retiredInstallations.some(
		(item) => item.installationId === receiptInstallationId,
	);
	if (!retired) return null;
	removeContainerCompanion(CONTAINER_COMPANION_KIND, host, paths);
	log.info("runtime.companion_retired_data_removed", {
		kind: CONTAINER_COMPANION_KIND,
		installationId: receiptInstallationId,
	});
	return receiptInstallationId;
}

function convergeContainerCompanionKind(
	input: {
		manifest: RuntimeManifest;
		secretValues: Record<string, string> | undefined;
		host: ContainerHost;
	},
	paths: ContainerCompanionPaths,
): ContainerCompanionConvergeResult {
	const entry = input.manifest.companions?.postiz;
	if (entry?.lifecycle === "remove") {
		const removed = removeContainerCompanion(CONTAINER_COMPANION_KIND, input.host, paths);
		clearContainerCompanionApplyFailure(input.host, paths);
		return {
			desiredServices: [],
			applied: null,
			withdrawn: null,
			removed,
			retiredRemoved: null,
			failure: null,
		};
	}
	if (entry?.lifecycle !== "present") {
		return {
			desiredServices: [],
			applied: null,
			withdrawn: withdrawContainerCompanion(CONTAINER_COMPANION_KIND, input.host, paths),
			removed: null,
			retiredRemoved: null,
			failure: null,
		};
	}
	let facts = readContainerCompanionFacts(input.host, paths, CONTAINER_COMPANION_KIND);
	const retiredRemoved = removeRetiredInstallation(
		entry,
		facts.receipt?.installationId,
		input.host,
		paths,
	);
	// The removal changed the host: the entry is planned against what is on it now.
	if (retiredRemoved !== null) {
		facts = readContainerCompanionFacts(input.host, paths, CONTAINER_COMPANION_KIND);
	}
	const secretValues = input.secretValues ?? {};
	let plan = planContainerCompanion(entry, secretValues, facts);
	if (plan.kind === "install") {
		// Plan 06-48 (G-06-1 WR-02, OPS-01): an install over an empty data directory restores the
		// repository's newest backup first. Which point it restores, and so which app digest wrote
		// that schema, is decided from the backup's annotations BEFORE the install is planned for
		// good: the app that starts is always the one that wrote the restored data. `null` when the
		// data directory is populated or the entry has no backups.
		const restore = probePostizRestore(plan, input.host, paths);
		if (restore !== null) plan = planContainerCompanion(entry, secretValues, facts, { restore });
	}
	const applied = applyContainerCompanion(plan, input.host, paths);
	clearContainerCompanionApplyFailure(input.host, paths);
	return {
		desiredServices: [...plan.services.all],
		applied,
		withdrawn: null,
		removed: null,
		retiredRemoved,
		failure: null,
	};
}

/** The services the host's applied receipt describes, or none; unreadable counts as none. */
function previouslyAppliedServices(host: ContainerHost, paths: ContainerCompanionPaths): string[] {
	try {
		const receipt = readContainerCompanionFacts(host, paths, CONTAINER_COMPANION_KIND).receipt;
		if (receipt?.state !== "applied") return [];
		return containerCompanionServices(CONTAINER_COMPANION_KIND, {
			tunnel: receipt.images.cloudflared !== undefined,
		});
	} catch {
		return [];
	}
}

function officialRuntimeSystemdPrograms(
	programs: RuntimeSystemdUserProgram[],
): RuntimeSystemdUserProgram[] {
	const byServiceName = new Map<string, RuntimeSystemdUserProgram>();
	for (const program of programs) {
		const serviceName = officialRuntimeSystemdProgramName(program);
		if (serviceName) byServiceName.set(serviceName, program);
	}
	return [...byServiceName.entries()]
		.sort(([left], [right]) => left.localeCompare(right))
		.map(([, program]) => program);
}

export function writeRuntimeSidecarSystemdUnit(input: {
	program: RuntimeEgressSystemdProgram;
	identity: RuntimeEgressIdentity;
	manifest: RuntimeManifest;
	paths: RuntimePaths;
	workspaceRoot: string;
	commonEnvironment: Record<string, string>;
}): string {
	return writeSystemdSystemUnit({
		paths: input.paths,
		name: RUNTIME_SYSTEM_UNIT_SIDECAR,
		description: "iforge hosted runtime sidecar",
		command: input.paths.cliManagedBin,
		args: ["runtime", "sidecar"],
		cwd: input.workspaceRoot,
		env: {
			...input.commonEnvironment,
			IFORGE_AUTH_TOKEN: "",
			CLAWDI_EGRESS_ENV_FILE: input.program.envFilePath,
			CLAWDI_MANAGED_CONTENT_DIGEST: runtimeImpactRevision({
				program: runtimeSidecarProgramRevision(input.manifest, input.program, input.identity),
				secretFile: input.program.secretFilePath
					? readFileSync(input.program.secretFilePath, "utf8")
					: null,
			}),
		},
		serviceType: "notify",
		extraUnitLines: [`Before=user@${input.identity.runtimeUid}.service`],
		extraServiceLines: [
			"NotifyAccess=main",
			// The egress process drops to its dedicated UID. Publish only the
			// verified engine into this unit's private mount namespace.
			`BindReadOnlyPaths=${systemdPath(input.program.engine.binaryPath)}:${systemdPath(input.paths.egressServiceBinary)}:norbind`,
		],
	});
}

export function writeRuntimeSystemdState(input: {
	runtimePrograms: RuntimeSystemdUserProgram[];
	egressProgram: RuntimeEgressSystemdProgram | null;
	egressIdentity: RuntimeEgressIdentity | null;
	runtimeIdentity: { uid: number; gid: number };
	manifest: RuntimeManifest;
	paths: RuntimePaths;
	workspaceRoot: string;
	daemonAuthTokenFile: string | null;
	secretValues: Record<string, string> | undefined;
	providerProjectionRevisions: Partial<Record<string, string | null>>;
	runtimeRevision: Parameters<typeof runtimeSystemdProgramRevision>[4];
	commonEnvironment: Record<string, string>;
	/**
	 * The container companion kind's host seam. Omitted in production, where the root-only real
	 * host is used; injected by tests so no podman, systemd or `/etc` is ever reached.
	 */
	containerHost?: ContainerHost;
	/**
	 * The container kind's result from an earlier publish in the SAME converge. Upstream publishes
	 * the units a second time after the official installers (#1194); the container kind converges
	 * once per converge (06 D-08), so that second publish reuses the first result instead of
	 * applying the kind again.
	 */
	containerCompanions?: ContainerCompanionConvergeResult;
	deferredRuntimeUserUnitNames?: readonly string[];
}): {
	systemUnits: string[];
	userUnits: string[];
	egressSidecarActive: boolean;
	staleFiles: RuntimeSystemdStaleFilePlan;
	/** The Quadlet-generated services the container kind keeps on this converge. */
	containerCompanionServices: string[];
	containerCompanions: ContainerCompanionConvergeResult;
} {
	const {
		runtimePrograms,
		egressProgram,
		egressIdentity,
		runtimeIdentity,
		manifest,
		paths,
		workspaceRoot,
		daemonAuthTokenFile,
		secretValues,
		providerProjectionRevisions,
		runtimeRevision,
		commonEnvironment,
	} = input;
	const systemUnits: string[] = [];
	const shouldRunEgress = egressProgram !== null && runtimePrograms.length > 0;
	const activeEgressProgram = shouldRunEgress ? egressProgram : null;
	const activeEgressIdentity = shouldRunEgress ? egressIdentity : null;
	const userUnits: string[] = [];
	// The container kind is isolated (plan 06-37, CR-04): its refusal never throws out of here, so
	// every other unit below is written exactly as it would be without it. A failure is recorded
	// root-only and travels as a `failed` install report after the converge.
	const containerCompanions =
		input.containerCompanions ??
		reconcileContainerCompanions({
			manifest,
			paths,
			secretValues,
			host: input.containerHost ?? nodeContainerHost(),
		});
	const deferredRuntimeUserUnitNames = new Set(input.deferredRuntimeUserUnitNames ?? []);
	const desiredSystemUnitNames = [
		// The generated services live in the generator directory, not in `systemdSystemRoot`, so
		// stale-unit collection could not reach them anyway; they are listed so the desired set is the
		// whole truth about what this converge keeps running.
		...containerCompanions.desiredServices,
		...(daemonAuthTokenFile
			? [`${RUNTIME_SYSTEM_UNIT_WATCH}.service`, `${RUNTIME_SYSTEM_UNIT_DAEMON}.service`]
			: []),
		...(activeEgressProgram ? [`${RUNTIME_SYSTEM_UNIT_SIDECAR}.service`] : []),
		...(runtimePrograms.some((program) => program.programKind === "file-browser")
			? [systemdUnitFileName(RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME)]
			: []),
	];
	const desiredUserUnitNames = runtimePrograms
		.filter((program) => program.programKind !== "file-browser")
		.map((program) => systemdUnitFileName(runtimeSystemdProgramName(program)));
	const staleFiles = planStaleRuntimeSystemdFiles(
		paths,
		desiredSystemUnitNames,
		desiredUserUnitNames,
		unrecognisedCompanionKinds(manifest),
	);
	if (daemonAuthTokenFile) {
		systemUnits.push(
			writeSystemdSystemUnit({
				paths,
				name: RUNTIME_SYSTEM_UNIT_WATCH,
				description: "iforge hosted runtime desired-state watcher",
				command: paths.cliManagedBin,
				args: ["runtime", "watch"],
				cwd: workspaceRoot,
				directoryKind: "platform",
				env: {
					...commonEnvironment,
					IFORGE_AUTH_TOKEN: "",
					// The watch hosts the companion dispatch claimer (phase 05/06), which needs the
					// environment it claims for. Upstream #1189 retired the instance-data replica it
					// used to read, so the unit carries the id, in its public spelling, as the daemon's
					// unit does.
					IFORGE_ENVIRONMENT_ID: manifest.environmentId,
				},
				extraServiceLines: ["TasksMax=infinity"],
			}),
		);
	}

	if (daemonAuthTokenFile) {
		systemUnits.push(
			writeSystemdSystemUnit({
				paths,
				name: RUNTIME_SYSTEM_UNIT_DAEMON,
				description: "iforge hosted runtime daemon",
				command: paths.cliManagedBin,
				args: ["daemon", "run", "--auth-token-file", daemonAuthTokenFile],
				cwd: workspaceRoot,
				env: {
					...commonEnvironment,
					IFORGE_ENVIRONMENT_ID: manifest.environmentId,
					IFORGE_SERVE_MODE: "container",
					// Now a contract member (plan 03.1-16 added the `IFORGE_STATE_DIR` pair).
					// The unit launches the iforge CLI, whose boundary refuses an internal
					// register name arriving without its public counterpart, so the unit must
					// write the PUBLIC spelling and let the daemon re-derive the register.
					IFORGE_STATE_DIR: paths.daemonStateRoot,
					IFORGE_API_URL: manifest.controlPlane.apiUrl,
					IFORGE_NO_AUTO_UPDATE: "1",
					IFORGE_NO_UPDATE_CHECK: "1",
					CLAWDI_MANAGED_CONTENT_DIGEST: runtimeImpactRevision({
						program: daemonProgramRevision(manifest),
						authToken: readFileSync(daemonAuthTokenFile, "utf8"),
					}),
				},
			}),
		);
	}

	if (activeEgressProgram) {
		if (!activeEgressIdentity) {
			throw new Error("runtime sidecar egress revision requires the configured numeric identity");
		}
		systemUnits.push(
			writeRuntimeSidecarSystemdUnit({
				program: activeEgressProgram,
				identity: activeEgressIdentity,
				manifest,
				paths,
				workspaceRoot,
				commonEnvironment,
			}),
		);
	}

	for (const program of runtimePrograms) {
		if (program.programKind === "file-browser") {
			systemUnits.push(
				writeFileBrowserSystemdUnit({
					program,
					manifest,
					paths,
					runtimeIdentity,
				}),
			);
			continue;
		}
		if (deferredRuntimeUserUnitNames.has(runtimeSystemdUserUnitName(program))) {
			removeGeneratedRuntimeBaseUnit(paths, runtimeSystemdUserUnitName(program));
			removeSystemdUserEnvironmentDropIn(paths, runtimeSystemdProgramName(program));
			continue;
		}
		userUnits.push(
			writeRuntimeSystemdUserProgram({
				program,
				commonEnvironment,
				manifest,
				paths,
				secretValues,
				providerProjectionRevisions,
				runtimeRevision,
			}),
		);
	}
	return {
		systemUnits,
		userUnits,
		egressSidecarActive: shouldRunEgress,
		staleFiles,
		containerCompanionServices: containerCompanions.desiredServices,
		containerCompanions,
	};
}

export function validateRuntimeSystemdPlan(programs: RuntimeSystemdUserProgram[]): void {
	for (const program of programs) {
		systemdUnitFileName(runtimeSystemdProgramName(program));
		systemdPath(program.cwd);
		systemdExec(program.command, program.args);
		for (const [key, value] of Object.entries(program.env)) {
			if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
				throw new Error(`invalid systemd environment key: ${key}`);
			}
			systemdEnvironmentFileQuote(value);
		}
	}
}
