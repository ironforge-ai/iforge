/**
 * The companion registry, TypeScript half.
 *
 * A companion is a server-owned program that runs beside the hosted runtime under its own
 * service identity. This module is the authority every consumer in the CLI consults to answer
 * "is this key a companion kind this build knows about?" -- `manifest-contract.ts`'s companions
 * envelope reads `KNOWN_COMPANION_KINDS` rather than carrying its own literal, so a kind is added
 * in one place.
 *
 * The entries below are written out verbatim rather than read from
 * `config/companion-registry.txt` at runtime, for the same reason `packages/cli/src/lib/brand-env.ts`
 * embeds its map: this module is bundled into a native binary that has no repository next to it,
 * so an install-time read of `config/` would resolve to nothing on a provisioned host.
 * `companion-registry.test.ts` asserts this embed equals the contract file column for column, and
 * `backend/tests/test_companion_registry.py` asserts the Python embed equals both -- so the three
 * representations cannot drift.
 *
 * See `config/companion-registry.txt` for the grammar, the membership rule, the append-only/never-
 * reclaim rule (D-13) and the per-column exclusions. Do not add a kind here without adding its row
 * there; the parity test exists to make that impossible to forget.
 */

export interface CompanionRegistryEntry {
	/**
	 * How the kind's digest pin is located, CLOSED to exactly two values: `binary-sha256` for a
	 * native binary, `oci-digest` for a container image located by its OCI content digest (the
	 * Quadlet seam Phase 4 D-02 left open; 06-03 uses it for `postiz`). No tag-shaped value exists:
	 * D-01 is fail closed, never fetch a floating reference.
	 */
	artifactKind: "binary-sha256" | "oci-digest";
	/** The lowest CLI semver that can parse this kind; consumed by the backend's emit gate (D-08). */
	minCliVersion: string;
	/** A decimal UID from the reserved block, or `preallocated` for a host-allocated account. */
	uid: "preallocated" | number;
	/** The single TCP port the kind listens on. Unique across the registry. */
	port: number;
	/** Whether the kind's UID participates in the transparent egress redirect. */
	egress: "redirect" | "bypass";
}

/**
 * The bound every companion key must satisfy, at the parse boundary rather than downstream.
 *
 * The key becomes a systemd unit name, a UID lookup name, a `StateDirectory=` value and a path
 * component, and it arrives from the provisioning image -- outside this repository -- so it is
 * untrusted input (T-4-01). `sysusers.d(5)` caps user names at 31 characters and forbids a leading
 * digit or hyphen; this is that cap. The same expression is mirrored in
 * `backend/app/core/companion_registry.py`.
 */
export const COMPANION_KEY_PATTERN = /^[a-z][a-z0-9-]{0,30}$/;

/**
 * The UID range companion service accounts are allocated from. See `config/companion-registry.txt`
 * -- the first numeric allocation from it is `postiz` = 10101 (06-03), and its confirmation against
 * the provisioning image's `/etc/passwd` was explicitly DEFERRED to host UAT at checkpoint CK-B.
 * `postiz` is reconcilable since 06-08 (see `COMPANION_RECONCILERS`), so the first converge that is
 * emitted a postiz entry allocates it. That converge fails closed rather than sharing: `useradd`
 * refuses a uid another account already holds, `assertCompanionServiceAccount` refuses a mismatched
 * pin, and the container kind refuses an overlapping subordinate range by the owning account's name.
 */
export const RESERVED_COMPANION_UID_BLOCK = { first: 10100, last: 10199 } as const;

/**
 * The most unrecognised companion kinds one observation may report, in ONE place.
 *
 * Mirrors `max_length=30` on `HostedRuntimeObservedV2.unsupported_companions` in
 * `backend/app/schemas/runtime_observed.py`. Both the persisted applied-state key set
 * (`applied-state.ts`) and the observation emitter (`observed.ts`) bound themselves by this, so a
 * set that survives to disk can never exceed what the wire will accept.
 *
 * The CLI TRUNCATES to this bound rather than emitting a longer array. `_StrictObservedWireModel`
 * is `extra="forbid"` and the bound is enforced server-side, so an over-long array is rejected
 * WHOLE -- losing the entire convergence report, not just the diagnostic. A truncated diagnostic
 * is strictly less bad than no observation (T-4-20).
 */
