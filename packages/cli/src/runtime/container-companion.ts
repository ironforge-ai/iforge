/**
 * The container companion kind: a manifest entry becomes rootful Quadlet units running upstream
 * images pinned by digest (plan 06-08).
 *
 * Shape, mirroring `file-browser-companion.ts`: a PURE plan over the manifest entry and the facts
 * read from the host, then an APPLY layer over one injectable `ContainerHost` seam. The seam is the
 * whole host surface this module touches (command runner, file reader/writer, stat, directory and
 * identity creation, clock), so the unprivileged suite drives every branch against an in-memory
 * host and never reaches podman, systemd or `/etc`.
 *
 * Decisions this module encodes, each recorded elsewhere and restated here only as far as the code
 * depends on it:
 *
 * - CK-B `rootful-static-map` (06-03-SUMMARY.md): rootful Quadlet system units, a host account at
 *   the registry UID, and a static user namespace `UIDMap=0:<base>:65536` with
 *   `base = 1_000_000_000 + (uid - 10100) * 65536`. Container root is therefore an unprivileged
 *   host UID, and state-directory ownership is identical across restart, redeploy and instance
 *   replacement because the map never moves (OPS-01, D-13).
 * - CK-A `own-redis-shared-temporal` (06-05-SUMMARY.md): PostgreSQL AND Redis run per installation;
 *   Temporal is connection material inside the app's credential, not a unit here.
 * - D-03: the upstream image runs UNMODIFIED. Configuration is environment, command-line flags
 *   rendered by Quadlet, and network only; nothing here builds, layers or patches an image.
 * - D-27 / Phase 4 D-11: credentials reach the containers only through `LoadCredentialEncrypted=`
 *   plus `EnvironmentFile=%d/<id>` (rendered by Quadlet as `--env-file`). There is NO plaintext
 *   fallback: this module never renders the plain `LoadCredential=` form.
 * - COMP-06: every image is pulled by digest, verified against its RepoDigests, and referenced with
 *   `Pull=never`, so what runs is exactly what the manifest priced.
 * - D-24 (plan 06-14): when the entry names the edge tunnel -- `dependencies.cloudflared` AND
 *   `credentials.tunnelTokenSecretRef`, both or neither -- a `cloudflared` connector runs as one more
 *   digest-pinned container on the installation's own network, reaching the app by its in-network
 *   name. It dials OUT to the edge, so it publishes no port; its token arrives only as its own
 *   encrypted credential. Whether the hostname routes to it is decided by the control plane, which
 *   publishes the ingress only after this runtime's ready report.
 * - CR-02 (plan 06-39): a per-kind nftables table, loaded before any container of the kind starts,
 *   refuses every new connection from a non-root local socket to the loopback port and to the
 *   installation network's pinned subnet. See "The loopback and network guard" below.
 */
