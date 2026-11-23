import { afterEach, describe, expect, test } from "bun:test";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
	chmodSync,
	chownSync,
	cpSync,
	existsSync,
	mkdirSync,
	mkdtempSync,
	readdirSync,
	readFileSync,
	readlinkSync,
	rmSync,
	statSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parse as parseYaml } from "yaml";
import { z } from "zod";
import { commitRuntimeAppliedState } from "../commands/runtime";
import { isReservedSkillArchivePath } from "../lib/tar";
import { installHermesNativeFixture } from "../test-support/hermes-native-fixture";
import { writeFakeOpenClawConfigMutationSdk } from "../test-support/openclaw-config-mutation";
import {
	readRuntimeAppliedState,
	runtimeContentSha256,
	writeRuntimeAppliedState,
} from "./applied-state";
import { applyRuntimeBundleChannelsToManifestLoad } from "./channels";
import {
	COMPANION_KEY_PATTERN,
	type CompanionRegistryEntry,
	KNOWN_COMPANION_KINDS,
	RESERVED_COMPANION_UID_BLOCK,
	recognisedCompanionKinds,
} from "./companion-registry";
import {
	captureComponentActivations,
	observeComponents,
	persistComponentActivations,
} from "./component-observation";
import { gcFileBrowserCompanionCandidates } from "./file-browser-companion";
import {
	assertCompanionServiceAccount,
	assertCompanionServiceGroups,
	COMPANION_SERVICE_ACCOUNT_EXEMPTIONS,
	type CompanionServiceIsolation,
	companionServiceNames,
	ensureExclusiveCompanionServiceIdentity,
	resolveCompanionServiceUid,
} from "./file-browser-isolation";
import type {
	PreparedHostedAgentPlugin,
	PreparedHostedAgentPlugins,
} from "./hosted-agent-plugin-package";
import type { HostedAgentPluginCommandRunner } from "./hosted-agent-plugin-runtime";
import { resolveHostedBundledSkill } from "./hosted-bundled-skill";
import { hostedHermesSkillSourceMatches } from "./hosted-hermes-skill";
import { createOpenClawHostedContext } from "./hosted-openclaw-context";
import { hostedAiProviderCatalog } from "./hosted-provider-resolution";
import type { PreparedHostedSkill } from "./hosted-sourced-skill-archive";
import { MANAGED_BAILEYS_STATIC_PATCH_TARGETS } from "./managed-baileys-compat";
import {
	managedSkillReservationLedgerPath,
	managedSkillReservationState,
	reserveManagedSkill,
	shouldIgnoreUserSkill,
} from "./managed-skill-reservation";
import {
	convergeRuntimeManifest as convergeRuntimeManifestWithContract,
	type RuntimeConvergenceOptions,
	type RuntimeManifest,
	type RuntimePrivateAppliedAuthority,
} from "./manifest";
import { openClawManagedChannelsPatch } from "./manifest-channels";
import {
	AGENT_PLUGIN_INSTALLATIONS_UNSUPPORTED_ERROR,
	fileBrowserCompanionSchema,
	type HostedRuntimeBundleV2Manifest,
	hostedRuntimeBundleV2ManifestSchema,
	OFFICIAL_INSTALL_URLS,
	officialInstallArgs,
} from "./manifest-contract";
import { runtimeCommandCurrentRevision } from "./manifest-install";
import { openClawGatewayHostedPatch } from "./manifest-providers";
import {
	AGENT_PLUGINS_SCHEMA_1_0_0,
	type HostedAgentPluginsDesiredState,
	type HostedSkillSource,
} from "./manifest-resources";
import { parseHostedRuntimeBundleV2, type RuntimeManifestLoad } from "./manifest-source";
import { applyOpenClawHostedChannelPatch } from "./openclaw-provider-config";
import { getRuntimePaths, type RuntimePaths, SYSTEMD_FILE_BROWSER_STATE_DIRECTORY } from "./paths";
import { type RuntimeRunSettings, runtimeRunConfigPath } from "./run-config";
import {
	COMPANION_SANDBOX_BASELINE,
	companionCredentialDirective,
	companionSandboxServiceLines,
	HERMES_DASHBOARD_BUILD_REVISION_FILE,
} from "./runtime-systemd-reconciliation";
import {
	canonicalSecretRefSchema,
	normalizeSecretValues,
	runtimeSecretValue,
} from "./secret-values";
import { ensureRuntimeStateDirs } from "./state";
import { readSystemdUnitSnapshot } from "./systemd-transaction";
// Unit names and the hosted drop-in filename are DERIVED rather than restated (P33-11), so a
// future move edits one place. The `clawdi-`-prefixed literals that REMAIN in this file are
// deliberate and each is one of three things: a FOREIGN decoy another product installed
// (`clawdi-foreign.*`), a legacy-named unit an already-installed host still carries
// (`clawdi-existing.service`, and the legacy half of the dual-read snapshot allowlist), or a
// P33-21 carve-out that did not move (the `clawdi-files:` token audience, which is a wire
// contract). The Files state and runtime directory names DID move -- they now derive from
// `SYSTEMD_FILE_BROWSER_STATE_DIRECTORY`.
import {
	GENERATED_RUNTIME_SYSTEMD_FILE_HEADER,
	isGeneratedRuntimeSystemdFile,
	RUNTIME_SYSTEM_UNIT_SIDECAR,
	RUNTIME_SYSTEMD_DROP_IN_FILE,
	RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME,
	RUNTIME_SYSTEMD_OWNED_NAME_PREFIX,
} from "./systemd-user";

const successfulPrerequisiteActivation = () => ({
	applied: true,
	systemUnitsChanged: [],
	userUnitsChanged: [],
});

const originalEnv = { ...process.env };
const tempRoots: string[] = [];
const TEST_HOSTED_LOCALE = { language: "en" as const, timezone: "UTC" };
const TEST_HOSTED_HOME = "/home/clawdi";
const TEST_PROCESS_UID = process.getuid?.() ?? 1_000;
const TEST_PROCESS_GID = process.getgid?.() ?? 1_000;
const TEST_RUNTIME_USER = String(TEST_PROCESS_UID);
const HERMES_CONFIG_CLI_MOCK = fileURLToPath(
	new URL("../test-support/hermes-config-cli-mock.ts", import.meta.url),
);
const HERMES_TEST_PROVIDER_TOKEN_REF = `\${HERMES_TEST_PROVIDER_TOKEN}`;
const FILE_BROWSER_VERSION = "v1.5.0-stable";
const FILE_BROWSER_COMMIT = "79552f8adb27c3e29934c4001660eb98f4aab5d6";
const FILE_BROWSER_AMD64_SHA256 =
	"8d51d1718d576d22e73e1f41a5194b451d152ddab0df97697cabe839cf59524e";
const FILE_BROWSER_ARM64_SHA256 =
	"3e18838ae33750a25da434dc6156a359968bf7935e01bdd884711f47f08ad92f";
const TEST_HOSTED_SECRET_VALUES = {
	"secret://clawdi/auth-token": "test-auth-token",
	"secret://runtime/openclaw/gateway-token": "gateway-token",
	"secret://tool.codex.apiKey": "test-codex-provider-key",
};
const TEST_HOSTED_CODEX_TOOLING = {
	codex: {
		enabled: true,
		provider_id: "codex-managed",
		primary_model: { provider_id: "codex-managed", model: "gpt-test" },
		provider: {
			kind: "openai-compatible",
			type: "openai",
			baseUrl: "https://provider.test/v1",
			apiMode: "openai_responses",
			managed_by: "clawdi",
			runtimeEnvName: "CLAWDI_AI_API_KEY",
			apiKeySecretRef: "secret://tool.codex.apiKey",
		},
	},
};
const TEST_AGENT_PLUGIN_INSTALLATION: HostedAgentPluginsDesiredState["installations"][string] = {
	installationId: "install_01hxyz",
	version: "1.2.3-rc.1+linux",
	agentPluginsSchema: AGENT_PLUGINS_SCHEMA_1_0_0,
	source: {
		type: "github",
		url: "https://github.com/acme/agent-plugins",
		path: "plugins/acme.tools",
		commit: "a".repeat(40),
	},
	contentDigest: `sha256-tree-v1:${"b".repeat(64)}`,
};
const TEST_AGENT_PLUGINS: HostedAgentPluginsDesiredState = {
	schemaVersion: 1,
	installations: { "acme.tools": TEST_AGENT_PLUGIN_INSTALLATION },
};
function preparedTestAgentPlugin(
	name: string,
	version: string,
	ownershipIdentity: string,
	remote = false,
): PreparedHostedAgentPlugin {
	const tree = [
		{
			path: "plugin.json",
			mode: 0o100644 as const,
			bytes: Buffer.from(JSON.stringify({ $schema: AGENT_PLUGINS_SCHEMA_1_0_0, name, version })),
		},
		...(remote
			? [
					{
						path: "mcp.json",
						mode: 0o100644 as const,
						bytes: Buffer.from(
							JSON.stringify({
								$schema: "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
								mcpServers: {
									remote: {
										type: "streamable-http",
										url: "https://mcp.example.test/mcp",
									},
								},
							}),
						),
					},
				]
			: []),
	].sort((left, right) => left.path.localeCompare(right.path));
	const treeDigest = createHash("sha256");
	for (const file of tree) {
		const fileDigest = createHash("sha256").update(file.bytes).digest("hex");
		treeDigest.update(
			`${file.mode.toString(8)}\0${file.path}\0${file.bytes.byteLength}\0${fileDigest}\n`,
		);
	}
	return {
		name,
		installation: {
			installationId: `install_${ownershipIdentity.slice(0, 8)}`,
			version,
			agentPluginsSchema: AGENT_PLUGINS_SCHEMA_1_0_0,
			source: {
				type: "github",
				url: "https://github.com/acme/agent-plugins",
				path: `plugins/${name}`,
				commit: ownershipIdentity.slice(0, 40),
			},
			contentDigest: `sha256-tree-v1:${treeDigest.digest("hex")}`,
			ownershipIdentity,
		},
		mcpServerNames: remote ? ["remote"] : [],
		tree,
	};
}

function testAgentPluginDesiredState(
	...plugins: PreparedHostedAgentPlugin[]
): HostedAgentPluginsDesiredState {
	return {
		schemaVersion: 1,
		installations: Object.fromEntries(
			plugins.map((plugin) => {
				const { ownershipIdentity: _ownershipIdentity, ...installation } = plugin.installation;
				return [plugin.name, installation];
			}),
		),
	};
}

function preparedTestAgentPluginState(
	desired: PreparedHostedAgentPlugin,
	previous?: PreparedHostedAgentPlugin,
): PreparedHostedAgentPlugins {
	return {
		runtime: "openclaw",
		desired: new Map([[desired.name, desired]]),
		previous: previous
			? new Map([
					[
						previous.name,
						{
							runtime: "openclaw" as const,
							name: previous.name,
							installation: previous.installation,
							nativeId: previous.name.replaceAll(".", "-"),
						},
					],
				])
			: new Map(),
		transientCacheOwnerships: new Set(),
	};
}

function hostedSystemFixture(overrides: Record<string, unknown> = {}): Record<string, unknown> {
	return {
		openclawControlUiAllowedOrigins: ["https://agent.example.test"],
		openclawGatewayTrustedProxies: ["10.173.0.1"],
		openclawControlUiBasePath: "/control",
		openclawGatewayAuth: hostedOpenClawNativeAuth(),
		...overrides,
	};
}

function hostedOpenClawNativeAuth(
	publicUrl = "https://agent.example.test/control",
): NonNullable<RuntimeManifest["openclawGatewayAuth"]> {
	void publicUrl;
	return {
		mode: "token",
		tokenRef: "secret://runtime/openclaw/gateway-token",
		deviceAuthRequired: false,
		activation: {
			enabled: true,
			capability: "openclaw-native-auth-v1",
		},
	};
}

function tempRuntimePaths(): RuntimePaths {
	const root = mkdtempSync(join(tmpdir(), "clawdi-runtime-reconcile-test-"));
	tempRoots.push(root);
	process.env.CLAWDI_SERVICE_STATE_DIR = join(root, "state");
	process.env.CLAWDI_RUN_DIR = join(root, "run");
	process.env.CLAWDI_SYSTEMD_SYSTEM_ROOT = join(root, "run", "systemd", "system");
	process.env.CLAWDI_RUNTIME_HOME = join(root, "home");
	process.env.CLAWDI_RUNTIME_MODE = "hosted";
	process.env.CLAWDI_RUNTIME_USER = TEST_RUNTIME_USER;
	process.env.CLAWDI_HOME = join(root, "clawdi-home");
	process.env.CLAWDI_CODEX_INSTALL_DISABLED = "1";
	// Point every test at a stub `systemctl` by DEFAULT rather than per-test. Unstubbed, the
	// stale-file GC in `removeStaleRuntimeSystemdFiles` shells out to the host's real
	// `systemctl daemon-reload`, which as an unprivileged user blocks on polkit until the 5s
	// test timeout kills it -- surfacing as "this test timed out" plus a leaked
	// "killed 1 dangling process". Tests that assert ON the systemctl invocation still
	// install their own stub after calling this helper.
	const systemctlStub = join(root, "systemctl-stub.sh");
	writeFileSync(systemctlStub, "#!/bin/sh\nexit 0\n");
	chmodSync(systemctlStub, 0o755);
	process.env.CLAWDI_SYSTEMCTL_PATH = systemctlStub;
	return getRuntimePaths({ mode: "hosted" });
}

function preparedTestSourcedSkill(
	skillId: string,
	source: HostedSkillSource,
	skillMd: string,
): PreparedHostedSkill & {
	identity: { source: HostedSkillSource; sourceIdentity: string; digest: string };
	tarBytes: Buffer;
} {
	const fixtureRoot = mkdtempSync(join(tmpdir(), "clawdi-prepared-skill-test-"));
	tempRoots.push(fixtureRoot);
	const sourceRoot = join(fixtureRoot, "source");
	const sourceDir = join(sourceRoot, skillId);
	mkdirSync(sourceDir, { recursive: true });
	writeFileSync(join(sourceDir, "SKILL.md"), skillMd);
	const archive = join(fixtureRoot, "skill.tar.gz");
	execFileSync("tar", ["-czf", archive, "-C", sourceRoot, skillId]);
	const tarBytes = readFileSync(archive);
	const sourceIdentity =
		source.type === "github"
			? ["github", skillId, source.url, source.path, source.commit].join("\0")
			: ["project", skillId, source.projectId, source.contentHash].join("\0");
	return {
		id: skillId,
		identity: {
			source,
			sourceIdentity,
			digest: createHash("sha256").update(tarBytes).digest("hex"),
		},
		tarBytes,
	};
}

function convergeRuntimeManifest(
	load: RuntimeManifestLoad,
	paths: RuntimePaths,
	opts: RuntimeConvergenceOptions = {},
) {
	ensureRuntimeStateDirs(paths);
	return convergeRuntimeManifestWithContract(load, paths, {
		...opts,
		systemdApply: opts.systemdApply,
		hostedRuntimeContract: opts.hostedRuntimeContract ?? {
			expectedIdentity: {
				home: paths.userHome,
				user: TEST_RUNTIME_USER,
				uid: TEST_PROCESS_UID,
				gid: TEST_PROCESS_GID,
			},
			resolveUserIdentity: () => ({ uid: TEST_PROCESS_UID, gid: TEST_PROCESS_GID }),
		},
	});
}

function runSettings(command: string, args: string[]): RuntimeRunSettings {
	return { command, args, env: {}, prependPath: [] };
}

function manifestLoad(
	manifest: RuntimeManifest,
	sourcePath: string,
	secretValues: Record<string, string> = TEST_HOSTED_SECRET_VALUES,
): RuntimeManifestLoad {
	return {
		manifest,
		source: "remote-datasource",
		sourcePath,
		offline: false,
		secretValues,
		applyContext: {
			kind: "context-file",
			backend: "incus",
			identity: {
				generation: manifest.applyGeneration ?? manifest.generation,
				manifestETag: `"test-${manifest.generation}"`,
				applyReceiptId: "test-apply-receipt",
				bootNonce: "test-boot-nonce",
			},
			manifestSource: {
				type: "http",
				url: "https://runtime.test/v1/runtime/manifest?environment_id=env-test",
				auth: { type: "bearer", token: "test-token" },
			},
		},
	};
}

function baseManifest(
	paths: RuntimePaths,
	runtimes: RuntimeManifest["runtimes"],
	overrides: Partial<RuntimeManifest> = {},
): RuntimeManifest {
	const openclaw = runtimes.openclaw;
	return {
		schemaVersion: "clawdi.runtimeDesiredState.v1",
		deploymentId: "hdep_reconcile",
		environmentId: "env_reconcile",
		instanceId: "hri_reconcile",
		generation: 1,
		issuedAt: "2026-07-01T00:00:00.000Z",
		workspaceRoot: join(paths.userHome, "clawdi"),
		controlPlane: { apiUrl: "https://cloud-api.example.test" },
		runtimes: openclaw?.run
			? {
					...runtimes,
					openclaw: {
						...openclaw,
						run: {
							...openclaw.run,
							secretEnv: {
								OPENCLAW_GATEWAY_TOKEN: "secret://runtime/openclaw/gateway-token",
								...openclaw.run.secretEnv,
							},
						},
					},
				}
			: runtimes,
		...(openclaw ? { openclawGatewayAuth: hostedOpenClawNativeAuth() } : {}),
		recovery: {},
		...overrides,
	};
}

function testEgressEnginePin(
	version: string,
	sha256: string,
): NonNullable<RuntimeManifest["egressEngine"]> {
	return {
		type: "mitmproxy",
		version,
		url: `https://downloads.mitmproxy.org/${version}/mitmproxy-${version}-linux-x86_64.tar.gz`,
		sha256,
	};
}

function installCachedTestEgressEngine(paths: RuntimePaths, version: string) {
	const engine = testEgressEnginePin(version, "a".repeat(64));
	const binaryPath = join(paths.egressEngineMaintainedRoot, version, engine.sha256, "mitmdump");
	mkdirSync(dirname(binaryPath), { recursive: true });
	writeFileSync(binaryPath, "#!/usr/bin/env sh\nexit 0\n");
	chmodSync(binaryPath, 0o755);
	return engine;
}

function writeTestMitmproxyArchive(
	paths: RuntimePaths,
	name: string,
	kind: "ready" | "missing-mitmdump" | "corrupt",
): { path: string; sha256: string } {
	const fixtureRoot = join(dirname(paths.serviceStateRoot), "egress-engine-fixtures", name);
	const archivePath = join(fixtureRoot, `${name}.tar.gz`);
	mkdirSync(fixtureRoot, { recursive: true });
	if (kind === "corrupt") {
		writeFileSync(archivePath, "not a tar.gz archive\n");
	} else {
		const sourceRoot = join(fixtureRoot, "source", `mitmproxy-${name}`);
		mkdirSync(sourceRoot, { recursive: true });
		if (kind === "ready") {
			const binaryPath = join(sourceRoot, "mitmdump");
			writeFileSync(binaryPath, "#!/usr/bin/env sh\nexit 0\n");
			chmodSync(binaryPath, 0o755);
		} else {
			writeFileSync(join(sourceRoot, "README.txt"), "mitmdump intentionally absent\n");
		}
		execFileSync("tar", ["-czf", archivePath, "-C", join(fixtureRoot, "source"), "."]);
	}
	return {
		path: archivePath,
		sha256: createHash("sha256").update(readFileSync(archivePath)).digest("hex"),
	};
}

function installTestMitmproxyCurl(
	paths: RuntimePaths,
	artifactPath: string | null,
): { commandPath: string; markerPath: string } {
	const binRoot = join(dirname(paths.serviceStateRoot), "egress-engine-test-bin");
	const curlPath = join(binRoot, "curl");
	const markerPath = join(binRoot, "curl-invoked");
	mkdirSync(binRoot, { recursive: true });
	writeFileSync(
		curlPath,
		artifactPath
			? [
					"#!/usr/bin/env bash",
					"set -euo pipefail",
					`printf invoked > ${JSON.stringify(markerPath)}`,
					`cp -- ${JSON.stringify(artifactPath)} "$5"`,
					"",
				].join("\n")
			: [
					"#!/usr/bin/env bash",
					"set -euo pipefail",
					`printf invoked > ${JSON.stringify(markerPath)}`,
					"printf 'artifact endpoint unavailable: test-token\\n' >&2",
					"exit 22",
					"",
				].join("\n"),
	);
	chmodSync(curlPath, 0o700);
	return { commandPath: curlPath, markerPath };
}

function egressRuntimeManifest(
	paths: RuntimePaths,
	input: {
		generation: number;
		engine?: RuntimeManifest["egressEngine"];
		profile: "enabled" | "disabled" | "absent";
	},
): RuntimeManifest {
	process.env.OPENCLAW_GATEWAY_TOKEN = "gateway-token";
	const commandPath = join(paths.userHome, ".local", "bin", "openclaw");
	writeFakeGatewayCli({
		path: commandPath,
		runtime: "openclaw",
		unitPath: join(paths.systemdUserRoot, "openclaw-gateway.service"),
	});
	return baseManifest(
		paths,
		{
			openclaw: {
				enabled: true,
				run: runSettings(commandPath, ["gateway", "run"]),
				services: {},
			},
		},
		{
			generation: input.generation,
			issuedAt: `2026-07-01T00:0${input.generation}:00.000Z`,
			openclawGatewayAuth: hostedOpenClawNativeAuth(),
			projection: {
				system: hostedSystemFixture(),
			},
			...(input.engine ? { egressEngine: input.engine } : {}),
			...(input.profile === "absent"
				? {}
				: {
						egressProfiles: {
							profiles: [
								{
									id: "required-egress",
									enabled: input.profile === "enabled",
									kind: "http" as const,
									match: {
										scheme: "https" as const,
										host: "api.example.test",
										headers: {},
										query: {},
									},
									rewrite: {
										upstreamBaseUrl: "https://upstream.example.test",
										preservePath: true,
										setHeaders: {},
									},
									logging: { redactHeaders: [], redactUrlPatterns: [] },
									priority: 100,
								},
							],
						},
					}),
		},
	);
}

function commitTestRuntimeAuthority(
	load: RuntimeManifestLoad,
	paths: RuntimePaths,
	convergence: ReturnType<typeof convergeRuntimeManifest>,
	authority: RuntimePrivateAppliedAuthority,
): void {
	commitRuntimeAppliedState({
		load,
		paths,
		etag: `"generation-${load.manifest.generation}"`,
		sourceRevision: runtimeContentSha256({ generation: load.manifest.generation }),
		convergence,
		applyIdentity: null,
		activated: authority.activated,
		officialServiceCommandRevisions: authority.officialServiceCommandRevisions,
	});
}

function hostedManifestFixture(overrides: Record<string, unknown> = {}): Record<string, unknown> {
	return {
		schemaVersion: "clawdi.hosted-runtime.manifest.v1",
		runtime: "openclaw",
		deploymentId: "hdep_locale",
		environmentId: "env_locale",
		instanceId: "hri_locale",
		generation: 1,
		issuedAt: "2026-07-11T00:00:00.000Z",
		locale: TEST_HOSTED_LOCALE,
		system: hostedSystemFixture(),
		controlPlane: { cloudApiUrl: "https://cloud-api.example.test" },
		clawdiCli: {
			source: "npm:clawdi",
			packageSpec: "clawdi@1.2.3-test",
			registry: "https://registry.npmjs.org",
		},
		providers: {
			default: {
				kind: "openai-compatible",
				status: "error",
				error: { code: "provider_not_found", message: "fixture provider unavailable" },
			},
		},
		terminalTooling: structuredClone(TEST_HOSTED_CODEX_TOOLING),
		liveSync: { enabled: false, agents: [] },
		recovery: { cacheManifest: true, allowOfflineBoot: true },
		runtimes: {
			openclaw: {
				enabled: true,
				install: { source: "official" },
				providerMode: "configured",
				provider_ids: ["default"],
				primary_model: { provider_id: "default", model: "gpt-test" },
				run: {
					args: ["gateway", "run"],
					secretEnv: {
						OPENCLAW_GATEWAY_TOKEN: "secret://runtime/openclaw/gateway-token",
					},
				},
			},
		},
		...overrides,
	};
}

function hostedRuntimeFixture(overrides: Record<string, unknown> = {}): Record<string, unknown> {
	return {
		enabled: true,
		install: { source: "official" },
		providerMode: "configured",
		provider_ids: ["default"],
		primary_model: { provider_id: "default", model: "gpt-test" },
		run: {
			args: ["gateway", "run"],
			secretEnv: {
				OPENCLAW_GATEWAY_TOKEN: "secret://runtime/openclaw/gateway-token",
			},
		},
		...overrides,
	};
}

function hostedHermesManifestFixture(
	overrides: Record<string, unknown> = {},
): Record<string, unknown> {
	return hostedManifestFixture({
		runtime: "hermes",
		system: {
			hermesDashboardAuth: {
				mode: "oidc",
				provider: "self-hosted",
				deploymentId: "hdep_K8fJ3pQm",
				issuer: "https://api.example.test/v2/hermes/oidc",
				clientId: "clawdi-hermes-hdep_K8fJ3pQm-r1",
				accessRevision: 1,
				publicUrl: "https://agent.example.test/hermes",
				trustedProxies: ["10.173.0.1"],
				activation: { enabled: true, capability: "hermes-self-hosted-oidc-v1" },
			},
		},
		runtimes: {
			hermes: hostedRuntimeFixture({
				run: { args: ["gateway", "run"] },
				services: {
					dashboard: {
						args: ["dashboard", "--host", "0.0.0.0", "--port", "9119", "--no-open"],
					},
				},
			}),
		},
		...overrides,
	});
}

function hostedOpenClawV2ManifestFixture(
	overrides: Record<string, unknown> = {},
	gatewayArgs: string[] = ["gateway", "run"],
): Record<string, unknown> {
	const publicUrl = "https://agent.example.test/control";
	return hostedManifestFixture({
		schemaVersion: "clawdi.hosted-runtime.manifest.v1",
		system: hostedSystemFixture({
			openclawControlUiAllowedOrigins: ["https://agent.example.test"],
			openclawControlUiBasePath: "/control",
			openclawGatewayAuth: hostedOpenClawNativeAuth(publicUrl),
		}),
		runtimes: {
			openclaw: hostedRuntimeFixture({
				run: {
					args: gatewayArgs,
					secretEnv: {
						OPENCLAW_GATEWAY_TOKEN: "secret://runtime/openclaw/gateway-token",
					},
				},
			}),
		},
		...overrides,
	});
}

const LEGACY_HOSTED_OPENCLAW_GATEWAY_RUN_ARGS = [
	"gateway",
	"run",
	"--allow-unconfigured",
	"--port",
	"18789",
	"--bind",
	"lan",
	"--force",
];

function normalizeHostedBundleFixture(
	manifest: Record<string, unknown>,
	secretValues: Record<string, string>,
): RuntimeManifestLoad {
	return parseHostedRuntimeBundleV2(
		{
			schemaVersion: "clawdi.hosted-runtime.bundle.v2",
			sourceRevision: "a".repeat(64),
			manifest,
			channelBindings: [],
			secretValues,
		},
		"test://manifest-reconciliation",
	);
}

function writeFakeGatewayCli(input: {
	path: string;
	runtime: "openclaw" | "hermes";
	unitPath: string;
	configPatchPath?: string;
	failInstall?: boolean;
	skillInstallSourceLog?: string;
	commandLog?: string;
}): void {
	const home = dirname(dirname(dirname(input.path)));
	mkdirSync(dirname(input.path), { recursive: true });
	writeFileSync(
		input.path,
		`#!/usr/bin/env bash
set -euo pipefail
${input.commandLog ? `printf '%s\\n' "$*" >> '${input.commandLog}'` : ""}
case "$*" in
	"--version")
		printf '%s\\n' '${input.runtime === "openclaw" ? "OpenClaw test-version" : "Hermes test-version"}'
		;;
	"config patch --stdin"*)
		${input.configPatchPath ? `cat > '${input.configPatchPath}'` : "cat >/dev/null"}
		;;
	"config path"|"config get "*|"config set "*|"config unset "*)
		HOME='${home}' exec '${process.execPath}' '${HERMES_CONFIG_CLI_MOCK}' "$@"
		;;
	"config schema")
		printf '%s\n' '{"type":"object","properties":{"memory":{"type":"object","properties":{"search":{"type":"object"}}}}}'
		;;
  "gateway install --force --json"|"gateway install --force --no-start-now"|"gateway install")
    ${
			input.failInstall
				? "exit 41"
				: `mkdir -p '${dirname(input.unitPath)}'
    cat > '${input.unitPath}' <<'EOF'
[Unit]
Description=Official gateway

[Service]
ExecStart=official gateway run
EOF
    printf '%s\\n' '{"ok":true}'`
		}
    ;;
  "gateway uninstall")
    rm -f '${input.unitPath}'
    ;;
	  "agents list --json")
	    printf '[{"id":"main","workspace":"%s"}]\n' "$HOME/.openclaw/workspace"
	    ;;
	  "skills install "*)
	    source_dir="$3"
	    skill_id="$7"
	    ${input.skillInstallSourceLog ? `printf '%s\\n' "$source_dir" > '${input.skillInstallSourceLog}'` : ""}
	    workspace="$HOME/.openclaw/workspace"
	    mkdir -p "$workspace/skills"
	    rm -rf "$workspace/skills/$skill_id"
	    cp -R "$source_dir" "$workspace/skills/$skill_id"
	    mkdir -p "$workspace/skills/$skill_id/.openclaw"
	    printf '{}\n' > "$workspace/skills/$skill_id/.openclaw/source-origin.json"
    ;;
  *)
    printf 'unexpected ${input.runtime} command: %s\\n' "$*" >&2
    exit 64
    ;;
esac
`,
	);
	chmodSync(input.path, 0o700);
}

type FileBrowserCompanion = NonNullable<NonNullable<RuntimeManifest["companions"]>["filebrowser"]>;

function fileBrowserCompanion(accessRevision = "a".repeat(64)): FileBrowserCompanion {
	const audience = "clawdi-files:hdep_files_reconcile";
	return {
		version: FILE_BROWSER_VERSION,
		commit: FILE_BROWSER_COMMIT,
		listen: "0.0.0.0",
		port: 9120,
		baseURL: "/",
		healthPath: "/health",
		sourceRoot: "/home/iforge",
		assets: {
			amd64: {
				url: `https://github.com/gtsteffaniak/filebrowser/releases/download/${FILE_BROWSER_VERSION}/linux-amd64-filebrowser`,
				sha256: FILE_BROWSER_AMD64_SHA256,
			},
			arm64: {
				url: `https://github.com/gtsteffaniak/filebrowser/releases/download/${FILE_BROWSER_VERSION}/linux-arm64-filebrowser`,
				sha256: FILE_BROWSER_ARM64_SHA256,
			},
		},
		auth: {
			method: "jwt",
			algorithm: "HS256",
			header: "X-JWT-Assertion",
			userIdentifier: "sub",
			groupsClaim: "groups",
			secret: accessRevision.slice(0, 43),
			audience,
			subject: "deployment:hdep_files_reconcile:owner",
			requiredGroup: `${audience}:${accessRevision}`,
			accessRevision,
		},
	};
}

function fileBrowserBinaryPath(paths: RuntimePaths, binary: string): string {
	const sha256 = createHash("sha256").update(binary).digest("hex");
	return join(paths.fileBrowserInstallRoot, "candidates", sha256, "filebrowser");
}

function fileBrowserManifest(
	paths: RuntimePaths,
	input: { generation: number; binary: string; accessRevision?: string },
): RuntimeManifest {
	const command = join(paths.userHome, ".local", "bin", "openclaw");
	writeFakeGatewayCli({
		path: command,
		runtime: "openclaw",
		unitPath: join(paths.systemdUserRoot, "openclaw-gateway.service"),
	});
	const companion = fileBrowserCompanion(input.accessRevision);
	Reflect.set(
		companion.assets.amd64,
		"sha256",
		createHash("sha256").update(input.binary).digest("hex"),
	);
	return baseManifest(
		paths,
		{
			openclaw: {
				enabled: true,
				run: runSettings(command, ["gateway", "run"]),
				services: {},
			},
		},
		{
			generation: input.generation,
			issuedAt: `2026-08-05T00:00:${String(input.generation).padStart(2, "0")}.000Z`,
			openclawGatewayAuth: hostedOpenClawNativeAuth(),
			projection: {
				system: hostedSystemFixture(),
			},
			companions: { filebrowser: companion },
		},
	);
}

function writeFakeHermesCli(paths: RuntimePaths): string {
	installHermesNativeFixture(paths.userHome);
	const path = join(paths.userHome, ".local", "bin", "hermes");
	writeFakeGatewayCli({
		path,
		runtime: "hermes",
		unitPath: join(paths.systemdUserRoot, "hermes-gateway.service"),
	});
	return path;
}

function fileBrowserManifestLoad(manifest: RuntimeManifest): RuntimeManifestLoad {
	const load = manifestLoad(manifest, `files-generation-${manifest.generation}`);
	if (!load.applyContext) throw new Error("Files test apply context is missing");
	return {
		...load,
		applyContext: { ...load.applyContext, backend: "incus" },
	};
}