export const UNSUPPORTED_COMPANIONS_MAX = 30;

/**
 * Every companion kind this build recognises, sorted, as a literal tuple so callers can narrow
 * against it rather than against `string`.
 */
export const KNOWN_COMPANION_KINDS = ["filebrowser", "postiz"] as const;

export type KnownCompanionKind = (typeof KNOWN_COMPANION_KINDS)[number];

/**
 * The registry key of the one companion this build ships, as a REGISTRY-TYPED export.
 *
 * Promoted here from a module-local `const FILE_BROWSER_COMPANION_KIND = "filebrowser"` in
 * `runtime-systemd-reconciliation.ts`, which the unit writer read as an unchecked `string`. The
 * annotation is `KnownCompanionKind` rather than `string` (and rather than `as const` alone) on
 * purpose: a registry that stopped carrying the key then fails `tsc --noEmit` here instead of
 * drifting silently into a unit that names an account no registry row owns.
 */
export const FILE_BROWSER_COMPANION_KIND: KnownCompanionKind = "filebrowser";

/**
 * The declared companion KEYS this build recognises: the intersection of `keys` with
 * `KNOWN_COMPANION_KINDS`, deduplicated and sorted.
 *
 * This answers "does a registry row exist", which is strictly WEAKER than "can this build run this
 * companion" — so it is NOT the allocation gate. Use `reconcilableCompanionKinds` for that; see the
 * burned-UID hazard recorded there. This function is the right answer for the observation's
 * supported/unsupported partition, which mirrors the backend's registry-keyed emit gate
 * (`backend/app/services/runtime_source.py`).
 *
 * The set is applied to the keys the MANIFEST declares — never to the registry alone. Under D-13
 * companion UIDs are append-only and never reclaimed, so an account created for a kind that is not
 * deployed on this host permanently burns a UID that can then never serve the companion it was
 * reserved for. D-14 is the other half: a key the registry does not recognise is skipped, not
 * provisioned.
 *
 * KEYS ONLY, and structurally so. The parameter is `Iterable<string>` rather than the companions
 * record because the `filebrowser` manifest entry embeds a live HS256 secret at `auth.secret`
 * (T-4-07) — this function is unable to read an entry value, which is a stronger statement than a
 * convention that it does not. Same discipline as `collectUnsupportedCompanions` in `observed.ts`.
 *
 * Sorted in and sorted out so two converges over one manifest allocate identically, regardless of
 * the order the manifest happened to serialize its companion keys in.
 */
export function recognisedCompanionKinds(keys: Iterable<string>): KnownCompanionKind[] {
	return [...new Set(keys)].filter(isKnownCompanionKind).sort();
}

/**
 * The runtime membership test, as a TYPE GUARD so the intersection narrows instead of merely
 * filtering.
 *
 * `Set.prototype.has` is what makes this prototype-safe — a plain-object membership map would
 * re-admit `constructor`, the one `Object.prototype` member `COMPANION_KEY_PATTERN` accepts and the
 * manifest parser preserves (see the `Object.setPrototypeOf` note under the registry below).
 * Module-private and typed `ReadonlySet`, so no consumer can add a kind to the recognised set
 * without adding a registry row.
 */
const KNOWN_COMPANION_KIND_SET: ReadonlySet<string> = new Set<string>(KNOWN_COMPANION_KINDS);

function isKnownCompanionKind(key: string): key is KnownCompanionKind {
	return KNOWN_COMPANION_KIND_SET.has(key);
}

/**
 * The kinds THIS BUILD can actually converge — a registry row alone must never provision an account.
 *
 * `KNOWN_COMPANION_KINDS` is derived from the registry, but the reconciler surface is
 * filebrowser-only: `writeFileBrowserSystemdUnit`, `fileBrowserCompanionProgram` and
 * `fileBrowserCompanionMutationPlan` all key on `manifest.companions.filebrowser` literally. Gating
 * allocation on registry membership therefore meant the commit that adds the SECOND registry row —
 * with no unit writer yet, which is how this codebase has been staging the work — would make every
 * converge whose manifest declares that kind run `groupadd`/`useradd` for an account nothing will
 * ever start. Under D-13 those UIDs are append-only and never reclaimed, which is the precise harm
 * `recognisedCompanionKinds` says it exists to prevent.
 *
 * `Partial<Record<…>>` and not `Record<…>`: an exhaustive table would make ADDING a registry row a
 * `tsc --noEmit` failure until its key were listed here, and the only way to clear that failure
 * before a reconciler exists would be to write `true` — which is the provisioning this gate exists
 * to withhold. Partial makes a new row INERT by default, which is the property wanted, while a typo'd
 * key is still a typecheck failure.
 *
 * A `true` is written here in the SAME commit as its reconciler, never before. A `false` may be
 * written earlier, and is: it records a kind that is recognised on purpose and withheld on purpose,
 * and it is inert exactly as an absent key is — `reconcilableCompanionKinds` admits `true` only.
 */