import { spawn, spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { once } from "node:events";
import {
	chmodSync,
	chownSync,
	createReadStream,
	createWriteStream,
	lstatSync,
	mkdirSync,
	readFileSync,
	realpathSync,
	rmSync,
	statSync,
} from "node:fs";
import { dirname, isAbsolute, join, normalize } from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { createGzip } from "node:zlib";
import { writePrivateFileAtomic } from "../lib/private-file";
import { readRuntimeCredential } from "./auth-token";
import {
	type CompanionBackupMarker,
	type CompanionBackupResult,
	type CompanionFetch,
	type CompanionHostConditionCode,
	type CompanionInstallOperation,
	type CompanionInstallReport,
	CompanionProgressReporter,
	type CompanionReadyAssertion,
	CompanionReportRejectedError,
	type CompanionReportResult,
	type CompanionReportSink,
	installationSecretValues,
	redactKnownSecrets,
	secretForms,
} from "./companion-progress";
import { RESERVED_COMPANION_UID_BLOCK } from "./companion-registry";
import {
	companionServiceNames,
	ensureCompanionServiceIdentity,
	ensureExclusiveCompanionServiceIdentity,
	observeCompanionServiceIdentity,
	resolveCompanionServiceUid,
} from "./file-browser-isolation";
import { HOSTED_RUNTIME_USER } from "./hosted-runtime-contract";
import {
	assertOfferedIsolationTier,
	POSTIZ_APP_IMAGE,
	POSTIZ_CLOUDFLARED_IMAGE,
	POSTIZ_POSTGRES_IMAGE,
	POSTIZ_REDIS_IMAGE,
	type PostizCompanionManifest,
	type RuntimeManifest,
} from "./manifest-contract";
import type { RuntimePaths } from "./paths";
import { runningAsRoot } from "./runtime-user-command";
import { runtimeSecretValue } from "./secret-values";

// ---------------------------------------------------------------------------------------------
// The host seam
// ---------------------------------------------------------------------------------------------

export interface ContainerHostCommandResult {
	status: number;
	stdout: string;
	stderr: string;
}

export interface ContainerHostStat {
	isFile: boolean;
	isDirectory: boolean;
	mode: number;
	uid: number;
	gid: number;
}

/**
 * Every host operation the container kind performs. Nothing in this module reaches the machine
 * except through one of these methods.
 */
export interface ContainerHost {
	/**
	 * Run a command without a shell. Never throws for a non-zero exit; the caller decides.
	 *
	 * `input` is written to the command's standard input. It is the ONLY channel a secret may
	 * reach a command through: an argv array is visible to every local user in `/proc`.
	 */
	run(
		command: string,
		args: readonly string[],
		options?: { input?: string },
	): ContainerHostCommandResult;
	/** A regular file's content, or `null` when it does not exist. */
	readFile(path: string): string | null;
	/** Atomically replace a file with this content, mode and owner (root when omitted). */
	writeFile(
		path: string,
		content: string,
		options: { mode: number; uid?: number; gid?: number },
	): void;
	/** Remove a file; absent is not an error. */
	removeFile(path: string): void;
	/**
	 * The canonical absolute path with every symlink resolved, or `null` when nothing exists there.
	 * The explicit `remove` lifecycle (plan 06-20) deletes only a data directory that resolves to
	 * exactly itself.
	 */
	realPath(path: string): string | null;
	/**
	 * Delete a directory tree; absent is not an error. Never follows a symlink: a link is removed
	 * as a link. Called ONLY by `removeContainerCompanion`, after its path guard.
	 */
	removeTree(path: string): void;
	stat(path: string): ContainerHostStat | null;
	/** Create the directory when absent, then assert it is a real directory with this owner/mode. */
	ensureDirectory(path: string, options: { mode: number; uid: number; gid: number }): void;
	/** Allocate (or adopt) the kind's host account through the hardened allocator. */
	ensureIdentity(kind: string): { uid: number; gid: number };
	now(): Date;
}

// ---------------------------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------------------------

/** The one container kind this build converges. */
export const CONTAINER_COMPANION_KIND = "postiz";
export type ContainerCompanionKind = typeof CONTAINER_COMPANION_KIND;

/** Width of every container kind's subordinate range and of its user-namespace map (CK-B). */
export const CONTAINER_SUBORDINATE_RANGE_WIDTH = 65536;
const CONTAINER_SUBORDINATE_RANGE_ORIGIN = 1_000_000_000;

export const QUADLET_GENERATOR_PATH = "/usr/libexec/podman/quadlet";
export const SYSTEMD_CREDENTIAL_SECRET_PATH = "/var/lib/systemd/credential.secret";
/** The nftables binary the converge and every container unit's `ExecStartPre=` load the guard with. */
export const NFT_BINARY_PATH = "/usr/sbin/nft";
/** The oldest systemd whose `systemd-creds` and `LoadCredentialEncrypted=` this module relies on. */
export const MINIMUM_CONTAINER_SYSTEMD_VERSION = 250;

/** Postiz's in-container nginx port; only ever published on loopback. */
const POSTIZ_CONTAINER_PORT = 5000;
/**
 * Pins the image's baked pnpm dlx cache so the start-time `prisma db push` never resolves against
 * the npm registry (research Pitfall 1). Not a secret, so it is an ordinary `Environment=` line.
 */
const POSTIZ_NPM_DLX_CACHE_MAX_AGE = "5256000";

const CONTAINER_RECEIPT_SCHEMA = "iforge.containerCompanionApplied.v1";

/**
 * First line of every Quadlet source this module writes. Withdrawal removes only files that carry
 * it, so a hand-placed unit that happens to share a name is never deleted.
 */
export const GENERATED_CONTAINER_SOURCE_HEADER =
	"# Generated by iforge runtime converge (container companion). Do not edit.";

/** First line of the guard's ruleset (an nft comment). */
const GENERATED_CONTAINER_GUARD_HEADER =
	"# Generated by iforge runtime converge (container companion guard). Do not edit.";

const SOURCE_FILE_MODE = 0o644;
const PRIVATE_FILE_MODE = 0o600;

// ---------------------------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------------------------

export interface ContainerCompanionPaths {
	/** Where Quadlet reads rootful system units from (`/etc/containers/systemd`). */
	quadletRoot: string;
	/** Root-only per-companion state (`/var/lib/iforge/companions/postiz`). */
	companionStateRoot: string;
	/** Encrypted credentials (`…/companions/postiz/credentials`). */
	credentialsRoot: string;
	/** The applied receipt (`…/companions/postiz/applied.json`). */
	receiptPath: string;
	/**
	 * The loopback and network guard's ruleset (`…/companions/postiz/loopback-guard.nft`), loaded
	 * with `nft -f` by the converge and by every container unit before it starts (plan 06-39).
	 */
	guardPath: string;
	/**
	 * The companion's durable data (`/var/lib/iforge-postiz`). Removed ONLY by the explicit
	 * `remove` lifecycle (`removeContainerCompanion`, plan 06-20), never by a withdrawal.
	 */
	dataRoot: string;
	/** Transient plaintext, on the run root's tmpfs, removed right after encryption. */
	plaintextRoot: string;
	subuidPath: string;
	subgidPath: string;
}

/**
 * Derive the container kind's paths from the runtime's platform roots.
 *
 * On a provisioned host this yields the hosted layout above. Under a sandboxed state root (tests,
 * worktrees) the `/etc` half follows the configuration root's parent, so nothing outside the
 * sandbox is ever addressed.
 */
export function containerCompanionPaths(
	paths: Pick<RuntimePaths, "serviceStateRoot" | "configurationRoot" | "runRoot">,
	kind: ContainerCompanionKind = CONTAINER_COMPANION_KIND,
): ContainerCompanionPaths {
	const names = companionServiceNames(kind);
	const etcRoot = dirname(paths.configurationRoot);
	const companionStateRoot = join(paths.serviceStateRoot, "companions", kind);
	return {
		quadletRoot: join(etcRoot, "containers", "systemd"),
		companionStateRoot,
		credentialsRoot: join(companionStateRoot, "credentials"),
		receiptPath: join(companionStateRoot, "applied.json"),
		guardPath: join(companionStateRoot, "loopback-guard.nft"),
		dataRoot: join(dirname(paths.serviceStateRoot), names.stateDirectory),
		plaintextRoot: join(paths.runRoot, "companions", kind),
		subuidPath: join(etcRoot, "subuid"),
		subgidPath: join(etcRoot, "subgid"),
	};
}

// ---------------------------------------------------------------------------------------------
// Names
// ---------------------------------------------------------------------------------------------

export interface ContainerCompanionUnitNames {
	/** The account, and the stem every unit name derives from (`iforge-postiz`). */
	stem: string;
	networkSource: string;
	databaseSource: string;
	redisSource: string;
	appSource: string;
	/** The edge tunnel's connector (plan 06-14), rendered only when the entry names the tunnel. */
	tunnelSource: string;
	networkService: string;
	databaseService: string;
	redisService: string;
	appService: string;
	tunnelService: string;
	databaseContainer: string;
	redisContainer: string;
	appContainer: string;
	tunnelContainer: string;
}

/**
 * Every unit and container name, derived from ONE registry key through `companionServiceNames`,
 * the same helper the account is allocated through. The service names are the ones the Quadlet
 * generator derives from the source names (`X.container` → `X.service`, `X.network` →
 * `X-network.service`).
 */
export function containerCompanionUnitNames(
	kind: ContainerCompanionKind = CONTAINER_COMPANION_KIND,
): ContainerCompanionUnitNames {
	const stem = companionServiceNames(kind).user;
	return {
		stem,
		networkSource: `${stem}.network`,
		databaseSource: `${stem}-db.container`,
		redisSource: `${stem}-redis.container`,
		appSource: `${stem}.container`,
		tunnelSource: `${stem}-tunnel.container`,
		networkService: `${stem}-network.service`,
		databaseService: `${stem}-db.service`,
		redisService: `${stem}-redis.service`,
		appService: `${stem}.service`,
		tunnelService: `${stem}-tunnel.service`,
		databaseContainer: `${stem}-db`,
		redisContainer: `${stem}-redis`,
		appContainer: stem,
		tunnelContainer: `${stem}-tunnel`,
	};
}

/**
 * The generated services, in start order (dependencies first). The edge tunnel, when the entry
 * names one, comes last: it starts after the app it routes to (plan 06-14).
 */
export function containerCompanionServices(
	kind: ContainerCompanionKind = CONTAINER_COMPANION_KIND,
	options: { tunnel?: boolean } = {},
): string[] {
	const names = containerCompanionUnitNames(kind);
	return [
		names.networkService,
		names.databaseService,
		names.redisService,
		names.appService,
		...(options.tunnel === true ? [names.tunnelService] : []),
	];
}

// ---------------------------------------------------------------------------------------------
// CK-B user namespace
// ---------------------------------------------------------------------------------------------

/**
 * The first host UID of a container kind's subordinate range (CK-B).
 *
 * `1_000_000_000 + (uid - 10100) * 65536`. Derived from the unique registry UID, so two kinds
 * cannot overlap by construction; refused outside the reserved block so a stray UID can never
 * address a range nobody reserved.
 */
export function containerCompanionSubuidBase(uid: number): number {
	if (
		!Number.isInteger(uid) ||
		uid < RESERVED_COMPANION_UID_BLOCK.first ||
		uid > RESERVED_COMPANION_UID_BLOCK.last
	) {
		throw new Error(
			`container companion uid ${uid} is outside the reserved companion UID block ${RESERVED_COMPANION_UID_BLOCK.first}-${RESERVED_COMPANION_UID_BLOCK.last}`,
		);
	}
	return (
		CONTAINER_SUBORDINATE_RANGE_ORIGIN +
		(uid - RESERVED_COMPANION_UID_BLOCK.first) * CONTAINER_SUBORDINATE_RANGE_WIDTH
	);
}

// ---------------------------------------------------------------------------------------------
// The loopback and network guard (plan 06-39, CR-02)
// ---------------------------------------------------------------------------------------------
//
// Between "the backend answers" and "the platform registered its owner" a companion is
// unauthenticated, and the first full backup runs inside that window, so it is long. Whoever
// registers first owns the account (CR-02). The window is closed at the kernel: a per-kind
// nftables table refuses every NEW connection from a non-root local socket to the app's loopback
// port and to the installation network's subnet. It is loaded before any container of the kind
// starts -- by the converge, and by every container unit's `ExecStartPre=` so a unit systemd
// starts on boot cannot come up without it.
//
// Why not the two simpler-looking options (06-VERIFICATION.md), so they are not re-opened:
//
// - "Publish the port only after the bootstrap, and bootstrap in-network": with rootful Quadlet
//   the host holds an address on the installation's bridge and routes to every container on it,
//   so any local process reaches the app at its bridge address with nothing published -- and the
//   database and a password-less Redis on the same bridge stay reachable too.
// - "Create the owner and organization with psql before the app starts": Postiz creates its own
//   schema with `prisma db push` as part of the app's start (research Pitfall 1), so there is no
//   `User` or `Organization` table to write into until the app has started.
//
// What passes: container-to-container traffic (the tunnel reaching the app, the app reaching its
// database) is forwarded, not locally generated, and never reaches the output hook; replies to
// connections already accepted are not new; and the platform's own loopback clients (bootstrap,
// readiness, the executor, the export) run as root in the runtime. No other UID is exempted --
// not the tenant runtime UID, not the egress UID.

/** Podman's default pools are 10.88/16 and 10.89/16; this /16 holds one /24 per reserved UID. */
const CONTAINER_COMPANION_SUBNET_PREFIX = "10.213";
/** nftables' own identifier rule, as `transparent-egress.ts` validates its table name. */
const NFT_IDENTIFIER_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * The installation network's pinned subnet: `10.213.<uid - 10100>.0/24`. Derived from the unique
 * registry UID, so two kinds never share one; refused outside the reserved block.
 */
export function containerCompanionSubnet(uid: number): string {
	if (
		!Number.isInteger(uid) ||
		uid < RESERVED_COMPANION_UID_BLOCK.first ||
		uid > RESERVED_COMPANION_UID_BLOCK.last
	) {
		throw new Error(
			`container companion uid ${uid} is outside the reserved companion UID block ${RESERVED_COMPANION_UID_BLOCK.first}-${RESERVED_COMPANION_UID_BLOCK.last}`,
		);
	}
	return `${CONTAINER_COMPANION_SUBNET_PREFIX}.${uid - RESERVED_COMPANION_UID_BLOCK.first}.0/24`;
}

/** The guard's nftables table (`inet iforge_companion_<kind>`). */
export function containerCompanionGuardTable(kind: ContainerCompanionKind): string {
	const table = `iforge_companion_${kind}`;
	if (!NFT_IDENTIFIER_PATTERN.test(table)) {
		throw new Error(`container companion ${kind}: ${table} is not a safe nft identifier`);
	}
	return table;
}

/**
 * The guard's ruleset: ONE `nft -f` transaction. `add table` then `delete table` makes it replace
 * the table atomically whether or not it exists, so a re-apply and a unit's `ExecStartPre=` are
 * both idempotent. The chain holds exactly two rules, both for new connections from any socket
 * whose owner is not root.
 */
export function renderContainerCompanionGuard(input: {
	kind: ContainerCompanionKind;
	port: number;
	subnet: string;
}): string {
	const table = containerCompanionGuardTable(input.kind);
	if (!Number.isInteger(input.port) || input.port < 1 || input.port > 65_535) {
		throw new Error(`container companion ${input.kind}: port ${input.port} is not a TCP port`);
	}
	if (!/^10\.213\.\d{1,3}\.0\/24$/.test(input.subnet)) {
		throw new Error(`container companion ${input.kind}: ${input.subnet} is not a pinned subnet`);
	}
	return [
		GENERATED_CONTAINER_GUARD_HEADER,
		`add table inet ${table}`,
		`delete table inet ${table}`,
		`table inet ${table} {`,
		"\tchain output {",
		"\t\ttype filter hook output priority 0; policy accept;",
		`\t\tct state new meta skuid != 0 ip daddr 127.0.0.1 tcp dport ${input.port} reject with tcp reset`,
		`\t\tct state new meta skuid != 0 ip daddr ${input.subnet} reject`,
		"\t}",
		"}",
		"",
	].join("\n");
}

// ---------------------------------------------------------------------------------------------
// Receipt and facts
// ---------------------------------------------------------------------------------------------

export type ContainerImageSlot = "app" | "postgres" | "redis" | "cloudflared";

export interface ContainerCompanionReceipt {
	schema: typeof CONTAINER_RECEIPT_SCHEMA;
	kind: ContainerCompanionKind;
	/** `applied` while units run; `withdrawn` once units are gone and only data remains. */
	state: "applied" | "withdrawn";
	installationId: string;
	/** The digest reference each slot was verified at. */
	images: Partial<Record<ContainerImageSlot, string>>;
	sourcesSha256: string;
	credentialsSha256: string;
	appliedAt: string;
	/** The data-retention marker: where the companion's data lives, and that it was kept. */
	dataRoot: string;
	dataRetained: true;
	withdrawnAt?: string;
	/**
	 * SHA-256 of the `POSTGRES_PASSWORD` this apply composed (plan 06-25). The official image
	 * reads that variable only when it initialises an EMPTY data directory, so a password the
	 * control plane regenerated reaches the running database only through `ALTER ROLE`; this is
	 * how the next apply knows it must. Absent on a receipt written before plan 06-25, whose
	 * password could not yet have changed since the database was initialised.
	 */
	databasePasswordSha256?: string;
	/**
	 * SHA-256 of the backup env this apply composed (plan 06-25). The running database archives
	 * WAL with the settings it started with, so a changed repository or key restarts it.
	 */
	backupEnvSha256?: string;
	/**
	 * The install report this apply produced (plan 06-12). Absent until the terminal report was
	 * delivered; a new apply rewrites the receipt without it, so each apply is reported once.
	 */
	bootstrap?: ContainerCompanionBootstrapMarker;
	/**
	 * Set by the apply that restored an empty data directory from the repository before the
	 * database first started (plan 06-15); the install report says so once.
	 */
	restore?: { label: string; restoredAt: string };
	/**
	 * When the last backups ran (plan 06-15): the maintenance pass's cache, so it does not ask
	 * the repository on every watch iteration. Absent means "ask pgBackRest once".
	 */
	backupSchedule?: ContainerCompanionBackupSchedule;
	/**
	 * An update this runtime refused (plan 06-18): its backup failed, or it was rolled back. Not
	 * retried while the manifest still names these images; cleared once the pin comes back.
	 */
	refusedUpdate?: {
		images: Partial<Record<ContainerImageSlot, string>>;
		step: string;
		/** The pre-update backup, when one was taken. */
		label: string | null;
		refusedAt: string;
	};
	/**
	 * An update's terminal report, kept until the control plane has it (plan 06-18). It carries the
	 * `to_digest` of the update it was made for (G-06-4), so it names that update however long it
	 * waits; one queued by an older CLI without it is named from this receipt when it is sent. A
	 * `null` one was left unnamed on purpose and is sent without it (06-REVIEW WR-05).
	 */
	pendingUpdateReport?: CompanionInstallReport;
	/**
	 * Where a pending `ready` update report rolls back to if the control plane refuses it (plan
	 * 06-40, CR-03): the receipt as it was before the swap, and the pre-update backup's label.
	 * Written in the same receipt write as that report and removed whenever the report settles.
	 *
	 * Plan 06-47 (D-28) also writes it AHEAD of the swap, with no report, before any unit is
	 * rendered or restarted: a receipt carrying it without `pendingUpdateReport` is a swap that was
	 * interrupted, and the next update pass rolls it back from `label` rather than take a fresh
	 * "pre-update" backup of a schema the new digest may already have written.
	 */
	pendingUpdateRollback?: {
		previous: ContainerCompanionReceipt;
		label: string;
		/** The organization count before the swap (plan 06-47); absent on a pre-06-47 record. */
		organizations?: number;
		/**
		 * The images the swap moves to (06-REVIEW WR-01): what an interrupted swap's rollback records
		 * as refused, whatever the entry pins by then. Absent on a record written before it.
		 */
		to?: Partial<Record<ContainerImageSlot, string>>;
	};
	/**
	 * Tenant export jobs whose database dump this runtime delivered (plan 06-19), newest last. A
	 * job listed here is never dumped again while the manifest still names it.
	 */
	deliveredExports?: string[];
	/**
	 * A rollback that did not finish (plan 06-46, WR-07): the host's real state is unknown, so the
	 * receipt keeps the images the swap rendered and records what a repair needs. While it is set
	 * every plan is `repair`, nothing re-renders, starts, backs up or dumps the installation, and
	 * only `applyPostizUpdate` acts on it: it retries this exact rollback on a backoff.
	 */
	rollbackIncomplete?: ContainerCompanionRollbackIncomplete;
	/**
	 * The app digest that last wrote the database schema (plan 06-47, G-06-1 WR-02, D-28): Postiz
	 * re-applies its schema with `--accept-data-loss` at every start, so this is the only app digest
	 * the converge may start on the data. It mirrors the data directory's root-only marker, which
	 * wins when both exist; a receipt written before plan 06-47 carries neither and falls back to
	 * its applied app digest.
	 */
	schemaDigest?: string;
	/**
	 * The companion network's subnet as last VERIFIED live (plan 06-49, G-06-2 WR-06): written only
	 * once `podman network inspect` reported the pinned subnet and the guard loaded over it. A
	 * receipt without it (written before plan 06-49, or dropped by the update path's check) is
	 * never planned `unchanged`, so the install path verifies -- or migrates -- the network first.
	 */
	networkSubnet?: string;
}

/** What an incomplete rollback records, so it can be retried exactly (plan 06-46, D-17, D-28). */
export interface ContainerCompanionRollbackIncomplete {
	/** The receipt the rollback goes back to: the one applied before the swap, nothing pending. */
	previous: ContainerCompanionReceipt;
	/** The pre-update backup set the rollback restores; never the end of the archive. */
	label: string;
	/** Which rollback failed: the control plane refused the ready, or the new version's data check. */
	step: "control-plane-refused" | "post-update-assertion";
	/** The organization count the previous version must show again after the restore. */
	organizations: number;
	/** How the last attempt failed, redacted. */
	error: string;
	failedAt: string;
	attempts: number;
	lastAttemptAt: string;
}

export interface ContainerCompanionBackupSchedule {
	/** ISO time the newest successful backup completed, or `null` for none. */
	lastBackupAt: string | null;
	/** ISO time the newest successful FULL backup completed, or `null` for none. */
	lastFullAt: string | null;
	lastLabel: string | null;
	/** ISO time of the newest failed attempt; a retry waits `BACKUP_RETRY_AFTER_MS` after it. */
	lastFailedAt?: string;
}

export interface ContainerCompanionBootstrapMarker {
	/** `receiptFingerprint` of the apply this report belongs to. */
	receipt: string;
	phase: "ready" | "failed";
	step: string;
	reportedAt: string;
	/**
	 * Set when the control plane REFUSED this apply's terminal report (plan 06-40): the 409's code
	 * and terminal status. The marker is then `failed` whatever the report said, so nothing gated
	 * on a ready installation (backups, updates, exports) runs for it.
	 */
	refused?: { code: string | null; terminalStatus: string | null };
}

export interface ContainerCompanionFacts {
	paths: ContainerCompanionPaths;
	receipt: ContainerCompanionReceipt | null;
	/** Current content of each source this kind owns, keyed by path (`null` when absent). */
	sourcesOnDisk: Record<string, string | null>;
	/** Whether each encrypted credential file exists, keyed by path. */
	credentialsOnDisk: Record<string, boolean>;
	/** The guard ruleset on disk, or `null` when absent (plan 06-39). */
	guardOnDisk: string | null;
	/**
	 * The app digest the data directory's schema marker names (plan 06-47), or `null` when the
	 * marker is absent or is not exactly 64 lowercase hex digits.
	 */
	schemaDigestOnDisk: string | null;
	/**
	 * Whether the data directory holds an initialised database (G-06-5): PGDATA's `PG_VERSION`
	 * exists and no restore was left unfinished. The same predicate decides the plan 06-48 restore
	 * path, so a directory is either held on its marker or handed to the repository, never both.
	 */
	dataPopulated: boolean;
}

function parseReceipt(raw: string | null): ContainerCompanionReceipt | null {
	if (raw === null) return null;
	try {
		const value = JSON.parse(raw) as Partial<ContainerCompanionReceipt>;
		if (
			value?.schema !== CONTAINER_RECEIPT_SCHEMA ||
			value.kind !== CONTAINER_COMPANION_KIND ||
			(value.state !== "applied" && value.state !== "withdrawn") ||
			typeof value.installationId !== "string" ||
			typeof value.sourcesSha256 !== "string" ||
			typeof value.credentialsSha256 !== "string" ||
			typeof value.images !== "object" ||
			value.images === null
		) {
			return null;
		}
		return value as ContainerCompanionReceipt;
	} catch {
		// A corrupt receipt proves nothing, so it is treated as absent and the next apply rewrites it.
		return null;
	}
}

function credentialIds(kind: ContainerCompanionKind): {
	app: string;
	database: string;
	tunnel: string;
	backup: string;
} {
	return {
		app: `${kind}.env`,
		database: `${kind}-db.env`,
		tunnel: `${kind}-tunnel.env`,
		backup: `${kind}-backup.env`,
	};
}

/** Every source this kind may own, the tunnel's included, so facts and withdrawal see them all. */
function sourcePaths(paths: ContainerCompanionPaths, kind: ContainerCompanionKind): string[] {
	const names = containerCompanionUnitNames(kind);
	return [
		names.networkSource,
		names.databaseSource,
		names.redisSource,
		names.appSource,
		names.tunnelSource,
	].map((name) => join(paths.quadletRoot, name));
}

function credentialPath(paths: ContainerCompanionPaths, id: string): string {
	return join(paths.credentialsRoot, `${id}.cred`);
}

/** Read everything the planner needs from the host. Read-only. */
export function readContainerCompanionFacts(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	kind: ContainerCompanionKind = CONTAINER_COMPANION_KIND,
): ContainerCompanionFacts {
	const sourcesOnDisk: Record<string, string | null> = {};
	for (const path of sourcePaths(paths, kind)) sourcesOnDisk[path] = host.readFile(path);
	const credentialsOnDisk: Record<string, boolean> = {};
	for (const id of Object.values(credentialIds(kind))) {
		const path = credentialPath(paths, id);
		credentialsOnDisk[path] = host.stat(path)?.isFile === true;
	}
	return {
		paths,
		receipt: parseReceipt(host.readFile(paths.receiptPath)),
		sourcesOnDisk,
		credentialsOnDisk,
		guardOnDisk: host.readFile(paths.guardPath),
		schemaDigestOnDisk: parseSchemaDigest(host.readFile(schemaDigestMarkerPath(paths))),
		dataPopulated: dataDirectoryPopulated(host, paths),
	};
}

/**
 * The app digest that last wrote the schema (plan 06-47, D-28): the data directory's marker, then
 * the receipt's record of it, then -- for a receipt written before plan 06-47 -- the receipt's
 * applied app digest. Without a readable receipt (absent, corrupt, or written by a newer CLI) the
 * marker alone answers (G-06-5), and `null` means the data directory names no digest either.
 */
function schemaDigestOf(facts: ContainerCompanionFacts): string | null {
	const receipt = facts.receipt;
	if (receipt === null) return facts.schemaDigestOnDisk;
	return (
		facts.schemaDigestOnDisk ??
		parseSchemaDigest(receipt.schemaDigest ?? null) ??
		digestOf(receipt.images.app)
	);
}

/** The host's schema digest now, as a backup taken now records it (plan 06-48, D-14). */
function currentSchemaDigest(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	kind: ContainerCompanionKind,
): string | null {
	return schemaDigestOf(readContainerCompanionFacts(host, paths, kind));
}

// ---------------------------------------------------------------------------------------------
// Plan
// ---------------------------------------------------------------------------------------------

export interface ContainerCompanionImage {
	slot: ContainerImageSlot;
	image: string;
	sha256: string;
	/** `<image>@sha256:<digest>`: the only form this module ever pulls or renders. */
	reference: string;
}

export interface ContainerCompanionSource {
	path: string;
	content: string;
}

export interface ContainerCompanionCredential {
	/** The credential id: the `--name=` embedded in the ciphertext and the file under `%d`. */
	id: string;
	/** The secret reference the value came from (names only, safe to report). */
	secretRef: string;
	/** Where the ciphertext lives. */
	path: string;
	/**
	 * The plaintext, behind a function so a plan that is ever serialised or logged carries no
	 * secret (`JSON.stringify` drops functions).
	 */
	plaintext: () => string;
}

export interface ContainerCompanionPlan {
	/**
	 * `repair` (plan 06-46, WR-07): the previous receipt records an incomplete rollback, so the host
	 * state is unknown and the apply does nothing at all; `applyPostizUpdate` repairs it.
	 */
	kind: "install" | "unchanged" | "update" | "repair";
	companion: ContainerCompanionKind;
	installationId: string;
	identity: { user: string; uid: number; subordinateBase: number; subordinateWidth: number };
	images: ContainerCompanionImage[];
	sources: ContainerCompanionSource[];
	credentials: ContainerCompanionCredential[];
	/** Data directories with the owner they must carry (container root = the subordinate base). */
	dataDirectories: Array<{ path: string; mode: number; uid: number; gid: number }>;
	/** `tunnel` is the edge connector's service, or `null` when the entry names no tunnel. */
	services: { app: string; tunnel: string | null; all: string[] };
	/** The installation's backup stanza and its credential's id, or `null` without backups. */
	backup: { stanza: string; credentialId: string } | null;
	/** The loopback and network guard (plan 06-39): its file, ruleset, subnet and table. */
	guard: ContainerCompanionGuard;
	receipt: ContainerCompanionReceipt;
	previousReceipt: ContainerCompanionReceipt | null;
	/**
	 * Present when an existing installation's install was planned at its receipt's data-bearing
	 * digests, with the app at the schema digest, instead of at the entry (plan 06-47, G-06-1
	 * WR-02): the converge never changes a data-bearing digest; only `applyPostizUpdate` does.
	 * `covered` is true when the difference is already being handled -- the receipt carries a
	 * pending update report or rollback, the app already is the pin (a dependency-only change the
	 * update path applies), or the entry announces exactly `schemaDigest` -> `pinned`. An uncovered
	 * hold raises `schema_digest_mismatch`.
	 */
	hold?: { schemaDigest: string; pinned: string; covered: boolean };
	/**
	 * What the entry pins, whatever digest this plan runs (plan 06-48): the app digest and the
	 * announced transition. Set by `planContainerCompanion`, so a restore decision taken from this
	 * plan decides against the entry, not against a held or restored digest.
	 */
	pin?: { sha256: string; update: PostizCompanionManifest["update"] };
	/**
	 * The restore an empty data directory gets before its first start (plan 06-48, OPS-01, D-17):
	 * decided from the repository's newest backup BEFORE this plan was made, so the plan's app is
	 * the digest that wrote the schema the restore brings back.
	 */
	restore?: PostizRestoreDecision;
}

/**
 * What a replacement host restores and at which app digest it starts (plan 06-48, G-06-1 WR-02).
 * `fresh`: the repository holds no backup. Otherwise `label` is the newest backup; `archive-end`
 * restores it to the end of the archive, `set-immediate` to its own consistency point (the
 * pre-update point of a swap that was never confirmed). `appDigest` wrote the restored schema, and
 * `covered` is true when it is the pin or the entry announces exactly `appDigest` -> pin.
 */
export type PostizRestoreDecision =
	| { mode: "fresh" }
	| {
			mode: "archive-end" | "set-immediate";
			label: string;
			appDigest: string;
			covered: boolean;
	  };

export interface ContainerCompanionGuard {
	path: string;
	content: string;
	subnet: string;
	table: string;
}

function imageReference(image: string, sha256: string): string {
	return `${image}@sha256:${sha256}`;
}

/** The edge tunnel an entry names (plan 06-14): both halves, or `null` when it names neither. */
interface ContainerCompanionTunnel {
	sha256: string;
	secretRef: string;
}

function entryTunnel(entry: PostizCompanionManifest): ContainerCompanionTunnel | null {
	const image = entry.dependencies.cloudflared;
	const secretRef = entry.credentials.tunnelTokenSecretRef;
	if (image === undefined && secretRef === undefined) return null;
	if (image === undefined || secretRef === undefined) {
		// Half a tunnel is a malformed entry, not "no tunnel": refused by name, never guessed at.
		throw new Error(
			`container companion ${entry.kind}: the edge tunnel needs both dependencies.cloudflared and credentials.tunnelTokenSecretRef`,
		);
	}
	return { sha256: image.sha256, secretRef };
}

/** The backup repository an entry names (plan 06-15): both halves, or `null` for neither. */
export interface ContainerCompanionBackup {
	stanza: string;
	secretRef: string;
}

/** pgBackRest's stanza names, as the manifest contract admits them; re-asserted before use. */
const BACKUP_STANZA_PATTERN = /^[a-z0-9][a-z0-9-]{0,62}$/;

function entryBackup(entry: PostizCompanionManifest): ContainerCompanionBackup | null {
	const backup = entry.backup;
	const secretRef = entry.credentials.backupEnvSecretRef;
	if ((backup === null || backup === undefined) && secretRef === undefined) return null;
	if (backup === null || backup === undefined || secretRef === undefined) {
		// Half a backup is a malformed entry, not "no backup": refused by name, never guessed at.
		throw new Error(
			`container companion ${entry.kind}: backups need both backup and credentials.backupEnvSecretRef`,
		);
	}
	if (!BACKUP_STANZA_PATTERN.test(backup.stanza)) {
		throw new Error(`container companion ${entry.kind}: backup stanza is not a valid name`);
	}
	return { stanza: backup.stanza, secretRef };
}

/**
 * The database's command line with continuous archiving, the house pattern (`config/deploy.yml`):
 * `archive_mode=on`, every WAL segment pushed by pgBackRest, and a segment switch at least every
 * 60 s, which bounds how much a replaced instance can lose. `%%p` is systemd's escape: the
 * generated unit's `ExecStart=` expands `%%` to `%`, so PostgreSQL receives its own `%p`.
 */
function postgresArchiveExec(stanza: string): string {
	return `Exec=postgres -c archive_mode=on -c "archive_command=pgbackrest --stanza=${stanza} archive-push %%p" -c archive_timeout=60`;
}

function containerImages(
	entry: PostizCompanionManifest,
	tunnel: ContainerCompanionTunnel | null,
): ContainerCompanionImage[] {
	// Fixed repository literals from the contract, never read back from the entry's strings: the
	// schema already pins them, and re-deriving here means a schema regression cannot widen what
	// this module is willing to run.
	const slots: Array<[ContainerImageSlot, string, string]> = [
		["app", POSTIZ_APP_IMAGE, entry.artifact.sha256],
		["postgres", POSTIZ_POSTGRES_IMAGE, entry.dependencies.postgres.sha256],
		["redis", POSTIZ_REDIS_IMAGE, entry.dependencies.redis.sha256],
	];
	if (tunnel) slots.push(["cloudflared", POSTIZ_CLOUDFLARED_IMAGE, tunnel.sha256]);
	return slots.map(([slot, image, sha256]) => ({
		slot,
		image,
		sha256,
		reference: imageReference(image, sha256),
	}));
}

function sha256Text(value: string): string {
	return createHash("sha256").update(value).digest("hex");
}

interface ContainerRender {
	description: string;
	requires: string[];
	containerName: string;
	reference: string;
	network: string;
	publish: string | null;
	volumes: string[];
	subordinateBase: number;
	extraContainerLines: string[];
	/** Each becomes its own `EnvironmentFile=%d/<id>` and `LoadCredentialEncrypted=` pair. */
	credentials: Array<{ id: string; path: string }>;
	/** The guard ruleset every container loads before it starts (plan 06-39). */
	guardPath: string;
	install: boolean;
}

function renderContainerSource(input: ContainerRender): string {
	const map = `0:${input.subordinateBase}:${CONTAINER_SUBORDINATE_RANGE_WIDTH}`;
	const lines = [
		GENERATED_CONTAINER_SOURCE_HEADER,
		"[Unit]",
		`Description=${input.description}`,
		...(input.requires.length > 0
			? [`Requires=${input.requires.join(" ")}`, `After=${input.requires.join(" ")}`]
			: []),
		"",
		"[Container]",
		`ContainerName=${input.containerName}`,
		`Image=${input.reference}`,
		// The CLI pulled and verified this exact digest before rendering; the unit must never fetch.
		"Pull=never",
		`Network=${input.network}`,
		...(input.publish ? [`PublishPort=${input.publish}`] : []),
		...input.volumes.map((volume) => `Volume=${volume}`),
		// CK-B static map: container root is the unprivileged host UID at the subordinate base.
		`UIDMap=${map}`,
		`GIDMap=${map}`,
		"NoNewPrivileges=true",
		...input.extraContainerLines,
		...input.credentials.map((credential) => `EnvironmentFile=%d/${credential.id}`),
		"",
		"[Service]",
		// The guard is loaded before the container starts, however it is started (plan 06-39).
		`ExecStartPre=${NFT_BINARY_PATH} -f ${input.guardPath}`,
		...input.credentials.map(
			(credential) => `LoadCredentialEncrypted=${credential.id}:${credential.path}`,
		),
		"Restart=on-failure",
		...(input.install ? ["", "[Install]", "WantedBy=multi-user.target"] : []),
		"",
	];
	return lines.join("\n");
}

function renderNetworkSource(kind: ContainerCompanionKind, subnet: string): string {
	const names = containerCompanionUnitNames(kind);
	return [
		GENERATED_CONTAINER_SOURCE_HEADER,
		"[Unit]",
		`Description=iforge ${kind} companion network (per installation)`,
		"",
		"[Network]",
		`NetworkName=${names.stem}`,
		// Pinned, so the guard's subnet rule covers exactly this bridge (plan 06-39).
		`Subnet=${subnet}`,
		"",
	].join("\n");
}

function renderSources(input: {
	kind: ContainerCompanionKind;
	paths: ContainerCompanionPaths;
	images: ContainerCompanionImage[];
	subordinateBase: number;
	port: number;
	tunnel: boolean;
	backup: ContainerCompanionBackup | null;
	subnet: string;
}): ContainerCompanionSource[] {
	const { kind, paths, images, subordinateBase, backup } = input;
	const guardPath = paths.guardPath;
	const names = containerCompanionUnitNames(kind);
	const ids = credentialIds(kind);
	const byslot = new Map(images.map((image) => [image.slot, image]));
	const reference = (slot: ContainerImageSlot): string => {
		const image = byslot.get(slot);
		if (!image) throw new Error(`container companion ${kind} has no ${slot} image`);
		return image.reference;
	};
	return [
		{
			path: join(paths.quadletRoot, names.networkSource),
			content: renderNetworkSource(kind, input.subnet),
		},
		{
			path: join(paths.quadletRoot, names.databaseSource),
			content: renderContainerSource({
				description: `iforge ${kind} companion database`,
				requires: [],
				containerName: names.databaseContainer,
				reference: reference("postgres"),
				network: names.networkSource,
				publish: null,
				volumes: [`${join(paths.dataRoot, "db")}:/var/lib/postgresql`],
				subordinateBase,
				// Continuous WAL archiving to the installation's repository (plan 06-15, D-14).
				extraContainerLines: backup ? [postgresArchiveExec(backup.stanza)] : [],
				credentials: [
					{ id: ids.database, path: credentialPath(paths, ids.database) },
					...(backup ? [{ id: ids.backup, path: credentialPath(paths, ids.backup) }] : []),
				],
				guardPath,
				install: false,
			}),
		},
		{
			path: join(paths.quadletRoot, names.redisSource),
			content: renderContainerSource({
				description: `iforge ${kind} companion redis (per installation)`,
				requires: [],
				containerName: names.redisContainer,
				reference: reference("redis"),
				network: names.networkSource,
				publish: null,
				volumes: [`${join(paths.dataRoot, "redis")}:/data`],
				subordinateBase,
				extraContainerLines: [],
				credentials: [],
				guardPath,
				install: false,
			}),
		},
		{
			path: join(paths.quadletRoot, names.appSource),
			content: renderContainerSource({
				description: `iforge ${kind} companion`,
				requires: [names.databaseService, names.redisService],
				containerName: names.appContainer,
				reference: reference("app"),
				network: names.networkSource,
				// Loopback only: the executor and the bootstrap reach it on the host, nothing else does.
				// The edge tunnel reaches it inside the network, by name, never through this port.
				publish: `127.0.0.1:${input.port}:${POSTIZ_CONTAINER_PORT}`,
				volumes: [],
				subordinateBase,
				extraContainerLines: [
					// Postiz regenerates its Prisma client into /app/node_modules at every start
					// (verified in research, Pattern 2), so the root filesystem must stay writable.
					"ReadOnly=false",
					`Environment=npm_config_dlx_cache_max_age=${POSTIZ_NPM_DLX_CACHE_MAX_AGE}`,
				],
				credentials: [{ id: ids.app, path: credentialPath(paths, ids.app) }],
				guardPath,
				install: true,
			}),
		},
		...(input.tunnel
			? [
					{
						path: join(paths.quadletRoot, names.tunnelSource),
						content: renderContainerSource({
							description: `iforge ${kind} companion edge tunnel`,
							// After the app it routes to; stopped with it, restarted on failure.
							requires: [names.appService],
							containerName: names.tunnelContainer,
							reference: reference("cloudflared"),
							network: names.networkSource,
							// Outbound only: the connector dials the edge; nothing is published.
							publish: null,
							volumes: [],
							subordinateBase,
							// The token is read from TUNNEL_TOKEN in the credential's env file, never argv.
							extraContainerLines: ["Exec=tunnel --no-autoupdate run"],
							credentials: [{ id: ids.tunnel, path: credentialPath(paths, ids.tunnel) }],
							guardPath,
							install: true,
						}),
					},
				]
			: []),
	];
}

function sameImages(
	left: Partial<Record<ContainerImageSlot, string>>,
	right: Partial<Record<ContainerImageSlot, string>>,
): boolean {
	// The data-bearing slots only. A new connector digest is a plain re-apply (its source changes),
	// not the backup-gated update path a Postiz or database digest change is.
	const slots: ContainerImageSlot[] = ["app", "postgres", "redis"];
	return slots.every((slot) => left[slot] === right[slot]);
}

/**
 * Decide what an apply would do, from the previous receipt and what is actually on disk.
 *
 * - `update`: the applied receipt pins a different digest for some slot. Postiz re-applies its
 *   schema with `--accept-data-loss` at every start (research Pitfall 1), so a digest change is a
 *   potential destructive migration and is never silently applied; plan 06-18 owns the gated path.
 * - `unchanged`: same digests, same rendered sources and credentials, and the host still carries
 *   exactly those sources, both ciphertexts and the guard's ruleset. Nothing is pulled, written or restarted.
 * - `install`: everything else, including a first install, a withdrawn receipt, a credential
 *   rotation and drifted files.
 * - `repair` comes before all of them (plan 06-46, WR-07): a receipt, applied or withdrawn, that
 *   records an incomplete rollback. Neither `install` nor `unchanged` may run over a host whose
 *   units and data are in an unknown state; only the rollback's own retry may touch it.
 */
function planKind(
	facts: ContainerCompanionFacts,
	desired: ContainerCompanionReceipt,
	sources: readonly ContainerCompanionSource[],
	credentials: readonly ContainerCompanionCredential[],
	guard: ContainerCompanionGuard,
): ContainerCompanionPlan["kind"] {
	const previous = facts.receipt;
	if (previous?.rollbackIncomplete !== undefined) return "repair";
	if (previous === null || previous.state !== "applied") return "install";
	// Plan 06-49 (G-06-2 WR-06): an applied receipt whose network subnet no apply verified is
	// planned `install`, never `unchanged`, so the install path verifies -- or migrates -- the
	// network first. That includes a digest change: the swap never migrates a network, and plan
	// 06-47's hold makes that install re-apply the receipt's own digests. A receipt whose update
	// is already in flight (a pending report or rollback) stays with the update pass, whose
	// rollback verifies the network before it starts anything.
	const verified = previous.networkSubnet === guard.subnet;
	const updateInFlight =
		previous.pendingUpdateReport !== undefined || previous.pendingUpdateRollback !== undefined;
	if (!sameImages(previous.images, desired.images)) {
		return verified || updateInFlight ? "update" : "install";
	}
	if (!verified) return "install";
	const onDisk =
		sources.every((source) => facts.sourcesOnDisk[source.path] === source.content) &&
		credentials.every((credential) => facts.credentialsOnDisk[credential.path] === true) &&
		facts.guardOnDisk === guard.content;
	return onDisk &&
		previous.sourcesSha256 === desired.sourcesSha256 &&
		previous.credentialsSha256 === desired.credentialsSha256
		? "unchanged"
		: "install";
}

/**
 * Plan one container companion. Pure over the entry, the secret values and the host facts.
 *
 * The isolation tier is asserted FIRST, before anything else is derived, so an unoffered tier is
 * refused by name before any command could run (D-06): approximating `dedicated` with the standard
 * composition would sell isolation this build does not provide.
 *
 * The install path holds an existing installation where its data is (plan 06-47, G-06-1 WR-02,
 * D-17, D-28). Postiz runs `prisma db push --accept-data-loss` at every start, so starting any app
 * digest other than the one that last wrote the schema is a potential destructive migration, and
 * a new postgres or redis digest is a change of the data's own engine. The hold covers the receipt
 * of this installation (applied or withdrawn), or, with no readable receipt, populated data whose
 * marker names another digest (G-06-5). When the receipt of this installation would be installed
 * over at the entry, and its data-bearing digests or its schema digest differ from the entry's,
 * the install is planned instead at the receipt's digests with the app at the schema digest;
 * without a receipt, at the marker's digest, or with no marker at the digest the generated app unit
 * on disk names (06-REVIEW WR-02), with PostgreSQL and Redis at the digests their generated units
 * on disk name (or the pins when those units are gone). Populated data that names neither is
 * refused with `schema_digest_mismatch`. An empty data directory is never held: the restore
 * decision below owns it. Only `applyPostizUpdate` -- behind its
 * pre-update backup, the plan 06-41 announcement guard and its data assertion -- ever moves them.
 * An announced transition from the schema digest to the pin is therefore carried out by the NEXT
 * update pass, never here: here it would start the new digest with no backup and no update report.
 *
 * `options.restore` (plan 06-48, OPS-01) is the converge's decision for an empty data directory,
 * taken from the repository before this call by `probePostizRestore`. A restore plans the app at
 * the digest that wrote the restored schema, and holds it against the pin exactly as above when
 * the two differ. Every plan records the entry's pin in `plan.pin`.
 */
export function planContainerCompanion(
	entry: PostizCompanionManifest,
	secretValues: Record<string, string>,
	facts: ContainerCompanionFacts,
	options: { restore?: PostizRestoreDecision } = {},
): ContainerCompanionPlan {
	const pin = { sha256: entry.artifact.sha256, update: entry.update };
	const restore = options.restore;
	if (restore === undefined) return { ...planHeld(entry, secretValues, facts), pin };
	if (restore.mode === "fresh") return { ...planHeld(entry, secretValues, facts), pin, restore };
	// Plan 06-48 (OPS-01, D-17, D-28): the data directory is empty and will be restored from a
	// backup whose schema `restore.appDigest` wrote, so that is the app this install starts. An
	// announced change from it to the pin is left to the update path, behind its own backup.
	const restored = planContainerCompanionAt(
		{ ...entry, artifact: { ...entry.artifact, sha256: restore.appDigest } },
		secretValues,
		facts,
	);
	return {
		...restored,
		pin,
		restore,
		...(restore.appDigest !== pin.sha256
			? { hold: { schemaDigest: restore.appDigest, pinned: pin.sha256, covered: restore.covered } }
			: {}),
	};
}

/** The plan 06-47 hold over an installation's retained data, for a plan with no restore decision. */
function planHeld(
	entry: PostizCompanionManifest,
	secretValues: Record<string, string>,
	facts: ContainerCompanionFacts,
): ContainerCompanionPlan {
	const plan = planContainerCompanionAt(entry, secretValues, facts);
	const previous = facts.receipt;
	if (plan.kind !== "install") return plan;
	if (previous === null) return planHeldOnMarker(entry, secretValues, facts, plan);
	const pinned = entry.artifact.sha256;
	const schemaDigest = schemaDigestOf(facts) ?? pinned;
	if (sameImages(previous.images, plan.receipt.images) && schemaDigest === pinned) return plan;
	const atReceipt = entryAtReceipt(entry, previous);
	const held = planContainerCompanionAt(
		{ ...atReceipt, artifact: { ...atReceipt.artifact, sha256: schemaDigest } },
		secretValues,
		facts,
	);
	const announced = entry.update;
	const covered =
		previous.pendingUpdateReport !== undefined ||
		previous.pendingUpdateRollback !== undefined ||
		schemaDigest === pinned ||
		(announced !== null && announced.fromDigest === schemaDigest && announced.toDigest === pinned);
	// An applied receipt whose schema digest is not its own app (an update interrupted after its
	// write-ahead) plans `update` here: the apply then leaves the units alone, and the update pass
	// rolls the interrupted swap back from its stored pre-update backup.
	return { ...held, hold: { schemaDigest, pinned, covered } };
}

/**
 * The hold without a readable receipt (G-06-5, review IN-03, D-28: the marker wins). `parseReceipt`
 * folds an absent receipt, a corrupt one and a newer CLI's into `null`, and none of them says
 * anything about the data, so over a POPULATED data directory the schema digest is decided in this
 * order (06-REVIEW WR-02):
 *
 * 1. the schema marker;
 * 2. with no marker (data written before plan 06-47 has none), the digest the generated app unit on
 *    disk names: the app this host last rendered and ran, the same evidence a readable pre-06-47
 *    receipt gives through `receipt.images.app`;
 * 3. with neither, nothing on the host names the digest that wrote the data, and the plan is
 *    refused with a `ContainerCompanionConditionError` coded `schema_digest_mismatch`. Starting the
 *    pin there would run its `prisma db push --accept-data-loss` over a schema whose writer is
 *    unknown. The converge's isolation catch records the refusal as the apply failure and raises
 *    the code on the installation's condition record, so it reaches the control plane.
 *
 * The install is planned with the app at that digest, and PostgreSQL and Redis at the digests
 * their generated units on disk name, or the entry's pins when those units are gone.
 *
 * An empty data directory (no `PG_VERSION`, or a restore left unfinished) has no schema to protect:
 * it is returned unchanged, still `install`, so the converge hands it to the plan 06-48 restore
 * probe. `covered` comes from the announcement alone: there is no receipt to carry a pending update.
 */
function planHeldOnMarker(
	entry: PostizCompanionManifest,
	secretValues: Record<string, string>,
	facts: ContainerCompanionFacts,
	plan: ContainerCompanionPlan,
): ContainerCompanionPlan {
	if (!facts.dataPopulated) return plan;
	const pinned = entry.artifact.sha256;
	const schemaDigest = schemaDigestOf(facts) ?? imageDigestOnDisk(facts, entry.kind, "app");
	if (schemaDigest === null) {
		throw new ContainerCompanionConditionError(
			"schema_digest_mismatch",
			`container companion ${entry.kind}: the populated data in ${JSON.stringify(facts.paths.dataRoot)} has no readable receipt, no schema marker and no generated app unit, so nothing names the digest that wrote it; refusing to start the pinned app ${pinned} over it`,
		);
	}
	const postgres =
		imageDigestOnDisk(facts, entry.kind, "postgres") ?? entry.dependencies.postgres.sha256;
	const redis = imageDigestOnDisk(facts, entry.kind, "redis") ?? entry.dependencies.redis.sha256;
	if (
		schemaDigest === pinned &&
		postgres === entry.dependencies.postgres.sha256 &&
		redis === entry.dependencies.redis.sha256
	) {
		return plan;
	}
	const held = planContainerCompanionAt(
		{
			...entry,
			artifact: { ...entry.artifact, sha256: schemaDigest },
			dependencies: {
				...entry.dependencies,
				postgres: { ...entry.dependencies.postgres, sha256: postgres },
				redis: { ...entry.dependencies.redis, sha256: redis },
			},
		},
		secretValues,
		facts,
	);
	const announced = entry.update;
	const covered =
		schemaDigest === pinned ||
		(announced !== null && announced.fromDigest === schemaDigest && announced.toDigest === pinned);
	return { ...held, hold: { schemaDigest, pinned, covered } };
}

const IMAGE_DIGEST_PATTERN = /^[a-f0-9]{64}$/;

/**
 * The digest a generated unit on disk runs: the `Image=` line of the app, database or Redis
 * source. PostgreSQL and Redis are read so a receipt-less hold keeps the data's own engine (G-06-5);
 * the app is read so populated data with no receipt and no schema marker is held at the app this
 * host last ran rather than at the pin (06-REVIEW WR-02). Only the digest is taken, and only when
 * the reference is exactly the house repository literal for that slot, `@sha256:` and 64 lowercase
 * hex digits; a unit with any other image, or with no single `Image=` line, reads as `null`, so the
 * caller falls back to the entry pin (dependencies) or refuses (the app). The repository itself is
 * never read back from disk.
 */
function imageDigestOnDisk(
	facts: ContainerCompanionFacts,
	kind: ContainerCompanionKind,
	slot: "app" | "postgres" | "redis",
): string | null {
	const names = containerCompanionUnitNames(kind);
	const [source, image] =
		slot === "app"
			? [names.appSource, POSTIZ_APP_IMAGE]
			: slot === "postgres"
				? [names.databaseSource, POSTIZ_POSTGRES_IMAGE]
				: [names.redisSource, POSTIZ_REDIS_IMAGE];
	const content = facts.sourcesOnDisk[join(facts.paths.quadletRoot, source)] ?? null;
	if (content === null) return null;
	const images = content.split("\n").filter((line) => line.startsWith("Image="));
	if (images.length !== 1) return null;
	const prefix = `Image=${image}@sha256:`;
	const line = images[0] ?? "";
	if (!line.startsWith(prefix)) return null;
	const digest = line.slice(prefix.length);
	return IMAGE_DIGEST_PATTERN.test(digest) ? digest : null;
}

/** Plan exactly at `entry`'s digests, with no hold: the update and rollback paths' own planning. */
function planContainerCompanionAt(
	entry: PostizCompanionManifest,
	secretValues: Record<string, string>,
	facts: ContainerCompanionFacts,
): ContainerCompanionPlan {
	assertOfferedIsolationTier(entry);
	const kind: ContainerCompanionKind = entry.kind;
	const previous = facts.receipt;
	if (previous !== null && previous.installationId !== entry.installationId) {
		// The data directory is per kind, not per installation. Adopting another installation's
		// database would hand one tenant install the previous one's data; destroying it is the
		// explicit `remove` lifecycle's job (plan 06-20), never a side effect of an install.
		throw new Error(
			`container companion ${kind}: ${facts.paths.dataRoot} holds installation ${previous.installationId}; installation ${entry.installationId} is refused until that data is explicitly removed`,
		);
	}
	const uid = resolveCompanionServiceUid(kind);
	if (uid === null) {
		throw new Error(
			`container companion ${kind} needs a registry-pinned uid, not a preallocated one`,
		);
	}
	const subordinateBase = containerCompanionSubuidBase(uid);
	const tunnel = entryTunnel(entry);
	const backup = entryBackup(entry);
	const images = containerImages(entry, tunnel);
	const subnet = containerCompanionSubnet(uid);
	const sources = renderSources({
		kind,
		paths: facts.paths,
		images,
		subordinateBase,
		port: entry.port,
		tunnel: tunnel !== null,
		backup,
		subnet,
	});
	const guard: ContainerCompanionGuard = {
		path: facts.paths.guardPath,
		content: renderContainerCompanionGuard({ kind, port: entry.port, subnet }),
		subnet,
		table: containerCompanionGuardTable(kind),
	};
	const ids = credentialIds(kind);
	const credentials = [
		{ id: ids.app, secretRef: entry.credentials.appEnvSecretRef },
		{ id: ids.database, secretRef: entry.credentials.dbEnvSecretRef },
		...(tunnel ? [{ id: ids.tunnel, secretRef: tunnel.secretRef }] : []),
		...(backup ? [{ id: ids.backup, secretRef: backup.secretRef }] : []),
	].map(({ id, secretRef }): ContainerCompanionCredential => {
		const value = runtimeSecretValue(secretValues, secretRef);
		if (value === null) {
			// Names the reference only, never a value.
			throw new Error(`container companion ${kind} credential ${secretRef} has no secret value`);
		}
		return { id, secretRef, path: credentialPath(facts.paths, id), plaintext: () => value };
	});
	const names = containerCompanionUnitNames(kind);
	const databasePassword = composedDatabasePassword(
		credentials.find((credential) => credential.id === ids.database)?.plaintext() ?? "",
	);
	const backupCredential = credentials.find((credential) => credential.id === ids.backup);
	const receipt: ContainerCompanionReceipt = {
		schema: CONTAINER_RECEIPT_SCHEMA,
		kind,
		state: "applied",
		installationId: entry.installationId,
		images: Object.fromEntries(images.map((image) => [image.slot, image.reference])),
		// The guard is part of what the units depend on, so a changed ruleset is a changed apply.
		sourcesSha256: sha256Text(JSON.stringify({ sources, guard: guard.content })),
		credentialsSha256: sha256Text(
			JSON.stringify(credentials.map((credential) => [credential.id, credential.plaintext()])),
		),
		appliedAt: "",
		dataRoot: facts.paths.dataRoot,
		dataRetained: true,
		...(databasePassword !== null ? { databasePasswordSha256: sha256Text(databasePassword) } : {}),
		...(backupCredential !== undefined
			? { backupEnvSha256: sha256Text(backupCredential.plaintext()) }
			: {}),
	};
	return {
		kind: planKind(facts, receipt, sources, credentials, guard),
		companion: kind,
		installationId: entry.installationId,
		identity: {
			user: names.stem,
			uid,
			subordinateBase,
			subordinateWidth: CONTAINER_SUBORDINATE_RANGE_WIDTH,
		},
		images,
		sources,
		credentials,
		dataDirectories: [
			{ path: facts.paths.dataRoot, mode: 0o755, uid: 0, gid: 0 },
			...["db", "redis"].map((leaf) => ({
				path: join(facts.paths.dataRoot, leaf),
				mode: 0o700,
				uid: subordinateBase,
				gid: subordinateBase,
			})),
		],
		services: {
			app: names.appService,
			tunnel: tunnel ? names.tunnelService : null,
			all: containerCompanionServices(kind, { tunnel: tunnel !== null }),
		},
		backup: backup ? { stanza: backup.stanza, credentialId: ids.backup } : null,
		guard,
		receipt,
		previousReceipt: facts.receipt,
	};
}

// ---------------------------------------------------------------------------------------------
// Probe
// ---------------------------------------------------------------------------------------------

export interface ContainerHostCapabilities {
	systemdVersion: number;
	credentialKey: "tpm2" | "host-secret";
}

function commandDetail(result: ContainerHostCommandResult): string {
	return [result.stderr, result.stdout]
		.filter((value) => value.trim().length > 0)
		.join("\n")
		.trim()
		.slice(-2000);
}

/**
 * Prove the host can run a container companion SAFELY, or refuse by name. Read-only.
 *
 * Research A1/A2 are exactly three of these facts (plan 06-39 added nftables for the guard), and Phase 4 deferred encrypted credentials because
 * they had never been observed on a provisioned image. They are now checked on every converge
 * instead of assumed: a unit the host cannot support would fail AT START while every local test
 * stays green. There is deliberately no degraded mode. In particular a host without a credential
 * key does NOT fall back to the plain `LoadCredential=` form; it refuses (T-06-08-05).
 */
export function probeContainerHost(host: ContainerHost): ContainerHostCapabilities {
	if (host.stat(QUADLET_GENERATOR_PATH)?.isFile !== true) {
		throw new Error(
			`container companions require podman's Quadlet generator (${QUADLET_GENERATOR_PATH} is absent; podman 4.4 or newer ships it)`,
		);
	}
	if (host.stat(NFT_BINARY_PATH)?.isFile !== true) {
		// No degraded mode here either: without the guard a companion is open to every local
		// user until it is bootstrapped (plan 06-39, CR-02).
		throw new Error(
			`container companions require nftables (${NFT_BINARY_PATH} is absent) to keep local users off a companion before it is bootstrapped`,
		);
	}
	const version = host.run("systemctl", ["--version"]);
	const match = version.status === 0 ? /^systemd\s+(\d+)/m.exec(version.stdout) : null;
	if (!match) {
		throw new Error("container companions require systemd, but systemctl --version was unreadable");
	}
	const systemdVersion = Number.parseInt(match[1] ?? "0", 10);
	if (systemdVersion < MINIMUM_CONTAINER_SYSTEMD_VERSION) {
		throw new Error(
			`container companions require systemd ${MINIMUM_CONTAINER_SYSTEMD_VERSION} or newer for encrypted credentials; this host reports systemd ${systemdVersion}`,
		);
	}
	if (host.run("systemd-creds", ["has-tpm2", "--quiet"]).status === 0) {
		return { systemdVersion, credentialKey: "tpm2" };
	}
	const secret = host.stat(SYSTEMD_CREDENTIAL_SECRET_PATH);
	if (secret?.isFile === true && secret.uid === 0 && (secret.mode & 0o077) === 0) {
		return { systemdVersion, credentialKey: "host-secret" };
	}
	throw new Error(
		`container companions require a usable systemd credential key: systemd-creds reports no usable TPM2 device and ${SYSTEMD_CREDENTIAL_SECRET_PATH} is ${secret?.isFile ? "not root-only" : "absent"}; refusing rather than delivering credentials unencrypted`,
	);
}

// ---------------------------------------------------------------------------------------------
// Apply
// ---------------------------------------------------------------------------------------------

export interface ContainerCompanionApplyResult {
	kind: ContainerCompanionPlan["kind"];
	pulled: string[];
	written: string[];
	started: string[];
	/** The backup label an empty data directory was restored from before the first start. */
	restoredFrom?: string | null;
}

function mustRun(
	host: ContainerHost,
	what: string,
	command: string,
	args: readonly string[],
): ContainerHostCommandResult {
	const result = host.run(command, args);
	if (result.status !== 0) {
		const detail = commandDetail(result);
		throw new Error(`container companion ${what} failed${detail ? `: ${detail}` : ""}`);
	}
	return result;
}

/**
 * Refuse any subordinate range that overlaps the companion's CK-B range, by the owning account's
 * name. Read-only; returns whether the companion's own line is already recorded.
 *
 * A line for the companion's own account (by name or numeric UID) must be EXACTLY the computed
 * range: a different range recorded under the same account means the static map and the host's
 * record disagree, and the map would silently move on the next redeploy.
 */
function assertSubordinateRangeFree(
	host: ContainerHost,
	path: string,
	owner: { name: string; uid: number },
	base: number,
): boolean {
	const end = base + CONTAINER_SUBORDINATE_RANGE_WIDTH;
	let recorded = false;
	for (const raw of (host.readFile(path) ?? "").split("\n")) {
		const line = raw.trim();
		if (line.length === 0 || line.startsWith("#")) continue;
		const [name = "", startText = "", countText = ""] = line.split(":");
		const start = Number(startText);
		const count = Number(countText);
		if (!Number.isSafeInteger(start) || !Number.isSafeInteger(count) || count <= 0) {
			throw new Error(`${path} has an unreadable subordinate range line for ${name || "?"}`);
		}
		const own = name === owner.name || name === String(owner.uid);
		if (own) {
			if (start !== base || count !== CONTAINER_SUBORDINATE_RANGE_WIDTH) {
				throw new Error(
					`${path} records ${owner.name} at ${start}:${count}, not its CK-B range ${base}:${CONTAINER_SUBORDINATE_RANGE_WIDTH}; refusing to move a static user-namespace map`,
				);
			}
			recorded = true;
			continue;
		}
		if (start < end && base < start + count) {
			throw new Error(
				`${path} range ${name}:${start}:${count} owned by account ${name} overlaps ${owner.name}'s subordinate range ${base}:${CONTAINER_SUBORDINATE_RANGE_WIDTH}; refusing the converge`,
			);
		}
	}
	return recorded;
}

function recordSubordinateRange(
	host: ContainerHost,
	path: string,
	owner: string,
	base: number,
): void {
	const current = host.readFile(path) ?? "";
	const prefix = current.length === 0 || current.endsWith("\n") ? current : `${current}\n`;
	host.writeFile(path, `${prefix}${owner}:${base}:${CONTAINER_SUBORDINATE_RANGE_WIDTH}\n`, {
		mode: 0o644,
	});
}

function inspectRepoDigests(host: ContainerHost, reference: string): string[] | null {
	const result = host.run("podman", [
		"image",
		"inspect",
		"--format",
		"{{json .RepoDigests}}",
		reference,
	]);
	if (result.status !== 0) return null;
	try {
		const parsed: unknown = JSON.parse(result.stdout.trim() || "null");
		return Array.isArray(parsed) ? parsed.filter((value) => typeof value === "string") : [];
	} catch {
		return [];
	}
}

/**
 * Make the exact digest present locally and prove it (COMP-06, fail closed). Returns whether a
 * pull was issued: an image already present at the verified digest is never pulled again.
 */
function ensureVerifiedImage(host: ContainerHost, image: ContainerCompanionImage): boolean {
	let digests = inspectRepoDigests(host, image.reference);
	let pulled = false;
	if (digests === null) {
		mustRun(host, `pull of ${image.image}`, "podman", ["pull", image.reference]);
		pulled = true;
		digests = inspectRepoDigests(host, image.reference);
	}
	if (digests === null || !digests.includes(image.reference)) {
		throw new Error(
			`container companion image ${image.image} failed digest verification: RepoDigests ${JSON.stringify(digests ?? [])} do not contain ${image.reference}`,
		);
	}
	return pulled;
}

function encryptCredential(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	credential: ContainerCompanionCredential,
): string {
	const plaintextPath = join(paths.plaintextRoot, `.${credential.id}.plain`);
	try {
		host.writeFile(plaintextPath, credential.plaintext(), { mode: PRIVATE_FILE_MODE });
		const result = mustRun(host, `encryption of credential ${credential.id}`, "systemd-creds", [
			"encrypt",
			`--name=${credential.id}`,
			plaintextPath,
			"-",
		]);
		return result.stdout;
	} finally {
		host.removeFile(plaintextPath);
	}
}

/**
 * Files written by one apply, with what each held before, so a failure can put the host back.
 */
class WriteJournal {
	private readonly entries: Array<{ path: string; previous: string | null; mode: number }> = [];

	constructor(private readonly host: ContainerHost) {}

	write(path: string, content: string, mode: number): void {
		this.entries.push({ path, previous: this.host.readFile(path), mode });
		this.host.writeFile(path, content, { mode });
	}

	get paths(): string[] {
		return this.entries.map((entry) => entry.path);
	}

	/** Restore every file in reverse order; best effort, so one failure does not strand the rest. */
	rollback(): string[] {
		const errors: string[] = [];
		for (const entry of [...this.entries].reverse()) {
			try {
				if (entry.previous === null) this.host.removeFile(entry.path);
				else this.host.writeFile(entry.path, entry.previous, { mode: entry.mode });
			} catch (error) {
				errors.push(error instanceof Error ? error.message : String(error));
			}
		}
		return errors;
	}
}

/**
 * A failure that is also a host condition the control plane must see (plan 06-49): the converge's
 * isolation catch raises `code` on the installation's condition record with the redacted message,
 * so a ready installation's failure is reported rather than only logged.
 */
export class ContainerCompanionConditionError extends Error {
	constructor(
		readonly code: CompanionHostConditionCode,
		message: string,
	) {
		super(message);
		this.name = "ContainerCompanionConditionError";
	}
}

/** The companion network's live subnet, as `podman network inspect` reports it. Read-only. */
function inspectContainerCompanionSubnet(
	plan: ContainerCompanionPlan,
	host: ContainerHost,
): string {
	const names = containerCompanionUnitNames(plan.companion);
	const inspected = mustRun(host, `inspection of network ${names.stem}`, "podman", [
		"network",
		"inspect",
		names.stem,
		"--format",
		"{{range .Subnets}}{{.Subnet}} {{end}}",
	]);
	return inspected.stdout.trim();
}

/** The live subnet, or why it could not be read, for the update path's read-only check. */
function readLiveSubnet(plan: ContainerCompanionPlan, host: ContainerHost): string {
	try {
		return inspectContainerCompanionSubnet(plan, host) || "(none)";
	} catch (error) {
		return `(unreadable: ${error instanceof Error ? error.message : String(error)})`;
	}
}

/** Start the network unit, so its network exists, and report whether its live subnet is pinned. */
function networkSubnetVerified(plan: ContainerCompanionPlan, host: ContainerHost): boolean {
	const names = containerCompanionUnitNames(plan.companion);
	mustRun(host, `start of ${names.networkService}`, "systemctl", ["start", names.networkService]);
	return inspectContainerCompanionSubnet(plan, host) === plan.guard.subnet;
}

/**
 * Move the companion network onto the pinned subnet (plan 06-49, G-06-2 WR-06). A network created
 * before plan 06-39 comes from Podman's default pool, which the guard's subnet rule does not cover,
 * so nothing of the kind may run on it.
 *
 * Every unit of the kind except the network is stopped (reverse start order); the network is
 * removed with `podman network rm` -- NEVER forced: a container still attached is something this
 * module does not own, and that refusal must surface -- and the network unit is restarted, so
 * Quadlet recreates it from its source's `Subnet=`. The data directory is not touched. Any failure,
 * or a recreated network that still reports another subnet, throws a
 * `network_subnet_unverified` condition with every unit of the kind left stopped: the 06-39
 * prohibition "no container of the kind starts unguarded" outranks availability.
 *
 * "Left stopped" must survive a reboot (06-REVIEW WR-03): the apply's journal then restores the
 * previous sources, which carry `WantedBy=multi-user.target` and, before plan 06-39, no guard. So
 * a failed migration also masks every unit of the kind but the network (`systemctl mask`, in
 * `/etc`, which outranks Quadlet's generator directory and is not journalled), recorded first in
 * a root-only marker. `ensureContainerCompanionNetwork` unmasks them once the subnet is verified.
 */
function migrateContainerCompanionNetwork(
	plan: ContainerCompanionPlan,
	host: ContainerHost,
	paths: ContainerCompanionPaths,
): void {
	const names = containerCompanionUnitNames(plan.companion);
	const units = plan.services.all.filter((service) => service !== names.networkService);
	const unverified = (reason: string) => {
		const unmasked = maskContainerCompanionUnits(host, paths, units);
		return new ContainerCompanionConditionError(
			"network_subnet_unverified",
			`container companion ${plan.companion}: network ${names.stem} could not be moved onto the pinned subnet ${plan.guard.subnet}; ${reason}; every unit of the kind stays stopped${unmasked === null ? "" : `, but ${unmasked}`}`,
		);
	};
	try {
		mustRun(host, `stop of ${plan.companion} before its network migration`, "systemctl", [
			"stop",
			...[...units].reverse(),
		]);
		mustRun(host, `removal of network ${names.stem}`, "podman", ["network", "rm", names.stem]);
		mustRun(host, `restart of ${names.networkService}`, "systemctl", [
			"restart",
			names.networkService,
		]);
		const actual = inspectContainerCompanionSubnet(plan, host);
		if (actual !== plan.guard.subnet) {
			throw unverified(`the recreated network reports subnet ${actual || "(none)"}`);
		}
	} catch (error) {
		if (error instanceof ContainerCompanionConditionError) throw error;
		throw unverified(error instanceof Error ? error.message : String(error));
	}
}

const UNITS_MASKED_SCHEMA = "iforge.containerCompanionUnitsMasked.v1";

/** The root-only record of the units a failed network migration masked (06-REVIEW WR-03). */
function unitsMaskedPath(paths: ContainerCompanionPaths): string {
	return join(paths.companionStateRoot, "units-masked.json");
}

/**
 * Persistently mask `units` after a failed network migration (06-REVIEW WR-03), so no reboot
 * starts them over the unverified network. The record is written FIRST: a pass that dies between
 * the two leaves a record whose unmask is a no-op, never a mask nothing will lift. Returns why the
 * mask did not take, or `null`; it never throws, so the migration's own failure is what surfaces.
 */
function maskContainerCompanionUnits(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	units: readonly string[],
): string | null {
	try {
		const recorded = readMaskedUnits(host, paths);
		host.ensureDirectory(paths.companionStateRoot, { mode: 0o700, uid: 0, gid: 0 });
		host.writeFile(
			unitsMaskedPath(paths),
			`${JSON.stringify(
				{
					schema: UNITS_MASKED_SCHEMA,
					units: [...new Set([...recorded, ...units])],
					maskedAt: host.now().toISOString(),
				},
				null,
				2,
			)}\n`,
			{ mode: PRIVATE_FILE_MODE, uid: 0, gid: 0 },
		);
		const masked = host.run("systemctl", ["mask", ...units]);
		if (masked.status !== 0) {
			const detail = commandDetail(masked);
			return `its units could not be masked, so a reboot may start them (systemctl mask exited ${masked.status}${detail ? `: ${detail}` : ""})`;
		}
		return null;
	} catch (error) {
		return `its units could not be masked, so a reboot may start them (${error instanceof Error ? error.message : String(error)})`;
	}
}

/** The units a failed migration recorded as masked; empty when none are. */
function readMaskedUnits(host: ContainerHost, paths: ContainerCompanionPaths): string[] {
	const raw = host.readFile(unitsMaskedPath(paths));
	if (raw === null) return [];
	try {
		const value = JSON.parse(raw) as { units?: unknown };
		return Array.isArray(value.units)
			? value.units.filter((unit): unit is string => typeof unit === "string")
			: [];
	} catch {
		return [];
	}
}

/**
 * Lift the mask a failed network migration left (06-REVIEW WR-03), shared by the network check
 * and removal so the two cannot drift. A no-op issuing no command when there is no record.
 * Otherwise it unmasks the record's units together with `units`, minus the kind's network
 * service, and deletes the record only once the unmask succeeded: a failed unmask throws through
 * `mustRun` with the record in place, so the next pass retries it.
 *
 * The union is taken because the record alone can understate what is masked:
 * `maskContainerCompanionUnits` writes the record before it masks, and `readMaskedUnits` reads a
 * corrupt record as empty. Unmasking a unit that is not masked is a no-op for systemctl.
 */
function liftContainerCompanionMask(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	kind: ContainerCompanionKind,
	units: readonly string[],
	purpose: string,
): void {
	if (host.readFile(unitsMaskedPath(paths)) === null) return;
	const names = containerCompanionUnitNames(kind);
	const unmask = new Set(
		[...readMaskedUnits(host, paths), ...units].filter((unit) => unit !== names.networkService),
	);
	mustRun(host, purpose, "systemctl", ["unmask", ...unmask]);
	host.removeFile(unitsMaskedPath(paths));
}

/**
 * Prove the installation network has the pinned subnet, migrating it when it does not (plan
 * 06-49), and only then lift the mask a failed migration left (06-REVIEW WR-03): the record's
 * units and the plan's own, before anything of the kind is started. A migration that cannot
 * finish throws with the units masked again.
 */
function ensureContainerCompanionNetwork(
	plan: ContainerCompanionPlan,
	host: ContainerHost,
	paths: ContainerCompanionPaths,
): void {
	if (!networkSubnetVerified(plan, host)) migrateContainerCompanionNetwork(plan, host, paths);
	liftContainerCompanionMask(
		host,
		paths,
		plan.companion,
		plan.services.all,
		`unmask of ${plan.companion} after its network was verified`,
	);
}

/**
 * Start the installation network, prove it has the pinned subnet, and load the guard (plan 06-39).
 *
 * A network that already existed with another subnet (created before the subnet was pinned) is
 * migrated first (plan 06-49): the guard would not cover it, so nothing of the kind may start on
 * it. A migration that cannot finish throws, and nothing starts.
 */
function loadContainerCompanionGuard(
	plan: ContainerCompanionPlan,
	host: ContainerHost,
	paths: ContainerCompanionPaths,
): void {
	ensureContainerCompanionNetwork(plan, host, paths);
	mustRun(host, `load of the ${plan.guard.table} guard`, NFT_BINARY_PATH, ["-f", plan.guard.path]);
}

/**
 * The `schema_digest_mismatch` detail (plan 06-47): digests only, so there is nothing to redact,
 * and it names both the digest the host keeps running and the pin it does not run.
 */
function schemaDigestMismatchDetail(running: string, pinned: string): string {
	return `the host runs Postiz ${running}, the digest that last wrote its data; the entry pins ${pinned} without announcing a change from ${running}, so the host keeps running ${running}`;
}

/** The app digest a plan renders. */
function planAppDigest(plan: ContainerCompanionPlan): string {
	const app = plan.images.find((image) => image.slot === "app");
	if (!app) throw new Error(`container companion ${plan.companion} has no app image`);
	return app.sha256;
}

/**
 * What an install-path receipt keeps from the previous receipt of the same installation (plan
 * 06-47, G-06-1 WR-02): an update's pending terminal report and where a refused `ready` rolls back
 * to, so a withdrawn -> install (or a credential rotation) never drops the rollback of plan 06-40;
 * the refused update, so it is not retried while the manifest names it; and the delivered exports,
 * so none is dumped again. The ready marker is not kept: each apply is reported once.
 */
function keptAcrossApply(
	previous: ContainerCompanionReceipt | null,
	installationId: string,
): Partial<ContainerCompanionReceipt> {
	if (previous === null || previous.installationId !== installationId) return {};
	const { pendingUpdateReport, pendingUpdateRollback, refusedUpdate, deliveredExports } = previous;
	return {
		...(pendingUpdateReport !== undefined ? { pendingUpdateReport } : {}),
		...(pendingUpdateRollback !== undefined ? { pendingUpdateRollback } : {}),
		...(refusedUpdate !== undefined ? { refusedUpdate } : {}),
		...(deliveredExports !== undefined ? { deliveredExports } : {}),
	};
}

/**
 * Apply a plan. The ORDER is the contract, and every step before "source writes" is either a
 * refusal or reversible:
 *
 *   tier (in the planner) → host probe → subordinate-range check → identity and range record →
 *   pulls and digest verification → credential encryption → guard and source writes → reload →
 *   network start, subnet check and guard load → start → receipt.
 *
 * A failure at any step after the first write restores every file this apply wrote (removing the
 * ones that did not exist before), so from the host's point of view the apply either happened or
 * did not. Two things are deliberately NOT rolled back: the companion's account and its recorded
 * subordinate range, both of which are never reclaimed by design (D-13, CK-B), and the data
 * directory, which this module never removes.
 */
export function applyContainerCompanion(
	plan: ContainerCompanionPlan,
	host: ContainerHost,
	paths: ContainerCompanionPaths,
): ContainerCompanionApplyResult {
	if (plan.kind === "repair") {
		// Plan 06-46 (WR-07): a rollback did not finish, so the units and the data are in an unknown
		// state. Nothing is probed, pulled, written or started over it; `applyPostizUpdate` retries
		// that rollback with its own pre-update backup. Like `update`, the applied units stay as they are.
		return { kind: "repair", pulled: [], written: [], started: [] };
	}
	probeContainerHost(host);
	const owner = { name: plan.identity.user, uid: plan.identity.uid };
	const recorded = [paths.subuidPath, paths.subgidPath].map((path) => ({
		path,
		present: assertSubordinateRangeFree(host, path, owner, plan.identity.subordinateBase),
	}));
	if (plan.kind === "unchanged") {
		return { kind: "unchanged", pulled: [], written: [], started: [] };
	}
	if (plan.kind === "update") {
		// Never here: Postiz's start-time schema push makes a new digest a potential destructive
		// migration (research Pitfall 1, OPS-02). The applied units stay exactly as they are, and
		// `applyPostizUpdate` -- which runs after the converge -- backs up, swaps and asserts.
		return { kind: "update", pulled: [], written: [], started: [] };
	}

	const identity = host.ensureIdentity(plan.companion);
	if (identity.uid !== plan.identity.uid) {
		throw new Error(
			`container companion ${plan.companion} account resolved to uid ${identity.uid}, not the registry-pinned ${plan.identity.uid}`,
		);
	}
	for (const { path, present } of recorded) {
		if (!present) recordSubordinateRange(host, path, owner.name, plan.identity.subordinateBase);
	}
	const pulled = plan.images
		.filter((image) => ensureVerifiedImage(host, image))
		.map((image) => image.reference);

	const journal = new WriteJournal(host);
	let reloaded = false;
	let restoredFrom: string | null = null;
	try {
		for (const directory of plan.dataDirectories) host.ensureDirectory(directory.path, directory);
		host.ensureDirectory(paths.companionStateRoot, { mode: 0o700, uid: 0, gid: 0 });
		host.ensureDirectory(paths.credentialsRoot, { mode: 0o700, uid: 0, gid: 0 });
		host.ensureDirectory(paths.plaintextRoot, { mode: 0o700, uid: 0, gid: 0 });
		// Before anything could start the database: an empty data directory whose repository
		// holds a backup is restored first (plan 06-15, OPS-01 instance replacement).
		const restore = restorePostizOnBootstrap(plan, host, paths);
		restoredFrom = restore.restored ? restore.label : null;
		for (const credential of plan.credentials) {
			journal.write(credential.path, encryptCredential(host, paths, credential), PRIVATE_FILE_MODE);
		}
		// The guard's ruleset, root-only, before any unit that loads it can exist (plan 06-39).
		journal.write(plan.guard.path, plan.guard.content, PRIVATE_FILE_MODE);
		host.ensureDirectory(paths.quadletRoot, { mode: 0o755, uid: 0, gid: 0 });
		const databaseSourceBefore = databaseSourceOnDisk(plan, host);
		for (const source of plan.sources) journal.write(source.path, source.content, SOURCE_FILE_MODE);
		reloaded = true;
		mustRun(host, "systemd daemon-reload", "systemctl", ["daemon-reload"]);
		// The network first, so its real subnet can be checked against the one the guard covers;
		// then the guard; only then anything that could start a container of the kind.
		loadContainerCompanionGuard(plan, host, paths);
		// Plan 06-49: the guard is loaded over the verified pinned subnet; an earlier failed
		// migration's condition is cleared (a no-op when none is raised).
		clearContainerCompanionCondition(host, paths, {
			installationId: plan.installationId,
			code: "network_subnet_unverified",
		});
		// A re-apply over running units (plan 06-25). The database restarts first when its own
		// unit or backup settings changed (a new stanza after abandoned backups, a re-minted backup
		// key); then a regenerated database password is applied to the running database BEFORE the
		// app restarts with a `DATABASE_URL` that carries it; and the app is RESTARTED -- `start` on
		// a running unit would leave it on the old environment -- whenever its credentials changed.
		const previous = plan.previousReceipt;
		const reapply = previous !== null && previous.state === "applied";
		if (reapply && databaseInputsChanged(plan, previous, databaseSourceBefore)) {
			const database = containerCompanionUnitNames(plan.companion).databaseService;
			mustRun(host, `restart of ${database}`, "systemctl", ["restart", database]);
		}
		if (reapply) reconcilePostizDatabasePassword(plan, host, previous);
		// Plan 06-47 (D-28): the app about to start re-applies its schema, so the data directory
		// records it first. Not journalled: a failed apply never rolls the data back either, and
		// the app it may have started is exactly the one the marker names.
		writeSchemaDigestMarker(host, paths, planAppDigest(plan));
		const appVerb =
			reapply && previous.credentialsSha256 !== plan.receipt.credentialsSha256
				? "restart"
				: "start";
		mustRun(host, `${appVerb} of ${plan.services.app}`, "systemctl", [appVerb, plan.services.app]);
		// The edge connector after the app it routes to. Running it routes nothing by itself: the
		// control plane publishes the hostname only after this runtime's ready report (plan 06-14).
		if (plan.services.tunnel !== null) {
			mustRun(host, `start of ${plan.services.tunnel}`, "systemctl", [
				"start",
				plan.services.tunnel,
			]);
		}
		const appliedAt = host.now().toISOString();
		const receipt: ContainerCompanionReceipt = {
			...plan.receipt,
			...keptAcrossApply(previous, plan.installationId),
			appliedAt,
			...(restoredFrom !== null ? { restore: { label: restoredFrom, restoredAt: appliedAt } } : {}),
			schemaDigest: planAppDigest(plan),
			// Plan 06-49: the guard loaded above only after the live subnet was verified.
			networkSubnet: plan.guard.subnet,
		};
		journal.write(paths.receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, PRIVATE_FILE_MODE);
	} catch (error) {
		// A first install that got as far as the generator may have started the database or Redis
		// as dependencies; stop them before their sources disappear. A re-apply leaves the
		// previously applied units alone: their sources are restored below.
		if (reloaded && plan.previousReceipt?.state !== "applied") {
			host.run("systemctl", ["stop", ...[...plan.services.all].reverse()]);
		}
		const rollbackErrors = journal.rollback();
		if (reloaded) host.run("systemctl", ["daemon-reload"]);
		const message = error instanceof Error ? error.message : String(error);
		const combined =
			rollbackErrors.length === 0
				? message
				: `${message}; rollback incomplete: ${rollbackErrors.join("; ")}`;
		// A condition (plan 06-49) keeps its code, so the converge raises it on the host record.
		if (error instanceof ContainerCompanionConditionError) {
			throw new ContainerCompanionConditionError(error.code, combined);
		}
		throw new Error(combined);
	}
	// Plan 06-47: the host now runs the app it planned. It is the pin, or it is the schema's own
	// digest held against a pin nobody announced, which the control plane must see.
	const running = planAppDigest(plan);
	const pinned = plan.hold?.pinned ?? running;
	if (running === pinned) {
		clearContainerCompanionCondition(host, paths, {
			installationId: plan.installationId,
			code: "schema_digest_mismatch",
		});
	} else if (plan.hold !== undefined && !plan.hold.covered) {
		raiseContainerCompanionCondition(host, paths, {
			installationId: plan.installationId,
			code: "schema_digest_mismatch",
			detail: schemaDigestMismatchDetail(running, pinned),
		});
	}
	return {
		kind: plan.kind,
		pulled,
		written: journal.paths,
		started: [plan.services.app, ...(plan.services.tunnel ? [plan.services.tunnel] : [])],
		restoredFrom,
	};
}

// ---------------------------------------------------------------------------------------------
// Database password reconciliation (plan 06-25, OPS-07)
// ---------------------------------------------------------------------------------------------
//
// The control plane can regenerate an installation's database password when its stored copy
// becomes unreadable (a lost vault key). The composed `db-env` then carries the new
// `POSTGRES_PASSWORD` and `app-env` a `DATABASE_URL` with it -- but PostgreSQL read that variable
// only when it initialised the data directory, so the running role still has the old password.
// The apply therefore sets it with `ALTER ROLE` before the app restarts. The new password is bound
// as a psql variable from a script read on STANDARD INPUT and used as `:'pw'`, so it never reaches
// a command line (`/proc/<pid>/cmdline`); psql connects over the container's local socket, which
// needs no password -- so the old one, which nobody can read any more, is never needed either.

const POSTGRES_PASSWORD_PREFIX = "POSTGRES_PASSWORD=";

function databaseSource(plan: ContainerCompanionPlan): ContainerCompanionSource | undefined {
	const name = containerCompanionUnitNames(plan.companion).databaseSource;
	return plan.sources.find((source) => source.path.endsWith(`/${name}`));
}

/** The database unit's source as it is on disk before this apply writes (`null` when absent). */
function databaseSourceOnDisk(plan: ContainerCompanionPlan, host: ContainerHost): string | null {
	const source = databaseSource(plan);
	return source === undefined ? null : host.readFile(source.path);
}

/**
 * Whether the running database must restart onto this apply's inputs: its unit changed (the
 * archive command names the stanza), or the backup settings it archives with changed. A receipt
 * written before plan 06-25 carries no backup hash; the unit comparison still catches a new stanza.
 */
function databaseInputsChanged(
	plan: ContainerCompanionPlan,
	previous: ContainerCompanionReceipt,
	sourceBefore: string | null,
): boolean {
	const source = databaseSource(plan);
	if (source !== undefined && sourceBefore !== null && sourceBefore !== source.content) return true;
	const desired = plan.receipt.backupEnvSha256;
	const applied = previous.backupEnvSha256;
	return desired !== undefined && applied !== undefined && desired !== applied;
}

/** The `POSTGRES_PASSWORD` a composed `db-env` carries, or `null` when it carries none. */
function composedDatabasePassword(dbEnv: string): string | null {
	for (const line of dbEnv.split("\n")) {
		if (line.startsWith(POSTGRES_PASSWORD_PREFIX)) {
			const value = line.slice(POSTGRES_PASSWORD_PREFIX.length);
			return value.length > 0 ? value : null;
		}
	}
	return null;
}

/**
 * On a re-apply, set a changed database password on the running role. A no-op when the password
 * is unchanged, and when the previous receipt predates the recorded hash (nothing could have
 * changed the password since the database was initialised). Throws, naming no value, when psql
 * refuses: the apply then rolls back and the next converge tries again.
 */
function reconcilePostizDatabasePassword(
	plan: ContainerCompanionPlan,
	host: ContainerHost,
	previous: ContainerCompanionReceipt,
): void {
	const desired = plan.receipt.databasePasswordSha256;
	const applied = previous.databasePasswordSha256;
	if (desired === undefined || applied === undefined || desired === applied) return;
	const ids = credentialIds(plan.companion);
	const credential = plan.credentials.find((candidate) => candidate.id === ids.database);
	const password = composedDatabasePassword(credential?.plaintext() ?? "");
	if (password === null) return;
	const names = containerCompanionUnitNames(plan.companion);
	// The database unit is already running on a re-apply; `start` is a no-op that makes sure.
	mustRun(host, `start of ${names.databaseService}`, "systemctl", ["start", names.databaseService]);
	const result = host.run(
		"podman",
		[
			"exec",
			"-i",
			names.databaseContainer,
			"psql",
			"-X",
			"-q",
			"-v",
			"ON_ERROR_STOP=1",
			"-U",
			POSTIZ_DB_USER,
			"-d",
			POSTIZ_DB_NAME,
		],
		{
			input: [
				`\\set pw ${psqlQuoted(password)}`,
				`ALTER ROLE ${POSTIZ_DB_USER} WITH PASSWORD :'pw';`,
				"",
			].join("\n"),
		},
	);
	if (result.status !== 0) {
		// PostgreSQL can echo a statement's text in an error: never let the password through.
		const detail = commandDetail(result).split(password).join("[redacted]");
		throw new Error(
			`container companion ${plan.companion}: applying the regenerated database password failed${
				detail ? `: ${detail}` : ""
			}`,
		);
	}
}

// ---------------------------------------------------------------------------------------------
// Restore-on-bootstrap (plan 06-15, OPS-01)
// ---------------------------------------------------------------------------------------------
//
// The `/v2` provisioning contract has no volume concept, so a replaced instance comes back with an
// EMPTY data directory. Starting PostgreSQL over it would initialise a new, empty cluster -- and
// its archive would then collide with the installation's real history. So, before the database
// unit's first start, an empty directory is checked against the repository and, when the stanza
// holds a backup, restored to the end of the archive. This is what "survives instance
// replacement" means until the provisioner offers durable volumes (the OWN-02 condition).
//
// The database container is not running at this point, so pgBackRest runs in a ONE-SHOT container
// from the same pinned image, in the same user namespace, with the backup credential materialised
// for the moment it runs on the run root's tmpfs and removed in `finally`, whatever happens.

/** PGDATA inside the data directory: the house image's pg18 layout (`/var/lib/postgresql/18/docker`). */
const PGDATA_SUBPATH = ["18", "docker"] as const;
/**
 * Present while a restore has begun and not finished. A partial restore is never started as a
 * database: the next converge restores again (with `--delta`) instead of trusting `PG_VERSION`.
 */
const RESTORE_INCOMPLETE_MARKER = ".iforge-restore-incomplete";
/**
 * Which app digest last wrote this data directory's schema (plan 06-47, G-06-1 WR-02, D-28):
 * root-only, `<64 lowercase hex>\n`. It is written BEFORE every start or restart that can run a new
 * app digest on the data, so the record is never older than the schema it describes -- the
 * receipt alone is not enough, because an update that crashes after its restart leaves the
 * receipt naming the old images.
 */
const SCHEMA_DIGEST_MARKER = ".iforge-schema-digest";
const SCHEMA_DIGEST_PATTERN = /^[a-f0-9]{64}$/;

function schemaDigestMarkerPath(paths: ContainerCompanionPaths): string {
	return join(databaseDataRoot(paths), SCHEMA_DIGEST_MARKER);
}

/** Record, root-only, that `digest` is the app that writes the schema from now on. */
function writeSchemaDigestMarker(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	digest: string,
): void {
	host.writeFile(schemaDigestMarkerPath(paths), `${digest}\n`, {
		mode: PRIVATE_FILE_MODE,
		uid: 0,
		gid: 0,
	});
}

/** A stored schema digest, or `null` for anything but exactly 64 lowercase hex digits. */
function parseSchemaDigest(raw: string | null): string | null {
	const value = raw?.replace(/\n$/, "") ?? null;
	return value !== null && SCHEMA_DIGEST_PATTERN.test(value) ? value : null;
}

export type ContainerCompanionRestoreResult =
	| { restored: false; reason: "no-backups-configured" | "populated" | "no-backup-to-restore" }
	| { restored: true; label: string };

function databaseDataRoot(paths: ContainerCompanionPaths): string {
	return join(paths.dataRoot, "db");
}

function planPostgresReference(plan: ContainerCompanionPlan): string {
	const image = plan.images.find((candidate) => candidate.slot === "postgres");
	if (!image) throw new Error(`container companion ${plan.companion} has no postgres image`);
	return image.reference;
}

/** Run pgBackRest in a one-shot container over the data directory. Never throws. */
function oneShotPgBackRest(
	host: ContainerHost,
	plan: ContainerCompanionPlan,
	paths: ContainerCompanionPaths,
	envFile: string,
	purpose: "info" | "restore",
	args: readonly string[],
): ContainerHostCommandResult {
	const map = `0:${plan.identity.subordinateBase}:${plan.identity.subordinateWidth}`;
	return host.run("podman", [
		"run",
		"--rm",
		"--replace",
		`--name=${plan.identity.user}-db-${purpose}`,
		"--pull=never",
		"--security-opt=no-new-privileges",
		`--uidmap=${map}`,
		`--gidmap=${map}`,
		// The repository settings, the key and the cipher pass: a file, never an argument.
		`--env-file=${envFile}`,
		// Only the restore touches the data; asking the repository needs none of it.
		...(purpose === "restore" ? [`--volume=${databaseDataRoot(paths)}:/var/lib/postgresql`] : []),
		"--entrypoint",
		"pgbackrest",
		planPostgresReference(plan),
		...args,
	]);
}

/**
 * Decide what an empty data directory is restored to, and which app digest then starts on it
 * (plan 06-48, G-06-1 WR-02, OPS-01, D-14, D-17, D-28). Pure.
 *
 * The repository is the only record that outlives an instance, so it says which app wrote the
 * schema: every backup is annotated with the schema digest current when it was taken (S), and a
 * pre-update backup additionally with the digest its swap was moving to (N) BEFORE anything
 * restarted -- WAL archived after it may hold N's schema. With P the pin, A the announced update
 * and E = N ?? S (the digest that wrote the end of the archive), the first rule that matches wins:
 *
 * 1. no backup: a fresh start;
 * 2. S absent (a repository written before plan 06-48): the end of the archive at P, as before;
 * 3. N present, and P is S again or A still announces exactly S -> N (the swap began and was never
 *    confirmed): that backup set at its own consistency point, at S -- the point plan 06-40's
 *    rollback would have restored had the host survived;
 * 4. E is P: the end of the archive at P;
 * 5. A announces exactly E -> P: the end of the archive at E, covered (the update path carries it out);
 * 6. otherwise: the end of the archive at E, not covered (`schema_digest_mismatch`).
 *
 * The app that starts is always the digest that wrote the restored schema; only the backup-gated
 * update path ever moves it to another.
 */
export function decidePostizRestore(
	newest: PostizBackupInfo | undefined,
	entry: {
		artifact: { sha256: string };
		update: { fromDigest: string; toDigest: string } | null;
	},
): PostizRestoreDecision {
	// Rule 1.
	if (newest === undefined) return { mode: "fresh" };
	const pinned = entry.artifact.sha256;
	const announced = entry.update;
	const announces = (from: string, to: string) =>
		announced !== null && announced.fromDigest === from && announced.toDigest === to;
	const decide = (
		mode: "archive-end" | "set-immediate",
		appDigest: string,
	): PostizRestoreDecision => ({
		mode,
		label: newest.label,
		appDigest,
		covered: appDigest === pinned || announces(appDigest, pinned),
	});
	const schema = newest.schemaDigest;
	// Rule 2.
	if (schema === null) return decide("archive-end", pinned);
	// A next digest equal to the schema's own is no pending change.
	const next =
		newest.nextSchemaDigest !== null && newest.nextSchemaDigest !== schema
			? newest.nextSchemaDigest
			: null;
	// Rule 3.
	if (next !== null && (pinned === schema || announces(schema, next))) {
		return decide("set-immediate", schema);
	}
	// Rules 4, 5 and 6: the end of the archive, at the digest that wrote it; `covered` tells them apart.
	return decide("archive-end", next ?? schema);
}

/** Whether the data directory needs a restore before its first start, and why not when it does not. */
function restoreNeed(
	plan: ContainerCompanionPlan,
	host: ContainerHost,
	paths: ContainerCompanionPaths,
):
	| { needed: false; reason: "no-backups-configured" | "populated" }
	| { needed: true; unfinished: boolean } {
	if (plan.backup === null) return { needed: false, reason: "no-backups-configured" };
	if (dataDirectoryPopulated(host, paths)) return { needed: false, reason: "populated" };
	return { needed: true, unfinished: restoreUnfinished(host, paths) };
}

/** Whether a restore began in this data directory and did not finish. */
function restoreUnfinished(host: ContainerHost, paths: ContainerCompanionPaths): boolean {
	return host.stat(join(databaseDataRoot(paths), RESTORE_INCOMPLETE_MARKER))?.isFile === true;
}

/**
 * Whether the data directory holds an initialised database: PGDATA's `PG_VERSION` is a file and no
 * restore was left unfinished. The ONE populated-data predicate (G-06-5): `restoreNeed` hands
 * everything else to the repository, and the receipt-less hold in `planHeld` holds only this, so
 * the two can never disagree about a directory.
 */
function dataDirectoryPopulated(host: ContainerHost, paths: ContainerCompanionPaths): boolean {
	if (restoreUnfinished(host, paths)) return false;
	return host.stat(join(databaseDataRoot(paths), ...PGDATA_SUBPATH, "PG_VERSION"))?.isFile === true;
}

/**
 * Run `fn` with the backup credential in a root-only env file on the run root's tmpfs, removed in
 * `finally` whatever happens: the one-shot pgBackRest container reads the repository settings, the
 * key and the cipher pass from it, never from an argument.
 */
function withOneShotEnv<T>(
	plan: ContainerCompanionPlan,
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	fn: (envFile: string, stanza: string) => T,
): T {
	if (plan.backup === null) {
		throw new Error(`container companion ${plan.companion} has no backup repository`);
	}
	const credentialId = plan.backup.credentialId;
	const credential = plan.credentials.find((candidate) => candidate.id === credentialId);
	if (!credential) {
		throw new Error(`container companion ${plan.companion} has no ${credentialId} credential`);
	}
	const envFile = join(paths.plaintextRoot, `.${credentialId}.oneshot.plain`);
	try {
		host.ensureDirectory(paths.plaintextRoot, { mode: 0o700, uid: 0, gid: 0 });
		host.writeFile(envFile, credential.plaintext(), { mode: PRIVATE_FILE_MODE });
		return fn(envFile, plan.backup.stanza);
	} finally {
		host.removeFile(envFile);
	}
}

/**
 * Ask the repository for its newest backup and decide the restore from it. Throws (so nothing
 * starts) when the repository cannot be read, and when a restore was left unfinished and the
 * repository now lists no backup: deciding "no backup" from either would start an empty or a
 * partial database over an installation's real history.
 */
function decideFromRepository(
	plan: ContainerCompanionPlan,
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	unfinished: boolean,
): PostizRestoreDecision {
	const newest = withOneShotEnv(plan, host, paths, (envFile, stanza) => {
		const info = oneShotPgBackRest(host, plan, paths, envFile, "info", [
			`--stanza=${stanza}`,
			"info",
			"--output=json",
		]);
		if (info.status !== 0) {
			const detail = commandDetail(info);
			throw new Error(
				`container companion ${plan.companion}: pgbackrest info before the first start failed${detail ? `: ${detail}` : ""}; refusing to start an empty database`,
			);
		}
		return parsePgBackRestInfo(info.stdout, stanza).backups.at(-1);
	});
	if (newest === undefined && unfinished) {
		throw new Error(
			`container companion ${plan.companion}: a restore was left unfinished and the repository now lists no backup; refusing to start the partial data`,
		);
	}
	const pin = plan.pin ?? { sha256: planAppDigest(plan), update: null };
	return decidePostizRestore(newest, { artifact: { sha256: pin.sha256 }, update: pin.update });
}

/**
 * Before an install is planned for good, ask the repository what an empty data directory will be
 * restored to (plan 06-48, OPS-01): the converge then re-plans at the decided app digest, so the
 * app that starts is the one that wrote the restored schema. Returns `null` when nothing would be
 * restored -- no backups configured, or a populated data directory (`PG_VERSION` present and no
 * restore left unfinished) -- without asking the repository anything.
 *
 * The plan's postgres image is verified first, because the one-shot container runs `--pull=never`.
 * Throws exactly where `restorePostizOnBootstrap` would: an unreadable repository, or an
 * unfinished restore over a repository that lists no backup.
 */
export function probePostizRestore(
	plan: ContainerCompanionPlan,
	host: ContainerHost,
	paths: ContainerCompanionPaths,
): PostizRestoreDecision | null {
	const need = restoreNeed(plan, host, paths);
	if (!need.needed) return null;
	const postgres = plan.images.find((image) => image.slot === "postgres");
	if (!postgres) throw new Error(`container companion ${plan.companion} has no postgres image`);
	ensureVerifiedImage(host, postgres);
	return decideFromRepository(plan, host, paths, need.unfinished);
}

/**
 * Restore an EMPTY data directory from the installation's repository before its first start.
 *
 * - No backups configured, or a populated directory (`PG_VERSION` present and no restore left
 *   unfinished): nothing is asked of the repository at all.
 * - Empty, and the stanza holds no backup (a first install): a fresh start.
 * - Empty, and the stanza holds a backup: the restore `plan.restore` decided (plan 06-48) --
 *   `restore --type=default` to the end of the archive, or `--set=<label> --type=immediate` for the
 *   pre-update set of a swap that was never confirmed -- with `--delta` over an unfinished one.
 *   The schema marker then names the app that wrote the restored schema, before anything else.
 *
 * A plan made without a decision decides here from the repository. It refuses by name when that
 * decision names another app digest than the plan's: re-planning is the converge's job, and
 * starting this plan's app over that schema is the downgrade plan 06-48 exists to prevent.
 *
 * Throws (so the apply fails and nothing starts) when the repository cannot be read or the
 * restore fails: deciding "no backup" from an unreadable repository would start an empty database
 * over an installation's real history.
 */
export function restorePostizOnBootstrap(
	plan: ContainerCompanionPlan,
	host: ContainerHost,
	paths: ContainerCompanionPaths,
): ContainerCompanionRestoreResult {
	const need = restoreNeed(plan, host, paths);
	if (!need.needed) return { restored: false, reason: need.reason };
	const decision = plan.restore ?? decideFromRepository(plan, host, paths, need.unfinished);
	if (decision.mode === "fresh") return { restored: false, reason: "no-backup-to-restore" };
	const running = planAppDigest(plan);
	if (decision.appDigest !== running) {
		throw new Error(
			`container companion ${plan.companion}: backup ${decision.label} holds the schema of app digest ${decision.appDigest}, not ${running} this plan would start; refusing to restore it for that app`,
		);
	}
	const dataRoot = databaseDataRoot(paths);
	const pgdata = join(dataRoot, ...PGDATA_SUBPATH);
	const marker = join(dataRoot, RESTORE_INCOMPLETE_MARKER);
	return withOneShotEnv(plan, host, paths, (envFile, stanza) => {
		// PGDATA must exist for pgBackRest; the image's entrypoint hands it to `postgres` at
		// start, and a restore run as the namespace's root sets each file's recorded owner.
		host.ensureDirectory(join(dataRoot, PGDATA_SUBPATH[0]), {
			mode: 0o755,
			uid: plan.identity.subordinateBase,
			gid: plan.identity.subordinateBase,
		});
		host.ensureDirectory(pgdata, {
			mode: 0o700,
			uid: plan.identity.subordinateBase,
			gid: plan.identity.subordinateBase,
		});
		host.writeFile(marker, `${decision.label}\n`, { mode: PRIVATE_FILE_MODE });
		const delta = need.unfinished ? ["--delta"] : [];
		const restored = oneShotPgBackRest(host, plan, paths, envFile, "restore", [
			`--stanza=${stanza}`,
			"restore",
			...(decision.mode === "set-immediate"
				? [...delta, `--set=${decision.label}`, "--type=immediate", "--target-action=promote"]
				: ["--type=default", ...delta]),
		]);
		if (restored.status !== 0) {
			const detail = commandDetail(restored);
			throw new Error(
				`container companion ${plan.companion}: pgbackrest restore of ${decision.label} failed${detail ? `: ${detail}` : ""}; the database was not started`,
			);
		}
		// Plan 06-48 (D-28): the restored schema was written by `decision.appDigest`, and the data
		// directory says so before anything else can fail or start.
		writeSchemaDigestMarker(host, paths, decision.appDigest);
		host.removeFile(marker);
		return { restored: true, label: decision.label };
	});
}

// ---------------------------------------------------------------------------------------------
// Withdrawal
// ---------------------------------------------------------------------------------------------

export interface ContainerCompanionWithdrawResult {
	withdrawn: boolean;
	stopped: string[];
	removed: string[];
}

const STILL_RUNNING_STATES = new Set(["active", "activating", "deactivating", "reloading"]);

/**
 * Take a container companion's units down WITHOUT touching its data (research Pitfall 12).
 *
 * Stops the generated services, removes the Quadlet sources this module generated and the
 * encrypted credentials, reloads so the generator forgets the units, and rewrites the receipt as a
 * `withdrawn` data-retention marker. The data directory is never read, written or removed here:
 * destroying a companion's data is the explicit `remove` lifecycle's job after the export offer
 * (plan 06-20), never a side effect of an entry disappearing from the manifest.
 *
 * A no-op, issuing no command at all, when there is nothing to withdraw. That matters because the
 * converge calls it on every run in which the kind is absent, on every host, including hosts that
 * have never had podman.
 */
export function withdrawContainerCompanion(
	kind: ContainerCompanionKind,
	host: ContainerHost,
	paths: ContainerCompanionPaths,
): ContainerCompanionWithdrawResult {
	const facts = readContainerCompanionFacts(host, paths, kind);
	const ownedSources = Object.entries(facts.sourcesOnDisk)
		.filter(([, content]) => content?.startsWith(GENERATED_CONTAINER_SOURCE_HEADER) === true)
		.map(([path]) => path);
	const credentials = Object.entries(facts.credentialsOnDisk)
		.filter(([, present]) => present)
		.map(([path]) => path);
	const receiptApplied = facts.receipt?.state === "applied";
	if (ownedSources.length === 0 && credentials.length === 0 && !receiptApplied) {
		return { withdrawn: false, stopped: [], removed: [] };
	}
	// The edge tunnel's service is stopped (first) only when this module rendered its source.
	const tunnelSource = join(paths.quadletRoot, containerCompanionUnitNames(kind).tunnelSource);
	const services = [
		...containerCompanionServices(kind, { tunnel: ownedSources.includes(tunnelSource) }),
	].reverse();
	// Stopping a unit that is not loaded exits non-zero, so the stop's own status is not the proof.
	// The proof is that nothing is still running afterwards; only then are the sources removed, so a
	// unit that refused to stop is never orphaned from its definition.
	host.run("systemctl", ["stop", ...services]);
	const running = services.filter((service) =>
		STILL_RUNNING_STATES.has(host.run("systemctl", ["is-active", service]).stdout.trim()),
	);
	if (running.length > 0) {
		throw new Error(
			`container companion ${kind} withdrawal could not stop ${running.join(", ")}; sources are left in place`,
		);
	}
	const removed = [...ownedSources, ...credentials];
	for (const path of removed) host.removeFile(path);
	mustRun(host, "systemd daemon-reload after withdrawal", "systemctl", ["daemon-reload"]);
	if (facts.receipt !== null) {
		const marker: ContainerCompanionReceipt = {
			...facts.receipt,
			state: "withdrawn",
			dataRoot: paths.dataRoot,
			dataRetained: true,
			withdrawnAt: host.now().toISOString(),
		};
		host.writeFile(paths.receiptPath, `${JSON.stringify(marker, null, 2)}\n`, {
			mode: PRIVATE_FILE_MODE,
		});
	}
	return { withdrawn: true, stopped: services, removed };
}

// ---------------------------------------------------------------------------------------------
// The explicit remove lifecycle (plan 06-20)
// ---------------------------------------------------------------------------------------------

export interface ContainerCompanionRemoveResult {
	withdrawal: ContainerCompanionWithdrawResult;
	/** Whether a data directory existed and was deleted by this call. */
	dataRemoved: boolean;
	dataRoot: string;
}

function refuseDataRemoval(kind: ContainerCompanionKind, dataRoot: string, why: string): never {
	throw new Error(
		`container companion ${kind}: refusing to delete ${JSON.stringify(dataRoot)}: ${why}`,
	);
}

/**
 * The one directory `remove` may delete, or `null` when it is already gone.
 *
 * The path must be exactly the kind's own data directory beside the platform state root -- the
 * same `/var/lib/iforge-postiz` `containerCompanionPaths` derives -- written in canonical form
 * (no `..`, no `.`, no trailing slash), and it must RESOLVE to exactly itself: a symlink anywhere
 * along it is refused rather than followed, so a link planted in place of the data directory can
 * never turn this into a recursive delete of something else (T-06-20-02).
 */
function removableDataRoot(
	kind: ContainerCompanionKind,
	host: ContainerHost,
	paths: ContainerCompanionPaths,
): string | null {
	const dataRoot = paths.dataRoot;
	const expected = join(
		dirname(dirname(dirname(paths.companionStateRoot))),
		companionServiceNames(kind).stateDirectory,
	);
	if (!isAbsolute(dataRoot) || normalize(dataRoot) !== dataRoot) {
		refuseDataRemoval(kind, dataRoot, "the path is not absolute and canonical");
	}
	if (dataRoot !== expected || dirname(dataRoot) === dataRoot) {
		refuseDataRemoval(kind, dataRoot, `it is not the ${kind} data directory ${expected}`);
	}
	const resolved = host.realPath(dataRoot);
	if (resolved === null) return null;
	if (resolved !== dataRoot) {
		refuseDataRemoval(kind, dataRoot, `it resolves to ${JSON.stringify(resolved)}`);
	}
	return dataRoot;
}

/**
 * Remove a container companion AND destroy its data -- only for the manifest's explicit
 * `lifecycle: "remove"` (research Pitfall 12, APP-07). An entry that merely disappears is
 * withdrawn by `withdrawContainerCompanion` and keeps its data.
 *
 * In order: the withdrawal (stop every generated service, remove the Quadlet sources and the
 * encrypted credentials, reload), then lift any mask a failed network migration left (06-REVIEW
 * WR-03), then the recursive delete of the data directory behind its path guard, then the loopback
 * and network guard (its nftables table, then its ruleset file; plan
 * 06-39), then the applied receipt. A withdrawal leaves the guard in place: nothing listens while
 * the units are withdrawn, and the guard costs nothing. The companion account and its `/etc/subuid` and `/etc/subgid`
 * lines are kept: a UID and its subordinate range are never reclaimed, so no later account can
 * inherit a file this one left anywhere (D-13, T-06-20-05). A failed unmask stops the removal
 * before anything is deleted, so the receipt keeps `removed` from being reported and the next
 * converge retries.
 *
 * Idempotent: with nothing left it deletes nothing and issues no command beyond a read-only
 * lookup of the guard's table, and the caller can still report `removed` (`reportPostizRemoved`).
 */
export function removeContainerCompanion(
	kind: ContainerCompanionKind,
	host: ContainerHost,
	paths: ContainerCompanionPaths,
): ContainerCompanionRemoveResult {
	// The path is checked before anything happens, and resolved again right before the delete.
	removableDataRoot(kind, host, paths);
	const withdrawal = withdrawContainerCompanion(kind, host, paths);
	liftContainerCompanionMask(
		host,
		paths,
		kind,
		containerCompanionServices(kind, { tunnel: true }),
		`unmask of ${kind} on removal`,
	);
	const dataRoot = removableDataRoot(kind, host, paths);
	if (dataRoot !== null) host.removeTree(dataRoot);
	// Only a host that has nft can hold the table; one that never did issues no command here.
	if (host.stat(NFT_BINARY_PATH)?.isFile === true) {
		const table = containerCompanionGuardTable(kind);
		if (host.run(NFT_BINARY_PATH, ["list", "table", "inet", table]).status === 0) {
			mustRun(host, `removal of the ${table} guard`, NFT_BINARY_PATH, [
				"delete",
				"table",
				"inet",
				table,
			]);
		}
	}
	host.removeFile(paths.guardPath);
	host.removeFile(paths.receiptPath);
	return { withdrawal, dataRemoved: dataRoot !== null, dataRoot: paths.dataRoot };
}

export type PostizRemovalReportOutcome =
	| { outcome: "reported"; status: number }
	/** The control plane refused it (plan 06-40); nothing on the host changes, and the next converge sends it again. */
	| { outcome: "refused"; code: string | null }
	| { outcome: "skipped"; reason: string }
	| { outcome: "unreportable"; reason: string };

/**
 * Tell the control plane the companion is gone from this host (plan 06-20): a `removed` report
 * for the entry carrying `lifecycle: "remove"`, once neither its data directory nor its applied
 * receipt exists any more. The control plane then tears down the companion's platform
 * resources and drops the entry. An already-absent installation reports `removed` too.
 */
export async function reportPostizRemoved(input: {
	manifest: RuntimeManifest;
	secretValues: Record<string, string> | undefined;
	paths: Pick<RuntimePaths, "serviceStateRoot" | "configurationRoot" | "runRoot">;
	host?: ContainerHost;
	reporter?: CompanionReportSink;
}): Promise<PostizRemovalReportOutcome> {
	const entry = input.manifest.companions?.postiz;
	if (entry?.lifecycle !== "remove") return { outcome: "skipped", reason: "no remove entry" };
	const host = input.host ?? nodeContainerHost();
	const paths = containerCompanionPaths(input.paths, entry.kind);
	if (host.readFile(paths.receiptPath) !== null || host.realPath(paths.dataRoot) !== null) {
		return { outcome: "skipped", reason: "removal not finished" };
	}
	const { reporter } = postizReporting(
		input.manifest,
		entry,
		input.secretValues ?? {},
		input.reporter,
	);
	if (reporter === null) {
		return { outcome: "unreportable", reason: "no runtime credential to report with" };
	}
	const result = await reporter.send({ operation: "uninstall", phase: "removed" });
	if (result.outcome === "refused") return { outcome: "refused", code: result.code };
	return { outcome: "reported", status: result.status };
}

// ---------------------------------------------------------------------------------------------
// The apply-failure record (plan 06-37, CR-04)
// ---------------------------------------------------------------------------------------------
//
// One container companion that cannot be applied must never cost the agent its converge, and an
// install it cannot apply must fail by name at once instead of sitting `running` until the
// control plane's deadline fails it with a generic message. The converge (which runs inside the
// manifest apply) catches the kind's refusal and writes it here, in the root-only state root; the
// post-converge reporter reads it back and sends ONE `failed` report. That is the same handoff the
// applied receipt already uses, so the manifest apply's return type is not threaded through.

const APPLY_FAILURE_SCHEMA = "iforge.containerCompanionApplyFailure.v1";

/** Bounds the record; the reporter cuts the error to the route's own, smaller bound anyway. */
const APPLY_FAILURE_MESSAGE_MAX = 8_192;

export interface ContainerCompanionApplyFailure {
	schema: typeof APPLY_FAILURE_SCHEMA;
	kind: ContainerCompanionKind;
	/** The installation the manifest's entry names, or `null` when it names none. */
	installationId: string | null;
	/** What the converge was doing: applying, removing on instruction, or withdrawing. */
	lifecycle: "present" | "remove" | "absent";
	/** The refusal, redacted of every known secret value BEFORE it was written. */
	message: string;
	failedAt: string;
	/** When the failure was delivered as a `failed` install report; it is never sent twice. */
	reportedAt?: string;
}

function applyFailurePath(paths: ContainerCompanionPaths): string {
	return join(paths.companionStateRoot, "apply-failure.json");
}

/**
 * Describe the kind's refusal on this converge, redacted the way its install reports are: every
 * composed value the entry names and the runtime's own credential (T-06-37-02).
 */
export function containerCompanionApplyFailure(input: {
	manifest: RuntimeManifest;
	secretValues: Record<string, string> | undefined;
	error: unknown;
	now: Date;
}): ContainerCompanionApplyFailure {
	const entry = input.manifest.companions?.postiz;
	const lifecycle =
		entry?.lifecycle === "present" || entry?.lifecycle === "remove" ? entry.lifecycle : "absent";
	const { redact } = installationRedaction(entry, input.secretValues ?? {});
	const raw = input.error instanceof Error ? input.error.message : String(input.error);
	return {
		schema: APPLY_FAILURE_SCHEMA,
		kind: CONTAINER_COMPANION_KIND,
		installationId: entry?.installationId ?? null,
		lifecycle,
		message: redact(raw).slice(0, APPLY_FAILURE_MESSAGE_MAX) || "failed",
		failedAt: input.now.toISOString(),
	};
}

/** The recorded failure, or `null` when there is none (a corrupt or foreign file reads as none). */
export function readContainerCompanionApplyFailure(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
): ContainerCompanionApplyFailure | null {
	const raw = host.readFile(applyFailurePath(paths));
	if (raw === null) return null;
	try {
		const value = JSON.parse(raw) as Partial<ContainerCompanionApplyFailure>;
		if (
			value?.schema !== APPLY_FAILURE_SCHEMA ||
			value.kind !== CONTAINER_COMPANION_KIND ||
			(value.installationId !== null && typeof value.installationId !== "string") ||
			(value.lifecycle !== "present" &&
				value.lifecycle !== "remove" &&
				value.lifecycle !== "absent") ||
			typeof value.message !== "string" ||
			typeof value.failedAt !== "string" ||
			(value.reportedAt !== undefined && typeof value.reportedAt !== "string")
		) {
			return null;
		}
		return value as ContainerCompanionApplyFailure;
	} catch {
		return null;
	}
}

/**
 * Write the failure root-only (0700 directory, 0600 file). A converge that fails again for the
 * same installation keeps the earlier delivery mark, so a delivered report is never repeated.
 */
export function recordContainerCompanionApplyFailure(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	failure: ContainerCompanionApplyFailure,
): void {
	const previous = readContainerCompanionApplyFailure(host, paths);
	const reportedAt =
		previous?.reportedAt !== undefined &&
		previous.lifecycle === failure.lifecycle &&
		previous.installationId === failure.installationId
			? previous.reportedAt
			: undefined;
	const record: ContainerCompanionApplyFailure = {
		...failure,
		...(reportedAt !== undefined ? { reportedAt } : {}),
	};
	if (reportedAt === undefined) delete record.reportedAt;
	host.ensureDirectory(paths.companionStateRoot, { mode: 0o700, uid: 0, gid: 0 });
	host.writeFile(applyFailurePath(paths), `${JSON.stringify(record, null, 2)}\n`, {
		mode: PRIVATE_FILE_MODE,
	});
}

/** Forget the recorded failure: the installation it belonged to has since applied. */
export function clearContainerCompanionApplyFailure(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
): void {
	host.removeFile(applyFailurePath(paths));
}

export type ContainerCompanionApplyFailureReportOutcome =
	| { outcome: "reported"; status: number }
	| { outcome: "refused"; code: string | null; terminalStatus: string | null }
	| { outcome: "skipped"; reason: string }
	| { outcome: "logged-only"; message: string }
	| { outcome: "unreportable"; reason: string };

/**
 * Fail the install by name (plan 06-37, APP-01, D-26): send the converge's recorded refusal as
 * ONE `failed` report at step `apply` for the installation the present entry names, then mark it
 * delivered so no later converge repeats it.
 *
 * Skipped when nothing is recorded, when the record was already delivered, or when it belongs to
 * another lifecycle or installation than the manifest's present entry -- a report is only ever
 * sent for the installation the control plane asked this host to run (T-06-37-03). A refused
 * remove or withdrawal is therefore never reported as an install failure.
 *
 * `logged-only` when this installation's receipt is applied and already reported ready: the
 * install succeeded, so a later re-apply's failure is the host's to log, not the install's.
 * `unreportable` when the send threw; the record stays undelivered and the next converge retries.
 * `refused` (plan 06-40) when the control plane answered with a refusal: marked delivered like a
 * recorded report, because a refusal is never retried, but never claimed as `reported`.
 */
export async function reportContainerCompanionApplyFailure(input: {
	manifest: RuntimeManifest;
	secretValues: Record<string, string> | undefined;
	paths: Pick<RuntimePaths, "serviceStateRoot" | "configurationRoot" | "runRoot">;
	host?: ContainerHost;
	reporter?: CompanionReportSink;
}): Promise<ContainerCompanionApplyFailureReportOutcome> {
	const host = input.host ?? nodeContainerHost();
	const paths = containerCompanionPaths(input.paths, CONTAINER_COMPANION_KIND);
	const failure = readContainerCompanionApplyFailure(host, paths);
	if (failure === null) return { outcome: "skipped", reason: "no recorded apply failure" };
	if (failure.reportedAt !== undefined) {
		return { outcome: "skipped", reason: "the apply failure was already reported" };
	}
	const entry = input.manifest.companions?.postiz;
	if (entry?.lifecycle !== "present") return { outcome: "skipped", reason: "no present entry" };
	if (failure.lifecycle !== "present" || failure.installationId !== entry.installationId) {
		return { outcome: "skipped", reason: "the recorded failure is not this installation's" };
	}
	const receipt = parseReceipt(host.readFile(paths.receiptPath));
	if (
		receipt?.state === "applied" &&
		receipt.installationId === entry.installationId &&
		receipt.bootstrap?.phase === "ready"
	) {
		// This installation already reported ready and its units still run on the applied receipt
		// (the apply rolled back to it). A later re-apply failing -- a credential rotation, a host
		// probe -- is not an install failure: logged by name on the host, never reported as one.
		return { outcome: "logged-only", message: failure.message };
	}
	const { reporter, redact } = postizReporting(
		input.manifest,
		entry,
		input.secretValues ?? {},
		input.reporter,
	);
	if (reporter === null) {
		return { outcome: "unreportable", reason: "no runtime credential to report with" };
	}
	let result: CompanionReportResult;
	try {
		// `already-terminal` (409, the install already completed) counts as delivered: a finished
		// install is never flipped. A refusal is answered `refused` below, never `reported`.
		result = await reporter.send({
			operation: "install",
			phase: "failed",
			step: "apply",
			error: redact(failure.message) || "failed",
		});
	} catch (error) {
		// Not marked delivered: the next converge's report pass sends it again.
		return {
			outcome: "unreportable",
			reason: redact(error instanceof Error ? error.message : String(error)),
		};
	}
	// Marked delivered for a refusal too: it is the control plane's answer, and it is never retried.
	// A new failure record is reported afresh.
	host.writeFile(
		applyFailurePath(paths),
		`${JSON.stringify({ ...failure, reportedAt: host.now().toISOString() }, null, 2)}\n`,
		{ mode: PRIVATE_FILE_MODE },
	);
	if (result.outcome === "refused") {
		return { outcome: "refused", code: result.code, terminalStatus: result.terminalStatus };
	}
	return { outcome: "reported", status: result.status };
}

// ---------------------------------------------------------------------------------------------
// Host conditions (plan 06-45, G-06-2)
// ---------------------------------------------------------------------------------------------
//
// A host condition is the runtime's report of a state one installation must be repaired from: a
// rollback that did not finish, a pinned app over a schema another digest wrote, a companion
// network whose live subnet is unverified. It is never a job state (D-26): the control plane
// records it under the installation's `host_conditions` in any status (plan 06-44).
//
// The converge and the update pass raise and clear conditions here, in the root-only state root;
// the post-converge pass reads them back and sends each unreported state ONCE. That is the
// apply-failure record's handoff, with one entry per (installation, code) and two states.

const CONDITIONS_SCHEMA = "iforge.containerCompanionConditions.v1";

/** Bounds a stored detail; the reporter cuts it to the route's own, smaller bound anyway. */
const CONDITION_DETAIL_MAX = 8_192;

const HOST_CONDITION_CODES: ReadonlySet<string> = new Set<CompanionHostConditionCode>([
	"update_rollback_incomplete",
	"schema_digest_mismatch",
	"network_subnet_unverified",
]);

export interface ContainerCompanionCondition {
	installationId: string;
	code: CompanionHostConditionCode;
	state: "raised" | "cleared";
	/** Already redacted by the caller that raised it; always `null` for `cleared`. */
	detail: string | null;
	/** When this state was raised or cleared. */
	at: string;
	/** When this state was delivered; it is never sent twice. */
	reportedAt?: string;
}

interface ContainerCompanionConditionsRecord {
	schema: typeof CONDITIONS_SCHEMA;
	kind: ContainerCompanionKind;
	conditions: ContainerCompanionCondition[];
}

function conditionsPath(paths: ContainerCompanionPaths): string {
	return join(paths.companionStateRoot, "conditions.json");
}

function isContainerCompanionCondition(value: unknown): value is ContainerCompanionCondition {
	if (typeof value !== "object" || value === null) return false;
	const entry = value as Partial<ContainerCompanionCondition>;
	return (
		typeof entry.installationId === "string" &&
		typeof entry.code === "string" &&
		HOST_CONDITION_CODES.has(entry.code) &&
		(entry.state === "raised" || entry.state === "cleared") &&
		(entry.detail === null || typeof entry.detail === "string") &&
		typeof entry.at === "string" &&
		(entry.reportedAt === undefined || typeof entry.reportedAt === "string")
	);
}

/**
 * Every recorded condition, in record order. An absent, corrupt or foreign file, or one with any
 * entry that fails the field checks or repeats an (installation, code), reads as none: a planted
 * or damaged record never makes the host send a condition it did not raise (T-06-45-02).
 */
export function readContainerCompanionConditions(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
): ContainerCompanionCondition[] {
	const raw = host.readFile(conditionsPath(paths));
	if (raw === null) return [];
	try {
		const value = JSON.parse(raw) as Partial<ContainerCompanionConditionsRecord>;
		if (
			value?.schema !== CONDITIONS_SCHEMA ||
			value.kind !== CONTAINER_COMPANION_KIND ||
			!Array.isArray(value.conditions) ||
			!value.conditions.every(isContainerCompanionCondition)
		) {
			return [];
		}
		const keys = new Set(value.conditions.map((entry) => `${entry.installationId}\n${entry.code}`));
		return keys.size === value.conditions.length ? value.conditions : [];
	} catch {
		return [];
	}
}

/** Write the record root-only (0700 directory, 0600 file); an empty record removes the file. */
function writeContainerCompanionConditions(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	conditions: readonly ContainerCompanionCondition[],
): void {
	if (conditions.length === 0) {
		host.removeFile(conditionsPath(paths));
		return;
	}
	const record: ContainerCompanionConditionsRecord = {
		schema: CONDITIONS_SCHEMA,
		kind: CONTAINER_COMPANION_KIND,
		conditions: [...conditions],
	};
	host.ensureDirectory(paths.companionStateRoot, { mode: 0o700, uid: 0, gid: 0 });
	host.writeFile(conditionsPath(paths), `${JSON.stringify(record, null, 2)}\n`, {
		mode: PRIVATE_FILE_MODE,
		uid: 0,
		gid: 0,
	});
}

function sameCondition(
	entry: ContainerCompanionCondition,
	key: { installationId: string; code: CompanionHostConditionCode },
): boolean {
	return entry.installationId === key.installationId && entry.code === key.code;
}

/**
 * Raise a condition on one installation. The caller passes an ALREADY-REDACTED detail
 * (`installationRedaction`); the reporter redacts it again before sending.
 *
 * Raising the entry exactly as it is already raised writes nothing and keeps its delivery mark, so
 * a condition re-raised on every converge is sent once. Anything else -- a new detail, a cleared
 * entry, no entry -- replaces it, raised and unreported.
 */
export function raiseContainerCompanionCondition(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	input: { installationId: string; code: CompanionHostConditionCode; detail: string | null },
): void {
	const detail =
		input.detail === null || input.detail.length === 0
			? null
			: input.detail.slice(0, CONDITION_DETAIL_MAX);
	const conditions = readContainerCompanionConditions(host, paths);
	const index = conditions.findIndex((entry) => sameCondition(entry, input));
	const current = index >= 0 ? conditions[index] : undefined;
	if (current?.state === "raised" && current.detail === detail) return;
	const raised: ContainerCompanionCondition = {
		installationId: input.installationId,
		code: input.code,
		state: "raised",
		detail,
		at: host.now().toISOString(),
	};
	if (index >= 0) conditions[index] = raised;
	else conditions.push(raised);
	writeContainerCompanionConditions(host, paths, conditions);
}

/**
 * Clear a raised condition: it becomes `cleared`, with no detail and unreported, so the control
 * plane is told once. Clearing an absent or already-cleared entry writes nothing.
 */
export function clearContainerCompanionCondition(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	input: { installationId: string; code: CompanionHostConditionCode },
): void {
	const conditions = readContainerCompanionConditions(host, paths);
	const index = conditions.findIndex((entry) => sameCondition(entry, input));
	if (index < 0 || conditions[index]?.state !== "raised") return;
	conditions[index] = {
		installationId: input.installationId,
		code: input.code,
		state: "cleared",
		detail: null,
		at: host.now().toISOString(),
	};
	writeContainerCompanionConditions(host, paths, conditions);
}

/** The operation a condition is reported under: the update's for a rollback, else the install's. */
function conditionOperation(code: CompanionHostConditionCode): CompanionInstallOperation {
	return code === "update_rollback_incomplete" ? "update" : "install";
}

export type ContainerCompanionConditionsReportOutcome =
	| { outcome: "skipped"; reason: string }
	| { outcome: "unreportable"; reason: string }
	| {
			outcome: "reported";
			sent: Array<{
				code: CompanionHostConditionCode;
				state: "raised" | "cleared";
				result: CompanionReportResult["outcome"] | "kept-due";
			}>;
	  };

/**
 * Send each unreported condition of the present entry's installation as one plan 06-44
 * `condition` report, in record order, through that installation's own reporter (T-06-45-04).
 *
 * `recorded`, `already-terminal` and `refused` all mark the state delivered: a refusal is the
 * control plane's answer and is never retried (T-06-45-03). A final 4xx rejection (400 or 422,
 * 06-REVIEW WR-02) is sent as `refused` too, so one rejected state never blocks the ones after it.
 * A 404 means the runtime key reaches no installation right now (a binding not resolved yet), so
 * it leaves that state due for the next pass, sent as `kept-due`, while the next condition still
 * goes (06-REVIEW WR-04). A delivered clear is forgotten. The record is written after EACH
 * delivered send, so a later throw cannot resend an earlier state; any other throw (network, 5xx,
 * 429, 401, 403) leaves that state and every later one for the next pass (`unreportable`). Another
 * installation's entries are neither sent nor changed.
 */
export async function reportContainerCompanionConditions(input: {
	manifest: RuntimeManifest;
	secretValues: Record<string, string> | undefined;
	paths: Pick<RuntimePaths, "serviceStateRoot" | "configurationRoot" | "runRoot">;
	host?: ContainerHost;
	reporter?: CompanionReportSink;
}): Promise<ContainerCompanionConditionsReportOutcome> {
	const host = input.host ?? nodeContainerHost();
	const paths = containerCompanionPaths(input.paths, CONTAINER_COMPANION_KIND);
	const entry = input.manifest.companions?.postiz;
	if (entry?.lifecycle !== "present") return { outcome: "skipped", reason: "no present entry" };
	const due = readContainerCompanionConditions(host, paths).filter(
		(condition) =>
			condition.installationId === entry.installationId && condition.reportedAt === undefined,
	);
	if (due.length === 0) return { outcome: "skipped", reason: "no unreported host condition" };
	const { reporter, redact } = postizReporting(
		input.manifest,
		entry,
		input.secretValues ?? {},
		input.reporter,
	);
	if (reporter === null) {
		return { outcome: "unreportable", reason: "no runtime credential to report with" };
	}
	const sent: Extract<ContainerCompanionConditionsReportOutcome, { outcome: "reported" }>["sent"] =
		[];
	for (const condition of due) {
		let outcome: CompanionReportResult["outcome"];
		try {
			const result = await reporter.send({
				operation: conditionOperation(condition.code),
				phase: "condition",
				condition: {
					code: condition.code,
					state: condition.state,
					// Redacted again here, and once more by the installation's reporter: a caller that
					// forgot to redact never sends a known secret (T-06-45-01).
					detail:
						condition.state === "raised" && condition.detail !== null
							? redact(condition.detail) || null
							: null,
				},
			});
			outcome = result.outcome;
		} catch (error) {
			if (error instanceof CompanionReportRejectedError && error.keepDue) {
				// 06-REVIEW WR-04: a 404 is the key's binding not resolving yet, not an answer about
				// this report. The state stays due for the next pass, and the next condition still goes.
				sent.push({ code: condition.code, state: condition.state, result: "kept-due" });
				continue;
			}
			if (!(error instanceof CompanionReportRejectedError && error.final)) {
				// Not marked delivered: the next pass sends this state and every later one again.
				return {
					outcome: "unreportable",
					reason: redact(error instanceof Error ? error.message : String(error)),
				};
			}
			// 06-REVIEW WR-02: a 400/422 is the control plane's final answer about this report
			// (an older backend that does not know the phase, or a report it cannot parse).
			// Re-sending cannot change it, so it is settled like a refusal and the next condition
			// still goes: one rejected report never blocks the ones queued after it.
			outcome = "refused";
		}
		sent.push({ code: condition.code, state: condition.state, result: outcome });
		// Re-read, and mark only the state that was sent: one raised or cleared since is left due.
		const conditions = readContainerCompanionConditions(host, paths);
		const index = conditions.findIndex(
			(stored) =>
				sameCondition(stored, condition) &&
				stored.state === condition.state &&
				stored.detail === condition.detail &&
				stored.at === condition.at,
		);
		if (index < 0) continue;
		if (condition.state === "cleared") conditions.splice(index, 1);
		else conditions[index] = { ...condition, reportedAt: host.now().toISOString() };
		writeContainerCompanionConditions(host, paths, conditions);
	}
	return { outcome: "reported", sent };
}

// ---------------------------------------------------------------------------------------------
// Bootstrap, data-asserted readiness and the install report (plan 06-12)
// ---------------------------------------------------------------------------------------------
//
// After the units start, the runtime finishes the install over LOOPBACK, before anything can
// reach the app from outside:
//
//   readiness (is-connected answers 401) → first-registrant bootstrap → API key seeded through
//   psql standard input → admin user id read → is-connected WITH the key → organization count →
//   ready report carrying that assertion.
//
// Why this order is the contract:
// - `DISABLE_REGISTRATION=true` still admits the FIRST registrant (research Pitfall 3). The
//   platform must be that registrant, over loopback, before plan 06-14 routes a hostname; that
//   plan publishes only after this report, so the window in which a stranger could claim the
//   owner account never opens (T-06-12-01).
// - "Ready" is evidence, not an HTTP status (D-16). `/` answers 307 even while the backend is
//   dead, so it is never consulted. Ready means Postiz answered `connected` to the seeded key AND
//   exactly one organization exists (T-06-12-04).
// - No secret reaches a command line (T-06-12-02). The key is bound as a psql variable from a
//   script read on standard input and quoted with `:'key'` (T-06-12-05); the admin password
//   travels only in the loopback register body.

/** The step an install report names; a failure is reported by the step it happened in. */
export type PostizBootstrapStep =
	| "restore"
	| "readiness"
	| "backup"
	| "bootstrap"
	| "key-seed"
	| "admin-user"
	| "data-assertion";

export class PostizBootstrapError extends Error {
	constructor(
		readonly step: PostizBootstrapStep,
		message: string,
	) {
		super(message);
		this.name = "PostizBootstrapError";
	}
}

/** Everything the bootstrap touches besides the host: loopback HTTP and a clock. */
export interface PostizLoopback {
	fetch: CompanionFetch;
	sleep(ms: number): Promise<void>;
	now(): number;
	readinessBudgetMs?: number;
	pollIntervalMs?: number;
}

/** A progress line for the install log; best effort, never fails the bootstrap. */
export type PostizProgress = (step: PostizBootstrapStep, message: string) => Promise<void>;

const NO_PROGRESS: PostizProgress = async () => {};

export interface PostizBootstrapResult {
	/** Whether this run registered the owning organization (false on a re-converge). */
	registered: boolean;
	adminUserId: string;
}

/** How long the backend has to answer is-connected after the units start. */
export const POSTIZ_READINESS_BUDGET_MS = 300_000;
export const POSTIZ_READINESS_POLL_MS = 5_000;
const POSTIZ_REQUEST_TIMEOUT_MS = 15_000;
const POSTIZ_IS_CONNECTED_PATH = "/api/public/v1/is-connected";
const POSTIZ_REGISTER_PATH = "/api/auth/register";
/**
 * The loopback sign-in the ownership proof asks (plan 06-38, CR-02). Postiz v2.24.0
 * `auth.controller.ts` `@Post('/login')` answers 200 `{"login": true}` with an `auth` cookie and
 * header on success; the proof reads only the status and the `login` field.
 */
const POSTIZ_LOGIN_PATH = "/api/auth/login";
/** Plan 06-10's composition: the database role and name are fixed, never credentials. */
const POSTIZ_DB_USER = "postiz";
const POSTIZ_DB_NAME = "postiz";
/** The generated key is url-safe; anything else is refused before it reaches psql. */
const POSTIZ_API_KEY_PATTERN = /^[A-Za-z0-9._~-]{1,512}$/;
/** Postiz's `CreateOrgUserDto` bounds (`@MinLength(3)`, `@MaxLength(64)`). */
const POSTIZ_PASSWORD_MIN = 3;
const POSTIZ_PASSWORD_MAX = 64;

function postizOrigin(entry: PostizCompanionManifest): string {
	// Loopback only, and the port is the contract's literal: nothing here is read from a URL.
	return `http://127.0.0.1:${entry.port}`;
}

interface PostizAdminCredential {
	email: string;
	password: string;
	company: string;
}

/** The admin bootstrap credential plan 06-10 composes. Errors name the reference, never a value. */
function postizAdminCredential(
	entry: PostizCompanionManifest,
	secretValues: Record<string, string>,
): PostizAdminCredential {
	const ref = entry.credentials.adminSecretRef;
	const raw = runtimeSecretValue(secretValues, ref);
	if (raw === null) {
		throw new PostizBootstrapError("bootstrap", `admin credential ${ref} has no secret value`);
	}
	let parsed: unknown;
	try {
		parsed = JSON.parse(raw);
	} catch {
		throw new PostizBootstrapError("bootstrap", `admin credential ${ref} is not a JSON object`);
	}
	const value = (parsed ?? {}) as Partial<Record<keyof PostizAdminCredential, unknown>>;
	const { email, password, company } = value;
	if (
		typeof email !== "string" ||
		email.length === 0 ||
		typeof company !== "string" ||
		company.length === 0 ||
		typeof password !== "string" ||
		password.length < POSTIZ_PASSWORD_MIN ||
		password.length > POSTIZ_PASSWORD_MAX
	) {
		throw new PostizBootstrapError(
			"bootstrap",
			`admin credential ${ref} must carry an email, a company and a ${POSTIZ_PASSWORD_MIN}-${POSTIZ_PASSWORD_MAX} character password`,
		);
	}
	return { email, password, company };
}

function postizApiKey(
	entry: PostizCompanionManifest,
	secretValues: Record<string, string>,
): string {
	const ref = entry.credentials.apiKeySecretRef;
	const key = runtimeSecretValue(secretValues, ref);
	if (key === null || !POSTIZ_API_KEY_PATTERN.test(key)) {
		throw new PostizBootstrapError(
			"key-seed",
			`API key ${ref} is absent or not url-safe; refusing to seed it`,
		);
	}
	return key;
}

/**
 * A value as a psql meta-command argument: single-quoted, with `\` and `'` escaped as psql's
 * own quoting rules require. Control characters are refused outright, so a value can never end
 * the `\set` line and start a new statement.
 */
function psqlQuoted(value: string): string {
	for (const character of value) {
		const code = character.charCodeAt(0);
		if (code <= 0x1f || code === 0x7f) {
			throw new PostizBootstrapError("key-seed", "a psql variable carried a control character");
		}
	}
	return `'${value.replace(/\\/g, "\\\\").replace(/'/g, "''")}'`;
}

/**
 * Run a psql script inside the installation's database container, read from STANDARD INPUT.
 *
 * The argv is fixed and carries no value: variables are bound with `\set` inside the script and
 * used through `:'name'`, so a secret never appears in `/proc/<pid>/cmdline`. Connects over the
 * container's local socket, so no database password is needed either.
 */
function runPostizPsql(
	host: ContainerHost,
	entry: PostizCompanionManifest,
	step: PostizBootstrapStep,
	what: string,
	script: string,
): string[] {
	const container = containerCompanionUnitNames(entry.kind).databaseContainer;
	const result = host.run(
		"podman",
		[
			"exec",
			"-i",
			container,
			"psql",
			"-X",
			"-q",
			"-t",
			"-A",
			"-v",
			"ON_ERROR_STOP=1",
			"-U",
			POSTIZ_DB_USER,
			"-d",
			POSTIZ_DB_NAME,
		],
		{ input: script },
	);
	if (result.status !== 0) {
		const detail = commandDetail(result);
		throw new PostizBootstrapError(step, `psql ${what} failed${detail ? `: ${detail}` : ""}`);
	}
	return result.stdout
		.split("\n")
		.map((line) => line.trim())
		.filter((line) => line.length > 0);
}

async function postizRequest(
	loopback: PostizLoopback,
	entry: PostizCompanionManifest,
	path: string,
	init: RequestInit,
): Promise<Response> {
	return loopback.fetch(`${postizOrigin(entry)}${path}`, {
		...init,
		// Loopback answers are read as-is; a redirect is never followed anywhere.
		redirect: "manual",
		signal: AbortSignal.timeout(POSTIZ_REQUEST_TIMEOUT_MS),
	});
}

/**
 * Wait until Postiz's BACKEND is up: `is-connected` without a key answers 401.
 *
 * Nothing else counts. A refused connection, a 502 from the in-container nginx, or any other
 * status means the backend is not serving yet; `/` is never asked because it answers 307 even
 * while the backend process is dead.
 */
async function waitForPostizBackend(
	entry: PostizCompanionManifest,
	loopback: PostizLoopback,
): Promise<void> {
	const budget = loopback.readinessBudgetMs ?? POSTIZ_READINESS_BUDGET_MS;
	const interval = loopback.pollIntervalMs ?? POSTIZ_READINESS_POLL_MS;
	const deadline = loopback.now() + budget;
	let last = "no answer";
	for (;;) {
		try {
			const response = await postizRequest(loopback, entry, POSTIZ_IS_CONNECTED_PATH, {
				method: "GET",
			});
			if (response.status === 401) return;
			last = `HTTP ${response.status}`;
		} catch (error) {
			last = error instanceof Error ? error.message : String(error);
		}
		if (loopback.now() >= deadline) {
			throw new PostizBootstrapError(
				"readiness",
				`the Postiz backend did not answer is-connected within ${Math.round(budget / 1000)} s (last: ${last})`,
			);
		}
		await loopback.sleep(interval);
	}
}

/**
 * Register the owning organization over loopback (or prove an existing one is the platform's),
 * seed the platform API key, and read the admin user id. Throws `PostizBootstrapError` naming the
 * step that failed.
 */
export async function bootstrapPostiz(
	entry: PostizCompanionManifest,
	secretValues: Record<string, string>,
	host: ContainerHost,
	loopback: PostizLoopback,
	progress: PostizProgress = NO_PROGRESS,
): Promise<PostizBootstrapResult> {
	await progress("readiness", "waiting for the Postiz backend to answer on loopback");
	await waitForPostizBackend(entry, loopback);
	await progress("readiness", "the Postiz backend is answering on loopback");
	const admin = postizAdminCredential(entry, secretValues);
	const key = postizApiKey(entry, secretValues);

	// Idempotent re-convergence: the database decides whether an organization already exists, and
	// adopting one requires proof that the platform registered it (plan 06-38, CR-02). Postiz
	// admits the FIRST registrant even with registration disabled (research Pitfall 3), so a
	// local process that registered first -- the tenant's agent, with the owner's email and a
	// password it chose -- must fail the install here, never receive the platform's key.
	// Asking first also keeps a re-converge from racing a stranger for an empty database.
	const existing = countPostizOrganizations(host, entry);
	if (existing > 1) {
		throw new PostizBootstrapError(
			"data-assertion",
			`the Postiz database already holds ${existing} organizations; refusing to register or seed a key into one the platform may not own`,
		);
	}
	const registered = existing === 0;
	if (registered) {
		await progress("bootstrap", "registering the owning organization over loopback");
		const response = await postizRequest(loopback, entry, POSTIZ_REGISTER_PATH, {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({
				email: admin.email,
				password: admin.password,
				provider: "LOCAL",
				company: admin.company,
			}),
		});
		if (response.status !== 200) {
			throw new PostizBootstrapError(
				"bootstrap",
				`Postiz refused the first registration over loopback: HTTP ${response.status}`,
			);
		}
	} else {
		await progress("bootstrap", "already bootstrapped; registration skipped");
		await progress("bootstrap", "proving the existing organization is the platform's");
	}
	// Proven BEFORE the key seed: a key seeded into an organization the platform did not
	// register would hand the platform's key holder's authority to whoever registered it.
	const proven = registered ? null : await provePostizOwnership(entry, host, loopback, admin);

	// Re-asserted on every run, so a rotated key reaches the database idempotently.
	await progress("key-seed", "seeding the platform API key");
	seedPostizApiKey(host, entry, key);
	const adminUserId = proven ?? readPostizAdminUserId(host, entry, admin.email);
	return { registered, adminUserId };
}

/** Why an existing organization is refused; never carries a credential, a body or a header. */
const POSTIZ_FOREIGN_ORGANIZATION =
	"the existing Postiz organization is not the one the platform registered";

/**
 * Prove the only existing organization is the platform's before adopting it, and return the
 * proven admin user id. Anything short of proof throws at step `data-assertion`.
 *
 * Installation that reached ready (`entry.adminUserId` is the id the control plane recorded from
 * the ready report): the Postiz database must hold a User row with exactly that id. No sign-in is
 * attempted, because CK-D lets the tenant replace the generated password, and a credential
 * rotation or an instance replacement must still re-bootstrap afterwards. A foreign registrant
 * cannot produce the platform user's random id.
 *
 * Never-ready installation (`adminUserId` null): the platform-generated admin credential must sign
 * in over loopback (200 with `login: true`). Only the status and that one field are read; the
 * session cookie and headers Postiz returns are discarded unread and reach no report, log or error.
 */
async function provePostizOwnership(
	entry: PostizCompanionManifest,
	host: ContainerHost,
	loopback: PostizLoopback,
	admin: PostizAdminCredential,
): Promise<string> {
	if (entry.adminUserId !== null) {
		const rows = runPostizPsql(
			host,
			entry,
			"data-assertion",
			"reading the recorded admin user",
			[
				`\\set id ${psqlQuoted(entry.adminUserId)}`,
				`SELECT "id" FROM "User" WHERE "id" = :'id';`,
				"",
			].join("\n"),
		);
		if (rows.length !== 1 || rows[0] !== entry.adminUserId) {
			throw new PostizBootstrapError(
				"data-assertion",
				`${POSTIZ_FOREIGN_ORGANIZATION}: its recorded owner account is absent`,
			);
		}
		return entry.adminUserId;
	}
	let status: number | "unreachable" = "unreachable";
	let signedIn = false;
	try {
		const response = await postizRequest(loopback, entry, POSTIZ_LOGIN_PATH, {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({ email: admin.email, password: admin.password, provider: "LOCAL" }),
		});
		status = response.status;
		if (response.status === 200) {
			// A body that does not parse is no proof.
			const body = (await response.json().catch(() => null)) as { login?: unknown } | null;
			signedIn = body !== null && typeof body === "object" && body.login === true;
		} else {
			await response.body?.cancel().catch(() => {});
		}
	} catch {
		// A sign-in that could not be asked proves nothing; the error text is not carried.
		signedIn = false;
	}
	if (!signedIn) {
		throw new PostizBootstrapError(
			"data-assertion",
			`${POSTIZ_FOREIGN_ORGANIZATION}: the platform's admin credential does not sign in (${
				status === "unreachable" ? "no answer" : `HTTP ${status}`
			})`,
		);
	}
	return readPostizAdminUserId(host, entry, admin.email);
}

/**
 * `UPDATE "Organization" SET "apiKey" = :'key'`, guarded so it only ever writes when exactly one
 * organization exists: a key seeded into an organization the platform did not register would
 * hand that organization to the platform's key holder.
 */
function seedPostizApiKey(host: ContainerHost, entry: PostizCompanionManifest, key: string): void {
	const updated = runPostizPsql(
		host,
		entry,
		"key-seed",
		"seeding the API key",
		[
			`\\set key ${psqlQuoted(key)}`,
			`UPDATE "Organization" SET "apiKey" = :'key' WHERE (SELECT count(*) FROM "Organization") = 1 RETURNING "id";`,
			"",
		].join("\n"),
	);
	if (updated.length !== 1) {
		throw new PostizBootstrapError(
			"data-assertion",
			`expected exactly one organization to seed the API key into; ${updated.length} were updated`,
		);
	}
}

function readPostizAdminUserId(
	host: ContainerHost,
	entry: PostizCompanionManifest,
	email: string,
): string {
	const rows = runPostizPsql(
		host,
		entry,
		"admin-user",
		"reading the admin user",
		[
			`\\set email ${psqlQuoted(email)}`,
			`SELECT "id" FROM "User" WHERE lower("email") = lower(:'email');`,
			"",
		].join("\n"),
	);
	const [id] = rows;
	if (rows.length !== 1 || id === undefined || id.length > 64) {
		throw new PostizBootstrapError(
			"admin-user",
			`expected exactly one admin user for the bootstrap email; found ${rows.length}`,
		);
	}
	return id;
}

function countPostizOrganizations(host: ContainerHost, entry: PostizCompanionManifest): number {
	const rows = runPostizPsql(
		host,
		entry,
		"data-assertion",
		"counting organizations",
		`SELECT count(*) FROM "Organization";\n`,
	);
	const count = Number.parseInt(rows[0] ?? "", 10);
	if (rows.length !== 1 || !Number.isSafeInteger(count) || count < 0) {
		throw new PostizBootstrapError("data-assertion", "the organization count was unreadable");
	}
	return count;
}

/**
 * The data assertion: what Postiz answers to the seeded key, and how many organizations exist.
 * Returned as observed; the caller decides (ready needs `connected` AND exactly one).
 */
export async function assertPostizReady(
	entry: PostizCompanionManifest,
	secretValues: Record<string, string>,
	host: ContainerHost,
	loopback: PostizLoopback,
): Promise<CompanionReadyAssertion> {
	const key = postizApiKey(entry, secretValues);
	let connected = false;
	try {
		// Bare key, no `Bearer`: Postiz compares the header verbatim with Organization.apiKey.
		const response = await postizRequest(loopback, entry, POSTIZ_IS_CONNECTED_PATH, {
			method: "GET",
			headers: { authorization: key },
		});
		if (response.status === 200) {
			const body = (await response.json().catch(() => null)) as { connected?: unknown } | null;
			connected = body?.connected === true;
		}
	} catch {
		connected = false;
	}
	return { connected, organizations: countPostizOrganizations(host, entry) };
}

// ---------------------------------------------------------------------------------------------
// Backups (plan 06-15, D-14)
// ---------------------------------------------------------------------------------------------
//
// The database unit archives every WAL segment from its first start (`postgresArchiveExec`). What
// the runtime adds here, after the database is up and BEFORE the Postiz bootstrap, is the stanza
// and the first full backup, so an install ends with a restorable repository. Every pgBackRest
// command runs inside the database container as `postgres` and reads its repository settings --
// the key and the cipher pass included -- from that container's encrypted env credential, so no
// secret is ever an argument.

/** The container's `postgres` account: pgBackRest must run as the data directory's owner. */
const POSTGRES_OS_USER = "postgres";
/** pgBackRest's labels: `<full>F`, or `<full>F_<stamp>D|I` for a differential or incremental. */
const BACKUP_LABEL_PATTERN = /^[0-9]{8}-[0-9]{6}F(_[0-9]{8}-[0-9]{6}[DI])?$/;

/** One backup the repository holds, as `pgbackrest info --output=json` describes it. */
export interface PostizBackupInfo {
	label: string;
	type: "full" | "diff" | "incr";
	/** When the backup finished, epoch milliseconds. */
	stoppedAt: number;
	/** The database's size at the backup, in bytes, when reported. */
	dbBytes: number | null;
	/**
	 * The app digest that last wrote the schema this backup holds (plan 06-48, D-14): its
	 * `iforge-schema-digest` annotation. `null` for a backup taken before plan 06-48 and for any
	 * value that is not 64 lowercase hex digits.
	 */
	schemaDigest: string | null;
	/**
	 * The app digest an update was moving to when this backup was its pre-update backup: its
	 * `iforge-schema-next` annotation, written before the swap restarted anything. WAL archived
	 * after this backup may then hold that digest's schema. `null` when absent or unreadable.
	 */
	nextSchemaDigest: string | null;
}

/** The backup annotation naming the app digest that wrote the backup's schema (plan 06-48). */
const SCHEMA_DIGEST_ANNOTATION = "iforge-schema-digest";
/** The backup annotation naming the app digest a swap after this backup was moving to. */
const NEXT_SCHEMA_DIGEST_ANNOTATION = "iforge-schema-next";

/** An annotated digest, or `null` for anything but exactly 64 lowercase hex digits. */
function annotatedDigest(annotation: Record<string, unknown>, key: string): string | null {
	const value = annotation[key];
	return typeof value === "string" && SCHEMA_DIGEST_PATTERN.test(value) ? value : null;
}

/** The repository as pgBackRest reports it for one stanza. */
export interface PostizRepositoryInfo {
	/** `false` when the stanza has never been created in this repository. */
	stanzaExists: boolean;
	/** Oldest first, as pgBackRest lists them. */
	backups: PostizBackupInfo[];
}

/** The outcome of one backup attempt, in the report's shape (`CompanionBackupResult`). */
export interface PostizBackupOutcome {
	result: CompanionBackupResult;
	/** The redacted-at-report-time failure, for the runtime's own log; never sent as a report. */
	error: string | null;
}

/** Everything the backup path needs besides the host: a clock that can wait. */
export interface PostizBackupClock {
	sleep(ms: number): Promise<void>;
	now(): number;
	readinessBudgetMs?: number;
	pollIntervalMs?: number;
}

function pgBackRestArgs(stanza: string, command: string, options: readonly string[]): string[] {
	return [`--stanza=${stanza}`, command, ...options];
}

/** Run pgBackRest inside the running database container, as `postgres`. Never throws. */
function execPgBackRest(
	host: ContainerHost,
	entry: PostizCompanionManifest,
	stanza: string,
	command: string,
	options: readonly string[] = [],
): ContainerHostCommandResult {
	return host.run("podman", [
		"exec",
		"--user",
		POSTGRES_OS_USER,
		containerCompanionUnitNames(entry.kind).databaseContainer,
		"pgbackrest",
		...pgBackRestArgs(stanza, command, options),
	]);
}

/**
 * Record in the repository that WAL archived after backup `label` may hold the schema of app
 * digest `next` (plan 06-48, D-14): its `iforge-schema-next` annotation, through the running
 * database container. The value is only ever a 64-hex digest this module produced, in an argv
 * array. Never throws.
 */
function annotateBackup(
	host: ContainerHost,
	entry: PostizCompanionManifest,
	label: string,
	next: string,
): ContainerHostCommandResult {
	const backup = entryBackup(entry);
	if (backup === null) {
		return { status: 1, stdout: "", stderr: "this installation has no backup repository" };
	}
	return execPgBackRest(host, entry, backup.stanza, "annotate", [
		`--set=${label}`,
		`--annotation=${NEXT_SCHEMA_DIGEST_ANNOTATION}=${next}`,
	]);
}

function backupType(value: unknown): PostizBackupInfo["type"] | null {
	return value === "full" || value === "diff" || value === "incr" ? value : null;
}

/**
 * Parse `pgbackrest info --output=json` for ONE stanza. Anything unreadable throws: deciding
 * "there is no backup" from output that could not be read would start an empty database over a
 * repository that holds one (or skip a first backup that never ran).
 */
export function parsePgBackRestInfo(stdout: string, stanza: string): PostizRepositoryInfo {
	let parsed: unknown;
	try {
		parsed = JSON.parse(stdout.trim() || "null");
	} catch {
		throw new Error("pgbackrest info answered unreadable JSON");
	}
	if (!Array.isArray(parsed)) throw new Error("pgbackrest info answered no stanza list");
	const entry = parsed.find(
		(item): item is Record<string, unknown> =>
			typeof item === "object" && item !== null && (item as { name?: unknown }).name === stanza,
	);
	if (entry === undefined) return { stanzaExists: false, backups: [] };
	const status = entry.status as { code?: unknown } | undefined;
	// Code 1 is "missing stanza path": the stanza was never created in this repository.
	const stanzaExists = status?.code !== 1;
	const rawBackups = Array.isArray(entry.backup) ? entry.backup : [];
	const backups: PostizBackupInfo[] = [];
	for (const raw of rawBackups) {
		const item = (raw ?? {}) as {
			label?: unknown;
			type?: unknown;
			timestamp?: { stop?: unknown };
			info?: { size?: unknown };
			error?: unknown;
			annotation?: unknown;
		};
		const type = backupType(item.type);
		const stop = item.timestamp?.stop;
		if (
			typeof item.label !== "string" ||
			!BACKUP_LABEL_PATTERN.test(item.label) ||
			type === null ||
			typeof stop !== "number" ||
			!Number.isFinite(stop)
		) {
			throw new Error("pgbackrest info listed a backup it did not describe");
		}
		// A backup pgBackRest itself flags as errored is never counted as a restorable one.
		if (item.error === true) continue;
		const size = item.info?.size;
		const annotation =
			typeof item.annotation === "object" &&
			item.annotation !== null &&
			!Array.isArray(item.annotation)
				? (item.annotation as Record<string, unknown>)
				: {};
		backups.push({
			label: item.label,
			type,
			stoppedAt: stop * 1000,
			dbBytes: typeof size === "number" && Number.isSafeInteger(size) && size >= 0 ? size : null,
			schemaDigest: annotatedDigest(annotation, SCHEMA_DIGEST_ANNOTATION),
			nextSchemaDigest: annotatedDigest(annotation, NEXT_SCHEMA_DIGEST_ANNOTATION),
		});
	}
	return { stanzaExists, backups };
}

function readRepositoryInfo(
	host: ContainerHost,
	entry: PostizCompanionManifest,
	stanza: string,
): PostizRepositoryInfo {
	const result = execPgBackRest(host, entry, stanza, "info", ["--output=json"]);
	if (result.status !== 0) {
		const detail = commandDetail(result);
		throw new Error(`pgbackrest info failed${detail ? `: ${detail}` : ""}`);
	}
	return parsePgBackRestInfo(result.stdout, stanza);
}

/** Wait until the database accepts connections on its local socket. */
async function waitForPostizDatabase(
	host: ContainerHost,
	entry: PostizCompanionManifest,
	clock: PostizBackupClock,
): Promise<void> {
	const budget = clock.readinessBudgetMs ?? POSTIZ_READINESS_BUDGET_MS;
	const interval = clock.pollIntervalMs ?? POSTIZ_READINESS_POLL_MS;
	const deadline = clock.now() + budget;
	const container = containerCompanionUnitNames(entry.kind).databaseContainer;
	for (;;) {
		const result = host.run("podman", [
			"exec",
			container,
			"pg_isready",
			"-q",
			"-U",
			POSTIZ_DB_USER,
			"-d",
			POSTIZ_DB_NAME,
		]);
		if (result.status === 0) return;
		if (clock.now() >= deadline) {
			throw new PostizBootstrapError(
				"backup",
				`the Postiz database did not accept connections within ${Math.round(budget / 1000)} s`,
			);
		}
		await clock.sleep(interval);
	}
}

/**
 * Take one backup and describe it. Never throws for a failed backup: that is an `ok: false`
 * outcome carrying the command's own error for the caller to redact and log.
 */
function takePostizBackup(
	host: ContainerHost,
	entry: PostizCompanionManifest,
	stanza: string,
	type: "full" | "diff",
	marker: CompanionBackupMarker | null,
	/**
	 * The app digest that last wrote the schema being backed up (plan 06-48, D-14), recorded as the
	 * backup's `iforge-schema-digest` annotation; `null` records nothing.
	 */
	schemaDigest: string | null,
	at: () => Date = () => host.now(),
): PostizBackupOutcome {
	const startedAt = at().toISOString();
	const result = execPgBackRest(host, entry, stanza, "backup", [
		`--type=${type}`,
		...(schemaDigest !== null ? [`--annotation=${SCHEMA_DIGEST_ANNOTATION}=${schemaDigest}`] : []),
	]);
	if (result.status !== 0) {
		const detail = commandDetail(result);
		return {
			result: {
				ok: false,
				label: null,
				started_at: startedAt,
				completed_at: at().toISOString(),
				db_bytes: null,
				marker,
			},
			error: `pgbackrest ${type} backup failed${detail ? `: ${detail}` : ""}`,
		};
	}
	let newest: PostizBackupInfo | undefined;
	try {
		newest = readRepositoryInfo(host, entry, stanza).backups.at(-1);
	} catch {
		// The backup itself succeeded; only its description is missing.
		newest = undefined;
	}
	return {
		result: {
			ok: true,
			label: newest?.label ?? null,
			started_at: startedAt,
			completed_at: at().toISOString(),
			db_bytes: newest?.dbBytes ?? null,
			marker,
		},
		error: null,
	};
}

/**
 * The install half of D-14: once the database is up, create the stanza (idempotent) and, when the
 * repository holds no backup yet, take the first FULL one. Returns `null` for an entry without
 * backups and for a repository that already holds one (a re-apply, or a restored instance);
 * otherwise the first backup's outcome, `ok: false` included. Throws `PostizBootstrapError` at
 * step `backup` only when the stanza cannot be created or read.
 */
export async function ensurePostizBackups(
	entry: PostizCompanionManifest,
	host: ContainerHost,
	clock: PostizBackupClock,
	progress: PostizProgress = NO_PROGRESS,
	/** The app digest that wrote the schema, annotated on the first backup (plan 06-48). */
	schemaDigest: string | null = null,
): Promise<PostizBackupOutcome | null> {
	const backup = entryBackup(entry);
	if (backup === null) return null;
	await progress("backup", "waiting for the database before creating its backup repository");
	await waitForPostizDatabase(host, entry, clock);
	const created = execPgBackRest(host, entry, backup.stanza, "stanza-create");
	if (created.status !== 0) {
		const detail = commandDetail(created);
		throw new PostizBootstrapError(
			"backup",
			`pgbackrest stanza-create failed${detail ? `: ${detail}` : ""}`,
		);
	}
	let repository: PostizRepositoryInfo;
	try {
		repository = readRepositoryInfo(host, entry, backup.stanza);
	} catch (error) {
		throw new PostizBootstrapError(
			"backup",
			error instanceof Error ? error.message : String(error),
		);
	}
	if (repository.backups.length > 0) {
		await progress("backup", "the backup repository already holds a backup");
		return null;
	}
	await progress("backup", "taking the first full backup");
	return takePostizBackup(host, entry, backup.stanza, "full", null, schemaDigest);
}

async function sendBackupReport(
	reporter: CompanionReportSink,
	operation: CompanionInstallOperation,
	outcome: PostizBackupOutcome,
): Promise<boolean> {
	try {
		const result = await reporter.send({ operation, phase: "backup", backup: outcome.result });
		// A refusal is an answer, not a delivery (plan 06-40): never counted as reported.
		return result.outcome !== "refused";
	} catch {
		// The backup exists either way; the next scheduled backup reports again.
		return false;
	}
}

export type PostizReadinessOutcome =
	| { outcome: "skipped"; reason: string }
	| { outcome: "ready" | "failed"; step: string }
	| {
			/**
			 * The control plane refused this apply's terminal report for a reason other than an
			 * operation mismatch (plan 06-40): settled `failed` on the host, never `ready`.
			 */
			outcome: "refused";
			step: string;
			code: string | null;
			terminalStatus: string | null;
	  }
	| { outcome: "unreportable"; reason: string };

/** The one install refusal that leaves the host's own data assertion standing (plan 06-40). */
const REPORT_OPERATION_MISMATCH_CODE = "companion_report_operation_mismatch";

function defaultSleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Identifies ONE apply: the installation, what it runs and the credentials it runs with, and when
 * it was applied. A credential rotation or a re-render changes it, so that apply is reported
 * (and its key re-seeded) again; a converge that changed nothing does not.
 */
function receiptFingerprint(receipt: ContainerCompanionReceipt): string {
	return sha256Text(
		JSON.stringify([
			receipt.installationId,
			receipt.images,
			receipt.sourcesSha256,
			receipt.credentialsSha256,
			receipt.appliedAt,
		]),
	);
}

const JOURNAL_TAIL_LINES = 200;

/**
 * The app unit's journal since this apply, as report lines. Untrusted text from inside the
 * container, so the caller redacts it before it goes anywhere; unreadable is simply empty.
 */
function postizJournalTail(
	host: ContainerHost,
	entry: PostizCompanionManifest,
	receipt: ContainerCompanionReceipt,
): string[] {
	const since = Date.parse(receipt.appliedAt);
	const result = host.run("journalctl", [
		`--unit=${containerCompanionUnitNames(entry.kind).appService}`,
		"--output=cat",
		"--no-pager",
		`--lines=${JOURNAL_TAIL_LINES}`,
		...(Number.isFinite(since) ? [`--since=@${Math.floor(since / 1000)}`] : []),
	]);
	if (result.status !== 0) return [];
	return result.stdout
		.split("\n")
		.map((line) => line.trimEnd())
		.filter((line) => line.length > 0)
		.slice(-JOURNAL_TAIL_LINES);
}

/**
 * The post-apply half of a container install: bootstrap, assert on data, report.
 *
 * Runs after the converge has applied an installation's units. Returns what it reported; it never
 * throws for an install that failed (that is a `failed` report) and returns `unreportable` when
 * the control plane could not be told, so the caller can log it and the next converge retries.
 *
 * Once per apply: a delivered terminal report is recorded in the root-only receipt against the
 * apply's fingerprint, so a converge that changed nothing does not bootstrap again, while a new
 * apply (a credential rotation re-renders the units) is bootstrapped idempotently and reported.
 *
 * `refused` (plan 06-40) when the control plane refused the terminal report for any reason but an
 * operation mismatch: the apply is recorded `failed` with the refusal, never `ready`, and is not
 * bootstrapped or re-sent; a new apply reports afresh.
 */
export async function convergePostizReadiness(input: {
	manifest: RuntimeManifest;
	secretValues: Record<string, string> | undefined;
	paths: Pick<RuntimePaths, "serviceStateRoot" | "configurationRoot" | "runRoot">;
	host?: ContainerHost;
	loopback?: Partial<PostizLoopback>;
	reporter?: CompanionReportSink;
	operation?: CompanionInstallOperation;
}): Promise<PostizReadinessOutcome> {
	const entry = input.manifest.companions?.postiz;
	if (entry?.lifecycle !== "present") return { outcome: "skipped", reason: "no present entry" };
	const host = input.host ?? nodeContainerHost();
	const paths = containerCompanionPaths(input.paths, entry.kind);
	const receipt = parseReceipt(host.readFile(paths.receiptPath));
	if (receipt?.state !== "applied" || receipt.installationId !== entry.installationId) {
		return { outcome: "skipped", reason: "installation not applied" };
	}
	if (receipt.rollbackIncomplete !== undefined) {
		// Plan 06-46 (WR-07): bootstrapping over an incomplete rollback would prove, and report ready,
		// units and data whose state nobody knows. The rollback's retry owns this installation.
		return { outcome: "skipped", reason: rollbackIncompleteReason(receipt.rollbackIncomplete) };
	}
	if (receipt.pendingUpdateReport !== undefined) {
		// An update's own terminal report is owed first; `applyPostizUpdate` delivers it (plan 06-18).
		return { outcome: "skipped", reason: "an update report is pending" };
	}
	if (receipt.pendingUpdateRollback !== undefined) {
		// An update swap is unsettled (plan 06-47): its rollback, not an install report, is owed.
		return { outcome: "skipped", reason: "an update rollback is pending" };
	}
	const fingerprint = receiptFingerprint(receipt);
	if (receipt.bootstrap?.receipt === fingerprint) {
		return { outcome: "skipped", reason: `already reported ${receipt.bootstrap.phase}` };
	}
	const secretValues = input.secretValues ?? {};
	const reporting = postizReporting(input.manifest, entry, secretValues, input.reporter);
	if (reporting.reporter === null) {
		return { outcome: "unreportable", reason: "no runtime credential to report with" };
	}
	const { reporter, redact } = reporting;
	const loopback: PostizLoopback = {
		fetch: input.loopback?.fetch ?? ((url, init) => fetch(url, init)),
		sleep: input.loopback?.sleep ?? defaultSleep,
		now: input.loopback?.now ?? Date.now,
		readinessBudgetMs: input.loopback?.readinessBudgetMs,
		pollIntervalMs: input.loopback?.pollIntervalMs,
	};
	const operation = input.operation ?? "install";

	const progress: PostizProgress = async (step, message) => {
		try {
			await reporter.send({
				operation,
				phase: "progress",
				step,
				lines: [{ stream: "stdout", line: redact(message) }],
			});
		} catch {
			// Progress is best effort: the terminal report below is the one that must arrive.
		}
	};

	let terminal: CompanionInstallReport;
	let backupSchedule = receipt.backupSchedule;
	try {
		if (receipt.restore?.restoredAt === receipt.appliedAt) {
			// This apply found an empty data directory and restored it before the first start.
			await progress(
				"restore",
				`restoring: the data directory was empty, so backup ${receipt.restore.label} was restored before the database first started`,
			);
		}
		// The repository and its first full backup, after the database is up and before the
		// bootstrap (plan 06-15). A first backup that fails fails the install: an installation
		// whose archive cannot be written would pile WAL on its disk and could never be restored.
		const firstBackup = await ensurePostizBackups(
			entry,
			host,
			loopback,
			progress,
			currentSchemaDigest(host, paths, entry.kind),
		);
		if (firstBackup !== null) {
			await sendBackupReport(reporter, operation, firstBackup);
			if (!firstBackup.result.ok) {
				throw new PostizBootstrapError(
					"backup",
					firstBackup.error ?? "the first full backup failed",
				);
			}
			const completed = firstBackup.result.completed_at;
			backupSchedule = {
				lastBackupAt: completed,
				lastFullAt: completed,
				lastLabel: firstBackup.result.label,
			};
			await progress("backup", "the first full backup completed");
		}
		const boot = await bootstrapPostiz(entry, secretValues, host, loopback, progress);
		await progress(
			"data-assertion",
			"asserting the seeded key connects and exactly one organization exists",
		);
		const assertion = await assertPostizReady(entry, secretValues, host, loopback);
		if (!assertion.connected || assertion.organizations !== 1) {
			throw new PostizBootstrapError(
				"data-assertion",
				`Postiz did not prove itself ready: connected=${assertion.connected}, organizations=${assertion.organizations} (ready needs connected and exactly one)`,
			);
		}
		terminal = {
			operation,
			phase: "ready",
			step: "ready",
			assertion,
			// The app this host runs (plan 06-47): a held installation may run a digest other than
			// the pin until the update path moves it, and a ready never claims what does not run.
			applied_digest: digestOf(receipt.images.app) ?? entry.artifact.sha256,
			admin_user_id: boot.adminUserId,
		};
	} catch (error) {
		const step = error instanceof PostizBootstrapError ? error.step : "bootstrap";
		const message = error instanceof Error ? error.message : String(error);
		// The unit's own journal is what tells the tenant WHY; redacted like everything else.
		const lines = postizJournalTail(host, entry, receipt).map((line) => ({
			stream: "stdout" as const,
			line: redact(line),
		}));
		terminal = {
			operation,
			phase: "failed",
			step,
			error: redact(message) || "failed",
			...(lines.length > 0 ? { lines } : {}),
		};
	}
	let result: CompanionReportResult;
	try {
		result = await reporter.send(terminal);
	} catch (error) {
		// No marker: the next converge bootstraps (idempotently) and reports again.
		return {
			outcome: "unreportable",
			reason: redact(error instanceof Error ? error.message : String(error)),
		};
	}
	const step = terminal.step ?? terminal.phase;
	// `recorded` and `already-terminal` (this operation already COMPLETED) settle as reported. So
	// does a refusal naming another operation: a credential-rotation or instance-replacement
	// re-bootstrap always reports `install` while the installation may now run `update` or
	// another operation, whose state this report never touches, so the host's own data assertion
	// stands. Every other refusal -- the control plane already failed this install (plan 06-37's
	// failed-at-apply report, or its deadline), a digest that is not the pin, a job not running --
	// settles `failed` with the refusal recorded, never `ready` (plan 06-40, D-26): nothing gated
	// on a ready installation runs for it. The host stops no unit and touches no data here.
	const refused =
		result.outcome === "refused" && result.code !== REPORT_OPERATION_MISMATCH_CODE ? result : null;
	const marked: ContainerCompanionReceipt = {
		...receipt,
		...(backupSchedule ? { backupSchedule } : {}),
		bootstrap: {
			receipt: fingerprint,
			phase: refused === null ? terminal.phase : "failed",
			step,
			reportedAt: host.now().toISOString(),
			...(refused === null
				? {}
				: { refused: { code: refused.code, terminalStatus: refused.terminalStatus } }),
		},
	};
	host.writeFile(paths.receiptPath, `${JSON.stringify(marked, null, 2)}\n`, {
		mode: PRIVATE_FILE_MODE,
	});
	if (refused !== null) {
		return {
			outcome: "refused",
			step,
			code: refused.code,
			terminalStatus: refused.terminalStatus,
		};
	}
	return { outcome: terminal.phase, step };
}

/**
 * The secret values one installation's text is redacted of: every composed value the entry names,
 * every env-file value inside them, and the runtime's own credential. With no entry, only the
 * runtime credential is known.
 */
function installationRedaction(
	entry: PostizCompanionManifest | undefined,
	secretValues: Record<string, string>,
): { runtimeToken: string | null; secrets: string[]; redact: (text: string) => string } {
	const runtimeToken = readRuntimeCredential(secretValues);
	const secrets = installationSecretValues(
		entry
			? Object.values(entry.credentials).filter((ref): ref is string => typeof ref === "string")
			: [],
		secretValues,
		runtimeToken ? [runtimeToken] : [],
	);
	return { runtimeToken, secrets, redact: (text: string) => redactKnownSecrets(text, secrets) };
}

/**
 * Where one installation's reports go, and how their text is redacted: every composed value the
 * entry names, every env-file value inside them, and the runtime's own credential. `reporter` is
 * `null` when none was injected and the runtime holds no credential to report with.
 */
function postizReporting(
	manifest: RuntimeManifest,
	entry: PostizCompanionManifest,
	secretValues: Record<string, string>,
	injected: CompanionReportSink | undefined,
): { reporter: CompanionReportSink | null; redact: (text: string) => string } {
	const { runtimeToken, secrets, redact } = installationRedaction(entry, secretValues);
	if (injected) return { reporter: injected, redact };
	if (!runtimeToken) return { reporter: null, redact };
	return {
		reporter: new CompanionProgressReporter({
			apiUrl: manifest.controlPlane.apiUrl,
			token: runtimeToken,
			installationId: entry.installationId,
			secrets,
		}),
		redact,
	};
}

// ---------------------------------------------------------------------------------------------
// Scheduled backups with rehearsal markers (plan 06-15, D-14; the D-16 prerequisite)
// ---------------------------------------------------------------------------------------------
//
// Once a day, the runtime writes a marker row -- a random token -- into `iforge_rehearsal.marker`
// and then takes a backup: differential, or full when the newest full one is a week old. The
// backup report carries the marker, so the control plane knows which token was committed before
// which backup; plan 06-16's rehearsal restores to a point between two markers and asserts that
// exactly the earlier one is there. The marker schema is never `public`, which Postiz's
// `prisma db push --accept-data-loss` reconciles to its own model at every start.

export const BACKUP_INTERVAL_MS = 24 * 3_600_000;
export const FULL_BACKUP_INTERVAL_MS = 7 * 24 * 3_600_000;
/** A failed scheduled backup is retried no sooner than this, not on every watch iteration. */
export const BACKUP_RETRY_AFTER_MS = 3_600_000;
const REHEARSAL_TIMESTAMP_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$/;

export type ContainerCompanionMaintenanceOutcome =
	| { outcome: "skipped"; reason: string }
	| {
			outcome: "backed-up";
			type: "full" | "diff";
			label: string | null;
			marker: CompanionBackupMarker | null;
			reported: boolean;
	  }
	| {
			outcome: "failed";
			type: "full" | "diff";
			/** Redacted against every known secret of the installation. */
			error: string;
			marker: CompanionBackupMarker | null;
			reported: boolean;
	  };

/**
 * Insert one rehearsal marker and return it as the database recorded it. The token is random
 * (not a secret, but still sent on standard input like every other value); the time is the
 * database's own clock, in UTC, at the microsecond precision it keeps.
 */
function writeRehearsalMarker(
	host: ContainerHost,
	entry: PostizCompanionManifest,
): CompanionBackupMarker {
	const token = randomBytes(16).toString("hex");
	const rows = runPostizPsql(
		host,
		entry,
		"backup",
		"writing the rehearsal marker",
		[
			"CREATE SCHEMA IF NOT EXISTS iforge_rehearsal;",
			"CREATE TABLE IF NOT EXISTS iforge_rehearsal.marker (id bigserial PRIMARY KEY, token text NOT NULL, written_at timestamptz NOT NULL DEFAULT now());",
			`\\set token ${psqlQuoted(token)}`,
			`INSERT INTO iforge_rehearsal.marker (token) VALUES (:'token') RETURNING id, to_char(written_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');`,
			"",
		].join("\n"),
	);
	const [idText = "", writtenAt = ""] = (rows[0] ?? "").split("|");
	const id = Number.parseInt(idText, 10);
	if (
		rows.length !== 1 ||
		!/^\d+$/.test(idText) ||
		!Number.isSafeInteger(id) ||
		!REHEARSAL_TIMESTAMP_PATTERN.test(writtenAt)
	) {
		throw new PostizBootstrapError("backup", "the rehearsal marker insert answered no row");
	}
	return { id, token, written_at: writtenAt };
}

function scheduleFromRepository(
	repository: PostizRepositoryInfo,
): ContainerCompanionBackupSchedule {
	const newest = repository.backups.at(-1);
	const newestFull = [...repository.backups].reverse().find((backup) => backup.type === "full");
	return {
		lastBackupAt: newest ? new Date(newest.stoppedAt).toISOString() : null,
		lastFullAt: newestFull ? new Date(newestFull.stoppedAt).toISOString() : null,
		lastLabel: newest?.label ?? null,
	};
}

function elapsedMs(since: string | null | undefined, now: Date): number {
	const at = since ? Date.parse(since) : Number.NaN;
	return Number.isFinite(at) ? now.getTime() - at : Number.POSITIVE_INFINITY;
}

/** Which backup is due now, or `null` when none is. */
function dueBackup(schedule: ContainerCompanionBackupSchedule, now: Date): "full" | "diff" | null {
	if (elapsedMs(schedule.lastFailedAt, now) < BACKUP_RETRY_AFTER_MS) return null;
	if (elapsedMs(schedule.lastBackupAt, now) < BACKUP_INTERVAL_MS) return null;
	return elapsedMs(schedule.lastFullAt, now) >= FULL_BACKUP_INTERVAL_MS ? "full" : "diff";
}

/**
 * One maintenance pass for the container companion (plan 06-15): when the last backup is 24 hours
 * old, write a rehearsal marker, take the backup, report both, and record when. Does nothing
 * sooner, and nothing for an installation that is not applied and ready.
 *
 * Never throws for a failed backup (that is a `failed` outcome and an `ok: false` report that
 * still carries the marker); the watch loop catches anything else, so a backup can never stop the
 * loop that keeps the runtime converged. Reporting is best effort: the backup exists either way.
 */
export async function runContainerCompanionMaintenance(input: {
	paths: Pick<RuntimePaths, "serviceStateRoot" | "configurationRoot" | "runRoot">;
	manifest: RuntimeManifest;
	secretValues: Record<string, string> | undefined;
	host?: ContainerHost;
	reporter?: CompanionReportSink;
	now?: Date;
}): Promise<ContainerCompanionMaintenanceOutcome> {
	const entry = input.manifest.companions?.postiz;
	if (entry?.lifecycle !== "present") return { outcome: "skipped", reason: "no present entry" };
	const backup = entryBackup(entry);
	if (backup === null) return { outcome: "skipped", reason: "no backups configured" };
	const host = input.host ?? nodeContainerHost();
	const paths = containerCompanionPaths(input.paths, entry.kind);
	const receipt = parseReceipt(host.readFile(paths.receiptPath));
	if (receipt?.state !== "applied" || receipt.installationId !== entry.installationId) {
		return { outcome: "skipped", reason: "installation not applied" };
	}
	if (receipt.rollbackIncomplete !== undefined) {
		// Plan 06-46 (WR-07): no rehearsal marker or backup against a database in an unknown state;
		// a backup of it could later be restored as if it were good.
		return { outcome: "skipped", reason: rollbackIncompleteReason(receipt.rollbackIncomplete) };
	}
	if (receipt.bootstrap?.phase !== "ready") {
		return { outcome: "skipped", reason: "installation not ready" };
	}
	const now = input.now ?? host.now();
	const { reporter, redact } = postizReporting(
		input.manifest,
		entry,
		input.secretValues ?? {},
		input.reporter,
	);
	const record = (schedule: ContainerCompanionBackupSchedule) =>
		recordBackupSchedule(host, paths, receipt, schedule);

	let schedule = receipt.backupSchedule;
	if (schedule === undefined) {
		// No cache yet (a re-applied receipt, or a restored instance): ask the repository once.
		try {
			schedule = scheduleFromRepository(readRepositoryInfo(host, entry, backup.stanza));
		} catch (error) {
			return {
				outcome: "skipped",
				reason: redact(
					`the backup repository could not be read: ${error instanceof Error ? error.message : String(error)}`,
				),
			};
		}
		record(schedule);
	}
	const type = dueBackup(schedule, now);
	if (type === null) return { outcome: "skipped", reason: "no backup due" };

	let marker: CompanionBackupMarker | null = null;
	try {
		marker = writeRehearsalMarker(host, entry);
	} catch {
		// The backup matters more than its marker: take it anyway, reported without one.
		marker = null;
	}
	const backedUp = takePostizBackup(
		host,
		entry,
		backup.stanza,
		type,
		marker,
		currentSchemaDigest(host, paths, entry.kind),
		() => now,
	);
	const reported = reporter !== null && (await sendBackupReport(reporter, "install", backedUp));
	if (!backedUp.result.ok) {
		record({ ...schedule, lastFailedAt: now.toISOString() });
		return {
			outcome: "failed",
			type,
			error: redact(backedUp.error ?? "the backup failed"),
			marker,
			reported,
		};
	}
	const completed = backedUp.result.completed_at;
	record({
		lastBackupAt: completed,
		lastFullAt: type === "full" ? completed : schedule.lastFullAt,
		lastLabel: backedUp.result.label,
	});
	return { outcome: "backed-up", type, label: backedUp.result.label, marker, reported };
}

/**
 * Record the schedule in the receipt -- only if the receipt is still the one this pass read, so a
 * converge that re-applied in the meantime is never overwritten with a stale apply's fields.
 */
function recordBackupSchedule(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	read: ContainerCompanionReceipt,
	schedule: ContainerCompanionBackupSchedule,
): void {
	const current = parseReceipt(host.readFile(paths.receiptPath));
	if (current === null || receiptFingerprint(current) !== receiptFingerprint(read)) return;
	const updated: ContainerCompanionReceipt = { ...current, backupSchedule: schedule };
	host.writeFile(paths.receiptPath, `${JSON.stringify(updated, null, 2)}\n`, {
		mode: PRIVATE_FILE_MODE,
	});
}

// ---------------------------------------------------------------------------------------------
// The backup-gated update (plan 06-18, APP-06, OPS-02, D-17)
// ---------------------------------------------------------------------------------------------
//
// Postiz runs `prisma db push --accept-data-loss` at every start (research Pitfall 1), so a new
// digest is a potentially destructive migration. The converge's synchronous apply therefore never
// moves a running installation to a new digest; this pass does, after the converge:
//
//   progress "Taking a backup before updating." → pgBackRest backup (diff, or full when the
//   newest full is a week old) → organization count → pull and verify → re-render and restart →
//   readiness and the data assertion (connected with the key, same organization count) → ready.
//
// The GATE is in-converge state only: the backup must be one this very pass took after seeing
// the new digest. Nothing persisted across converges can satisfy it, so a pass interrupted after
// its backup and before the swap takes a fresh backup the next time.
//
// If the backup fails, nothing changes: no pull, no unit write, no restart, and a `failed` report
// at step `backup` (the control plane returns the pin). If Postiz fails its assertion, the app
// and database are stopped, the pre-update backup is restored into the stopped database in a
// one-shot container, the previous digest is re-rendered and restarted and re-asserted, and the
// update is reported `failed` at step `post-update-assertion`. The pre-update backup covers the
// tenant database only, which is enough: media lives in object storage and Redis/Temporal hold
// only replayable state (D-17, D-18).

/**
 * Where an update stopped; the control plane names each (plan 06-18). `backup`: the pre-update
 * backup failed and nothing changed. `pre-update`: the backup was taken, but the organization
 * count or an image could not be read or pulled, so nothing changed either. `post-update-assertion`:
 * the new digest ran and failed its data assertion, and was rolled back.
 */
export type PostizUpdateStep = "backup" | "pre-update" | "post-update-assertion";

export const POSTIZ_UPDATE_BACKUP_MESSAGE = "Taking a backup before updating.";
export const POSTIZ_UPDATE_SWAP_MESSAGE = "Updating Postiz.";

export type PostizUpdateOutcome =
	| { outcome: "skipped"; reason: string }
	| { outcome: "ready"; step: "ready"; label: string | null }
	| { outcome: "failed"; step: PostizUpdateStep; label: string | null }
	| {
			/**
			 * The control plane refused the update's `ready` report (plan 06-40, CR-03), so the host
			 * rolled back to the previous images from the pre-update backup `label`. `code` is the
			 * refusal's; `rollback` says how the rollback ended (redacted).
			 */
			outcome: "rolled-back";
			step: "control-plane-refused";
			label: string;
			code: string | null;
			rollback: string;
	  }
	| {
			/**
			 * The entry names an app digest this host does not run, and the control plane did not
			 * announce exactly that transition (plan 06-41, CR-03). Nothing was backed up, pulled,
			 * written or restarted, nothing was reported, and the receipt is untouched: a host still
			 * on the newer images after the pin was reverted is never walked back down. The host
			 * raises `schema_digest_mismatch` (plan 06-47), which the condition pass reports.
			 */
			outcome: "unrequested";
			reason: string;
	  }
	| {
			/**
			 * A rollback did not finish (plan 06-46, WR-07): the receipt records `rollbackIncomplete`,
			 * the host raised `update_rollback_incomplete`, and nothing runs over the installation
			 * until a retry of the same rollback completes. `code` is the ready's refusal code, or
			 * `null` for the post-update data check; `error` is redacted.
			 */
			outcome: "rollback-incomplete";
			step: ContainerCompanionRollbackIncomplete["step"];
			label: string;
			code: string | null;
			error: string;
			attempts: number;
	  }
	| {
			/**
			 * A retry of an incomplete rollback completed (plan 06-46): the receipt is the previous one
			 * again, with the refused images recorded, and the condition is cleared.
			 */
			outcome: "repaired";
			label: string;
			rollback: string;
	  }
	| { outcome: "unreportable"; reason: string };

/**
 * How long an incomplete rollback waits before the same rollback is retried (plan 06-46): long
 * enough that a watch loop does not hammer a repository that is down, short enough that a fixed
 * cause heals within the hour.
 */
export const ROLLBACK_REPAIR_RETRY_AFTER_MS = 30 * 60_000;

/** A refusal the update reports and records; anything else thrown is an interruption. */
class PostizUpdateRefusal extends Error {
	constructor(
		readonly step: PostizUpdateStep,
		message: string,
	) {
		super(message);
		this.name = "PostizUpdateRefusal";
	}
}

function digestOf(reference: string | undefined): string | null {
	const match = /@sha256:([a-f0-9]{64})$/.exec(reference ?? "");
	return match?.[1] ?? null;
}

/** The entry as it was when `receipt` was applied: the same entry at the receipt's digests. */
function entryAtReceipt(
	entry: PostizCompanionManifest,
	receipt: ContainerCompanionReceipt,
): PostizCompanionManifest {
	const app = digestOf(receipt.images.app);
	const postgres = digestOf(receipt.images.postgres);
	const redis = digestOf(receipt.images.redis);
	if (app === null || postgres === null || redis === null) {
		throw new Error(
			`container companion ${entry.kind}: the applied receipt does not name every data-bearing digest`,
		);
	}
	return {
		...entry,
		artifact: { ...entry.artifact, sha256: app },
		dependencies: {
			...entry.dependencies,
			postgres: { ...entry.dependencies.postgres, sha256: postgres },
			redis: { ...entry.dependencies.redis, sha256: redis },
		},
	};
}

function sameReferences(
	left: Partial<Record<ContainerImageSlot, string>>,
	right: Partial<Record<ContainerImageSlot, string>>,
): boolean {
	const slots: ContainerImageSlot[] = ["app", "postgres", "redis", "cloudflared"];
	return slots.every((slot) => left[slot] === right[slot]);
}

function writeReceipt(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	receipt: ContainerCompanionReceipt,
): void {
	host.writeFile(paths.receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, {
		mode: PRIVATE_FILE_MODE,
	});
}

/** Pull (when absent) and verify one image; the failure text, or `null`. Never throws for a status. */
function pullVerifiedImage(host: ContainerHost, image: ContainerCompanionImage): string | null {
	let digests = inspectRepoDigests(host, image.reference);
	if (digests === null) {
		const pulled = host.run("podman", ["pull", image.reference]);
		if (pulled.status !== 0) {
			const detail = commandDetail(pulled);
			return `pull of ${image.image} failed${detail ? `: ${detail}` : ""}`;
		}
		digests = inspectRepoDigests(host, image.reference);
	}
	if (digests === null || !digests.includes(image.reference)) {
		return `${image.image} failed digest verification: RepoDigests ${JSON.stringify(digests ?? [])} do not contain ${image.reference}`;
	}
	return null;
}

/**
 * The services a swap from `previous` to `plan` must restart, dependencies first: each whose image
 * changed (even when an interrupted earlier pass already wrote its source), each whose source
 * changed, and all of them when the credentials changed.
 */
function servicesToRestart(
	paths: ContainerCompanionPaths,
	plan: ContainerCompanionPlan,
	previous: ContainerCompanionReceipt,
	sourcesOnDisk: Record<string, string | null>,
): string[] {
	const names = containerCompanionUnitNames(plan.companion);
	const units: Array<[string, ContainerImageSlot, string]> = [
		[names.databaseService, "postgres", names.databaseSource],
		[names.redisService, "redis", names.redisSource],
		[names.appService, "app", names.appSource],
		[names.tunnelService, "cloudflared", names.tunnelSource],
	];
	const credentialsChanged = previous.credentialsSha256 !== plan.receipt.credentialsSha256;
	return units
		.filter(([service]) => plan.services.all.includes(service))
		.filter(([, slot, sourceName]) => {
			const path = join(paths.quadletRoot, sourceName);
			const source = plan.sources.find((candidate) => candidate.path === path);
			return (
				credentialsChanged ||
				previous.images[slot] !== plan.receipt.images[slot] ||
				(source !== undefined && sourcesOnDisk[path] !== source.content)
			);
		})
		.map(([service]) => service);
}

/** Write a plan's credentials (when they changed) and sources, and reload systemd. */
function renderUnits(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	plan: ContainerCompanionPlan,
	previous: ContainerCompanionReceipt,
	journal: WriteJournal,
): void {
	if (previous.credentialsSha256 !== plan.receipt.credentialsSha256) {
		for (const credential of plan.credentials) {
			journal.write(credential.path, encryptCredential(host, paths, credential), PRIVATE_FILE_MODE);
		}
	}
	// Every unit it renders loads the guard in `ExecStartPre=` (plan 06-39).
	journal.write(plan.guard.path, plan.guard.content, PRIVATE_FILE_MODE);
	for (const source of plan.sources) journal.write(source.path, source.content, SOURCE_FILE_MODE);
	mustRun(host, "systemd daemon-reload", "systemctl", ["daemon-reload"]);
}

/**
 * Take the pre-update backup: THIS pass's, after it saw the new digest. Returns its label, or
 * throws `PostizUpdateRefusal("backup")` having changed nothing.
 */
async function takePreUpdateBackup(input: {
	host: ContainerHost;
	entry: PostizCompanionManifest;
	reporter: CompanionReportSink;
	redact: (text: string) => string;
	now: Date;
	/** The schema digest before the swap: the backup's `iforge-schema-digest` (plan 06-48). */
	schemaDigest: string | null;
}): Promise<{ label: string; schedule: ContainerCompanionBackupSchedule }> {
	const { host, entry, reporter, redact, now } = input;
	const backup = entryBackup(entry);
	if (backup === null) {
		throw new PostizUpdateRefusal(
			"backup",
			"this installation has no backup repository, and an update is never applied without a backup",
		);
	}
	let repository: PostizRepositoryInfo;
	try {
		repository = readRepositoryInfo(host, entry, backup.stanza);
	} catch (error) {
		throw new PostizUpdateRefusal(
			"backup",
			`the backup repository could not be read: ${redact(error instanceof Error ? error.message : String(error))}`,
		);
	}
	const known = scheduleFromRepository(repository);
	const type = elapsedMs(known.lastFullAt, now) >= FULL_BACKUP_INTERVAL_MS ? "full" : "diff";
	const taken = takePostizBackup(host, entry, backup.stanza, type, null, input.schemaDigest);
	await sendBackupReport(reporter, "update", taken);
	if (!taken.result.ok) {
		throw new PostizUpdateRefusal("backup", redact(taken.error ?? `the ${type} backup failed`));
	}
	const label = taken.result.label;
	if (label === null) {
		// The backup exists, but a rollback could not name it: no swap without a restorable set.
		throw new PostizUpdateRefusal(
			"backup",
			"the pre-update backup completed but its label could not be read, so it could not be restored",
		);
	}
	const completed = taken.result.completed_at;
	return {
		label,
		schedule: {
			lastBackupAt: completed,
			lastFullAt: type === "full" ? completed : known.lastFullAt,
			lastLabel: label,
		},
	};
}

/**
 * Restore the pre-update backup set into the STOPPED database, in a one-shot container from the
 * postgres image the database runs, with the backup credential on the tmpfs only while it runs.
 */
function restorePreUpdateBackup(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	plan: ContainerCompanionPlan,
	label: string,
): void {
	if (plan.backup === null) throw new Error("no backup repository to restore from");
	const credentialId = plan.backup.credentialId;
	const credential = plan.credentials.find((candidate) => candidate.id === credentialId);
	if (!credential) throw new Error(`no ${credentialId} credential to restore with`);
	const envFile = join(paths.plaintextRoot, `.${credentialId}.oneshot.plain`);
	const marker = join(databaseDataRoot(paths), RESTORE_INCOMPLETE_MARKER);
	try {
		host.ensureDirectory(paths.plaintextRoot, { mode: 0o700, uid: 0, gid: 0 });
		host.writeFile(envFile, credential.plaintext(), { mode: PRIVATE_FILE_MODE });
		// A restore interrupted halfway is never started as a database (plan 06-15's marker).
		host.writeFile(marker, `${label}\n`, { mode: PRIVATE_FILE_MODE });
		const restored = oneShotPgBackRest(host, plan, paths, envFile, "restore", [
			`--stanza=${plan.backup.stanza}`,
			"restore",
			"--delta",
			`--set=${label}`,
			"--type=immediate",
			"--target-action=promote",
		]);
		if (restored.status !== 0) {
			const detail = commandDetail(restored);
			throw new Error(`pgbackrest restore of ${label} failed${detail ? `: ${detail}` : ""}`);
		}
		host.removeFile(marker);
	} finally {
		host.removeFile(envFile);
	}
}

async function assertAfterUpdate(
	entry: PostizCompanionManifest,
	secretValues: Record<string, string>,
	host: ContainerHost,
	loopback: PostizLoopback,
	before: number,
): Promise<{ assertion: CompanionReadyAssertion; passed: boolean; problem: string | null }> {
	try {
		await waitForPostizBackend(entry, loopback);
		const observed = await assertPostizReady(entry, secretValues, host, loopback);
		const assertion = { ...observed, previous_organizations: before };
		const passed = observed.connected && observed.organizations === before && before >= 1;
		return {
			assertion,
			passed,
			problem: passed
				? null
				: `connected=${observed.connected}, organizations=${observed.organizations} (${before} before the update)`,
		};
	} catch (error) {
		return {
			assertion: { connected: false, organizations: 0, previous_organizations: before },
			passed: false,
			problem: error instanceof Error ? error.message : String(error),
		};
	}
}

/**
 * Put the previous version back (plan 06-18 step 5, shared by plan 06-40): stop the tunnel, app
 * and database; render `previousPlan`'s units over the ones `fromReceipt` records; restore the
 * pre-update backup `label` into the stopped database; start the previous services; and assert on
 * data against the `before` organization count. Returns the sentence that says how it ended; a
 * step that throws ends it as "the rollback failed", never as an exception.
 *
 * `completed` (plan 06-46, WR-07) is true when the stop, the render, the restore and the start all
 * ran: the host is then known to run the previous version over the pre-update data, whether or not
 * that version passed its data check (the check's result is only in the sentence). False means the
 * host state is unknown, and the caller records `rollbackIncomplete` instead of settling.
 */
async function rollBackPostizUpdate(input: {
	host: ContainerHost;
	paths: ContainerCompanionPaths;
	entry: PostizCompanionManifest;
	secretValues: Record<string, string>;
	loopback: PostizLoopback;
	previousPlan: ContainerCompanionPlan;
	fromReceipt: ContainerCompanionReceipt;
	label: string;
	before: number;
	/**
	 * A repair (plan 06-46): an earlier attempt or a withdrawal may have left the units unloaded,
	 * and stopping an unloaded unit exits non-zero. The proof is then that nothing still runs.
	 */
	unitsMayBeAbsent?: boolean;
}): Promise<{ completed: boolean; sentence: string }> {
	const { host, paths, entry, secretValues, loopback, previousPlan, label, before } = input;
	const names = containerCompanionUnitNames(entry.kind);
	let completed = false;
	let unreset = "";
	try {
		const stopping = [
			...(previousPlan.services.tunnel ? [previousPlan.services.tunnel] : []),
			names.appService,
			names.databaseService,
		];
		if (input.unitsMayBeAbsent === true) {
			host.run("systemctl", ["stop", ...stopping]);
			const running = stopping.filter((service) =>
				STILL_RUNNING_STATES.has(host.run("systemctl", ["is-active", service]).stdout.trim()),
			);
			if (running.length > 0) {
				throw new Error(`the rollback could not stop ${running.join(", ")}`);
			}
		} else {
			mustRun(host, "stop before the rollback", "systemctl", ["stop", ...stopping]);
		}
		renderUnits(host, paths, previousPlan, input.fromReceipt, new WriteJournal(host));
		// Plan 06-49 (G-06-2 WR-06): the ONE gate before any rollback starts a unit. The previous
		// units now carry the pinned `Subnet=` and systemd has reloaded them; the live network must
		// have it too, or be migrated here, inside this rollback's own stop window (the migration
		// stops the rest of the kind), before the restore and the start. It covers every rollback,
		// since all of them run through this function: the refused ready, the post-update
		// assertion, the interrupted swap (plan 06-47) and the plan 06-46 repair retry. A read-only
		// check could never unblock that repair -- `planKind` returns `repair` before the subnet rule,
		// so no converge migrates an installation whose rollback is incomplete. A migration that
		// fails throws into the catch below: the rollback is incomplete and nothing starts.
		ensureContainerCompanionNetwork(previousPlan, host, paths);
		restorePreUpdateBackup(host, paths, previousPlan, label);
		// Plan 06-47 (D-28): the data is the pre-update set again, and the previous app is about to
		// start on it; the marker says so before it does.
		writeSchemaDigestMarker(host, paths, planAppDigest(previousPlan));
		mustRun(host, "restart of the previous version", "systemctl", [
			"start",
			...previousPlan.services.all.filter((service) => service !== names.networkService),
		]);
		completed = true;
		// Plan 06-48 (D-14): the swap this rollback undid is no longer pending, so the pre-update
		// backup's next-schema record goes back to the previous digest, which wrote this data. A
		// failed reset does not make the rollback incomplete: the host is known to run the previous
		// version on the pre-update data. The stale record only matters to a replacement host, and
		// there `decidePostizRestore` rule 3 restores this very backup set at its own point for the
		// previous pin -- the right schema; the cost is only the writes made after this rollback,
		// until the next scheduled backup supersedes the set as the newest.
		const reset = annotateBackup(host, entry, label, planAppDigest(previousPlan));
		if (reset.status !== 0) {
			unreset = `; its ${NEXT_SCHEMA_DIGEST_ANNOTATION} annotation was not reset (pgbackrest annotate exited ${reset.status})`;
		}
		const again = await assertAfterUpdate(entry, secretValues, host, loopback, before);
		return {
			completed,
			sentence: again.passed
				? `backup ${label} was restored and the previous version passed its check${unreset}`
				: `backup ${label} was restored, but the previous version did not pass its check (${again.problem})${unreset}`,
		};
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		if (error instanceof ContainerCompanionConditionError) {
			raiseContainerCompanionCondition(host, paths, {
				installationId: entry.installationId,
				code: error.code,
				detail: installationRedaction(entry, secretValues).redact(message),
			});
		}
		return completed
			? {
					completed,
					sentence: `backup ${label} was restored, but the previous version could not be checked (${message})${unreset}`,
				}
			: { completed, sentence: `the rollback failed: ${message}` };
	}
}

/** What a pending update report needs to roll back if the control plane refuses it. */
interface PendingUpdateContext {
	entry: PostizCompanionManifest;
	secretValues: Record<string, string>;
	loopback: PostizLoopback;
	facts: ContainerCompanionFacts;
}

/**
 * Why a pass skips an installation whose rollback is incomplete (plan 06-46, WR-07). Every pass
 * that needs a known host state -- readiness, maintenance, exports, the update's swap -- names it,
 * rather than relying on the missing ready marker alone.
 */
function rollbackIncompleteReason(incomplete: ContainerCompanionRollbackIncomplete): string {
	return `the update rollback to backup ${incomplete.label} is incomplete; nothing runs until it is repaired`;
}

/** A receipt to settle back on: nothing pending, refused or incomplete of its own. */
function withoutPendingState(receipt: ContainerCompanionReceipt): ContainerCompanionReceipt {
	const {
		pendingUpdateReport: _report,
		pendingUpdateRollback: _rollback,
		refusedUpdate: _refusal,
		rollbackIncomplete: _incomplete,
		...rest
	} = receipt;
	return rest;
}

/**
 * The receipt a COMPLETED rollback settles on (plan 06-47, D-28): the previous one, nothing
 * pending, whose app wrote the restored schema -- the rollback wrote the marker before it started.
 */
function settledOn(previous: ContainerCompanionReceipt): ContainerCompanionReceipt {
	const settled = withoutPendingState(previous);
	const app = digestOf(previous.images.app);
	return app === null ? settled : { ...settled, schemaDigest: app };
}

/**
 * Raise `update_rollback_incomplete` on the plan 06-45 host record with an ALREADY-REDACTED
 * detail; the post-converge pass reports it once per distinct state (plan 06-46, WR-07).
 */
function raiseRollbackIncomplete(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	installationId: string,
	error: string,
): void {
	raiseContainerCompanionCondition(host, paths, {
		installationId,
		code: "update_rollback_incomplete",
		detail: error,
	});
}

/**
 * Retry an incomplete rollback (plan 06-46, WR-07, D-17, D-28): at most once every
 * `ROLLBACK_REPAIR_RETRY_AFTER_MS`, the SAME rollback -- the stored previous receipt's units, and
 * the stored pre-update backup set restored `--type=immediate`, as the first attempt did.
 *
 * A completed retry settles exactly as a completed rollback does: the stored previous receipt,
 * with the refused images recorded, and the condition cleared. A failed one records the attempt
 * and re-raises the condition with the new detail. The repair sends no update report: that
 * operation is already terminal on the control plane; the condition is how it learns the outcome.
 */
async function repairIncompleteRollback(input: {
	host: ContainerHost;
	paths: ContainerCompanionPaths;
	entry: PostizCompanionManifest;
	secretValues: Record<string, string>;
	loopback: PostizLoopback;
	facts: ContainerCompanionFacts;
	receipt: ContainerCompanionReceipt;
	incomplete: ContainerCompanionRollbackIncomplete;
}): Promise<PostizUpdateOutcome> {
	const { host, paths, entry, secretValues, loopback, facts, receipt, incomplete } = input;
	const { label } = incomplete;
	const now = host.now();
	const lastAttempt = Date.parse(incomplete.lastAttemptAt);
	if (Number.isFinite(lastAttempt)) {
		const nextAttempt = lastAttempt + ROLLBACK_REPAIR_RETRY_AFTER_MS;
		if (now.getTime() < nextAttempt) {
			return {
				outcome: "skipped",
				reason: `the update rollback to backup ${label} is incomplete; it is retried at ${new Date(nextAttempt).toISOString()}`,
			};
		}
	}
	const { redact } = installationRedaction(entry, secretValues);
	// NOT `restorePostizOnBootstrap`, which the review suggested: that restores the NEWEST backup to
	// the END of the archive, which would replay the refused version's writes under the previous
	// units -- the downgrade over newer data this rollback exists to prevent. Only the stored
	// pre-update set, restored to its own end (D-17, D-28).
	let rollback: { completed: boolean; sentence: string };
	try {
		const previousPlan = planContainerCompanionAt(
			entryAtReceipt(entry, incomplete.previous),
			secretValues,
			facts,
		);
		// A withdrawal removed the encrypted credentials: render them again, whatever the hashes say.
		const credentialsPresent = previousPlan.credentials.every(
			(credential) => facts.credentialsOnDisk[credential.path] === true,
		);
		rollback = await rollBackPostizUpdate({
			host,
			paths,
			entry,
			secretValues,
			loopback,
			previousPlan,
			fromReceipt: credentialsPresent ? receipt : { ...receipt, credentialsSha256: "" },
			label,
			before: incomplete.organizations,
			unitsMayBeAbsent: true,
		});
	} catch (error) {
		rollback = {
			completed: false,
			sentence: `the rollback failed: ${error instanceof Error ? error.message : String(error)}`,
		};
	}
	const at = now.toISOString();
	if (!rollback.completed) {
		const error = redact(rollback.sentence);
		const attempts = incomplete.attempts + 1;
		writeReceipt(host, paths, {
			...receipt,
			// `failedAt` stays the first failure; the attempt count and time move.
			rollbackIncomplete: { ...incomplete, error, attempts, lastAttemptAt: at },
		});
		raiseRollbackIncomplete(host, paths, receipt.installationId, error);
		return {
			outcome: "rollback-incomplete",
			step: incomplete.step,
			label,
			code: null,
			error,
			attempts,
		};
	}
	writeReceipt(host, paths, {
		...settledOn(incomplete.previous),
		// An update report still owed (a failed report the last pass could not deliver) stays owed.
		...(receipt.pendingUpdateReport === undefined
			? {}
			: { pendingUpdateReport: receipt.pendingUpdateReport }),
		refusedUpdate: { images: receipt.images, step: incomplete.step, label, refusedAt: at },
	});
	clearContainerCompanionCondition(host, paths, {
		installationId: receipt.installationId,
		code: "update_rollback_incomplete",
	});
	return { outcome: "repaired", label, rollback: redact(rollback.sentence) };
}

/**
 * Deliver a terminal update report kept in the receipt, and settle the receipt once it is answered.
 *
 * `recorded` and `already-terminal` (409 `companion_install_terminal`/`complete`: this update
 * already completed) settle the receipt as delivered. A `refused` `ready` for an update that
 * changed the app digest is the control plane saying it does not run that version -- its deadline
 * already failed the update and returned the pin -- so the host rolls back to the receipt it
 * stored before the swap, from the pre-update backup, and settles on the previous images with the
 * refused ones recorded (plan 06-40, CR-03, D-28). Any other refusal settles as delivered: a
 * `failed` report's host already aborted or rolled back, and a dependency-only change is not an
 * update the control plane runs. A refusal is never re-sent.
 */
async function deliverPendingUpdateReport(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	receipt: ContainerCompanionReceipt,
	reporter: CompanionReportSink,
	redact: (text: string) => string,
	context: PendingUpdateContext,
): Promise<PostizUpdateOutcome> {
	const report = receipt.pendingUpdateReport;
	if (report === undefined) return { outcome: "skipped", reason: "no update report pending" };
	let result: CompanionReportResult;
	try {
		result = await reporter.send(identifiedUpdateReport(report, receipt));
	} catch (error) {
		// Kept in the receipt: the next converge delivers it before anything else.
		return {
			outcome: "unreportable",
			reason: redact(error instanceof Error ? error.message : String(error)),
		};
	}
	const {
		pendingUpdateReport: _delivered,
		pendingUpdateRollback: rollbackTo,
		...settled
	} = receipt;
	if (
		report.phase === "ready" &&
		result.outcome === "refused" &&
		rollbackTo !== undefined &&
		digestOf(rollbackTo.previous.images?.app) !== digestOf(receipt.images.app)
	) {
		const { previous, label } = rollbackTo;
		const before = report.assertion.previous_organizations ?? report.assertion.organizations;
		let rollback: { completed: boolean; sentence: string };
		try {
			const previousPlan = planContainerCompanionAt(
				entryAtReceipt(context.entry, previous),
				context.secretValues,
				context.facts,
			);
			rollback = await rollBackPostizUpdate({
				host,
				paths,
				entry: context.entry,
				secretValues: context.secretValues,
				loopback: context.loopback,
				previousPlan,
				fromReceipt: receipt,
				label,
				before,
			});
		} catch (error) {
			rollback = {
				completed: false,
				sentence: `the rollback failed: ${error instanceof Error ? error.message : String(error)}`,
			};
		}
		if (!rollback.completed) {
			// Plan 06-46 (WR-07): the rollback stopped part-way, so nothing is known about the units or
			// the data. The receipt keeps the images the swap rendered -- never the previous ones it
			// did not get back to -- drops the report and the ready marker, and records what the
			// repair needs. The condition tells the control plane in this same pass.
			const {
				bootstrap: _unknown,
				refusedUpdate: _noRefusal,
				rollbackIncomplete: _earlier,
				...current
			} = settled;
			const error = redact(rollback.sentence);
			const now = host.now().toISOString();
			writeReceipt(host, paths, {
				...current,
				rollbackIncomplete: {
					previous: withoutPendingState(previous),
					label,
					step: "control-plane-refused",
					organizations: before,
					error,
					failedAt: now,
					attempts: 1,
					lastAttemptAt: now,
				},
			});
			raiseRollbackIncomplete(host, paths, receipt.installationId, error);
			return {
				outcome: "rollback-incomplete",
				step: "control-plane-refused",
				label,
				code: result.code,
				error,
				attempts: 1,
			};
		}
		// The rollback completed: the host runs the previous images over the pre-update data, whether
		// or not that version passed its check. The receipt never keeps recording images the control
		// plane refused, and the refused ones are not tried again while the manifest names them.
		writeReceipt(host, paths, {
			...settledOn(previous),
			refusedUpdate: {
				images: receipt.images,
				step: "control-plane-refused",
				label,
				refusedAt: host.now().toISOString(),
			},
		});
		return {
			outcome: "rolled-back",
			step: "control-plane-refused",
			label,
			code: result.code,
			rollback: redact(rollback.sentence),
		};
	}
	const step = report.step ?? report.phase;
	if (report.phase === "ready") {
		settled.bootstrap = {
			receipt: receiptFingerprint(settled),
			phase: "ready",
			step,
			reportedAt: host.now().toISOString(),
		};
	}
	writeReceipt(host, paths, settled);
	return report.phase === "ready"
		? { outcome: "ready", step: "ready", label: settled.backupSchedule?.lastLabel ?? null }
		: {
				outcome: "failed",
				step: step === "backup" || step === "pre-update" ? step : "post-update-assertion",
				label: settled.refusedUpdate?.label ?? null,
			};
}

/**
 * A queued update report as it is sent (G-06-4). A report this CLI made already names its update.
 *
 * - An absent `to_digest` key means an older CLI queued the report. It is named from what the
 *   receipt recorded when it was made: a `failed` report from the refused update's app digest, a
 *   `ready` from its applied digest. Neither recorded, it is sent unchanged, as before.
 * - A `null` means this CLI left the identity out on purpose, because nothing recorded which
 *   update an interrupted swap belonged to (06-REVIEW WR-05). It stays out: the report is sent
 *   without the key, and `sanitize` would drop a `null` too. It is never named from
 *   `refusedUpdate`, which then holds the entry's pin.
 *
 * Never from the entry, which may name a later update by now. The value is sent, never written
 * back into the receipt.
 */
function identifiedUpdateReport(
	report: CompanionInstallReport,
	receipt: ContainerCompanionReceipt,
): CompanionInstallReport {
	if (report.operation !== "update") return report;
	if (
		(report.phase === "progress" || report.phase === "ready" || report.phase === "failed") &&
		report.to_digest === null
	) {
		const { to_digest: _leftOut, ...unlabeled } = report;
		return unlabeled;
	}
	if (report.phase === "failed" && report.to_digest === undefined) {
		const refused = digestOf(receipt.refusedUpdate?.images.app);
		return refused === null ? report : { ...report, to_digest: refused };
	}
	if (report.phase === "ready" && report.to_digest === undefined) {
		return { ...report, to_digest: report.applied_digest };
	}
	return report;
}

/**
 * Record an update's refusal and its `failed` report in the receipt, then deliver it. `receipt` is
 * the pass's own (its apply time bounds the journal tail); the refusal is written over the receipt
 * as it is on disk now, so a record made earlier in the pass (an incomplete rollback) survives.
 */
async function refusePostizUpdate(input: {
	host: ContainerHost;
	paths: ContainerCompanionPaths;
	entry: PostizCompanionManifest;
	receipt: ContainerCompanionReceipt;
	/** The images the refused update moves to. */
	images: Partial<Record<ContainerImageSlot, string>>;
	/**
	 * The app digest of the update being refused, as recorded when that update ran (G-06-4): the
	 * report's `to_digest`, kept with it in the receipt. `null` records in the queued report that
	 * the update's identity is unknown and was left out on purpose (06-REVIEW WR-05): the report is
	 * then sent without one, and never named from `refusedUpdate` or the entry.
	 */
	toDigest: string | null;
	reporter: CompanionReportSink;
	redact: (text: string) => string;
	context: PendingUpdateContext;
	step: PostizUpdateStep;
	message: string;
	label: string | null;
}): Promise<PostizUpdateOutcome> {
	const { host, paths, entry, receipt, reporter, redact, step, label, toDigest } = input;
	const lines = postizJournalTail(host, entry, receipt).map((line) => ({
		stream: "stdout" as const,
		line: redact(line),
	}));
	const current = parseReceipt(host.readFile(paths.receiptPath)) ?? receipt;
	const refused: ContainerCompanionReceipt = {
		...current,
		refusedUpdate: {
			images: input.images,
			step,
			label,
			refusedAt: host.now().toISOString(),
		},
		pendingUpdateReport: {
			operation: "update",
			phase: "failed",
			step,
			error: redact(input.message) || "failed",
			...(lines.length > 0 ? { lines } : {}),
			to_digest: toDigest,
		},
	};
	writeReceipt(host, paths, refused);
	return deliverPendingUpdateReport(host, paths, refused, reporter, redact, input.context);
}

/**
 * Settle a rollback made at the post-update step, whether the swap's check failed (step 5) or the
 * swap was interrupted (plan 06-47), then send the update's `failed` report.
 *
 * - Completed: the receipt is the one on disk (the pre-swap images) without the write-ahead's
 *   pending rollback, and with the previous app as the schema digest again.
 * - Not completed (plan 06-46, WR-07, prior WR-03): the receipt stops claiming the previous
 *   images -- it carries the ones the swap rendered, loses its ready marker and records the
 *   rollback to retry -- and `update_rollback_incomplete` is raised.
 *
 * The refusal is written over that receipt, so either record survives into the settled one.
 */
async function settlePostUpdateRollback(input: {
	host: ContainerHost;
	paths: ContainerCompanionPaths;
	entry: PostizCompanionManifest;
	/** The pass's receipt, as it was before the swap's write-ahead. */
	receipt: ContainerCompanionReceipt;
	/** The receipt the rollback goes back to. */
	previous: ContainerCompanionReceipt;
	swapped: Partial<Record<ContainerImageSlot, string>>;
	/**
	 * The swap's target app digest, as recorded when the update ran (G-06-4): the `failed` report's
	 * `to_digest`. Never read from `swapped`, whose legacy fallback can be the entry's pin.
	 */
	toDigest: string | null;
	label: string;
	before: number;
	rollback: { completed: boolean; sentence: string };
	message: string;
	reporter: CompanionReportSink;
	redact: (text: string) => string;
	context: PendingUpdateContext;
	/**
	 * Whether the control plane's update in flight is this swap's (06-REVIEW WR-01). When it is
	 * not, the refusal is recorded but no `failed` report is sent. The report names its update
	 * (`toDigest`), so a control plane that checks it would refuse it anyway; this stop-gap stays for
	 * one that predates that check, which would apply the report to whatever update is in flight
	 * now, one this host never tried. Defaults to `true`.
	 */
	reportable?: boolean;
}): Promise<PostizUpdateOutcome> {
	const { host, paths, entry, receipt, previous, label, before, rollback, redact } = input;
	const { pendingUpdateRollback: _undone, ...onDisk } =
		parseReceipt(host.readFile(paths.receiptPath)) ?? receipt;
	let incomplete: ContainerCompanionRollbackIncomplete | null = null;
	if (rollback.completed) {
		const previousApp = digestOf(previous.images.app);
		writeReceipt(host, paths, {
			...onDisk,
			...(previousApp !== null ? { schemaDigest: previousApp } : {}),
		});
	} else {
		const error = redact(rollback.sentence);
		const now = host.now().toISOString();
		const { bootstrap: _unknown, ...current } = onDisk;
		incomplete = {
			previous: withoutPendingState(previous),
			label,
			step: "post-update-assertion",
			organizations: before,
			error,
			failedAt: now,
			attempts: 1,
			lastAttemptAt: now,
		};
		writeReceipt(host, paths, {
			...current,
			images: input.swapped,
			rollbackIncomplete: incomplete,
		});
		raiseRollbackIncomplete(host, paths, entry.installationId, error);
	}
	if (input.reportable === false) {
		// Settled here only: the refusal is recorded so this swap is not retried while the entry
		// names it, and nothing is reported.
		const current = parseReceipt(host.readFile(paths.receiptPath)) ?? receipt;
		writeReceipt(host, paths, {
			...current,
			refusedUpdate: {
				images: input.swapped,
				step: "post-update-assertion",
				label,
				refusedAt: host.now().toISOString(),
			},
		});
		return incomplete === null
			? { outcome: "failed", step: "post-update-assertion", label }
			: {
					outcome: "rollback-incomplete",
					step: "post-update-assertion",
					label,
					code: null,
					error: incomplete.error,
					attempts: incomplete.attempts,
				};
	}
	const refused = await refusePostizUpdate({
		host,
		paths,
		entry,
		receipt,
		images: input.swapped,
		toDigest: input.toDigest,
		reporter: input.reporter,
		redact,
		context: input.context,
		step: "post-update-assertion",
		message: input.message,
		label,
	});
	if (incomplete === null || refused.outcome === "unreportable") return refused;
	return {
		outcome: "rollback-incomplete",
		step: "post-update-assertion",
		label,
		code: null,
		error: incomplete.error,
		attempts: incomplete.attempts,
	};
}

/**
 * Roll back an update swap interrupted after its write-ahead (plan 06-47, D-17, D-28): the receipt
 * carries `pendingUpdateRollback` and no report, so the pass that swapped died before it could
 * check or report. The stored previous receipt's units are rendered again, the stored pre-update
 * backup is restored `--type=immediate`, the previous version starts and is checked, and the
 * update's `failed` report is sent at `post-update-assertion`. No backup is taken.
 */
async function rollBackInterruptedSwap(input: {
	host: ContainerHost;
	paths: ContainerCompanionPaths;
	entry: PostizCompanionManifest;
	secretValues: Record<string, string>;
	loopback: PostizLoopback;
	facts: ContainerCompanionFacts;
	receipt: ContainerCompanionReceipt;
	rollbackTo: NonNullable<ContainerCompanionReceipt["pendingUpdateRollback"]>;
	swapped: Partial<Record<ContainerImageSlot, string>>;
	reporter: CompanionReportSink;
	redact: (text: string) => string;
	context: PendingUpdateContext;
}): Promise<PostizUpdateOutcome> {
	const { host, paths, entry, secretValues, loopback, facts, receipt, rollbackTo } = input;
	const { previous, label } = rollbackTo;
	// Only a pre-06-47 record lacks the count, and such a record always carries its report too, so
	// it is delivered by the pending-report branch and never reaches here.
	const before = rollbackTo.organizations ?? 1;
	// The update this swap belonged to (G-06-4), from what its write-ahead recorded before anything
	// was rendered: the swap's own target, or, on a record written before that field, the schema
	// digest the write-ahead named. Never `swapped`, whose legacy fallback can be the entry's pin.
	const toDigest =
		digestOf(rollbackTo.to?.app) ?? parseSchemaDigest(receipt.schemaDigest ?? null) ?? null;
	let rollback: { completed: boolean; sentence: string };
	try {
		const previousPlan = planContainerCompanionAt(
			entryAtReceipt(entry, previous),
			secretValues,
			facts,
		);
		rollback = await rollBackPostizUpdate({
			host,
			paths,
			entry,
			secretValues,
			loopback,
			previousPlan,
			// The swap may have re-encrypted the credentials before it died: render them again.
			fromReceipt: { ...receipt, credentialsSha256: "" },
			label,
			before,
			// A crash leaves the units in any state, a host reboot included.
			unitsMayBeAbsent: true,
		});
	} catch (error) {
		rollback = {
			completed: false,
			sentence: `the rollback failed: ${error instanceof Error ? error.message : String(error)}`,
		};
	}
	return settlePostUpdateRollback({
		host,
		paths,
		entry,
		receipt,
		previous,
		swapped: input.swapped,
		toDigest,
		label,
		before,
		rollback,
		message: `the update was interrupted before its post-update check; ${rollback.sentence}`,
		reporter: input.reporter,
		redact: input.redact,
		context: input.context,
		reportable: announcesSwap(entry, previous.images, input.swapped),
	});
}

/**
 * Whether an interrupted swap's `failed` report belongs to the control plane's update in flight
 * (06-REVIEW WR-01). Every update report now names its update (`to_digest`, G-06-4), and a control
 * plane that checks it refuses a report naming another update and changes nothing. This check is
 * the stop-gap from before that, kept because it costs nothing and still protects a host whose
 * control plane predates the check: such a control plane applies a `failed` to whatever update
 * runs. When the entry announces a transition other than this swap's -- the pin was reverted past
 * the swap's deadline and the tenant accepted a later update -- the report is not sent. With
 * nothing announced, it goes as before: the control plane refuses it as terminal, or as another
 * update's, if the swap's update already ended.
 */
function announcesSwap(
	entry: PostizCompanionManifest,
	previous: Partial<Record<ContainerImageSlot, string>>,
	swapped: Partial<Record<ContainerImageSlot, string>>,
): boolean {
	const announced = entry.update;
	if (announced === null) return true;
	return (
		announced.fromDigest === digestOf(previous.app) && announced.toDigest === digestOf(swapped.app)
	);
}

/**
 * Apply an accepted update to a ready installation: backup, swap, assert -- or abort, or roll back.
 *
 * A no-op (no backup, no pull, no restart) unless the entry names a data-bearing digest the
 * applied receipt does not run. An update this runtime already refused is not retried while the
 * manifest still names it: the control plane returns the pin, and a fresh acceptance names it
 * again only after this runtime has seen the pin come back.
 *
 * Throws only for an interruption (a command that could not even be run). One that dies before
 * the swap's write-ahead starts over from a fresh backup; one that dies after it is rolled back
 * from its own backup by the next pass (plan 06-47).
 *
 * After the pass, a host settled on the pin -- nothing pending, the pin's app running and writing
 * the schema -- clears `schema_digest_mismatch` (plan 06-47; a no-op when none is raised).
 */
export async function applyPostizUpdate(input: {
	manifest: RuntimeManifest;
	secretValues: Record<string, string> | undefined;
	paths: Pick<RuntimePaths, "serviceStateRoot" | "configurationRoot" | "runRoot">;
	host?: ContainerHost;
	loopback?: Partial<PostizLoopback>;
	reporter?: CompanionReportSink;
}): Promise<PostizUpdateOutcome> {
	const entry = input.manifest.companions?.postiz;
	if (entry?.lifecycle !== "present") return { outcome: "skipped", reason: "no present entry" };
	const host = input.host ?? nodeContainerHost();
	const outcome = await applyPostizUpdatePass({ ...input, host });
	clearSettledSchemaDigestMismatch(host, containerCompanionPaths(input.paths, entry.kind), entry);
	return outcome;
}

/** Clear `schema_digest_mismatch` when the host runs, and wrote its schema with, the pinned app. */
function clearSettledSchemaDigestMismatch(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	entry: PostizCompanionManifest,
): void {
	const facts = readContainerCompanionFacts(host, paths, entry.kind);
	const receipt = facts.receipt;
	if (receipt?.state !== "applied" || receipt.installationId !== entry.installationId) return;
	if (
		receipt.pendingUpdateReport !== undefined ||
		receipt.pendingUpdateRollback !== undefined ||
		receipt.rollbackIncomplete !== undefined
	) {
		return;
	}
	const pinned = entry.artifact.sha256;
	if (digestOf(receipt.images.app) !== pinned || schemaDigestOf(facts) !== pinned) return;
	clearContainerCompanionCondition(host, paths, {
		installationId: entry.installationId,
		code: "schema_digest_mismatch",
	});
}

/** One update pass (see `applyPostizUpdate`). */
async function applyPostizUpdatePass(input: {
	manifest: RuntimeManifest;
	secretValues: Record<string, string> | undefined;
	paths: Pick<RuntimePaths, "serviceStateRoot" | "configurationRoot" | "runRoot">;
	host: ContainerHost;
	loopback?: Partial<PostizLoopback>;
	reporter?: CompanionReportSink;
}): Promise<PostizUpdateOutcome> {
	const entry = input.manifest.companions?.postiz;
	if (entry?.lifecycle !== "present") return { outcome: "skipped", reason: "no present entry" };
	const host = input.host;
	const paths = containerCompanionPaths(input.paths, entry.kind);
	const facts = readContainerCompanionFacts(host, paths, entry.kind);
	const receipt = facts.receipt;
	const secretValues = input.secretValues ?? {};
	const loopback: PostizLoopback = {
		fetch: input.loopback?.fetch ?? ((url, init) => fetch(url, init)),
		sleep: input.loopback?.sleep ?? defaultSleep,
		now: input.loopback?.now ?? Date.now,
		readinessBudgetMs: input.loopback?.readinessBudgetMs,
		pollIntervalMs: input.loopback?.pollIntervalMs,
	};
	if (
		receipt?.installationId === entry.installationId &&
		receipt.rollbackIncomplete !== undefined &&
		// An applied receipt's owed failed report is delivered first, by the branch below.
		(receipt.pendingUpdateReport === undefined || receipt.state !== "applied")
	) {
		// Plan 06-46 (WR-07): the only thing that acts on an incomplete rollback, applied or withdrawn,
		// whatever the entry pins. It never backs up, pulls or swaps; it retries that rollback.
		return repairIncompleteRollback({
			host,
			paths,
			entry,
			secretValues,
			loopback,
			facts,
			receipt,
			incomplete: receipt.rollbackIncomplete,
		});
	}
	if (receipt?.state !== "applied" || receipt.installationId !== entry.installationId) {
		return { outcome: "skipped", reason: "installation not applied" };
	}
	const reporting = postizReporting(input.manifest, entry, secretValues, input.reporter);
	const context: PendingUpdateContext = { entry, secretValues, loopback, facts };
	if (receipt.pendingUpdateReport !== undefined) {
		if (reporting.reporter === null) {
			return { outcome: "unreportable", reason: "no runtime credential to report with" };
		}
		return deliverPendingUpdateReport(
			host,
			paths,
			receipt,
			reporting.reporter,
			reporting.redact,
			context,
		);
	}
	if (receipt.rollbackIncomplete !== undefined) {
		// Plan 06-46 (WR-07): never back up, pull or swap over an unknown host state.
		return { outcome: "skipped", reason: rollbackIncompleteReason(receipt.rollbackIncomplete) };
	}
	const plan = planContainerCompanionAt(entry, secretValues, facts);
	if (receipt.pendingUpdateRollback !== undefined) {
		// Plan 06-47 (D-28): a swap was interrupted after its write-ahead. It is rolled back from its
		// own pre-update backup -- whatever the entry pins now -- and never resumed: resuming would
		// take a "pre-update" backup of a schema the new digest may already have written, and a
		// later rollback would restore that under the old units.
		if (reporting.reporter === null) {
			return { outcome: "unreportable", reason: "no runtime credential to report with" };
		}
		return rollBackInterruptedSwap({
			host,
			paths,
			entry,
			secretValues,
			loopback,
			facts,
			receipt,
			rollbackTo: receipt.pendingUpdateRollback,
			// What the swap was moving to, as its write-ahead recorded it (06-REVIEW WR-01) -- never
			// the entry's pin, which may name a later update by now. A record written before that
			// falls back to the entry when it still names an update, otherwise to the receipt.
			swapped:
				receipt.pendingUpdateRollback.to ??
				(plan.kind === "update" ? plan.receipt.images : receipt.images),
			reporter: reporting.reporter,
			redact: reporting.redact,
			context,
		});
	}
	// The digests, not `plan.kind`: a receipt with no verified subnet plans `install` even for a
	// digest change (plan 06-49), and the subnet check below is what answers that case here.
	if (sameImages(receipt.images, plan.receipt.images)) {
		if (receipt.refusedUpdate !== undefined) {
			// The pin came back to what runs: a later acceptance of the same digest is a new update.
			const { refusedUpdate: _cleared, ...rest } = receipt;
			writeReceipt(host, paths, rest);
		}
		return { outcome: "skipped", reason: "no digest change" };
	}
	// Plan 06-41 (CR-03): the app digest changes only on the transition the control plane announced.
	// Digests carry no order, so an older target is never recognised here; instead every app-digest
	// change the entry does not announce exactly -- from what this host runs, to the pin -- is
	// refused before any backup, pull, write or restart. A dependency-only change needs none.
	const running = digestOf(receipt.images.app);
	const pinned = entry.artifact.sha256;
	if (running !== pinned) {
		const announced = entry.update;
		if (announced === null || announced.fromDigest !== running || announced.toDigest !== pinned) {
			// Plan 06-47: the host holds a digest other than the pin, the same condition as a held
			// install; the control plane sees it through the condition record.
			if (running !== null) {
				raiseContainerCompanionCondition(host, paths, {
					installationId: entry.installationId,
					code: "schema_digest_mismatch",
					detail: schemaDigestMismatchDetail(running, pinned),
				});
			}
			return {
				outcome: "unrequested",
				reason: `the control plane did not announce a change from ${running ?? "an unpinned image"} to ${pinned}`,
			};
		}
	}
	if (receipt.bootstrap?.phase !== "ready") {
		return { outcome: "skipped", reason: "installation not ready" };
	}
	if (
		receipt.refusedUpdate !== undefined &&
		sameReferences(receipt.refusedUpdate.images, plan.receipt.images)
	) {
		return { outcome: "skipped", reason: "update already refused; awaiting the previous pin" };
	}
	// Plan 06-49 (G-06-2 WR-06): the swap renders the guard and the network's `Subnet=` and restarts
	// units, so it runs only over a network whose LIVE subnet is the pinned one. The check is
	// read-only and comes before any backup, pull, write or restart. The swap never migrates: the
	// converge's install path owns that, which keeps stopping units out of the update flow. A
	// mismatch drops the recorded subnet, so the next converge plans that install, and raises the
	// condition.
	const liveSubnet = readLiveSubnet(plan, host);
	if (liveSubnet !== plan.guard.subnet) {
		const names = containerCompanionUnitNames(entry.kind);
		const reason = `the update waits: network ${names.stem} reports subnet ${liveSubnet}, not the pinned ${plan.guard.subnet}, and the next converge moves it`;
		if (receipt.networkSubnet !== undefined) {
			const { networkSubnet: _unverified, ...unverified } = receipt;
			writeReceipt(host, paths, unverified);
		}
		raiseContainerCompanionCondition(host, paths, {
			installationId: entry.installationId,
			code: "network_subnet_unverified",
			detail: reporting.redact(reason),
		});
		return { outcome: "skipped", reason: reporting.redact(reason) };
	}
	if (receipt.networkSubnet !== liveSubnet) {
		// Verified now: every receipt this pass writes spreads `receipt`, so each one carries it.
		receipt.networkSubnet = liveSubnet;
		writeReceipt(host, paths, receipt);
	}
	if (reporting.reporter === null) {
		// Never swap what cannot be reported: the control plane must learn how the update ended.
		return { outcome: "unreportable", reason: "no runtime credential to report with" };
	}
	const { reporter, redact } = reporting;
	// The update this pass runs (G-06-4): every report it makes names it, fixed now from the plan,
	// so a report kept in the receipt goes on naming it whatever the entry pins when it is sent.
	const toDigest = planAppDigest(plan);
	const progress = async (step: string, message: string): Promise<void> => {
		try {
			await reporter.send({
				operation: "update",
				phase: "progress",
				step,
				lines: [{ stream: "stdout", line: redact(message) }],
				to_digest: toDigest,
			});
		} catch {
			// Progress is best effort: the terminal report is the one that must arrive.
		}
	};
	const previousPlan = planContainerCompanionAt(
		entryAtReceipt(entry, receipt),
		secretValues,
		facts,
	);

	/** Record the refusal and its report in the receipt, then deliver it. */
	const refuse = (
		step: PostizUpdateStep,
		message: string,
		label: string | null,
	): Promise<PostizUpdateOutcome> =>
		refusePostizUpdate({
			host,
			paths,
			entry,
			receipt,
			images: plan.receipt.images,
			toDigest,
			reporter,
			redact,
			context,
			step,
			message,
			label,
		});

	// 1. The gate: a backup taken by THIS pass, after it saw the new digest.
	await progress("backup", POSTIZ_UPDATE_BACKUP_MESSAGE);
	let gate: { label: string; schedule: ContainerCompanionBackupSchedule };
	try {
		gate = await takePreUpdateBackup({
			host,
			entry,
			reporter,
			redact,
			now: host.now(),
			schemaDigest: schemaDigestOf(facts),
		});
	} catch (error) {
		if (error instanceof PostizUpdateRefusal) return refuse(error.step, error.message, null);
		throw error;
	}
	await progress("backup", `Backup ${gate.label} was taken before updating.`);

	// 2. What the update must not lose, counted before anything changes.
	let before: number;
	try {
		before = countPostizOrganizations(host, entry);
	} catch (error) {
		return refuse(
			"pre-update",
			`the organization count before the update was unreadable:${error instanceof Error ? error.message : String(error)}`,
			gate.label,
		);
	}

	// 3. The swap: pull and verify every image first, so a registry failure changes nothing.
	await progress("swap", POSTIZ_UPDATE_SWAP_MESSAGE);
	for (const image of plan.images) {
		const problem = pullVerifiedImage(host, image);
		if (problem !== null) {
			return refuse("pre-update", `the update was not applied: ${problem}`, gate.label);
		}
		// Each pull may take up to its own timeout: report between them, which pushes the control
		// plane's reporting deadline out, so a healthy update is never failed by it (plan 06-40).
		await progress("swap", `Pulled ${image.image}.`);
	}
	// Plan 06-48 (G-06-1 WR-02, D-14): the repository outlives this host. Before the new app can
	// write its schema, the pre-update backup records that WAL archived after it may hold that
	// digest's schema, so a replacement host restores this backup's own point instead of the end of
	// the archive when the swap is never confirmed. A dependency-only update keeps the schema's
	// writer and records nothing. Without the record no swap is started.
	const nextSchemaDigest = planAppDigest(plan);
	if (nextSchemaDigest !== schemaDigestOf(facts)) {
		const annotated = annotateBackup(host, entry, gate.label, nextSchemaDigest);
		if (annotated.status !== 0) {
			const detail = commandDetail(annotated);
			return refuse(
				"pre-update",
				`the update was not applied: the pre-update backup ${gate.label} could not record the next schema digest${detail ? `: ${detail}` : ""}`,
				gate.label,
			);
		}
	}
	// The write-ahead (plan 06-47, D-28), before any unit is rendered or restarted: the data
	// directory names the app about to write its schema, and the receipt names the swap to undo
	// -- this pass's backup and the count it must find again. A pass that dies from here on is
	// rolled back from that backup by the next one, never resumed from a fresh one.
	const {
		refusedUpdate: _previous,
		pendingUpdateReport: _noReport,
		pendingUpdateRollback: _noRollback,
		...kept
	} = receipt;
	const rollbackTo = {
		previous: kept,
		label: gate.label,
		organizations: before,
		to: plan.receipt.images,
	};
	writeSchemaDigestMarker(host, paths, planAppDigest(plan));
	writeReceipt(host, paths, {
		...receipt,
		schemaDigest: planAppDigest(plan),
		pendingUpdateRollback: rollbackTo,
	});
	const restart = servicesToRestart(paths, plan, receipt, facts.sourcesOnDisk);
	const journal = new WriteJournal(host);
	let assertion: CompanionReadyAssertion | null = null;
	let problem: string | null = null;
	try {
		renderUnits(host, paths, plan, receipt, journal);
		if (restart.length > 0) {
			mustRun(host, `restart of ${restart.join(", ")}`, "systemctl", ["restart", ...restart]);
		}
		// 4. Proof on data: connected with the key, and the organizations it had before. The wait for
		// Postiz to answer can be long, so it starts with a fresh reporting deadline (plan 06-40).
		await progress("post-update-assertion", "Waiting for Postiz to answer after the update.");
		const checked = await assertAfterUpdate(entry, secretValues, host, loopback, before);
		assertion = checked.assertion;
		problem = checked.problem;
		if (checked.passed) {
			const adminEmail = postizAdminCredential(entry, secretValues).email;
			const adminUserId = readPostizAdminUserId(host, entry, adminEmail);
			const appliedAt = host.now().toISOString();
			const updated: ContainerCompanionReceipt = {
				...kept,
				...plan.receipt,
				appliedAt,
				// The new app has started on the data: it wrote the schema now (plan 06-47).
				schemaDigest: planAppDigest(plan),
				backupSchedule: gate.schedule,
				pendingUpdateReport: {
					operation: "update",
					phase: "ready",
					step: "ready",
					assertion,
					applied_digest: entry.artifact.sha256,
					admin_user_id: adminUserId,
					to_digest: toDigest,
				},
				// In the same write as the report: if the control plane refuses it, this is where the
				// host goes back to (plan 06-40, CR-03).
				pendingUpdateRollback: rollbackTo,
			};
			delete updated.bootstrap;
			delete updated.restore;
			writeReceipt(host, paths, updated);
			return deliverPendingUpdateReport(host, paths, updated, reporter, redact, context);
		}
	} catch (error) {
		problem = error instanceof Error ? error.message : String(error);
	}

	// 5. Roll back: the pre-update backup set, under the previous digest.
	await progress(
		"post-update-assertion",
		`Postiz did not pass its post-update check (${problem ?? "unknown"}); restoring backup ${gate.label} and the previous version.`,
	);
	const rollback = await rollBackPostizUpdate({
		host,
		paths,
		entry,
		secretValues,
		loopback,
		previousPlan,
		fromReceipt: plan.receipt,
		label: gate.label,
		before,
	});
	return settlePostUpdateRollback({
		host,
		paths,
		entry,
		receipt,
		previous: kept,
		swapped: plan.receipt.images,
		toDigest,
		label: gate.label,
		before,
		rollback,
		message: `Postiz did not pass its post-update check (${problem ?? "unknown"}); ${rollback.sentence}`,
		reporter,
		redact,
		context,
	});
}

// ---------------------------------------------------------------------------------------------
// The tenant export's database dump (plan 06-19, D-31, APP-07)
// ---------------------------------------------------------------------------------------------
//
// A tenant export that promised Postiz's data names this installation's pending jobs in the
// entry's `exportRequests`. For each job the runtime has not delivered yet, it dumps the database
// ON THE HOST -- the database is reachable only on the installation's own network -- and PUTs the
// gzip stream to `/v1/runtime/companion-installations/{id}/exports/{jobId}` with its own runtime
// credential. The control plane adds the dump and the media bucket's objects to the tenant's
// Phase 3 archive.
//
// The dump is restorable without the platform's credential (APP-03, T-06-19-01):
//
//   SET session_replication_role = replica;      -- restore without foreign-key ordering
//   <pg_dump --schema=public --schema-only --no-owner --no-privileges>
//   COPY "public"."<table>" (<columns>) FROM stdin;  -- one per public table, rows from
//   <COPY (SELECT <columns> FROM "public"."<table>") TO STDOUT>
//   \.
//
// with the `Organization` query selecting `NULL AS "apiKey"` in that column's place, so no
// export ever carries the platform-generated API key. The social-account tokens Postiz stores
// for the tenant are the tenant's own data and are exported. `iforge_rehearsal` is platform-
// internal and never dumped: only `public` is named, by both the schema dump and the listing.
//
// Every command goes through `podman exec -i` into the database container over its local socket:
// no password is needed and nothing secret is ever on a command line. The output is streamed
// through gzip into a root-only spool file and the upload streams that file, so the dump is never
// held whole in memory, and a retried upload re-reads the file rather than re-dumping.

/** Streams one command's standard output, chunk by chunk, instead of buffering it. */
export interface ContainerStreamHost {
	stream(
		command: string,
		args: readonly string[],
		options: { input?: string; onStdout: (chunk: Uint8Array) => Promise<void> | void },
	): Promise<{ status: number; stderr: string }>;
}

export class PostizExportError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "PostizExportError";
	}
}