function fileBrowserApplyHooks(
	input: { activationApplied?: boolean; onActivate?: () => void } = {},
) {
	return {
		activateEgressPrerequisite: successfulPrerequisiteActivation,
		activate: () => {
			input.onActivate?.();
			return {
				applied: input.activationApplied ?? true,
				systemUnitsChanged: [],
				userUnitsChanged: [],
			};
		},
	};
}

const testFileBrowserServiceIsolation = () => ({
	uid: typeof process.geteuid === "function" ? process.geteuid() : 0,
	gid: typeof process.getegid === "function" ? process.getegid() : 0,
});

afterEach(() => {
	process.env = { ...originalEnv };
	for (const root of tempRoots.splice(0)) rmSync(root, { recursive: true, force: true });
});

describe("runtime manifest reconciliation invariants", () => {
	test.each([
		["OpenClaw", hostedOpenClawV2ManifestFixture()],
		["Hermes", hostedHermesManifestFixture()],
	] as const)(
		"rejects the removed bridge field in every hosted %s manifest schema",
		(_name, valid) => {
			expect(hostedRuntimeBundleV2ManifestSchema.safeParse(valid).success).toBe(true);
			const withBridge = { ...valid, bridge: {} };
			expect(hostedRuntimeBundleV2ManifestSchema.safeParse(withBridge).success).toBe(false);
		},
	);

	test("requires typed native token auth for hosted OpenClaw v2", () => {
		const valid = hostedOpenClawV2ManifestFixture();
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(valid).success).toBe(true);

		const missingAuth = structuredClone(valid);
		delete (missingAuth.system as { openclawGatewayAuth?: unknown }).openclawGatewayAuth;
		const missingAuthResult = hostedRuntimeBundleV2ManifestSchema.safeParse(missingAuth);
		expect(missingAuthResult.success).toBe(false);
		expect(
			missingAuthResult.error?.issues.some(
				(issue) => issue.message === "OpenClaw native auth activation must be explicitly enabled",
			),
		).toBe(false);

		const inactive = structuredClone(valid);
		(
			inactive.system as { openclawGatewayAuth: { activation: { enabled: boolean } } }
		).openclawGatewayAuth.activation.enabled = false;
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(inactive).success).toBe(false);

		const mismatchedOrigin = structuredClone(valid);
		(
			mismatchedOrigin.system as { openclawControlUiAllowedOrigins: string[] }
		).openclawControlUiAllowedOrigins = ["https://other.example.test"];
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(mismatchedOrigin).success).toBe(true);
		const missingOrigin = structuredClone(valid);
		(
			missingOrigin.system as { openclawControlUiAllowedOrigins: string[] }
		).openclawControlUiAllowedOrigins = [];
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(missingOrigin).success).toBe(false);
	});

	test("keeps hosted runtime process ownership on the exact upstream commands", () => {
		for (const manifest of [hostedOpenClawV2ManifestFixture(), hostedHermesManifestFixture()]) {
			const runtime = manifest.runtime as "openclaw" | "hermes";
			delete (manifest.runtimes as Record<string, { run?: unknown }>)[runtime].run;
			expect(hostedRuntimeBundleV2ManifestSchema.safeParse(manifest).success).toBe(false);
		}

		for (const [field, value] of [
			["command", "openclaw"],
			["cwd", "/home/clawdi"],
			["env", { EXTRA: "value" }],
			["prependPath", ["/custom/bin"]],
		] as const) {
			const manifest = hostedOpenClawV2ManifestFixture();
			const run = (manifest.runtimes as { openclaw: { run: Record<string, unknown> } }).openclaw
				.run;
			run[field] = value;
			expect(hostedRuntimeBundleV2ManifestSchema.safeParse(manifest).success).toBe(false);
		}

		const openClawService = hostedOpenClawV2ManifestFixture();
		(
			openClawService.runtimes as { openclaw: { services: Record<string, unknown> } }
		).openclaw.services = { helper: { args: ["helper"] } };
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(openClawService).success).toBe(false);

		const hermesService = hostedHermesManifestFixture();
		(
			hermesService.runtimes as { hermes: { services: Record<string, unknown> } }
		).hermes.services.helper = { args: ["helper"] };
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(hermesService).success).toBe(false);

		const hermesDashboardEnv = hostedHermesManifestFixture();
		const dashboard = (
			hermesDashboardEnv.runtimes as {
				hermes: { services: { dashboard: Record<string, unknown> } };
			}
		).hermes.services.dashboard;
		dashboard.env = { EXTRA: "value" };
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(hermesDashboardEnv).success).toBe(false);
	});

	test("normalizes the hosted OpenClaw producer gateway args", () => {
		const legacy = hostedRuntimeBundleV2ManifestSchema.parse(
			hostedOpenClawV2ManifestFixture({}, LEGACY_HOSTED_OPENCLAW_GATEWAY_RUN_ARGS),
		);
		expect(legacy.runtimes.openclaw.run?.args).toEqual(["gateway", "run"]);

		const unsupported = hostedOpenClawV2ManifestFixture({}, ["gateway", "run", "--force"]);
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(unsupported).success).toBe(false);

		const legacyHermes = hostedHermesManifestFixture();
		(legacyHermes.runtimes as { hermes: { run: { args: string[] } } }).hermes.run.args = [
			"gateway",
			"run",
			"--replace",
		];
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(legacyHermes).success).toBe(false);
	});

	test("requires OIDC auth and direct 9119 exposure for hosted Hermes v2", () => {
		const valid = hostedHermesManifestFixture();
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(valid).success).toBe(true);
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse({
				...valid,
				system: hostedSystemFixture(),
			}).success,
		).toBe(false);
		const inactive = structuredClone(valid);
		(
			inactive.system as { hermesDashboardAuth: { activation: { enabled: boolean } } }
		).hermesDashboardAuth.activation.enabled = false;
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(inactive).success).toBe(false);

		const oidc = hostedHermesManifestFixture({
			deploymentId: "hdep_K8fJ3pQm",
			system: {
				hermesDashboardAuth: {
					mode: "oidc",
					provider: "self-hosted",
					deploymentId: "hdep_K8fJ3pQm",
					issuer: "https://api.example.test/v2/hermes/oidc",
					clientId: "clawdi-hermes-hdep_K8fJ3pQm-r7",
					accessRevision: 7,
					publicUrl: "https://agent.example.test/hermes",
					trustedProxies: ["10.173.0.1"],
					activation: { enabled: true, capability: "hermes-self-hosted-oidc-v1" },
				},
			},
		});
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(oidc).success).toBe(true);
		const rebound = structuredClone(oidc);
		(
			rebound.system as { hermesDashboardAuth: { deploymentId: string } }
		).hermesDashboardAuth.deploymentId = "hdep_Q9mN4sTr";
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(rebound).success).toBe(false);
	});
	test("accepts and preserves the exact hosted locale contract", () => {
		const parsed = hostedRuntimeBundleV2ManifestSchema.parse(
			hostedManifestFixture({ locale: { language: "zh-CN", timezone: "Asia/Shanghai" } }),
		);
		expect(parsed.locale).toEqual({ language: "zh-CN", timezone: "Asia/Shanghai" });
	});

	test("strictly parses and preserves the Agent Plugins desired-state contract", () => {
		const parsed = hostedRuntimeBundleV2ManifestSchema.parse(
			hostedManifestFixture({ agentPlugins: TEST_AGENT_PLUGINS }),
		);
		expect(parsed.projection?.agentPlugins).toEqual(TEST_AGENT_PLUGINS);
		const maximumVersion = `1.2.3+${"a".repeat(250)}`;
		const maximumLengthPlugins = {
			...TEST_AGENT_PLUGINS,
			installations: {
				"acme.tools": { ...TEST_AGENT_PLUGIN_INSTALLATION, version: maximumVersion },
			},
		};
		expect(
			hostedRuntimeBundleV2ManifestSchema.parse(
				hostedManifestFixture({ agentPlugins: maximumLengthPlugins }),
			).projection?.agentPlugins,
		).toEqual(maximumLengthPlugins);

		const empty = hostedRuntimeBundleV2ManifestSchema.parse(
			hostedManifestFixture({ agentPlugins: { schemaVersion: 1, installations: {} } }),
		);
		expect(empty.projection?.agentPlugins).toEqual({
			schemaVersion: 1,
			installations: {},
		});
		expect(
			hostedRuntimeBundleV2ManifestSchema.parse(hostedManifestFixture()).projection?.agentPlugins,
		).toBeUndefined();
	});

	test.each([
		["mutable version", { ...TEST_AGENT_PLUGIN_INSTALLATION, version: "^1.2.3" }],
		[
			"overlong version",
			{ ...TEST_AGENT_PLUGIN_INSTALLATION, version: `1.2.3+${"a".repeat(251)}` },
		],
		["noncanonical schema URI", { ...TEST_AGENT_PLUGIN_INSTALLATION, agentPluginsSchema: "1.0.0" }],
		[
			"mutable source",
			{
				...TEST_AGENT_PLUGIN_INSTALLATION,
				source: { ...TEST_AGENT_PLUGIN_INSTALLATION.source, commit: "main" },
			},
		],
		[
			"unsafe source path",
			{
				...TEST_AGENT_PLUGIN_INSTALLATION,
				source: { ...TEST_AGENT_PLUGIN_INSTALLATION.source, path: "plugins/../escape" },
			},
		],
		[
			"noncanonical digest",
			{ ...TEST_AGENT_PLUGIN_INSTALLATION, contentDigest: `sha256-tree-v1:${"B".repeat(64)}` },
		],
		[
			"unknown secret shape",
			{ ...TEST_AGENT_PLUGIN_INSTALLATION, secretValues: { "api-token": "plaintext" } },
		],
	] as const)("rejects Agent Plugins %s", (_name, installation) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({
					agentPlugins: {
						schemaVersion: 1,
						installations: { "acme.tools": installation },
					},
				}),
			).success,
		).toBe(false);
	});

	test("rejects a noncanonical Agent Plugin installation key", () => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({
					agentPlugins: {
						schemaVersion: 1,
						installations: { Acme: TEST_AGENT_PLUGIN_INSTALLATION },
					},
				}),
			).success,
		).toBe(false);
	});

	test("fails closed before converging unsupported Agent Plugins installations", () => {
		const paths = tempRuntimePaths();
		const hosted = hostedRuntimeBundleV2ManifestSchema.parse(
			hostedManifestFixture({ agentPlugins: TEST_AGENT_PLUGINS }),
		);

		expect(() =>
			convergeRuntimeManifestWithContract(
				manifestLoad(hosted, "inline-hosted-agent-plugins"),
				paths,
			),
		).toThrow(AGENT_PLUGIN_INSTALLATIONS_UNSUPPORTED_ERROR);
	});

	test("isolates an unsupported Hermes streamable-http plugin during apply", () => {
		const paths = tempRuntimePaths();
		const command = writeFakeHermesCli(paths);
		const desired = preparedTestAgentPlugin("acme.tools", "1.2.3", "a".repeat(64), true);
		const manifest = baseManifest(
			paths,
			{ hermes: { enabled: true, run: runSettings(command, ["gateway"]), services: {} } },
			{
				runtime: "hermes",
				projection: { agentPlugins: testAgentPluginDesiredState(desired) },
			},
		);
		const runner: HostedAgentPluginCommandRunner = {
			run: (input) => {
				if (input.command === "git") return { status: 0, stdout: "", stderr: "" };
				if (input.args[0] === "config") {
					const configPath = join(input.home, ".hermes", "config.yaml");
					mkdirSync(dirname(configPath), { recursive: true });
					writeFileSync(configPath, "plugins:\n  scan_on_install: false\n");
					return { status: 0, stdout: "", stderr: "" };
				}
				if (input.args[1] === "list") return { status: 0, stdout: "[]", stderr: "" };
				if (input.args[1] === "install") {
					const partialRoot = join(input.home, ".hermes", "plugins", "acme.tools");
					mkdirSync(partialRoot, { recursive: true });
					writeFileSync(join(partialRoot, "partial"), "partial install");
					return { status: 1, stdout: "", stderr: "streamable-http unsupported" };
				}
				throw new Error(`unexpected Agent Plugin command: ${input.args.join(" ")}`);
			},
		};
		let authorityCommits = 0;
		const result = convergeRuntimeManifest(
			manifestLoad(manifest, "hermes-plugin-unsupported"),
			paths,
			{
				cacheLastGood: false,
				preparedHostedAgentPlugins: {
					...preparedTestAgentPluginState(desired),
					runtime: "hermes",
				},
				hostedAgentPluginCommandRunner: runner,
				commitAuthority: () => authorityCommits++,
				systemdApply: {
					activateEgressPrerequisite: successfulPrerequisiteActivation,
					activate: () => ({ applied: true, systemUnitsChanged: [], userUnitsChanged: [] }),
				},
			},
		);

		expect(result.installErrors).toEqual([]);
		expect(result.resourceProjectionErrors).toEqual([
			"runtime Agent Plugin projection failed: Hermes native Agent Plugin install failed: streamable-http unsupported",
		]);
		expect(result.agentPluginFailedNames).toEqual(["acme.tools"]);
		expect(authorityCommits).toBe(1);
	});

	test.each([
		["missing locale", undefined],
		["unknown locale key", { language: "en", timezone: "UTC", personality: "warm" }],
		["malformed language", { language: "zh-cn", timezone: "UTC" }],
		["unsupported language", { language: "en-US", timezone: "UTC" }],
		["invalid timezone", { language: "en", timezone: "Mars/Olympus" }],
	])("rejects hosted manifests with %s", (_name, locale) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(hostedManifestFixture({ locale })).success,
		).toBe(false);
	});

	test.each([
		["missing providers", undefined],
		["missing selected provider", {}],
		[
			"unselected provider",
			{
				default: { kind: "openai-compatible" },
				extra: { kind: "openai-compatible" },
			},
		],
	])("rejects hosted manifests with %s", (_name, providers) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(hostedManifestFixture({ providers })).success,
		).toBe(false);
	});

	test("accepts and preserves canonical hosted model capability fields", () => {
		const parsed = hostedRuntimeBundleV2ManifestSchema.parse(
			hostedManifestFixture({
				providers: {
					default: {
						kind: "openai-compatible",
						type: "custom_openai_compatible",
						baseUrl: "https://provider.example.test/v1",
						apiMode: "openai_responses",
						managed_by: "clawdi",
						runtimeEnvName: "CLAWDI_AI_API_KEY",
						models: [
							{
								id: "k3",
								context_window: 1_048_576,
								max_input_tokens: 1_048_576,
								input_modalities: ["text", "image"],
								supports_tools: true,
								supports_reasoning: true,
								compat: { supportsDeveloperRole: false },
							},
						],
					},
				},
				runtimes: {
					openclaw: hostedRuntimeFixture({
						primary_model: { provider_id: "default", model: "k3" },
					}),
				},
			}),
		);
		expect(parsed.projection?.providers?.default).toMatchObject({
			models: [
				{
					id: "k3",
					context_window: 1_048_576,
					max_input_tokens: 1_048_576,
					input_modalities: ["text", "image"],
					supports_tools: true,
					supports_reasoning: true,
					compat: { supportsDeveloperRole: false },
				},
			],
		});
	});

	test.each([
		["enabled without agents", { enabled: true, agents: [] }],
		[
			"disabled with agents",
			{ enabled: false, agents: [{ agentType: "openclaw", environmentId: "env-live" }] },
		],
		[
			"duplicate agents",
			{
				enabled: true,
				agents: [
					{ agentType: "openclaw", environmentId: "env-live" },
					{ agentType: "openclaw", environmentId: "env-live" },
				],
			},
		],
		[
			"environment id with surrounding whitespace",
			{ enabled: true, agents: [{ agentType: "openclaw", environmentId: " env-live " }] },
		],
		[
			"unsupported agent type",
			{ enabled: true, agents: [{ agentType: "custom-runtime", environmentId: "env-live" }] },
		],
		[
			"overlong environment id",
			{ enabled: true, agents: [{ agentType: "openclaw", environmentId: "e".repeat(201) }] },
		],
	])("rejects hosted live sync with %s", (_name, liveSync) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(hostedManifestFixture({ liveSync })).success,
		).toBe(false);
	});

	test.each([
		["language", "en"],
		["timezone", "UTC"],
		["personality", "warm"],
	])("rejects the top-level %s compatibility field", (field, value) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(hostedManifestFixture({ [field]: value }))
				.success,
		).toBe(false);
	});

	test.each([
		["providerIds", hostedRuntimeFixture({ providerIds: ["default"] })],
		[
			"primaryModel",
			hostedRuntimeFixture({
				primaryModel: { provider_id: "default", model: "gpt-test" },
			}),
		],
		[
			"primary_model.providerId",
			hostedRuntimeFixture({
				primary_model: { providerId: "default", model: "gpt-test" },
			}),
		],
		["string primary_model", hostedRuntimeFixture({ primary_model: "gpt-test" })],
		[
			"paths.stateDir",
			hostedRuntimeFixture({
				paths: { home: "/home/clawdi", workspace: "/workspace", stateDir: "/state" },
			}),
		],
	])("rejects noncanonical hosted runtime field %s", (_name, runtime) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({ runtimes: { openclaw: runtime } }),
			).success,
		).toBe(false);
	});

	test("accepts a primary provider with an additional capability provider", () => {
		const canonical = hostedRuntimeBundleV2ManifestSchema.parse(
			hostedManifestFixture({
				providers: {
					default: {
						kind: "openai-compatible",
						status: "error",
						error: { code: "provider_not_found", message: "fixture provider unavailable" },
					},
					capability: {
						kind: "openai-compatible",
						status: "error",
						error: { code: "provider_not_found", message: "fixture provider unavailable" },
					},
				},
				runtimes: {
					openclaw: hostedRuntimeFixture({
						provider_ids: ["default", "capability"],
						primary_model: { provider_id: "default", model: "gpt-test" },
					}),
				},
			}),
		);
		expect(canonical.runtimes.openclaw).toMatchObject({
			provider_ids: ["default", "capability"],
			primary_model: { provider_id: "default", model: "gpt-test" },
		});
	});

	test("accepts explicit unmanaged provider mode without provider state", () => {
		const runtime = hostedRuntimeFixture({
			providerMode: "unmanaged",
			provider_ids: [],
		});
		delete runtime.primary_model;
		const parsed = hostedRuntimeBundleV2ManifestSchema.parse(
			hostedManifestFixture({
				providers: {},
				runtimes: { openclaw: runtime },
			}),
		);
		expect(parsed.runtimes.openclaw).toMatchObject({
			providerMode: "unmanaged",
			provider_ids: [],
		});
		expect(parsed.runtimes.openclaw.primary_model).toBeUndefined();
		expect(parsed.projection?.providers).toEqual({});
	});

	test.each([
		[
			"unmanaged provider ids",
			hostedRuntimeFixture({ providerMode: "unmanaged", provider_ids: ["default"] }),
		],
		[
			"unmanaged primary model",
			hostedRuntimeFixture({ providerMode: "unmanaged", provider_ids: [] }),
		],
		[
			"configured empty provider ids",
			hostedRuntimeFixture({ providerMode: "configured", provider_ids: [] }),
		],
		[
			"missing provider mode",
			(() => {
				const runtime = hostedRuntimeFixture();
				delete runtime.providerMode;
				return runtime;
			})(),
		],
	])("rejects mixed provider contract: %s", (_name, runtime) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({ runtimes: { openclaw: runtime } }),
			).success,
		).toBe(false);
	});

	test("rejects the terminal Codex env name for managed runtime providers", () => {
		const provider = {
			...TEST_HOSTED_CODEX_TOOLING.codex.provider,
			runtimeEnvName: "OPENAI_API_KEY",
			apiKeySecretRef: "secret://provider.default.apiKey",
		};
		const manifest = hostedManifestFixture({ providers: { default: provider } });

		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(manifest).success).toBe(false);
	});

	test("rejects terminal Codex with a runtime-provider secret ref", () => {
		const terminalTooling = structuredClone(TEST_HOSTED_CODEX_TOOLING);
		terminalTooling.codex.provider.apiKeySecretRef = "secret://provider.codex-managed.apiKey";
		const manifest = hostedManifestFixture({ terminalTooling });
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(manifest).success).toBe(false);
	});

	test("rejects retired terminal Codex provider shapes", () => {
		const legacyEnv = structuredClone(TEST_HOSTED_CODEX_TOOLING);
		legacyEnv.codex.provider.runtimeEnvName = "OPENAI_API_KEY";
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({ terminalTooling: legacyEnv }),
			).success,
		).toBe(false);

		const legacyCatalog: unknown = {
			...structuredClone(TEST_HOSTED_CODEX_TOOLING),
			codex: {
				...structuredClone(TEST_HOSTED_CODEX_TOOLING.codex),
				provider: {
					...structuredClone(TEST_HOSTED_CODEX_TOOLING.codex.provider),
					models: [{ id: "legacy-codex-model" }],
				},
			},
		};
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({ terminalTooling: legacyCatalog }),
			).success,
		).toBe(false);
	});

	test.each(["openai_chat", "anthropic_messages", "google_generate_content"])(
		"rejects terminal Codex without the fixed responses API mode (%s)",
		(apiMode) => {
			const terminalTooling = structuredClone(TEST_HOSTED_CODEX_TOOLING);
			terminalTooling.codex.provider.apiMode = apiMode;
			const manifest = hostedManifestFixture({ terminalTooling });
			expect(hostedRuntimeBundleV2ManifestSchema.safeParse(manifest).success).toBe(false);
		},
	);

	test("rejects terminal Codex without an API mode", () => {
		const { apiMode: _apiMode, ...provider } = TEST_HOSTED_CODEX_TOOLING.codex.provider;
		const terminalTooling = {
			codex: { ...TEST_HOSTED_CODEX_TOOLING.codex, provider },
		};
		const manifest = hostedManifestFixture({ terminalTooling });
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(manifest).success).toBe(false);
	});

	test.each(["secret://provider.stale.apiKey", "secret://provider.other.apiKey"])(
		"rejects provider secret value %s in unmanaged mode",
		(secretRef) => {
			const runtime = hostedRuntimeFixture({
				providerMode: "unmanaged",
				provider_ids: [],
			});
			delete runtime.primary_model;
			const manifest = hostedManifestFixture({
				providers: {},
				runtimes: { openclaw: runtime },
			});
			expect(() => normalizeHostedBundleFixture(manifest, { [secretRef]: "secret" })).toThrow(
				"unmanaged provider mode must not include provider secret values",
			);
		},
	);

	test("accepts either Codex tool secret-ref alias in unmanaged mode", () => {
		const runtime = hostedRuntimeFixture({ providerMode: "unmanaged", provider_ids: [] });
		delete runtime.primary_model;
		const manifest = hostedManifestFixture({ providers: {}, runtimes: { openclaw: runtime } });
		const codexRef = TEST_HOSTED_CODEX_TOOLING.codex.provider.apiKeySecretRef;
		expect(codexRef).toBeDefined();
		for (const secretRef of [codexRef, `secret://${codexRef}`]) {
			expect(() => normalizeHostedBundleFixture(manifest, { [secretRef]: "secret" })).not.toThrow();
		}
	});

	test.each([
		["missing provider_ids", { provider_ids: undefined }],
		["empty provider_ids", { provider_ids: [] }],
		["duplicate provider_ids", { provider_ids: ["default", "default"] }],
		["more than two provider_ids", { provider_ids: ["default", "secondary", "tertiary"] }],
		["missing primary_model", { primary_model: undefined }],
		[
			"primary model provider outside provider_ids",
			{
				provider_ids: ["default"],
				primary_model: { provider_id: "other", model: "gpt-test" },
			},
		],
	])("rejects hosted runtime with %s", (_name, overrides) => {
		const runtime = hostedRuntimeFixture(overrides);
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({ runtimes: { openclaw: runtime } }),
			).success,
		).toBe(false);
	});

	test.each([
		["missing install", { install: undefined }],
		["remote install channel", { install: { source: "official", channel: "stable" } }],
		["remote install args", { install: { source: "official", args: [] } }],
	])("rejects hosted runtime with %s", (_name, overrides) => {
		const runtime = hostedRuntimeFixture(overrides);
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({ runtimes: { openclaw: runtime } }),
			).success,
		).toBe(false);
	});

	test.each([
		"system.user",
		"system.home",
		"system.workspace",
		"system.persistentPaths",
		"runtime.paths",
	])("rejects obsolete hosted manifest field %s", (field) => {
		const manifest = structuredClone(hostedManifestFixture()) as Record<string, unknown>;
		const system = manifest.system as Record<string, unknown>;
		const runtimes = manifest.runtimes as Record<string, Record<string, unknown>>;
		const runtime = runtimes.openclaw;
		if (field === "system.user") system.user = "clawdi";
		if (field === "system.home") system.home = TEST_HOSTED_HOME;
		if (field === "system.workspace") system.workspace = TEST_HOSTED_HOME;
		if (field === "system.persistentPaths") system.persistentPaths = [TEST_HOSTED_HOME];
		if (field === "runtime.paths") runtime.paths = { home: TEST_HOSTED_HOME };

		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(manifest).success).toBe(false);
	});

	test.each([
		["base_url", { base_url: "https://provider.example.test/v1" }],
		["api_mode", { api_mode: "openai_chat" }],
		["runtime_env_name", { runtime_env_name: "OPENAI_API_KEY" }],
		["api_key_secret_ref", { api_key_secret_ref: "secret://provider.default.apiKey" }],
	])("rejects noncanonical hosted provider field %s", (_name, provider) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({ providers: { default: provider } }),
			).success,
		).toBe(false);
	});

	test.each([
		["empty provider", {}],
		[
			"unsupported kind",
			{
				kind: "anthropic-compatible",
				type: "anthropic",
				baseUrl: "https://api.anthropic.com",
			},
		],
		["kind only", { kind: "openai-compatible" }],
		[
			"error status without error",
			{
				kind: "openai-compatible",
				type: "custom_openai_compatible",
				baseUrl: "https://provider.example.test/v1",
				status: "error",
			},
		],
		[
			"error without error status",
			{
				kind: "openai-compatible",
				type: "custom_openai_compatible",
				baseUrl: "https://provider.example.test/v1",
				error: {
					code: "provider_secret_unavailable",
					message: "provider secret is unavailable",
				},
			},
		],
		[
			"non-not-found error without normal projection",
			{
				kind: "openai-compatible",
				status: "error",
				error: {
					code: "provider_secret_unavailable",
					message: "provider secret is unavailable",
				},
			},
		],
		[
			"provider_not_found without error message",
			{
				kind: "openai-compatible",
				status: "error",
				error: { code: "provider_not_found" },
			},
		],
		[
			"provider_secret_unavailable without error message",
			{
				kind: "openai-compatible",
				type: "anthropic",
				baseUrl: "https://api.anthropic.com",
				status: "error",
				error: { code: "provider_secret_unavailable" },
			},
		],
		[
			"empty error message",
			{
				kind: "openai-compatible",
				status: "error",
				error: { code: "provider_not_found", message: "" },
			},
		],
		[
			"singular model alias",
			{
				kind: "openai-compatible",
				type: "custom_openai_compatible",
				baseUrl: "https://provider.example.test/v1",
				model: "gpt-test",
			},
		],
	])("rejects hosted manifests with %s", (_name, provider) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({ providers: { default: provider } }),
			).success,
		).toBe(false);
	});

	test.each([
		[
			"provider_not_found projection",
			{
				kind: "openai-compatible",
				status: "error",
				error: { code: "provider_not_found", message: "provider is missing" },
			},
		],
		[
			"provider_secret_unavailable projection",
			{
				kind: "openai-compatible",
				type: "anthropic",
				baseUrl: "https://api.anthropic.com",
				apiMode: "anthropic_messages",
				models: [{ id: "claude-opus-4-6" }],
				runtimeEnvName: "ANTHROPIC_API_KEY",
				apiKeyRequired: true,
				status: "error",
				error: {
					code: "provider_secret_unavailable",
					message: "provider secret is unavailable",
				},
			},
		],
		[
			"healthy provider projection",
			{
				kind: "openai-compatible",
				type: "custom_openai_compatible",
				baseUrl: "https://provider.example.test/v1",
				apiMode: "openai_chat",
				models: [{ id: "gpt-test" }],
				apiKeySecretRef: "secret://provider.default.apiKey",
			},
		],
	])("accepts Cloud %s", (_name, provider) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({ providers: { default: provider } }),
			).success,
		).toBe(true);
	});

	test.each([
		"not-an-origin",
		"ftp://app-v2.example.test",
		"https://app-v2.example.test/path",
		"https://user@app-v2.example.test",
	])("rejects invalid OpenClaw Control UI origin %s", (origin) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({
					system: hostedSystemFixture({
						openclawControlUiAllowedOrigins: [origin],
					}),
				}),
			).success,
		).toBe(false);
	});

	test.each([
		{ trustedProxies: ["10.173.0.0/20"] },
		{ trustedProxies: ["incusbr0"] },
		{ trustedProxies: ["10.173.0.1", "10.173.0.1"] },
	])("rejects non-exact OpenClaw trusted proxy IPs $trustedProxies", ({ trustedProxies }) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({
					system: hostedSystemFixture({
						openclawGatewayTrustedProxies: trustedProxies,
					}),
				}),
			).success,
		).toBe(false);
	});

	test("accepts legacy OpenClaw manifests without trusted proxies and omits the patch", () => {
		const system = hostedSystemFixture();
		delete system.openclawGatewayTrustedProxies;
		const manifest = hostedRuntimeBundleV2ManifestSchema.parse(hostedManifestFixture({ system }));

		const patch = openClawGatewayHostedPatch(manifest, TEST_HOSTED_SECRET_VALUES, false);

		expect(patch).not.toBeNull();
		expect(patch).not.toHaveProperty("gateway.trustedProxies");
	});

	test("preserves canonical OpenClaw Control UI origins through gateway projection", () => {
		const paths = tempRuntimePaths();
		const openclawBin = join(paths.userHome, ".local", "bin", "openclaw");
		const patchPath = join(paths.serviceStateRoot, "openclaw-gateway-patch.json");
		const allowedOrigins = ["https://app-v2-18789.k3s.example.test"];
		process.env.OPENCLAW_GATEWAY_TOKEN = "gateway-token";
		mkdirSync(dirname(openclawBin), { recursive: true });
		writeFileSync(
			openclawBin,
			[
				"#!/bin/sh",
				'if [ "$1" = "--version" ]; then',
				`  printf '%s\\n' 'OpenClaw test-version'`,
				"  exit 0",
				"fi",
				'if [ "$1 $2 $3" = "agents list --json" ]; then',
				'  printf \'[{"id":"main","workspace":"%s"}]\\n\' "$HOME/.openclaw/workspace"',
				"  exit 0",
				"fi",
				'if [ "$1 $2 $3" = "config patch --stdin" ]; then',
				`  cat > '${patchPath}'`,
				"  exit 0",
				"fi",
				"exit 0",
				"",
			].join("\n"),
		);
		chmodSync(openclawBin, 0o700);

		const hosted = hostedRuntimeBundleV2ManifestSchema.parse(
			hostedManifestFixture({
				system: hostedSystemFixture({
					openclawControlUiAllowedOrigins: allowedOrigins,
				}),
				runtimes: {
					openclaw: hostedRuntimeFixture(),
				},
			}),
		);
		const normalized: RuntimeManifest = {
			...hosted,
			egressEngine: installCachedTestEgressEngine(paths, "12.2.3-test-control-ui"),
		};
		expect(normalized.projection?.system).toEqual(
			hostedManifestFixture({
				system: hostedSystemFixture({
					openclawControlUiAllowedOrigins: allowedOrigins,
				}),
			}).system,
		);

		const result = convergeRuntimeManifest(
			manifestLoad(normalized, "inline-hosted-control-ui-origins"),
			paths,
		);

		expect(result.installErrors).toEqual([]);
		const controlUiPatch = JSON.parse(readFileSync(patchPath, "utf8"));
		expect(controlUiPatch).toMatchObject({
			gateway: {
				port: 18789,
				bind: "lan",
				trustedProxies: ["10.173.0.1"],
				auth: { mode: "token", token: "gateway-token" },
				controlUi: {
					allowedOrigins,
					dangerouslyAllowHostHeaderOriginFallback: false,
				},
			},
		});
		// Retired in current openclaw; emitting it fails strict config validation
		// and convergence never writes manifest.last-good.json. toMatchObject
		// ignores extra keys, so the absence has to be asserted outright.
		expect(controlUiPatch.gateway.controlUi).not.toHaveProperty("allowInsecureAuth");
	});

	test("projects hosted OpenClaw v2 direct token auth", () => {
		const paths = tempRuntimePaths();
		const openclawBin = join(paths.userHome, ".local", "bin", "openclaw");
		const patchPath = join(paths.serviceStateRoot, "openclaw-native-auth-patch.json");
		const openclawPackageRoot = join(
			paths.userHome,
			".local",
			"tools",
			"node",
			"lib",
			"node_modules",
			"openclaw",
		);
		mkdirSync(dirname(openclawBin), { recursive: true });
		mkdirSync(openclawPackageRoot, { recursive: true });
		writeFileSync(
			join(openclawPackageRoot, "device-bootstrap.mjs"),
			"export const normalizeDeviceBootstrapProfile = (profile) => profile;\n",
		);
		writeFileSync(
			join(openclawPackageRoot, "package.json"),
			JSON.stringify({
				name: "openclaw",
				type: "module",
				exports: { "./plugin-sdk/device-bootstrap": "./device-bootstrap.mjs" },
			}),
		);
		writeFileSync(
			openclawBin,
			[
				"#!/bin/sh",
				'if [ "$1" = "--version" ]; then',
				`  printf '%s\\n' 'OpenClaw test-version'`,
				"  exit 0",
				"fi",
				'if [ "$1 $2 $3" = "agents list --json" ]; then',
				'  printf \'[{"id":"main","workspace":"%s"}]\\n\' "$HOME/.openclaw/workspace"',
				"  exit 0",
				"fi",
				'if [ "$1 $2 $3" = "config patch --stdin" ]; then',
				`  cat > '${patchPath}'`,
				"  exit 0",
				"fi",
				"exit 0",
				"",
			].join("\n"),
		);
		chmodSync(openclawBin, 0o700);

		const projected = hostedRuntimeBundleV2ManifestSchema.parse(
			hostedOpenClawV2ManifestFixture({}, LEGACY_HOSTED_OPENCLAW_GATEWAY_RUN_ARGS),
		);
		expect(projected.runtimes.openclaw.run?.args).toEqual(["gateway", "run"]);
		const normalized: RuntimeManifest = {
			...projected,
			egressEngine: installCachedTestEgressEngine(paths, "12.2.3-test-native-auth"),
		};
		expect(() =>
			convergeRuntimeManifest(
				manifestLoad(normalized, "inline-hosted-openclaw-native-auth-missing-token", {}),
				paths,
			),
		).toThrow("Runtime secret secret://runtime/openclaw/gateway-token is unavailable");
		expect(existsSync(patchPath)).toBe(false);
		const result = convergeRuntimeManifest(
			manifestLoad(normalized, "inline-hosted-openclaw-native-auth", {
				"secret://runtime/openclaw/gateway-token": "gateway-token",
				"secret://tool.codex.apiKey": "test-codex-provider-key",
			}),
			paths,
		);

		expect(result.installErrors).toEqual([]);
		const gatewayPatch = JSON.parse(readFileSync(patchPath, "utf8"));
		expect(gatewayPatch).toMatchObject({
			gateway: {
				port: 18789,
				bind: "lan",
				auth: {
					mode: "token",
					token: "gateway-token",
				},
				controlUi: {
					basePath: "/control",
					allowedOrigins: ["https://agent.example.test"],
					dangerouslyAllowHostHeaderOriginFallback: false,
					dangerouslyDisableDeviceAuth: null,
				},
			},
		});
		// Retired in current openclaw; emitting it fails strict config validation
		// and convergence never writes manifest.last-good.json. toMatchObject
		// ignores extra keys, so the absence has to be asserted outright.
		expect(gatewayPatch.gateway.controlUi).not.toHaveProperty("allowInsecureAuth");
		expect(JSON.stringify(gatewayPatch)).toContain("gateway-token");
		const gatewayEnv = readFileSync(
			join(paths.systemdEnvRoot, "openclaw-gateway.service.env"),
			"utf8",
		);
		expect(gatewayEnv).not.toContain("OPENCLAW_GATEWAY_TOKEN");
		expect(gatewayEnv).not.toContain("gateway-token");
		const gatewayDropIn = readFileSync(
			join(paths.systemdUserRoot, "openclaw-gateway.service.d", RUNTIME_SYSTEMD_DROP_IN_FILE),
			"utf8",
		);
		expect(gatewayDropIn).not.toContain("\nExecStart=");
		expect(gatewayDropIn).not.toContain("\nWorkingDirectory=");
		expect(result.outputs.systemdSystemUnits.map((path) => path.split("/").at(-1))).toContain(
			`${RUNTIME_SYSTEM_UNIT_SIDECAR}.service`,
		);
	});

	test("rejects hosted manifests without an explicit CLI package policy", () => {
		expect(() =>
			hostedRuntimeBundleV2ManifestSchema.parse({
				schemaVersion: "clawdi.hosted-runtime.manifest.v1",
				runtime: "openclaw",
				deploymentId: "hdep_missing_cli_policy",
				environmentId: "env_missing_cli_policy",
				instanceId: "hri_missing_cli_policy",
				generation: 1,
				issuedAt: "2026-07-11T00:00:00.000Z",
				locale: TEST_HOSTED_LOCALE,
				controlPlane: { cloudApiUrl: "https://cloud-api.example.test" },
				runtimes: { openclaw: { enabled: true } },
			}),
		).toThrow(/clawdiCli/);
	});

	test.each([
		["missing environmentId", {}],
		["appId fallback", { appId: "app_legacy_identity" }],
	])("rejects hosted manifests with %s", (_name, identity) => {
		const manifest = hostedManifestFixture(identity);
		delete manifest.environmentId;
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(manifest).success).toBe(false);
	});

	test("uses only the hosted environmentId as the runtime environment identity", () => {
		const parsed = hostedRuntimeBundleV2ManifestSchema.parse(
			hostedManifestFixture({
				deploymentId: "hdep_distinct_identity",
				environmentId: "env_canonical_identity",
			}),
		);

		expect(parsed.environmentId).toBe("env_canonical_identity");
	});

	test.each([
		["missing cloudApiUrl", {}],
		[
			"manifestUrl",
			{
				cloudApiUrl: "https://cloud-api.example.test",
				manifestUrl: "https://cloud-api.example.test/v1/runtime/manifest",
			},
		],
		[
			"apiUrl",
			{
				cloudApiUrl: "https://cloud-api.example.test",
				apiUrl: "https://cloud-api.example.test",
			},
		],
		["unknown key", { cloudApiUrl: "https://cloud-api.example.test", unknown: true }],
	])("rejects hosted controlPlane with %s", (_name, controlPlane) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(hostedManifestFixture({ controlPlane }))
				.success,
		).toBe(false);
	});

	test.each([
		{
			name: "wrong source",
			clawdiCli: {
				source: "npm:other",
				packageSpec: "clawdi@1.2.3-test",
				registry: "https://registry.npmjs.org",
			},
		},
		{
			name: "missing registry",
			clawdiCli: { source: "npm:clawdi", packageSpec: "clawdi@1.2.3-test" },
		},
		{
			name: "non-official registry",
			clawdiCli: {
				source: "npm:clawdi",
				packageSpec: "clawdi@1.2.3-test",
				registry: "https://registry.example.test",
			},
		},
		{
			name: "dead managed flags",
			clawdiCli: {
				source: "npm:clawdi",
				packageSpec: "clawdi@1.2.3-test",
				registry: "https://registry.npmjs.org",
				managedConfig: true,
				userEditableConfig: false,
			},
		},
	])("rejects hosted CLI policy with $name", ({ clawdiCli }) => {
		const valid = hostedManifestFixture();
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(valid).success).toBe(true);
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse({ ...valid, clawdiCli }).success).toBe(
			false,
		);
	});

	test.each(["clawdi@1.2.3-test", "clawdi@1.2.3-rc-1.2", "clawdi@1.2.3"])(
		"accepts exact hosted CLI package spec %s",
		(packageSpec) => {
			expect(
				hostedRuntimeBundleV2ManifestSchema.safeParse(
					hostedManifestFixture({
						clawdiCli: {
							source: "npm:clawdi",
							packageSpec,
							registry: "https://registry.npmjs.org",
						},
					}),
				).success,
			).toBe(true);
		},
	);

	test("enforces the Cloud package spec length limit", () => {
		const atLimit = `clawdi@1.2.3-${"a".repeat(187)}`;
		const overLimit = `clawdi@1.2.3-${"a".repeat(188)}`;
		expect(atLimit).toHaveLength(200);
		expect(overLimit).toHaveLength(201);

		for (const packageSpec of [atLimit, overLimit]) {
			const manifest = hostedManifestFixture({
				clawdiCli: {
					source: "npm:clawdi",
					packageSpec,
					registry: "https://registry.npmjs.org",
				},
			});
			const expected = packageSpec === atLimit;
			expect(hostedRuntimeBundleV2ManifestSchema.safeParse(manifest).success).toBe(expected);
		}
	});

	test("rejects raw secretValues keys in the Hosted fixture contract", () => {
		expect(
			z.record(canonicalSecretRefSchema, z.string()).safeParse({
				"tool.codex.apiKey": "must-be-rejected",
			}).success,
		).toBe(false);
	});

	test.each([
		"clawdi@agent-v2",
		"clawdi@latest",
		"clawdi@beta",
		"clawdi",
		"clawdi@candidate",
		"clawdi@1.2.3+build.1",
		"clawdi@1.2.3-beta..1",
		"clawdi@1.2.3-beta.",
		"clawdi@1.2.3-.beta",
		"clawdi@1.2.3-01",
		"clawdi@01.2.3",
		"./clawdi.tgz",
		"/tmp/clawdi.tgz",
		"/usr/local/share/clawdi/bootstrap/clawdi-1.2.3-test.tgz",
		"/usr/local/share/clawdi/bootstrap/../clawdi.tgz",
		"/usr/local/share/clawdi/bootstrap/nested/clawdi.tgz",
		"/usr/local/share/clawdi/bootstrap/clawdi..tgz",
	])("rejects hosted CLI package spec %s", (packageSpec) => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(
				hostedManifestFixture({
					clawdiCli: {
						source: "npm:clawdi",
						packageSpec,
						registry: "https://registry.npmjs.org",
					},
				}),
			).success,
		).toBe(false);
	});

	test("normalizes hosted manifest responses into runtime desired state without embedding secrets", () => {
		const hostedResponse = {
			manifest: {
				schemaVersion: "clawdi.hosted-runtime.manifest.v1",
				runtime: "openclaw",
				deploymentId: "hdep_normalize",
				environmentId: "env_normalize",
				instanceId: "hri_normalize",
				generation: 7,
				issuedAt: "2026-07-01T00:00:00.000Z",
				locale: TEST_HOSTED_LOCALE,
				system: hostedSystemFixture(),
				controlPlane: {
					cloudApiUrl: "https://cloud-api.example.test",
				},
				clawdiCli: {
					source: "npm:clawdi",
					packageSpec: "clawdi@1.2.3-test",
					registry: "https://registry.npmjs.org",
				},
				runtimes: {
					openclaw: {
						enabled: true,
						providerMode: "configured",
						provider_ids: ["default"],
						primary_model: { provider_id: "default", model: "gpt-test" },
						install: { source: "official" },
						run: {
							args: ["gateway", "run"],
							secretEnv: {
								OPENCLAW_GATEWAY_TOKEN: "secret://runtime/openclaw/gateway-token",
							},
						},
					},
				},
				providers: {
					default: {
						kind: "openai-compatible",
						type: "custom_openai_compatible",
						baseUrl: "https://api.example.test/v1",
						models: [{ id: "gpt-test" }],
						apiMode: "openai_chat",
						apiKeySecretRef: "secret://providers/default/api-key",
					},
				} satisfies HostedRuntimeBundleV2Manifest["providers"],
				terminalTooling: TEST_HOSTED_CODEX_TOOLING,
				liveSync: {
					enabled: true,
					agents: [{ agentType: "openclaw", environmentId: "env_normalize" }],
				},
				egressProfiles: {
					profiles: [
						{
							id: "api-proxy",
							enabled: true,
							kind: "http",
							match: {
								scheme: "https",
								host: "api.example.test",
								pathPrefix: "/v1",
								headers: {},
								query: {},
							},
							rewrite: {
								upstreamBaseUrl: "https://upstream.example.test/v1",
								preservePath: true,
								setHeaders: {
									authorization: {
										type: "secretRef",
										secretRef: "secret://providers/default/api-key",
										prefix: "Bearer ",
									},
								},
							},
							logging: { redactHeaders: ["authorization"], redactUrlPatterns: [] },
							priority: 120,
						},
					],
				},
				recovery: { cacheManifest: true, allowOfflineBoot: true },
			},
			secretValues: {
				"secret://providers/default/api-key": "sk-normalized",
			},
		};

		const hostedManifest = hostedRuntimeBundleV2ManifestSchema.parse(hostedResponse.manifest);
		const normalized = {
			manifest: hostedManifest,
			secretValues: normalizeSecretValues(hostedResponse.secretValues),
		};
		expect(normalized.manifest.schemaVersion).toBe("clawdi.runtimeDesiredState.v1");
		expect(normalized.manifest.runtime).toBe("openclaw");
		expect(Object.keys(normalized.manifest.runtimes)).toEqual(["openclaw"]);
		expect(normalized.manifest.runtimes.openclaw.enabled).toBe(true);
		expect(normalized.manifest.runtimes.openclaw.updateChannel).toBeUndefined();
		const install = normalized.manifest.runtimes.openclaw.install;
		expect(install?.url).toBe(OFFICIAL_INSTALL_URLS.openclaw);
		expect(install?.args).toEqual(officialInstallArgs("openclaw", install?.home ?? ""));
		expect(install?.args).not.toContain("--version");
		expect(normalized.manifest.runtimes.openclaw.run?.args).toEqual(["gateway", "run"]);
		expect(normalized.manifest.runtimes.openclaw.run?.secretEnv).toEqual({
			OPENCLAW_GATEWAY_TOKEN: "secret://runtime/openclaw/gateway-token",
		});
		expect(normalized.manifest.projection?.providers).toEqual(hostedResponse.manifest.providers);
		expect(normalized.manifest.egressProfiles?.profiles.map((profile) => profile.id)).toContain(
			"api-proxy",
		);
		expect(normalized.manifest.liveSync).toEqual(hostedResponse.manifest.liveSync);
		expect("secretValues" in normalized.manifest).toBe(false);
		expect(normalized.secretValues).toEqual({
			"secret://providers/default/api-key": "sk-normalized",
		});
	});

	test("rejects a missing explicit runtime even with one runtime entry", () => {
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse({
				schemaVersion: "clawdi.hosted-runtime.manifest.v1",
				deploymentId: "hdep_infer_runtime",
				environmentId: "env_infer_runtime",
				instanceId: "hri_infer_runtime",
				generation: 1,
				issuedAt: "2026-07-07T00:00:00.000Z",
				locale: TEST_HOSTED_LOCALE,
				system: hostedSystemFixture(),
				controlPlane: {
					cloudApiUrl: "https://cloud-api.example.test",
				},
				clawdiCli: {
					source: "npm:clawdi",
					packageSpec: "clawdi@1.2.3-test",
					registry: "https://registry.npmjs.org",
				},
				providers: {
					default: {
						kind: "openai-compatible",
						status: "error",
						error: { code: "provider_not_found", message: "provider is missing" },
					},
				},
				terminalTooling: TEST_HOSTED_CODEX_TOOLING,
				liveSync: { enabled: false, agents: [] },
				recovery: { cacheManifest: true, allowOfflineBoot: true },
				runtimes: {
					openclaw: hostedRuntimeFixture(),
				},
			}).success,
		).toBe(false);
	});

	test.each([
		["top level", (manifest: Record<string, unknown>) => ({ ...manifest, unknown: true })],
		[
			"system",
			(manifest: Record<string, unknown>) => ({
				...manifest,
				system: hostedSystemFixture({ unknown: true }),
			}),
		],
		[
			"control plane",
			(manifest: Record<string, unknown>) => ({
				...manifest,
				controlPlane: {
					...(manifest.controlPlane as Record<string, unknown>),
					unknown: true,
				},
			}),
		],
		[
			"runtime entry",
			(manifest: Record<string, unknown>) => ({
				...manifest,
				runtimes: {
					openclaw: {
						...((manifest.runtimes as Record<string, unknown>).openclaw as Record<string, unknown>),
						unknown: true,
					},
				},
			}),
		],
		[
			"runtime run settings",
			(manifest: Record<string, unknown>) => ({
				...manifest,
				runtimes: {
					openclaw: {
						...((manifest.runtimes as Record<string, unknown>).openclaw as Record<string, unknown>),
						run: {
							...(hostedRuntimeFixture().run as Record<string, unknown>),
							unknown: true,
						},
					},
				},
			}),
		],
	])("rejects unknown hosted manifest fields at the %s", (_name, addUnknownField) => {
		const cleanManifest = hostedManifestFixture();
		expect(hostedRuntimeBundleV2ManifestSchema.safeParse(cleanManifest).success).toBe(true);

		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse(addUnknownField(cleanManifest)).success,
		).toBe(false);
	});

	test("rejects hosted manifests that still declare multiple execution runtimes", () => {
		expect(() =>
			hostedRuntimeBundleV2ManifestSchema.parse({
				schemaVersion: "clawdi.hosted-runtime.manifest.v1",
				runtime: "openclaw",
				deploymentId: "hdep_multi",
				environmentId: "env_multi",
				instanceId: "hri_multi",
				generation: 1,
				issuedAt: "2026-07-01T00:00:00.000Z",
				locale: TEST_HOSTED_LOCALE,
				system: hostedSystemFixture(),
				controlPlane: {
					cloudApiUrl: "https://cloud-api.example.test",
				},
				clawdiCli: {
					source: "npm:clawdi",
					packageSpec: "clawdi@1.2.3-test",
					registry: "https://registry.npmjs.org",
				},
				providers: {
					default: {
						kind: "openai-compatible",
						status: "error",
						error: { code: "provider_not_found", message: "provider is missing" },
					},
				},
				terminalTooling: TEST_HOSTED_CODEX_TOOLING,
				liveSync: { enabled: false, agents: [] },
				recovery: { cacheManifest: true, allowOfflineBoot: true },
				runtimes: {
					openclaw: {
						enabled: true,
						install: { source: "official" },
						providerMode: "configured",
						provider_ids: ["default"],
						primary_model: { provider_id: "default", model: "gpt-test" },
						run: {
							args: ["gateway", "run"],
							secretEnv: {
								OPENCLAW_GATEWAY_TOKEN: "secret://runtime/openclaw/gateway-token",
							},
						},
						services: {},
					},
					hermes: {
						enabled: true,
						install: { source: "official" },
						providerMode: "configured",
						provider_ids: ["default"],
						primary_model: { provider_id: "default", model: "gpt-test" },
						run: { args: ["gateway", "run"] },
						services: {
							dashboard: {
								args: ["dashboard", "--host", "0.0.0.0", "--port", "9119", "--no-open"],
							},
						},
					},
				},
			}),
		).toThrow("hosted runtime manifests must declare exactly one selected runtime");
	});

	test("converges OpenClaw native token auth from canonical bundle secret refs", () => {
		const paths = tempRuntimePaths();
		writeFakeGatewayCli({
			path: join(paths.userHome, ".local", "bin", "openclaw"),
			runtime: "openclaw",
			unitPath: join(paths.systemdUserRoot, "openclaw-gateway.service"),
		});
		const manifest = baseManifest(
			paths,
			{
				openclaw: {
					enabled: true,
					run: {
						command: "openclaw",
						args: ["gateway", "run"],
						env: {},
						secretEnv: {
							OPENCLAW_GATEWAY_TOKEN: "secret://runtime/openclaw/gateway-token",
						},
						prependPath: [],
					},
					services: {},
				},
			},
			{ runtime: "openclaw" },
		);

		const secretValues = {
			"secret://runtime/openclaw/gateway-token": "gateway-token",
		};
		const result = convergeRuntimeManifest(
			manifestLoad(manifest, "inline-openclaw", secretValues),
			paths,
		);

		expect(result.installErrors).toEqual([]);
		expect(result.enabledRuntimes).toEqual(["openclaw"]);
		expect(result.outputs.systemdUserUnits.map((path) => path.split("/").at(-1))).toEqual([
			"openclaw-gateway.service",
		]);
		const runConfig = JSON.parse(readFileSync(runtimeRunConfigPath("openclaw", paths), "utf8")) as {
			defaultArgs?: string[];
			secretEnv?: Record<string, string>;
			secretFilePath?: string | null;
		};
		expect(runConfig.defaultArgs).toEqual(["gateway", "run"]);
		expect(runConfig.secretEnv).toEqual({
			OPENCLAW_GATEWAY_TOKEN: "secret://runtime/openclaw/gateway-token",
		});
		expect(runConfig.secretFilePath).toBeNull();
		expect(runtimeSecretValue(secretValues, "secret://runtime/openclaw/gateway-token")).toBe(
			"gateway-token",
		);
		const unit = readFileSync(
			join(paths.systemdUserRoot, "openclaw-gateway.service.d", RUNTIME_SYSTEMD_DROP_IN_FILE),
			"utf8",
		);
		expect(unit).not.toContain("\nExecStart=");
		expect(unit).not.toContain("\nWorkingDirectory=");
		const envFile = readFileSync(
			join(paths.systemdEnvRoot, "openclaw-gateway.service.env"),
			"utf8",
		);
		expect(envFile).not.toContain("OPENCLAW_GATEWAY_TOKEN");
	});

	test.each(["switch", "unbind"])("recovers a native key after failed commit then %s", (action) => {
		const paths = tempRuntimePaths();
		const configPath = writeFakeOpenClawConfigMutationSdk(paths.userHome, {
			initialConfig: {
				agents: { defaults: { model: { primary: "google/user-selected-model" } } },
				auth: { profiles: { personal: { provider: "google", mode: "api_key" } } },
				secrets: {
					providers: { default: { source: "file", path: "/user/secrets.json" } },
					defaults: { file: "default" },
				},
				models: {
					mode: "replace",
					providers: { google: { timeoutSeconds: 45, models: [{ id: "user-selected-model" }] } },
				},
				plugins: { entries: { google: { config: { webSearch: { enabled: false } } } } },
			},
		});
		const command = join(paths.userHome, ".local", "bin", "openclaw");
		const baseCommand = `${command}-base`;
		const pluginState = join(paths.userHome, "plugin-state.json");
		const pluginLog = join(paths.userHome, "plugin-commands.log");
		writeFileSync(pluginState, '{"plugins":[]}');
		writeFakeGatewayCli({
			path: command,
			runtime: "openclaw",
			unitPath: join(paths.systemdUserRoot, "openclaw-gateway.service"),
		});
		cpSync(command, baseCommand);
		writeFileSync(
			command,
			`#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = "plugins" ]; then
  printf '%s\\n' "$*" >> '${pluginLog}'
  case "$*" in
    "plugins list --json") cat '${pluginState}' ;;
    "plugins install --help"|"plugins enable --help") printf '%s\\n' '--accept-capabilities' ;;
    "plugins install @openclaw/google-plugin --force --accept-capabilities") printf '%s' '{"plugins":[{"id":"google","enabled":false,"status":"disabled"}]}' > '${pluginState}' ;;
    "plugins enable google --accept-capabilities") printf '%s' '{"plugins":[{"id":"google","enabled":true,"status":"loaded"}]}' > '${pluginState}' ;;
    *) exit 42 ;;
  esac
elif [ "$1 $2 $3" = "config patch --stdin" ]; then
  printf '%s\\n' "$*" >> '${pluginLog}'
  python3 -c '
import json, sys
path = sys.argv[1]
with open(path) as f: current = json.load(f)
def merge(target, patch):
    for key, value in patch.items():
        if value is None: target.pop(key, None)
        elif isinstance(value, dict):
            if not isinstance(target.get(key), dict): target[key] = {}
            merge(target[key], value)
        else: target[key] = value
merge(current, json.load(sys.stdin))
with open(path, "w") as f: json.dump(current, f)
' '${configPath}'
else
  exec '${baseCommand}' "$@"
fi
`,
		);
		const provider = {
			kind: "openai-compatible",
			type: "gemini",
			configurationMode: "native",
			nativeProvider: "google",
			baseUrl: "https://generativelanguage.googleapis.com/v1beta",
			apiMode: "google_generate_content",
			runtimeEnvName: "GEMINI_API_KEY",
			apiKeySecretRef: "secret://provider.google.apiKey",
		};
		const manifest = hostedRuntimeBundleV2ManifestSchema.parse(
			hostedOpenClawV2ManifestFixture({
				providers: { default: provider },
				runtimes: { openclaw: hostedRuntimeFixture({ primary_model: null }) },
			}),
		);
		const egressEngine = installCachedTestEgressEngine(paths, "12.2.3-native-google");
		const connected = convergeRuntimeManifest(
			manifestLoad({ ...manifest, egressEngine }, "native-google", {
				...TEST_HOSTED_SECRET_VALUES,
				"secret://provider.google.apiKey": "native-google-test-key",
			}),
			paths,
			{
				commitAuthority: () => {
					throw new Error("Injected authority commit failure");
				},
			},
		);
		expect(connected.installErrors.join("\n")).toContain("Injected authority commit failure");
		expect(readRuntimeAppliedState(paths)).toBeNull();
		const configured = JSON.parse(readFileSync(configPath, "utf8"));
		expect(configured.agents.defaults.model.primary).toBe("google/user-selected-model");
		expect(configured.auth.profiles.personal).toEqual({ provider: "google", mode: "api_key" });
		expect(configured.models.providers.google).toEqual({
			timeoutSeconds: 45,
			models: [{ id: "user-selected-model" }],
			baseUrl: provider.baseUrl,
			auth: "api-key",
			apiKey: { source: "env", provider: "clawdi-native", id: "GEMINI_API_KEY" },
		});
		expect(configured.plugins.entries.google.config).toEqual({ webSearch: { enabled: false } });
		expect(configured.secrets.providers.default).toEqual({
			source: "file",
			path: "/user/secrets.json",
		});
		expect(configured.secrets.defaults).toEqual({ file: "default" });
		expect(readFileSync(pluginLog, "utf8")).toContain(
			"plugins enable google --accept-capabilities",
		);
		expect(readFileSync(pluginLog, "utf8")).toContain("config patch --stdin");
		expect(readFileSync(pluginLog, "utf8")).not.toContain("native-google-test-key");
		expect(readFileSync(configPath, "utf8")).not.toContain("native-google-test-key");

		writeFileSync(
			pluginState,
			JSON.stringify({
				plugins: [{ id: "anthropic", enabled: true, status: "loaded" }],
			}),
		);
		const nextProvider = {
			...provider,
			type: "anthropic",
			nativeProvider: "anthropic",
			baseUrl: "https://api.anthropic.com",
			apiMode: "anthropic_messages",
			runtimeEnvName: "ANTHROPIC_API_KEY",
			apiKeySecretRef: "secret://provider.anthropic.apiKey",
		};
		const nextRuntime = hostedRuntimeFixture({
			primary_model: null,
			...(action === "unbind" ? { providerMode: "unmanaged", provider_ids: [] } : {}),
		});
		if (action === "unbind") delete nextRuntime.primary_model;
		const nextManifest = hostedRuntimeBundleV2ManifestSchema.parse(
			hostedOpenClawV2ManifestFixture({
				providers: action === "switch" ? { default: nextProvider } : {},
				runtimes: { openclaw: nextRuntime },
			}),
		);
		const nextLoad = manifestLoad(
			{ ...nextManifest, generation: 2, egressEngine },
			"native-recovery",
			{
				...TEST_HOSTED_SECRET_VALUES,
				...(action === "switch"
					? { "secret://provider.anthropic.apiKey": "next-native-test-key" }
					: {}),
			},
		);
		const recovered = convergeRuntimeManifest(nextLoad, paths);
		expect(recovered.installErrors).toEqual([]);
		expect(recovered.nativeCredentialProviderIds?.openclaw).toEqual(
			action === "switch" ? ["anthropic"] : [],
		);
		const restored = JSON.parse(readFileSync(configPath, "utf8"));
		expect(restored.models.providers.google).toEqual({
			timeoutSeconds: 45,
			models: [{ id: "user-selected-model" }],
		});
		expect(restored.agents.defaults.model).toEqual(configured.agents.defaults.model);
		expect(restored.auth).toEqual(configured.auth);
		if (action === "switch")
			expect(restored.models.providers.anthropic.apiKey).toEqual({
				source: "env",
				provider: "clawdi-native",
				id: "ANTHROPIC_API_KEY",
			});
		const retried = convergeRuntimeManifest(nextLoad, paths);
		expect(retried.installErrors).toEqual([]);
		expect(JSON.parse(readFileSync(configPath, "utf8"))).toEqual(restored);
	});

	test.each(["update", "delete"])(
		"guards channel %s against credential replacement before native mutation",
		(operation) => {
			const paths = tempRuntimePaths();
			const managed = {
				enabled: true,
				botToken: { source: "env", provider: "default", id: "CLAWDI_CHANNEL_TEST_AGENT_TOKEN" },
			};
			const previous = {
				telegram: { enabled: true, defaultAccount: "managed", accounts: { managed } },
			};
			const edited = {
				channels: {
					telegram: {
						defaultAccount: "personal",
						accounts: {
							managed: {
								enabled: true,
								botToken: "native-replacement",
								dmPolicy: "pairing",
								allowFrom: ["owner"],
							},
							personal: { enabled: true, botToken: "native-personal" },
						},
					},
				},
				session: { dmScope: "per-channel-peer" },
			};
			const configPath = writeFakeOpenClawConfigMutationSdk(paths.userHome, {
				initialConfig: { channels: previous },
				beforeMutation: edited,
			});
			const context = createOpenClawHostedContext(baseManifest(paths, {}), paths.userHome);
			const apply = () =>
				applyOpenClawHostedChannelPatch(
					openClawManagedChannelsPatch(operation === "update" ? previous : {}),
					previous,
					operation === "update" ? [managed.botToken.id] : [],
					context,
					paths.userHome,
				);
			if (operation === "update") expect(apply).toThrow("ownership changed");
			else apply();
			const result = JSON.parse(readFileSync(configPath, "utf8"));
			expect(result.channels).toEqual(edited.channels);
			expect(result.session).toEqual(edited.session);
		},
	);

	test("native channel unlink removes only committed accounts and rejects unowned missing refs", () => {
		const paths = tempRuntimePaths();
		const managed = {
			enabled: true,
			botToken: { source: "env", provider: "default", id: "CLAWDI_CHANNEL_TEST_AGENT_TOKEN" },
		};
		const previous = {
			telegram: { enabled: true, defaultAccount: "managed", accounts: { managed } },
		};
		const native = { enabled: true, botToken: "native-personal", dmPolicy: "pairing" };
		const original = {
			channels: { telegram: { ...previous.telegram, accounts: { managed, personal: native } } },
		};
		const configPath = writeFakeOpenClawConfigMutationSdk(paths.userHome, {
			initialConfig: original,
		});
		const context = createOpenClawHostedContext(baseManifest(paths, {}), paths.userHome);
		expect(() =>
			applyOpenClawHostedChannelPatch(
				openClawManagedChannelsPatch({}),
				null,
				[],
				context,
				paths.userHome,
			),
		).toThrow("withdrawn managed credential");
		expect(JSON.parse(readFileSync(configPath, "utf8"))).toEqual(original);
		const command = join(paths.userHome, ".local", "bin", "openclaw");
		writeFakeGatewayCli({
			path: command,
			runtime: "openclaw",
			unitPath: join(paths.systemdUserRoot, "openclaw-gateway.service"),
		});
		const desired = baseManifest(
			paths,
			{
				openclaw: {
					enabled: true,
					services: {},
					providerMode: "unmanaged",
					run: runSettings(command, ["gateway", "run"]),
				},
			},
			{ projection: { channels: {} } },
		);
		const failed = convergeRuntimeManifest(
			manifestLoad(desired, "missing-channel-ownership"),
			paths,
		);
		expect(failed.installErrors.join("\n")).toContain("withdrawn managed credential");
		expect(failed.outputs.manifestLastGood).toBeNull();
		expect(existsSync(paths.appliedState)).toBe(false);
		expect(existsSync(paths.manifestLastGood)).toBe(false);
		applyOpenClawHostedChannelPatch(
			openClawManagedChannelsPatch({}),
			previous,
			[],
			context,
			paths.userHome,
		);
		const result = JSON.parse(readFileSync(configPath, "utf8"));
		expect(result.channels.telegram.accounts).toEqual({ personal: native });
		expect(result.channels.telegram).not.toHaveProperty("defaultAccount");
	});

	test.each(["legacy", "current"])(
		"preserves user memory selection in the %s layout and keeps the provider key out of agent env",
		(layout) => {
			const paths = tempRuntimePaths();
			const search = {
				provider: "local",
				model: "user-embedding-model",
				cache: { enabled: false },
				query: { hybrid: { enabled: true, vectorWeight: 0.7 }, maxResults: 9 },
			};
			const configPath = writeFakeOpenClawConfigMutationSdk(paths.userHome, {
				initialConfig:
					layout === "legacy"
						? {
								agents: {
									defaults: {
										memorySearch: { ...search, query: { hybrid: search.query.hybrid } },
									},
								},
								memory: { search: { query: { maxResults: 9 } } },
							}
						: { memory: { search } },
			});
			writeFakeGatewayCli({
				path: join(paths.userHome, ".local", "bin", "openclaw"),
				runtime: "openclaw",
				unitPath: join(paths.systemdUserRoot, "openclaw-gateway.service"),
			});
			const hosted = hostedRuntimeBundleV2ManifestSchema.parse(
				hostedManifestFixture({
					providers: {
						default: {
							kind: "openai-compatible",
							type: "custom_openai_compatible",
							managed_by: "clawdi",
							baseUrl: "https://api.example.test/v1",
							models: [
								{ id: "gpt-test" },
								{
									id: "test-embedding-model",
									capabilities: { embeddings: true, chat: false },
								},
							],
							apiMode: "openai_responses",
							runtimeEnvName: "CLAWDI_AI_API_KEY",
							apiKeySecretRef: "secret://providers/default/api-key",
						},
					},
				}),
			);
			const manifest = {
				...hosted,
				egressEngine: installCachedTestEgressEngine(paths, "12.2.3-test-provider-model"),
			};
			const provider = hostedAiProviderCatalog(manifest, "openclaw")?.catalog.providers[0];
			expect(provider?.runtime_env_name).toBe("CLAWDI_AI_API_KEY");

			const result = convergeRuntimeManifest(
				manifestLoad(manifest, "inline-managed-provider", {
					...TEST_HOSTED_SECRET_VALUES,
					"secret://providers/default/api-key": "sk-managed",
				}),
				paths,
			);

			expect(result.installErrors).toEqual([]);
			expect(result.projectedProviderIds.openclaw).toEqual(["clawdi-managed"]);
			const config = JSON.parse(readFileSync(configPath, "utf8"));
			expect(config.agents.defaults).not.toHaveProperty("memorySearch");
			expect(config).toMatchObject({
				memory: {
					search: {
						...search,
					},
				},
				models: {
					providers: {
						"clawdi-managed": {
							apiKey: {
								source: "env",
								provider: "default",
								id: "CLAWDI_AI_API_KEY",
							},
						},
					},
				},
			});
			const runConfig = JSON.parse(
				readFileSync(runtimeRunConfigPath("openclaw", paths), "utf8"),
			) as {
				env?: Record<string, string>;
			};
			expect(runConfig.env?.CLAWDI_AI_API_KEY).toBe("clawdi-egress-placeholder");
			expect(runConfig.env?.OPENAI_API_KEY).toBeUndefined();
			const envFile = readFileSync(
				join(paths.systemdEnvRoot, "openclaw-gateway.service.env"),
				"utf8",
			);
			expect(envFile).toContain('CLAWDI_AI_API_KEY="clawdi-egress-placeholder"');
			expect(envFile).not.toMatch(/^OPENAI_API_KEY=/m);
			expect(envFile).not.toContain("sk-managed");
		},
	);

	test("reuses OpenClaw probes until the provider revision changes", () => {
		const paths = tempRuntimePaths();
		const commandLog = join(paths.serviceStateRoot, "openclaw-probe-commands.log");
		const sdkLog = join(paths.serviceStateRoot, "openclaw-probe-sdk.log");
		const configPath = writeFakeOpenClawConfigMutationSdk(paths.userHome, { importLog: sdkLog });
		writeFakeGatewayCli({
			path: join(paths.userHome, ".local", "bin", "openclaw"),
			runtime: "openclaw",
			unitPath: join(paths.systemdUserRoot, "openclaw-gateway.service"),
			commandLog,
		});
		const egressEngine = installCachedTestEgressEngine(paths, "12.2.3-test-probe-cache");
		const manifestFor = (baseUrl: string, generation: number): RuntimeManifest => ({
			...hostedRuntimeBundleV2ManifestSchema.parse(
				hostedManifestFixture({
					generation,
					issuedAt: `2026-07-11T00:00:0${generation}.000Z`,
					providers: {
						clawdi: {
							kind: "openai-compatible",
							type: "custom_openai_compatible",
							managed_by: "clawdi",
							baseUrl,
							apiMode: "openai_responses",
							models: [{ id: "gpt-test" }],
							runtimeEnvName: "CLAWDI_AI_API_KEY",
							apiKeySecretRef: "secret://providers/clawdi/api-key",
						},
					},
					runtimes: {
						openclaw: hostedRuntimeFixture({
							provider_ids: ["clawdi"],
							primary_model: { provider_id: "clawdi", model: "gpt-test" },
						}),
					},
				}),
			),
			egressEngine,
		});
		const secrets = {
			...TEST_HOSTED_SECRET_VALUES,
			"secret://providers/clawdi/api-key": "sk-managed",
		};
		const converge = (manifest: RuntimeManifest, opts: RuntimeConvergenceOptions = {}) =>
			convergeRuntimeManifest(
				manifestLoad(manifest, `provider-${manifest.generation}`, secrets),
				paths,
				opts,
			);
		const callCounts = (calls: string[]) =>
			Object.fromEntries(
				[...new Set(calls)].map((call) => [
					call,
					calls.filter((candidate) => candidate === call).length,
				]),
			);
		const hotspotCounts = () =>
			callCounts(
				readFileSync(commandLog, "utf8")
					.trim()
					.split("\n")
					.filter((command) => command === "agents list --json"),
			);
		const sdkCounts = () => callCounts(readFileSync(sdkLog, "utf8").trim().split("\n"));

		expect(converge(manifestFor("https://provider.example.test/v1", 1)).installErrors).toEqual([]);
		const firstHotspots = hotspotCounts();
		const firstSdkCalls = sdkCounts();
		for (const hotspot of ["agents list --json"]) {
			expect(firstHotspots[hotspot]).toBeGreaterThan(0);
		}
		for (const sdk of ["device-bootstrap", "provider-auth", "config-mutation"]) {
			expect(firstSdkCalls[sdk]).toBeGreaterThan(0);
		}

		expect(converge(manifestFor("https://provider.example.test/v1", 1)).installErrors).toEqual([]);
		expect(hotspotCounts()).toEqual(firstHotspots);
		expect(sdkCounts()).toEqual(firstSdkCalls);

		const driftedConfig = JSON.parse(readFileSync(configPath, "utf8")) as Record<string, unknown>;
		const driftedModels = driftedConfig.models as Record<string, unknown>;
		const driftedProviders = driftedModels.providers as Record<string, unknown>;
		delete driftedProviders.clawdi;
		writeFileSync(configPath, `${JSON.stringify(driftedConfig, null, 2)}\n`);
		expect(converge(manifestFor("https://provider.example.test/v1", 1)).installErrors).toEqual([]);
		expect(
			(
				(JSON.parse(readFileSync(configPath, "utf8")) as Record<string, unknown>).models as Record<
					string,
					Record<string, unknown>
				>
			).providers.clawdi,
		).toBeDefined();
		expect(hotspotCounts()).toEqual(firstHotspots);
		const repairedProviderSdkCalls = sdkCounts();
		expect(repairedProviderSdkCalls["config-mutation"]).toBeGreaterThan(
			firstSdkCalls["config-mutation"] ?? 0,
		);

		const rosterConfig = JSON.parse(readFileSync(configPath, "utf8")) as Record<string, unknown>;
		const agents = rosterConfig.agents as Record<string, unknown>;
		agents.list = [{ id: "research", agentDir: join(paths.userHome, "research-agent") }];
		writeFileSync(configPath, `${JSON.stringify(rosterConfig, null, 2)}\n`);
		const beforeRosterChange = sdkCounts();
		expect(converge(manifestFor("https://provider.example.test/v1", 1)).installErrors).toEqual([]);
		expect(hotspotCounts()["agents list --json"]).toBeGreaterThan(
			firstHotspots["agents list --json"] ?? 0,
		);
		const afterRosterChange = sdkCounts();
		expect(afterRosterChange["provider-auth"]).toBeGreaterThan(
			beforeRosterChange["provider-auth"] ?? 0,
		);
		const rosterChangedHotspots = hotspotCounts();

		expect(converge(manifestFor("https://provider-v2.example.test/v1", 2)).installErrors).toEqual(
			[],
		);
		const revisedHotspots = hotspotCounts();
		expect(revisedHotspots["agents list --json"]).toBe(rosterChangedHotspots["agents list --json"]);
		const revisedSdkCalls = sdkCounts();
		for (const [sdk, count] of Object.entries(firstSdkCalls)) {
			expect(revisedSdkCalls[sdk]).toBeGreaterThan(count);
		}
	});

	test.each(["managed", "native-path", "native-pairing", "local-disabled"])(
		"withdraws archived Hermes WhatsApp through committed bundle reconciliation (%s)",
		(ownership) => {
			const paths = tempRuntimePaths();
			const command = writeFakeHermesCli(paths);
			const webDist = join(paths.userHome, ".hermes", "hermes-agent", "hermes_cli", "web_dist");
			mkdirSync(webDist, { recursive: true });
			writeFileSync(join(webDist, "index.html"), "<html>dashboard fixture</html>");
			const revision = runtimeCommandCurrentRevision(command, paths.userHome, paths.userHome);
			if (!revision) throw new Error("Hermes fixture command revision is missing");
			writeFileSync(join(webDist, HERMES_DASHBOARD_BUILD_REVISION_FILE), revision);
			const bridge = join(paths.userHome, ".hermes", "hermes-agent", "scripts", "whatsapp-bridge");
			const baileys = join(bridge, "node_modules", "@whiskeysockets", "baileys");
			const pristineBaileys = join(
				import.meta.dir,
				"../../../whatsapp-baileys-sidecar/node_modules/baileys",
			);
			for (const target of MANAGED_BAILEYS_STATIC_PATCH_TARGETS) {
				const destination = join(baileys, target.relativePath);
				mkdirSync(dirname(destination), { recursive: true });
				cpSync(join(pristineBaileys, target.relativePath), destination);
			}
			cpSync(join(pristineBaileys, "package.json"), join(baileys, "package.json"));
			writeFileSync(
				join(bridge, "package.json"),
				JSON.stringify({ name: "hermes-whatsapp-bridge" }),
			);
			const configPath = join(paths.userHome, ".hermes", "config.yaml");
			const native = {
				telegram: { enabled: true, allowed_users: ["owner"] },
				discord: { enabled: true, require_mention: false },
				whatsapp: { dm_policy: "pairing" },
				platforms: { whatsapp: { extra: { group_policy: "disabled" } } },
			};
			writeFileSync(configPath, JSON.stringify(native));
			const accountKey = "clawdi_50000000000040008000000000000005";
			const linkId = "60000000-0000-4000-8000-000000000006";
			const credentialId = "80000000-0000-4000-8000-000000000011";
			const agentRef = `secret://channels/whatsapp/${accountKey}/links/${linkId}/agent-token`;
			const credentialRef = `secret://channels/whatsapp/${accountKey}/credentials/${credentialId}/creds-json`;
			const binding = {
				provider: "whatsapp",
				accountId: "50000000-0000-4000-8000-000000000005",
				accountKey,
				linkId,
				agentTokenSecretRef: agentRef,
				credential: {
					id: credentialId,
					credsSecretRef: credentialRef,
					authCert: {
						SERIAL: 7,
						ISSUER: "clawdi",
						PUBLIC_KEY: { type: "Buffer", data: Buffer.alloc(32, 7).toString("base64") },
					},
				},
			};
			const engine = installCachedTestEgressEngine(paths, "12.2.3-channel-archive");
			const loadFor = (active: boolean, rotated = false) => {
				const parsed = parseHostedRuntimeBundleV2(
					{
						schemaVersion: "clawdi.hosted-runtime.bundle.v2",
						sourceRevision: (active ? (rotated ? "c" : "a") : "b").repeat(64),
						manifest: hostedHermesManifestFixture({
							providers: {},
							runtimes: {
								hermes: {
									enabled: true,
									install: { source: "official" },
									providerMode: "unmanaged",
									provider_ids: [],
									run: { args: ["gateway", "run"] },
									services: {
										dashboard: {
											args: ["dashboard", "--host", "0.0.0.0", "--port", "9119", "--no-open"],
										},
									},
								},
							},
						}),
						channelBindings: active ? [binding] : [],
						secretValues: {
							...TEST_HOSTED_SECRET_VALUES,
							...(active
								? {
										[agentRef]: "test-agent-token",
										[credentialRef]: JSON.stringify({
											registered: true,
											revision: rotated ? 2 : 1,
										}),
									}
								: {}),
						},
					},
					"test://whatsapp-archive",
				);
				const projected = applyRuntimeBundleChannelsToManifestLoad(parsed, paths);
				return {
					...projected,
					manifest: { ...projected.manifest, egressEngine: engine },
					applyContext: manifestLoad(projected.manifest, "unused").applyContext,
				};
			};
			const converge = (load: RuntimeManifestLoad) => {
				const result = convergeRuntimeManifest(load, paths, {
					cacheLastGood: false,
					commitAuthority: (convergence, authority) =>
						commitTestRuntimeAuthority(load, paths, convergence, authority),
					systemdApply: {
						activateEgressPrerequisite: successfulPrerequisiteActivation,
						activate: successfulPrerequisiteActivation,
					},
				});
				expect(result.installErrors).toEqual([]);
				return result;
			};
			const active = loadFor(true);
			const nativeEnabled = () =>
				JSON.parse(
					execFileSync(
						join(paths.userHome, ".hermes", "hermes-agent", "venv", "bin", "python"),
						[
							"-c",
							"import json; from gateway.config import load_gateway_config; print(json.dumps({p.value: c.enabled for p, c in load_gateway_config().platforms.items()}))",
						],
						{
							encoding: "utf8",
							timeout: 15_000,
							env: {
								PATH: process.env.PATH,
								HOME: paths.userHome,
								HERMES_HOME: join(paths.userHome, ".hermes"),
							},
						},
					),
				);
			converge(active);
			const credsPath = join(
				paths.userHome,
				".hermes",
				"platforms",
				"whatsapp",
				"session",
				"creds.json",
			);
			expect(existsSync(credsPath)).toBe(true);
			const configured = parseYaml(readFileSync(configPath, "utf8"));
			expect(configured.whatsapp.enabled).toBe(true);
			expect(configured.platforms.whatsapp.enabled).toBe(true);
			expect(configured.platforms.whatsapp.extra.session_path).toBe(dirname(credsPath));
			expect(nativeEnabled().whatsapp).toBe(true);
			converge(loadFor(true, true));
			expect(JSON.parse(readFileSync(credsPath, "utf8")).revision).toBe(2);
			const nativePairing = JSON.stringify({ registered: true, me: { id: "native-owner" } });
			if (ownership === "native-path") {
				configured.platforms.whatsapp.extra.session_path = join(paths.userHome, "personal-pairing");
				mkdirSync(configured.platforms.whatsapp.extra.session_path, { recursive: true });
				writeFileSync(
					join(configured.platforms.whatsapp.extra.session_path, "creds.json"),
					nativePairing,
				);
			} else if (ownership === "native-pairing") {
				writeFileSync(credsPath, nativePairing);
			} else if (ownership === "local-disabled") {
				configured.whatsapp.enabled = false;
				configured.platforms.whatsapp.enabled = false;
			}
			writeFileSync(configPath, JSON.stringify(configured));
			const gatewayBefore = readSystemdUnitSnapshot(paths).user.get("hermes-gateway.service");
			expect(gatewayBefore).toBeDefined();
			// Rootfs replacement discards download caches, not committed channel authority.
			rmSync(paths.cacheRoot, { recursive: true, force: true });
			const archived = loadFor(false);
			converge(archived);
			const gatewayAfter = readSystemdUnitSnapshot(paths).user.get("hermes-gateway.service");
			expect(gatewayAfter).toBeDefined();
			expect(gatewayAfter).not.toBe(gatewayBefore);
			expect(existsSync(credsPath)).toBe(ownership === "native-pairing");
			const removed = parseYaml(readFileSync(configPath, "utf8"));
			expect(nativeEnabled()).toEqual(
				expect.objectContaining({
					telegram: true,
					discord: true,
					whatsapp: ownership === "native-path" || ownership === "native-pairing",
				}),
			);
			expect(removed.telegram).toEqual(native.telegram);
			expect(removed.discord).toEqual(native.discord);
			expect(removed.whatsapp.dm_policy).toBe("pairing");
			expect(removed.platforms.whatsapp.extra.group_policy).toBe("disabled");
			if (ownership === "native-path" || ownership === "native-pairing") {
				expect(
					readFileSync(
						join(configured.platforms.whatsapp.extra.session_path, "creds.json"),
						"utf8",
					),
				).toBe(nativePairing);
				expect(removed.whatsapp).toEqual(configured.whatsapp);
				expect(removed.platforms.whatsapp).toEqual(configured.platforms.whatsapp);
			} else {
				expect(removed.whatsapp.enabled).toBe(ownership === "local-disabled" ? false : undefined);
				expect(removed.platforms.whatsapp.enabled).toBe(
					ownership === "local-disabled" ? false : undefined,
				);
				expect(removed.platforms.whatsapp.extra).not.toHaveProperty("session_path");
			}
			const run = JSON.parse(readFileSync(runtimeRunConfigPath("hermes", paths), "utf8"));
			expect(Object.keys(run.env).filter((key) => key.startsWith("WHATSAPP_"))).toEqual([]);
			const beforeRetry = readFileSync(configPath, "utf8");
			converge(archived);
			expect(readFileSync(configPath, "utf8")).toBe(beforeRetry);
			expect(readSystemdUnitSnapshot(paths).user.get("hermes-gateway.service")).toBe(gatewayAfter);
		},
	);

	test("replaces the selected Hermes provider with secret refs and stale cleanup", () => {
		const paths = tempRuntimePaths();
		process.env.HERMES_TEST_PROVIDER_TOKEN = "resolved-provider-secret-must-not-be-written";
		const hermesCommand = join(paths.userHome, ".local", "bin", "hermes");
		const hermesConfig = join(paths.userHome, ".hermes", "config.yaml");
		const legacyPlugin = join(
			paths.userHome,
			".hermes",
			"plugins",
			"model-providers",
			"clawdi",
			"__init__.py",
		);
		const responsesKey = "sentinel-responses-runtime";
		const anthropicKey = "sentinel-anthropic-runtime";
		mkdirSync(dirname(legacyPlugin), { recursive: true });
		writeFakeGatewayCli({
			path: hermesCommand,
			runtime: "hermes",
			unitPath: join(paths.systemdUserRoot, "hermes-gateway.service"),
		});
		writeFileSync(legacyPlugin, 'raise RuntimeError("obsolete")\n');
		writeFileSync(
			hermesConfig,
			[
				"model:",
				"  provider: responses",
				"providers:",
				"  responses:",
				"    api: https://stale.example.test/v1",
				"    api_key: stale-inline-secret",
				'  "user.custom":',
				"    api: https://user-provider.example.test/v1",
				`    api_key: "${HERMES_TEST_PROVIDER_TOKEN_REF}"`,
				"",
			].join("\n"),
		);
		const providerEntries = {
			responses: {
				type: "openai",
				baseUrl: "https://responses.example.test/v1",
				apiMode: "openai_responses",
				models: [{ id: "gpt-test" }],
				runtimeEnvName: "RESPONSES_API_KEY",
				apiKeySecretRef: "secret://providers/responses/api-key",
			},
			anthropic: {
				type: "anthropic",
				baseUrl: "https://anthropic.example.test",
				apiMode: "anthropic_messages",
				models: [{ id: "claude-test" }],
				runtimeEnvName: "ANTHROPIC_TEST_API_KEY",
				apiKeySecretRef: "secret://providers/anthropic/api-key",
			},
		} satisfies NonNullable<NonNullable<RuntimeManifest["projection"]>["providers"]>;
		const manifestFor = (
			providers: NonNullable<NonNullable<RuntimeManifest["projection"]>["providers"]>,
			primaryModel: { provider_id: string; model: string } | undefined,
			generation: number,
		): RuntimeManifest =>
			baseManifest(
				paths,
				{
					hermes: {
						enabled: true,
						run: runSettings(hermesCommand, ["gateway", "run"]),
						provider_ids: Object.keys(providers),
						primary_model: primaryModel,
						services: {},
					},
				},
				{
					runtime: "hermes",
					generation,
					issuedAt: `2026-07-01T00:0${generation}:00.000Z`,
					projection: { system: { home: paths.userHome }, providers },
				},
			);
		const writeAppliedProviders = (generation: number, providerIds: string[]) => {
			writeRuntimeAppliedState(
				{
					schemaVersion: "clawdi.runtimeAppliedState.v2",
					appliedAt: `2026-07-01T00:1${generation}:00.000Z`,
					instanceId: "hri_reconcile",
					etag: `"generation-${generation}"`,
					sourceRevision: String(generation).repeat(64),
					generation,
					contentIdentity: {
						sourcePath: `inline-hermes-generation-${generation}`,
						sha256: "a".repeat(64),
					},
					activated: {},
					providerIds,
					projectedProviderIds: { hermes: providerIds },
				},
				paths,
			);
		};

		const initial = convergeRuntimeManifest(
			manifestLoad(
				manifestFor(
					{ responses: providerEntries.responses },
					{ provider_id: "responses", model: "gpt-test" },
					1,
				),
				"inline-hermes-native-providers",
				{ "secret://providers/responses/api-key": responsesKey },
			),
			paths,
		);

		expect(initial.installErrors).toEqual([]);
		expect(initial.projectedProviderIds.hermes).toEqual(["responses"]);
		expect(readFileSync(legacyPlugin, "utf8")).toBe('raise RuntimeError("obsolete")\n');
		const initialConfig = readFileSync(hermesConfig, "utf8");
		const initialRunConfig = readFileSync(runtimeRunConfigPath("hermes", paths), "utf8");
		const initialHermes = parseYaml(initialConfig) as {
			model?: { default?: string; provider?: string };
			providers?: Record<string, unknown>;
		};
		expect(initialHermes.model).toMatchObject({
			default: "gpt-test",
			provider: "custom:responses",
		});
		expect(initialHermes.providers?.responses).toMatchObject({
			api: "https://responses.example.test/v1",
			key_env: "RESPONSES_API_KEY",
			models: { "gpt-test": {} },
			transport: "codex_responses",
		});
		expect(initialHermes.providers?.["user.custom"]).toMatchObject({
			api: "https://user-provider.example.test/v1",
			api_key: HERMES_TEST_PROVIDER_TOKEN_REF,
		});
		expect(JSON.parse(initialRunConfig)).toMatchObject({
			secretEnv: {
				RESPONSES_API_KEY: "secret://providers/responses/api-key",
			},
		});
		expect(initialConfig).not.toContain(responsesKey);
		expect(initialConfig).not.toContain("resolved-provider-secret-must-not-be-written");
		expect(initialRunConfig).not.toContain(responsesKey);
		expect(initialConfig).not.toContain("stale-inline-secret");
		expect(initialConfig).not.toContain("https://stale.example.test/v1");

		writeAppliedProviders(1, initial.projectedProviderIds.hermes ?? []);
		const switched = convergeRuntimeManifest(
			manifestLoad(
				manifestFor(
					{ anthropic: providerEntries.anthropic },
					{ provider_id: "anthropic", model: "claude-test" },
					2,
				),
				"inline-hermes-provider-switch",
				{ "secret://providers/anthropic/api-key": anthropicKey },
			),
			paths,
		);
		expect(switched.installErrors).toEqual([]);
		expect(switched.projectedProviderIds.hermes).toEqual(["anthropic"]);
		const switchedConfig = readFileSync(hermesConfig, "utf8");
		const switchedProviders = (parseYaml(switchedConfig) as { providers?: Record<string, unknown> })
			.providers;
		expect(switchedProviders).not.toHaveProperty("responses");
		expect(switchedProviders?.["user.custom"]).toMatchObject({
			api_key: HERMES_TEST_PROVIDER_TOKEN_REF,
		});
		expect(parseYaml(switchedConfig)).toMatchObject({
			model: { default: "claude-test", provider: "custom:anthropic" },
			providers: {
				anthropic: {
					api: "https://anthropic.example.test",
					key_env: "ANTHROPIC_TEST_API_KEY",
					models: { "claude-test": {} },
					transport: "anthropic_messages",
				},
			},
		});

		writeAppliedProviders(2, switched.projectedProviderIds.hermes ?? []);
		const deleted = convergeRuntimeManifest(
			manifestLoad(manifestFor({}, undefined, 3), "inline-hermes-provider-delete"),
			paths,
		);
		expect(deleted.installErrors).toEqual([]);
		expect(deleted.projectedProviderIds.hermes).toEqual([]);
		const deletedProviders = (
			parseYaml(readFileSync(hermesConfig, "utf8")) as { providers?: Record<string, unknown> }
		).providers;
		expect(deletedProviders).not.toHaveProperty("anthropic");
		expect(deletedProviders?.["user.custom"]).toMatchObject({
			api_key: HERMES_TEST_PROVIDER_TOKEN_REF,
		});
	}, 30_000);

	test("preserves managed hosted provider model capabilities after primary resolution", () => {
		const paths = tempRuntimePaths();
		const manifest = baseManifest(
			paths,
			{
				openclaw: {
					enabled: true,
					run: runSettings("openclaw", ["gateway", "run"]),
					provider_ids: ["default"],
					primary_model: { provider_id: "default", model: "k3" },
					services: {},
				},
			},
			{
				projection: {
					providers: {
						default: {
							type: "custom_openai_compatible",
							managed_by: "clawdi",
							baseUrl: "https://api.example.test/v1",
							models: [
								{
									id: "k3",
									context_window: 1_048_576,
									max_input_tokens: 1_048_576,
									input_modalities: ["text", "image"],
									supports_tools: true,
									supports_reasoning: true,
									compat: { supportsDeveloperRole: false },
								},
								{ id: "kimi-for-coding" },
								{ id: "kimi-for-coding-highspeed", context_window: 262_144 },
								{
									id: "manifest-embedding-model",
									capabilities: { embeddings: true, chat: false },
								},
							],
							apiMode: "openai_responses",
							runtimeEnvName: "CLAWDI_AI_API_KEY",
							apiKeySecretRef: "secret://providers/default/api-key",
						},
					},
				},
			},
		);

		const projection = hostedAiProviderCatalog(manifest, "openclaw");
		expect(projection?.primaryModel).toEqual({ provider_id: "default", model: "k3" });
		expect(projection?.catalog.defaults?.embedding_provider_id).toBe("default");
		expect(projection?.catalog.providers[0]?.models).toEqual([
			{
				id: "k3",
				context_window: 1_048_576,
				max_input_tokens: 1_048_576,
				input_modalities: ["text", "image"],
				supports_tools: true,
				supports_reasoning: true,
				compat: { supportsDeveloperRole: false },
			},
			{ id: "kimi-for-coding" },
			{ id: "kimi-for-coding-highspeed", context_window: 262_144 },
			{
				id: "manifest-embedding-model",
				capabilities: { embeddings: true, chat: false },
			},
		]);
	});

	test.each(["openclaw", "default"])(
		"does not infer strict hosted provider bindings from the %s provider key",
		(providerKey) => {
			const paths = tempRuntimePaths();
			const manifest = baseManifest(
				paths,
				{
					openclaw: {
						enabled: true,
						run: runSettings("openclaw", ["gateway", "run"]),
						provider_ids: ["default"],
						services: {},
					},
				},
				{
					projection: {
						providers: {
							[providerKey]: {
								type: "custom_openai_compatible",
								baseUrl: "https://api.example.test/v1",
								model: "gpt-inferred",
								models: [{ id: "gpt-inferred" }],
								apiMode: "openai_chat",
							},
						},
					},
				},
			);

			expect(hostedAiProviderCatalog(manifest, "openclaw")).toBeNull();
		},
	);

	test("does not infer a strict hosted primary model from the first provider", () => {
		const paths = tempRuntimePaths();
		const manifest = baseManifest(
			paths,
			{
				openclaw: {
					enabled: true,
					providerMode: "configured",
					run: runSettings("openclaw", ["gateway", "run"]),
					provider_ids: ["default"],
					services: {},
				},
			},
			{
				projection: {
					providers: {
						default: {
							type: "custom_openai_compatible",
							baseUrl: "https://api.example.test/v1",
							model: "gpt-inferred",
							models: [{ id: "gpt-inferred" }],
							apiMode: "openai_chat",
						},
					},
				},
			},
		);

		expect(hostedAiProviderCatalog(manifest, "openclaw")).toBeNull();
		const runtime = manifest.runtimes.openclaw;
		if (!runtime) throw new Error("OpenClaw fixture runtime is missing");
		runtime.primary_model = { provider_id: "default", model: "gpt-inferred" };
		expect(hostedAiProviderCatalog(manifest, "openclaw")).not.toBeNull();
	});

	test("preserves hosted provider model alias and cost metadata", () => {
		const paths = tempRuntimePaths();
		const manifest = baseManifest(
			paths,
			{
				openclaw: {
					enabled: true,
					run: runSettings("openclaw", ["gateway", "run"]),
					provider_ids: ["custom"],
					primary_model: { provider_id: "custom", model: "example-model" },
					services: {},
				},
			},
			{
				projection: {
					providers: {
						custom: {
							type: "custom_openai_compatible",
							baseUrl: "https://api.example.test/v1",
							apiMode: "openai_chat",
							models: [
								{
									id: "example-model",
									alias: "Example Model",
									context_window: 128_000,
									cost: {
										input: 0.3,
										output: 1.2,
										cache_read: 0.06,
										cache_write: 0,
									},
								},
							],
							runtimeEnvName: "CUSTOM_API_KEY",
							apiKeySecretRef: "secret://providers/custom/api-key",
						},
					},
				},
			},
		);

		const projection = hostedAiProviderCatalog(manifest, "openclaw");
		expect(projection?.catalog.providers[0]?.models).toEqual([
			{
				id: "example-model",
				alias: "Example Model",
				context_window: 128_000,
				cost: {
					input: 0.3,
					output: 1.2,
					cache_read: 0.06,
					cache_write: 0,
				},
			},
		]);
	});

	test("converges enabled egress when the pinned engine is ready", () => {
		const paths = tempRuntimePaths();
		const artifact = writeTestMitmproxyArchive(paths, "ready-success", "ready");
		const curl = installTestMitmproxyCurl(paths, artifact.path);
		const manifest = egressRuntimeManifest(paths, {
			generation: 1,
			engine: testEgressEnginePin("12.2.3-test-success", artifact.sha256),
			profile: "enabled",
		});
		const load = manifestLoad(manifest, "inline-egress-success");
		let commits = 0;
		const result = convergeRuntimeManifest(load, paths, {
			cacheLastGood: false,
			commitAuthority: (convergence, authority) => {
				commits += 1;
				commitTestRuntimeAuthority(load, paths, convergence, authority);
			},
			egressEngineEnsureOptions: { downloadCommand: curl.commandPath },
			systemdApply: {
				activateEgressPrerequisite: successfulPrerequisiteActivation,
				activate: successfulPrerequisiteActivation,
			},
		});

		expect(result.installErrors).toEqual([]);
		expect(result.outputs.egressEngine).toEqual(expect.objectContaining({ status: "ready" }));
		expect(commits).toBe(1);
		expect(readRuntimeAppliedState(paths)?.generation).toBe(1);
		expect(
			existsSync(join(paths.systemdSystemRoot, `${RUNTIME_SYSTEM_UNIT_SIDECAR}.service`)),
		).toBe(true);
	});

	test("publishes transparent egress env as a root-authored read-only handoff to the numeric egress identity", () => {
		const numericPrivilegeToolPath = ["/usr/bin/set", "priv"].join("");
		if (process.geteuid?.() !== 0 || !existsSync(numericPrivilegeToolPath)) return;
		const paths = tempRuntimePaths();
		const egressUid = 10_002;
		const egressGid = 10_002;
		process.env.CLAWDI_EGRESS_UID = String(egressUid);
		process.env.CLAWDI_EGRESS_GID = String(egressGid);
		process.env.CLAWDI_RUNTIME_USER = "clawdi";
		process.env.CLAWDI_RUNTIME_UID = "10001";
		process.env.CLAWDI_RUNTIME_GID = "10001";
		const manifest = egressRuntimeManifest(paths, {
			generation: 1,
			engine: installCachedTestEgressEngine(paths, "12.2.3-test-egress-env-identity"),
			profile: "enabled",
		});
		chmodSync(dirname(paths.serviceStateRoot), 0o777);
		const result = convergeRuntimeManifest(
			manifestLoad(manifest, "root-egress-env-handoff"),
			paths,
			{
				cacheLastGood: false,
				systemdApply: {
					activateEgressPrerequisite: successfulPrerequisiteActivation,
					activate: successfulPrerequisiteActivation,
				},
				hostedRuntimeContract: {
					expectedIdentity: {
						home: paths.userHome,
						user: "clawdi",
						uid: 10_001,
						gid: 10_001,
					},
					resolveUserIdentity: () => ({ uid: 10_001, gid: 10_001 }),
				},
			},
		);
		expect(result.installErrors).toEqual([]);

		const envFile = paths.egressTransparentEnv;
		const node = statSync(envFile);
		expect([node.uid, node.gid]).toEqual([0, egressGid]);
		expect(node.mode & 0o777).toBe(0o640);
		expect(statSync(paths.egressRoot).mode & 0o777).toBe(0o711);
		expect(statSync(paths.egressTransparentEnv).mode & 0o022).toBe(0);

		const runAsEgressIdentity = (args: string[]) =>
			execFileSync(
				numericPrivilegeToolPath,
				[`--reuid=${egressUid}`, `--regid=${egressGid}`, "--clear-groups", "--", ...args],
				{ encoding: "utf8" },
			);
		expect(runAsEgressIdentity(["sh", "-c", `cat -- ${JSON.stringify(envFile)}`])).toContain(
			`CLAWDI_EGRESS_GID="${egressGid}"`,
		);
		expect(() => runAsEgressIdentity(["sh", "-c", `: > ${JSON.stringify(envFile)}`])).toThrow();
	});

	test("tracks egress secret lifecycle in rendered sidecar bytes", () => {
		const paths = tempRuntimePaths();
		const commandPath = join(paths.userHome, ".local", "bin", "openclaw");
		const egressEngine = {
			type: "mitmproxy" as const,
			version: "12.2.3",
			url: "https://downloads.mitmproxy.org/12.2.3/mitmproxy-12.2.3-linux-x86_64.tar.gz",
			sha256: "2e95286b618fa6fd33e5e62a78c2e5112571d85f42ec2bac29b97ee242bdb5c5",
		};
		const engineBinary = join(
			paths.egressEngineMaintainedRoot,
			egressEngine.version,
			egressEngine.sha256,
			"mitmdump",
		);
		writeFakeGatewayCli({
			path: commandPath,
			runtime: "openclaw",
			unitPath: join(paths.systemdUserRoot, "openclaw-gateway.service"),
		});
		mkdirSync(dirname(engineBinary), { recursive: true });
		writeFileSync(engineBinary, "#!/usr/bin/env sh\nexit 0\n");
		chmodSync(engineBinary, 0o700);
		const secretRef = "secret://runtime/egress/test-token";
		const activeManifest = baseManifest(
			paths,
			{
				openclaw: {
					enabled: true,
					run: runSettings(commandPath, ["gateway", "run"]),
					services: {},
				},
			},
			{
				egressEngine,
				egressProfiles: {
					profiles: [
						{
							id: "managed-api",
							enabled: true,
							kind: "http",
							match: {
								scheme: "https",
								host: "api.example.test:443",
								path: { type: "equals", value: "/v1/data" },
								headers: {
									"X-Route-Key": {
										type: "equals",
										value: "managed",
									},
								},
								query: {},
							},
							rewrite: {
								upstreamBaseUrl: "http://localhost:9000",
								preservePath: true,
								setHeaders: {
									authorization: {
										type: "secretRef",
										secretRef,
										prefix: "Bearer ",
									},
								},
							},
							logging: { redactHeaders: ["authorization"], redactUrlPatterns: [] },
							priority: 60,
							owner: "runtime:test",
						},
					],
				},
			},
		);
		const converge = (manifest: RuntimeManifest, secret: string | undefined) =>
			convergeRuntimeManifest(
				manifestLoad(manifest, "inline-egress-secret-lifecycle", {
					...TEST_HOSTED_SECRET_VALUES,
					...(secret === undefined ? {} : { [secretRef]: secret }),
				}),
				paths,
				{
					cacheLastGood: false,
					commitAuthority: (_convergence, authority) => {
						writeRuntimeAppliedState(
							{
								schemaVersion: "clawdi.runtimeAppliedState.v2",
								appliedAt: "2026-07-28T00:00:00.000Z",
								instanceId: manifest.instanceId,
								etag: '"egress-lifecycle"',
								sourceRevision: "a".repeat(64),
								generation: manifest.generation,
								contentIdentity: {
									sourcePath: "inline-egress-secret-lifecycle",
									sha256: "b".repeat(64),
								},
								...authority,
								providerIds: [],
								projectedProviderIds: {},
							},
							paths,
						);
					},
					systemdApply: {
						activateEgressPrerequisite: successfulPrerequisiteActivation,
						activate: successfulPrerequisiteActivation,
					},
				},
			);
		const secretFile = join(paths.managedSecretRoot, "egress-secrets.json");
		const sidecarUnit = join(paths.systemdSystemRoot, `${RUNTIME_SYSTEM_UNIT_SIDECAR}.service`);
		const sidecarEnv = join(paths.systemdEnvRoot, `${RUNTIME_SYSTEM_UNIT_SIDECAR}.service.env`);

		const initial = converge(activeManifest, "000000");
		expect(initial.installErrors).toEqual([]);
		const renderedGatewayUnit = initial.outputs.systemdUserUnits[0];
		if (!renderedGatewayUnit) throw new Error("active runtime did not render a gateway unit");
		const gatewayUnitName = renderedGatewayUnit.split("/").at(-1);
		if (!gatewayUnitName) throw new Error("rendered gateway unit has no file name");
		const gatewayUnit = join(
			paths.systemdUserRoot,
			`${gatewayUnitName}.d`,
			RUNTIME_SYSTEMD_DROP_IN_FILE,
		);
		const gatewayEnv = join(paths.systemdEnvRoot, `${gatewayUnitName}.env`);
		expect(statSync(secretFile).mode & 0o777).toBe(0o600);
		expect(readFileSync(secretFile, "utf-8")).toContain("000000");
		const initialSidecarEnv = readFileSync(sidecarEnv, "utf-8");
		const activeGatewayUnit = readFileSync(gatewayUnit, "utf-8");
		const activeGatewayEnv = readFileSync(gatewayEnv, "utf-8");
		expect(readFileSync(gatewayEnv, "utf-8")).toContain("NODE_EXTRA_CA_CERTS");
		expect(readFileSync(gatewayEnv, "utf-8")).not.toContain("000000");

		expect(converge(activeManifest, "000000").installErrors).toEqual([]);
		expect(readFileSync(sidecarEnv, "utf-8")).toBe(initialSidecarEnv);
		expect(readFileSync(gatewayUnit, "utf-8")).toBe(activeGatewayUnit);

		expect(converge(activeManifest, "000001").installErrors).toEqual([]);
		expect(readFileSync(sidecarEnv, "utf-8")).not.toBe(initialSidecarEnv);
		expect(readFileSync(secretFile, "utf-8")).toContain("000001");
		expect(readFileSync(gatewayUnit, "utf-8")).toBe(activeGatewayUnit);

		const changedProfileManifest: RuntimeManifest = {
			...activeManifest,
			egressProfiles: {
				profiles:
					activeManifest.egressProfiles?.profiles.map((profile) => ({
						...profile,
						priority: profile.priority + 1,
					})) ?? [],
			},
		};
		expect(converge(changedProfileManifest, "000001").installErrors).toEqual([]);
		expect(readFileSync(gatewayUnit, "utf-8")).toBe(activeGatewayUnit);

		const noEgressManifest: RuntimeManifest = {
			...activeManifest,
			egressProfiles: { profiles: [] },
		};
		expect(converge(noEgressManifest, undefined).installErrors).toEqual([]);
		expect(readFileSync(gatewayUnit, "utf-8")).toBe(activeGatewayUnit);
		expect(readFileSync(gatewayEnv, "utf-8")).not.toBe(activeGatewayEnv);
		expect(readFileSync(gatewayEnv, "utf-8")).not.toContain("NODE_EXTRA_CA_CERTS");

		const noSidecarManifest: RuntimeManifest = {
			...changedProfileManifest,
			runtimes: { openclaw: { ...activeManifest.runtimes.openclaw, enabled: false } },
		};
		expect(converge(noSidecarManifest, "000002").installErrors).toEqual([]);
		expect(readFileSync(secretFile, "utf-8")).toContain("000002");
		expect(existsSync(sidecarUnit)).toBe(false);

		const deletedManifest: RuntimeManifest = {
			...noSidecarManifest,
			egressProfiles: { profiles: [] },
		};
		expect(converge(deletedManifest, undefined).installErrors).toEqual([]);
		expect(existsSync(secretFile)).toBe(false);

		expect(converge(deletedManifest, undefined).installErrors).toEqual([]);
	});

	test("reconciles 0.13.92 Skill trees from ledger-backed ownership", () => {
		const paths = tempRuntimePaths();
		ensureRuntimeStateDirs(paths);
		const runningAsRoot = process.geteuid?.() === 0;
		const runtimeUser = runningAsRoot ? "nobody" : TEST_RUNTIME_USER;
		const runtimeUid = runningAsRoot
			? Number.parseInt(execFileSync("id", ["-u", runtimeUser], { encoding: "utf8" }).trim(), 10)
			: TEST_PROCESS_UID;
		const runtimeGid = runningAsRoot
			? Number.parseInt(execFileSync("id", ["-g", runtimeUser], { encoding: "utf8" }).trim(), 10)
			: TEST_PROCESS_GID;
		process.env.CLAWDI_RUNTIME_USER = runtimeUser;
		process.env.CLAWDI_RUNTIME_UID = String(runtimeUid);
		process.env.CLAWDI_RUNTIME_GID = String(runtimeGid);
		const hermesCommand = writeFakeHermesCli(paths);
		const openClawCommand = join(paths.userHome, ".local", "bin", "openclaw");
		writeFakeGatewayCli({
			path: openClawCommand,
			runtime: "openclaw",
			unitPath: join(paths.systemdUserRoot, "openclaw-gateway.service"),
		});
		if (runningAsRoot) {
			chmodSync(dirname(paths.serviceStateRoot), 0o755);
			for (const path of [
				paths.userHome,
				join(paths.userHome, ".local"),
				dirname(hermesCommand),
				hermesCommand,
				openClawCommand,
			]) {
				chownSync(path, runtimeUid, runtimeGid);
			}
		}
		const skillsRoot = join(paths.userHome, ".hermes", "skills");
		const enabledTarget = join(skillsRoot, "iforge");
		const enabledSourcedId = "review-pr";
		const disabledId = "disabled-review-pr";
		const disabledTarget = join(skillsRoot, disabledId);
		const legacyReceiptDirectory = join(skillsRoot, ".clawdi-manifest-receipts");
		const openClawSkillsRoot = join(paths.userHome, ".openclaw", "workspace", "skills");
		const openClawEnabledSourcedTarget = join(openClawSkillsRoot, enabledSourcedId);
		const openClawLegacyReceiptDirectory = join(openClawSkillsRoot, ".clawdi-manifest-receipts");
		const platformReceiptDirectory = join(paths.managedResourceRoot, "skill-receipts");
		const bundledSource = resolve(
			import.meta.dir,
			"../..",
			"skills",
			"hosted-versions",
			"1",
			"iforge",
		);
		const catalogEntry = resolveHostedBundledSkill("iforge", 1);
		const enabledSourcedSource = {
			type: "github" as const,
			url: "https://github.com/Clawdi-AI/store",
			path: `skills/${enabledSourcedId}`,
			commit: "b".repeat(40),
		};
		const enabledSourced = preparedTestSourcedSkill(
			enabledSourcedId,
			enabledSourcedSource,
			"# Current Review PR\n",
		);
		const disabledSource = {
			type: "github" as const,
			url: "https://github.com/Clawdi-AI/store",
			path: `skills/${disabledId}`,
			commit: "a".repeat(40),
		};
		const disabledSourceIdentity = [
			"github",
			disabledId,
			disabledSource.url,
			disabledSource.path,
			disabledSource.commit,
		].join("\0");
		cpSync(bundledSource, enabledTarget, { recursive: true });
		writeFileSync(
			join(enabledTarget, ".clawdi-managed.json"),
			`${JSON.stringify({
				schema: "clawdi.hostedBundledSkillMarker.v1",
				owner: "clawdi runtime init",
				id: "iforge",
				version: 1,
				digest: catalogEntry.digest,
			})}\n`,
		);
		mkdirSync(disabledTarget, { recursive: true });
		writeFileSync(join(disabledTarget, "SKILL.md"), "# Review PR\n");
		mkdirSync(openClawEnabledSourcedTarget, { recursive: true });
		writeFileSync(join(openClawEnabledSourcedTarget, "SKILL.md"), "# Legacy Review PR\n");
		mkdirSync(legacyReceiptDirectory, { recursive: true });
		writeFileSync(
			join(legacyReceiptDirectory, `${disabledId}.json`),
			'{"schemaVersion":"clawdi.hermesManifestSkillReceipt.v2"}\n',
		);
		mkdirSync(openClawLegacyReceiptDirectory, { recursive: true });
		writeFileSync(
			join(openClawLegacyReceiptDirectory, `${enabledSourcedId}.json`),
			'{"schemaVersion":"clawdi.openclawManifestSkillReceipt.v2"}\n',
		);
		mkdirSync(join(platformReceiptDirectory, "hermes"), { recursive: true });
		writeFileSync(join(platformReceiptDirectory, "hermes", "iforge.json"), "{}\n");
		reserveManagedSkill({
			targetDir: enabledTarget,
			id: "iforge",
			manager: "hosted-manifest",
			version: 1,
			digest: catalogEntry.digest,
		});
		reserveManagedSkill({
			targetDir: disabledTarget,
			id: disabledId,
			manager: "hosted-manifest",
			sourceIdentity: disabledSourceIdentity,
		});
		reserveManagedSkill({
			targetDir: openClawEnabledSourcedTarget,
			id: enabledSourcedId,
			manager: "hosted-manifest",
			sourceIdentity: enabledSourced.identity.sourceIdentity,
		});
		const manifest = baseManifest(
			paths,
			{
				hermes: {
					enabled: true,
					run: runSettings(hermesCommand, ["gateway"]),
					services: {},
				},
				openclaw: {
					enabled: true,
					run: runSettings(openClawCommand, ["gateway"]),
					services: {},
				},
			},
			{
				projection: {
					skills: {
						entries: {
							iforge: { enabled: true, version: 1 },
							[enabledSourcedId]: { enabled: true, source: enabledSourcedSource },
							[disabledId]: { enabled: false, source: disabledSource },
						},
					},
				},
			},
		);
		const result = convergeRuntimeManifest(manifestLoad(manifest, "legacy-ledger-skills"), paths, {
			preparedHostedSourcedSkills: new Map([[enabledSourcedId, enabledSourced]]),
			hostedRuntimeContract: {
				expectedIdentity: {
					home: paths.userHome,
					user: runtimeUser,
					uid: runtimeUid,
					gid: runtimeGid,
				},
				resolveUserIdentity: () => ({ uid: runtimeUid, gid: runtimeGid }),
			},
		});

		expect([...result.installErrors, ...result.resourceProjectionErrors]).toEqual([]);
		expect(readFileSync(join(enabledTarget, "SKILL.md"))).toEqual(
			readFileSync(join(bundledSource, "SKILL.md")),
		);
		expect(existsSync(join(enabledTarget, ".clawdi-managed.json"))).toBe(false);
		expect(managedSkillReservationState(enabledTarget, "iforge")).toBe("reserved");
		const ledger = JSON.parse(readFileSync(managedSkillReservationLedgerPath(), "utf8"));
		expect(ledger.reservations[enabledTarget].digest).toBe(catalogEntry.digest);
		expect(readFileSync(join(openClawEnabledSourcedTarget, "SKILL.md"), "utf8")).toBe(
			"# Current Review PR\n",
		);
		expect(ledger.reservations[openClawEnabledSourcedTarget]).toMatchObject({
			id: enabledSourcedId,
			digest: enabledSourced.identity.digest,
			sourceIdentity: enabledSourced.identity.sourceIdentity,
			manager: "hosted-manifest",
		});
		expect(existsSync(disabledTarget)).toBe(false);
		expect(managedSkillReservationState(disabledTarget, disabledId)).toBe("unreserved");
		expect(existsSync(legacyReceiptDirectory)).toBe(false);
		expect(existsSync(openClawLegacyReceiptDirectory)).toBe(false);
		expect(existsSync(platformReceiptDirectory)).toBe(false);
	});

	test("upgrades a reserved bundled Skill from a private platform source idempotently", () => {
		if (process.geteuid?.() !== 0) return;
		const paths = tempRuntimePaths();
		const fixtureRoot = dirname(paths.serviceStateRoot);
		const command = join(paths.userHome, ".local", "bin", "openclaw");
		writeFakeGatewayCli({
			path: command,
			runtime: "openclaw",
			unitPath: join(paths.systemdUserRoot, "openclaw-gateway.service"),
		});
		const skillDir = join(paths.userHome, ".openclaw", "workspace", "skills", "iforge");
		const ledger = join(paths.managedResourceRoot, "managed-skills.json");
		const previousDigest = "272ec28025eb3c5227e4f7d7215327d5c070e7c4c87933e4d6df2f5bf33f9b9c";
		const cliRoot = resolve(import.meta.dir, "../..");
		const skillSource = join(cliRoot, "skills", "hosted-versions", "1", "iforge");
		const protectedSourceAncestors = [
			skillSource,
			dirname(skillSource),
			dirname(dirname(skillSource)),
			dirname(dirname(dirname(skillSource))),
			cliRoot,
		];
		const originalSourceModes = new Map(
			protectedSourceAncestors.map((path) => [path, statSync(path).mode & 0o777]),
		);
		const runtimeUid = 10_001;
		const runtimeGid = 10_001;
		process.env.CLAWDI_RUNTIME_USER = "clawdi";
		process.env.CLAWDI_RUNTIME_UID = String(runtimeUid);
		process.env.CLAWDI_RUNTIME_GID = String(runtimeGid);
		process.env.CLAWDI_RUNTIME_MODE = "hosted";

		chmodSync(fixtureRoot, 0o755);
		mkdirSync(paths.clawdiHome, { recursive: true });
		chownSync(paths.clawdiHome, runtimeUid, runtimeGid);
		mkdirSync(skillDir, { recursive: true });
		writeFileSync(join(skillDir, "SKILL.md"), "# Clawdi 0.14.13\n");
		for (const path of [
			paths.userHome,
			join(paths.userHome, ".local"),
			dirname(command),
			command,
			join(paths.userHome, ".openclaw"),
			join(paths.userHome, ".openclaw", "workspace"),
			dirname(skillDir),
			skillDir,
			join(skillDir, "SKILL.md"),
		]) {
			chownSync(path, runtimeUid, runtimeGid);
		}
		ensureRuntimeStateDirs(paths);
		reserveManagedSkill({
			targetDir: skillDir,
			id: "iforge",
			manager: "hosted-manifest",
			version: 1,
			digest: previousDigest,
		});
		const manifest = baseManifest(
			paths,
			{
				openclaw: {
					enabled: true,
					run: runSettings(command, ["gateway"]),
					services: {},
				},
			},
			{ projection: { skills: { entries: { iforge: { enabled: true, version: 1 } } } } },
		);
		for (const path of protectedSourceAncestors) chmodSync(path, 0o700);
		const accountPrivilegeTool = ["run", "user"].join("");
		try {
			for (const path of protectedSourceAncestors) {
				expect(() =>
					execFileSync(accountPrivilegeTool, ["-u", "nobody", "--", "test", "-x", path]),
				).toThrow();
			}
			expect(() =>
				execFileSync(accountPrivilegeTool, [
					"-u",
					"nobody",
					"--",
					"test",
					"-r",
					join(skillSource, "SKILL.md"),
				]),
			).toThrow();
			const hostedRuntimeContract = {
				expectedIdentity: {
					home: paths.userHome,
					user: "clawdi",
					uid: runtimeUid,
					gid: runtimeGid,
				},
				resolveUserIdentity: () => ({ uid: runtimeUid, gid: runtimeGid }),
			};
			const result = convergeRuntimeManifest(
				manifestLoad(manifest, "bundled-skill-upgrade"),
				paths,
				{
					hostedRuntimeContract,
				},
			);
			expect(result.installErrors).toEqual([]);
			expect(result.resourceProjectionErrors).toEqual([]);
			expect(readFileSync(join(skillDir, "SKILL.md"), "utf8")).toContain("# Clawdi");
			expect(statSync(skillDir).uid).toBe(runtimeUid);
			expect(statSync(join(skillDir, "SKILL.md")).uid).toBe(runtimeUid);
			expect(statSync(paths.managedResourceRoot).uid).toBe(0);
			expect(statSync(paths.managedResourceRoot).mode & 0o777).toBe(0o755);
			expect(statSync(ledger).uid).toBe(0);
			expect(statSync(ledger).mode & 0o022).toBe(0);
			const upgradedLedger = JSON.parse(readFileSync(ledger, "utf8"));
			expect(upgradedLedger.reservations[skillDir].digest).toBe(
				resolveHostedBundledSkill("iforge", 1).digest,
			);
			expect(upgradedLedger.pendingReservations).toEqual({});
			for (const path of protectedSourceAncestors) {
				expect(statSync(path).mode & 0o777).toBe(0o700);
			}
			const upgradedInode = statSync(skillDir).ino;
			const unchanged = convergeRuntimeManifest(
				manifestLoad({ ...manifest, generation: 2 }, "bundled-skill-unchanged"),
				paths,
				{ hostedRuntimeContract },
			);
			expect([...unchanged.installErrors, ...unchanged.resourceProjectionErrors]).toEqual([]);
			expect(statSync(skillDir).ino).toBe(upgradedInode);

			expect(() =>
				execFileSync(accountPrivilegeTool, [
					"-u",
					"nobody",
					"--",
					"test",
					"-w",
					paths.managedResourceRoot,
				]),
			).toThrow();

			const removal = convergeRuntimeManifest(
				manifestLoad(
					{ ...manifest, projection: { skills: { entries: {} } } },
					"bundled-skill-removal",
				),
				paths,
				{ hostedRuntimeContract },
			);

			expect(removal.installErrors).toEqual([]);
			expect(existsSync(skillDir)).toBe(false);
			expect(readFileSync(ledger, "utf8")).not.toContain(skillDir);
			expect(statSync(ledger).uid).toBe(0);
			expect(statSync(ledger).mode & 0o022).toBe(0);
			for (const path of protectedSourceAncestors) {
				expect(statSync(path).mode & 0o777).toBe(0o700);
			}
		} finally {
			for (const [path, mode] of [...originalSourceModes].reverse()) chmodSync(path, mode);
		}
	});

	test("reconciles exact-source Hermes Workspace Skills through the reservation ledger", () => {
		const paths = tempRuntimePaths();
		process.env.CLAWDI_RUNTIME_MODE = "hosted";
		const hermesCommand = writeFakeHermesCli(paths);
		const source = {
			type: "github" as const,
			url: "https://github.com/Clawdi-AI/store",
			path: "skills/review-pr",
			commit: "a".repeat(40),
		};
		const prepared = preparedTestSourcedSkill("review-pr", source, "manifest-owned\n");
		const manifest = baseManifest(
			paths,
			{ hermes: { enabled: true, run: runSettings(hermesCommand, ["gateway"]), services: {} } },
			{ projection: { skills: { entries: { "review-pr": { enabled: true, source } } } } },
		);
		const skillDir = join(paths.userHome, ".hermes", "skills", "review-pr");
		const userOwnedSibling = join(paths.userHome, ".hermes", "skills", "user-owned");
		mkdirSync(skillDir, { recursive: true });
		writeFileSync(join(skillDir, "SKILL.md"), "user-owned collision\n");
		const preparedSkills = new Map([[prepared.id, prepared]]);

		const collision = convergeRuntimeManifest(
			manifestLoad(manifest, "skill-ledger-collision"),
			paths,
			{ preparedHostedSourcedSkills: preparedSkills },
		);
		expect(collision.resourceProjectionErrors.join("\n")).toContain(
			`refusing to replace unmanaged review-pr skill at ${skillDir}`,
		);
		expect(readFileSync(join(skillDir, "SKILL.md"), "utf8")).toBe("user-owned collision\n");
		rmSync(skillDir, { recursive: true, force: true });
		mkdirSync(userOwnedSibling, { recursive: true });
		writeFileSync(join(userOwnedSibling, "SKILL.md"), "keep me\n");

		const installed = convergeRuntimeManifest(
			manifestLoad({ ...manifest, generation: 2 }, "skill-ledger-install"),
			paths,
			{ preparedHostedSourcedSkills: preparedSkills },
		);
		expect([...installed.installErrors, ...installed.resourceProjectionErrors]).toEqual([]);
		expect(readFileSync(join(skillDir, "SKILL.md"), "utf8")).toBe("manifest-owned\n");
		const ledger = JSON.parse(readFileSync(managedSkillReservationLedgerPath(), "utf8"));
		expect(ledger.reservations[skillDir]).toMatchObject({
			id: "review-pr",
			digest: prepared.identity.digest,
			sourceIdentity: prepared.identity.sourceIdentity,
			manager: "hosted-manifest",
		});

		const stableInode = statSync(skillDir).ino;
		const unchanged = convergeRuntimeManifest(
			manifestLoad({ ...manifest, generation: 3 }, "skill-ledger-unchanged"),
			paths,
			{ preparedHostedSourcedSkills: preparedSkills },
		);
		expect([...unchanged.installErrors, ...unchanged.resourceProjectionErrors]).toEqual([]);
		expect(statSync(skillDir).ino).toBe(stableInode);
		const movedSource = { ...source, commit: "c".repeat(40) };
		const movedPrepared = {
			...prepared,
			identity: {
				...prepared.identity,
				source: movedSource,
				sourceIdentity:
					"github\0review-pr\0https://github.com/Clawdi-AI/store\0skills/review-pr\0" +
					movedSource.commit,
			},
		};
		const moved = convergeRuntimeManifest(
			manifestLoad(
				{
					...manifest,
					generation: 4,
					projection: {
						skills: { entries: { "review-pr": { enabled: true, source: movedSource } } },
					},
				},
				"skill-ledger-source-moved",
			),
			paths,
			{ preparedHostedSourcedSkills: new Map([[movedPrepared.id, movedPrepared]]) },
		);
		expect([...moved.installErrors, ...moved.resourceProjectionErrors]).toEqual([]);
		expect(hostedHermesSkillSourceMatches(paths.userHome, skillDir, movedSource)).toBe(true);
		expect(
			JSON.parse(readFileSync(managedSkillReservationLedgerPath(), "utf8")).reservations[skillDir]
				.sourceIdentity,
		).toBe(movedPrepared.identity.sourceIdentity);

		const removed = convergeRuntimeManifest(
			manifestLoad(
				{ ...manifest, generation: 5, projection: { skills: { entries: {} } } },
				"skill-ledger-remove",
			),
			paths,
			{ preparedHostedSourcedSkills: new Map() },
		);
		expect([...removed.installErrors, ...removed.resourceProjectionErrors]).toEqual([]);
		expect(existsSync(skillDir)).toBe(false);
		expect(managedSkillReservationState(skillDir, "review-pr")).toBe("unreserved");
		expect(readFileSync(join(userOwnedSibling, "SKILL.md"), "utf8")).toBe("keep me\n");
	});
	test("recovers a killed hosted Skill install before retrying convergence", async () => {
		const paths = tempRuntimePaths();
		ensureRuntimeStateDirs(paths);
		const skillId = "crash-recovery";
		const source = {
			type: "github" as const,
			url: "https://github.com/Clawdi-AI/store",
			path: `skills/${skillId}`,
			commit: "a".repeat(40),
		};
		const prepared = preparedTestSourcedSkill(skillId, source, "verified tree\n");
		const sourceIdentity = prepared.identity.sourceIdentity;
		const target = join(paths.userHome, ".hermes", "skills", skillId);
		mkdirSync(target, { recursive: true });
		writeFileSync(join(target, "SKILL.md"), "old committed tree\n");
		reserveManagedSkill({
			targetDir: target,
			id: skillId,
			digest: "a".repeat(64),
			sourceIdentity,
			manager: "hosted-manifest",
		});
		const ready = join(dirname(paths.serviceStateRoot), "skill-installer-ready");
		const moduleUrl = new URL("./managed-skill-reservation.ts", import.meta.url).href;
		const child = Bun.spawn(
			[
				process.execPath,
				"-e",
				`import { writeFileSync } from "node:fs";
const { installReservedManagedSkill } = await import(${JSON.stringify(moduleUrl)});
installReservedManagedSkill(${JSON.stringify({
					targetDir: target,
					id: skillId,
					digest: prepared.identity.digest,
					sourceIdentity,
					manager: "hosted-manifest",
				})}, () => {
  writeFileSync(${JSON.stringify(ready)}, "ready");
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 60_000);
}, { verify: () => true, discard: () => {} });`,
			],
			{ env: process.env, stdout: "pipe", stderr: "pipe" },
		);
		for (let attempt = 0; attempt < 200 && !existsSync(ready); attempt += 1) {
			await Bun.sleep(10);
		}
		expect(existsSync(ready)).toBe(true);
		child.kill("SIGKILL");
		expect(await child.exited).not.toBe(0);

		const ledgerPath = managedSkillReservationLedgerPath();
		const interruptedLedger = JSON.parse(readFileSync(ledgerPath, "utf8"));
		expect(interruptedLedger.reservations[target].digest).toBe("a".repeat(64));
		expect(interruptedLedger.pendingReservations[target]).toBeDefined();
		expect(readFileSync(join(target, "SKILL.md"), "utf8")).toBe("old committed tree\n");
		expect(() =>
			reserveManagedSkill({
				targetDir: target,
				id: skillId,
				sourceIdentity,
				manager: "hosted-manifest",
			}),
		).toThrow("pending installation that requires recovery");

		const hermesCommand = writeFakeHermesCli(paths);
		const manifest = baseManifest(
			paths,
			{ hermes: { enabled: true, run: runSettings(hermesCommand, ["gateway"]), services: {} } },
			{ projection: { skills: { entries: { [skillId]: { enabled: true, source } } } } },
		);
		const result = convergeRuntimeManifest(
			manifestLoad(manifest, "recover-killed-skill-installer"),
			paths,
			{ preparedHostedSourcedSkills: new Map([[skillId, prepared]]) },
		);

		expect([...result.installErrors, ...result.resourceProjectionErrors]).toEqual([]);
		expect(readFileSync(join(target, "SKILL.md"), "utf8")).toBe("verified tree\n");
		expect(managedSkillReservationState(target, skillId)).toBe("reserved");
		const committedLedger = JSON.parse(readFileSync(ledgerPath, "utf8"));
		expect(committedLedger.reservations[target].digest).toBe(prepared.identity.digest);
		expect(committedLedger.pendingReservations).toEqual({});
	});

	test("releases a stale hosted reservation after its Skill tree disappears", () => {
		const paths = tempRuntimePaths();
		ensureRuntimeStateDirs(paths);
		mkdirSync(paths.managedResourceRoot, { recursive: true });
		const skillId = "attio-composio-client-updates";
		const sourceIdentity = `github\0${skillId}\0https://github.com/Clawdi-AI/store\0skills/${skillId}\0${"a".repeat(40)}`;
		const target = join(paths.userHome, ".hermes", "skills", skillId);
		const hermesCommand = writeFakeHermesCli(paths);
		mkdirSync(target, { recursive: true });
		writeFileSync(join(target, "SKILL.md"), "historical test Skill\n");
		reserveManagedSkill({
			targetDir: target,
			id: skillId,
			manager: "hosted-manifest",
			sourceIdentity,
		});
		rmSync(target, { recursive: true });

		const manifest = baseManifest(
			paths,
			{ hermes: { enabled: true, run: runSettings(hermesCommand, ["gateway"]), services: {} } },
			{ projection: { skills: { entries: {} } } },
		);
		const result = convergeRuntimeManifest(
			manifestLoad(manifest, "stale-absent-hermes-skill"),
			paths,
		);

		expect([...result.installErrors, ...result.resourceProjectionErrors]).toEqual([]);
		expect(managedSkillReservationState(target, skillId)).toBe("unreserved");
		expect(existsSync(target)).toBe(false);
	});

	test("isolates per-Skill resource failures without starving later Skills", () => {
		const paths = tempRuntimePaths();
		const hermesCommand = writeFakeHermesCli(paths);
		const skillIds = ["a-fail", "b-ready", "c-ready"];
		const preparedSkills = new Map<string, PreparedHostedSkill>();
		const entries: NonNullable<RuntimeManifest["projection"]>["skills"] = { entries: {} };
		for (const skillId of skillIds) {
			const source = {
				type: "github" as const,
				url: "https://github.com/Clawdi-AI/store",
				path: `skills/${skillId}`,
				commit: "a".repeat(40),
			};
			preparedSkills.set(skillId, preparedTestSourcedSkill(skillId, source, `${skillId}\n`));
			entries.entries[skillId] = { enabled: true, source };
		}
		const failed = preparedSkills.get("a-fail");
		if (!failed) throw new Error("missing failing Skill fixture");
		if (!("tarBytes" in failed)) throw new Error("failing Skill fixture is not sourced");
		failed.tarBytes = Buffer.from("invalid archive");
		failed.identity.digest = createHash("sha256").update(failed.tarBytes).digest("hex");
		const manifest = baseManifest(
			paths,
			{
				hermes: {
					enabled: true,
					run: runSettings(hermesCommand, ["gateway"]),
					services: {},
				},
			},
			{ projection: { skills: entries } },
		);
		const result = convergeRuntimeManifest(manifestLoad(manifest, "skill-item-isolation"), paths, {
			preparedHostedSourcedSkills: preparedSkills,
		});

		expect(result.installErrors).toEqual([]);
		expect(result.resourceProjectionErrors).toEqual([
			"runtime hermes Skill projection failed: a-fail: prepared Skill archive could not be staged",
		]);
		expect(existsSync(join(paths.userHome, ".hermes", "skills", "a-fail"))).toBe(false);
		for (const skillId of ["b-ready", "c-ready"]) {
			expect(
				readFileSync(join(paths.userHome, ".hermes", "skills", skillId, "SKILL.md"), "utf8"),
			).toBe(`${skillId}\n`);
		}
	});

	test("keeps unmanaged Skill rejection fail-closed across the Skill domain", () => {
		const paths = tempRuntimePaths();
		const hermesCommand = writeFakeHermesCli(paths);
		const skillIds = ["a-unmanaged", "b-ready", "c-ready"];
		const preparedSkills = new Map<string, PreparedHostedSkill>();
		const entries: NonNullable<RuntimeManifest["projection"]>["skills"] = { entries: {} };
		for (const skillId of skillIds) {
			const source = {
				type: "github" as const,
				url: "https://github.com/Clawdi-AI/store",
				path: `skills/${skillId}`,
				commit: "a".repeat(40),
			};
			preparedSkills.set(skillId, preparedTestSourcedSkill(skillId, source, `${skillId}\n`));
			entries.entries[skillId] = { enabled: true, source };
		}
		const unmanaged = join(paths.userHome, ".hermes", "skills", "a-unmanaged");
		mkdirSync(unmanaged, { recursive: true });
		writeFileSync(join(unmanaged, "SKILL.md"), "tenant owned\n");
		const manifest = baseManifest(
			paths,
			{
				hermes: {
					enabled: true,
					run: runSettings(hermesCommand, ["gateway"]),
					services: {},
				},
			},
			{ projection: { skills: entries } },
		);
		const result = convergeRuntimeManifest(
			manifestLoad(manifest, "skill-domain-integrity"),
			paths,
			{
				preparedHostedSourcedSkills: preparedSkills,
			},
		);

		expect(result.resourceProjectionErrors.join("\n")).toContain(
			`refusing to replace unmanaged a-unmanaged skill at ${unmanaged}`,
		);
		expect(readFileSync(join(unmanaged, "SKILL.md"), "utf8")).toBe("tenant owned\n");
		expect(existsSync(join(paths.userHome, ".hermes", "skills", "b-ready"))).toBe(false);
		expect(existsSync(join(paths.userHome, ".hermes", "skills", "c-ready"))).toBe(false);
	});

	test("installs a bundled OpenClaw Skill from cleaned runtime-readable staging", () => {
		const paths = tempRuntimePaths();
		const command = join(paths.userHome, ".local", "bin", "openclaw");
		const unitPath = join(paths.systemdUserRoot, "openclaw-gateway.service");
		const sourceLog = join(dirname(paths.serviceStateRoot), "openclaw-skill-source.log");
		writeFakeGatewayCli({
			path: command,
			runtime: "openclaw",
			unitPath,
			skillInstallSourceLog: sourceLog,
		});
		const manifest = baseManifest(
			paths,
			{ openclaw: { enabled: true, run: runSettings(command, ["gateway"]), services: {} } },
			{ projection: { skills: { entries: { iforge: { enabled: true, version: 1 } } } } },
		);
		const target = join(paths.userHome, ".openclaw", "workspace", "skills", "iforge");
		const packageSource = resolve(
			import.meta.dir,
			"../..",
			"skills",
			"hosted-versions",
			"1",
			"iforge",
		);

		const result = convergeRuntimeManifest(manifestLoad(manifest, "bundled-openclaw-skill"), paths);

		expect(result.installErrors).toEqual([]);
		const stagedSource = readFileSync(sourceLog, "utf8").trim();
		expect(stagedSource).not.toBe(packageSource);
		expect(stagedSource.startsWith(join(tmpdir(), "clawdi-managed-skill-"))).toBe(true);
		expect(existsSync(stagedSource)).toBe(false);
		expect(readFileSync(join(target, "SKILL.md"))).toEqual(
			readFileSync(join(packageSource, "SKILL.md")),
		);
		expect(statSync(target).mode & 0o777).toBe(0o755);
		expect(statSync(join(target, "SKILL.md")).mode & 0o777).toBe(0o644);
		expect(existsSync(join(target, ".clawdi-managed.json"))).toBe(false);
		expect(shouldIgnoreUserSkill(target, "iforge")).toBe(true);

		const targetBeforeRetiredReceipts = statSync(target).ino;
		const legacyReceiptDirectory = join(dirname(target), ".clawdi-manifest-receipts");
		const platformReceiptDirectory = join(paths.managedResourceRoot, "skill-receipts");
		mkdirSync(legacyReceiptDirectory, { recursive: true });
		writeFileSync(join(legacyReceiptDirectory, "clawdi.json"), "{}\n");
		mkdirSync(join(platformReceiptDirectory, "openclaw"), { recursive: true });
		writeFileSync(join(platformReceiptDirectory, "openclaw", "clawdi.json"), "{}\n");
		const reconverged = convergeRuntimeManifest(
			manifestLoad({ ...manifest, generation: 2 }, "bundled-openclaw-skill-retired-receipts"),
			paths,
		);
		expect([...reconverged.installErrors, ...reconverged.resourceProjectionErrors]).toEqual([]);
		expect(statSync(target).ino).toBe(targetBeforeRetiredReceipts);
		expect(readFileSync(sourceLog, "utf8").trim().split("\n")).toHaveLength(1);
		expect(existsSync(legacyReceiptDirectory)).toBe(false);
		expect(existsSync(platformReceiptDirectory)).toBe(false);

		const targetBeforeRepair = statSync(target).ino;
		writeFileSync(join(target, "SKILL.md"), "tenant mutation\n");
		const repaired = convergeRuntimeManifest(
			manifestLoad({ ...manifest, generation: 3 }, "bundled-openclaw-skill-repair"),
			paths,
		);
		expect([...repaired.installErrors, ...repaired.resourceProjectionErrors]).toEqual([]);
		expect(readFileSync(join(target, "SKILL.md"))).toEqual(
			readFileSync(join(packageSource, "SKILL.md")),
		);
		expect(statSync(target).ino).not.toBe(targetBeforeRepair);

		writeFileSync(join(target, "SKILL.md"), "tenant mutation\n");
		rmSync(managedSkillReservationLedgerPath(), { force: true });
		const withoutLedger = convergeRuntimeManifest(
			manifestLoad({ ...manifest, generation: 4 }, "bundled-openclaw-skill-restart"),
			paths,
		);
		expect(withoutLedger.installErrors).toEqual([]);
		expect(withoutLedger.resourceProjectionErrors.join("\n")).toContain(
			`refusing to replace unmanaged iforge skill at ${target}`,
		);
		expect(readFileSync(join(target, "SKILL.md"), "utf8")).toBe("tenant mutation\n");
		expect(shouldIgnoreUserSkill(target, "iforge")).toBe(false);
	});

	test("ignores legacy OpenClaw markers without reservation-backed ownership", () => {
		const paths = tempRuntimePaths();
		process.env.CLAWDI_RUNTIME_MODE = "hosted";
		const command = join(paths.userHome, ".local", "bin", "openclaw");
		writeFakeGatewayCli({
			path: command,
			runtime: "openclaw",
			unitPath: join(paths.systemdUserRoot, "openclaw-gateway.service"),
		});
		const openClawWorkspaceRoot = join(paths.userHome, ".openclaw", "workspace");
		const target = join(openClawWorkspaceRoot, "skills", "iforge");
		const source = resolve(import.meta.dir, "../..", "skills", "hosted-versions", "1", "iforge");
		mkdirSync(dirname(target), { recursive: true });
		cpSync(source, target, { recursive: true });
		chmodSync(target, 0o755);
		chmodSync(join(target, "SKILL.md"), 0o644);
		writeFileSync(
			join(target, ".clawdi-managed.json"),
			`${JSON.stringify({ managedBy: "iforge runtime init", skillName: "iforge" })}\n`,
		);
		const manifest = baseManifest(
			paths,
			{ openclaw: { enabled: true, run: runSettings(command, ["gateway"]), services: {} } },
			{ projection: { skills: { entries: {} } } },
		);
		const result = convergeRuntimeManifest(manifestLoad(manifest, "legacy-openclaw-remove"), paths);
		expect(result.installErrors).toEqual([]);
		expect(existsSync(target)).toBe(true);
		expect(existsSync(join(target, ".clawdi-managed.json"))).toBe(true);
	});

	test("removes stale unit files before activation and authority commit", () => {
		const paths = tempRuntimePaths();
		const staleSystemUnit = join(paths.systemdSystemRoot, `${RUNTIME_SYSTEM_UNIT_SIDECAR}.service`);
		const staleUserUnit = join(paths.systemdUserRoot, "clawdi-old.service");
		const staleDropIn = join(
			paths.systemdUserRoot,
			"openclaw-gateway.service.d",
			RUNTIME_SYSTEMD_DROP_IN_FILE,
		);
		const staleUserEnvironment = join(paths.systemdEnvRoot, "clawdi-old.service.env");
		const staleDropInEnvironment = join(paths.systemdEnvRoot, "openclaw-gateway.service.env");
		const staleFiles = [
			staleSystemUnit,
			staleUserUnit,
			staleDropIn,
			staleUserEnvironment,
			staleDropInEnvironment,
		];
		for (const path of staleFiles) {
			mkdirSync(dirname(path), { recursive: true });
			writeFileSync(path, `${GENERATED_RUNTIME_SYSTEMD_FILE_HEADER}\nstale\n`);
		}
		for (const path of [
			paths.systemdSystemRoot,
			paths.systemdUserRoot,
			paths.systemdEnvRoot,
			dirname(staleDropIn),
		]) {
			chmodSync(path, 0o755);
		}
		const systemctl = join(dirname(paths.serviceStateRoot), "systemctl-success.sh");
		writeFileSync(systemctl, "#!/bin/sh\nexit 0\n");
		chmodSync(systemctl, 0o755);
		process.env.CLAWDI_SYSTEMCTL_PATH = systemctl;
		const manifest = baseManifest(paths, {});
		let activated = false;
		let committed = false;

		const result = convergeRuntimeManifest(
			manifestLoad(manifest, "systemd-post-commit-gc"),
			paths,
			{
				cacheLastGood: false,
				commitAuthority: () => {
					expect(activated).toBe(true);
					for (const path of staleFiles) expect(existsSync(path)).toBe(false);
					committed = true;
				},
				systemdApply: {
					activateEgressPrerequisite: successfulPrerequisiteActivation,
					activate: (signal) => {
						expect(signal.staleSystemUnits).toEqual([`${RUNTIME_SYSTEM_UNIT_SIDECAR}.service`]);
						expect(signal.staleUserUnits).toEqual([
							"clawdi-old.service",
							"openclaw-gateway.service",
						]);
						for (const path of staleFiles) expect(existsSync(path)).toBe(false);
						activated = true;
						return { applied: true, systemUnitsChanged: [], userUnitsChanged: [] };
					},
				},
			},
		);

		expect(result.installErrors).toEqual([]);
		expect(committed).toBe(true);
		for (const path of staleFiles) expect(existsSync(path)).toBe(false);
	});

	// --- 03.3-04: ownership proved from a file's own bytes -------------------------------------
	// `~/.config/systemd/user` is a namespace upstream clawdi writes into as well, so everything
	// below is about one distinction: an entry this product can PROVE it wrote, versus an entry
	// that merely shares a name prefix with it. The second kind is somebody else's install.
	//
	// Upstream clawdi's own generated header, restated verbatim. iforge has no production
	// deployment to stay compatible with, so that spelling marks ANOTHER product's unit: this
	// product must never treat it as its own (user directive, 2026-09-30).
	const UPSTREAM_GENERATED_RUNTIME_SYSTEMD_FILE_HEADER =
		"# Generated by clawdi runtime init. Do not edit inside hosted runtime.";

	test("accepts only this product's generated header and rejects upstream clawdi's", () => {
		expect(GENERATED_RUNTIME_SYSTEMD_FILE_HEADER).not.toBe(
			UPSTREAM_GENERATED_RUNTIME_SYSTEMD_FILE_HEADER,
		);
		expect(
			isGeneratedRuntimeSystemdFile(
				`${GENERATED_RUNTIME_SYSTEMD_FILE_HEADER}\n[Unit]\nDescription=ours\n`,
			),
		).toBe(true);
		for (const contents of [
			"",
			"[Unit]\nDescription=another product\n",
			"# Generated by some other runtime init. Do not edit inside hosted runtime.\n",
			`${UPSTREAM_GENERATED_RUNTIME_SYSTEMD_FILE_HEADER}\n[Unit]\nDescription=upstream clawdi\n`,
			// The marker is a HEADER, not a substring: a foreign unit that merely quotes it
			// somewhere in its body is not this product's to reclaim.
			`[Unit]\n${GENERATED_RUNTIME_SYSTEMD_FILE_HEADER}\n`,
		]) {
			expect(isGeneratedRuntimeSystemdFile(contents)).toBe(false);
		}
	});

	test("stamps every generated systemd file it writes with only the current header", () => {
		const paths = tempRuntimePaths();
		const command = join(paths.userHome, ".local", "bin", "openclaw");
		writeFakeGatewayCli({
			path: command,
			runtime: "openclaw",
			unitPath: join(paths.systemdUserRoot, "openclaw-gateway.service"),
		});
		const result = convergeRuntimeManifest(
			manifestLoad(
				baseManifest(paths, {
					openclaw: { enabled: true, run: runSettings(command, ["gateway"]), services: {} },
				}),
				"generated-header-writer",
			),
			paths,
		);
		expect(result.installErrors).toEqual([]);

		const generated: string[] = [];
		const walk = (root: string): void => {
			if (!existsSync(root)) return;
			for (const entry of readdirSync(root, { withFileTypes: true })) {
				const path = join(root, entry.name);
				if (entry.isDirectory()) {
					walk(path);
					continue;
				}
				if (!entry.isFile()) continue;
				const contents = readFileSync(path, "utf-8");
				if (!isGeneratedRuntimeSystemdFile(contents)) continue;
				generated.push(path);
				expect(contents.startsWith(GENERATED_RUNTIME_SYSTEMD_FILE_HEADER)).toBe(true);
				expect(contents).not.toContain(UPSTREAM_GENERATED_RUNTIME_SYSTEMD_FILE_HEADER);
			}
		};
		walk(paths.systemdUserRoot);
		walk(paths.systemdEnvRoot);
		// A walk that judged nothing would pass for the wrong reason.
		expect(generated.length).toBeGreaterThan(0);
	});

	test("recognises a managed-Skill ownership receipt under either marker prefix", () => {
		// Receipts are read back off disk on user machines. The reader has to accept the legacy
		// spelling BEFORE any writer moves, or every reservation already recorded is orphaned --
		// and this same predicate is the archive validator that refuses an uploaded Skill trying
		// to forge an ownership marker, so widening it refuses strictly more, never less.
		for (const marker of [".clawdi-managed", ".iforge-managed"]) {
			expect(isReservedSkillArchivePath(`review-pr/${marker}.json`)).toBe(true);
			expect(isReservedSkillArchivePath(`${marker}-whatsapp-auth.json`)).toBe(true);
			expect(isReservedSkillArchivePath(`a/b/${marker.toUpperCase()}.JSON`)).toBe(true);
			expect(isReservedSkillArchivePath(`a\\b\\${marker}.json`)).toBe(true);
		}
		for (const path of [
			"review-pr/SKILL.md",
			// No leading dot: an ordinary file whose name merely starts with the product name.
			"clawdi-managed.json",
			"iforge-managed.json",
		]) {
			expect(isReservedSkillArchivePath(path)).toBe(false);
		}
	});

	test("reclaims only the systemd entries it can prove it wrote, never upstream clawdi's", () => {
		const paths = tempRuntimePaths();
		// Another product's install, sitting in the SHARED user systemd root: the legacy name
		// prefix, and no generated header anywhere in it. None of this is ours to touch, and a
		// converge that reclaimed it would be a denial of service against software that never
		// mentioned us (T-03.3-14).
		const foreignUnit = join(paths.systemdUserRoot, "clawdi-foreign.service");
		const foreignEnvironment = join(paths.systemdEnvRoot, "clawdi-foreign.service.env");
		const foreignDropIn = join(
			paths.systemdUserRoot,
			"clawdi-foreign.service.d",
			RUNTIME_SYSTEMD_DROP_IN_FILE,
		);
		// Upstream clawdi's own generated unit on the same machine: its header is not this product's,
		// so it is foreign too (no pre-rename iforge hosts exist to recognise; user directive
		// 2026-09-30).
		const upstreamUnit = join(paths.systemdUserRoot, "clawdi-upstream.service");
		const foreign = new Map([
			[foreignUnit, "[Unit]\nDescription=Upstream gateway\n\n[Service]\nExecStart=/bin/true\n"],
			[foreignEnvironment, "FOREIGN=1\n"],
			[foreignDropIn, "[Service]\nExecStart=\nExecStart=/bin/true\n"],
			[
				upstreamUnit,
				"# Generated by clawdi runtime init. Do not edit inside hosted runtime.\n[Unit]\nDescription=upstream\n",
			],
		]);
		// The control, and the reason this test fails for the right reason: the SAME name prefix,
		// but stamped with the header this product writes. A decoy-only test would pass just as well
		// against a converge that had stopped collecting anything at all -- which is a different bug
		// with the same green tick.
		const ownedUnit = join(paths.systemdUserRoot, "clawdi-old.service");
		const ownedEnvironment = join(paths.systemdEnvRoot, "clawdi-old.service.env");
		const owned = new Map([
			[ownedUnit, `${GENERATED_RUNTIME_SYSTEMD_FILE_HEADER}\n[Unit]\nDescription=ours\n`],
			[ownedEnvironment, `${GENERATED_RUNTIME_SYSTEMD_FILE_HEADER}\nOWNED=1\n`],
		]);
		for (const [path, contents] of [...foreign, ...owned]) {
			mkdirSync(dirname(path), { recursive: true });
			writeFileSync(path, contents);
		}
		for (const path of [paths.systemdUserRoot, paths.systemdEnvRoot, dirname(foreignDropIn)]) {
			chmodSync(path, 0o755);
		}

		// Upstream #1194 retired the user-unit mutation plan this test also asserted on; the
		// activation signal below is now the planned-collection evidence, so a converge that never
		// reached its cleanup step still fails here (the owned control must be collected).
		const systemctl = join(dirname(paths.serviceStateRoot), "systemctl-success.sh");
		writeFileSync(systemctl, "#!/bin/sh\nexit 0\n");
		chmodSync(systemctl, 0o755);
		process.env.CLAWDI_SYSTEMCTL_PATH = systemctl;
		let staleUserUnits: string[] = [];
		const result = convergeRuntimeManifest(
			manifestLoad(baseManifest(paths, {}), "foreign-unit-decoy"),
			paths,
			{
				cacheLastGood: false,
				systemdApply: {
					activateEgressPrerequisite: successfulPrerequisiteActivation,
					activate: (signal) => {
						staleUserUnits = [...signal.staleUserUnits];
						return { applied: true, systemUnitsChanged: [], userUnitsChanged: [] };
					},
				},
			},
		);

		expect(result.installErrors).toEqual([]);
		expect(staleUserUnits).not.toContain("clawdi-foreign.service");
		expect(staleUserUnits).toContain("clawdi-old.service");
		for (const [path, contents] of foreign) {
			expect(existsSync(path)).toBe(true);
			expect(readFileSync(path, "utf-8")).toBe(contents);
		}
		for (const path of owned.keys()) expect(existsSync(path)).toBe(false);
	});

	test("runs the official latest OpenClaw installer with a sanitized environment and timeout", () => {
		const paths = tempRuntimePaths();
		const home = paths.userHome;
		const commandPath = join(home, ".local", "bin", "openclaw");
		const configPath = join(home, ".openclaw", "openclaw.json");
		const installedVersionPath = join(home, ".openclaw", "installed-version");
		const fixtureRoot = dirname(paths.serviceStateRoot);
		const commandFixturePath = join(fixtureRoot, "openclaw");
		const installerPath = join(fixtureRoot, "install-openclaw.sh");
		const installerResultPath = join(fixtureRoot, "installer-result");
		const installerLog = join(fixtureRoot, "installer.log");
		const installerEnvironmentLog = join(fixtureRoot, "installer-environment.log");
		const installedVersion = "2026.7.1-2";
		const configWriterVersion = "2026.8.1.beta.1";
		const openClawInstallerOverrides = [
			"OPENCLAW_HOME",
			"OPENCLAW_STATE_DIR",
			"OPENCLAW_CONFIG_PATH",
			"OPENCLAW_PREFIX",
			"OPENCLAW_VERSION",
			"OPENCLAW_INSTALL_METHOD",
			"OPENCLAW_GIT_DIR",
			"OPENCLAW_GIT_UPDATE",
		] as const;
		const config = {
			meta: {
				lastTouchedVersion: configWriterVersion,
				migrations: { applied: ["agents.entries"] },
			},
			agents: { entries: [{ id: "main" }] },
		};

		mkdirSync(dirname(commandPath), { recursive: true });
		mkdirSync(dirname(configPath), { recursive: true });
		writeFileSync(installedVersionPath, `${installedVersion}\n`);
		writeFileSync(configPath, `${JSON.stringify(config)}\n`);
		writeFileSync(
			commandFixturePath,
			`#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  "--version") printf 'OpenClaw %s\n' "$(cat '${installedVersionPath}')" ;;
  "agents list --json") printf '[{"id":"main","workspace":"%s"}]\n' "$HOME/.openclaw/workspace" ;;
  *) exit 0 ;;
esac
`,
		);
		chmodSync(commandFixturePath, 0o700);
		writeFileSync(
			installerPath,
			`#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$#" "$@" >> '${installerLog}'
printf '%s\n' "$HOME" > '${installerEnvironmentLog}'
for name in ${openClawInstallerOverrides.join(" ")}; do
  if [ "\${!name+x}" = x ]; then printf '%s\n' "$name" >> '${installerEnvironmentLog}'; fi
done
cp '${installerResultPath}' '${installedVersionPath}'
cp '${commandFixturePath}' '${commandPath}'
`,
		);
		chmodSync(installerPath, 0o700);
		Object.assign(process.env, {
			OPENCLAW_HOME: "stale-openclaw-home",
			OPENCLAW_STATE_DIR: dirname(configPath),
			OPENCLAW_CONFIG_PATH: configPath,
			OPENCLAW_PREFIX: "stale-openclaw-prefix",
			OPENCLAW_VERSION: "stale-openclaw-version",
			OPENCLAW_INSTALL_METHOD: "stale-openclaw-install-method",
			OPENCLAW_GIT_DIR: "stale-openclaw-git-dir",
			OPENCLAW_GIT_UPDATE: "stale-openclaw-git-update",
		});
		process.env.CLAWDI_RUNTIME_ALLOW_TEST_INSTALLERS = "1";
		process.env.CLAWDI_RUNTIME_TEST_OPENCLAW_INSTALLER = `file://${installerPath}`;
		process.env.CLAWDI_RUNTIME_INSTALL_TIMEOUT = "invalid";

		const install = {
			authority: "official" as const,
			method: "official-installer" as const,
			url: OFFICIAL_INSTALL_URLS.openclaw,
			home,
			args: officialInstallArgs("openclaw", home),
		};
		const load = manifestLoad(
			baseManifest(paths, {
				openclaw: {
					enabled: true,
					install,
					run: runSettings(commandPath, ["gateway", "run"]),
					services: {},
				},
			}),
			"hosted-v2-openclaw-official-latest",
		);

		writeFileSync(installerResultPath, `${installedVersion}\n`);
		const warnings: string[] = [];
		const originalWarn = console.warn;
		let converged: ReturnType<typeof convergeRuntimeManifest>;
		try {
			console.warn = (message) => warnings.push(String(message));
			converged = convergeRuntimeManifest(load, paths, {
				executeOfficialServiceInstallers: false,
			});
		} finally {
			console.warn = originalWarn;
		}
		expect(converged.installErrors).toEqual([]);
		expect(warnings).toEqual([
			"CLAWDI_RUNTIME_INSTALL_TIMEOUT must be a valid positive integer; using 1800000ms",
		]);
		expect(execFileSync(commandPath, ["--version"], { encoding: "utf8" }).trim()).toBe(
			`OpenClaw ${installedVersion}`,
		);
		expect(JSON.parse(readFileSync(configPath, "utf8"))).toEqual(config);
		expect(readFileSync(installerEnvironmentLog, "utf8").trim().split("\n")).toEqual([home]);
		const expectedArgs = officialInstallArgs("openclaw", home);
		expect(readFileSync(installerLog, "utf8").trim().split("\n")).toEqual([
			String(expectedArgs.length),
			...expectedArgs,
		]);
		expect(expectedArgs).not.toContain("--version");
	});

	test("accepts an installed runtime command symlink as an exact transaction target", () => {
		const paths = tempRuntimePaths();
		const appRoot = join(paths.userHome, ".openclaw");
		const commandPath = join(paths.userHome, ".local", "bin", "openclaw");
		const commandTarget = join(appRoot, "openclaw-entrypoint");
		mkdirSync(dirname(commandPath), { recursive: true });
		mkdirSync(dirname(commandTarget), { recursive: true });
		writeFileSync(
			commandTarget,
			`#!/bin/sh
if [ "$*" = "agents list --json" ]; then
  printf '[{"id":"main","workspace":"%s"}]\\n' "$HOME/.openclaw/workspace"
fi
exit 0
`,
		);
		chmodSync(commandTarget, 0o755);
		symlinkSync(commandTarget, commandPath);
		const manifest = baseManifest(paths, {
			openclaw: {
				enabled: true,
				install: {
					authority: "official",
					method: "official-installer",
					url: OFFICIAL_INSTALL_URLS.openclaw,
					home: paths.userHome,
					args: officialInstallArgs("openclaw", paths.userHome),
				},
				run: runSettings(commandPath, ["gateway", "run"]),
				services: {},
			},
		});

		const result = convergeRuntimeManifest(manifestLoad(manifest, "symlinked-openclaw"), paths);
		expect(result.installErrors).toEqual([]);
		expect(readlinkSync(commandPath)).toBe(commandTarget);
	});

	test("0.14.18 does not recursively chown tenant home and keeps tenant writes owned", () => {
		const numericPrivilegeToolPath = ["/usr/bin/set", "priv"].join("");
		if (process.geteuid?.() !== 0 || !existsSync(numericPrivilegeToolPath)) return;
		const paths = tempRuntimePaths();
		const fixtureRoot = dirname(paths.serviceStateRoot);
		const runtimeUid = 10_001;
		const runtimeGid = 10_001;
		const appRoot = join(paths.userHome, ".openclaw");
		const localRoot = join(paths.userHome, ".local");
		const binDir = join(paths.userHome, ".local", "bin");
		const commandPath = join(binDir, "openclaw");
		const skillProjectionLog = join(appRoot, "skill-projection-owners.log");
		const rootOwnedSentinel = join(paths.userHome, "root-owned-sentinel");

		chmodSync(fixtureRoot, 0o755);
		mkdirSync(paths.userHome, { recursive: true });
		mkdirSync(paths.clawdiHome, { recursive: true });
		mkdirSync(binDir, { recursive: true });
		mkdirSync(appRoot, { recursive: true });
		writeFileSync(
			commandPath,
			`#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = "10001"
case "$*" in
  "--version")
    printf '%s\\n' 'OpenClaw test-version'
    ;;
  "agents list --json")
    printf '[{"id":"main","workspace":"%s"}]\\n' "$HOME/.openclaw/workspace"
    ;;
  "skills install "*)
    printf '%s:%s\\n' "$(id -u)" "$(id -g)" > '${skillProjectionLog}'
    exit 45
    ;;
  *) exit 64 ;;
esac
`,
		);
		chmodSync(commandPath, 0o700);
		for (const path of [
			paths.userHome,
			paths.clawdiHome,
			localRoot,
			binDir,
			appRoot,
			commandPath,
		]) {
			chownSync(path, runtimeUid, runtimeGid);
		}
		writeFileSync(rootOwnedSentinel, "preserve root ownership\n", { mode: 0o600 });

		process.env.CLAWDI_RUNTIME_USER = "clawdi";
		process.env.CLAWDI_RUNTIME_UID = String(runtimeUid);
		process.env.CLAWDI_RUNTIME_GID = String(runtimeGid);
		const hostedRuntimeContract = {
			expectedIdentity: {
				home: paths.userHome,
				user: "clawdi",
				uid: runtimeUid,
				gid: runtimeGid,
			},
			resolveUserIdentity: () => ({ uid: runtimeUid, gid: runtimeGid }),
		};
		const manifest = baseManifest(
			paths,
			{
				openclaw: {
					enabled: true,
					install: {
						authority: "official",
						method: "official-installer",
						url: OFFICIAL_INSTALL_URLS.openclaw,
						home: paths.userHome,
						args: officialInstallArgs("openclaw", paths.userHome),
					},
					run: runSettings(commandPath, ["gateway", "run"]),
					services: {},
				},
			},
			{ projection: { skills: { entries: { iforge: { enabled: true, version: 1 } } } } },
		);

		const result = convergeRuntimeManifest(
			manifestLoad(manifest, "root-openclaw-ownership"),
			paths,
			{
				executeOfficialServiceInstallers: false,
				hostedRuntimeContract,
			},
		);
		expect(result.installErrors).toEqual([]);
		expect(result.resourceProjectionErrors.join("\n")).toContain(
			"OpenClaw official Skill install failed: exit code 45 without output",
		);
		expect(readFileSync(skillProjectionLog, "utf8")).toBe(`${runtimeUid}:${runtimeGid}\n`);
		expect([statSync(skillProjectionLog).uid, statSync(skillProjectionLog).gid]).toEqual([
			runtimeUid,
			runtimeGid,
		]);
		expect(readFileSync(rootOwnedSentinel, "utf8")).toBe("preserve root ownership\n");
		expect([statSync(rootOwnedSentinel).uid, statSync(rootOwnedSentinel).gid]).toEqual([0, 0]);
		expect(statSync(rootOwnedSentinel).mode & 0o777).toBe(0o600);
		expect([statSync(paths.daemonAuthToken).uid, statSync(paths.daemonAuthToken).gid]).toEqual([
			0, 0,
		]);
		expect(statSync(paths.daemonAuthToken).mode & 0o777).toBe(0o600);
	});

	test("rejects a malformed Hermes MCP patch before Apply", () => {
		const paths = tempRuntimePaths();
		const hermesCommand = join(paths.userHome, ".local", "bin", "hermes");
		const hermesConfig = join(paths.userHome, ".hermes", "config.yaml");
		writeFakeGatewayCli({
			path: hermesCommand,
			runtime: "hermes",
			unitPath: join(paths.systemdUserRoot, "hermes-gateway.service"),
		});
		mkdirSync(dirname(hermesConfig), { recursive: true });
		writeFileSync(hermesConfig, "mcp_servers: []\n");
		const previousConfig = readFileSync(hermesConfig);
		const manifest = baseManifest(
			paths,
			{
				hermes: {
					enabled: true,
					run: runSettings(hermesCommand, ["gateway", "run"]),
					services: {},
				},
			},
			{
				projection: {
					mcp: {
						servers: {
							clawdi: {
								url: "https://mcp.example.test/clawdi",
								transport: "streamable-http",
								headers: {
									Authorization: {
										secretRef: "secret://clawdi/auth-token",
										prefix: "Bearer ",
									},
								},
							},
						},
					},
				},
			},
		);

		expect(() =>
			convergeRuntimeManifest(manifestLoad(manifest, "inline-hermes-patch-failure"), paths),
		).toThrow(/config field mcp_servers must be an object/);
		expect(readFileSync(hermesConfig)).toEqual(previousConfig);
	});

	test("garbage collects stale run configs when a runtime is removed", () => {
		const paths = tempRuntimePaths();
		writeFakeGatewayCli({
			path: join(paths.userHome, ".local", "bin", "openclaw"),
			runtime: "openclaw",
			unitPath: join(paths.systemdUserRoot, "openclaw-gateway.service"),
		});
		writeFakeGatewayCli({
			path: join(paths.userHome, ".local", "bin", "hermes"),
			runtime: "hermes",
			unitPath: join(paths.systemdUserRoot, "hermes-gateway.service"),
		});
		const initialManifest = baseManifest(paths, {
			hermes: {
				enabled: true,
				run: runSettings(join(paths.userHome, ".local", "bin", "hermes"), ["gateway", "run"]),
				services: {},
			},
			openclaw: {
				enabled: true,
				run: runSettings(join(paths.userHome, ".local", "bin", "openclaw"), ["gateway", "run"]),
				services: {},
			},
		});
		const openclawRunConfig = runtimeRunConfigPath("openclaw", paths);
		const hermesRunConfig = runtimeRunConfigPath("hermes", paths);

		const initial = convergeRuntimeManifest(manifestLoad(initialManifest, "inline-initial"), paths);
		expect(initial.installErrors).toEqual([]);
		expect(existsSync(openclawRunConfig)).toBe(true);
		expect(existsSync(hermesRunConfig)).toBe(true);

		const nextManifest = baseManifest(
			paths,
			{
				hermes: {
					enabled: true,
					run: runSettings(join(paths.userHome, ".local", "bin", "hermes"), ["gateway", "run"]),
					services: {},
				},
			},
			{ generation: 2, issuedAt: "2026-07-01T00:03:00.000Z" },
		);
		const next = convergeRuntimeManifest(manifestLoad(nextManifest, "inline-removed"), paths);

		expect(next.installErrors).toEqual([]);
		expect(existsSync(openclawRunConfig)).toBe(false);
		expect(existsSync(hermesRunConfig)).toBe(true);
	});

	test("resolves runtime secret refs only by exact canonical secret:// keys", () => {
		expect(
			runtimeSecretValue(
				{ "secret://providers/default/api-key": "sk-exact" },
				"secret://providers/default/api-key",
			),
		).toBe("sk-exact");
		expect(runtimeSecretValue({}, "secret://providers/default/api-key")).toBeNull();
		expect(runtimeSecretValue({}, "providers/default/api-key")).toBeNull();
		expect(() => normalizeSecretValues({ "env://CLAWDI_AUTH_TOKEN": "deployment-token" })).toThrow(
			"runtime secret value key must be a canonical secret:// reference",
		);
		expect(() => normalizeSecretValues({ "providers/default/api-key": "sk-alias" })).toThrow(
			"runtime secret value key must be a canonical secret:// reference",
		);
	});

	test("uses bundle secretValues instead of stale process env", () => {
		process.env.OPENCLAW_GATEWAY_TOKEN = "stale-process-token";
		const projected = normalizeSecretValues({
			"secret://runtime/openclaw/gateway-token": "bundle-token",
		});
		expect(runtimeSecretValue(projected, "secret://runtime/openclaw/gateway-token")).toBe(
			"bundle-token",
		);
		expect(runtimeSecretValue(projected, "env://OPENCLAW_GATEWAY_TOKEN")).toBeNull();
	});

	test("installs the pinned Files companion post-boot and reconverges idempotently", () => {
		const paths = tempRuntimePaths();
		const binary = "#!/bin/sh\nprintf 'test File Browser'\n";
		const manifest = fileBrowserManifest(paths, { generation: 1, binary });
		let downloads = 0;
		let activations = 0;
		let authorityCommits = 0;
		const options = {
			fileBrowserInstallOptions: {
				serviceIsolation: testFileBrowserServiceIsolation,
				download: (_url: string, destination: string) => {
					downloads++;
					writeFileSync(destination, binary);
				},
				versionProbe: () => `${FILE_BROWSER_VERSION} ${FILE_BROWSER_COMMIT.slice(0, 7)}`,
			},
			fileBrowserReadinessProbe: (url: string) => url === "http://127.0.0.1:9120/health",
			systemdApply: fileBrowserApplyHooks({ onActivate: () => activations++ }),
			commitAuthority: () => authorityCommits++,
		};

		const first = convergeRuntimeManifest(fileBrowserManifestLoad(manifest), paths, options);
		expect(first.installErrors).toEqual([]);
		expect(downloads).toBe(1);
		expect(activations).toBe(1);
		expect(authorityCommits).toBe(1);
		const active = fileBrowserBinaryPath(paths, binary);
		expect(readFileSync(active, "utf8")).toBe(binary);
		const config = readFileSync(paths.fileBrowserConfig, "utf8");
		expect(parseYaml(config)).toMatchObject({
			server: {
				database: join(paths.fileBrowserStateRoot, "filebrowser.db"),
				sources: [
					{
						config: {
							rules: [{ folderPath: "/", ignoreSymlinks: true }],
						},
					},
				],
			},
			userDefaults: {
				sidebar: { sticky: false },
				listing: { showHidden: true },
				account: {
					lockPassword: true,
					disableSettings: false,
					loginMethod: "jwt",
					permissions: {
						admin: false,
						api: false,
						modify: true,
						share: false,
						realtime: false,
						delete: true,
						create: true,
						download: true,
					},
				},
			},
		});
		expect(config).toContain("listen: 0.0.0.0");
		expect(config).toContain("port: 9120");
		expect(config).toContain("path: /home/iforge");
		expect(config).not.toContain("ignoreHidden");
		expect(config).toContain("ignoreSymlinks: true");
		expect(config).toContain("disableWebDAV: true");
		expect(config).toContain("password:\n      enabled: false");
		expect(config).toContain("share: false");
		const unitPath = join(
			paths.systemdSystemRoot,
			`${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service`,
		);
		const unit = readFileSync(unitPath, "utf8");
		expect(first.outputs.systemdSystemUnits).toContain(unitPath);
		expect(first.outputs.systemdUserUnits).not.toContain(
			join(paths.systemdUserRoot, `${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service`),
		);
		expect(unit).not.toContain("RootDirectory=");
		expect(unit).toContain(`User=${TEST_RUNTIME_USER}`);
		expect(unit).toContain(`Group=${process.getegid?.() ?? 0}`);
		expect(unit).toContain("ProtectHome=tmpfs");
		expect(unit).toContain(`BindPaths=${paths.userHome}`);
		expect(unit).toContain(`StateDirectory=${SYSTEMD_FILE_BROWSER_STATE_DIRECTORY}`);
		expect(unit).toContain("StateDirectoryMode=0700");
		expect(unit).toContain(`RuntimeDirectory=${SYSTEMD_FILE_BROWSER_STATE_DIRECTORY}`);
		expect(unit).toContain("RuntimeDirectoryMode=0700");
		expect(unit).not.toContain("OpenFile=");
		expect(unit).not.toContain("LoadCredential=");
		expect(unit).toContain(`ReadWritePaths=${paths.userHome}`);
		expect(unit).toContain(`BindReadOnlyPaths=${active}:${paths.fileBrowserServiceBinary}:norbind`);
		expect(unit).toContain(
			`BindReadOnlyPaths=${paths.fileBrowserConfig}:${dirname(paths.fileBrowserServiceBinary)}/filebrowser.yaml:norbind`,
		);
		expect(unit).toContain('ExecStartPre="/bin/sh" "-c"');
		expect(unit).toContain(paths.fileBrowserServiceBinary);
		expect(unit).toContain(FILE_BROWSER_VERSION);
		expect(unit).toContain(FILE_BROWSER_COMMIT.slice(0, 7));
		expect(unit.split("\n")).not.toContain(`ReadOnlyPaths=${paths.fileBrowserConfig}`);
		expect(unit.match(/^ExecStart=.*$/m)?.[0]).toBe(
			`ExecStart="${paths.fileBrowserServiceBinary}" "-c" "${dirname(paths.fileBrowserServiceBinary)}/filebrowser.yaml"`,
		);
		expect(unit).toContain(`NoExecPaths=${paths.userHome} ${paths.fileBrowserStateRoot}`);
		expect(unit).toContain("ProtectSystem=strict");
		expect(unit).toContain("PrivatePIDs=true");
		expect(unit).toContain("CapabilityBoundingSet=");
		expect(unit).toContain("TasksMax=128");
		expect(unit).toContain(
			`EnvironmentFile=${join(paths.systemdEnvRoot, `${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service.env`)}`,
		);
		expect(
			readFileSync(
				join(paths.systemdEnvRoot, `${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service.env`),
				"utf8",
			),
		).toContain(`FILEBROWSER_DATABASE="${join(paths.fileBrowserStateRoot, "filebrowser.db")}"`);
		const second = convergeRuntimeManifest(fileBrowserManifestLoad(manifest), paths, options);
		expect(second.installErrors).toEqual([]);
		expect(downloads).toBe(1);
		expect(activations).toBe(2);
		expect(authorityCommits).toBe(2);
		expect(readFileSync(active, "utf8")).toBe(binary);
	});

	test("retains only the desired Files candidate after authority commit", () => {
		const paths = tempRuntimePaths();
		const firstBinary = "first Files candidate\n";
		const secondBinary = "second Files candidate\n";
		const install = (manifest: RuntimeManifest, binary: string) =>
			convergeRuntimeManifest(fileBrowserManifestLoad(manifest), paths, {
				fileBrowserInstallOptions: {
					serviceIsolation: testFileBrowserServiceIsolation,
					download: (_url, destination) => writeFileSync(destination, binary),
					versionProbe: () => `${FILE_BROWSER_VERSION} ${FILE_BROWSER_COMMIT.slice(0, 7)}`,
				},
				fileBrowserReadinessProbe: () => true,
				systemdApply: fileBrowserApplyHooks(),
			});
		expect(
			install(fileBrowserManifest(paths, { generation: 1, binary: firstBinary }), firstBinary)
				.installErrors,
		).toEqual([]);
		const secondManifest = fileBrowserManifest(paths, { generation: 2, binary: secondBinary });
		expect(install(secondManifest, secondBinary).installErrors).toEqual([]);
		const firstTarget = dirname(fileBrowserBinaryPath(paths, firstBinary));
		const desiredTarget = dirname(fileBrowserBinaryPath(paths, secondBinary));
		const orphan = join(paths.fileBrowserInstallRoot, "candidates", "c".repeat(64));
		mkdirSync(orphan, { recursive: true });

		expect(gcFileBrowserCompanionCandidates(secondManifest, paths)).toEqual([orphan]);
		expect(existsSync(firstTarget)).toBe(false);
		expect(existsSync(desiredTarget)).toBe(true);
		expect(existsSync(orphan)).toBe(false);
	});

	test("withdraws only the Files system unit when the companion becomes ineligible", () => {
		const paths = tempRuntimePaths();
		const binary = "Files eligibility fixture\n";
		const manifest = fileBrowserManifest(paths, { generation: 1, binary });
		const installOptions = {
			fileBrowserInstallOptions: {
				serviceIsolation: testFileBrowserServiceIsolation,
				download: (_url: string, destination: string) => writeFileSync(destination, binary),
				versionProbe: () => `${FILE_BROWSER_VERSION} ${FILE_BROWSER_COMMIT.slice(0, 7)}`,
			},
			fileBrowserReadinessProbe: () => true,
			systemdApply: fileBrowserApplyHooks(),
		};
		expect(
			convergeRuntimeManifest(fileBrowserManifestLoad(manifest), paths, installOptions)
				.installErrors,
		).toEqual([]);

		const fileBrowserUnit = join(
			paths.systemdSystemRoot,
			`${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service`,
		);
		const runtimeUnit = join(paths.systemdUserRoot, "openclaw-gateway.service");
		expect(existsSync(fileBrowserUnit)).toBe(true);
		expect(existsSync(runtimeUnit)).toBe(true);
		const withoutFileBrowser: RuntimeManifest = {
			...manifest,
			generation: 2,
			issuedAt: "2026-08-05T00:00:02.000Z",
			companions: {},
		};
		let staleSystemUnits: string[] = [];
		const result = convergeRuntimeManifest(fileBrowserManifestLoad(withoutFileBrowser), paths, {
			systemdApply: {
				activateEgressPrerequisite: successfulPrerequisiteActivation,
				activate: (signal) => {
					staleSystemUnits = signal.staleSystemUnits;
					return { applied: true, systemUnitsChanged: [], userUnitsChanged: [] };
				},
			},
		});

		expect(result.installErrors).toEqual([]);
		expect(staleSystemUnits).toEqual([`${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service`]);
		expect(existsSync(fileBrowserUnit)).toBe(false);
		expect(existsSync(runtimeUnit)).toBe(true);
	});

	// D-14. A CLI that cannot PARSE a companion kind also cannot judge that the kind should be
	// REMOVED: a downgrade is not a deprovision. The test directly above is the deliberate
	// asymmetry and the guard that this exemption is not a blanket — a kind the CLI DOES
	// understand and has decided not to run is still withdrawn.
	describe("declared-but-unrecognised companion kinds are skipped, never withdrawn", () => {
		const UNRECOGNISED = "clipboard";
		const SECOND_UNRECOGNISED = "biblioteca";
		const unrecognisedUnit = `${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}${UNRECOGNISED}.service`;
		// A plain user runtime with no official installer behind it: the point of these tests is
		// the collect set, and an official-service install failure would mask it.
		const testRuntimes: RuntimeManifest["runtimes"] = {
			demo: { enabled: true, run: runSettings(process.execPath, ["--version"]), services: {} },
		};

		function companionsEnvelope(...kinds: string[]): RuntimeManifest["companions"] {
			return Object.fromEntries(
				kinds.map((kind) => [kind, { kind, artifact: { sha256: "c".repeat(64) } }]),
			);
		}

		function declaringManifest(paths: RuntimePaths, kinds: string[]): RuntimeManifest {
			return baseManifest(
				paths,
				testRuntimes,
				kinds.length === 0 ? {} : { companions: companionsEnvelope(...kinds) },
			);
		}

		function plantOwnedFile(path: string, body: string): string {
			mkdirSync(dirname(path), { recursive: true });
			writeFileSync(path, `${GENERATED_RUNTIME_SYSTEMD_FILE_HEADER}\n${body}`);
			return path;
		}

		function convergeCollecting(paths: RuntimePaths, manifest: RuntimeManifest, label: string) {
			let staleSystemUnits: string[] = [];
			let staleUserUnits: string[] = [];
			const result = convergeRuntimeManifest(manifestLoad(manifest, label), paths, {
				cacheLastGood: false,
				systemdApply: {
					activateEgressPrerequisite: successfulPrerequisiteActivation,
					activate: (signal) => {
						staleSystemUnits = [...signal.staleSystemUnits];
						staleUserUnits = [...signal.staleUserUnits];
						return { applied: true, systemUnitsChanged: [], userUnitsChanged: [] };
					},
				},
			});
			return { result, staleSystemUnits, staleUserUnits };
		}

		test("leaves the system-scope unit in neither the collected nor the withdrawn set", () => {
			const paths = tempRuntimePaths();
			const unit = plantOwnedFile(
				join(paths.systemdSystemRoot, unrecognisedUnit),
				"[Unit]\nDescription=unrecognised companion\n",
			);

			const { result, staleSystemUnits } = convergeCollecting(
				paths,
				declaringManifest(paths, [UNRECOGNISED]),
				"unrecognised-system-scope",
			);

			expect(result.installErrors).toEqual([]);
			expect(staleSystemUnits).not.toContain(unrecognisedUnit);
			expect(existsSync(unit)).toBe(true);
		});

		test("leaves the user-scope unit the ownership-prefix rule would otherwise reclaim", () => {
			const paths = tempRuntimePaths();
			const unit = plantOwnedFile(
				join(paths.systemdUserRoot, unrecognisedUnit),
				"[Unit]\nDescription=unrecognised companion\n",
			);
			const environment = plantOwnedFile(
				join(paths.systemdEnvRoot, `${unrecognisedUnit}.env`),
				"COMPANION=1\n",
			);

			const { result, staleUserUnits } = convergeCollecting(
				paths,
				declaringManifest(paths, [UNRECOGNISED]),
				"unrecognised-user-scope",
			);

			expect(result.installErrors).toEqual([]);
			expect(staleUserUnits).not.toContain(unrecognisedUnit);
			expect(existsSync(unit)).toBe(true);
			// Deleting the unit's EnvironmentFile while leaving the unit running breaks it on the
			// next restart, which is the availability outage D-14 exists to prevent.
			expect(existsSync(environment)).toBe(true);
		});

		test("collects the unit once the manifest no longer declares the kind", () => {
			const paths = tempRuntimePaths();
			const unit = plantOwnedFile(
				join(paths.systemdUserRoot, unrecognisedUnit),
				"[Unit]\nDescription=unrecognised companion\n",
			);

			const { result, staleUserUnits } = convergeCollecting(
				paths,
				declaringManifest(paths, []),
				"unrecognised-undeclared",
			);

			expect(result.installErrors).toEqual([]);
			expect(staleUserUnits).toContain(unrecognisedUnit);
			expect(existsSync(unit)).toBe(false);
		});

		test("reports the unrecognised kinds, sorted, so the skip and the observation agree", () => {
			const paths = tempRuntimePaths();
			const { result } = convergeCollecting(
				paths,
				declaringManifest(paths, [UNRECOGNISED, SECOND_UNRECOGNISED]),
				"unrecognised-report",
			);

			expect(result.installErrors).toEqual([]);
			expect(result.unsupportedCompanionKinds).toEqual([SECOND_UNRECOGNISED, UNRECOGNISED]);
		});

		test("reports nothing when every declared companion kind is recognised", () => {
			const paths = tempRuntimePaths();
			const { result } = convergeCollecting(
				paths,
				declaringManifest(paths, []),
				"unrecognised-none",
			);

			expect(result.unsupportedCompanionKinds).toEqual([]);
		});

		test("reconciles the rest of the manifest identically with and without an unrecognised key", () => {
			const withKey = tempRuntimePaths();
			const withoutKey = tempRuntimePaths();
			const shape = (paths: RuntimePaths, manifest: RuntimeManifest, label: string) => {
				const { result, staleSystemUnits, staleUserUnits } = convergeCollecting(
					paths,
					manifest,
					label,
				);
				return {
					installErrors: result.installErrors,
					enabledRuntimes: result.enabledRuntimes,
					systemUnits: result.outputs.systemdSystemUnits.map((path) => path.split("/").pop()),
					userUnits: result.outputs.systemdUserUnits.map((path) => path.split("/").pop()),
					// The egress decision: bundle, engine and transparent env are the three
					// observable halves of "did this converge turn the sidecar on".
					egressProfileBundle: result.outputs.egressProfileBundle === null,
					egressEngine: result.outputs.egressEngine,
					egressTransparentEnv: result.outputs.egressTransparentEnv === null,
					staleSystemUnits,
					staleUserUnits,
				};
			};

			expect(
				shape(withKey, declaringManifest(withKey, [UNRECOGNISED]), "scoped-degradation-with"),
			).toEqual(shape(withoutKey, declaringManifest(withoutKey, []), "scoped-degradation-without"));
		});
	});

	test("cleans interrupted Files staging directories without following symlinks", () => {
		const paths = tempRuntimePaths();
		const binary = "Files staging fixture\n";
		const manifest = fileBrowserManifest(paths, { generation: 1, binary });
		const options = {
			fileBrowserInstallOptions: {
				serviceIsolation: testFileBrowserServiceIsolation,
				download: (_url: string, destination: string) => writeFileSync(destination, binary),
				versionProbe: () => `${FILE_BROWSER_VERSION} ${FILE_BROWSER_COMMIT.slice(0, 7)}`,
			},
			fileBrowserReadinessProbe: () => true,
			systemdApply: fileBrowserApplyHooks(),
		};
		expect(
			convergeRuntimeManifest(fileBrowserManifestLoad(manifest), paths, options).installErrors,
		).toEqual([]);

		const candidates = join(paths.fileBrowserInstallRoot, "candidates");
		const interrupted = join(candidates, ".staging-interrupted");
		mkdirSync(interrupted);
		expect(
			convergeRuntimeManifest(fileBrowserManifestLoad(manifest), paths, options).installErrors,
		).toEqual([]);
		expect(existsSync(interrupted)).toBe(false);

		const outside = join(dirname(paths.fileBrowserInstallRoot), "outside-staging-target");
		mkdirSync(outside);
		const unsafe = join(candidates, ".staging-unsafe");
		symlinkSync(outside, unsafe);
		const refused = convergeRuntimeManifest(fileBrowserManifestLoad(manifest), paths, options);
		expect(refused.installErrors.join("\n")).toContain(
			"Files companion staging entry is not a trusted directory",
		);
		expect(existsSync(outside)).toBe(true);
	});

	test('treats a user runtime named "files" as a normal runtime program', () => {
		const paths = tempRuntimePaths();
		const manifest = baseManifest(paths, {
			files: {
				enabled: true,
				run: runSettings(process.execPath, ["--version"]),
				services: {},
			},
		});
		const result = convergeRuntimeManifest(manifestLoad(manifest, "runtime-named-files"), paths);

		expect(result.installErrors).toEqual([]);
		expect(result.outputs.systemdSystemUnits).not.toContain(
			join(paths.systemdSystemRoot, `${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service`),
		);
		expect(result.outputs.systemdUserUnits).toContain(
			join(paths.systemdUserRoot, `${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service`),
		);
	});

	test("enforces pinned and internally bound Files contracts", () => {
		const paths = tempRuntimePaths();
		const binary = "Files gate fixture\n";
		const manifest = fileBrowserManifest(paths, { generation: 1, binary });
		expect(() => convergeRuntimeManifest(fileBrowserManifestLoad(manifest), paths)).toThrow(
			"Files companion requires systemd apply and readiness hooks",
		);
		expect(existsSync(paths.fileBrowserInstallRoot)).toBe(false);
		expect(
			existsSync(
				join(paths.systemdSystemRoot, `${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service`),
			),
		).toBe(false);

		const pinned = fileBrowserCompanion();
		expect(fileBrowserCompanionSchema.safeParse(pinned).success).toBe(true);
		expect(manifest.deploymentId).not.toBe("hdep_files_reconcile");
		for (const [field, value] of [
			["audience", "clawdi-files:hdep_other"],
			["subject", "deployment:hdep_other:owner"],
			["requiredGroup", `clawdi-files:hdep_files_reconcile:${"b".repeat(64)}`],
		] as const) {
			expect(
				fileBrowserCompanionSchema.safeParse({
					...pinned,
					auth: { ...pinned.auth, [field]: value },
				}).success,
			).toBe(false);
		}
		expect(
			hostedRuntimeBundleV2ManifestSchema.safeParse({
				...manifest,
				companions: { files: pinned },
			}).success,
		).toBe(false);
		expect(fileBrowserCompanionSchema.safeParse({ ...pinned, kind: "filebrowser" }).success).toBe(
			false,
		);
		expect(fileBrowserCompanionSchema.safeParse({ ...pinned, port: 9000 }).success).toBe(false);
		const nextRelease = {
			...pinned,
			version: "v1.6.0-stable",
			commit: "b".repeat(40),
			assets: {
				amd64: {
					url: "https://github.com/gtsteffaniak/filebrowser/releases/download/v1.6.0-stable/linux-amd64-filebrowser",
					sha256: "c".repeat(64),
				},
				arm64: {
					url: "https://github.com/gtsteffaniak/filebrowser/releases/download/v1.6.0-stable/linux-arm64-filebrowser",
					sha256: "d".repeat(64),
				},
			},
		};
		expect(fileBrowserCompanionSchema.safeParse(nextRelease).success).toBe(true);
		expect(
			fileBrowserCompanionSchema.safeParse({
				...pinned,
				listen: "127.0.0.1",
			}).success,
		).toBe(false);
		expect(
			fileBrowserCompanionSchema.safeParse({
				...pinned,
				assets: {
					...pinned.assets,
					amd64: {
						...pinned.assets.amd64,
						url: "https://github.com/gtsteffaniak/filebrowser/releases/download/v1.6.0-stable/linux-amd64-filebrowser",
					},
				},
			}).success,
		).toBe(false);
	});

	test("rejects direct convergence without an explicit apply context", () => {
		const paths = tempRuntimePaths();
		const load = manifestLoad(
			baseManifest(paths, {
				openclaw: { enabled: false, run: runSettings("openclaw", []), services: {} },
			}),
			"inline-missing-apply-context",
		);
		expect(() => convergeRuntimeManifest({ ...load, applyContext: undefined }, paths)).toThrow(
			"runtime manifest convergence requires an explicit apply context",
		);
	});
});