const COMPANION_RECONCILERS: Readonly<Partial<Record<KnownCompanionKind, boolean>>> = {
	filebrowser: true,
	// Reconcilable since plan 06-08, in the same change as its reconciler: the container companion
	// kind (`container-companion.ts`, wired through `reconcileContainerCompanions`) renders, starts
	// and withdraws it, so allocating uid 10101 now provisions an account something actually runs.
	// It was an explicit `false` from 06-03 until then, because under D-13 a UID allocated for a
	// companion nothing starts is burned forever. Read with `Object.hasOwn` AND the value -- see
	// `reconcilableCompanionKinds`.
	postiz: true,
};

/**
 * The allocation gate: declared ∩ recognised ∩ reconcilable, deduplicated and sorted.
 *
 * This is what a privileged allocation path must derive its kind set from. It narrows
 * `recognisedCompanionKinds` by the reconciler table above, so a registry row with no reconciler
 * burns no UID.
 *
 * `reconcilers` is injectable for the same reason `resolveCompanionServiceUid`'s `registry` is: the
 * "a registry row alone is inert" property is otherwise unprovable while exactly one row ships and
 * that row does have a reconciler. Read with `Object.hasOwn`, never a bare index read — the key may
 * be `constructor`.
 *
 * Does NOT change what the observation reports. The supported/unsupported partition stays keyed on
 * registry membership so it continues to mirror the backend's emit gate; a kind that is recognised
 * but not yet reconcilable is simply not provisioned.
 */
export function reconcilableCompanionKinds(
	keys: Iterable<string>,
	reconcilers: Readonly<Partial<Record<string, boolean>>> = COMPANION_RECONCILERS,
): KnownCompanionKind[] {
	// OWN property AND a literal `true`: `postiz: false` is present but withheld, so membership
	// alone would re-admit exactly the kind the table exists to hold back.
	return recognisedCompanionKinds(keys).filter(
		(kind) => Object.hasOwn(reconcilers, kind) && reconcilers[kind] === true,
	);
}

export const COMPANION_REGISTRY: Record<string, CompanionRegistryEntry> = {
	filebrowser: {
		artifactKind: "binary-sha256",
		minCliVersion: "0.0.0",
		uid: "preallocated",
		port: 9120,
		egress: "bypass",
	},
	// 06-03 (D-01): the first container kind, the first gated kind and the first numeric UID. Each is
	// one-way under D-13; the exclusions for its egress and minimum are in the contract file.
	postiz: {
		artifactKind: "oci-digest",
		minCliVersion: "0.14.0",
		uid: 10101,
		port: 9130,
		egress: "bypass",
	},
};

// NO PROTOTYPE CHAIN. `COMPANION_KEY_PATTERN` accepts exactly one `Object.prototype` member --
// `constructor` -- so on a plain object literal `COMPANION_REGISTRY["constructor"]` resolves to the
// inherited `Object` function instead of `undefined`, and every consumer that reads a row and tests
// the result against `undefined` silently accepts a kind no row owns (`resolveCompanionServiceUid`
// then returned `undefined` rather than throwing, which made the registry-pinned-UID assertion
// downstream a no-op). The manifest parser ACCEPTS that key: `manifest-contract.ts` validates a
// companion key against this same pattern and its catchall preserves it, so the guard cannot be
// left to the intersection filter in another module.
//
// Stripped here rather than by writing the literal through `Object.assign(Object.create(null), ...)`
// because `backend/tests/test_companion_registry.py` pins the two languages to each other by
// SOURCE READ of this declaration -- it requires the rows at one tab under `export const
// COMPANION_REGISTRY` and a `\n};` terminator. The lookups are hardened independently
// (`Object.hasOwn` in `file-browser-isolation.ts`), because `resolveCompanionServiceUid` takes a
// caller-supplied registry that this line cannot reach. The Python mirror needs no equivalent:
// `dict.get` has no prototype chain to fall through to.
//
// Python mirror: `backend/app/core/companion_registry.py`.
Object.setPrototypeOf(COMPANION_REGISTRY, null);

