import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { AGENT_TYPES, adapterRegistry, agentSkillTargetDir } from "../../src/adapters/registry";
import {
	type AgentHomeOverrideSnapshot,
	restoreAgentHomeOverrides,
	snapshotAndClearAgentHomeOverrides,
} from "../commands/helpers";

let tmpHome: string;
let origHome: string | undefined;
let origHomeOverrides: AgentHomeOverrideSnapshot = {};

beforeEach(() => {
	origHome = process.env.HOME;
	origHomeOverrides = snapshotAndClearAgentHomeOverrides();
	tmpHome = mkdtempSync(join(tmpdir(), "clawdi-registry-"));
	process.env.HOME = tmpHome;
});

afterEach(() => {
	if (origHome) process.env.HOME = origHome;
	else delete process.env.HOME;
	restoreAgentHomeOverrides(origHomeOverrides);
	origHomeOverrides = {};
	rmSync(tmpHome, { recursive: true, force: true });
});

describe("agentSkillTargetDir exhaustiveness", () => {
	// `agentSkillTargetDir` dispatches on string comparison, not on
	// `Record<AgentType, …>`, so adding a member to the AGENT_TYPES tuple
	// produces NO compile error here. The fallthrough returns null,
	// `installBuiltinSkill` returns early with no message, and the bundled
	// `clawdi` skill never lands — MCP tools are then unavailable inside that
	// runtime with no error anywhere. Iterating the tuple converts the whole
	// class of omission into a failing test, for future agent types too.
	//
	// The invariant is "never silently null", not "never throws". `openclaw`
	// resolves its workspace by shelling out to `openclaw agents list --json`
	// and throws a named error when the binary is absent — which is exactly the
	// loud failure this guard wants, and is the only reason a throw is accepted
	// below. A branch that returns null is the silent failure and always fails.
	// Upstream #1229 made adapters modular: a sessions-only agent (pi, opencode) has no Skill
	// module, so there is no bundled Skill to place and `null` is its correct answer. The guard
	// covers every agent that DOES carry a Skill module, which is where a silent null loses it.
	const skillAgentTypes = AGENT_TYPES.filter(
		(agentType) => adapterRegistry[agentType].create().skills !== undefined,
	);
	it.each([...skillAgentTypes])("never silently returns null for %s", (agentType) => {
		let target: string | null;
		try {
			target = agentSkillTargetDir(agentType, "clawdi");
		} catch (e) {
			// Loud failure is acceptable; a thrown non-Error or an empty message
			// would not be, because neither tells the user what to fix.
			expect(e).toBeInstanceOf(Error);
			expect((e as Error).message.length).toBeGreaterThan(0);
			return;
		}
		expect(target).not.toBeNull();
	});

	it("covers every registry entry, so the loop above cannot silently shrink", () => {
		expect([...AGENT_TYPES].sort()).toEqual(Object.keys(adapterRegistry).sort());
	});
});

describe("agentSkillTargetDir for ironhermes", () => {
	it("resolves under the IronHermes home's skills directory", () => {
		expect(agentSkillTargetDir("ironhermes", "clawdi")).toBe(
			join(tmpHome, ".ironhermes", "skills", "clawdi"),
		);
	});

	it("honours an IRONHERMES_HOME override", () => {
		const override = join(tmpHome, "elsewhere", "ironhermes-root");
		process.env.IRONHERMES_HOME = override;
		try {
			expect(agentSkillTargetDir("ironhermes", "clawdi")).toBe(join(override, "skills", "clawdi"));
		} finally {
			delete process.env.IRONHERMES_HOME;
		}
	});

	it("does not resolve into the Hermes tree", () => {
		const target = agentSkillTargetDir("ironhermes", "clawdi");
		expect(target).not.toBeNull();
		expect(target).not.toContain(join(tmpHome, ".hermes"));
	});

	it("keeps an explicit homeOverride ahead of the registry resolver", () => {
		const override = join(tmpHome, "explicit-root");
		expect(agentSkillTargetDir("ironhermes", "clawdi", override)).toBe(
			join(override, "skills", "clawdi"),
		);
	});
});

describe("adapterRegistry ironhermes entry", () => {
	it("declares the IronHermes display name and its own env file", () => {
		expect(adapterRegistry.ironhermes.displayName).toBe("IronHermes");
		expect(adapterRegistry.ironhermes.envFileName).toBe("ironhermes.json");
	});

	it("constructs an adapter whose agentType is ironhermes", () => {
		expect(adapterRegistry.ironhermes.create().agentType).toBe("ironhermes");
	});

	it("gives every runtime a distinct env file, so registrations cannot collide", () => {
		const envFiles = [...AGENT_TYPES].map((t) => adapterRegistry[t].envFileName);
		expect(new Set(envFiles).size).toBe(envFiles.length);
	});
});