// ---------------------------------------------------------------------------
// 04-06 Task 1 — per-kind companion service identity.
//
// These exercise the PURE halves of the identity module: name derivation, uid resolution from the
// registry, the five account assertions and the post-mutation supplementary-group re-assertion.
// The impure half (`groupadd`/`useradd`/`getent`) is deliberately NOT driven here — it is root-only
// and `ensureExclusiveCompanionServiceIdentity` takes an injectable allocator for exactly that reason, the
// same seam `FileBrowserCompanionInstallOptions.serviceIsolation` provides for the install path.
// ---------------------------------------------------------------------------
describe("companion service identity is per kind", () => {
	// One BOUNDED source slice helper, shared by the source-read guards at the end of this describe.
	//
	// Bounded at BOTH ends. An unbounded `source.slice(declaration)` runs to end-of-file, which makes
	// a `toContain` satisfiable by an occurrence anywhere below the anchor — including inside a
	// different function, which is exactly the refactor the anti-orphan guard exists to catch.
	const isolationSource = readFileSync(join(import.meta.dir, "file-browser-isolation.ts"), "utf8");
	const bodyOf = (signature: string): string => {
		const start = isolationSource.indexOf(signature);
		// Asserted FOUND before the slice. A guard that slices from `indexOf`'s -1 asserts over the
		// whole file, or over nothing, and passes vacuously. This phase already paid for that bug
		// once — 04-06's rename decapitated exactly such a guard in
		// `tests/hosted-identity-move.test.ts` and it sat red for four commits with its real
		// assertions unreachable.
		expect(start, `anchor not found in file-browser-isolation.ts: ${signature}`).toBeGreaterThan(0);
		// The next TOP-LEVEL declaration, exported or not: this module's private helpers
		// (`lookupUser`, `lookupGroup`, `commandOutput`) sit between the anchors the guards below
		// use, so bounding on `\nexport ` alone would still swallow them. `-1` means the
		// anchor is the last declaration, and only then does the slice legitimately run to EOF.
		const next = isolationSource
			.slice(start + signature.length)
			.search(/\n(?:export |function |const |type |interface |class )/);
		return next === -1
			? isolationSource.slice(start)
			: isolationSource.slice(start, start + signature.length + next);
	};

	const runtimeAccount = { runtimeUid: 10001, runtimeGid: 10001 };
	const healthyAccount = {
		uid: 10100,
		gid: 10100,
		home: "/nonexistent",
		shell: "/usr/sbin/nologin",
	};

	const syntheticRegistry = (
		rows: Record<string, "preallocated" | number>,
	): Record<string, CompanionRegistryEntry> =>
		Object.fromEntries(
			Object.entries(rows).map(([key, uid], index) => [
				key,
				{
					artifactKind: "binary-sha256" as const,
					minCliVersion: "0.0.0",
					uid,
					port: 9200 + index,
					egress: "bypass" as const,
				},
			]),
		);

	test("derives the shipped file-browser names from the registry key through one helper", () => {
		const names = companionServiceNames("filebrowser");
		// The SHIPPED values. `filebrowser` carries a name exemption for the same reason it carries
		// the `preallocated` uid exemption: the account and the state directory already exist under
		// the `files` stem on every provisioned host and own files on disk. Since #939 (sync step 1)
		// Files runs as the tenant runtime user and no longer creates this account; the stem still
		// names its `StateDirectory=`.
		expect(names.user).toBe(`${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}files`);
		expect(names.group).toBe(`${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}files`);
		expect(names.stateDirectory).toBe(SYSTEMD_FILE_BROWSER_STATE_DIRECTORY);
		expect(names.credentialId).toBe("filebrowser.yaml");
		expect(COMPANION_SERVICE_ACCOUNT_EXEMPTIONS.filebrowser).toBeDefined();
	});

	test("derives an unexempted kind's names straight from its key, under the owned-name prefix", () => {
		const names = companionServiceNames("clipboard");
		expect(names.user).toBe(`${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}clipboard`);
		expect(names.group).toBe(`${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}clipboard`);
		expect(names.stateDirectory).toBe(`${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}clipboard`);
		expect(names.credentialId).toBe("clipboard.yaml");
	});

	test("refuses to derive names for a key outside the registry key pattern", () => {
		expect(() => companionServiceNames("../etc")).toThrow(/companion key pattern/);
	});

	test("resolves a preallocated kind to no explicit uid — the documented exemption", () => {
		expect(resolveCompanionServiceUid("filebrowser")).toBeNull();
	});

	test("resolves a decimal uid inside the reserved block to that uid", () => {
		const registry = syntheticRegistry({ clipboard: RESERVED_COMPANION_UID_BLOCK.first });
		expect(resolveCompanionServiceUid("clipboard", registry)).toBe(
			RESERVED_COMPANION_UID_BLOCK.first,
		);
	});

	test("throws for a decimal uid outside the reserved block, naming the kind and the block", () => {
		const registry = syntheticRegistry({ clipboard: RESERVED_COMPANION_UID_BLOCK.last + 1 });
		expect(() => resolveCompanionServiceUid("clipboard", registry)).toThrow(/clipboard/);
		expect(() => resolveCompanionServiceUid("clipboard", registry)).toThrow(
			new RegExp(`${RESERVED_COMPANION_UID_BLOCK.first}-${RESERVED_COMPANION_UID_BLOCK.last}`),
		);
	});

	// A companion key the manifest parser ACCEPTS and `Object.prototype` also answers to. Exactly one
	// inherited member matches COMPANION_KEY_PATTERN, and this is it; a bare `registry[kind]` index
	// read resolved the inherited `Object` function for it, so the "no row in the registry" refusal
	// did not fire and `resolveCompanionServiceUid` returned `undefined` rather than throwing — in
	// violation of its own `number | null` signature, which made the registry-pinned-UID assertion
	// in `assertCompanionServiceAccount` (`expectedUid != null`) silently no-op on a host where the
	// account already existed. Kept as a named constant so the reason survives a rename.
	const PROTOTYPE_KEY = "constructor";

	test("refuses a prototype-inherited key outright rather than resolving it to an absent uid", () => {
		expect(COMPANION_KEY_PATTERN.test(PROTOTYPE_KEY)).toBe(true);
		expect(() => resolveCompanionServiceUid(PROTOTYPE_KEY)).toThrow(
			/no row in the companion registry/,
		);
		// And against a CALLER-SUPPLIED registry too: the parameter's default has a stripped
		// prototype, which says nothing about the object a caller actually hands in.
		expect(() =>
			resolveCompanionServiceUid(PROTOTYPE_KEY, syntheticRegistry({ clipboard: 10100 })),
		).toThrow(/no row in the companion registry/);
	});

	test("resolves a real row to a uid, so the own-property guard did not refuse everything", () => {
		// The complement of the assertion above. A guard that refused every key would make that test
		// pass while breaking every converge.
		expect(
			resolveCompanionServiceUid(PROTOTYPE_KEY, syntheticRegistry({ constructor: 10100 })),
		).toBe(10100);
	});

	test("never returns a non-null sentinel that is not a number — the argv gate is total", () => {
		// `groupadd`/`useradd` gate `--gid`/`--uid` on this sentinel. `undefined` passing a
		// `=== null` test is how a lost pin became `--gid undefined` in privileged argv.
		for (const kind of KNOWN_COMPANION_KINDS) {
			const resolved = resolveCompanionServiceUid(kind);
			expect(resolved === null || typeof resolved === "number").toBe(true);
		}
	});

	test("derives names for a prototype-inherited key from the key, never from an inherited member", () => {
		// The same shape one function earlier: `COMPANION_SERVICE_ACCOUNT_EXEMPTIONS[kind]` read an
		// inherited member for this key. Benign only because the `Object` function has no `nameStem`.
		const names = companionServiceNames(PROTOTYPE_KEY);
		expect(names.user).toBe(`${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}${PROTOTYPE_KEY}`);
		expect(names.stateDirectory).toBe(`${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}${PROTOTYPE_KEY}`);
		expect(names.credentialId).toBe(`${PROTOTYPE_KEY}.yaml`);
	});

	test("throws when another registry row already claims the uid — D-13 never-reclaim", () => {
		const registry = syntheticRegistry({ clipboard: 10100, notepad: 10100 });
		expect(() => resolveCompanionServiceUid("notepad", registry)).toThrow(/clipboard/);
	});

	test("keeps two kinds whose uids are numerically adjacent separated in every derived name", () => {
		const registry = syntheticRegistry({ clipboard: 10100, notepad: 10101 });
		expect(resolveCompanionServiceUid("clipboard", registry)).toBe(10100);
		expect(resolveCompanionServiceUid("notepad", registry)).toBe(10101);
		const left = companionServiceNames("clipboard");
		const right = companionServiceNames("notepad");
		expect(left.user).not.toBe(right.user);
		expect(left.group).not.toBe(right.group);
		expect(left.stateDirectory).not.toBe(right.stateDirectory);
		expect(left.credentialId).not.toBe(right.credentialId);
	});

	test("keeps two kinds whose keys share a prefix separated in every derived name", () => {
		const short = companionServiceNames("note");
		const long = companionServiceNames("notepad");
		expect(short.stateDirectory).not.toBe(long.stateDirectory);
		expect(short.credentialId).not.toBe(long.credentialId);
	});

	test("throws when the resolved uid is 0", () => {
		expect(() =>
			assertCompanionServiceAccount({
				names: companionServiceNames("filebrowser"),
				account: { ...healthyAccount, uid: 0 },
				gid: healthyAccount.gid,
				...runtimeAccount,
			}),
		).toThrow(/root/i);
	});

	test("throws when the resolved uid equals the tenant runtime uid", () => {
		expect(() =>
			assertCompanionServiceAccount({
				names: companionServiceNames("filebrowser"),
				account: { ...healthyAccount, uid: runtimeAccount.runtimeUid },
				gid: healthyAccount.gid,
				...runtimeAccount,
			}),
		).toThrow(/tenant runtime/);
	});

	test("throws when the resolved gid does not match the kind's group", () => {
		expect(() =>
			assertCompanionServiceAccount({
				names: companionServiceNames("filebrowser"),
				account: { ...healthyAccount, gid: 10199 },
				gid: healthyAccount.gid,
				...runtimeAccount,
			}),
		).toThrow(/group/);
	});

	test("throws when the resolved gid equals the tenant runtime gid", () => {
		expect(() =>
			assertCompanionServiceAccount({
				names: companionServiceNames("filebrowser"),
				account: { ...healthyAccount, gid: runtimeAccount.runtimeGid },
				gid: runtimeAccount.runtimeGid,
				...runtimeAccount,
			}),
		).toThrow(/tenant runtime/);
	});

	test("throws when the account has a home directory or a login shell", () => {
		const names = companionServiceNames("filebrowser");
		expect(() =>
			assertCompanionServiceAccount({
				names,
				account: { ...healthyAccount, home: "/home/iforge-files" },
				gid: healthyAccount.gid,
				...runtimeAccount,
			}),
		).toThrow(/non-login/);
		expect(() =>
			assertCompanionServiceAccount({
				names,
				account: { ...healthyAccount, shell: "/bin/bash" },
				gid: healthyAccount.gid,
				...runtimeAccount,
			}),
		).toThrow(/non-login/);
	});

	test("throws when the host allocated a uid other than the one the registry pinned", () => {
		expect(() =>
			assertCompanionServiceAccount({
				names: companionServiceNames("filebrowser"),
				account: healthyAccount,
				gid: healthyAccount.gid,
				expectedUid: 10101,
				...runtimeAccount,
			}),
		).toThrow(/10101/);
	});

	test("post-mutation re-assertion fires when the service user retains a non-primary gid", () => {
		expect(() =>
			assertCompanionServiceGroups({
				names: companionServiceNames("filebrowser"),
				runtimeGids: [10001],
				serviceGids: [10100, 27],
				gid: 10100,
			}),
		).toThrow(/supplementary group/);
	});

	test("post-mutation re-assertion fires when the runtime user still carries the service gid", () => {
		expect(() =>
			assertCompanionServiceGroups({
				names: companionServiceNames("filebrowser"),
				runtimeGids: [10001, 10100],
				serviceGids: [10100],
				gid: 10100,
			}),
		).toThrow(/supplementary group/);
	});

	test("post-mutation re-assertion passes for a correctly stripped pair", () => {
		expect(() =>
			assertCompanionServiceGroups({
				names: companionServiceNames("filebrowser"),
				runtimeGids: [10001],
				serviceGids: [10100],
				gid: 10100,
			}),
		).not.toThrow();
	});

	test("two kinds derive distinct accounts, state directories and credential ids", () => {
		// T-4-06: the isolation boundary is that two kinds cannot land on one account, one state
		// directory or one credential id. Asserted through the single derivation helper; the runtime
		// half (two kinds that RESOLVED to one uid or gid on a host) is the cross-kind refusal below.
		const left = companionServiceNames("clipboard");
		const right = companionServiceNames("notepad");
		expect(left.user).not.toBe(right.user);
		expect(left.stateDirectory).not.toBe(right.stateDirectory);
		expect(left.credentialId).not.toBe(right.credentialId);
	});

	test("Files allocates no per-kind account: its identity is the tenant runtime user (#939)", () => {
		// Upstream #939 retired the Files service account, and with it the workspace ACL phase 06-04
		// wrote for that account (removed at sync step 1). The entry point resolves the tenant
		// runtime user's own uid and gid and reaches none of the per-kind allocators.
		const entryBody = bodyOf("export function ensureFileBrowserServiceIsolation(");
		expect(entryBody).toContain("uid: runtimeUserUid(runtimeUser)");
		expect(entryBody).toContain("gid: runtimeUserGid(runtimeUser)");
		for (const allocator of [
			"companionServiceNames(",
			"ensureCompanionServiceIdentit",
			"ensureExclusiveCompanionServiceIdentity(",
		]) {
			expect(entryBody).not.toContain(allocator);
		}
	});
});