/** The one Postiz column the platform generates and that no export may carry (APP-03). */
const POSTIZ_API_KEY_TABLE = "Organization";
const POSTIZ_API_KEY_COLUMN = "apiKey";
/** How many delivered job ids the receipt remembers; older ones are no longer in any manifest. */
const DELIVERED_EXPORTS_KEPT = 20;

export const EXPORT_UPLOAD_MAX_ATTEMPTS = 5;
export const EXPORT_UPLOAD_BASE_DELAY_MS = 1_000;
export const EXPORT_UPLOAD_MAX_DELAY_MS = 30_000;
export const EXPORT_UPLOAD_TIMEOUT_MS = 1_800_000;

/** A SQL identifier, double-quoted with embedded quotes doubled. */
function sqlIdentifier(name: string): string {
	return `"${name.replace(/"/g, '""')}"`;
}

function psqlExportArgs(container: string): string[] {
	return [
		"exec",
		"-i",
		container,
		"psql",
		"-X",
		"-q",
		"-t",
		"-A",
		"-v",
		"ON_ERROR_STOP=1",
		"-U",
		POSTIZ_DB_USER,
		"-d",
		POSTIZ_DB_NAME,
	];
}

async function streamOrThrow(
	stream: ContainerStreamHost,
	args: string[],
	what: string,
	onStdout: (chunk: Uint8Array) => Promise<void> | void,
	input?: string,
): Promise<void> {
	const result = await stream.stream("podman", args, {
		...(input === undefined ? {} : { input }),
		onStdout,
	});
	if (result.status !== 0) {
		const detail = result.stderr.trim().split("\n").slice(-5).join(" | ").slice(0, 500);
		throw new PostizExportError(`${what} failed${detail ? `: ${detail}` : ""}`);
	}
}

