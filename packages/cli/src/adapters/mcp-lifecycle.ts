import { execFileSync } from "node:child_process";
import * as p from "@clack/prompts";
import chalk from "chalk";
import { reconcileLocalHermesMcp } from "../commands/hermes-mcp";
import { errMessage } from "../lib/errors";

export interface McpLifecycle {
	register(): Promise<void>;
	unregister(): Promise<void>;
}

type Command = readonly [command: string, ...args: string[]];

/**
 * The executable every MCP adapter registers. `@iforge/cli` ships exactly one binary (this
 * package's `bin`), so this is the only value that can actually start.
 */
export const MCP_COMMAND = "iforge";

/**
 * The server NAME this product registers inside a third-party agent's config, at all four
 * adapters. It used to stay `clawdi` under D-07 on the grounds that it was the key third-party
 * config already held; P33-07 retired that, because upstream `clawdi` registers under the same
 * key on the same machine and one key cannot hold two servers. `packages/cli/src/mcp/server.ts`
 * moved its self-reported name to match in plan 03.3-01. Teardown removes THIS key and no other:
 * removing `clawdi` would delete an upstream install's registration from a config file iforge
 * does not own (T-03.3-43).
 */
export const MCP_SERVER_NAME = "iforge";

function run(command: Command, encoding?: "utf8"): string {
	const [file, ...args] = command;
	const output = execFileSync(file, args, {
		stdio: ["ignore", "pipe", "pipe"],
		env: process.env,
		...(encoding ? { encoding } : {}),
	});
	return typeof output === "string" ? output : output.toString("utf8");
}

/**
 * The executable an agent currently has registered for an MCP server name, or `undefined`
 * when that cannot be established -- not registered, agent not installed, or an output shape this
 * does not parse.
 *
 * `undefined` deliberately means "unknown", never "healthy". Callers must re-register on unknown:
 * accepting an unreadable probe as a pass is precisely the defect this replaces.
 */
function registeredMcpCommand(probe: Command): string | undefined {
	let output: string;
	try {
		output = run(probe, "utf8");
	} catch {
		return undefined;
	}

	try {
		const parsed: unknown = JSON.parse(output);
		if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
			const command = (parsed as Record<string, unknown>).command;
			if (typeof command === "string") return command.trim() || undefined;
		}
	} catch {
		// Not JSON — older agent builds print a plain-text record, handled below.
	}

	const field = output.match(/^[ \t]*"?command"?[ \t]*[:=][ \t]*"?([^"\n]+?)"?[ \t]*,?$/im);
	return field?.[1]?.trim() || undefined;
}

/**
 * Best-effort removal of an existing entry before re-registering. Both agents' `add` refuse to
 * overwrite an existing server name, so without this a stale entry survives every future
 * setup run. Each candidate is tried in turn because the scope flag is not universal; a failure
 * here is expected whenever nothing is registered and must not stop registration.
 */
function removeStaleMcpEntry(candidates: readonly Command[]): void {
	for (const candidate of candidates) {
		try {
			run(candidate);
			return;
		} catch {
			// Not registered, unsupported flag, or the agent is absent — try the next form.
		}
	}
}

function commandLifecycle(input: {
	label: string;
	/**
	 * Idempotency keys on the registered *command*, not on the server name: a name match cannot
	 * tell a healthy entry from one an earlier setup wrote pointing at a binary that no longer
	 * exists, and "already registered" for both left the user permanently unrepairable behind a
	 * green checkmark.
	 */
	probeCommand?: Command;
	staleRemoveCommands?: readonly Command[];
	registerCommand: Command;
	unregisterCommand: Command;
	manualRegister: string;
	registeredMessage: string;
}): McpLifecycle {
	return {
		async register() {
			if (input.probeCommand && registeredMcpCommand(input.probeCommand) === MCP_COMMAND) {
				console.log(chalk.gray(`✓ MCP server already registered in ${input.label}`));
				return;
			}
			if (input.staleRemoveCommands) removeStaleMcpEntry(input.staleRemoveCommands);
			try {
				run(input.registerCommand);
				console.log(chalk.green(input.registeredMessage));
			} catch {
				console.log(chalk.yellow(`⚠ Could not auto-register MCP server in ${input.label}.`));
				// The printed form must name the same key the executed form used. A hint naming the
				// old key sends the user to register the wrong server by hand.
				console.log(chalk.gray(`  Run manually: ${input.manualRegister}`));
			}
		},
		async unregister() {
			try {
				run(input.unregisterCommand);
				console.log(chalk.green(`${input.label}: removed MCP server registration`));
			} catch {
				// `mcp remove` returns non-zero if the entry didn't exist — that's fine.
				console.log(
					chalk.gray(`${input.label}: MCP server already absent (or removal not supported)`),
				);
			}
		},
	};
}

