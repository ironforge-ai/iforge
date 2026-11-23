import { afterEach, describe, expect, it } from "bun:test";
import {
	chmodSync,
	cpSync,
	existsSync,
	mkdirSync,
	readFileSync,
	rmSync,
	writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { parse as parseYaml } from "yaml";
import type { AgentType } from "../../src/adapters/agent-types";
import { teardown } from "../../src/commands/teardown";
import { stateRootFor } from "../../src/lib/state-root";
import {
	managedSkillReservationState,
	reserveManagedSkill,
} from "../../src/runtime/managed-skill-reservation";
import { cleanupTmp, copyFixtureToTmp } from "../adapters/helpers";
import {
	type AgentHomeOverrideSnapshot,
	restoreAgentHomeOverrides,
	seedAuthAndEnv,
	snapshotAndClearAgentHomeOverrides,
} from "./helpers";

let tmpHome: string;
let origHome: string | undefined;
let origPath: string | undefined;
let origPathSet = false;
let origHomeOverrides: AgentHomeOverrideSnapshot = {};
let origIsTTY: boolean | undefined;
const HERMES_CONFIG_CLI_MOCK = resolve(
	import.meta.dir,
	"../../src/test-support/hermes-config-cli-mock.ts",
);

function setup(
	agent: AgentType,
	options: { managed?: boolean } = {},
): {
	envPath: string;
	skillPath: string;
} {
	origHome = process.env.HOME;
	origHomeOverrides = snapshotAndClearAgentHomeOverrides();
	tmpHome = copyFixtureToTmp(agent);
	process.env.HOME = tmpHome;
	process.env.HERMES_HOME = join(tmpHome, ".hermes");
	if (agent === "hermes") installHermesStub();
	seedAuthAndEnv(tmpHome, agent);

	const envPath = join(stateRootFor(tmpHome), "environments", `${agent}.json`);

	// Plant the bundled skill where the registry expects to find it.
	//
	// The MANAGED case models a current install, so it sits at the moved id (`iforge`) and is
	// reserved under that id -- `installReservedManagedSkill` requires `basename === id`, so a
	// harness that plants one name and reserves another is not modelling anything real. The
	// UNMANAGED case is the pre-ledger / custom-same-name world, which by definition lives at
	// the OLD name: those cases are about the directory earlier releases wrote.
	const skillId = options.managed === false ? "clawdi" : "iforge";
	let skillPath: string;
	if (agent === "openclaw") {
		const oid = process.env.OPENCLAW_AGENT_ID || "main";
		skillPath = join(tmpHome, ".openclaw", "agents", oid, "skills", skillId, "SKILL.md");
	} else {
		const home = `.${agent === "claude_code" ? "claude" : agent}`;
		skillPath = join(tmpHome, home, "skills", skillId, "SKILL.md");
	}
	mkdirSync(join(skillPath, ".."), { recursive: true });
	writeFileSync(skillPath, `---\nname: ${skillId}\ndescription: bundled\n---\n`);
	if (options.managed !== false) {
		reserveManagedSkill({
			targetDir: dirname(skillPath),
			id: skillId,
			version: 1,
			digest: "a".repeat(64),
			manager: "local-setup",
		});
	}

	return { envPath, skillPath };
}

afterEach(() => {
	if (origHome) process.env.HOME = origHome;
	else delete process.env.HOME;
	if (origPathSet) {
		if (origPath !== undefined) process.env.PATH = origPath;
		else delete process.env.PATH;
		origPath = undefined;
		origPathSet = false;
	}
	restoreAgentHomeOverrides(origHomeOverrides);
	origHomeOverrides = {};
	process.exitCode = 0;
	if (origIsTTY !== undefined) {
		Object.defineProperty(process.stdin, "isTTY", { value: origIsTTY, configurable: true });
		origIsTTY = undefined;
	}
	if (tmpHome) cleanupTmp(tmpHome);
});

/**
 * Force isInteractive() → false by clearing process.stdin.isTTY for the test
 * (matches CI). teardown.ts uses that gate to refuse interactive picker.
 */
function makeNonInteractive() {
	const desc = Object.getOwnPropertyDescriptor(process.stdin, "isTTY");
	origIsTTY = desc?.value;
	Object.defineProperty(process.stdin, "isTTY", { value: false, configurable: true });
}

/**
 * Capture what teardown PRINTS. `@clack/prompts` writes through `process.stdout`, so the
 * user-facing scope note is only observable here — and a scope note nobody can read is the same
 * as no scope note at all.
 */
function captureStdout(): { text: () => string; restore: () => void } {
	const chunks: string[] = [];
	const original = process.stdout.write.bind(process.stdout);
	process.stdout.write = ((chunk: unknown, ...rest: unknown[]) => {
		chunks.push(String(chunk));
		return (original as (...args: unknown[]) => boolean)(chunk, ...rest);
	}) as typeof process.stdout.write;
	return {
		text: () => chunks.join(""),
		restore: () => {
			process.stdout.write = original;
		},
	};
}

function installOpenClawStub(): string {
	if (!origPathSet) {
		origPath = process.env.PATH;
		origPathSet = true;
	}
	const stubDir = join(tmpHome, "bin");
	const argsPath = join(tmpHome, "openclaw-mcp-args");
	mkdirSync(stubDir, { recursive: true });
	const openclawPath = join(stubDir, "openclaw");
	writeFileSync(
		openclawPath,
		'#!/bin/sh\nif [ "$*" = "agents list --json" ]; then printf \'[{"id":"main","workspace":"%s/.openclaw/agents/main"}]\\n\' "$HOME"; exit 0; fi\nprintf "%s\\n" "$@" > "$HOME/openclaw-mcp-args"\nexit 0\n',
		{
			mode: 0o755,
		},
	);
	chmodSync(openclawPath, 0o755);
	process.env.PATH = `${stubDir}:${origPath ?? ""}`;
	return argsPath;
}

function installHermesStub(): void {
	if (!origPathSet) {
		origPath = process.env.PATH;
		origPathSet = true;
	}
	const stubDir = join(tmpHome, "bin");
	mkdirSync(stubDir, { recursive: true });
	const hermesPath = join(stubDir, "hermes");
	writeFileSync(
		hermesPath,
		`#!/bin/sh\nexec ${JSON.stringify(process.execPath)} ${JSON.stringify(HERMES_CONFIG_CLI_MOCK)} "$@"\n`,
		{ mode: 0o755 },
	);
	chmodSync(hermesPath, 0o755);
	process.env.PATH = `${stubDir}:${origPath ?? ""}`;
}

describe("teardown — basic round-trip per agent", () => {
	it("Claude Code: removes env file + bundled skill (--keep-mcp to skip claude exec)", async () => {
		const { envPath, skillPath } = setup("claude_code");
		expect(existsSync(envPath)).toBe(true);
		expect(existsSync(skillPath)).toBe(true);

		await teardown({ agent: "claude_code", yes: true, keepMcp: true });

		expect(existsSync(envPath)).toBe(false);
		expect(existsSync(skillPath)).toBe(false);
	});

	it("Codex: removes env file + bundled skill (--keep-mcp)", async () => {
		const { envPath, skillPath } = setup("codex");
		await teardown({ agent: "codex", yes: true, keepMcp: true });
		expect(existsSync(envPath)).toBe(false);
		expect(existsSync(skillPath)).toBe(false);
	});

	it("Hermes: removes env file + bundled skill (--keep-mcp)", async () => {
		const { envPath, skillPath } = setup("hermes");
		await teardown({ agent: "hermes", yes: true, keepMcp: true });
		expect(existsSync(envPath)).toBe(false);
		expect(existsSync(skillPath)).toBe(false);
	});

	it("OpenClaw: removes env file + bundled skill + MCP registration", async () => {
		const { envPath, skillPath } = setup("openclaw");
		const argsPath = installOpenClawStub();
		await teardown({ agent: "openclaw", yes: true });
		expect(existsSync(envPath)).toBe(false);
		expect(existsSync(skillPath)).toBe(false);
		expect(readFileSync(argsPath, "utf-8").trim().split("\n")).toEqual(["mcp", "unset", "iforge"]);
	});
});

describe("teardown — flag behavior", () => {
	it("--keep-skill leaves the bundled skill in place", async () => {
		const { envPath, skillPath } = setup("claude_code");
		const target = dirname(skillPath);
		await teardown({ agent: "claude_code", yes: true, keepMcp: true, keepSkill: true });
		expect(existsSync(envPath)).toBe(false);
		expect(existsSync(skillPath)).toBe(true);
		expect(managedSkillReservationState(target, "iforge")).toBe("unreserved");

		seedAuthAndEnv(tmpHome, "claude_code");
		writeFileSync(skillPath, "# User-edited retained Skill\n");
		await teardown({ agent: "claude_code", yes: true, keepMcp: true });
		expect(readFileSync(skillPath, "utf8")).toBe("# User-edited retained Skill\n");
		expect(managedSkillReservationState(target, "iforge")).toBe("unreserved");
	});

	it("leaves upstream clawdi's pre-ledger bundle untouched on direct teardown", async () => {
		// iforge names only (user directive, 2026-09-30): teardown neither adopts nor removes the
		// `clawdi` Skill directory. It is upstream clawdi's bundle, not a pre-rename iforge copy.
		const { skillPath } = setup("claude_code", { managed: false });
		const target = dirname(skillPath);
		rmSync(target, { recursive: true, force: true });
		cpSync(resolve(import.meta.dir, "../fixtures/legacy-local-clawdi"), target, {
			recursive: true,
		});

		await teardown({ agent: "claude_code", yes: true, keepMcp: true });

		expect(existsSync(target)).toBe(true);
		expect(managedSkillReservationState(target, "clawdi")).toBe("unreserved");
	});

	it("preserves an unproven custom same-name Skill on direct teardown", async () => {
		const { skillPath } = setup("claude_code", { managed: false });
		const target = dirname(skillPath);
		writeFileSync(skillPath, "---\nname: clawdi\ndescription: Custom Skill\n---\n");

		await teardown({ agent: "claude_code", yes: true, keepMcp: true });

		expect(existsSync(skillPath)).toBe(true);
		expect(managedSkillReservationState(target, "clawdi")).toBe("unreserved");
	});

	it("releases a stale reservation even when the managed target is already absent", async () => {
		const { skillPath } = setup("codex");
		const target = dirname(skillPath);
		rmSync(target, { recursive: true, force: true });
		expect(managedSkillReservationState(target, "iforge")).toBe("reserved");

		await teardown({ agent: "codex", yes: true, keepMcp: true });

		expect(managedSkillReservationState(target, "iforge")).toBe("unreserved");
	});

	it("--all tears down every registered agent", async () => {
		setup("hermes");
		// Plant a second registered agent — claude_code env + skill — by hand.
		seedAuthAndEnv(tmpHome, "claude_code");
		const claudeSkill = join(tmpHome, ".claude", "skills", "clawdi", "SKILL.md");
		mkdirSync(join(claudeSkill, ".."), { recursive: true });
		writeFileSync(claudeSkill, "x");

		await teardown({ all: true, yes: true, keepMcp: true });

		expect(existsSync(join(stateRootFor(tmpHome), "environments", "hermes.json"))).toBe(false);
		expect(existsSync(join(stateRootFor(tmpHome), "environments", "claude_code.json"))).toBe(false);
	});
});

describe("teardown — preflight errors set exitCode = 1", () => {
	it("--agent X but X not registered", async () => {
		setup("hermes");
		await teardown({ agent: "claude_code", yes: true });
		expect(process.exitCode).toBe(1);
	});

	it("invalid --agent value", async () => {
		setup("hermes");
		await teardown({ agent: "not_an_agent" as string, yes: true });
		expect(process.exitCode).toBe(1);
	});

	it("--agent and --all together", async () => {
		setup("hermes");
		await teardown({ agent: "hermes", all: true, yes: true });
		expect(process.exitCode).toBe(1);
	});

	it("no flags + non-TTY → refuse with exitCode 1", async () => {
		setup("hermes");
		makeNonInteractive();
		await teardown({ yes: true });
		expect(process.exitCode).toBe(1);
	});
});

describe("teardown — Hermes config.yaml MCP removal", () => {
	it("removes the iforge block when sibling entries come BEFORE it", async () => {
		setup("hermes");
		const configPath = join(tmpHome, ".hermes", "config.yaml");
		writeFileSync(
			configPath,
			[
				"server:",
				"  port: 8080",
				"mcp_servers:",
				"  other:",
				'    command: "other"',
				"  iforge:",
				'    command: "clawdi"',
				'    args: ["mcp"]',
				"",
			].join("\n"),
		);

		await teardown({ agent: "hermes", yes: true });

		const after = readFileSync(configPath, "utf-8");
		expect(after).not.toContain("iforge:");
		expect(parseYaml(after)).toMatchObject({
			mcp_servers: { other: { command: "other" } },
		});
	});

	it("removes the iforge block when sibling entries come AFTER it (regression: sibling at same indent must NOT be absorbed)", async () => {
		setup("hermes");
		const configPath = join(tmpHome, ".hermes", "config.yaml");
		writeFileSync(
			configPath,
			[
				"mcp_servers:",
				"  iforge:",
				'    command: "clawdi"',
				'    args: ["mcp"]',
				"  other:",
				'    command: "other"',
				'    args: ["serve"]',
				"",
			].join("\n"),
		);

		await teardown({ agent: "hermes", yes: true });

		const after = readFileSync(configPath, "utf-8");
		expect(after).not.toContain("iforge:");
		expect(parseYaml(after)).toMatchObject({
			mcp_servers: { other: { command: "other", args: ["serve"] } },
		});
	});

	it("logs gracefully when the iforge entry is absent", async () => {
		setup("hermes");
		const configPath = join(tmpHome, ".hermes", "config.yaml");
		writeFileSync(configPath, ["mcp_servers:", "  other:", '    command: "x"', ""].join("\n"));

		await teardown({ agent: "hermes", yes: true });

		const after = readFileSync(configPath, "utf-8");
		expect(parseYaml(after)).toMatchObject({
			mcp_servers: { other: { command: "x" } },
		});
	});

	it("removes stale iforge-mcp HTTP entries too", async () => {
		setup("hermes");
		const configPath = join(tmpHome, ".hermes", "config.yaml");
		writeFileSync(
			configPath,
			[
				"mcp_servers:",
				"  iforge-mcp:",
				"    url: https://backend.example.test/composio/mcp",
				"    headers:",
				"      Authorization: placeholder",
				"  other:",
				'    command: "other"',
				"",
			].join("\n"),
		);

		await teardown({ agent: "hermes", yes: true });

		const after = readFileSync(configPath, "utf-8");
		expect(after).not.toContain("iforge-mcp:");
		expect(after).not.toContain("https://backend.example.test/composio/mcp");
		expect(parseYaml(after)).toMatchObject({
			mcp_servers: { other: { command: "other" } },
		});
	});

	it("leaves an upstream clawdi block value-identical while removing its own", async () => {
		// The defect this fixes (P33-07 / T-03.3-43): teardown used to remove the `clawdi` and
		// `clawdi-mcp` keys by name. On a machine carrying BOTH products those are upstream's
		// blocks in a config file neither product owns exclusively, so `iforge teardown` silently
		// deleted a working upstream install's MCP registration.
		setup("hermes");
		const configPath = join(tmpHome, ".hermes", "config.yaml");
		const foreignBlock = [
			"  clawdi:",
			'    command: "clawdi"',
			'    args: ["mcp"]',
			"  clawdi-mcp:",
			"    url: https://upstream.example.test/composio/mcp",
			"    headers:",
			"      Authorization: upstream-token",
		].join("\n");
		writeFileSync(
			configPath,
			[
				"mcp_servers:",
				foreignBlock,
				"  iforge:",
				'    command: "iforge"',
				'    args: ["mcp"]',
				"  iforge-mcp:",
				"    url: https://backend.example.test/composio/mcp",
				"",
			].join("\n"),
		);

		const printed = captureStdout();
		try {
			await teardown({ agent: "hermes", yes: true });
		} finally {
			printed.restore();
		}

		const after = readFileSync(configPath, "utf-8");
		// Value-identical, and complete. Since upstream #1114 removal goes through Hermes' own
		// `hermes config` writer, which re-serialises the whole file, so byte identity is no longer
		// something either product can promise; what this product still guarantees is that it never
		// changes or removes another product's keys. `toEqual` on the whole map proves exactly the
		// upstream blocks remain.
		expect(foreignBlock).toContain("  clawdi-mcp:");
		expect(parseYaml(after).mcp_servers).toEqual({
			clawdi: { command: "clawdi", args: ["mcp"] },
			"clawdi-mcp": {
				url: "https://upstream.example.test/composio/mcp",
				headers: { Authorization: "upstream-token" },
			},
		});
		expect(after).not.toContain("https://backend.example.test/composio/mcp");
		// UI-SPEC S2: the scope is stated out loud, because silence is indistinguishable from the
		// defect having never been fixed.
		expect(printed.text()).toContain(
			"Removing iforge entries only. Any clawdi entries in this file are left alone.",
		);
	});
});