/** Every public base table's non-generated columns, in ordinal order, from the database itself. */
async function listPublicColumns(
	stream: ContainerStreamHost,
	container: string,
): Promise<Map<string, string[]>> {
	const decoder = new TextDecoder();
	let text = "";
	await streamOrThrow(
		stream,
		psqlExportArgs(container),
		"listing the database's tables",
		(chunk) => {
			text += decoder.decode(chunk, { stream: true });
		},
		[
			"SELECT coalesce(json_agg(json_build_array(c.table_name, c.column_name) ORDER BY c.table_name, c.ordinal_position), '[]'::json)",
			"FROM information_schema.columns c",
			"JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name",
			"WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' AND c.is_generated = 'NEVER';",
			"",
		].join("\n"),
	);
	text += decoder.decode();
	let pairs: unknown;
	try {
		pairs = JSON.parse(text.trim() || "[]");
	} catch {
		throw new PostizExportError("the table listing was not JSON");
	}
	if (!Array.isArray(pairs)) throw new PostizExportError("the table listing was not a list");
	const tables = new Map<string, string[]>();
	for (const pair of pairs) {
		if (
			!Array.isArray(pair) ||
			pair.length !== 2 ||
			typeof pair[0] !== "string" ||
			typeof pair[1] !== "string"
		) {
			throw new PostizExportError("the table listing had an unreadable row");
		}
		const columns = tables.get(pair[0]) ?? [];
		columns.push(pair[1]);
		tables.set(pair[0], columns);
	}
	return tables;
}