// ---------------------------------------------------------------------------
// 06.1-04 — the cross-kind refusal on the production allocation path (COMP-05, post-#939).
//
// #939 took Files off the per-kind accounts, which left the multi-kind allocator with no production
// caller: container companions allocate one kind per call through `ContainerHost.ensureIdentity`,
// so the shared-uid/gid refusal ran only in this suite again (the 04-VERIFICATION COMP-05 defect).
// The single-kind path now checks the identity it allocated against every OTHER registry kind's
// account already on the host. `ensure` and `observe` are injected because the real ones shell out
// to root-only `useradd` and to `getent`.
// ---------------------------------------------------------------------------
describe("the cross-kind companion identity refusal runs on the production allocation path", () => {
	const KINDS = ["clipboard", "notepad", "postiz"] as const;
	const allocate = (
		observed: Record<string, { uid: number | null; gids: number[] } | null>,
		identity = { uid: 10101, gid: 10101 },
	) =>
		ensureExclusiveCompanionServiceIdentity({
			kind: "postiz",
			runtimeUser: "iforge",
			registryKinds: KINDS,
			ensure: () => identity,
			observe: (kind) => observed[kind] ?? null,
		});

	test("refuses an identity whose uid another registry kind's account already holds", () => {
		expect(() => allocate({ notepad: { uid: 10101, gids: [10150] } })).toThrow(
			/Companion postiz resolved to uid 10101, already held by companion notepad/,
		);
	});

	test("refuses an identity whose gid another registry kind's group already holds", () => {
		// Two group names at one gid is an `/etc/group` that disagrees with the registry; the
		// per-account assertions only ever compare an account with its OWN group.
		expect(() => allocate({ clipboard: { uid: null, gids: [10101] } })).toThrow(
			/Companion postiz resolved to gid 10101, already held by companion clipboard/,
		);
	});

	test("names the first holder in registry-key order when several kinds collide", () => {
		expect(() =>
			allocate({
				notepad: { uid: 10101, gids: [10101] },
				clipboard: { uid: 10101, gids: [10101] },
			}),
		).toThrow(/already held by companion clipboard/);
	});

	test("does not mistake a gid equal to another kind's uid for a collision", () => {
		// uids and gids are separate namespaces; 10101 in each is the sysusers convention.
		expect(
			allocate({ notepad: { uid: 10101, gids: [10150] } }, { uid: 10150, gid: 10101 }),
		).toEqual({ uid: 10150, gid: 10101 });
		expect(() =>
			allocate({ notepad: { uid: 10101, gids: [10150] } }, { uid: 10102, gid: 10101 }),
		).not.toThrow();
	});

	test("never compares the allocated kind with its own account", () => {
		expect(allocate({ postiz: { uid: 10101, gids: [10101] } })).toEqual({
			uid: 10101,
			gid: 10101,
		});
	});

	test("allocates exactly once and observes every other registry kind", () => {
		const ensured: string[] = [];
		const observedKinds: string[] = [];
		const identity = ensureExclusiveCompanionServiceIdentity({
			kind: "postiz",
			runtimeUser: "iforge",
			registryKinds: ["postiz", "notepad", "clipboard", "notepad"],
			ensure: (kind) => {
				ensured.push(kind);
				return { uid: 10101, gid: 10101 };
			},
			observe: (kind) => {
				observedKinds.push(kind);
				return null;
			},
		});
		expect(identity).toEqual({ uid: 10101, gid: 10101 });
		expect(ensured).toEqual(["postiz"]);
		expect(observedKinds).toEqual(["clipboard", "notepad"]);
	});

	test("the container host's ensureIdentity routes through the cross-kind refusal", () => {
		// THE ANTI-ORPHAN GUARD for the post-#939 production path. Anchors are asserted FOUND before
		// they are sliced from, so a rename fails loudly rather than asserting over nothing.
		const containerSource = readFileSync(join(import.meta.dir, "container-companion.ts"), "utf8");
		const start = containerSource.indexOf("ensureIdentity(kind) {");
		expect(start).toBeGreaterThan(0);
		const body = containerSource.slice(start, containerSource.indexOf("now() {", start));
		expect(body).toContain("ensureExclusiveCompanionServiceIdentity({");
		expect(body).toContain("ensure: ensureCompanionServiceIdentity,");
		expect(body).toContain("observe: observeCompanionServiceIdentity,");
		expect(body).not.toContain("return ensureCompanionServiceIdentity(kind, runtimeUser);");
	});
});

