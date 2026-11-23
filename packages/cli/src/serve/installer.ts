/**
 * Generate / load / unload `iforge daemon` as a per-user OS
 * service.
 *
 * Two backends, one shape:
 *
 *   - macOS: ~/Library/LaunchAgents/dev.iforge.serve.plist + launchctl
 *   - Linux: ~/.config/systemd/user/iforge-serve.service + systemctl --user
 *
 * Per-user (not system-wide) on purpose:
 *   - the daemon reads auth.json from the state root, which is per-user
 *   - keeping it user-scoped means no sudo, no risk of stomping
 *     on a different user's auth, and the unit dies cleanly when
 *     the user logs out (laptops where each session ssh's into
 *     a fresh shell)
 *
 * `iforge daemon install` writes one singleton unit
 * (`dev.iforge.serve` / `iforge-serve.service`) whose process runs
 * every registered agent's sync engine. Older per-agent units are
 * still detected so setup/install can remove them during migration.
 *
 * Windows: current-user InteractiveToken Task Scheduler task.
 */

import { execFileSync } from "node:child_process";
import {
	chmodSync,
	existsSync,
	mkdirSync,
	readFileSync,
	statSync,
	unlinkSync,
	writeFileSync,
} from "node:fs";
import { homedir, platform } from "node:os";
import { join } from "node:path";
import { AGENT_TYPES, type AgentType } from "../adapters/agent-types";
import { BRAND_ENV_ALIASES } from "../lib/brand-env";
import {
	type CurrentCliInvocation,
	detectDesktopManagedNativeLayout,
	detectHomebrewManagedNativeLayout,
	resolveCurrentCliInvocation,
	resolveCurrentCliLayout,
} from "../lib/current-cli-invocation";
import { stateRoot } from "../lib/state-root";
import { GENERATED_RUNTIME_SYSTEMD_FILE_HEADER } from "../runtime/systemd-user";
import {
	installWindowsTask,
	restartWindowsTask,
	stopWindowsTask,
	uninstallWindowsTask,
	windowsTaskInstalled,
	windowsTaskRunning,
	windowsTaskStatus,
} from "./windows-task";

interface InstallOpts {
	/** Internal migration hook for removing pre-singleton per-agent units. */
	agent?: string;
	rpcHost?: string;
	rpcPort?: number;
	rpcAllowRemote?: boolean;
}

/**
 * The reverse-DNS launchd label, from the domain this fork actually owns (P33-20: `iforge.dev`).
 *
 * Exported because `serve/sync-engine.ts` removes launchd supervision by label and had its own
 * copy of the string. A launchd label is unique per user -- two products claiming one label is a
 * hard conflict rather than a last-writer-wins -- so it gets exactly one derivation.
 */
export const LAUNCHD_DAEMON_LABEL = "dev.iforge.serve";

/** The systemd `--user` unit basename. The `.service` suffix and per-agent form derive from it. */
export const SYSTEMD_DAEMON_UNIT_BASENAME = "iforge-serve";

function home(): string {
	return process.env.HOME || homedir();
}

function unitName(): string {
	return LAUNCHD_DAEMON_LABEL;
}

function daemonProgramArgs(opts: InstallOpts): string[] {
	if (opts.agent) return ["daemon", "run", "--agent", opts.agent];
	return ["daemon", "run"];
}

/** Env vars that need to be baked into the supervisor
 * unit so the daemon under launchd / systemd sees them after
 * reboot. Capturing these at install time matches "the daemon
 * runs the same way it ran when I installed it" — the user's
 * mental model. Without this, an env-only auth setup
 * (`IFORGE_AUTH_TOKEN=… iforge daemon install`) silently breaks
 * after the first reboot because the supervisor strips the
 * shell env and the state root's `auth.json` was never written.
 *
 * The names below are the PUBLIC spelling of every contract member,
 * because the unit's environment is inherited by an `iforge` process
 * whose own entrypoint refuses an internal register name that arrives
 * without its public counterpart. The VALUE is still read out of the
 * internal register (see `persistedEnvSourceName`), so a token supplied
 * via `--auth-token-file` — which writes the internal name directly and
 * has no public spelling — is still captured.
 *
 * Whitelist deliberately narrow:
 *   - IFORGE_AUTH_TOKEN / IFORGE_API_URL: auth + endpoint
 *   - IFORGE_STATE_DIR: state dir override
 *   - IFORGE_NO_AUTO_UPDATE: keep an embedding application in charge of updates
 *   - IFORGE_DAEMON_RPC_HOST / IFORGE_DAEMON_RPC_PORT /
 *     IFORGE_DAEMON_RPC_ALLOW_REMOTE: HTTP listener settings for
 *     the owner-token-protected control RPC
 *   - IFORGE_AGENT_TYPE: container fallback when no env registry exists
 *   - IFORGE_SERVE_MODE: container/laptop mode
 *   - IFORGE_SERVE_DEBUG: verbose log level
 *   - CLAUDE_CONFIG_DIR / CODEX_HOME / HERMES_HOME /
 *     OPENCLAW_STATE_DIR / OPENCLAW_AGENT_ID: per-adapter
 *     overrides (the daemon depends on these to find each
 *     agent's local data root)
 *
 * NOTE: `IFORGE_ENVIRONMENT_ID` is deliberately NOT captured here.
 * It's per-agent state and lives in `<state root>/environments/<agent>.json`
 * (written by `iforge setup`). Capturing the shell env var would let a
 * single env id leak into the singleton daemon and be misapplied across
 * multiple engines.
 */
