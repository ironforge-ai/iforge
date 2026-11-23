/**
 * Resolves on-disk locations for `iforge daemon`.
 *
 * Two override knobs, in precedence order:
 *
 *   1. `CLAWDI_STATE_DIR` — explicit serve-state-only redirect.
 *      Pods that mount a writable scratch volume into a non-home
 *      path use this to keep queue.jsonl / health out of the
 *      read-only image filesystem, while keeping auth/config in the
 *      state root.
 *
 *   2. The state root itself, resolved by `lib/state-root.ts` — the
 *      single authority (P33-05), which is where the `CLAWDI_HOME`
 *      whole-tree override is honoured and documented. This file
 *      used to re-derive that precedence; without it a
 *      `CLAWDI_HOME`-isolated install would write daemon queue +
 *      health into the user's real state tree, defeating the
 *      isolation guarantee.
 *
 *   Serve state then lives at `<state root>/serve/<agent>/` so each
 *   sync engine's queue file sits alongside auth.json. Per-agent
 *   suffix still matters inside the singleton daemon: Claude Code and
 *   Codex engines can't share `queue.jsonl` — atomic-rename would race
 *   and one engine's view of the queue would silently overwrite
 *   the other's.
 *
 *   The two knobs are deliberately NOT the same knob and the order is
 *   load-bearing. `CLAWDI_STATE_DIR` is narrower on purpose: it moves
 *   serve state ONLY, leaving auth and config where they are. The
 *   collapse onto the authority layers the state root underneath it
 *   rather than swallowing it, so a pod redirecting its scratch volume
 *   cannot silently relocate the credential root along with it.
 */

import { join } from "node:path";
import { stateRoot } from "../lib/state-root";

export function getServeStateDir(agentType: string): string {
	const stateOverride = process.env.CLAWDI_STATE_DIR;
	if (stateOverride) return join(stateOverride, agentType);
	return join(stateRoot(), "serve", agentType);
}

/** Path to a daemon's stderr/stdout log file. The daemon writes
 * structured JSON-per-line to stderr; stdout stays empty (reserved
 * for future MCP-style framing). Both files are created by
 * launchd/systemd at unit-load time per the path baked into the
 * unit definition (see `installer.ts`). */
export function getServeLogPath(agentType: string, stream: "stderr" | "stdout"): string {
	return join(stateRoot(), "serve", "logs", `${agentType}.${stream}.log`);
}

export function getDaemonControlDir(): string {
	const stateOverride = process.env.CLAWDI_STATE_DIR;
	if (stateOverride) return join(stateOverride, "control");
	return join(stateRoot(), "daemon");
}

export function getDaemonControlTokenPath(): string {
	return join(getDaemonControlDir(), "control-token");
}