// ---------------------------------------------------------------------------
// 04-06 Task 2 — the shared companion sandbox baseline.
//
// The pre-change render of the ONLY companion, captured verbatim from this suite before the
// extraction and templated only where a temp path varies per run. Full-text equality against it is
// the evidence that the baseline was EXTRACTED rather than rebuilt; spot-checking directives would
// pass for a block that had silently lost one.
//
// Re-captured at sync step 1 for upstream #939/#915: `User=`/`Group=` are the tenant runtime
// identity (templated, because the suite runs as whatever uid the runner has), the config arrives
// by a second `BindReadOnlyPaths=` instead of `LoadCredential=`, and `PrivatePIDs=true` joined the
// sandbox. Every other line is the pre-change capture unchanged.
//
// Sync step 3: upstream (#1180-#1194) no longer stamps the environment-revision comment into
// generated units, so that one line left the capture.
// ---------------------------------------------------------------------------
const PRE_CHANGE_FILE_BROWSER_UNIT = [
	"# Generated by iforge runtime init. Do not edit inside hosted runtime.",
	"[Unit]",
	"Description=iforge hosted Files companion",
	"After=network-online.target",
	"Wants=network-online.target",
	"",
	"[Service]",
	"Type=simple",
	"WorkingDirectory=/home/iforge",
	"StateDirectory=iforge-files",
	"StateDirectoryMode=0700",
	"RuntimeDirectory=iforge-files",
	"RuntimeDirectoryMode=0700",
	"User={{RUNTIME_USER}}",
	"Group={{RUNTIME_GID}}",
	"BindReadOnlyPaths={{ACTIVE_BINARY}}:{{FB_SERVICE_BINARY}}:norbind",
	"BindReadOnlyPaths={{FB_CONFIG}}:{{FB_SERVICE_BINARY}}.yaml:norbind",
	'ExecStartPre="/bin/sh" "-c" "output=$(\\"$1\\" version 2>&1) || exit $?; case \\"$output\\" in *\\"$2\\"*) ;; *) exit 65 ;; esac; case \\"$output\\" in *\\"$3\\"*) ;; *) exit 65 ;; esac" "sh" "{{FB_SERVICE_BINARY}}" "v1.5.0-stable" "79552f8"',
	"UMask=0077",
	"NoNewPrivileges=true",
	"PrivateTmp=true",
	"PrivateDevices=true",
	"ProtectSystem=strict",
	"ProtectHome=tmpfs",
	"BindPaths={{USER_HOME}}",
	"ReadWritePaths={{USER_HOME}}",
	"NoExecPaths={{USER_HOME}} {{FB_STATE_ROOT}}",
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
	"EnvironmentFile={{ENV_ROOT}}/iforge-files.service.env",
	// The config path is the service binary's sibling `filebrowser.yaml`, which the binary
	// placeholder's replacement renders as `{{FB_SERVICE_BINARY}}.yaml`.
	'ExecStart="{{FB_SERVICE_BINARY}}" "-c" "{{FB_SERVICE_BINARY}}.yaml"',
	"Restart=always",
	"RestartSec=2",
	"KillMode=mixed",
	"TimeoutStopSec=30",
	"",
	"[Install]",
	"WantedBy=multi-user.target",
	"",
].join("\n");

