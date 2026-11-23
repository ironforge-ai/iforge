import { dirname, isAbsolute, relative, resolve } from "node:path";
import { publicNameFor } from "../lib/brand-env";
import type { RuntimeApplyContext } from "./apply-identity";
import type { RuntimePaths } from "./paths";
import {
	type RuntimeUserIdentityResolver,
	resolveRuntimeUserIdentity,
} from "./runtime-user-command";
import { assertRuntimePlatformRoots } from "./state";

export const HOSTED_RUNTIME_HOME = "/home/iforge";
export const HOSTED_RUNTIME_USER = "iforge";
export const HOSTED_RUNTIME_UID = 10_001;
export const HOSTED_RUNTIME_GID = 10_001;

export interface HostedRuntimeIdentityExpectation {
	home: string;
	user: string;
	uid: number;
	gid: number;
}

export interface HostedRuntimeContractOptions {
	expectedIdentity?: HostedRuntimeIdentityExpectation;
	resolveUserIdentity?: RuntimeUserIdentityResolver;
}

export interface HostedRuntimeIdentityInspection {
	mode: { ok: boolean; detail: string; error?: string };
	home: { ok: boolean; detail: string; error?: string };
	user: { ok: boolean; detail: string; error?: string };
}

export interface HostedRuntimeContract {
	identity: HostedRuntimeIdentityExpectation;
}

const PRODUCTION_IDENTITY: HostedRuntimeIdentityExpectation = {
	home: HOSTED_RUNTIME_HOME,
	user: HOSTED_RUNTIME_USER,
	uid: HOSTED_RUNTIME_UID,
	gid: HOSTED_RUNTIME_GID,
};

/**
 * The register this module READS. Internal spelling, upstream-aligned, never printed.
 *
 * `brand-env.ts` copies `IFORGE_*` into `CLAWDI_*` before anything here runs, so the internal
 * name is what is actually present in `process.env` by the time this executes.
 */
const RUNTIME_MODE_REGISTER = "CLAWDI_RUNTIME_MODE";

function explicitRuntimeMode(): string {
	return process.env[RUNTIME_MODE_REGISTER]?.trim().toLowerCase() || "missing";
}

export function inspectHostedRuntimeIdentity(
	paths: RuntimePaths,
	options: HostedRuntimeContractOptions = {},
): HostedRuntimeIdentityInspection {
	const expected = options.expectedIdentity ?? PRODUCTION_IDENTITY;
	const mode = explicitRuntimeMode();
	// THE ASYMMETRY ON THESE TWO LINES IS DELIBERATE, not a typo: the name this module READS is
	// the internal register above, and the name it PRINTS is that register's PUBLIC spelling,
	// taken from the alias map rather than restated. That is the environment cut (D-05a) --
	// `IFORGE_*` is the only spelling an operator may set, while every deployment already
	// exporting the internal one keeps working because `brand-env.ts` copies it across first.
	// "Fixing" the two to agree would break one side or the other. WINDOWS #62.
	const publicModeRegister = publicNameFor(RUNTIME_MODE_REGISTER) ?? RUNTIME_MODE_REGISTER;
	const modeError =
		mode === "hosted" && paths.mode === "hosted"
			? undefined
			: `hosted convergence requires ${publicModeRegister}=hosted explicitly; resolved runtime mode is ${paths.mode} (environment: ${mode})`;
	const homeError =
		paths.userHome === expected.home
			? undefined
			: `hosted runtime HOME must resolve to ${expected.home}; resolved ${paths.userHome}`;
	const runtimeUser = process.env.CLAWDI_RUNTIME_USER?.trim() ?? "";
	let userError: string | undefined;
	let userDetail = runtimeUser || "missing";
	if (runtimeUser !== expected.user) {
		userError = runtimeUser
			? `hosted runtime user must be ${expected.user}; resolved ${runtimeUser}`
			: `hosted runtime user must be ${expected.user}; CLAWDI_RUNTIME_USER is missing`;
	} else {
		try {
			const identity = (options.resolveUserIdentity ?? resolveRuntimeUserIdentity)(runtimeUser);
			userDetail = `${runtimeUser} (${identity.uid}:${identity.gid})`;
			if (identity.uid === 0 || identity.gid === 0) {
				userError = `hosted runtime user ${runtimeUser} resolved to root identity ${identity.uid}:${identity.gid}`;
			} else if (identity.uid !== expected.uid || identity.gid !== expected.gid) {
				userError = `hosted runtime user ${runtimeUser} must resolve to ${expected.uid}:${expected.gid}; resolved ${identity.uid}:${identity.gid}`;
			} else {
				for (const [name, value, resolved] of [
					["CLAWDI_RUNTIME_UID", process.env.CLAWDI_RUNTIME_UID?.trim(), identity.uid],
					["CLAWDI_RUNTIME_GID", process.env.CLAWDI_RUNTIME_GID?.trim(), identity.gid],
				] as const) {
					if (value && value !== String(resolved)) {
						// Read the internal register, print its public spelling (BRAND-02).
						userError = `${publicNameFor(name) ?? name} must match the resolved hosted runtime identity ${resolved}; resolved ${value}`;
						break;
					}
				}
			}
		} catch (error) {
			userError = error instanceof Error ? error.message : String(error);
		}
	}

	return {
		mode: {
			ok: modeError === undefined,
			detail: paths.mode,
			...(modeError ? { error: modeError } : {}),
		},
		home: {
			ok: homeError === undefined,
			detail: paths.userHome,
			...(homeError ? { error: homeError } : {}),
		},
		user: {
			ok: userError === undefined,
			detail: userDetail,
			...(userError ? { error: userError } : {}),
		},
	};
}

export function assertHostedRuntimeContract(
	paths: RuntimePaths,
	applyContext: RuntimeApplyContext,
	options: HostedRuntimeContractOptions & { platformRoots?: "required" | "deferred" } = {},
): HostedRuntimeContract {
	const inspection = inspectHostedRuntimeIdentity(paths, options);
	const error = [inspection.mode, inspection.home, inspection.user].find(
		(check) => !check.ok,
	)?.error;
	if (error) throw new Error(error);
	const stateRoot = resolve(paths.clawdiHome);
	const serviceStateRoot = resolve(paths.serviceStateRoot);
	const relativeState = relative(resolve(paths.userHome), stateRoot);
	if (relativeState === "" || (!relativeState.startsWith("..") && !isAbsolute(relativeState))) {
		throw new Error(
			`hosted CLAWDI_HOME must be outside the tenant home; resolved ${paths.clawdiHome}`,
		);
	}
	if (
		!isAbsolute(paths.clawdiHome) ||
		stateRoot === serviceStateRoot ||
		dirname(stateRoot) !== dirname(serviceStateRoot)
	) {
		throw new Error(
			`hosted CLAWDI_HOME must be an absolute sibling of ${paths.serviceStateRoot}; resolved ${paths.clawdiHome}`,
		);
	}
	if (applyContext.backend !== "incus") {
		throw new Error(
			`hosted v2 convergence requires runtime context backend incus; resolved ${String(applyContext.backend ?? "missing")}`,
		);
	}
	if (options.platformRoots !== "deferred") assertRuntimePlatformRoots(paths);
	return {
		identity: options.expectedIdentity ?? PRODUCTION_IDENTITY,
	};
}
