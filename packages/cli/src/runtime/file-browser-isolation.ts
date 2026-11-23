import { spawnSync } from "node:child_process";
import { lstatSync } from "node:fs";
import {
	COMPANION_KEY_PATTERN,
	COMPANION_REGISTRY,
	type CompanionRegistryEntry,
	type KnownCompanionKind,
	RESERVED_COMPANION_UID_BLOCK,
} from "./companion-registry";
import { HOSTED_RUNTIME_USER } from "./hosted-runtime-contract";
import type { RuntimePaths } from "./paths";
import { runtimeUserGid, runtimeUserUid } from "./runtime-user-command";
import { RUNTIME_SYSTEMD_OWNED_NAME_PREFIX } from "./systemd-user";

// Files has no service account of its own any more. Upstream #939 (64916c0a) runs the file
// browser as the tenant runtime user that owns the home it serves, so the per-kind account machinery
// below is used by the container companions (`container-companion.ts`) and no longer by Files.

/**
 * The POSIX identity one companion's unit runs as.
 *
 * Named per companion rather than per product: D-10 gives every companion kind its own non-login
 * system account, so this shape is no longer the file browser's private concern. The file-browser
 * spelling below is kept as an ALIAS so `file-browser-companion.ts` and the 200+ reconciliation
 * tests that inject a `serviceIsolation` seam keep compiling unchanged.
 */
export interface CompanionServiceIdentity {
	uid: number;
	gid: number;
}

/**
 * The isolation seam the install path invokes, and the tests inject.
 *
 * `kinds` is REQUIRED, not optional. An optional third parameter is the one shape this type must
 * not have: a caller that omitted it would degrade back to a single hardcoded kind — the exact
 * unenforced state 04-08 exists to end — while still typechecking. Required means `tsc --noEmit`
 * catches that caller instead. Existing fewer-parameter test stubs stay assignable under
 * TypeScript's parameter-count rule, so injecting a two- or zero-parameter stub still compiles.
 *
 * `kinds` is `KnownCompanionKind[]`, not `string[]`, so the D-14 intersection is enforced BY THE
 * TYPE rather than by a comment at one call site. With `string[]` the exact mistake
 * `recognisedCompanionKinds`' docstring warns against —
 * `ensureFileBrowserServiceIsolation(paths, root, Object.keys(manifest.companions ?? {}))` — compiled
 * cleanly, and its consequence is concrete: an unrecognised key reaching
 * `ensureCompanionServiceIdentity` throws, i.e. a HARD CONVERGE FAILURE, which contradicts D-14's
 * own availability rationale that the CLI skips what it cannot parse rather than dying on it.
 * Only the registry's own kind-set helpers produce this type — `recognisedCompanionKinds` and, as of
 * WR-05, the narrower `reconcilableCompanionKinds` layered on top of it — which is the point: the
 * narrowing lives on the SEAM, where the call-site mistake is made.
 */
export type CompanionServiceIsolation = (
	paths: RuntimePaths,
	sourceRoot: string,
	kinds: readonly KnownCompanionKind[],
) => CompanionServiceIdentity;

export type FileBrowserServiceIdentity = CompanionServiceIdentity;
/**
 * The Files isolation seam: the tenant runtime identity the file browser runs as (#939). It takes
 * no companion kinds, because Files allocates no account of its own.
 */
export type FileBrowserServiceIsolation = (
	paths: RuntimePaths,
	sourceRoot: string,
) => FileBrowserServiceIdentity;

/**
 * Every per-kind name one companion needs, all derived from ONE registry key.
 *
 * Single derivation is the isolation property, not a tidiness preference: T-4-06 is companion A
 * reading companion B's state, and the mitigation is that two kinds cannot land on the same
 * `StateDirectory=`, the same account or the same credential id. With one helper there is exactly
 * one place for an adjacency test to assert against; with four ad-hoc concatenations there are
 * four places for two kinds to collide and no single place to prove they do not.
 */