const CLAUDE_MCP_CONFIG = JSON.stringify({ type: "stdio", command: MCP_COMMAND, args: ["mcp"] });

export const claudeMcpLifecycle: McpLifecycle = commandLifecycle({
	label: "Claude Code",
	probeCommand: ["claude", "mcp", "get", MCP_SERVER_NAME, "--json"],
	staleRemoveCommands: [
		["claude", "mcp", "remove", MCP_SERVER_NAME, "--scope", "user"],
		["claude", "mcp", "remove", MCP_SERVER_NAME],
	],
	// `--scope user` is Claude Code CLI terminology: register MCP at user level (not
	// project-local). This is unrelated to iforge Project data boundaries.
	registerCommand: [
		"claude",
		"mcp",
		"add-json",
		MCP_SERVER_NAME,
		CLAUDE_MCP_CONFIG,
		"--scope",
		"user",
	],
	unregisterCommand: ["claude", "mcp", "remove", MCP_SERVER_NAME],
	manualRegister: `claude mcp add-json ${MCP_SERVER_NAME} '${CLAUDE_MCP_CONFIG}' --scope user`,
	registeredMessage: "✓ MCP server registered in Claude Code",
});

// In `codex mcp add <name> -- <command...>` the token after `--` is the executable. `@iforge/cli`
// ships exactly one binary, `iforge`, so any other token here registers an MCP server that cannot
// start and reports success anyway.
export const codexMcpLifecycle: McpLifecycle = commandLifecycle({
	label: "Codex",
	probeCommand: ["codex", "mcp", "get", MCP_SERVER_NAME, "--json"],
	staleRemoveCommands: [["codex", "mcp", "remove", MCP_SERVER_NAME]],
	registerCommand: ["codex", "mcp", "add", MCP_SERVER_NAME, "--", MCP_COMMAND, "mcp"],
	unregisterCommand: ["codex", "mcp", "remove", MCP_SERVER_NAME],
	manualRegister: `codex mcp add ${MCP_SERVER_NAME} -- ${MCP_COMMAND} mcp`,
	registeredMessage: "✓ MCP server registered in Codex",
});

const OPENCLAW_MCP_CONFIG = JSON.stringify({ command: MCP_COMMAND, args: ["mcp"] });

// OpenClaw exposes a non-interactive config setter, so prefer its native CLI over editing
// ~/.openclaw/openclaw.json directly.
export const openClawMcpLifecycle: McpLifecycle = commandLifecycle({
	label: "OpenClaw",
	registerCommand: ["openclaw", "mcp", "set", MCP_SERVER_NAME, OPENCLAW_MCP_CONFIG],
	unregisterCommand: ["openclaw", "mcp", "unset", MCP_SERVER_NAME],
	manualRegister: `openclaw mcp set ${MCP_SERVER_NAME} '${OPENCLAW_MCP_CONFIG}'`,
	registeredMessage: "✓ MCP server registered in OpenClaw",
});

export const hermesMcpLifecycle: McpLifecycle = {
	async register() {
		try {
			if (!reconcileLocalHermesMcp(true)) {
				console.log(chalk.gray("✓ MCP server already registered in Hermes"));
				return;
			}
			console.log(chalk.green("✓ MCP server registered in Hermes"));
		} catch (error) {
			console.log(chalk.yellow(`⚠ Could not register MCP server in Hermes: ${errMessage(error)}`));
			console.log(chalk.gray("  Check with: hermes config get mcp_servers --json"));
		}
	},
	async unregister() {
		try {
			if (reconcileLocalHermesMcp(false)) {
				console.log(chalk.green("Hermes: removed MCP server registration"));
				// UI-SPEC S2. A user running both products must be able to READ that iforge left the
				// other install alone; silence here is indistinguishable from the P33-07 defect.
				p.log.info(
					`  Removing ${MCP_SERVER_NAME} entries only. Any clawdi entries in this file are left alone.`,
				);
			} else {
				console.log(chalk.gray("Hermes: MCP server already absent"));
			}
		} catch (error) {
			console.log(
				chalk.yellow(`Hermes: could not remove MCP server registration (${errMessage(error)})`),
			);
			console.log(chalk.gray("  Check with: hermes config get mcp_servers --json"));
		}
	},
};