export const PERSISTED_ENV_KEYS = [
	"IFORGE_AUTH_TOKEN",
	"IFORGE_API_URL",
	// Now a contract member: plan 03.1-16 added the `IFORGE_STATE_DIR`
	// pair. The supervised daemon runs the same entrypoint boundary as
	// this process, so an internal spelling persisted here would be
	// REFUSED by that boundary on the next start; the public spelling is
	// what the child re-derives its own register from.
	"IFORGE_STATE_DIR",
	"IFORGE_NO_AUTO_UPDATE",
	// Internal: set by the bundled desktop runtime (upstream #1289 desktop), never by a user.
	"CLAWDI_DESKTOP_RUNTIME",
	"IFORGE_DAEMON_RPC_HOST",
	"IFORGE_DAEMON_RPC_PORT",
	"IFORGE_DAEMON_RPC_ALLOW_REMOTE",
	// CLAWDI_HOME redirects the entire CLI state tree (auth.json,
	// environments, locks, serve queue/health) to a sibling
	// directory; honored by `lib/config.ts:clawdiDir()` and
	// `serve/paths.ts:getServeStateDir()`. Without persisting it
	// in the supervisor unit, an install run via
	// `CLAWDI_HOME=… iforge daemon install` would foreground-work
	// but the supervised daemon would fall back to the real
	// default state root after the user logs out — splitting state
	// across two directories and breaking the isolation guarantee.
	// It is also NOT a contract member (D-07 / X-1, deferred state-root
	// migration), which is why it is the one brand entry here that keeps
	// the internal spelling.
	"CLAWDI_HOME",
	"IFORGE_AGENT_TYPE",
	"IFORGE_SERVE_MODE",
	"IFORGE_SERVE_DEBUG",
	"CLAUDE_CONFIG_DIR",
	"CODEX_HOME",
	"HERMES_HOME",
	"OPENCLAW_STATE_DIR",
	"OPENCLAW_AGENT_ID",
] as const;

/**
 * The register a persisted key's VALUE is read from.
 *
 * Derived from the alias map rather than retyped, so the list above cannot drift from
 * `config/rebrand-env-contract.txt`. A non-member is read under its own name.
 */
function persistedEnvSourceName(key: string): string {
	return BRAND_ENV_ALIASES[key] ?? key;
}

function capturedEnv(
	opts: InstallOpts = {},
	desktopRuntime: { runtimeRoot?: string } | null = null,
	homebrewManaged = false,
): { key: string; value: string }[] {
	const out: { key: string; value: string }[] = [];
	for (const key of PERSISTED_ENV_KEYS) {
		const value = process.env[persistedEnvSourceName(key)];
		if (value !== undefined && value !== "") {
			out.push({ key, value });
		}
	}
	upsertCapturedEnv(out, "IFORGE_DAEMON_RPC_HOST", opts.rpcHost);
	upsertCapturedEnv(
		out,
		"IFORGE_DAEMON_RPC_PORT",
		opts.rpcPort === undefined ? undefined : String(opts.rpcPort),
	);
	upsertCapturedEnv(
		out,
		"IFORGE_DAEMON_RPC_ALLOW_REMOTE",
		opts.rpcAllowRemote === true ? "1" : undefined,
	);
	if (desktopRuntime || homebrewManaged) {
		upsertCapturedEnv(out, "IFORGE_NO_AUTO_UPDATE", "1");
	}
	if (desktopRuntime) {
		upsertCapturedEnv(out, "CLAWDI_DESKTOP_RUNTIME", desktopRuntime.runtimeRoot);
	}
	return out;
}

function upsertCapturedEnv(
	out: { key: string; value: string }[],
	key: string,
	value?: string,
): void {
	if (value === undefined || value === "") return;
	const existing = out.find((item) => item.key === key);
	if (existing) {
		existing.value = value;
		return;
	}
	out.push({ key, value });
}

interface DaemonInstallContext {
	invocation: CurrentCliInvocation;
	environment: { key: string; value: string }[];
}