/** The SELECT list for one table: every column, with the platform API key replaced by NULL. */
function exportSelectList(table: string, columns: readonly string[]): string {
	return columns
		.map((column) =>
			table === POSTIZ_API_KEY_TABLE && column === POSTIZ_API_KEY_COLUMN
				? `NULL AS ${sqlIdentifier(column)}`
				: sqlIdentifier(column),
		)
		.join(", ");
}

/**
 * Write a restorable, credential-free dump of this installation's database to `write`, streamed.
 *
 * Throws `PostizExportError` naming the step that failed; nothing it wrote is then worth keeping.
 */
export async function dumpPostizDatabase(input: {
	stream: ContainerStreamHost;
	entry: PostizCompanionManifest;
	write: (chunk: string | Uint8Array) => Promise<void>;
}): Promise<{ tables: number }> {
	const container = containerCompanionUnitNames(input.entry.kind).databaseContainer;
	await input.write("SET session_replication_role = replica;\n\n");
	await streamOrThrow(
		input.stream,
		[
			"exec",
			"-i",
			container,
			"pg_dump",
			"-U",
			POSTIZ_DB_USER,
			"-d",
			POSTIZ_DB_NAME,
			"--schema=public",
			"--schema-only",
			"--no-owner",
			"--no-privileges",
		],
		"the schema dump",
		(chunk) => input.write(chunk),
	);
	const tables = await listPublicColumns(input.stream, container);
	for (const [table, columns] of [...tables].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) {
		if (columns.length === 0) continue;
		const qualified = `"public".${sqlIdentifier(table)}`;
		await input.write(
			`\nCOPY ${qualified} (${columns.map(sqlIdentifier).join(", ")}) FROM stdin;\n`,
		);
		await streamOrThrow(
			input.stream,
			psqlExportArgs(container),
			`copying ${table}`,
			(chunk) => input.write(chunk),
			`COPY (SELECT ${exportSelectList(table, columns)} FROM ${qualified}) TO STDOUT;\n`,
		);
		await input.write("\\.\n");
	}
	return { tables: tables.size };
}

