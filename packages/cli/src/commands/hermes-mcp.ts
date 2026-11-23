import { homedir } from "node:os";
import { getHermesHome } from "../adapters/paths";
import {
	getHermesRawConfigValue,
	type HermesConfigCommandContext,
	reconcileHermesConfigValue,
} from "../runtime/hermes-config";

/** This product's own `mcp_servers` key, command and stale cloud-HTTP key. */
const MCP_SERVER_NAME = "iforge";
const STALE_HTTP_MCP_SERVER_NAME = `${MCP_SERVER_NAME}-mcp`;
const IFORGE_HERMES_MCP_SERVER = {
	command: "iforge",
	args: ["mcp"],
};

function localHermesConfigContext(): HermesConfigCommandContext {
	const home = process.env.HOME?.trim() || homedir();
	return {
		command: "hermes",
		home,
		cwd: process.cwd(),
		environment: { HERMES_HOME: getHermesHome() },
	};
}

export function reconcileLocalHermesMcp(enabled: boolean): boolean {
	const context = localHermesConfigContext();
	const current = getHermesRawConfigValue(context, "mcp_servers");
	if (
		current.exists &&
		(typeof current.value !== "object" || current.value === null || Array.isArray(current.value))
	) {
		throw new Error("Hermes config field mcp_servers must be an object");
	}
	const next: Record<string, unknown> = current.exists
		? { ...(current.value as Record<string, unknown>) }
		: {};
	// Only THIS product's keys are touched (P33-07 / T-03.3-43). Under coexistence
	// `clawdi` and `clawdi-mcp` are upstream's registrations in a config file
	// neither product owns exclusively; reconciling them would silently break a
	// working upstream install. `setup.test.ts` and `teardown.test.ts` assert a
	// seeded foreign block survives a run.
	delete next[STALE_HTTP_MCP_SERVER_NAME];
	if (enabled) next[MCP_SERVER_NAME] = IFORGE_HERMES_MCP_SERVER;
	else delete next[MCP_SERVER_NAME];
	return reconcileHermesConfigValue(
		context,
		"mcp_servers",
		Object.keys(next).length > 0 ? next : undefined,
	);
}