/** Resolve this exact CLI installation and its supervisor environment. */
function currentDaemonInstallContext(opts: InstallOpts): DaemonInstallContext {
	let invocation: CurrentCliInvocation;
	let desktopRuntime: ReturnType<typeof detectDesktopManagedNativeLayout> = null;
	let homebrewRuntime: ReturnType<typeof detectHomebrewManagedNativeLayout> = null;
	try {
		const layout = resolveCurrentCliLayout();
		desktopRuntime = detectDesktopManagedNativeLayout(layout, platform());
		homebrewRuntime = detectHomebrewManagedNativeLayout(layout, platform());
		if (
			layout.kind === "native" &&
			!layout.nativeOwnership &&
			!desktopRuntime &&
			!homebrewRuntime
		) {
			throw new Error(
				"an unowned native executable cannot install a daemon; install through Homebrew, the native distribution, or iforge Desktop",
			);
		}
		invocation = resolveCurrentCliInvocation(daemonProgramArgs(opts));
		if (homebrewRuntime) invocation.command = homebrewRuntime.activationPath;
	} catch (error) {
		throw new Error(
			`could not resolve the current CLI for daemon installation: ${
				error instanceof Error ? error.message : String(error)
			}. Reinstall the CLI and try again.`,
		);
	}
	// Reject TypeScript source paths. A common dev-mode footgun:
	// running `bun run packages/cli/src/index.ts daemon install`
	// from a clone bakes the .ts source path into the launchd /
	// systemd unit. After reboot, the supervisor launches that
	// unit via the system `node` binary which can't execute raw
	// TypeScript — daemon crashes silently in a respawn loop and
	// the user has no idea what's wrong because `launchctl load`
	// itself succeeded. Fail loudly at install time instead.
	if (invocation.entryPath && /\.tsx?$/.test(invocation.entryPath)) {
		throw new Error(
			"refusing to install a daemon unit with a TypeScript source path " +
				`(entry=${invocation.entryPath}). The OS supervisor can't run .ts files. ` +
				"Build a JS bundle first (npm i -g @iforge/cli or bun run build) " +
				"and re-run install from the installed binary.",
		);
	}
	return {
		invocation,
		environment: capturedEnv(opts, desktopRuntime, homebrewRuntime !== null),
	};
}

export function install(opts: InstallOpts = {}): {
	unit: string;
	instructions: string;
	replaced: boolean;
} {
	const p = platform();
	if (p === "darwin") return installLaunchd(opts);
	if (p === "linux") return installSystemd(opts);
	if (p === "win32") {
		const context = currentDaemonInstallContext(opts);
		return installWindowsTask(stateRoot(), context.invocation, [
			{ key: "HOME", value: home() },
			...context.environment,
		]);
	}
	throw new Error(`unsupported platform for service install: ${p}`);
}

export function uninstall(opts: InstallOpts = {}): { removed: boolean } {
	const p = platform();
	if (p === "darwin") return uninstallLaunchd(opts);
	if (p === "linux") return uninstallSystemd(opts);
	if (p === "win32") return uninstallWindowsTask(stateRoot());
	throw new Error(`unsupported platform for service uninstall: ${p}`);
}

export function statusLines(opts: InstallOpts = {}): string[] {
	const p = platform();
	if (p === "darwin") return statusLaunchd(opts);
	if (p === "linux") return statusSystemd(opts);
	if (p === "win32") return windowsTaskStatus();
	return [`unsupported platform: ${p}`];
}

/** Restart an already-installed daemon unit. Throws if no unit is
 * installed (caller should install first) or if the supervisor
 * refuses to restart (corrupt unit, permissions, etc). */
export function restart(opts: InstallOpts = {}): void {
	const p = platform();
	if (p === "darwin") {
		restartLaunchd(opts);
	} else if (p === "linux") {
		restartSystemd(opts);
	} else if (p === "win32") {
		restartWindowsTask();
	} else {
		throw new Error(`unsupported platform for service restart: ${p}`);
	}
}

/** Stop an installed daemon without removing or disabling its supervisor unit. */
export function stop(opts: InstallOpts = {}): void {
	const p = platform();
	if (p === "darwin") {
		stopLaunchd(opts);
	} else if (p === "linux") {
		stopSystemd(opts);
	} else if (p === "win32") {
		stopWindowsTask();
	} else {
		throw new Error(`unsupported platform for service stop: ${p}`);
	}
}

function restartLaunchd(opts: InstallOpts): void {
	const path = opts.agent ? plistPath(opts.agent) : singletonPlistPath();
	if (!existsSync(path)) {
		throw new Error("no daemon unit installed (run `iforge daemon install` first)");
	}
	const label = opts.agent ? legacyUnitName(opts.agent) : unitName();
	const target = `gui/${process.getuid?.() ?? 501}/${label}`;
	// Two restart shapes depending on launchd's view of the unit:
	//   - Loaded: hot restart via `kickstart -k` (kills the running
	//     job and lets launchd respawn it; preserves the unit's
	//     OnDemand/KeepAlive policies).
	//   - Unloaded but plist exists (launchd auto-ejected after
	//     enough crash-loop exits, or user manually `bootout`'d
	//     without removing the file): cold reload via unload+load.
	// Pre-fix `restart` always tried kickstart and gave up with a
	// "kickstart failed" error when the unit was ejected — the user
	// then had to manually `launchctl load -w <path>` themselves.
	const isLoaded = tryRunCapture(["launchctl", "list", label]) !== null;
	if (isLoaded && tryRun(["launchctl", "kickstart", "-k", target])) return;
	tryRun(["launchctl", "unload", path]);
	if (!tryRun(["launchctl", "load", "-w", path])) {
		throw new Error(
			`launchctl could not (re)load ${label}. ` + `Try manually: launchctl load -w "${path}"`,
		);
	}
}