/** Dump the database through gzip into a new private spool file; returns its size in bytes. */
async function writeExportSpool(input: {
	stream: ContainerStreamHost;
	entry: PostizCompanionManifest;
	path: string;
}): Promise<number> {
	const gzip = createGzip();
	const file = createWriteStream(input.path, { flags: "wx", mode: PRIVATE_FILE_MODE });
	const piped = pipeline(gzip, file);
	// A rejected pipeline surfaces through `piped`; this keeps it from being reported unhandled
	// while the dump is still writing.
	piped.catch(() => {});
	const write = async (chunk: string | Uint8Array) => {
		if (!gzip.write(chunk)) await once(gzip, "drain");
	};
	try {
		await dumpPostizDatabase({ stream: input.stream, entry: input.entry, write });
		gzip.end();
		await piped;
	} catch (error) {
		gzip.destroy();
		await piped.catch(() => {});
		throw error;
	}
	return statSync(input.path).size;
}

export type CompanionExportUploadOutcome =
	/** Stored by the control plane. */
	| { outcome: "uploaded"; status: number }
	/** 409: the job no longer wants this component (done, failed or already received). */
	| { outcome: "not-wanted"; status: 409 };

/**
 * PUT one export's gzip dump to its job's upload path with the runtime credential.
 *
 * `body` opens a FRESH stream per attempt, so a retry re-reads the spool rather than re-dumping.
 * Transport failures, 429 and 5xx are retried with a doubling delay capped at `maxDelayMs`, up to
 * `maxAttempts` sends, then the error surfaces. A 409 means the job no longer wants this upload,
 * which is done, not failed. Any other 4xx is a refusal the same bytes can never fix. Every error
 * message is redacted of the credential.
 */
