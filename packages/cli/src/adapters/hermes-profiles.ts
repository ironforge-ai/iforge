import { accessSync, constants, existsSync, readdirSync, statSync } from "node:fs";
import { basename, join } from "node:path";
import { storeHasSessionsTable } from "./sqlite-store";

/**
 * The one store walk shared by both Hermes-lineage runtimes.
 *
 * Hermes and IronHermes both keep state in SQLite, and both are
 * profile-scoped: an install root holds `state.db` for the default profile plus
 * `profiles/<name>/state.db` for every named profile. Reading only the root
 * silently drops every profile's sessions — measured at 37% of recorded
 * activity on the live IronHermes install, and the profile stores are the
 * better-attributed ones. Upstream Hermes has the identical layout, so the same
 * walk closes the same gap for both (D-12).
 *
 * Scope, deliberately narrow:
 *
 * - Profiles are enumerated INSIDE one install root. A second install would sit
 *   BESIDE that root, and this walk never goes looking for one.
 * - No active-profile marker file is read. D-11 pools every store regardless of
 *   which profile is currently selected, and no such marker exists at the live
 *   install root, so reading one would be dead code with a misleading name.
 * - Every path this returns is only ever opened READ-ONLY. Profile directory
 *   names are untrusted input; they never construct a write target (writes
 *   resolve to the install root alone).
 */

export interface ListAgentStoreOptions {
	/** Bracketed prefix for warnings, e.g. `ironhermes`. Defaults to the root's basename. */
	label?: string;
}

/**
 * Synchronous candidate walk: every `state.db` inside `installRoot` that exists
 * and is non-empty, root first, then profiles in stable sorted order.
 *
 * This is the traversal. {@link listAgentStorePaths} is this function plus the
 * SQLite `sessions`-table probe, which cannot be synchronous because the
 * runtime's SQLite binding is imported lazily (see `sqlite-store.ts`). The
 * `AgentAdapter.getSessionsWatchPaths()` contract IS synchronous, and it
 * explicitly tolerates paths that do not exist or are not yet valid, so the
 * watcher consumes this candidate form directly rather than forcing a second
 * copy of the traversal into existence.
 *
 * The non-empty check keeps the two forms agreeing on the zero-byte store,
 * which is the one invalid-store case that actually occurs in the wild.
 */
export function listAgentStoreCandidatePaths(
	installRoot: string,
	opts: ListAgentStoreOptions = {},
): string[] {
	if (!existsSync(installRoot)) return [];

	const label = opts.label ?? basename(installRoot);
	const out: string[] = [];

	// The root EXISTS but may not be readable (T-02-07). `existsSync` tests
	// F_OK, not readability, so a chmod-000 root passes the check above and then
	// every child probe below fails — yielding an empty list with no warning,
	// which is indistinguishable from "this runtime isn't installed". That is
	// the same silent-skip this module refuses everywhere else: `iforge push`
	// would report success with 0 sessions while the stores sit there unread.
	//
	// Probed with `readdirSync` rather than a permission-bit test because the
	// question is "can this process enumerate it", and only trying answers that
	// across NFS, fuse mounts and encrypted-at-rest volumes.
	try {
		readdirSync(installRoot);
	} catch (e) {
		console.warn(
			`[${label}] could not enumerate ${installRoot}: ${e instanceof Error ? e.message : String(e)}`,
		);
		return out;
	}

	const rootStore = join(installRoot, "state.db");
	if (isNonEmptyFile(rootStore)) out.push(rootStore);

	const profilesDir = join(installRoot, "profiles");
	if (!existsSync(profilesDir)) return out;

	let entries: import("node:fs").Dirent[];
	try {
		entries = readdirSync(profilesDir, { withFileTypes: true });
	} catch (e) {
		// `profiles/` is present but unreadable (perm bits, encrypted-at-rest,
		// stale fuse mount, …). Silently treating that as "no profiles" hides the
		// fact that we actively skipped data — surface it on stderr so
		// `iforge push` doesn't appear to succeed with 0 sessions. Whatever was
		// already collected is still returned; never an empty list.
		console.warn(
			`[${label}] could not enumerate ${profilesDir}: ${e instanceof Error ? e.message : String(e)}`,
		);
		return out;
	}

	const profileNames = entries
		.filter((d) => d.isDirectory() && !d.name.startsWith("."))
		.map((d) => d.name)
		.sort();

	for (const name of profileNames) {
		// A profile directory with no store is skipped, not fatal: 3 of the 8 live
		// profile directories are exactly this case.
		const store = join(profilesDir, name, "state.db");
		if (isNonEmptyFile(store)) out.push(store);
	}

	return out;
}

/**
 * Every readable store inside `installRoot` that actually carries a `sessions`
 * table — root first, then profiles in stable sorted order.
 *
 * The single-store install is the ONE-ELEMENT case of this list, not a separate
 * branch: an install with no `profiles/` directory returns exactly
 * `[<installRoot>/state.db]`. An install whose root store is absent but whose
 * profile store is valid returns just the profile store (D-11).
 */
export async function listAgentStorePaths(
	installRoot: string,
	opts: ListAgentStoreOptions = {},
): Promise<string[]> {
	const label = opts.label ?? basename(installRoot);
	const candidates = listAgentStoreCandidatePaths(installRoot, opts);
	const out: string[] = [];
	for (const candidate of candidates) {
		// A store the process cannot read is skipped LOUDLY. The probe alone would
		// answer a flat "no sessions table" here, which is indistinguishable from
		// "not a store" — and that is exactly how a push reports success with 0
		// sessions while silently swallowing the user's data (T-02-07).
		try {
			accessSync(candidate, constants.R_OK);
		} catch (e) {
			console.warn(
				`[${label}] skipping unreadable store ${candidate}: ${e instanceof Error ? e.message : String(e)}`,
			);
			continue;
		}
		if (await storeHasSessionsTable(candidate)) out.push(candidate);
	}
	return out;
}

function isNonEmptyFile(path: string): boolean {
	try {
		const s = statSync(path);
		return s.isFile() && s.size > 0;
	} catch {
		return false;
	}
}