function restartSystemd(opts: InstallOpts): void {
	const unit = unitFileName(opts.agent);
	const ok = tryRun(["systemctl", "--user", "restart", unit]);
	if (!ok) {
		throw new Error(
			`systemctl --user restart ${unit} failed. ` +
				`Check \`systemctl --user status ${unit}\` for details.`,
		);
	}
}

// ---------------------------------------------------------------------------
// macOS / launchd
// ---------------------------------------------------------------------------

function launchAgentsDir(): string {
	const dir = join(home(), "Library", "LaunchAgents");
	if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
	return dir;
}

function plistPath(agent: string): string {
	return join(launchAgentsDir(), `${legacyUnitName(agent)}.plist`);
}

function singletonPlistPath(): string {
	return join(launchAgentsDir(), `${unitName()}.plist`);
}

/**
 * The per-agent launchd label, from the pre-singleton era.
 *
 * "legacy" names the ERA, not the brand: the singleton replaced per-agent units, and this form is
 * still written by `install({ agent })` so setup can detect and retire it.
 */
function legacyUnitName(agent: string): string {
	return `${LAUNCHD_DAEMON_LABEL}.${agent}`;
}

/**
 * The plist key this release stamps into every launchd job it writes, as proof of ownership.
 *
 * `~/Library/LaunchAgents` is a shared namespace, exactly as `~/.config/systemd/user` is: upstream
 * `clawdi` writes a job there too, on the same machine, for the same person. A label therefore
 * proves nothing, so ownership is read from the file's own bytes, the same rule
 * `isGeneratedRuntimeSystemdFile` applies on Linux.
 *
 * launchd ignores unknown top-level keys, so the marker is inert to the daemon. It is metadata
 * this product writes and reads back, never something the OS interprets.
 */
export const GENERATED_LAUNCHD_PLIST_OWNER_KEY = "dev.iforge.generated-by";

/** The marker value the writer emits. */
const GENERATED_LAUNCHD_PLIST_OWNER_VALUE = "iforge runtime init";

/**
 * Whether a launchd plist's bytes carry this product's ownership marker.
 *
 * The key must be followed IMMEDIATELY by its `<string>` value — that is what makes it a plist
 * key/value pair rather than the two strings happening to appear in one file, and a decoy that
 * merely mentions the key somewhere in a comment or a path is not a proof of anything.
 *
 * Comparison is over the exact bytes: no case folding, no Unicode normalisation, no trimming of
 * the value (MIGR-01, edge: encoding). Ownership decides whether a daemon someone depends on gets
 * disabled, and every softening of an equality test is a way for another product's file to answer
 * yes. It fails closed: anything it cannot read as the marker is not ours.
 */
export function isGeneratedLaunchdPlist(contents: string): boolean {
	const key = `<key>${GENERATED_LAUNCHD_PLIST_OWNER_KEY}</key>`;
	const at = contents.indexOf(key);
	if (at === -1) return false;
	const value = /^\s*<string>([^<]*)<\/string>/.exec(contents.slice(at + key.length));
	if (value === null) return false;
	return value[1] === GENERATED_LAUNCHD_PLIST_OWNER_VALUE;
}

/**
 * The launchd log directory, created if absent.
 *
 * Resolved through the state-root authority, the same one `getServeLogPath` in `paths.ts` uses.
 * That shared resolution is the fix for the original defect: this once hardcoded the home-derived
 * path, so `CLAWDI_HOME=/foo iforge daemon install` baked one path into the plist while
 * `daemon logs` (which did honour the override) looked at another, and the log file could not be
 * found. One authority makes the installer and the log command unable to disagree.
 */
function launchdLogDir(): string {
	const logDir = join(stateRoot(), "serve", "logs");
	if (!existsSync(logDir)) mkdirSync(logDir, { recursive: true });
	return logDir;
}

/**
 * The plist body this release writes, rendered without loading anything.
 *
 * Split out of `installLaunchd` so the ownership marker it stamps is provable off macOS.
 * `install()` dispatches on `platform()`, so `installLaunchd` is unreachable on the Linux hosts
 * this repository's Docker-backed runner and its CI use — and a marker whose emission only a
 * macOS run could check is a marker nothing here checks. An ownership claim no test can fail is
 * the exact shape this plan exists to remove, so the emission gets a seam rather than an excuse.
 */