export async function uploadCompanionExport(input: {
	apiUrl: string;
	token: string;
	installationId: string;
	jobId: string;
	body: () => ReadableStream<Uint8Array> | Uint8Array;
	size: number;
	fetch?: CompanionFetch;
	sleep?: (ms: number) => Promise<void>;
	maxAttempts?: number;
	baseDelayMs?: number;
	maxDelayMs?: number;
	timeoutMs?: number;
}): Promise<CompanionExportUploadOutcome> {
	const base = new URL(input.apiUrl);
	if (base.protocol !== "https:" && base.protocol !== "http:") {
		throw new PostizExportError(
			`export uploads need an http(s) control plane, not ${base.protocol}`,
		);
	}
	const url = `${input.apiUrl.replace(/\/+$/, "")}/v1/runtime/companion-installations/${encodeURIComponent(input.installationId)}/exports/${encodeURIComponent(input.jobId)}`;
	const fetchImpl = input.fetch ?? fetch;
	const sleep = input.sleep ?? defaultSleep;
	const maxAttempts = Math.max(1, input.maxAttempts ?? EXPORT_UPLOAD_MAX_ATTEMPTS);
	const maxDelay = input.maxDelayMs ?? EXPORT_UPLOAD_MAX_DELAY_MS;
	let delay = input.baseDelayMs ?? EXPORT_UPLOAD_BASE_DELAY_MS;
	const redact = (text: string) => redactKnownSecrets(text, secretForms(input.token));
	for (let attempt = 1; ; attempt += 1) {
		let response: Response | null = null;
		let lastFailure = "";
		try {
			response = await fetchImpl(url, {
				method: "PUT",
				headers: {
					authorization: `Bearer ${input.token}`,
					"content-type": "application/gzip",
					"content-length": String(input.size),
				},
				body: input.body(),
				// A streamed request body must say so to Node's fetch.
				duplex: "half",
				// A redirect would carry the bearer token somewhere this client never chose.
				redirect: "error",
				signal: AbortSignal.timeout(input.timeoutMs ?? EXPORT_UPLOAD_TIMEOUT_MS),
			} as RequestInit);
		} catch (error) {
			lastFailure = error instanceof Error ? error.message : String(error);
		}
		if (response !== null) {
			if (response.ok) return { outcome: "uploaded", status: response.status };
			if (response.status === 409) return { outcome: "not-wanted", status: 409 };
			const detail = (await response.text().catch(() => "")).trim().slice(0, 300);
			lastFailure = `HTTP ${response.status}${detail ? `: ${detail}` : ""}`;
			if (response.status < 500 && response.status !== 429) {
				throw new PostizExportError(redact(`export upload refused: ${lastFailure}`));
			}
		}
		if (attempt >= maxAttempts) {
			throw new PostizExportError(
				redact(`export upload failed after ${attempt} attempts: ${lastFailure}`),
			);
		}
		await sleep(delay);
		delay = Math.min(delay * 2, maxDelay);
	}
}

