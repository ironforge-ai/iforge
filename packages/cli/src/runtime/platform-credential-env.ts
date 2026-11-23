import { publicNameFor } from "../lib/brand-env";

const PLATFORM_CREDENTIAL_ENV_KEYS = [
	"CLAWDI_AUTH_TOKEN",
	"CLAWDI_DAEMON_RPC_TOKEN",
	"CLAWDI_EGRESS_SECRET_FILE",
] as const;

/**
 * Remove BOTH spellings of every platform credential from an environment bound for a child
 * process.
 *
 * Deleting only the internal register is not a scrub. The public spelling survives in
 * `process.env` by design (it is what a child re-derives its own register from), so a child that
 * inherited `IFORGE_AUTH_TOKEN` would copy the credential straight back into
 * `CLAWDI_AUTH_TOKEN` at its own entrypoint — reintroducing exactly what this removes
 * (T-03.1G-01). The public name is derived from the alias map, never typed as a second literal;
 * a key with no contract entry (`CLAWDI_EGRESS_SECRET_FILE`) has no public spelling to remove.
 */
export function clearPlatformCredentialEnv(env: NodeJS.ProcessEnv): void {
	for (const key of PLATFORM_CREDENTIAL_ENV_KEYS) {
		delete env[key];
		const publicName = publicNameFor(key);
		if (publicName !== undefined) delete env[publicName];
	}
}