export function renderLaunchdPlist(opts: InstallOpts = {}): string {
	const label = opts.agent ? legacyUnitName(opts.agent) : unitName();
	const context = currentDaemonInstallContext(opts);
	const invocation = context.invocation;
	const logDir = launchdLogDir();

	// `KeepAlive=true` so launchd respawns on crash. `RunAtLoad`
	// starts at user login. `ThrottleInterval=10` prevents a
	// crashloop from melting the box. `StandardErrorPath` →
	// stderr (where we emit JSON logs) lands in a rotating
	// file the user can `tail`.
	//
	const programArgs = [invocation.command, ...invocation.args]
		.map((arg) => `    <string>${escapeXml(arg)}</string>`)
		.join("\n");
	// The ownership marker goes FIRST inside the top-level `<dict>`, before `Label`, for the
	// reason the systemd header goes on line one of a unit file: `~/Library/LaunchAgents` is a
	// namespace shared with upstream `clawdi` on the same machine for the same person, so a
	// plist's label proves nothing about who wrote it. launchd ignores
	// unknown top-level keys, so this is inert to the daemon and read back only by
	// `isGeneratedLaunchdPlist`.
	return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>${escapeXml(GENERATED_LAUNCHD_PLIST_OWNER_KEY)}</key>
  <string>${escapeXml(GENERATED_LAUNCHD_PLIST_OWNER_VALUE)}</string>
  <key>Label</key>
  <string>${escapeXml(label)}</string>
  <key>ProgramArguments</key>
  <array>
${programArgs}
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardErrorPath</key>
  <string>${escapeXml(join(logDir, `${opts.agent ?? "daemon"}.stderr.log`))}</string>
  <key>StandardOutPath</key>
  <string>${escapeXml(join(logDir, `${opts.agent ?? "daemon"}.stdout.log`))}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${escapeXml(home())}</string>${context.environment
			.map(
				({ key, value }) =>
					`\n    <key>${escapeXml(key)}</key>\n    <string>${escapeXml(value)}</string>`,
			)
			.join("")}
  </dict>
</dict>
</plist>
`;
}

function installLaunchd(opts: InstallOpts): {
	unit: string;
	instructions: string;
	replaced: boolean;
} {
	const label = opts.agent ? legacyUnitName(opts.agent) : unitName();
	const logDir = launchdLogDir();
	const plist = renderLaunchdPlist(opts);

	const path = opts.agent ? plistPath(opts.agent) : singletonPlistPath();
	// Sample BEFORE we writeFileSync — caller wants to know
	// whether this install was a fresh write or replacing an
	// existing unit.
	const replaced = existsSync(path);
	// 0600: the plist body inlines `CLAWDI_AUTH_TOKEN` and any
	// other captured shell env vars under `<key>EnvironmentVariables</key>`.
	// World-readable mode would let any other local user on a
	// multi-user host read the API token. launchd reads the file
	// as the owning user, so 0600 still loads correctly. The
	// `writeFileSync({ mode })` option only fires at create time
	// — explicit chmodSync covers the overwrite case
	// (re-running install on top of a 0644 leftover from older
	// builds).
	writeFileSync(path, plist, { mode: 0o600 });
	try {
		chmodSync(path, 0o600);
	} catch {
		/* best effort — owner of the file is the only writer here */
	}

	// Best-effort load. If an old version is already loaded,
	// unload first — `launchctl load -w` is idempotent on the
	// label but the file path swap still requires a clean
	// reload to pick up edits.
	tryRun(["launchctl", "unload", path]);
	const loaded = tryRun(["launchctl", "load", "-w", path]);
	if (!loaded) {
		throw new Error(
			`Wrote daemon unit to ${path}, but launchctl activation failed. ` +
				`The unit was preserved. Try: launchctl load -w "${path}"`,
		);
	}

	const instructions = `Loaded ${label}. Tail logs with: tail -f ${join(logDir, `${opts.agent ?? "daemon"}.stderr.log`)}`;
	return { unit: path, instructions, replaced };
}

function uninstallLaunchd(opts: InstallOpts): { removed: boolean } {
	const path = opts.agent ? plistPath(opts.agent) : singletonPlistPath();
	if (!existsSync(path)) return { removed: false };
	// Stop the daemon BEFORE removing the plist. Pre-fix this used a
	// bare `tryRun(["launchctl", "unload", path])` and ignored the
	// return code, then `unlinkSync(path)` regardless — so a
	// failed unload (corrupt plist, label mismatch, race) left the
	// daemon process running while the unit file was gone, with the
	// CLI confidently reporting "✓ Removed".
	//
	// macOS has two stop forms:
	//   - `launchctl unload <path>`: legacy, works for plists under
	//     ~/Library/LaunchAgents.
	//   - `launchctl bootout gui/<uid>/<label>`: modern (10.10+),
	//     works regardless of whether the plist file still exists.
	// Try bootout first; fall back to the legacy unload form. Only proceed to
	// unlink the plist after we've confirmed the daemon is stopped
	// (or was never loaded in the first place).
	stopLaunchd(opts);
	unlinkSync(path);
	return { removed: true };
}

function stopLaunchd(opts: InstallOpts): void {
	const path = opts.agent ? plistPath(opts.agent) : singletonPlistPath();
	if (!existsSync(path)) {
		throw new Error("no daemon unit installed (run `iforge daemon install` first)");
	}
	const label = opts.agent ? legacyUnitName(opts.agent) : unitName();
	if (tryRunCapture(["launchctl", "list", label]) === null) return;
	const target = `gui/${process.getuid?.() ?? 501}/${label}`;
	if (tryRun(["launchctl", "bootout", target]) || tryRun(["launchctl", "unload", path])) return;
	throw new Error(
		`Failed to stop running daemon ${label}. Try manually: launchctl bootout gui/$(id -u)/${label}`,
	);
}

function statusLaunchd(opts: InstallOpts): string[] {
	const label = opts.agent ? legacyUnitName(opts.agent) : unitName();
	const lines: string[] = [];
	const path = opts.agent ? plistPath(opts.agent) : singletonPlistPath();
	lines.push(`unit:    ${existsSync(path) ? path : "(not installed)"}`);
	const out = tryRunCapture(["launchctl", "list", label]);
	if (out !== null) {
		// launchctl list <label> prints a plist-ish dict on stdout
		// or fails if not loaded. We surface the raw output —
		// `PID = <n>` and `LastExitStatus = <n>` are the bits
		// the user wants.
		lines.push("launchctl:");
		for (const ln of out.split("\n").filter(Boolean)) {
			lines.push(`  ${ln}`);
		}
	} else {
		lines.push("launchctl: not loaded");
	}
	return lines;
}

// ---------------------------------------------------------------------------
// Linux / systemd --user
// ---------------------------------------------------------------------------

function systemdUserDir(): string {
	const dir = join(home(), ".config", "systemd", "user");
	if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
	return dir;
}

export function unitFileName(agent?: string): string {
	return agent
		? `${SYSTEMD_DAEMON_UNIT_BASENAME}-${agent}.service`
		: `${SYSTEMD_DAEMON_UNIT_BASENAME}.service`;
}

function unitPath(agent?: string): string {
	return join(systemdUserDir(), unitFileName(agent));
}

function installSystemd(opts: InstallOpts): {
	unit: string;
	instructions: string;
	replaced: boolean;
} {
	const context = currentDaemonInstallContext(opts);
	const invocation = context.invocation;
	const path = unitPath(opts.agent);
	const replaced = existsSync(path);

	// systemd `Environment="KEY=VALUE"` parses backslash + double-
	// quote inside the value. A $HOME containing `"` could close
	// the value early and append arbitrary directives; `\` + `n`
	// could be interpreted as a newline by some parsers. Reject
	// any control char, then escape `\` and `"` for the rest. We
	// trust process.execPath / argv[1] (kernel-provided, already
	// realpath'd) but $HOME is user-controlled.
	const homeValue = home();
	// biome-ignore lint/suspicious/noControlCharactersInRegex: targeting control chars on purpose
	if (/[\x00-\x1F\x7F]/.test(homeValue)) {
		throw new Error(
			"HOME contains control characters; refusing to write systemd unit. " +
				"Set HOME to a clean path before running install.",
		);
	}
	const escapedHome = homeValue.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/%/g, "%%");

	// Same control-char + quote escaping for every captured env
	// value. Reject control chars outright (would let an attacker
	// inject newlines + extra Environment= directives); escape
	// `\` and `"` for the rest.
	const envLines: string[] = [`Environment="HOME=${escapedHome}"`];
	for (const { key, value } of context.environment) {
		// biome-ignore lint/suspicious/noControlCharactersInRegex: targeting control chars on purpose
		if (/[\x00-\x1F\x7F]/.test(value)) {
			throw new Error(
				`Env var ${key} contains control characters; refusing to write systemd unit.`,
			);
		}
		const esc = value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/%/g, "%%");
		envLines.push(`Environment="${key}=${esc}"`);
	}

	// `Restart=always` matches launchd's KeepAlive (which restarts
	// regardless of exit code). `Restart=on-failure` looks safer
	// but it's wrong for our auto-update path: when the daemon
	// detects a binary upgrade it exits cleanly with code 0 so
	// the next start picks up the new binary. systemd reads code
	// 0 as a deliberate stop and won't relaunch — the daemon
	// silently dies until the user logs in again. macOS launchd
	// already does the right thing here; align Linux to match.
	// `RestartPreventExitStatus=2` reserves a supervisor control outcome for
	// states that require user action (auth revoked, Agent disconnected, or a
	// schema older than this binary expects). It suppresses restart without
	// classifying those intentional stops as application crashes.
	// `WantedBy=default.target` is the systemd --user equivalent
	// of "start at user login"; requires `loginctl enable-linger
	// <user>` to fire on boot rather than first login session.
	// The ownership header, first line, for the reason the whole of P33-01 exists: two products
	// write into `~/.config/systemd/user`, so a unit's name proves nothing about who wrote it.
	//
	// Safe only because `isOwnedRuntimeSystemdEntry` exempts the serve daemon's unit by name --
	// otherwise a hosted converge, which walks this same directory and collects everything
	// carrying this header that is not in its desired state, would delete it.
	//
	// The unit's description field is COPY, not a marker. systemd never matches on it, nothing in
	// this tree reads it back, and it is what `systemctl --user status` prints to a user looking
	// at a unit this product wrote onto their disk — so it names this product.
	const unit = `${GENERATED_RUNTIME_SYSTEMD_FILE_HEADER}
[Unit]
Description=iforge daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${[invocation.command, ...invocation.args].map(shellEscape).join(" ")}
Restart=always
RestartSec=10
RestartPreventExitStatus=2
StandardOutput=journal
StandardError=journal
${envLines.join("\n")}

[Install]
WantedBy=default.target
`;

	// 0600: same reasoning as the macOS plist above — the unit's
	// `Environment="CLAWDI_AUTH_TOKEN=…"` line carries the API
	// token, so any other local user with read access to
	// `~/.config/systemd/user/` would otherwise lift it. systemd
	// --user reads as the owning user, so 0600 still loads.
	writeFileSync(path, unit, { mode: 0o600 });
	try {
		chmodSync(path, 0o600);
	} catch {
		/* best effort */
	}
	const activated =
		tryRun(["systemctl", "--user", "daemon-reload"]) &&
		tryRun(["systemctl", "--user", "enable", "--now", unitFileName(opts.agent)]) &&
		(!replaced || tryRun(["systemctl", "--user", "restart", unitFileName(opts.agent)]));
	if (!activated) {
		throw new Error(
			`Wrote daemon unit to ${path}, but systemctl activation failed. ` +
				"The unit was preserved. Try: " +
				`systemctl --user daemon-reload && systemctl --user enable --now ${unitFileName(opts.agent)}`,
		);
	}

	const instructions = `Enabled and started ${unitFileName(opts.agent)}. Tail logs with: journalctl --user -u ${unitFileName(opts.agent)} -f`;
	return { unit: path, instructions, replaced };
}

