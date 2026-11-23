import { join } from "node:path";
import { assertAgentRootContains, assertAgentRootIsolated } from "./agent-root-guard";
import { AGENT_TYPES, type AgentType } from "./agent-types";
import type { AgentAdapter } from "./base";
import { ClaudeCodeAdapter } from "./claude-code";
import { CodexAdapter } from "./codex";
import { HermesAdapter } from "./hermes";
import { IronHermesAdapter } from "./ironhermes";
import {
	claudeMcpLifecycle,
	codexMcpLifecycle,
	hermesMcpLifecycle,
	type McpLifecycle,
	openClawMcpLifecycle,
} from "./mcp-lifecycle";
import { OpenClawAdapter } from "./openclaw";
import { resolveOpenClawAgentWorkspace } from "./openclaw-workspace";
import { OpenCodeAdapter } from "./opencode";
import {
	getClaudeHome,
	getCodexHome,
	getHermesHome,
	getIronHermesHome,
	getOpenClawHome,
	getOpenCodeDataDir,
	getPiHome,
} from "./paths";
import { PiAdapter } from "./pi";

export { AGENT_TYPES, type AgentType } from "./agent-types";

export interface AdapterRegistryEntry {
	displayName: string;
	/** File name stored under `~/.iforge/environments/` when the agent is registered. */
	envFileName: string;
	/** Lazy home-dir resolver (honors env overrides, probes fallback paths). */
	home: () => string;
	/** Construct an adapter instance. */
	create: () => AgentAdapter;
	/** Optional local MCP lifecycle. Both actions are one indivisible contract. */
	mcpLifecycle?: McpLifecycle;
}

// Registry: every `AgentType` must have exactly one entry — `Record<AgentType, …>`
// enforces exhaustiveness at compile time.
export const adapterRegistry: Record<AgentType, AdapterRegistryEntry> = {
	claude_code: {
		displayName: "Claude Code",
		envFileName: "claude_code.json",
		home: getClaudeHome,
		create: () => new ClaudeCodeAdapter(),
		mcpLifecycle: claudeMcpLifecycle,
	},
	codex: {
		displayName: "Codex",
		envFileName: "codex.json",
		home: getCodexHome,
		create: () => new CodexAdapter(),
		mcpLifecycle: codexMcpLifecycle,
	},
	openclaw: {
		displayName: "OpenClaw",
		envFileName: "openclaw.json",
		home: getOpenClawHome,
		create: () => new OpenClawAdapter(),
		mcpLifecycle: openClawMcpLifecycle,
	},
	hermes: {
		displayName: "Hermes",
		envFileName: "hermes.json",
		home: getHermesHome,
		create: () => new HermesAdapter(),
		mcpLifecycle: hermesMcpLifecycle,
	},
	pi: {
		displayName: "Pi",
		envFileName: "pi.json",
		home: getPiHome,
		create: () => new PiAdapter(),
	},
	opencode: {
		displayName: "OpenCode",
		envFileName: "opencode.json",
		home: getOpenCodeDataDir,
		create: () => new OpenCodeAdapter(),
	},
	// A distinct runtime from upstream Hermes, never a variant of it: separate
	// install root, separate agent identity, separate registration key.
	ironhermes: {
		displayName: "IronHermes",
		envFileName: "ironhermes.json",
		home: getIronHermesHome,
		create: () => new IronHermesAdapter(),
	},
};

export const AGENT_TYPE_HELP_LABEL = AGENT_TYPES.join(", ");
export const SKILL_AGENT_TYPE_HELP_LABEL = AGENT_TYPES.filter(
	(agentType) => adapterRegistry[agentType].create().skills !== undefined,
).join(", ");

/** Registry entry annotated with its agent type — convenience for iteration. */
export interface AnnotatedAdapterEntry extends AdapterRegistryEntry {
	agentType: AgentType;
}

export function allAdapterEntries(): AnnotatedAdapterEntry[] {
	return AGENT_TYPES.map((agentType) => ({
		agentType,
		...adapterRegistry[agentType],
	}));
}

export function getAdapterEntry(type: AgentType): AdapterRegistryEntry | null {
	return adapterRegistry[type] ?? null;
}

/**
 * Where the bundled `iforge` skill lives inside an agent's home.
 * Both `setup` (write) and `teardown` (delete) use this so the two
 * commands can never disagree about the path.
 *
 * This is the ONLY write path for the bundled skill. The
 * `migrateLegacyLocalSetupSkill` call sites in the adapters are upstream's own
 * local-setup-skill migration and name upstream's `clawdi` directory.
 */
export function builtinSkillTargetDir(agentType: AgentType, homeOverride?: string): string | null {
	return agentSkillTargetDir(agentType, "iforge", homeOverride);
}

export function agentSkillTargetDir(
	agentType: AgentType,
	skillName: string,
	homeOverride?: string,
): string | null {
	const home = homeOverride ?? adapterRegistry[agentType]?.home();
	if (!home) return null;
	if (agentType === "openclaw") {
		return join(resolveOpenClawAgentWorkspace(), "skills", skillName);
	}
	if (agentType === "hermes" || agentType === "ironhermes") {
		// D-21, fourth write path. `setup` writes the bundled `clawdi` skill here
		// and `teardown` DELETES it from here, so a home variable pointed at the
		// other Hermes-lineage runtime would have teardown remove that runtime's
		// bundled skill. Guarding the derivation covers both commands, plus the
		// hosted manifest install, with one check. Fails closed: callers get a
		// thrown Error naming the offending root rather than a wrong path.
		const foreignRoot = agentType === "hermes" ? getIronHermesHome() : getHermesHome();
		const context = `${agentType} bundled Skill target`;
		assertAgentRootIsolated(home, [foreignRoot], context);
		const target = join(home, "skills", skillName);
		assertAgentRootContains(home, target, context);
		return target;
	}
	if (agentType === "claude_code" || agentType === "codex") {
		return join(home, "skills", skillName);
	}
	// NOTE: this dispatch is string comparison, NOT `Record<AgentType, …>`, so a
	// new AGENT_TYPES member compiles fine and lands here. Returning null makes
	// `installBuiltinSkill` return early with no message, so the bundled `clawdi`
	// skill never installs and MCP tools are unavailable inside that runtime with
	// no error anywhere. `tests/adapters/registry.test.ts` iterates AGENT_TYPES to
	// make that omission fail loudly instead.
	return null;
}
