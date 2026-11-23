import { afterAll, afterEach, beforeEach, describe, expect, it } from "bun:test";
import {
	chmodSync,
	cpSync,
	existsSync,
	mkdirSync,
	mkdtempSync,
	readFileSync,
	rmSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { parse as parseYaml } from "yaml";
import { setup } from "../../src/commands/setup";
import { stateRootFor } from "../../src/lib/state-root";
import {
	managedSkillReservationState,
	releaseManagedSkill,
} from "../../src/runtime/managed-skill-reservation";
// Unit names and the launchd label are DERIVED from the installer that WRITES them (P33-11),
// so a future identity move edits one place rather than every fixture that names a unit.
import {
	unitFileName as daemonUnitFileName,
	LAUNCHD_DAEMON_LABEL,
} from "../../src/serve/installer";
import {
	type AgentHomeOverrideSnapshot,
	jsonResponse,
	mockFetch,
	restoreAgentHomeOverrides,
	snapshotAndClearAgentHomeOverrides,
} from "./helpers";

const tmpRoot = mkdtempSync(join(tmpdir(), "clawdi-setup-test-"));
const HERMES_CONFIG_CLI_MOCK = resolve(
	import.meta.dir,
	"../../src/test-support/hermes-config-cli-mock.ts",
);
const HERMES_TEST_MCP_TOKEN_REF = `\${HERMES_TEST_MCP_TOKEN}`;
const ENV_KEYS = [
	"CI",
	"HOME",
	"PATH",
	"CLAWDI_HOME",
	"CLAWDI_API_URL",
	"CLAWDI_AUTH_TOKEN",
	"CLAWDI_ENVIRONMENT_ID",
	"CLAWDI_STATE_DIR",
	"CLAWDI_SERVICE_STATE_DIR",
	"CLAWDI_RUNTIME_MODE",
	"CLAWDI_SERVE_MODE",
	"CLAWDI_SERVE_DEBUG",
	"HERMES_TEST_MCP_TOKEN",
	"PI_CODING_AGENT_DIR",
	"XDG_DATA_HOME",
] as const;

let envSnapshot: Partial<Record<(typeof ENV_KEYS)[number], string>> = {};
let agentHomeSnapshot: AgentHomeOverrideSnapshot = {};
let restoreFetch: (() => void) | null = null;
let restoreConsole: (() => void) | null = null;
let originalArgv1: string | undefined;
let home = "";
let consoleOutput: string[] = [];

afterAll(() => {
	rmSync(tmpRoot, { recursive: true, force: true });
});

beforeEach(() => {
	home = mkdtempSync(join(tmpRoot, "case-"));
	envSnapshot = {};
	for (const key of ENV_KEYS) {
		const value = process.env[key];
		if (value !== undefined) envSnapshot[key] = value;
		delete process.env[key];
	}
	agentHomeSnapshot = snapshotAndClearAgentHomeOverrides();

	process.env.CI = "1";
	process.env.HOME = home;
	process.env.CLAWDI_API_URL = "https://api.test";

	originalArgv1 = process.argv[1];
	const fakeEntry = join(home, "clawdi-bin");
	writeExecutable(fakeEntry, "#!/bin/sh\nexit 0\n");
	process.argv[1] = fakeEntry;

	const stubDir = join(home, "bin");
	mkdirSync(stubDir, { recursive: true });
	writeExecutable(join(stubDir, "codex"), "#!/bin/sh\nexit 0\n");
	writeExecutable(
		join(stubDir, "hermes"),
		`#!/bin/sh\nexec ${JSON.stringify(process.execPath)} ${JSON.stringify(HERMES_CONFIG_CLI_MOCK)} "$@"\n`,
	);
	writeExecutable(
		join(stubDir, "openclaw"),
		'#!/bin/sh\nif [ "$*" = "agents list --json" ]; then printf \'[{"id":"main","workspace":"%s/.openclaw/agents/main"}]\\n\' "$HOME"; exit 0; fi\nprintf "%s\\n" "$@" > "$HOME/openclaw-mcp-args"\nexit 0\n',
	);
	writeExecutable(join(stubDir, "systemctl"), "#!/bin/sh\nexit 0\n");
	writeExecutable(join(stubDir, "launchctl"), "#!/bin/sh\nexit 0\n");
	process.env.PATH = `${stubDir}:${envSnapshot.PATH ?? ""}`;

	seedAuth();
	const originalLog = console.log;
	const originalError = console.error;
	consoleOutput = [];
	console.log = (...args: unknown[]) => {
		consoleOutput.push(args.map(String).join(" "));
	};
	console.error = (...args: unknown[]) => {
		consoleOutput.push(args.map(String).join(" "));
	};
	restoreConsole = () => {
		console.log = originalLog;
		console.error = originalError;
	};
	process.exitCode = undefined;
});

afterEach(() => {
	restoreFetch?.();
	restoreFetch = null;
	restoreConsole?.();
	restoreConsole = null;
	process.argv[1] = originalArgv1 ?? "";
	process.exitCode = undefined;

	for (const key of ENV_KEYS) delete process.env[key];
	for (const [key, value] of Object.entries(envSnapshot)) {
		if (value !== undefined) process.env[key as (typeof ENV_KEYS)[number]] = value;
	}
	restoreAgentHomeOverrides(agentHomeSnapshot);
	rmSync(home, { recursive: true, force: true });
});

describe("setup daemon install", () => {
	it("registers MCP when the idempotency probe fails", async () => {
		installEnvironmentMock("env-codex-probe");
		writeExecutable(
			join(home, "bin", "codex"),
			'#!/bin/sh\nif [ "$*" = "mcp get iforge --json" ]; then exit 9; fi\nif [ "$*" = "mcp add iforge -- iforge mcp" ]; then printf "%s\\n" "$*" > "$HOME/codex-mcp-register"; fi\nexit 0\n',
		);

		await setup({ agent: "codex", yes: true, daemon: false });

		expect(readFileSync(join(home, "codex-mcp-register"), "utf-8").trim()).toBe(
			"mcp add iforge -- iforge mcp",
		);
		expect(consoleOutput.some((line) => line.includes("Could not auto-register"))).toBe(false);
	});

	it("registers Pi as sessions-only without installing Skill or MCP state", async () => {
		const { captured } = installEnvironmentMock("env-pi");
		process.env.PI_CODING_AGENT_DIR = join(home, "pi-agent");

		await setup({ agent: "pi", yes: true, daemon: false });

		const registration = captured.find((req) => req.method === "POST" && req.path === "/v1/agents");
		expect(registration?.body).toMatchObject({
			agent_type: "pi",
			adapter_modules: ["sessions"],
		});
		expect(existsSync(join(home, "pi-agent", "skills"))).toBe(false);
		expect(existsSync(join(home, "pi-agent", "mcp.json"))).toBe(false);
	});

	it("registers OpenCode as sessions-only without installing Skill or MCP state", async () => {
		const { captured } = installEnvironmentMock("env-opencode");
		process.env.XDG_DATA_HOME = join(home, "xdg-data");

		await setup({ agent: "opencode", yes: true, daemon: false });

		const registration = captured.find((req) => req.method === "POST" && req.path === "/v1/agents");
		expect(registration?.body).toMatchObject({
			agent_type: "opencode",
			adapter_modules: ["sessions"],
		});
		const openCodeHome = join(home, "xdg-data", "opencode");
		expect(existsSync(join(openCodeHome, "skills"))).toBe(false);
		expect(existsSync(join(openCodeHome, "mcp.json"))).toBe(false);
	});

	it("defaults to installing one daemon unit for all registered agents", async () => {
		seedRegisteredAgent("claude_code", "env-claude");
		seedDaemonUnit("claude_code");
		const { captured } = installEnvironmentMock("env-codex");

		await setup({ agent: "codex", yes: true });

		expect(
			captured.some(
				(req) =>
					req.method === "POST" &&
					req.path === "/v1/agents" &&
					(req.body as { agent_type?: string } | undefined)?.agent_type === "codex",
			),
		).toBe(true);
		expectDaemonRunSingleton();
		expect(daemonUnitExists("claude_code")).toBe(false);
		expect(daemonUnitExists("codex")).toBe(false);
	});

	it("binds the explicitly selected official OpenClaw workspace", async () => {
		installEnvironmentMock("env-openclaw");
		const workspace = join(home, ".openclaw", "agents", "main");
		mkdirSync(workspace, { recursive: true });
		await setup({ agent: "openclaw", yes: true, daemon: false, vaultNativeAgent: "main" });
		const registration = JSON.parse(
			readFileSync(join(stateRootFor(home), "environments", "openclaw.json"), "utf8"),
		);
		expect(registration.vaultWorkspace).toMatchObject({ path: workspace, nativeAgentId: "main" });
	});

	it("only binds a Vault workspace when explicitly selected", async () => {
		installEnvironmentMock("env-pi");
		await setup({ agent: "pi", yes: true, daemon: false });
		const path = join(stateRootFor(home), "environments", "pi.json");
		expect(JSON.parse(readFileSync(path, "utf8")).vaultWorkspace).toBeUndefined();
		const workspace = join(home, "project");
		mkdirSync(workspace);
		await setup({ agent: "pi", yes: true, daemon: false, vaultWorkspace: workspace });
		expect(JSON.parse(readFileSync(path, "utf8")).vaultWorkspace.path).toBe(workspace);
		await setup({ agent: "pi", yes: true, daemon: false });
		expect(JSON.parse(readFileSync(path, "utf8")).vaultWorkspace.path).toBe(workspace);
		await expect(setup({ yes: true, vaultWorkspace: workspace })).rejects.toThrow(
			"require --agent",
		);
	});

	it("honors --no-daemon while still registering the requested agent", async () => {
		installEnvironmentMock("env-codex");

		await setup({ agent: "codex", yes: true, daemon: false });

		const registrationPath = join(stateRootFor(home), "environments", "codex.json");
		expect(existsSync(registrationPath)).toBe(true);
		expect(JSON.parse(readFileSync(registrationPath, "utf8"))).toMatchObject({
			id: "env-codex",
			agentType: "codex",
			userId: "u1",
		});
		expect(daemonUnitExists("daemon")).toBe(false);
		expect(daemonUnitExists("codex")).toBe(false);
	});

	it("does not install a daemon when environment registration fails", async () => {
		installFailingEnvironmentMock();

		await setup({ agent: "codex", yes: true });

		expect(process.exitCode).toBe(1);
		process.exitCode = 0;
		expect(existsSync(join(stateRootFor(home), "environments", "codex.json"))).toBe(false);
		expect(daemonUnitExists("daemon")).toBe(false);
		expect(daemonUnitExists("codex")).toBe(false);
	});

	it("installs the bundled Skill with explicit local-setup ownership", async () => {
		installEnvironmentMock("env-codex");

		await setup({ agent: "codex", yes: true, daemon: false });

		const target = join(home, ".codex", "skills", "iforge");
		expect(existsSync(join(target, "SKILL.md"))).toBe(true);
		expect(managedSkillReservationState(target, "iforge")).toBe("reserved");
	});

	it("reconciles the bundled Skill as an exact directory replacement", async () => {
		installEnvironmentMock("env-codex");
		await setup({ agent: "codex", yes: true, daemon: false });
		const target = join(home, ".codex", "skills", "iforge");
		writeFileSync(join(target, "removed-by-upgrade.txt"), "stale\n");

		await setup({ agent: "codex", yes: true, daemon: false });

		expect(existsSync(join(target, "removed-by-upgrade.txt"))).toBe(false);
		expect(managedSkillReservationState(target, "iforge")).toBe("reserved");
	});

	it("leaves upstream clawdi's pre-ledger bundle in place when installing the iforge Skill", async () => {
		// iforge names only (user directive, 2026-09-30): the `clawdi` directory is upstream
		// clawdi's bundle, not a pre-rename iforge copy, so setup neither adopts nor removes it.
		const upstreamTarget = join(home, ".codex", "skills", "clawdi");
		cpSync(resolve(import.meta.dir, "../fixtures/legacy-local-clawdi"), upstreamTarget, {
			recursive: true,
		});
		installEnvironmentMock("env-codex");

		await setup({ agent: "codex", yes: true, daemon: false });

		expect(existsSync(upstreamTarget)).toBe(true);
		expect(managedSkillReservationState(upstreamTarget, "clawdi")).toBe("unreserved");
		const target = join(home, ".codex", "skills", "iforge");
		expect(managedSkillReservationState(target, "iforge")).toBe("reserved");
	});

	it("leaves an unadoptable same-name directory alone when installing the moved Skill", async () => {
		// Under coexistence this is upstream `clawdi`'s own bundled Skill: same path, content we
		// have never shipped, so no legacy digest matches and no reservation is ours. Setup must
		// install beside it and leave it byte-identical (T-03.3-44).
		const foreign = join(home, ".codex", "skills", "clawdi");
		mkdirSync(foreign, { recursive: true });
		const foreignBytes = "---\nname: clawdi\ndescription: upstream bundled skill\n---\n";
		writeFileSync(join(foreign, "SKILL.md"), foreignBytes);
		installEnvironmentMock("env-codex");

		await setup({ agent: "codex", yes: true, daemon: false });

		expect(readFileSync(join(foreign, "SKILL.md"), "utf-8")).toBe(foreignBytes);
		expect(managedSkillReservationState(join(home, ".codex", "skills", "iforge"), "iforge")).toBe(
			"reserved",
		);
	});

	it("refuses to replace a custom pre-ledger same-name Skill", async () => {
		const target = join(home, ".codex", "skills", "clawdi");
		mkdirSync(target, { recursive: true });
		writeFileSync(join(target, "SKILL.md"), "# User-owned Clawdi\n");
		installEnvironmentMock("env-codex");

		await setup({ agent: "codex", yes: true, daemon: false });

		expect(readFileSync(join(target, "SKILL.md"), "utf-8")).toBe("# User-owned Clawdi\n");
		expect(managedSkillReservationState(target, "clawdi")).toBe("unreserved");
	});

	it("does not reclaim a future user clawdi target after migration and release", async () => {
		installEnvironmentMock("env-codex");
		await setup({ agent: "codex", yes: true, daemon: false });
		const target = join(home, ".codex", "skills", "clawdi");
		releaseManagedSkill({
			targetDir: target,
			id: "clawdi",
			manager: "local-setup",
			removeTarget: () => rmSync(target, { recursive: true, force: true }),
		});
		mkdirSync(target, { recursive: true });
		writeFileSync(join(target, "SKILL.md"), "# Future user Clawdi\n");

		await setup({ agent: "codex", yes: true, daemon: false });

		expect(readFileSync(join(target, "SKILL.md"), "utf-8")).toBe("# Future user Clawdi\n");
		expect(managedSkillReservationState(target, "clawdi")).toBe("unreserved");
	});

	it("sets a failing exit code without printing install success when service activation fails", async () => {
		if (process.platform !== "linux") return;
		installEnvironmentMock("env-codex");
		writeExecutable(join(home, "bin", "systemctl"), "#!/bin/sh\nexit 1\n");

		await setup({ agent: "codex", yes: true });

		expect(process.exitCode).toBe(1);
		process.exitCode = 0;
		expect(daemonUnitExists("daemon")).toBe(true);
		const output = consoleOutput.join("\n");
		expect(output).toContain("systemctl activation failed");
		expect(output).toContain("systemctl --user daemon-reload");
		expect(output).not.toContain("Singleton daemon installed");
	});
});

describe("setup Hermes MCP registration", () => {
	it("replaces cloud HTTP iforge-mcp with canonical stdio iforge", async () => {
		const configPath = prepareHermesConfig(
			[
				"model:",
				"  provider: custom",
				"mcp_servers:",
				"  iforge-mcp:",
				"    url: https://backend.example.test/composio/mcp",
				"    headers:",
				"      Authorization: placeholder",
				"",
			].join("\n"),
		);
		installEnvironmentMock("env-hermes-test");

		await setup({ agent: "hermes", yes: true, daemon: false });

		const after = readFileSync(configPath, "utf-8");
		expect(parseYaml(after)).toMatchObject({
			mcp_servers: { iforge: { command: "iforge", args: ["mcp"] } },
		});
		expect(after).not.toContain("iforge-mcp:");
		expect(after).not.toContain("https://backend.example.test/composio/mcp");
		expect(after).not.toContain("Authorization: placeholder");
	});

	it("normalizes a mixed iforge block back to stdio-only", async () => {
		const configPath = prepareHermesConfig(
			[
				"mcp_servers:",
				"  iforge:",
				'    command: "clawdi"',
				'    args: ["mcp"]',
				"    url: https://backend.example.test/composio/mcp",
				"    headers:",
				"      Authorization: placeholder",
				"  other:",
				'    command: "other"',
				"",
			].join("\n"),
		);
		installEnvironmentMock("env-hermes-test");

		await setup({ agent: "hermes", yes: true, daemon: false });

		const after = readFileSync(configPath, "utf-8");
		expect(parseYaml(after)).toMatchObject({
			mcp_servers: {
				iforge: { command: "iforge", args: ["mcp"] },
				other: { command: "other" },
			},
		});
		expect(after).not.toContain("https://backend.example.test/composio/mcp");
		expect(after).not.toContain("Authorization: placeholder");
	});

	it("removes duplicate HTTP sibling when stdio iforge is already present", async () => {
		const configPath = prepareHermesConfig(
			[
				"mcp_servers:",
				"  iforge-mcp:",
				"    url: https://backend.example.test/composio/mcp",
				"    headers:",
				"      Authorization: placeholder",
				"  iforge:",
				'    command: "clawdi"',
				'    args: ["mcp"]',
				"  other:",
				'    command: "other"',
				"",
			].join("\n"),
		);
		installEnvironmentMock("env-hermes-test");

		await setup({ agent: "hermes", yes: true, daemon: false });

		const after = readFileSync(configPath, "utf-8");
		expect(parseYaml(after)).toMatchObject({
			mcp_servers: {
				iforge: { command: "iforge", args: ["mcp"] },
				other: { command: "other" },
			},
		});
		expect(after).not.toContain("iforge-mcp:");
		expect(after).not.toContain("https://backend.example.test/composio/mcp");
	});

	it("keeps installing legacy stdio for local Hermes configs without HTTP MCP", async () => {
		const configPath = prepareHermesConfig(["model:", "  provider: custom", ""].join("\n"));
		installEnvironmentMock("env-hermes-test");

		await setup({ agent: "hermes", yes: true, daemon: false });

		const after = readFileSync(configPath, "utf-8");
		expect(parseYaml(after)).toMatchObject({
			mcp_servers: { iforge: { command: "iforge", args: ["mcp"] } },
		});
	});

	it("preserves env references in unrelated Hermes MCP servers", async () => {
		process.env.HERMES_TEST_MCP_TOKEN = "resolved-secret-must-not-be-written";
		const configPath = prepareHermesConfig(
			[
				"mcp_servers:",
				"  user.server:",
				"    url: https://mcp.example.test",
				"    headers:",
				`      Authorization: "Bearer ${HERMES_TEST_MCP_TOKEN_REF}"`,
				"",
			].join("\n"),
		);
		installEnvironmentMock("env-hermes-test");

		await setup({ agent: "hermes", yes: true, daemon: false });

		const after = readFileSync(configPath, "utf-8");
		expect(parseYaml(after)).toMatchObject({
			mcp_servers: {
				"user.server": {
					headers: { Authorization: `Bearer ${HERMES_TEST_MCP_TOKEN_REF}` },
				},
				iforge: { command: "iforge", args: ["mcp"] },
			},
		});
		expect(after).not.toContain("resolved-secret-must-not-be-written");
	});

	it("leaves a foreign clawdi block value-identical while normalizing its own", async () => {
		// Coexistence (P33-01): upstream `clawdi` writes its own blocks into the SAME
		// `~/.hermes/config.yaml`. Normalisation removes this product's own stale forms and
		// nothing else, so upstream's entries survive a setup run untouched -- the writer-side
		// half of the rule teardown enforces on removal (T-03.3-43).
		const foreignBlock = [
			"  clawdi:",
			'    command: "clawdi"',
			'    args: ["mcp"]',
			"  clawdi-mcp:",
			"    url: https://upstream.example.test/mcp",
			"    headers:",
			"      Authorization: upstream-token",
		].join("\n");
		const configPath = prepareHermesConfig(
			[
				"mcp_servers:",
				foreignBlock,
				"  iforge-mcp:",
				"    url: https://backend.example.test/composio/mcp",
				"",
			].join("\n"),
		);
		installEnvironmentMock("env-hermes-test");

		await setup({ agent: "hermes", yes: true, daemon: false });

		const after = readFileSync(configPath, "utf-8");
		// Value-identical, and complete. Since upstream #1114 registration goes through Hermes' own
		// `hermes config` writer, which re-serialises the whole file, so byte identity is no longer
		// something either product can promise; what this product still guarantees is that it never
		// adds, changes or removes another product's keys. `toEqual` on the whole map (not
		// `toMatchObject`) is what proves nothing else was touched.
		expect(parseYaml(after).mcp_servers).toEqual({
			clawdi: { command: "clawdi", args: ["mcp"] },
			"clawdi-mcp": {
				url: "https://upstream.example.test/mcp",
				headers: { Authorization: "upstream-token" },
			},
			iforge: { command: "iforge", args: ["mcp"] },
		});
		expect(after).not.toContain("https://backend.example.test/composio/mcp");
	});
});

describe("setup OpenClaw MCP registration", () => {
	it("registers canonical stdio iforge through OpenClaw MCP config", async () => {
		installEnvironmentMock("env-openclaw-test");

		await setup({ agent: "openclaw", yes: true, daemon: false });

		const args = readFileSync(join(home, "openclaw-mcp-args"), "utf-8").trim().split("\n");
		expect(args).toEqual(["mcp", "set", "iforge", '{"command":"iforge","args":["mcp"]}']);
	});
});

describe("setup MCP registration names the shipped executable", () => {
	// `@iforge/cli` ships exactly one binary -- `iforge` (this package's `bin`). Any adapter that
	// registers `clawdi` as the *command* writes an MCP entry that cannot start, and every one of
	// these adapters reports success before that failure is observable.
	//
	// The server *name* moved too, in plan 03.3-10. D-07 had kept it as the key third-party
	// config already held; P33-07 retired that, because upstream `clawdi` claims the same key on
	// the same machine and one key cannot hold two servers, so the `clawdi` key is never touched.
	it("registers iforge, not clawdi, as the Codex MCP command", async () => {
		const recorded = recordAgentInvocations("codex");
		installEnvironmentMock("env-codex-test");

		await setup({ agent: "codex", yes: true, daemon: false });

		const add = recorded().find((line) => line.startsWith("mcp add"));
		expect(add).toBe("mcp add iforge -- iforge mcp");
	});

	it("registers iforge, not clawdi, as the Claude Code MCP command", async () => {
		const recorded = recordAgentInvocations("claude");
		installEnvironmentMock("env-claude-test");

		await setup({ agent: "claude_code", yes: true, daemon: false });

		const add = recorded().find((line) => line.startsWith("mcp add-json"));
		expect(add).toContain('"command":"iforge"');
		expect(add).not.toContain('"command":"clawdi"');
	});

	it("prints a manual fallback that would actually work when Codex registration fails", async () => {
		writeExecutable(join(home, "bin", "codex"), "#!/bin/sh\nexit 1\n");
		installEnvironmentMock("env-codex-fallback");

		await setup({ agent: "codex", yes: true, daemon: false });

		const hint = consoleOutput.find((line) => line.includes("codex mcp add"));
		// The printed form must name the same key the executed form used, or it walks the user
		// into registering the wrong server by hand.
		expect(hint).toContain("codex mcp add iforge -- iforge mcp");
		expect(hint).not.toContain("codex mcp add clawdi");
	});
});

describe("setup repairs a stale MCP registration", () => {
	// A name-keyed short-circuit reports success for an entry still pointing at a binary that no
	// longer exists. Every assertion here is about the registered *command*, because that is what
	// distinguishes a healthy entry from a stale one -- and, since the server name moved in plan
	// 03.3-10, what distinguishes OUR entry from an upstream `clawdi` install's.
	it("re-registers Claude Code when the stored command is the pre-rename executable", async () => {
		const recorded = stubAgentWithRegisteredCommand("claude", "clawdi");
		installEnvironmentMock("env-claude-stale");

		await setup({ agent: "claude_code", yes: true, daemon: false });

		expect(recorded().some((line) => line.startsWith("mcp remove iforge"))).toBe(true);
		expect(recorded().find((line) => line.startsWith("mcp add-json"))).toContain(
			'"command":"iforge"',
		);
		expect(consoleOutput.join("\n")).not.toContain("already registered");
	});

	it("re-registers Codex when the stored command is the pre-rename executable", async () => {
		const recorded = stubAgentWithRegisteredCommand("codex", "clawdi");
		installEnvironmentMock("env-codex-stale");

		await setup({ agent: "codex", yes: true, daemon: false });

		expect(recorded().some((line) => line.startsWith("mcp remove iforge"))).toBe(true);
		expect(recorded()).toContain("mcp add iforge -- iforge mcp");
	});

	it("never removes a clawdi entry, even one whose command is iforge", async () => {
		// iforge names only (user directive, 2026-09-30): the `clawdi` key is upstream clawdi's,
		// and there is no pre-rename iforge registration to retire.
		const recorded = stubAgentWithRegisteredCommand("claude", "iforge", "clawdi");
		installEnvironmentMock("env-claude-upstream-key");

		await setup({ agent: "claude_code", yes: true, daemon: false });

		expect(recorded().some((line) => line.startsWith("mcp remove clawdi"))).toBe(false);
		expect(recorded().find((line) => line.startsWith("mcp add-json"))).toContain(
			'"command":"iforge"',
		);
	});

	it("leaves a foreign clawdi entry registered when its command is not ours", async () => {
		// Under coexistence the `clawdi` key belongs to an upstream install pointing at its own
		// executable. Removing it would be the denial-of-service P33-07 fixes in teardown, so the
		// command probe -- not the key -- decides (T-03.3-43).
		const recorded = stubAgentWithRegisteredCommand("claude", "clawdi", "clawdi");
		installEnvironmentMock("env-claude-foreign");

		await setup({ agent: "claude_code", yes: true, daemon: false });

		expect(recorded().some((line) => line.startsWith("mcp remove clawdi"))).toBe(false);
		expect(recorded().find((line) => line.startsWith("mcp add-json"))).toContain(
			'"command":"iforge"',
		);
	});

	it("leaves an already-correct Claude Code registration untouched", async () => {
		const recorded = stubAgentWithRegisteredCommand("claude", "iforge");
		installEnvironmentMock("env-claude-fresh");

		await setup({ agent: "claude_code", yes: true, daemon: false });

		expect(consoleOutput.join("\n")).toContain("already registered");
		expect(recorded().some((line) => line.startsWith("mcp add-json"))).toBe(false);
		expect(recorded().some((line) => line.startsWith("mcp remove"))).toBe(false);
	});

	it("re-registers rather than trusting a probe it cannot read", async () => {
		// An agent build without `mcp get --json` must not be read as "healthy". Unknown has to
		// mean re-register, or the stale entry survives on exactly the machines being upgraded.
		const recorded = recordAgentInvocations("claude");
		installEnvironmentMock("env-claude-unknown");

		await setup({ agent: "claude_code", yes: true, daemon: false });

		expect(recorded().find((line) => line.startsWith("mcp add-json"))).toContain(
			'"command":"iforge"',
		);
		expect(consoleOutput.join("\n")).not.toContain("already registered");
	});
});

function installEnvironmentMock(envId: string) {
	const mock = mockFetch([
		{
			method: "POST",
			path: "/v1/agents",
			response: () => jsonResponse({ id: envId }),
		},
	]);
	restoreFetch = mock.restore;
	return mock;
}

function installFailingEnvironmentMock() {
	const mock = mockFetch([
		{
			method: "POST",
			path: "/v1/agents",
			response: () => jsonResponse({ error: "boom" }, 500),
		},
	]);
	restoreFetch = mock.restore;
	return mock;
}

function prepareHermesConfig(configYaml: string): string {
	const hermesHome = join(home, ".hermes");
	process.env.HERMES_HOME = hermesHome;
	const configPath = join(hermesHome, "config.yaml");
	mkdirSync(hermesHome, { recursive: true });
	writeFileSync(configPath, configYaml);
	return configPath;
}

function seedAuth(): void {
	const stateDir = stateRootFor(home);
	mkdirSync(stateDir, { recursive: true });
	writeFileSync(
		join(stateDir, "auth.json"),
		`${JSON.stringify({
			apiKey: "test-key",
			userId: "u1",
			email: "u@example.test",
			endpointBinding: { version: 1, cloudApiOrigin: "https://api.test" },
		})}\n`,
		{ mode: 0o600 },
	);
}

function seedRegisteredAgent(agent: string, envId: string): void {
	const envDir = join(stateRootFor(home), "environments");
	mkdirSync(envDir, { recursive: true });
	writeFileSync(
		join(envDir, `${agent}.json`),
		`${JSON.stringify({ id: envId, agentType: agent })}\n`,
		{ mode: 0o600 },
	);
}

function daemonUnitPath(agent: string): string {
	if (agent === "daemon") {
		if (process.platform === "darwin") {
			return join(home, "Library", "LaunchAgents", `${LAUNCHD_DAEMON_LABEL}.plist`);
		}
		return join(home, ".config", "systemd", "user", daemonUnitFileName());
	}
	if (process.platform === "darwin") {
		return join(home, "Library", "LaunchAgents", `${LAUNCHD_DAEMON_LABEL}.${agent}.plist`);
	}
	return join(home, ".config", "systemd", "user", daemonUnitFileName(agent));
}

function daemonUnitExists(agent: string): boolean {
	return existsSync(daemonUnitPath(agent));
}

function seedDaemonUnit(agent: string): void {
	const path = daemonUnitPath(agent);
	mkdirSync(dirname(path), { recursive: true });
	writeFileSync(path, "legacy daemon unit\n");
}

function readDaemonUnit(agent: string): string {
	return readFileSync(daemonUnitPath(agent), "utf-8");
}

function expectDaemonRunSingleton(): void {
	const content = readDaemonUnit("daemon");
	if (process.platform === "darwin") {
		expect(content).toContain("<string>daemon</string>");
		expect(content).toContain("<string>run</string>");
		expect(content).not.toContain("<string>--all</string>");
		expect(content).not.toContain("<string>--agent</string>");
		return;
	}
	expect(content).toContain("daemon run");
	expect(content).not.toContain("--all");
	expect(content).not.toContain("--agent");
}

function writeExecutable(path: string, content: string): void {
	writeFileSync(path, content, { mode: 0o755 });
	chmodSync(path, 0o755);
}

/**
 * Replace a stubbed agent binary with one that appends each invocation's argument line to a log,
 * and return a reader for it. Appending rather than overwriting keeps the idempotency probe and
 * the registration call both visible, which is what the stale-command assertions inspect.
 */
function recordAgentInvocations(binary: string): () => string[] {
	const log = join(home, `${binary}-invocations`);
	writeExecutable(
		join(home, "bin", binary),
		`#!/bin/sh\nprintf '%s\\n' "$*" >> "${log}"\nexit 0\n`,
	);
	return () => (existsSync(log) ? readFileSync(log, "utf-8").trim().split("\n") : []);
}

/**
 * A stubbed agent that answers `mcp get <name> --json` with a registration whose command is
 * `command`, and records every other invocation. This is what distinguishes a repair from a
 * no-op: the probe has to be answerable for the "already registered" path to be reachable at all.
 */
/**
 * Stub an agent CLI that reports `command` registered under `serverName`, and nothing under any
 * other name. `serverName` is explicit so a case can prove the `clawdi` key is left alone.
 */
function stubAgentWithRegisteredCommand(
	binary: string,
	command: string,
	serverName = "iforge",
): () => string[] {
	const log = join(home, `${binary}-invocations`);
	writeExecutable(
		join(home, "bin", binary),
		`${[
			"#!/bin/sh",
			`printf '%s\\n' "$*" >> "${log}"`,
			'case "$*" in',
			`  "mcp get ${serverName} --json") printf '{"command":"${command}","args":["mcp"]}\\n'; exit 0;;`,
			'  "mcp get "*) exit 1;;',
			"esac",
			"exit 0",
		].join("\n")}\n`,
	);
	return () => (existsSync(log) ? readFileSync(log, "utf-8").trim().split("\n") : []);
}
