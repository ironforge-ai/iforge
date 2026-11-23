/**
 * The public environment contract boundary.
 *
 * `IFORGE_*` is the only spelling a user may set (D-05a). This module copies each public name
 * into the internal `CLAWDI_*` register that the rest of the CLI already reads, so no
 * upstream-aligned read site has to move.
 *
 * The map is an explicit record, deliberately not a `Proxy` over `process.env`: a Proxy cannot
 * intercept Vite's build-time `import.meta.env` substitution and it hides which names are
 * actually part of the contract. The entries below are written out verbatim rather than read
 * from `config/rebrand-env-contract.txt` at runtime, because this module is bundled into a
 * native binary that has no repository next to it. `brand-env.test.ts` asserts the two agree.
 */

export const BRAND_ENV_ALIASES: Record<string, string> = {
	IFORGE_AGENT_TYPE: "CLAWDI_AGENT_TYPE",
	IFORGE_API_URL: "CLAWDI_API_URL",
	IFORGE_AUTH_TOKEN: "CLAWDI_AUTH_TOKEN",
	IFORGE_AUTH_TOKEN_ORIGIN: "CLAWDI_AUTH_TOKEN_ORIGIN",
	IFORGE_BACKEND_IMAGE: "CLAWDI_BACKEND_IMAGE",
	IFORGE_BACKEND_IMAGE_TAG: "CLAWDI_BACKEND_IMAGE_TAG",
	IFORGE_CHANNEL: "CLAWDI_CHANNEL",
	IFORGE_CLOUD_ADMIN_API_KEY: "CLAWDI_CLOUD_ADMIN_API_KEY",
	IFORGE_CLOUD_WORKLOAD_SIGNING_PRIVATE_KEY: "CLAWDI_CLOUD_WORKLOAD_SIGNING_PRIVATE_KEY",
	IFORGE_DAEMON_RPC_ALLOW_REMOTE: "CLAWDI_DAEMON_RPC_ALLOW_REMOTE",
	IFORGE_DAEMON_RPC_HOST: "CLAWDI_DAEMON_RPC_HOST",
	IFORGE_DAEMON_RPC_PORT: "CLAWDI_DAEMON_RPC_PORT",
	IFORGE_DAEMON_RPC_TOKEN: "CLAWDI_DAEMON_RPC_TOKEN",
	IFORGE_DEBUG: "CLAWDI_DEBUG",
	IFORGE_DEPLOY_API_URL: "CLAWDI_DEPLOY_API_URL",
	IFORGE_DESKTOP_ARCH: "CLAWDI_DESKTOP_ARCH",
	IFORGE_DESKTOP_UPDATE_CHANNEL: "CLAWDI_DESKTOP_UPDATE_CHANNEL",
	IFORGE_DESKTOP_UPDATE_FEED_URL: "CLAWDI_DESKTOP_UPDATE_FEED_URL",
	IFORGE_DESKTOP_VERSION: "CLAWDI_DESKTOP_VERSION",
	IFORGE_EGRESS_GID: "CLAWDI_EGRESS_GID",
	IFORGE_EGRESS_UID: "CLAWDI_EGRESS_UID",
	IFORGE_ENVIRONMENT_ID: "CLAWDI_ENVIRONMENT_ID",
	IFORGE_INSTALL_PREFIX: "CLAWDI_INSTALL_PREFIX",
	IFORGE_NO_AUTO_UPDATE: "CLAWDI_NO_AUTO_UPDATE",
	IFORGE_NO_UPDATE_CHECK: "CLAWDI_NO_UPDATE_CHECK",
	IFORGE_OPENCLAW_HOSTED_SOURCE: "CLAWDI_OPENCLAW_HOSTED_SOURCE",
	IFORGE_PROCESS_ROLE: "CLAWDI_PROCESS_ROLE",
	IFORGE_RUN_DIR: "CLAWDI_RUN_DIR",
	IFORGE_RUNTIME_GID: "CLAWDI_RUNTIME_GID",
	IFORGE_RUNTIME_HOME: "CLAWDI_RUNTIME_HOME",
	IFORGE_RUNTIME_MODE: "CLAWDI_RUNTIME_MODE",
	IFORGE_RUNTIME_UID: "CLAWDI_RUNTIME_UID",
	IFORGE_SECRET_EXPORT_PASSPHRASE: "CLAWDI_SECRET_EXPORT_PASSPHRASE",
	IFORGE_SERVE_DEBUG: "CLAWDI_SERVE_DEBUG",
	IFORGE_SERVE_MODE: "CLAWDI_SERVE_MODE",
	IFORGE_STATE_DIR: "CLAWDI_STATE_DIR",
	IFORGE_SYSTEMD_APPLY: "CLAWDI_SYSTEMD_APPLY",
	IFORGE_TEST_POSTGRES_CPUS: "CLAWDI_TEST_POSTGRES_CPUS",
	IFORGE_TEST_POSTGRES_MEMORY_LIMIT: "CLAWDI_TEST_POSTGRES_MEMORY_LIMIT",
	IFORGE_TEST_POSTGRES_PIDS_LIMIT: "CLAWDI_TEST_POSTGRES_PIDS_LIMIT",
	IFORGE_TEST_RUNNER_CPUS: "CLAWDI_TEST_RUNNER_CPUS",
	IFORGE_TEST_RUNNER_MEMORY_LIMIT: "CLAWDI_TEST_RUNNER_MEMORY_LIMIT",
	IFORGE_TEST_RUNNER_PIDS_LIMIT: "CLAWDI_TEST_RUNNER_PIDS_LIMIT",
	IFORGE_VERSION: "CLAWDI_VERSION",
	IFORGE_WA_NETWORK_NAMESPACE_MARKER: "CLAWDI_WA_NETWORK_NAMESPACE_MARKER",
	IFORGE_WA_SIDECAR_LOG_LEVEL: "CLAWDI_WA_SIDECAR_LOG_LEVEL",
	IFORGE_WA_SIDECAR_SOCKET_PATH: "CLAWDI_WA_SIDECAR_SOCKET_PATH",
	IFORGE_WA_SIDECAR_STATE_ROOT: "CLAWDI_WA_SIDECAR_STATE_ROOT",
	IFORGE_WA_WEBSOCKET_URL: "CLAWDI_WA_WEBSOCKET_URL",
	IFORGE_WEB_IMAGE: "CLAWDI_WEB_IMAGE",
	IFORGE_WEB_IMAGE_TAG: "CLAWDI_WEB_IMAGE_TAG",
	IFORGE_WINDOWS_PUBLISHER: "CLAWDI_WINDOWS_PUBLISHER",
	IFORGE_WINDOWS_TASK_TEST: "CLAWDI_WINDOWS_TASK_TEST",
};