export type PostizExportDeliveryOutcome =
	| { outcome: "skipped"; reason: string }
	| {
			outcome: "delivered";
			/** Jobs uploaded (or no longer wanted) in this pass, recorded in the receipt. */
			delivered: string[];
			/** Jobs that failed in this pass; each is tried again by the next one. */
			failed: Array<{ jobId: string; error: string }>;
	  };

/**
 * Answer the entry's `exportRequests`: dump and upload once per job this runtime has not
 * delivered yet, and record each delivered job in the receipt so it is never dumped again.
 *
 * Runs from the converge and from the maintenance pass. Never throws for one job's failure (that
 * job is reported in `failed`, redacted, and retried by the next pass); the export fails by name
 * on the control plane if no pass delivers it in time.
 */
export async function deliverPostizExportRequests(input: {
	manifest: RuntimeManifest;
	secretValues: Record<string, string> | undefined;
	paths: Pick<RuntimePaths, "serviceStateRoot" | "configurationRoot" | "runRoot">;
	host?: ContainerHost;
	stream?: ContainerStreamHost;
	fetch?: CompanionFetch;
	sleep?: (ms: number) => Promise<void>;
	/** Where spool files are written; defaults to the companion's root-only state directory. */
	exportRoot?: string;
}): Promise<PostizExportDeliveryOutcome> {
	const entry = input.manifest.companions?.postiz;
	if (entry?.lifecycle !== "present") return { outcome: "skipped", reason: "no present entry" };
	if (entry.exportRequests.length === 0) {
		return { outcome: "skipped", reason: "no export requests" };
	}
	const host = input.host ?? nodeContainerHost();
	const paths = containerCompanionPaths(input.paths, entry.kind);
	const receipt = parseReceipt(host.readFile(paths.receiptPath));
	if (receipt?.state !== "applied" || receipt.installationId !== entry.installationId) {
		return { outcome: "skipped", reason: "installation not applied" };
	}
	if (receipt.rollbackIncomplete !== undefined) {
		// Plan 06-46 (WR-07): a tenant export is never a dump of a half-restored database.
		return { outcome: "skipped", reason: rollbackIncompleteReason(receipt.rollbackIncomplete) };
	}
	if (receipt.bootstrap?.phase !== "ready") {
		return { outcome: "skipped", reason: "installation not ready" };
	}
	const secretValues = input.secretValues ?? {};
	const token = readRuntimeCredential(secretValues);
	if (!token) return { outcome: "skipped", reason: "no runtime credential" };
	const { redact } = postizReporting(input.manifest, entry, secretValues, undefined);
	const already = new Set(receipt.deliveredExports ?? []);
	const pending = entry.exportRequests
		.map((request) => request.jobId)
		.filter((jobId) => !already.has(jobId));
	if (pending.length === 0) return { outcome: "skipped", reason: "every request delivered" };

	const stream = input.stream ?? nodeContainerStreamHost();
	const exportRoot = input.exportRoot ?? join(paths.companionStateRoot, "exports");
	mkdirSync(exportRoot, { recursive: true, mode: 0o700 });
	const delivered: string[] = [];
	const failed: Array<{ jobId: string; error: string }> = [];
	for (const jobId of pending) {
		const spool = join(exportRoot, `${jobId}.sql.gz`);
		rmSync(spool, { force: true });
		try {
			const size = await writeExportSpool({ stream, entry, path: spool });
			await uploadCompanionExport({
				apiUrl: input.manifest.controlPlane.apiUrl,
				token,
				installationId: entry.installationId,
				jobId,
				// Node's web stream and the DOM's are the same object at run time, distinct types.
				body: () =>
					Readable.toWeb(createReadStream(spool)) as unknown as ReadableStream<Uint8Array>,
				size,
				...(input.fetch ? { fetch: input.fetch } : {}),
				...(input.sleep ? { sleep: input.sleep } : {}),
			});
			delivered.push(jobId);
			recordDeliveredExport(host, paths, jobId);
		} catch (error) {
			failed.push({
				jobId,
				error: redact(error instanceof Error ? error.message : String(error)),
			});
		} finally {
			rmSync(spool, { force: true });
		}
	}
	return { outcome: "delivered", delivered, failed };
}

/** Remember one delivered job in the CURRENT receipt, keeping the newest few. */
function recordDeliveredExport(
	host: ContainerHost,
	paths: ContainerCompanionPaths,
	jobId: string,
): void {
	const current = parseReceipt(host.readFile(paths.receiptPath));
	if (current === null) return;
	const kept = [...(current.deliveredExports ?? []).filter((id) => id !== jobId), jobId].slice(
		-DELIVERED_EXPORTS_KEPT,
	);
	host.writeFile(
		paths.receiptPath,
		`${JSON.stringify({ ...current, deliveredExports: kept }, null, 2)}\n`,
		{ mode: PRIVATE_FILE_MODE },
	);
}

// ---------------------------------------------------------------------------------------------
// The real host
// ---------------------------------------------------------------------------------------------

const PULL_TIMEOUT_MS = 1_800_000;
const COMMAND_TIMEOUT_MS = 120_000;
/** A backup or a restore moves the whole database; two minutes would cut a real one short. */
const BACKUP_COMMAND_TIMEOUT_MS = 3_600_000;

/** How long the production host lets one command run before it is killed. */
export function containerCommandTimeoutMs(command: string, args: readonly string[]): number {
	if (command !== "podman") return COMMAND_TIMEOUT_MS;
	if (args[0] === "pull") return PULL_TIMEOUT_MS;
	if (args.includes("pgbackrest") && (args.includes("backup") || args.includes("restore"))) {
		return BACKUP_COMMAND_TIMEOUT_MS;
	}
	return COMMAND_TIMEOUT_MS;
}

/**
 * The production `ContainerStreamHost` (plan 06-19): standard output is handed on chunk by chunk,
 * with the child paused while a chunk is being written, so a dump is never buffered whole.
 * Standard input carries the psql script; nothing secret is ever on the command line.
 */
export function nodeContainerStreamHost(): ContainerStreamHost {
	return {
		stream(command, args, options) {
			return new Promise((resolve) => {
				const child = spawn(command, [...args], { stdio: ["pipe", "pipe", "pipe"] });
				let stderr = "";
				let failure: string | null = null;
				let pending: Promise<void> = Promise.resolve();
				const timer = setTimeout(() => {
					failure = "timed out";
					child.kill("SIGKILL");
				}, BACKUP_COMMAND_TIMEOUT_MS);
				child.stdout.on("data", (chunk: Buffer) => {
					child.stdout.pause();
					pending = pending
						.then(() => options.onStdout(new Uint8Array(chunk)))
						.catch((error: unknown) => {
							failure ??= error instanceof Error ? error.message : String(error);
							child.kill("SIGKILL");
						})
						.finally(() => child.stdout.resume());
				});
				child.stderr.on("data", (chunk: Buffer) => {
					if (stderr.length < 64 * 1024) stderr += chunk.toString("utf8");
				});
				child.on("error", (error) => {
					failure ??= error.message;
				});
				child.on("close", (code) => {
					clearTimeout(timer);
					void pending.then(() =>
						resolve({
							status: failure === null ? (code ?? 1) : 1,
							stderr: [failure, stderr].filter(Boolean).join("\n"),
						}),
					);
				});
				child.stdin.end(options.input ?? "");
			});
		},
	};
}

/**
 * The production `ContainerHost`. Root-only: the identity step refuses a non-root converge before
 * anything is written.
 */
export function nodeContainerHost(): ContainerHost {
	return {
		run(command, args, options) {
			const result = spawnSync(command, [...args], {
				encoding: "utf8",
				// Standard input is the one channel a secret may use (never argv).
				...(options?.input === undefined ? {} : { input: options.input }),
				maxBuffer: 16 * 1024 * 1024,
				timeout: containerCommandTimeoutMs(command, args),
			});
			return {
				status: result.status ?? 1,
				stdout: result.stdout ?? "",
				stderr: [result.error?.message, result.stderr].filter(Boolean).join("\n"),
			};
		},
		readFile(path) {
			try {
				const stat = lstatSync(path);
				if (stat.isSymbolicLink() || !stat.isFile()) {
					throw new Error(`container companion path is not a regular file: ${path}`);
				}
				return readFileSync(path, "utf8");
			} catch (error) {
				if ((error as NodeJS.ErrnoException)?.code === "ENOENT") return null;
				throw error;
			}
		},
		writeFile(path, content, options) {
			writePrivateFileAtomic(path, content, { mode: options.mode, dirMode: 0o755 });
			chownSync(path, options.uid ?? 0, options.gid ?? 0);
		},
		removeFile(path) {
			rmSync(path, { force: true });
		},
		realPath(path) {
			try {
				return realpathSync(path);
			} catch (error) {
				if ((error as NodeJS.ErrnoException)?.code === "ENOENT") return null;
				throw error;
			}
		},
		removeTree(path) {
			// `rm -r` semantics: a symlink inside (or at) the path is removed as a link, never followed.
			rmSync(path, { recursive: true, force: true });
		},
		stat(path) {
			try {
				const stat = lstatSync(path);
				return {
					isFile: stat.isFile() && !stat.isSymbolicLink(),
					isDirectory: stat.isDirectory() && !stat.isSymbolicLink(),
					mode: stat.mode & 0o7777,
					uid: stat.uid,
					gid: stat.gid,
				};
			} catch {
				return null;
			}
		},
		ensureDirectory(path, options) {
			mkdirSync(path, { recursive: true, mode: options.mode });
			const stat = lstatSync(path);
			if (!stat.isDirectory() || stat.isSymbolicLink()) {
				throw new Error(`container companion path is not a trusted directory: ${path}`);
			}
			chownSync(path, options.uid, options.gid);
			chmodSync(path, options.mode);
		},
		ensureIdentity(kind) {
			if (!runningAsRoot()) {
				throw new Error("container companions require root reconciliation");
			}
			const runtimeUser = process.env.CLAWDI_RUNTIME_USER?.trim() || HOSTED_RUNTIME_USER;
			// The cross-kind refusal (COMP-05): the identity is refused when another registry kind's
			// account or group on this host already holds its uid or gid.
			return ensureExclusiveCompanionServiceIdentity({
				kind,
				runtimeUser,
				ensure: ensureCompanionServiceIdentity,
				observe: observeCompanionServiceIdentity,
			});
		},
		now() {
			return new Date();
		},
	};
}