export interface CompanionServiceNames {
	/** The registry key this name set was derived from. */
	kind: string;
	/** The non-login system account the unit's `User=` names. */
	user: string;
	/** The system group the unit's `Group=` names. */
	group: string;
	/** The unit's `StateDirectory=` / `RuntimeDirectory=` value. */
	stateDirectory: string;
	/** The credential id in the unit's credential directive, and under `$CREDENTIALS_DIRECTORY`. */
	credentialId: string;
}

/**
 * Kinds whose shipped names or uid are what they are BECAUSE INSTALLS EXIST, not because they were
 * chosen — each a named, documented exemption rather than a fall-through.
 *
 * `filebrowser` is the one entry, and it carries two exemptions for one underlying reason. Its
 * account (`iforge-files`), its group and its `StateDirectory=` all use the `files` stem rather
 * than the registry key `filebrowser`, and its uid is host-allocated rather than pinned to a number
 * from the reserved block. Both predate the registry: the account already exists on every
 * provisioned host and OWNS FILES ON DISK, and the state directory already holds that companion's
 * data. Deriving either from the key today would rename a live account and strand a live state
 * directory — the re-own migration D-10 records as the one-way door of this decision.
 *
 * This is the same shape as the comment in `hosted-runtime-contract.ts` about 10001 never having
 * been renamed: the value is what it is because installs exist, not because it was chosen. A kind
 * added later has no install history, so it has no exemption and derives everything from its key.
 *
 * `config/companion-registry.txt`'s EXCLUSIONS block carries the `preallocated` half of this in
 * prose; `resolveCompanionServiceUid` honours it in code.
 */
export const COMPANION_SERVICE_ACCOUNT_EXEMPTIONS: Readonly<
	Record<string, { readonly nameStem: string; readonly uid: "preallocated" }>
> = {
	filebrowser: { nameStem: "files", uid: "preallocated" },
};

/**
 * Derive one kind's names from its registry key.
 *
 * The key is bounded by `COMPANION_KEY_PATTERN` at the manifest parse boundary (04-02, T-4-01).
 * That bound is IMPORTED and re-checked here rather than re-derived, because this function turns a
 * key into a UID lookup name and a `StateDirectory=` value — the two places a traversal or an
 * over-long user name would actually bite — and this module is also callable from paths the
 * manifest parser never ran on.
 */
export function companionServiceNames(kind: string): CompanionServiceNames {
	if (!COMPANION_KEY_PATTERN.test(kind)) {
		throw new Error(
			`Companion kind ${JSON.stringify(kind)} is outside the companion key pattern ${COMPANION_KEY_PATTERN.source}`,
		);
	}
	// OWN properties only. `COMPANION_KEY_PATTERN` accepts one `Object.prototype` member —
	// `constructor` — so a bare index read on a plain object literal resolves an INHERITED member for
	// that key instead of `undefined`. Benign here only by accident (the `Object` function carries no
	// `nameStem`), and the identical shape one function below was not benign at all.
	const exemption = Object.hasOwn(COMPANION_SERVICE_ACCOUNT_EXEMPTIONS, kind)
		? COMPANION_SERVICE_ACCOUNT_EXEMPTIONS[kind]
		: undefined;
	const stem = exemption?.nameStem ?? kind;
	const name = `${RUNTIME_SYSTEMD_OWNED_NAME_PREFIX}${stem}`;
	return {
		kind,
		user: name,
		group: name,
		stateDirectory: name,
		// The credential id is the KEY, not the name stem: it is already `filebrowser.yaml` in the
		// shipped unit, and it is embedded in and verified against the ciphertext when the encrypted
		// form is used, so it must not follow a rename the account did.
		credentialId: `${kind}.yaml`,
	};
}