/**
 * The classification vocabulary a verb row declares (D-05).
 *
 * This is the SERVER-SIDE gate's input. Scope mapping (phase 06, D-09): every class requires
 * `companions:invoke` to call, and `tools/list` requires `companions:read`. The class-specific
 * protection sits after the scope check -- the policy default for the class and, for `destroy`,
 * the tenant's enablement row as well (D-06). The MCP annotations `deriveAnnotations` produces
 * from it are advisory client-side UX and are NEVER the gate.
 */
export type CompanionToolClass = "read" | "create" | "mutate" | "destroy";

/** The HTTP methods a verb row may declare. */
export type CompanionVerbMethod = "GET" | "POST" | "PATCH" | "PUT" | "DELETE";

/** The unit a verb's blast-radius cap is counted in (TOOL-04). */
export type CompanionCapUnit = "calls" | "records" | "bytes";

/**
 * Where a verb's arguments travel (05-13, WR-06b).
 *
 * `query` puts each argument in the request's query string; `body` sends them as one JSON object
 * with the JSON content-type header and an empty query string. Closed, and with NO DEFAULT —
 * placement used to be derived from `call.method` inside `companion-executor.ts`, so three verbs
 * sent their target path somewhere their sibling verbs did not and no reader could see the
 * decision in a diff. A default would reintroduce exactly that invisible rule.
 *
 * Python mirror: `_VERB_ARGS_IN` in `backend/app/core/companion_registry.py`.
 */
export type CompanionVerbArgsIn = "query" | "body";

/**
 * The assembled tool name (D-04): a bounded kind key, `__`, a bounded verb.
 *
 * The separator cannot appear in a kind key — `COMPANION_KEY_PATTERN` forbids `_` outright — so
 * the split back into `(kind, verb)` is unambiguous in both directions. Mirrored character for
 * character by `COMPANION_TOOL_NAME_PATTERN` in `backend/app/core/companion_registry.py`.
 */
export const COMPANION_TOOL_NAME_PATTERN = /^[a-z][a-z0-9-]{0,30}__[a-z][a-z0-9_]{0,60}$/;

export interface CompanionVerbEntry {
	/** `read` | `create` | `mutate` | `destroy` — the server-side gate's input (D-05). */
	toolClass: CompanionToolClass;
	/** The HTTP method the runtime-side executor issues against the companion. */
	method: CompanionVerbMethod;
	/** The companion-relative path. Never carries a scheme, host or port — those come from the registry row. */
	pathTemplate: string;
	/** The verb of the same kind that previews this one's effect, or `null` for none (D-13). */
	readProbe: string | null;
	/** The unit this verb's blast-radius cap is counted in (TOOL-04). */
	capUnit: CompanionCapUnit;
	/** Where this verb's arguments travel — declared per row, never derived from `method`. */
	argsIn: CompanionVerbArgsIn;
}

/**
 * The MCP annotation triple, derived MECHANICALLY from a tool class (D-07).
 *
 * Pure and total over the class vocabulary. Never hand-authored per verb: the whole point of
 * deriving is that the class vocabulary and the annotation vocabulary have exactly one join, so
 * they cannot drift. The spec's own default for an unannotated tool is "destructive,
 * non-idempotent, open-world", which is already fail-safe — emitting nothing would be safe;
 * emitting something WRONG would not be, which is why this is a lookup and not a heuristic.
 *
 * Python mirror: `derive_annotations` in `backend/app/core/companion_registry.py`, asserted to
 * produce the same triples by `backend/tests/test_companion_registry.py`.
 */
export function deriveAnnotations(toolClass: CompanionToolClass): {
	readOnlyHint: boolean;
	destructiveHint: boolean;
	idempotentHint: boolean;
} {
	return {
		readOnlyHint: toolClass === "read",
		destructiveHint: toolClass === "mutate" || toolClass === "destroy",
		idempotentHint: toolClass === "read",
	};
}

