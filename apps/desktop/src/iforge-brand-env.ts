/**
 * The desktop app's public environment boundary (iforge, 06.1-14, D-23). Fork-owned.
 *
 * `IFORGE_*` is the only spelling a user sets for the desktop release names (03.1 D-05a, and the
 * 2026-09-30 user directive: no Clawdi-name aliases). Upstream's release reader,
 * `release-contract.ts`, reads the internal `CLAWDI_*` register and stays upstream-verbatim for
 * those reads. This module sits in front of it:
 *
 *  - a register name that arrived WITHOUT its public counterpart is dropped, never read, exactly
 *    as `packages/cli/src/lib/brand-env.ts` `refuseUserSuppliedInternalNames` drops one;
 *  - each public value is then copied into its register name.
 *
 * The six pairs are written out rather than read from `config/rebrand-env-contract.txt`, the same
 * choice brand-env.ts makes; `iforge-brand-env.test.ts` asserts they equal the contract's
 * `IFORGE_DESKTOP_*` and `IFORGE_WINDOWS_*` pairs.
 */

export const IFORGE_DESKTOP_ENV_ALIASES: Readonly<Record<string, string>> = Object.freeze({
	IFORGE_DESKTOP_ARCH: "CLAWDI_DESKTOP_ARCH",
	IFORGE_DESKTOP_UPDATE_CHANNEL: "CLAWDI_DESKTOP_UPDATE_CHANNEL",
	IFORGE_DESKTOP_UPDATE_FEED_URL: "CLAWDI_DESKTOP_UPDATE_FEED_URL",
	IFORGE_DESKTOP_VERSION: "CLAWDI_DESKTOP_VERSION",
	IFORGE_WINDOWS_PUBLISHER: "CLAWDI_WINDOWS_PUBLISHER",
	IFORGE_WINDOWS_TASK_TEST: "CLAWDI_WINDOWS_TASK_TEST",
});

/**
 * Return a copy of `env` with the desktop contract applied. Pure: `env` is never mutated.
 *
 * An empty public value counts as unset, so it neither reaches the register nor rescues a
 * user-set register name.
 */
export function withDesktopBrandEnv(
	env: Record<string, string | undefined>,
): Record<string, string | undefined> {
	const result: Record<string, string | undefined> = { ...env };
	for (const [publicName, internalName] of Object.entries(IFORGE_DESKTOP_ENV_ALIASES)) {
		const value = env[publicName];
		if (typeof value === "string" && value !== "") {
			result[internalName] = value;
		} else {
			delete result[internalName];
		}
	}
	return result;
}

/**
 * Rewrite every desktop register name in `text` to its public spelling.
 *
 * Upstream's release reader names the register in its errors; a user only ever sets the public
 * name, so an error must tell them that one. Whole names only: a longer identifier that merely
 * starts with a register name is left alone.
 */
export function publicDesktopEnvText(text: string): string {
	let result = text;
	for (const [publicName, internalName] of Object.entries(IFORGE_DESKTOP_ENV_ALIASES)) {
		result = result.replace(new RegExp(`\\b${internalName}\\b`, "g"), publicName);
	}
	return result;
}