/**
 * The numeric uid a kind's account must be pinned to, or `null` when the host allocates it.
 *
 * `null` means "call `useradd` with no `--uid`, exactly as today". That is the `preallocated`
 * sentinel, and it is the behaviour the existing `filebrowser` account depends on.
 *
 * A decimal uid must fall inside `RESERVED_COMPANION_UID_BLOCK` — a number outside it risks
 * colliding with an account the provisioning image created (T-4-30) — and must not already be
 * claimed by another registry row. That second check is where D-13's never-reclaim rule becomes
 * enforceable in code rather than in prose: the registry file is append-only, and a later editor
 * who reuses a burned number by editing one column is refused here rather than silently granted a
 * retired companion's files on disk.
 */
export function resolveCompanionServiceUid(
	kind: string,
	registry: Readonly<Record<string, CompanionRegistryEntry>> = COMPANION_REGISTRY,
): number | null {
	// OWN properties only, and belt-and-braces with `COMPANION_REGISTRY`'s own stripped prototype:
	// `registry` is a CALLER-SUPPLIED parameter, so the default's null prototype says nothing about
	// the object this call actually reads. Without this the refusal below did not fire for the key
	// `constructor` — a key the manifest parser accepts — and this function returned the inherited
	// `Object` function's absent `uid`, i.e. `undefined`, in violation of its own `number | null`
	// signature. `assertCompanionServiceAccount`'s registry-pinned-UID check is `!= null`, so an
	// `undefined` pin made that assertion silently no-op on a host where the account already existed.
	const entry = Object.hasOwn(registry, kind) ? registry[kind] : undefined;
	if (entry === undefined) {
		throw new Error(`Companion kind ${JSON.stringify(kind)} has no row in the companion registry`);
	}
	if (entry.uid === "preallocated") return null;
	const uid = entry.uid;
	if (uid < RESERVED_COMPANION_UID_BLOCK.first || uid > RESERVED_COMPANION_UID_BLOCK.last) {
		throw new Error(
			`Companion ${kind} declares uid ${uid}, outside the reserved companion UID block ${RESERVED_COMPANION_UID_BLOCK.first}-${RESERVED_COMPANION_UID_BLOCK.last}`,
		);
	}
	for (const [otherKind, other] of Object.entries(registry)) {
		if (otherKind !== kind && other.uid === uid) {
			throw new Error(
				`Companion ${kind} declares uid ${uid}, already claimed by companion ${otherKind} — companion UIDs are append-only and never reclaimed (D-13)`,
			);
		}
	}
	return uid;
}

interface PasswdEntry {
	uid: number;
	gid: number;
	home: string;
	shell: string;
}

/**
 * The account assertions, every one of them, kept from the file-browser-only original.
 *
 * Split one-per-throw rather than left as the original's single combined condition so each failure
 * names the property that failed. Nothing was dropped in the split — T-4-29 is exactly "a companion
 * account that is root, is the tenant runtime user, or carries a shared supplementary group", and
 * the plan counts the throw sites in this file precisely so a generalisation cannot quietly lose
 * one.
 */
export function assertCompanionServiceAccount(input: {
	names: CompanionServiceNames;
	account: { uid: number; gid: number; home: string; shell: string };
	gid: number;
	runtimeUid: number;
	runtimeGid: number;
	expectedUid?: number | null;
}): void {
	const { names, account, gid, runtimeUid, runtimeGid } = input;
	if (account.uid === 0) {
		throw new Error(`Companion ${names.kind} service identity must not be root`);
	}
	if (account.uid === runtimeUid) {
		throw new Error(
			`Companion ${names.kind} service identity is not distinct from the tenant runtime identity`,
		);
	}
	if (account.gid !== gid) {
		throw new Error(
			`Companion ${names.kind} service identity does not belong to its own group ${names.group}`,
		);
	}
	if (account.gid === runtimeGid) {
		throw new Error(`Companion ${names.kind} service identity shares the tenant runtime group`);
	}
	if (input.expectedUid != null && account.uid !== input.expectedUid) {
		throw new Error(
			`Companion ${names.kind} service identity resolved to uid ${account.uid}, not the registry-pinned ${input.expectedUid}`,
		);
	}
	if (account.home !== "/nonexistent" || !account.shell.endsWith("/nologin")) {
		throw new Error(
			`Companion ${names.kind} service identity must be non-login with no home directory`,
		);
	}
}