describe("the companion sandbox baseline is shared and per-kind", () => {
	const renderFileBrowserUnit = (): { unit: string; templated: string } => {
		const paths = tempRuntimePaths();
		const binary = "#!/bin/sh\nprintf 'test File Browser'\n";
		const manifest = fileBrowserManifest(paths, { generation: 1, binary });
		const result = convergeRuntimeManifest(fileBrowserManifestLoad(manifest), paths, {
			fileBrowserInstallOptions: {
				serviceIsolation: testFileBrowserServiceIsolation,
				download: (_url: string, destination: string) => writeFileSync(destination, binary),
				versionProbe: () => `${FILE_BROWSER_VERSION} ${FILE_BROWSER_COMMIT.slice(0, 7)}`,
			},
			fileBrowserReadinessProbe: (url: string) => url === "http://127.0.0.1:9120/health",
			systemdApply: fileBrowserApplyHooks(),
			commitAuthority: () => {},
		});
		expect(result.installErrors).toEqual([]);
		const unit = readFileSync(
			join(paths.systemdSystemRoot, `${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service`),
			"utf8",
		);
		const templated = unit
			.replaceAll(fileBrowserBinaryPath(paths, binary), "{{ACTIVE_BINARY}}")
			.replaceAll(paths.fileBrowserServiceBinary, "{{FB_SERVICE_BINARY}}")
			.replaceAll(paths.fileBrowserConfig, "{{FB_CONFIG}}")
			.replaceAll(paths.fileBrowserStateRoot, "{{FB_STATE_ROOT}}")
			.replaceAll(paths.systemdEnvRoot, "{{ENV_ROOT}}")
			.replaceAll(paths.userHome, "{{USER_HOME}}")
			.replace(/^User=.*$/m, "User={{RUNTIME_USER}}")
			.replace(/^Group=.*$/m, "Group={{RUNTIME_GID}}");
		return { unit, templated };
	};

	test("renders the filebrowser unit byte-identically to the pre-extraction capture", () => {
		expect(renderFileBrowserUnit().templated).toBe(PRE_CHANGE_FILE_BROWSER_UNIT);
	});

	test("renders identically twice, so two converges agree", () => {
		expect(renderFileBrowserUnit().templated).toBe(renderFileBrowserUnit().templated);
	});

	test("carries every baseline directive in the rendered unit", () => {
		const { unit } = renderFileBrowserUnit();
		for (const directive of [
			"ProtectHome=tmpfs",
			"NoNewPrivileges=true",
			"PrivateTmp=true",
			"PrivateDevices=true",
			"ProtectSystem=strict",
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
			"UMask=0077",
			"User=",
			"Group=",
		]) {
			expect(unit).toContain(directive);
		}
	});

	test("places no credential material inline in the unit file", () => {
		// `systemd.exec(5)`: do not use the inline form for anything secret, it is accessible to
		// unprivileged processes via IPC. Stronger than D-11's stated `systemctl cat` reason.
		expect(renderFileBrowserUnit().unit).not.toContain("SetCredential=");
	});

	test("holds every baseline directive in COMPANION_SANDBOX_BASELINE, none of them optional", () => {
		const all = [
			...COMPANION_SANDBOX_BASELINE.beforeAllowList,
			...COMPANION_SANDBOX_BASELINE.afterAllowList,
		];
		expect(all).toEqual([
			"UMask=0077",
			"NoNewPrivileges=true",
			"PrivateTmp=true",
			"PrivateDevices=true",
			"ProtectSystem=strict",
			"ProtectHome=tmpfs",
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
		]);
	});

	test("renders a kind with no allow-list as a read-only empty home, not a weakened baseline", () => {
		// A SYNTHETIC KIND, at the builder — the phase ships no second companion and no second
		// reconciler. An empty allow-list under `ProtectHome=tmpfs` is a read-only empty home, which
		// is at least as strong as the coarser `ProtectHome=yes` that `BindPaths=` cannot re-open.
		const lines = companionSandboxServiceLines({ kind: "clipboard" });
		expect(lines).toContain("ProtectHome=tmpfs");
		expect(lines.some((line) => line.startsWith("BindPaths="))).toBe(false);
		expect(lines.some((line) => line.startsWith("ReadWritePaths="))).toBe(false);
		expect(lines.some((line) => line.startsWith("NoExecPaths="))).toBe(false);
		// Every baseline directive is still present — the empty allow-list removes paths, never
		// protection.
		for (const directive of COMPANION_SANDBOX_BASELINE.beforeAllowList) {
			expect(lines).toContain(directive);
		}
		for (const directive of COMPANION_SANDBOX_BASELINE.afterAllowList) {
			expect(lines).toContain(directive);
		}
	});

	test("takes User= and Group= from the per-kind identity, never a literal", () => {
		expect(companionSandboxServiceLines({ kind: "clipboard" }).slice(0, 2)).toEqual([
			`User=${companionServiceNames("clipboard").user}`,
			`Group=${companionServiceNames("clipboard").group}`,
		]);
	});

	test("runs the Files unit as the tenant runtime identity, not a per-kind account (#939)", () => {
		// The values the templated capture above hides. `beforeEach` sets CLAWDI_RUNTIME_USER to the
		// suite's own uid, the stand-in for the hosted `iforge` account.
		const lines = renderFileBrowserUnit().unit.split("\n");
		expect(lines).toContain(`User=${TEST_RUNTIME_USER}`);
		expect(lines).toContain(`Group=${process.getegid?.() ?? 0}`);
		expect(lines).not.toContain(`User=${companionServiceNames("filebrowser").user}`);
	});

	test("expresses the per-kind variation as a path list with no opt-out switch", () => {
		const source = readFileSync(join(import.meta.dir, "runtime-systemd-reconciliation.ts"), "utf8");
		// The shape rule from 04-RESEARCH.md Pitfall 3: a list of paths, not a boolean. A baseline any
		// kind can opt out of is a baseline every future kind can cite.
		expect(/allowHome|protectHome/.test(source)).toBe(false);
	});

	test("derives StateDirectory= per kind so two kinds cannot share one", () => {
		expect(companionServiceNames("filebrowser").stateDirectory).toBe(
			SYSTEMD_FILE_BROWSER_STATE_DIRECTORY,
		);
		expect(companionServiceNames("clipboard").stateDirectory).not.toBe(
			companionServiceNames("filebrowser").stateDirectory,
		);
	});
});

