import { beforeAll, describe, expect, test } from "bun:test";

type AgentRailLoadState = typeof import("@/components/app-sidebar").agentRailLoadState;

let getAgentRailLoadState: AgentRailLoadState | null = null;

beforeAll(async () => {
	process.env.VITE_IFORGE_API_URL = "http://localhost:8000";
	process.env.VITE_CLERK_PUBLISHABLE_KEY = "pk_test_dummy";
	const sidebar = await import("@/components/app-sidebar");
	getAgentRailLoadState = sidebar.agentRailLoadState;
});

function agentRailLoadState(args: Parameters<AgentRailLoadState>[0]) {
	if (!getAgentRailLoadState) throw new Error("agentRailLoadState was not loaded");
	return getAgentRailLoadState(args);
}

const COLD = {
	agentsLoaded: false,
	agentCount: 0,
	billingLocked: false,
	inventoryUnavailable: false,
};

describe("agent rail load state", () => {
	test("a first load still shows the skeleton", () => {
		expect(agentRailLoadState(COLD)).toBe("loading");
	});

	test("an unavailable hosted inventory settles instead of loading forever", () => {
		// e.g. GET /v2/deployments answering 503 compute_unavailable.
		expect(agentRailLoadState({ ...COLD, inventoryUnavailable: true })).toBe("unavailable");
	});

	test("resolved membership or visible agents are settled", () => {
		expect(agentRailLoadState({ ...COLD, agentsLoaded: true })).toBe("settled");
		expect(agentRailLoadState({ ...COLD, agentCount: 2, inventoryUnavailable: true })).toBe(
			"settled",
		);
	});

	test("a billing-locked shell never reads as loading or unavailable", () => {
		expect(agentRailLoadState({ ...COLD, billingLocked: true })).toBe("settled");
		expect(agentRailLoadState({ ...COLD, billingLocked: true, inventoryUnavailable: true })).toBe(
			"settled",
		);
	});
});