/**
 * The POST-MUTATION re-assertion: after the supplementary-group stripping, prove it worked.
 *
 * Kept as a separate exported function specifically so it survives the generalisation with a test
 * that can drive it directly. Stripping a group and then trusting that it was stripped is how a
 * shared group survives a converge.
 */
export function assertCompanionServiceGroups(input: {
	names: CompanionServiceNames;
	runtimeGids: readonly number[];
	serviceGids: readonly number[];
	gid: number;
}): void {
	if (
		input.runtimeGids.includes(input.gid) ||
		input.serviceGids.some((memberGid) => memberGid !== input.gid)
	) {
		throw new Error(
			`Companion ${input.names.kind} service identity has unsafe supplementary group membership`,
		);
	}
}

function commandOutput(command: string, args: string[]): string {
	const result = spawnSync(command, args, {
		encoding: "utf8",
		maxBuffer: 64 * 1024,
		timeout: 30_000,
	});
	if (result.status !== 0) {
		const detail = [result.error?.message, result.stderr, result.stdout]
			.filter((value): value is string => typeof value === "string" && value.trim().length > 0)
			.join("\n")
			.trim()
			.slice(-2000);
		throw new Error(
			`Companion service isolation command failed: ${command}${detail ? `: ${detail}` : ""}`,
		);
	}
	return result.stdout.trim();
}

function lookupGroup(name: string): number | null {
	const result = spawnSync("getent", ["group", name], { encoding: "utf8" });
	if (result.status !== 0) return null;
	const fields = result.stdout.trim().split(":");
	const gid = Number.parseInt(fields[2] ?? "", 10);
	if (!Number.isInteger(gid) || gid <= 0) {
		throw new Error(`Companion service group ${name} has an invalid gid`);
	}
	return gid;
}

function lookupUser(name: string): PasswdEntry | null {
	const result = spawnSync("getent", ["passwd", name], { encoding: "utf8" });
	if (result.status !== 0) return null;
	const fields = result.stdout.trim().split(":");
	const uid = Number.parseInt(fields[2] ?? "", 10);
	const gid = Number.parseInt(fields[3] ?? "", 10);
	if (!Number.isInteger(uid) || uid <= 0 || !Number.isInteger(gid) || gid <= 0) {
		throw new Error(`Companion service user ${name} has an invalid filesystem identity`);
	}
	return { uid, gid, home: fields[5] ?? "", shell: fields[6] ?? "" };
}

function groupIdsForUser(name: string): number[] {
	const output = commandOutput("id", ["-G", name]);
	const gids = output.split(/\s+/).map((value) => Number.parseInt(value, 10));
	if (gids.length === 0 || gids.some((gid) => !Number.isInteger(gid) || gid <= 0)) {
		throw new Error(`Companion service identity ${name} has invalid group membership`);
	}
	return [...new Set(gids)];
}

function groupNameForGid(gid: number): string {
	const result = spawnSync("getent", ["group", String(gid)], { encoding: "utf8" });
	const name = result.status === 0 ? result.stdout.trim().split(":")[0]?.trim() : "";
	if (!name) throw new Error(`Companion service identity group ${gid} cannot be resolved`);
	return name;
}

function removeSupplementaryGroup(user: string, gid: number): void {
	commandOutput("gpasswd", ["--delete", user, groupNameForGid(gid)]);
}