// ---------------------------------------------------------------------------
// 04-06 Task 3 — credential delivery, DEFERRED variant.
//
// The user answered this plan's `gate="blocking-human"` decision checkpoint with
// `defer-encryption`: keep the plain per-unit credential directive, ship the seam type and the
// per-kind credential id so the later switch is a one-line change, and carry D-11's at-rest
// encryption increment as an open Phase 6 obligation.
//
// Per-UID credential ISOLATION — what COMP-05 actually names — is already true and is asserted
// here against the directive pair. At-rest ENCRYPTION of the credential file is the separate,
// deferred property. The two are stated separately on purpose.
//
// Since sync step 1 (upstream #915 and #939) the Files unit carries NO credential directive: its
// config is bind-mounted read-only into the unit and readable by the tenant runtime identity it
// runs as. The per-kind directive builder below stays for the next companion that needs one.
// ---------------------------------------------------------------------------
describe("companion credential delivery is per unit, per kind, and not encrypted yet", () => {
	const renderUnit = (existing?: RuntimePaths): { unit: string; paths: RuntimePaths } => {
		const paths = existing ?? tempRuntimePaths();
		const binary = "#!/bin/sh\nprintf 'test File Browser'\n";
		const manifest = fileBrowserManifest(paths, { generation: 1, binary });
		const result = convergeRuntimeManifest(fileBrowserManifestLoad(manifest), paths, {
			fileBrowserInstallOptions: {
				serviceIsolation: testFileBrowserServiceIsolation,
				download: (_url: string, destination: string) => writeFileSync(destination, binary),
				versionProbe: () => `${FILE_BROWSER_VERSION} ${FILE_BROWSER_COMMIT.slice(0, 7)}`,
			},
			fileBrowserReadinessProbe: (url: string) => url === "http://127.0.0.1:9120/health",
			systemdApply: fileBrowserApplyHooks(),
			commitAuthority: () => {},
		});
		expect(result.installErrors).toEqual([]);
		return {
			unit: readFileSync(
				join(paths.systemdSystemRoot, `${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service`),
				"utf8",
			),
			paths,
		};
	};

	test("delivers the Files config read-only by bind mount, with no credential directive", () => {
		const { unit, paths } = renderUnit();
		const lines = unit.split("\n");
		expect(
			lines.filter((line) => line.startsWith(`BindReadOnlyPaths=${paths.fileBrowserConfig}:`)),
		).toHaveLength(1);
		expect(unit).not.toContain("LoadCredential=");
		// The deferred increment, asserted as ABSENT rather than assumed absent.
		expect(unit).not.toContain("LoadCredentialEncrypted=");
	});

	test("keeps the TPM device node closed — the deferred form's cost is not paid", () => {
		const { unit } = renderUnit();
		// The encrypted form would add the TPM device node rw to `DeviceAllow=` under
		// `PrivateDevices=`, re-opening a node this sandbox closes. Deferring means not paying that.
		expect(unit).toContain("PrivateDevices=true");
		expect(unit).not.toContain("DeviceAllow=");
		expect(unit).not.toContain("/dev/tpmrm0");
	});

	test("writes no ciphertext artifact anywhere under the runtime state roots", () => {
		const { paths } = renderUnit();
		const found: string[] = [];
		const walk = (root: string): void => {
			if (!existsSync(root)) return;
			for (const entry of readdirSync(root, { withFileTypes: true })) {
				const path = join(root, entry.name);
				if (entry.isDirectory()) {
					walk(path);
					continue;
				}
				if (/\.(cred|enc|ciphertext)$/.test(entry.name)) found.push(path);
			}
		};
		for (const root of [paths.configurationRoot, paths.serviceStateRoot, paths.runRoot]) {
			walk(root);
		}
		// Deferral means there is no derived host-bound state at all — nothing to regenerate, nothing
		// to snapshot, nothing to strand across an instance replacement.
		expect(found).toEqual([]);
	});

	test("adds no ciphertext target to the activated unit set, because there is no derived state to protect", () => {
		// #1194 retired the pre-apply live snapshot (`runtimeRootLiveMutationTargets`) and its
		// rollback. The flat apply now records the content hash of every managed unit it activates,
		// read through `readSystemdUnitSnapshot`, so that set is where this intent lives: the Files
		// unit enters it under its constant name and no credential artifact enters it at all.
		const { paths } = renderUnit();
		const snapshot = readSystemdUnitSnapshot(paths);
		const activated = [...snapshot.system.keys(), ...snapshot.user.keys()];
		expect(activated).toContain(`${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service`);
		expect(activated.some((target) => /\.(cred|enc|ciphertext)$/.test(target))).toBe(false);
	});

	test("renders an identical config delivery directive on two consecutive converges", () => {
		// The SAME paths converged twice — the point is that a reconverge does not rotate, re-derive
		// or otherwise move the credential id or its source, not that two unrelated temp roots agree.
		const first = renderUnit();
		const second = renderUnit(first.paths);
		const directive = (unit: string): string | undefined =>
			unit
				.split("\n")
				.find((line) => line.startsWith(`BindReadOnlyPaths=${first.paths.fileBrowserConfig}:`));
		expect(directive(first.unit)).toBeDefined();
		expect(directive(second.unit)).toBe(directive(first.unit));
		expect(second.unit).toBe(first.unit);
	});

	test("builds the plain directive for any kind through one site", () => {
		expect(
			companionCredentialDirective({ kind: "clipboard", source: "/etc/iforge/clipboard.yaml" }),
		).toBe("LoadCredential=clipboard.yaml:/etc/iforge/clipboard.yaml");
	});

	test("fails closed when an encryption seam is injected while D-11 is deferred", () => {
		// Failing loudly beats silently ignoring the seam: a caller who wires encryption WITHOUT the
		// A1/A2 host observation would otherwise ship a directive the host may not support, and the
		// service would fail at start on a real host while every local test stayed green. That is the
		// exact failure mode the user deferred to avoid.
		expect(() =>
			companionCredentialDirective({
				kind: "filebrowser",
				source: "/etc/iforge/filebrowser.yaml",
				encrypt: () => "/run/iforge/filebrowser.cred",
			}),
		).toThrow(/D-11/);
	});
});

