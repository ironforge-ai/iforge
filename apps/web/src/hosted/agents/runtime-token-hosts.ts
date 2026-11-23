import { env, isProductionRuntime } from "@/lib/env";

/**
 * The OpenClaw browser-session and Files bootstrap endpoints are named by the
 * deployment record and receive the user's session token. Only an `https`
 * host under a configured runtime suffix may receive it, so a wrong or
 * tampered record cannot send the token to an arbitrary host.
 */
export function parseRuntimeHostSuffixes(raw: string | undefined): string[] {
	return (raw ?? "")
		.split(",")
		.map((suffix) =>
			suffix
				.trim()
				.toLowerCase()
				.replace(/^\.+|\.+$/g, ""),
		)
		.filter((suffix) => suffix.length > 0);
}

export function isTrustedRuntimeTokenUrl(
	value: string,
	suffixes: readonly string[],
	allowUnconfigured: boolean,
): boolean {
	let url: URL;
	try {
		url = new URL(value);
	} catch {
		return false;
	}
	if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) {
		return false;
	}
	if (suffixes.length === 0) return allowUnconfigured;
	const host = url.hostname.toLowerCase();
	return suffixes.some((suffix) => host.endsWith(`.${suffix}`));
}

export function assertRuntimeTokenUrl(value: string): void {
	const suffixes = parseRuntimeHostSuffixes(env.VITE_IFORGE_RUNTIME_HOST_SUFFIXES);
	if (!isTrustedRuntimeTokenUrl(value, suffixes, !isProductionRuntime())) {
		throw new Error("Runtime host is not trusted for this session.");
	}
}