/** Reverse index of {@link BRAND_ENV_ALIASES}, built once so lookups cannot drift from the map. */
const INTERNAL_TO_PUBLIC: Record<string, string> = Object.fromEntries(
	Object.entries(BRAND_ENV_ALIASES).map(([publicName, internalName]) => [internalName, publicName]),
);

/**
 * The public spelling a contract member is set under, or `undefined` for a non-member.
 *
 * Call sites that print an environment name to the user, or write one into a child process, must
 * route it through here rather than hardcoding a literal: a future contract change then moves the
 * printed label and the child's key together, instead of leaving either behind.
 */
export function publicNameFor(internalName: string): string | undefined {
	return INTERNAL_TO_PUBLIC[internalName];
}

/**
 * Delete every internal register name that arrived WITHOUT its public counterpart, and return the
 * names deleted, in map order. Mutates `env` in place.
 *
 * The `CLAWDI_*` register is the CLI's own private namespace: the only supported way to put a
 * value in it is to set the `IFORGE_*` name the contract maps onto it (D-05a). A value found
 * there with no public counterpart present therefore cannot have come from this boundary — it was
 * supplied from outside it, which is exactly what BRAND-02 says is never accepted.
 *
 * This must run BEFORE {@link applyBrandEnvAliases}. Applying first would copy public values into
 * the register and make a name this process just wrote indistinguishable from one a user typed.
 *
 * The refusal is driven by the contract, never by the `CLAWDI_` prefix. A non-member such as
 * `CLAWDI_HOME` (D-07/X-1) or the build-time `CLAWDI_DEFAULT_API_URL` has no public spelling, so
 * it is not a name anyone could have "supplied under the wrong spelling", and it is left alone.
 */
export function refuseUserSuppliedInternalNames(env: NodeJS.ProcessEnv = process.env): string[] {
	const refused: string[] = [];
	for (const [publicName, internalName] of Object.entries(BRAND_ENV_ALIASES)) {
		const publicValue = env[publicName];
		// An empty public value is unset everywhere else in this module, so it cannot rescue a
		// supplied internal name here either.
		if (typeof publicValue === "string" && publicValue !== "") continue;
		if (!(internalName in env)) continue;
		delete env[internalName];
		refused.push(internalName);
	}
	return refused;
}

/**
 * Resolve the internal bindings a public environment implies. Pure: `env` is never mutated.
 *
 * A bare internal name contributes nothing. The boundary does not read the internal register as
 * user configuration, which is the negative half of REBRAND-13.
 */
export function resolveBrandEnv(env: NodeJS.ProcessEnv = process.env): Record<string, string> {
	const resolved: Record<string, string> = {};
	for (const [publicName, internalName] of Object.entries(BRAND_ENV_ALIASES)) {
		const value = env[publicName];
		if (typeof value !== "string" || value === "") continue;
		resolved[internalName] = value;
	}
	return resolved;
}

/**
 * Apply the contract to `env` in place and return how many pairs were applied.
 *
 * The public key it consumed is KEPT, not deleted. That is the load-bearing half of the exclusive
 * boundary: the public spelling is what a child process inherits and re-derives its own internal
 * register from, which is what lets {@link refuseUserSuppliedInternalNames} run unconditionally at
 * every entrypoint with no provenance marker and no special case for "the parent set this".
 *
 * The reason the previous, additive design gave for never removing an internal name it did not set
 * — the CLI writes mapped names into its own child environments (systemd units, the daemon,
 * self-update and operation subprocesses), and stripping them at the child's entrypoint would
 * break every one of them — is preserved by this design rather than abandoned. Those child
 * environments now carry the PUBLIC spelling of every contract member, so the child's own boundary
 * re-derives the register instead of inheriting it.
 */
export function applyBrandEnvAliases(env: NodeJS.ProcessEnv = process.env): number {
	const resolved = resolveBrandEnv(env);
	for (const [internalName, value] of Object.entries(resolved)) {
		env[internalName] = value;
	}
	return Object.keys(resolved).length;
}