// ---------------------------------------------------------------------------
// 04-08 Task 1 — the multi-kind isolation boundary, wired through the real converge path.
//
// 04-06 built a multi-kind allocator carrying the shared-uid refusal and left it with ZERO
// production call sites (`04-VERIFICATION.md`, COMP-05 gap); 04-08 wired it through the Files
// converge. #939 took Files off per-kind accounts, and 06.1-04 retired that allocator: the refusal
// now runs where production allocates, in `ContainerHost.ensureIdentity`
// (`ensureExclusiveCompanionServiceIdentity`, asserted above). These tests drive the Files converge.
//
// The kind set is the manifest's declared companion KEYS intersected with the registry (D-13,
// D-14): never the registry alone, because under D-13 companion UIDs are append-only and never
// reclaimed, so a speculatively provisioned account permanently burns a UID.
//
// Since #939 (sync step 1) the Files converge allocates no account at all: its seam takes the
// paths and the source root and returns the tenant runtime identity. What stays true, and is
// asserted here, is that a declared unrecognised key reaches no allocation and is reported as
// unsupported.
// ---------------------------------------------------------------------------
describe("the companion isolation boundary is reachable from the real converge path", () => {
	// Satisfies COMPANION_KEY_PATTERN, sorts last so an ordering assertion reads unambiguously, and
	// is not a plausible future registry key.
	const UNRECOGNISED = "zzz-unrecognised";

	const convergeDeclaring = (
		extraKinds: readonly string[],
	): {
		unit: string;
		templated: string;
		seamCalls: number[];
		unsupportedCompanionKinds: string[];
	} => {
		const paths = tempRuntimePaths();
		const binary = "#!/bin/sh\nprintf 'test File Browser'\n";
		const manifest = fileBrowserManifest(paths, { generation: 1, binary });
		const companions = manifest.companions;
		// Thrown rather than defaulted to `{}`: writing the extra keys into a throwaway object would
		// leave every assertion below passing over a manifest that declared nothing.
		if (!companions) {
			throw new Error("the file-browser fixture must declare a companions envelope");
		}
		for (const kind of extraKinds) {
			// `kind` agrees with its key deliberately — `minimalCompanionSchema` requires that
			// agreement (T-4-03), so this is a well-formed FUTURE kind rather than a typo.
			Reflect.set(companions, kind, { kind, artifact: { sha256: "c".repeat(64) } });
		}
		const seamCalls: number[] = [];
		const result = convergeRuntimeManifest(fileBrowserManifestLoad(manifest), paths, {
			fileBrowserInstallOptions: {
				// Records each call's arity: the #939 seam is (paths, sourceRoot), with no kinds.
				serviceIsolation: (...args: unknown[]) => {
					seamCalls.push(args.length);
					return testFileBrowserServiceIsolation();
				},
				download: (_url: string, destination: string) => writeFileSync(destination, binary),
				versionProbe: () => `${FILE_BROWSER_VERSION} ${FILE_BROWSER_COMMIT.slice(0, 7)}`,
			},
			fileBrowserReadinessProbe: (url: string) => url === "http://127.0.0.1:9120/health",
			systemdApply: fileBrowserApplyHooks(),
			commitAuthority: () => {},
		});
		expect(result.installErrors).toEqual([]);
		const unit = readFileSync(
			join(paths.systemdSystemRoot, `${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service`),
			"utf8",
		);
		return {
			unit,
			templated: unit
				.replaceAll(fileBrowserBinaryPath(paths, binary), "{{ACTIVE_BINARY}}")
				.replaceAll(paths.fileBrowserServiceBinary, "{{FB_SERVICE_BINARY}}")
				.replaceAll(paths.fileBrowserConfig, "{{FB_CONFIG}}")
				.replaceAll(paths.fileBrowserStateRoot, "{{FB_STATE_ROOT}}")
				.replaceAll(paths.systemdEnvRoot, "{{ENV_ROOT}}")
				.replaceAll(paths.userHome, "{{USER_HOME}}")
				.replace(/^User=.*$/m, "User={{RUNTIME_USER}}")
				.replace(/^Group=.*$/m, "Group={{RUNTIME_GID}}"),
			seamCalls,
			unsupportedCompanionKinds: [...result.unsupportedCompanionKinds],
		};
	};

	test("calls the Files seam once, with no companion kinds, while an unrecognised kind is declared", () => {
		const { seamCalls } = convergeDeclaring([UNRECOGNISED]);
		// One invocation carrying the paths and the source root only: nothing reaches an allocator,
		// so the unrecognised key cannot burn a UID (D-13).
		expect(seamCalls).toEqual([2]);
	});

	test("the Files install path allocates no companion account (#939)", () => {
		// Before #939 this pinned the one production allocation call site to the reconciler table
		// (`reconcilableCompanionKinds`). Files no longer allocates, so the guard is that its install
		// path reaches no allocator and no registry kind-set at all.
		const source = readFileSync(join(import.meta.dir, "file-browser-companion.ts"), "utf8");
		const signature = "export function ensureFileBrowserCompanion(";
		const start = source.indexOf(signature);
		// Asserted found BEFORE the slice, and the slice is bounded at the next top-level
		// declaration — a guard that runs to end-of-file passes on an occurrence anywhere below.
		expect(start, `anchor not found in file-browser-companion.ts: ${signature}`).toBeGreaterThan(0);
		const next = source
			.slice(start + signature.length)
			.search(/\n(?:export |function |const |type |interface |class )/);
		const body =
			next === -1 ? source.slice(start) : source.slice(start, start + signature.length + next);
		expect(body).toContain("ensureFileBrowserServiceIsolation");
		for (const allocator of [
			"reconcilableCompanionKinds(",
			"recognisedCompanionKinds(",
			"ensureCompanionServiceIdentit",
			"ensureExclusiveCompanionServiceIdentity(",
		]) {
			expect(body).not.toContain(allocator);
		}
	});

	test("reports the unrecognised kind, and only it, as unsupported", () => {
		const { unsupportedCompanionKinds } = convergeDeclaring([UNRECOGNISED]);
		expect(unsupportedCompanionKinds).toEqual([UNRECOGNISED]);
	});

	test("renders the shipped unit byte-identically while an unrecognised kind is declared", () => {
		const { unit, templated } = convergeDeclaring([UNRECOGNISED]);
		// D-12: the wiring changes WHO calls the allocator, not what one companion gets.
		expect(templated).toBe(PRE_CHANGE_FILE_BROWSER_UNIT);
		expect(unit.split("\n")).toContain(`User=${TEST_RUNTIME_USER}`);
		expect(unit.split("\n")).toContain(`Group=${process.getegid?.() ?? 0}`);
		expect(unit).toContain(`StateDirectory=${SYSTEMD_FILE_BROWSER_STATE_DIRECTORY}`);
	});

	test("the isolation seam refuses raw manifest keys AT THE TYPE, not by convention", () => {
		// The D-14 intersection used to be enforced by a comment at one call site: with
		// `kinds: readonly string[]`, `ensureFileBrowserServiceIsolation(paths, root,
		// Object.keys(manifest.companions ?? {}))` compiled cleanly, and its consequence is a HARD
		// CONVERGE FAILURE for an unrecognised key — the opposite of D-14's "skip what you cannot
		// parse" availability rule.
		//
		// This is a TYPECHECK assertion that runs in the suite: delete the narrowing and the
		// `@ts-expect-error` below becomes unused, which `tsc --noEmit` reports as an error HERE.
		const rawKeys: string[] = ["filebrowser", UNRECOGNISED];
		// @ts-expect-error — raw manifest keys are not the declared-AND-recognised set.
		const wrong: Parameters<CompanionServiceIsolation>[2] = rawKeys;
		// Read at runtime so the binding is not merely unused — the assertion that matters is the
		// suppressed error above, which only exists while the seam is narrowed.
		expect(wrong).toHaveLength(2);
		// The complement: the recognised set IS assignable, so the narrowing did not simply break
		// the one production producer.
		const right: Parameters<CompanionServiceIsolation>[2] = recognisedCompanionKinds(rawKeys);
		expect(right).toEqual(["filebrowser"]);
	});

	test("intersects declared keys with the registry, deduplicated and sorted", () => {
		expect(recognisedCompanionKinds([])).toEqual([]);
		expect(recognisedCompanionKinds(["filebrowser"])).toEqual(["filebrowser"]);
		expect(recognisedCompanionKinds(["zzz-unknown", "filebrowser"])).toEqual(["filebrowser"]);
		expect(recognisedCompanionKinds(["filebrowser", "filebrowser"])).toEqual(["filebrowser"]);
	});
});

test("component proof requires committed configuration and a stable live invocation", async () => {
	const paths = tempRuntimePaths();
	const manifest = fileBrowserManifest(paths, { generation: 1, binary: "fixture" });
	mkdirSync(paths.systemdSystemRoot, { recursive: true });
	mkdirSync(dirname(paths.fileBrowserConfig), { recursive: true });
	writeFileSync(
		join(paths.systemdSystemRoot, `${RUNTIME_SYSTEMD_FILE_BROWSER_UNIT_NAME}.service`),
		`${GENERATED_RUNTIME_SYSTEMD_FILE_HEADER}\n[Service]\nExecStart=/fixture/files\n`,
	);
	writeFileSync(paths.fileBrowserConfig, "fixture-config");
	const activated = Object.fromEntries(readSystemdUnitSnapshot(paths).system);
	let invocation = "a".repeat(32);
	let managerConfiguration = "1".repeat(64);
	const readInvocation = () => ({
		invocationId: invocation,
		configurationRevision: managerConfiguration,
	});
	const componentActivations = captureComponentActivations(
		fileBrowserManifestLoad(manifest),
		paths,
		activated,
		readInvocation,
	);
	expect(componentActivations).toHaveLength(1);
	const applied = {
		schemaVersion: "clawdi.runtimeAppliedState.v2" as const,
		appliedAt: new Date().toISOString(),
		instanceId: manifest.instanceId,
		sourceRevision: "b".repeat(64),
		etag: `"sha256:${"b".repeat(64)}"`,
		generation: 1,
		manifestETag: '"fixture-manifest"',
		applyReceiptId: "fixture-apply-receipt",
		bootNonce: "fixture-boot-nonce",
		contentIdentity: { sourcePath: "fixture", sha256: "c".repeat(64) },
		activated,
		providerIds: [],
		projectedProviderIds: {},
	};
	mkdirSync(paths.serviceStateRoot, { recursive: true });
	writeRuntimeAppliedState(applied, paths);
	persistComponentActivations(fileBrowserManifestLoad(manifest), paths, readInvocation);
	const observe = () => observeComponents(paths, applied, readInvocation, async () => true);
	mkdirSync(
		join(paths.systemdSystemRoot, `${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}broken-peer.service`),
	);
	expect((await observe())?.entries[0]?.status).toBe("ok");
	const failedProbe = await observeComponents(paths, applied, readInvocation, async () => {
		throw new Error("HTTP unavailable");
	});
	expect(failedProbe?.entries[0]?.status).toBe("unknown");
	managerConfiguration = "2".repeat(64);
	expect((await observe())?.entries[0]?.status).toBe("unknown");
	managerConfiguration = "1".repeat(64);
	invocation = "d".repeat(32);
	expect((await observe())?.entries[0]).toMatchObject({ status: "ok", invocationId: invocation });
	const changing = await observeComponents(paths, applied, readInvocation, async () => {
		invocation = "e".repeat(32);
		return true;
	});
	expect(changing?.entries[0]?.status).toBe("unknown");
	invocation = "a".repeat(32);
	writeFileSync(paths.fileBrowserConfig, "changed-credential-config");
	expect((await observe())?.entries[0]?.status).toBe("unknown");
	expect(
		await observeComponents(
			paths,
			{ ...applied, appliedAt: "2000-01-01T00:00:00.000Z" },
			readInvocation,
			async () => true,
		),
	).toBeUndefined();
});