/**
 * Allocate (or adopt) one companion kind's service identity and prove it is safe.
 *
 * This is the file-browser-only `ensureServiceIdentity` WIDENED, not rewritten. `04-RESEARCH.md`
 * § "Don't Hand-Roll" records that every expensive mistake in this domain is already solved once,
 * narrowly, here — the supplementary-group stripping in particular is a thing a fresh
 * implementation would not think to do — and that the dominant risk of this task is a second
 * implementation of a hardened path rather than a wider first one. Every assertion the original
 * made is still made, and the post-mutation re-assertion still runs after the stripping.
 */
export function ensureCompanionServiceIdentity(
	kind: string,
	runtimeUser: string,
): CompanionServiceIdentity {
	const names = companionServiceNames(kind);
	const pinnedUid = resolveCompanionServiceUid(kind);
	// With a pinned uid the gid is pinned to the same number. That is the sysusers convention and it
	// is what makes `RESERVED_COMPANION_UID_BLOCK` mean anything: a block that reserves uids while
	// leaving gids to the host cannot promise two companions do not share a group. Under the
	// `preallocated` exemption both are left to the host, exactly as today.
	let gid = lookupGroup(names.group);
	if (gid === null) {
		commandOutput("groupadd", [
			"--system",
			// `== null` rather than `=== null`, so an accidental `undefined` can never mean "pinned".
			// The sentinel gates ARGV to a privileged command: a non-total check spells a lost pin as
			// `--gid undefined`, which fails as an opaque `groupadd` error rather than as the refusal
			// the caller was owed.
			...(pinnedUid == null ? [] : ["--gid", String(pinnedUid)]),
			names.group,
		]);
		gid = lookupGroup(names.group);
	}
	if (gid === null) throw new Error(`Companion ${kind} service group was not created`);

	let user = lookupUser(names.user);
	if (user === null) {
		commandOutput("useradd", [
			"--system",
			"--gid",
			names.group,
			// `== null`, for the same reason as the `groupadd` gate above.
			...(pinnedUid == null ? [] : ["--uid", String(pinnedUid)]),
			"--home-dir",
			"/nonexistent",
			"--no-create-home",
			"--shell",
			"/usr/sbin/nologin",
			names.user,
		]);
		user = lookupUser(names.user);
	}
	if (user === null) throw new Error(`Companion ${kind} service user was not created`);
	const runtimeUid = runtimeUserUid(runtimeUser);
	const runtimeGid = runtimeUserGid(runtimeUser);
	assertCompanionServiceAccount({
		names,
		account: user,
		gid,
		runtimeUid,
		runtimeGid,
		expectedUid: pinnedUid,
	});
	if (groupIdsForUser(runtimeUser).includes(gid)) {
		removeSupplementaryGroup(runtimeUser, gid);
	}
	for (const supplementaryGid of groupIdsForUser(names.user)) {
		if (supplementaryGid !== gid) {
			removeSupplementaryGroup(names.user, supplementaryGid);
		}
	}
	// POST-MUTATION RE-ASSERTION. Re-reads both group lists from the host after the stripping
	// instead of trusting that `gpasswd --delete` did what it was asked.
	assertCompanionServiceGroups({
		names,
		runtimeGids: groupIdsForUser(runtimeUser),
		serviceGids: groupIdsForUser(names.user),
		gid,
	});
	return { uid: user.uid, gid };
}

/**
 * What the host already holds for one companion kind's account and group. Read-only.
 */
export interface ObservedCompanionServiceIdentity {
	/** The uid of the kind's account, or `null` when the account does not exist. */
	uid: number | null;
	/** Every gid the kind holds: its named group's and its account's primary group. */
	gids: readonly number[];
}

/**
 * Read one kind's account and group from the host WITHOUT creating either, or `null` when neither
 * exists. The observation half of {@link ensureExclusiveCompanionServiceIdentity}: it must never
 * allocate, because D-13 companion uids are never reclaimed, so observing a kind the manifest did
 * not declare must not burn its uid.
 */