function uninstallSystemd(opts: InstallOpts): { removed: boolean } {
	const path = unitPath(opts.agent);
	if (!existsSync(path)) return { removed: false };
	stopSystemd(opts);
	if (!tryRun(["systemctl", "--user", "disable", unitFileName(opts.agent)])) {
		throw new Error(`systemctl --user disable ${unitFileName(opts.agent)} failed.`);
	}
	unlinkSync(path);
	tryRun(["systemctl", "--user", "daemon-reload"]);
	return { removed: true };
}

function stopSystemd(opts: InstallOpts): void {
	const path = unitPath(opts.agent);
	const unit = unitFileName(opts.agent);
	if (!existsSync(path)) {
		throw new Error("no daemon unit installed (run `iforge daemon install` first)");
	}
	if (!tryRun(["systemctl", "--user", "stop", unit])) {
		throw new Error(
			`systemctl --user stop ${unit} failed. ` +
				`Check \`systemctl --user status ${unit}\` for details.`,
		);
	}
}

function statusSystemd(opts: InstallOpts): string[] {
	const lines: string[] = [];
	const path = unitPath(opts.agent);
	lines.push(`unit:    ${existsSync(path) ? path : "(not installed)"}`);
	const out = tryRunCapture(["systemctl", "--user", "is-active", unitFileName(opts.agent)]);
	lines.push(`active:  ${out?.trim() ?? "unknown"}`);
	const sub = tryRunCapture([
		"systemctl",
		"--user",
		"status",
		unitFileName(opts.agent),
		"--no-pager",
	]);
	if (sub !== null) {
		lines.push("systemctl:");
		// status is verbose; show first ~10 lines (header +
		// process tree) — that's all we need for triage.
		for (const ln of sub.split("\n").slice(0, 10)) lines.push(`  ${ln}`);
	}
	return lines;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function tryRun(argv: string[]): boolean {
	try {
		execFileSync(argv[0], argv.slice(1), { env: process.env, stdio: "ignore" });
		return true;
	} catch {
		return false;
	}
}

function tryRunCapture(argv: string[]): string | null {
	try {
		// Pipe stdout (we want it), discard stdin/stderr — pre-fix
		// stderr leaked through, e.g. `launchctl list <label>`
		// failing with "Could not find service ..." printed during
		// `daemon restart`'s liveness probe.
		return execFileSync(argv[0], argv.slice(1), {
			encoding: "utf-8",
			env: process.env,
			stdio: ["ignore", "pipe", "ignore"],
		});
	} catch {
		return null;
	}
}

function escapeXml(s: string): string {
	return s
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;")
		.replace(/'/g, "&apos;");
}

function shellEscape(s: string): string {
	// ExecStart is parsed by systemd, not a shell. Quote the entire argument,
	// including systemd's environment and specifier expansion characters.
	// biome-ignore lint/suspicious/noControlCharactersInRegex: reject directive injection
	if (/[\x00-\x1F\x7F]/.test(s)) throw new Error("Daemon argument contains control characters.");
	if (!/[\\\s"'%$]/.test(s)) return s;
	return `"${s.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/%/g, "%%").replace(/\$/g, "$$$$")}"`;
}

/** Scan the OS supervisor for every iforge daemon unit installed
 * by older `iforge daemon install --agent <agent>` builds,
 * regardless of whether its agent is still registered in
 * the state root's `environments/`. Used only for migration cleanup and
 * logout warnings.
 *
 * Cheap implementation: enumerate `~/Library/LaunchAgents/` (macOS)
 * or `~/.config/systemd/user/` (Linux) and pattern-match against
 * this product's per-agent unit name shape. Skips agents not in `AGENT_TYPES` so
 * a malicious filename can't smuggle into the iteration.
 */
export function listInstalledAgents(): AgentType[] {
	const installed: AgentType[] = [];
	for (const agent of AGENT_TYPES) {
		const p = platform();
		const path = p === "darwin" ? plistPath(agent) : p === "linux" ? unitPath(agent) : null;
		if (path && existsSync(path)) installed.push(agent);
	}
	return installed;
}

export type InstalledDaemonTarget = AgentType | "daemon";

export function isSingletonDaemonInstalled(): boolean {
	const p = platform();
	if (p === "win32") return windowsTaskInstalled();
	const path = p === "darwin" ? singletonPlistPath() : p === "linux" ? unitPath() : null;
	return path ? existsSync(path) : false;
}

/** Health files survive a stop. Query the supervisor before reporting a live
 * daemon so update/startup recovery cannot mistake a fresh old heartbeat for
 * the new process. */
export function isSingletonDaemonRunning(): boolean {
	const p = platform();
	if (p === "win32") return windowsTaskRunning();
	if (p === "linux") {
		return tryRunCapture(["systemctl", "--user", "is-active", unitFileName()])?.trim() === "active";
	}
	if (p === "darwin") {
		const state = tryRunCapture(["launchctl", "list", unitName()]);
		return state !== null && /"?PID"?\s*=\s*[1-9]\d*/.test(state);
	}
	return false;
}

export function listInstalledDaemonTargets(): InstalledDaemonTarget[] {
	const targets: InstalledDaemonTarget[] = [];
	if (isSingletonDaemonInstalled()) targets.push("daemon");
	targets.push(...listInstalledAgents());
	return targets;
}

/** Health-file age check, used by `iforge daemon status` even
 * before the unit framework reports anything. The daemon writes
 * `<state-dir>/health` after every successful heartbeat as a JSON
 * payload (`{"timestamp", "version"}`); file mtime within ~90s
 * means the daemon is alive and reaching the cloud. The `version`
 * field lets `daemon status` flag drift after a CLI upgrade
 * (daemon needs a restart to pick up the new bundle). Older
 * daemons wrote a bare ISO timestamp; the parser falls back to
 * that shape and reports `version: null`.
 */
export function readHealth(stateDir: string): {
	exists: boolean;
	ageSeconds: number | null;
	timestamp: string | null;
	version: string | null;
	executablePath?: string;
} {
	const p = join(stateDir, "health");
	if (!existsSync(p)) return { exists: false, ageSeconds: null, timestamp: null, version: null };
	try {
		const stat = statSync(p);
		const raw = readFileSync(p, "utf-8").trim();
		const age = Math.round((Date.now() - stat.mtimeMs) / 1000);
		// New JSON shape: parse and pull out fields. Old timestamp-
		// only shape: keep `timestamp = raw`, `version = null`.
		if (raw.startsWith("{")) {
			try {
				const parsed = JSON.parse(raw) as {
					timestamp?: string;
					version?: string;
					executablePath?: unknown;
				};
				return {
					exists: true,
					ageSeconds: age,
					timestamp: parsed.timestamp ?? null,
					version: parsed.version ?? null,
					...(typeof parsed.executablePath === "string"
						? { executablePath: parsed.executablePath }
						: {}),
				};
			} catch {
				/* fall through to legacy interpretation */
			}
		}
		return { exists: true, ageSeconds: age, timestamp: raw, version: null };
	} catch {
		return { exists: true, ageSeconds: null, timestamp: null, version: null };
	}
}