/**
 * The curated toolsets (TOOL-08), one entry per INTEGRATE capability in each kind's COVERAGE
 * matrix (`filebrowser`: Phase 5, `postiz`: Phase 6), in the contract file's ascending
 * `(kind, verb)` order.
 *
 * `delete_path` is `destroy` class. Its presence here is the CLASSIFICATION, never the
 * permission: D-06 keeps a `destroy` verb out of `tools/list` until the tenant enables it, and
 * refuses it by name at call time even when an agent names it from memory.
 *
 * Python mirror: `COMPANION_VERBS` in `backend/app/core/companion_registry.py`, asserted equal in
 * both directions by `backend/tests/test_companion_registry.py`.
 */
export const COMPANION_VERBS: Record<string, CompanionVerbEntry> = {
	filebrowser__create_dir: {
		toolClass: "create",
		method: "POST",
		pathTemplate: "/api/resources",
		readProbe: "list_dir",
		capUnit: "calls",
		argsIn: "query",
	},
	filebrowser__delete_path: {
		toolClass: "destroy",
		method: "DELETE",
		pathTemplate: "/api/resources",
		readProbe: "list_dir",
		capUnit: "records",
		argsIn: "query",
	},
	filebrowser__list_dir: {
		toolClass: "read",
		method: "GET",
		pathTemplate: "/api/resources",
		readProbe: null,
		capUnit: "calls",
		argsIn: "query",
	},
	filebrowser__move_path: {
		toolClass: "mutate",
		method: "PATCH",
		pathTemplate: "/api/resources",
		readProbe: "list_dir",
		capUnit: "records",
		argsIn: "query",
	},
	filebrowser__read_file: {
		toolClass: "read",
		method: "GET",
		pathTemplate: "/api/raw",
		readProbe: null,
		capUnit: "bytes",
		argsIn: "query",
	},
	filebrowser__write_file: {
		toolClass: "create",
		method: "POST",
		pathTemplate: "/api/resources",
		readProbe: "list_dir",
		capUnit: "bytes",
		argsIn: "query",
	},
	// The curated `postiz` toolset (06-06): one entry per INTEGRATE capability in the Phase 6
	// COVERAGE matrix, decided against the Postiz v2.24.0 public controller. No postiz verb is
	// `destroy` class; every excluded capability is listed, with its reason, in the contract file.
	postiz__create_post: {
		toolClass: "create",
		method: "POST",
		pathTemplate: "/api/public/v1/posts",
		readProbe: "list_posts",
		capUnit: "records",
		argsIn: "body",
	},
	postiz__is_connected: {
		toolClass: "read",
		method: "GET",
		pathTemplate: "/api/public/v1/is-connected",
		readProbe: null,
		capUnit: "calls",
		argsIn: "query",
	},
	postiz__list_groups: {
		toolClass: "read",
		method: "GET",
		pathTemplate: "/api/public/v1/groups",
		readProbe: null,
		capUnit: "calls",
		argsIn: "query",
	},
	postiz__list_integrations: {
		toolClass: "read",
		method: "GET",
		pathTemplate: "/api/public/v1/integrations",
		readProbe: null,
		capUnit: "calls",
		argsIn: "query",
	},
	postiz__list_notifications: {
		toolClass: "read",
		method: "GET",
		pathTemplate: "/api/public/v1/notifications",
		readProbe: null,
		capUnit: "calls",
		argsIn: "query",
	},
	postiz__list_posts: {
		toolClass: "read",
		method: "GET",
		pathTemplate: "/api/public/v1/posts",
		readProbe: null,
		capUnit: "calls",
		argsIn: "query",
	},
};

// NO PROTOTYPE CHAIN, for the same reason the kind registry has none: a bare index read of
// `constructor` on a plain object literal resolves the inherited `Object` function instead of
// `undefined`, so every consumer that reads a row and tests the result against `undefined`
// silently accepts a verb no row owns. Stripped here rather than written through
// `Object.assign(Object.create(null), …)` because `backend/tests/test_companion_registry.py` pins
// the two languages to each other by SOURCE READ of this declaration — it requires the rows at
// one tab under `export const COMPANION_VERBS` and a `\n};` terminator, so the shape above is
// load-bearing and not merely a formatting choice.
//
// Python mirror: `COMPANION_VERBS` in `backend/app/core/companion_registry.py`.
Object.setPrototypeOf(COMPANION_VERBS, null);