export function observeCompanionServiceIdentity(
	kind: string,
): ObservedCompanionServiceIdentity | null {
	const names = companionServiceNames(kind);
	const user = lookupUser(names.user);
	const groupGid = lookupGroup(names.group);
	if (user === null && groupGid === null) return null;
	const gids = new Set<number>();
	if (groupGid !== null) gids.add(groupGid);
	if (user !== null) gids.add(user.gid);
	return { uid: user?.uid ?? null, gids: [...gids] };
}

/**
 * Allocate one companion kind's identity and refuse it when another registry kind already holds its
 * uid or its gid on this host. This is the production allocation path (`ContainerHost.ensureIdentity`).
 *
 * Why it exists (COMP-05, `04-VERIFICATION.md`). The per-account assertions compare an account only
 * with its OWN group, so nothing else is cross-kind. 04-08 put the cross-kind refusal on the converge
 * path through the multi-kind Files allocation; upstream #939 (sync step 1) took Files off per-kind
 * accounts, and container companions allocate one kind per call, which left the refusal running only
 * under test. Checking the allocated identity against every other registry kind's EXISTING account is
 * the same refusal on the path production actually takes: two kinds that resolved to one uid, or to
 * one gid (an `/etc/group` that disagrees with the registry, e.g. two group names pre-created at one
 * gid), are not an isolation boundary.
 *
 * uids and gids are separate ledgers: a gid equal to another kind's uid is the sysusers convention,
 * not a collision. Other kinds are observed in sorted key order so the refusal names the same holder
 * on every host. `ensure` and `observe` are REQUIRED with no default, for the reason
 * `CompanionServiceIsolation`'s `kinds` is required: an omitted refusal must not typecheck.
 */
export function ensureExclusiveCompanionServiceIdentity(input: {
	kind: string;
	runtimeUser: string;
	ensure: (kind: string, runtimeUser: string) => CompanionServiceIdentity;
	observe: (kind: string) => ObservedCompanionServiceIdentity | null;
	registryKinds?: readonly string[];
}): CompanionServiceIdentity {
	const identity = input.ensure(input.kind, input.runtimeUser);
	const others = [...new Set(input.registryKinds ?? Object.keys(COMPANION_REGISTRY))]
		.filter((other) => other !== input.kind)
		.sort();
	for (const other of others) {
		const held = input.observe(other);
		if (held === null) continue;
		const collision =
			held.uid === identity.uid
				? (["uid", identity.uid] as const)
				: held.gids.includes(identity.gid)
					? (["gid", identity.gid] as const)
					: null;
		if (collision !== null) {
			throw new Error(
				`Companion ${input.kind} resolved to ${collision[0]} ${collision[1]}, already held by companion ${other} — a shared identity is not an isolation boundary`,
			);
		}
	}
	return identity;
}

/**
 * The Files identity: the tenant runtime user that owns the home Files serves (upstream #939,
 * 64916c0a). Files runs as that user, so it needs no service account and no workspace ACL; the
 * checks below refuse a root runtime user and a source root the runtime user does not own.
 */
export function ensureFileBrowserServiceIsolation(
	paths: RuntimePaths,
	sourceRoot: string,
): FileBrowserServiceIdentity {
	if (sourceRoot !== paths.userHome || /\s/.test(sourceRoot)) {
		throw new Error("Files source root must be the canonical tenant home path");
	}
	const runtimeUser = process.env.CLAWDI_RUNTIME_USER?.trim() || HOSTED_RUNTIME_USER;
	if (runtimeUser === "root" || runtimeUser === "0") {
		throw new Error("Files requires a non-root tenant runtime user");
	}
	const identity = {
		uid: runtimeUserUid(runtimeUser),
		gid: runtimeUserGid(runtimeUser),
	};
	const root = lstatSync(sourceRoot);
	if (!root.isDirectory() || root.isSymbolicLink() || root.uid !== identity.uid) {
		throw new Error("Files source root must be owned by the tenant runtime user");
	}
	return identity;
}
