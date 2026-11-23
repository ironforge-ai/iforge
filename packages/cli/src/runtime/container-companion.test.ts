import { afterEach, describe, expect, it } from "bun:test";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { gunzipSync } from "node:zlib";
import {
	type CompanionInstallReport,
	CompanionProgressReporter,
	type CompanionReportResult,
	type CompanionReportSink,
	installationSecretValues,
} from "./companion-progress";
import { reconcilableCompanionKinds } from "./companion-registry";
import * as containerCompanionModule from "./container-companion";
import {
	applyContainerCompanion,
	ContainerCompanionConditionError,
	type ContainerCompanionPaths,
	type ContainerHost,
	type ContainerHostCommandResult,
	type ContainerHostStat,
	type ContainerStreamHost,
	containerCommandTimeoutMs,
	containerCompanionPaths,
	containerCompanionSubuidBase,
	convergePostizReadiness,
	deliverPostizExportRequests,
	planContainerCompanion,
	readContainerCompanionFacts,
	runContainerCompanionMaintenance,
	withdrawContainerCompanion,
} from "./container-companion";
import {
	type PostizCompanionManifest,
	postizCompanionSchema,
	type RuntimeManifest,
} from "./manifest-contract";
import { manifestSecretRefs } from "./manifest-source";
import { getRuntimePaths, type RuntimePaths } from "./paths";
import {
	reconcileContainerCompanions,
	writeRuntimeSystemdState,
} from "./runtime-systemd-reconciliation";

// ---------------------------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------------------------

const REPO_ROOT = join(import.meta.dir, "..", "..", "..", "..");
const FIXTURE_PATH = join(REPO_ROOT, "test-fixtures", "companions", "postiz-manifest-entry.json");

function fixtureEntry(): PostizCompanionManifest {
	return postizCompanionSchema.parse(JSON.parse(readFileSync(FIXTURE_PATH, "utf8")));
}

// Distinctive tokens so a leak into any rendered source is unambiguous.
const APP_SECRET_TOKEN = "app-jwt-secret-7f3c91d2";
const DB_PASSWORD_TOKEN = "db-password-4b8e02aa";
const API_KEY_TOKEN = "api-key-9d1e77c0";
const ADMIN_TOKEN = "admin-secret-2c6a5f13";

function fixtureSecrets(entry: PostizCompanionManifest): Record<string, string> {
	return {
		[entry.credentials.appEnvSecretRef]: [
			`JWT_SECRET=${APP_SECRET_TOKEN}`,
			`DATABASE_URL=postgresql://postiz:${DB_PASSWORD_TOKEN}@iforge-postiz-db:5432/postiz`,
			"REDIS_URL=redis://iforge-postiz-redis:6379",
			"",
		].join("\n"),
		[entry.credentials.dbEnvSecretRef]: [
			"POSTGRES_USER=postiz",
			`POSTGRES_PASSWORD=${DB_PASSWORD_TOKEN}`,
			"POSTGRES_DB=postiz",
			"",
		].join("\n"),
		[entry.credentials.apiKeySecretRef]: API_KEY_TOKEN,
		[entry.credentials.adminSecretRef]: ADMIN_TOKEN,
	};
}

const SECRET_TOKENS = [APP_SECRET_TOKEN, DB_PASSWORD_TOKEN, API_KEY_TOKEN, ADMIN_TOKEN];

// The hosted default layout. The fake host below is an in-memory filesystem, so these are never
// touched on the machine running the suite.
const HOSTED_PATHS = containerCompanionPaths({
	serviceStateRoot: "/var/lib/iforge",
	configurationRoot: "/etc/iforge",
	runRoot: "/run/iforge",
});

function reference(image: string, sha256: string): string {
	return `${image}@sha256:${sha256}`;
}

function fixtureReferences(entry: PostizCompanionManifest): string[] {
	return [
		reference(entry.artifact.image, entry.artifact.sha256),
		reference(entry.dependencies.postgres.image, entry.dependencies.postgres.sha256),
		reference(entry.dependencies.redis.image, entry.dependencies.redis.sha256),
	];
}

// ---------------------------------------------------------------------------------------------
// A recording fake host: an in-memory filesystem plus a scripted command runner.
// ---------------------------------------------------------------------------------------------

interface FakeFile {
	content: string;
	mode: number;
	uid: number;
	gid: number;
}

interface FakeHostOptions {
	quadlet?: boolean;
	systemdBanner?: string;
	tpm2?: boolean;
	credentialSecret?: { mode: number; uid: number } | null;
	/** RepoDigests the fake reports for a reference; defaults to exactly the reference. */
	repoDigests?: (ref: string) => string[];
	subuid?: string;
	subgid?: string;
	/** Images already present before the first pull. */
	preloaded?: string[];
	/** Make one command fail, to drive the rollback path. */
	failCommand?: (command: string, args: readonly string[]) => boolean;
	/** Answers `podman exec -i <db> psql …` from the script it reads on standard input. */
	psql?: (script: string) => ContainerHostCommandResult;
	/** Answers every `pgbackrest` invocation, in the database container or a one-shot one. */
	pgbackrest?: (argv: readonly string[]) => ContainerHostCommandResult;
	/** What `journalctl` prints for the app unit. */
	journal?: string;
	/** Record host events into this array (shared with the fake Postiz for one timeline). */
	events?: string[];
	/** Whether `/usr/sbin/nft` exists (plan 06-39); present by default. */
	nft?: boolean;
	/** What `podman network inspect` reports as the network's subnet; the pinned one by default. */
	networkSubnet?: string;
	/**
	 * The subnet the network has once `podman network rm` removed it and the network unit's
	 * restart recreated it (plan 06-49); the pinned one by default.
	 */
	recreatedSubnet?: string;
}

/** The pinned subnet of the postiz kind at registry uid 10101 (plan 06-39). */
const POSTIZ_SUBNET = "10.213.1.0/24";
const GUARD_PATH = "/var/lib/iforge/companions/postiz/loopback-guard.nft";
const GUARD_TABLE = "iforge_companion_postiz";

class FakeContainerHost implements ContainerHost {
	readonly files = new Map<string, FakeFile>();
	readonly directories = new Map<string, { mode: number; uid: number; gid: number }>();
	readonly commands: Array<{ command: string; args: string[]; input?: string }> = [];
	readonly events: string[];
	readonly identities: string[] = [];
	/** The nftables tables currently loaded, by name (`nft -f`, `nft delete table`). */
	readonly nftTables = new Set<string>();
	/** Make a command THROW -- the process dying under it -- rather than fail with a status. */
	throwOn: ((command: string, args: readonly string[]) => boolean) | null = null;
	/**
	 * The companion network's live subnet (plan 06-49): `null` once `podman network rm` removed it,
	 * until a restart of the network unit recreates it with `recreatedSubnet`.
	 */
	networkSubnet: string | null;
	/** While set, `podman network rm` fails with this text: a container is still attached. */
	networkRemovalFails: string | null = null;
	private readonly pulled = new Set<string>();
	private readonly options: FakeHostOptions;

	constructor(options: FakeHostOptions = {}) {
		this.options = options;
		this.events = options.events ?? [];
		this.networkSubnet = options.networkSubnet ?? POSTIZ_SUBNET;
		if (options.quadlet !== false) {
			this.files.set("/usr/libexec/podman/quadlet", { content: "", mode: 0o755, uid: 0, gid: 0 });
		}
		if (options.nft !== false) {
			this.files.set("/usr/sbin/nft", { content: "", mode: 0o755, uid: 0, gid: 0 });
		}
		const secret =
			options.credentialSecret === undefined ? null : (options.credentialSecret ?? null);
		if (secret) {
			this.files.set("/var/lib/systemd/credential.secret", {
				content: "k",
				mode: secret.mode,
				uid: secret.uid,
				gid: 0,
			});
		}
		this.files.set("/etc/subuid", {
			content: options.subuid ?? "iforge:100000:65536\n",
			mode: 0o644,
			uid: 0,
			gid: 0,
		});
		this.files.set("/etc/subgid", {
			content: options.subgid ?? "iforge:100000:65536\n",
			mode: 0o644,
			uid: 0,
			gid: 0,
		});
		for (const ref of options.preloaded ?? []) this.pulled.add(ref);
	}

	run(
		command: string,
		args: readonly string[],
		options?: { input?: string },
	): ContainerHostCommandResult {
		const argv = [...args];
		this.commands.push({
			command,
			args: argv,
			...(options?.input === undefined ? {} : { input: options.input }),
		});
		this.events.push(`run:${command} ${argv.join(" ")}`);
		const ok = (stdout = ""): ContainerHostCommandResult => ({ status: 0, stdout, stderr: "" });
		const fail = (stderr: string, status = 1): ContainerHostCommandResult => ({
			status,
			stdout: "",
			stderr,
		});
		if (this.throwOn?.(command, argv)) throw new Error(`interrupted: ${command} ${argv[0]}`);
		if (this.options.failCommand?.(command, argv)) return fail(`injected failure: ${command}`);
		if (command === "systemctl" && argv[0] === "--version") {
			return ok(this.options.systemdBanner ?? "systemd 255 (255.4-1ubuntu8)\n+PAM +AUDIT\n");
		}
		if (command === "systemd-creds" && argv[0] === "has-tpm2") {
			return this.options.tpm2 === false ? fail("no", 1) : ok("yes\n");
		}
		if (command === "systemd-creds" && argv[0] === "encrypt") {
			const name = argv.find((arg) => arg.startsWith("--name="))?.slice("--name=".length) ?? "";
			const input = argv[argv.length - 2] ?? "";
			const plain = this.files.get(input);
			if (!plain) return fail(`input ${input} missing`);
			if ((plain.mode & 0o777) !== 0o600) return fail(`input ${input} is not 0600`);
			// A stand-in ciphertext that never carries the plaintext.
			return ok(`CIPHERTEXT[${name}]:${Buffer.from(name).toString("base64")}\n`);
		}
		if (command === "podman" && argv[0] === "pull") {
			this.pulled.add(argv[1] ?? "");
			return ok("");
		}
		if (command === "podman" && argv[0] === "image" && argv[1] === "inspect") {
			const ref = argv[argv.length - 1] ?? "";
			if (!this.pulled.has(ref)) return fail(`image not known: ${ref}`, 125);
			const digests = this.options.repoDigests ? this.options.repoDigests(ref) : [ref];
			return ok(`${JSON.stringify(digests)}\n`);
		}
		if (
			command === "podman" &&
			(argv[0] === "exec" || argv[0] === "run") &&
			argv.includes("pgbackrest") &&
			this.options.pgbackrest
		) {
			return this.options.pgbackrest(argv);
		}
		if (command === "podman" && argv[0] === "exec" && argv.includes("pg_isready")) {
			return ok("/var/run/postgresql:5432 - accepting connections\n");
		}
		if (command === "podman" && argv[0] === "exec" && argv.includes("psql") && this.options.psql) {
			return this.options.psql(options?.input ?? "");
		}
		if (command === "/usr/sbin/nft") {
			if (argv[0] === "-f") {
				const ruleset = this.files.get(argv[1] ?? "")?.content;
				if (ruleset === undefined) return fail(`could not open ${argv[1]}`);
				for (const match of ruleset.matchAll(/^table inet ([A-Za-z0-9_]+) \{/gm)) {
					this.nftTables.add(match[1] ?? "");
				}
				return ok("");
			}
			const table = argv[3] ?? "";
			if (argv[0] === "list" && argv[1] === "table" && argv[2] === "inet") {
				return this.nftTables.has(table) ? ok(`table inet ${table} {\n}\n`) : fail("No such file");
			}
			if (argv[0] === "delete" && argv[1] === "table" && argv[2] === "inet") {
				return this.nftTables.delete(table) ? ok("") : fail("No such file");
			}
			return fail(`unexpected nft ${argv.join(" ")}`, 127);
		}
		if (command === "podman" && argv[0] === "network" && argv[1] === "inspect") {
			if (this.networkSubnet === null) return fail(`network ${argv[2]}: network not found`, 125);
			return ok(`${this.networkSubnet} \n`);
		}
		if (command === "podman" && argv[0] === "network" && argv[1] === "rm") {
			if (this.networkRemovalFails !== null) return fail(this.networkRemovalFails, 2);
			this.networkSubnet = null;
			return ok(`${argv[argv.length - 1]}\n`);
		}
		if (
			command === "systemctl" &&
			argv[0] === "restart" &&
			argv[1] === "iforge-postiz-network.service" &&
			this.networkSubnet === null
		) {
			// Quadlet's network unit runs `podman network create` with the source's `Subnet=`.
			this.networkSubnet = this.options.recreatedSubnet ?? POSTIZ_SUBNET;
			return ok("");
		}
		if (command === "journalctl") return ok(this.options.journal ?? "");
		if (command === "systemctl" && argv[0] === "is-active") return fail("inactive", 3);
		if (command === "systemctl") return ok("");
		return fail(`unexpected command ${command} ${argv.join(" ")}`, 127);
	}

	readFile(path: string): string | null {
		return this.files.get(path)?.content ?? null;
	}

	writeFile(path: string, content: string, options: { mode: number; uid?: number; gid?: number }) {
		this.files.set(path, {
			content,
			mode: options.mode,
			uid: options.uid ?? 0,
			gid: options.gid ?? 0,
		});
		this.events.push(`write:${path}`);
	}

	removeFile(path: string): void {
		if (this.files.delete(path)) this.events.push(`remove:${path}`);
	}

	/** Paths that resolve somewhere else, as a symlink would (plan 06-20's path guard). */
	readonly symlinks = new Map<string, string>();
	/** Every recursive delete, in order. */
	readonly removedTrees: string[] = [];

	realPath(path: string): string | null {
		const target = this.symlinks.get(path);
		if (target !== undefined) return target;
		const inside = (candidate: string) => candidate === path || candidate.startsWith(`${path}/`);
		const exists = [...this.files.keys()].some(inside) || [...this.directories.keys()].some(inside);
		return exists ? path : null;
	}

	removeTree(path: string): void {
		this.removedTrees.push(path);
		this.events.push(`rmtree:${path}`);
		const inside = (candidate: string) => candidate === path || candidate.startsWith(`${path}/`);
		for (const key of [...this.files.keys()]) if (inside(key)) this.files.delete(key);
		for (const key of [...this.directories.keys()]) if (inside(key)) this.directories.delete(key);
	}

	stat(path: string): ContainerHostStat | null {
		const file = this.files.get(path);
		if (file) return { isFile: true, isDirectory: false, ...file };
		const directory = this.directories.get(path);
		if (directory) return { isFile: false, isDirectory: true, ...directory };
		return null;
	}

	ensureDirectory(path: string, options: { mode: number; uid: number; gid: number }): void {
		if (!this.directories.has(path)) this.events.push(`mkdir:${path}`);
		this.directories.set(path, { ...options });
	}

	ensureIdentity(kind: string): { uid: number; gid: number } {
		this.identities.push(kind);
		this.events.push(`identity:${kind}`);
		return { uid: 10101, gid: 10101 };
	}

	now(): Date {
		return new Date("2026-09-23T12:00:00.000Z");
	}

	sources(paths: ContainerCompanionPaths = HOSTED_PATHS): Map<string, string> {
		const out = new Map<string, string>();
		for (const [path, file] of this.files) {
			if (path.startsWith(`${paths.quadletRoot}/`)) out.set(path, file.content);
		}
		return out;
	}
}

function planFor(
	host: FakeContainerHost,
	entry: PostizCompanionManifest = fixtureEntry(),
	secrets: Record<string, string> = fixtureSecrets(entry),
) {
	return planContainerCompanion(entry, secrets, readContainerCompanionFacts(host, HOSTED_PATHS));
}

// ---------------------------------------------------------------------------------------------
// Task 1 (tracer): a postiz entry becomes running containers over one injected host.
// ---------------------------------------------------------------------------------------------

describe("container companion tracer: a postiz entry becomes running containers", () => {
	it("plans, pulls by digest, encrypts, renders and starts over one injected host", () => {
		const entry = fixtureEntry();
		const secrets = fixtureSecrets(entry);
		const host = new FakeContainerHost();
		const plan = planFor(host, entry, secrets);
		expect(plan.kind).toBe("install");

		const result = applyContainerCompanion(plan, host, HOSTED_PATHS);
		expect(result.kind).toBe("install");

		// Pulls are digest references only, and exactly the entry's three images.
		const pulls = host.commands
			.filter((call) => call.command === "podman" && call.args[0] === "pull")
			.map((call) => call.args.slice(1));
		for (const args of pulls) {
			expect(args).toHaveLength(1);
			expect(args[0]).toMatch(/^[a-z0-9./-]+@sha256:[a-f0-9]{64}$/);
		}
		expect(pulls.map((args) => args[0]).sort()).toEqual(fixtureReferences(entry).sort());

		// Four sources: the network, the database, Redis (CK-A) and the app.
		const sources = host.sources();
		expect([...sources.keys()].sort()).toEqual(
			[
				"/etc/containers/systemd/iforge-postiz-db.container",
				"/etc/containers/systemd/iforge-postiz-redis.container",
				"/etc/containers/systemd/iforge-postiz.container",
				"/etc/containers/systemd/iforge-postiz.network",
			].sort(),
		);
		for (const [path, content] of sources) {
			if (path.endsWith(".container")) {
				expect(content).toContain("Pull=never");
				expect(content).toMatch(/^Image=[a-z0-9./-]+@sha256:[a-f0-9]{64}$/m);
			} else {
				expect(content).not.toContain("Image=");
			}
			expect(host.files.get(path)?.mode).toBe(0o644);
			expect(host.files.get(path)?.uid).toBe(0);
		}

		// The ONLY publish anywhere is the app's loopback publish.
		const publishLines = [...sources.values()].flatMap((content) =>
			content.split("\n").filter((line) => line.startsWith("PublishPort")),
		);
		expect(publishLines).toEqual(["PublishPort=127.0.0.1:9130:5000"]);
		expect(sources.get("/etc/containers/systemd/iforge-postiz.container")).toContain(
			"PublishPort=127.0.0.1:9130:5000",
		);

		// Both credentials produced by `systemd-creds encrypt --name=<id>` and written 0600.
		const encrypts = host.commands.filter(
			(call) => call.command === "systemd-creds" && call.args[0] === "encrypt",
		);
		expect(encrypts.map((call) => call.args.find((arg) => arg.startsWith("--name=")))).toEqual([
			"--name=postiz.env",
			"--name=postiz-db.env",
		]);
		for (const id of ["postiz.env", "postiz-db.env"]) {
			const cred = host.files.get(`/var/lib/iforge/companions/postiz/credentials/${id}.cred`);
			expect(cred?.mode).toBe(0o600);
			expect(cred?.uid).toBe(0);
			expect(cred?.content).toContain(`CIPHERTEXT[${id}]`);
		}
		// Plaintext existed only under the run root and is gone.
		for (const call of encrypts) {
			const input = call.args[call.args.length - 2] ?? "";
			expect(input.startsWith("/run/iforge/")).toBe(true);
			expect(host.files.has(input)).toBe(false);
		}

		// No secret value from the fixture's secret map reaches any rendered source.
		for (const content of sources.values()) {
			for (const token of SECRET_TOKENS) expect(content).not.toContain(token);
			for (const value of Object.values(secrets)) expect(content).not.toContain(value.trim());
		}

		// The app service starts after the generator ran.
		const reload = host.events.indexOf("run:systemctl daemon-reload");
		const start = host.events.indexOf("run:systemctl start iforge-postiz.service");
		expect(reload).toBeGreaterThanOrEqual(0);
		expect(start).toBeGreaterThan(reload);

		// The receipt is root-only.
		expect(host.files.get("/var/lib/iforge/companions/postiz/applied.json")?.mode).toBe(0o600);
	});

	it("derives the user namespace from the CK-B formula", () => {
		// base = 1_000_000_000 + (uid - 10100) * 65536 (06-03-SUMMARY.md, CK-B).
		expect(containerCompanionSubuidBase(10101)).toBe(1_000_000_000 + (10101 - 10100) * 65536);
		expect(containerCompanionSubuidBase(10101)).toBe(1_000_065_536);
		const host = new FakeContainerHost();
		const plan = planFor(host);
		for (const source of plan.sources.filter((s) => s.path.endsWith(".container"))) {
			expect(source.content).toContain("UIDMap=0:1000065536:65536");
			expect(source.content).toContain("GIDMap=0:1000065536:65536");
		}
	});

	it("renders the app with the unmodified image, the npm cache pin and encrypted credentials", () => {
		const host = new FakeContainerHost();
		const plan = planFor(host);
		const app = plan.sources.find((s) => s.path.endsWith("/iforge-postiz.container"))?.content;
		expect(app).toBeDefined();
		expect(app).toContain(
			"Image=ghcr.io/gitroomhq/postiz-app@sha256:01b24a4fc1055f6833122c99614805bea702fdc0ee30c9b5d09330b521fa1efa",
		);
		expect(app).toContain("Environment=npm_config_dlx_cache_max_age=5256000");
		expect(app).toContain("EnvironmentFile=%d/postiz.env");
		expect(app).toContain("NoNewPrivileges=true");
		expect(app).toContain("ReadOnly=false");
		expect(app).toContain(
			"LoadCredentialEncrypted=postiz.env:/var/lib/iforge/companions/postiz/credentials/postiz.env.cred",
		);
		expect(app).toContain("Network=iforge-postiz.network");
		// No build, no derivative image, no entrypoint override (D-03).
		for (const forbidden of ["Exec=", "Entrypoint=", "Build", "Mount=type=bind,source=/app"]) {
			expect(app).not.toContain(forbidden);
		}
		const db = plan.sources.find((s) => s.path.endsWith("/iforge-postiz-db.container"))?.content;
		expect(db).toContain("EnvironmentFile=%d/postiz-db.env");
		expect(db).toContain("Volume=/var/lib/iforge-postiz/db:/var/lib/postgresql");
		expect(db).not.toContain("PublishPort");
	});

	it("renders byte-stable sources across two plans", () => {
		const first = planFor(new FakeContainerHost());
		const second = planFor(new FakeContainerHost());
		expect(second.sources).toEqual(first.sources);
	});

	const dryRunAvailable =
		process.env.IFORGE_QUADLET_DRYRUN === "1" &&
		(() => {
			try {
				return readFileSync("/usr/libexec/podman/quadlet").length > 0;
			} catch {
				return false;
			}
		})();

	it.skipIf(!dryRunAvailable)(
		"renders sources the real Quadlet generator accepts (opt-in: IFORGE_QUADLET_DRYRUN=1)",
		async () => {
			const { mkdtempSync, rmSync, writeFileSync } = await import("node:fs");
			const { tmpdir } = await import("node:os");
			const { spawnSync } = await import("node:child_process");
			const dir = mkdtempSync(join(tmpdir(), "iforge-quadlet-"));
			try {
				const plan = planFor(new FakeContainerHost());
				for (const source of plan.sources) {
					writeFileSync(join(dir, source.path.split("/").pop() ?? ""), source.content);
				}
				const result = spawnSync("/usr/libexec/podman/quadlet", ["-dryrun"], {
					encoding: "utf8",
					env: { ...process.env, QUADLET_UNIT_DIRS: dir },
				});
				expect(result.status).toBe(0);
				const out = result.stdout;
				const appExec =
					out
						.split(/^---iforge-postiz\.service---$/m)[1]
						?.split("\n")
						.find((line) => line.startsWith("ExecStart=")) ?? "";
				expect(appExec).toContain("--pull never");
				expect(appExec).toContain("@sha256:");
				expect(appExec).toContain("--publish 127.0.0.1:9130:5000");
				expect(appExec).toContain("--env-file");
			} finally {
				rmSync(dir, { recursive: true, force: true });
			}
		},
	);
});

// ---------------------------------------------------------------------------------------------
// Plan 06-12: bootstrap, data-asserted readiness and the install report.
// ---------------------------------------------------------------------------------------------

const RUNTIME_TOKEN = "runtime-token-3a7c55e1";
const ADMIN_PASSWORD = "admin-password-5e2f8c19";
const ADMIN_EMAIL = "owner@example.test";
const ADMIN_USER_ID = "7c1d9a64-2b3e-4f5a-8c6d-0e1f2a3b4c5d";
/** What the fake Postiz hands back on a successful sign-in; it must never leave the runtime. */
const POSTIZ_SESSION_TOKEN = "postiz-session-9b4e2a71";
/** The password a prompt-injected agent chose when it registered first (plan 06-38, CR-02). */
const AGENT_CHOSEN_PASSWORD = "agent-chosen-password";
const FOREIGN_USER_ID = "f0000000-0000-4000-8000-00000000f00d";

/** The composed values plan 06-10 emits, with an admin JSON the bootstrap can read. */
function bootstrapSecrets(entry: PostizCompanionManifest): Record<string, string> {
	return {
		...fixtureSecrets(entry),
		[entry.credentials.adminSecretRef]: JSON.stringify({
			company: "Acme Agent",
			email: ADMIN_EMAIL,
			password: ADMIN_PASSWORD,
		}),
		"secret://clawdi/auth-token": RUNTIME_TOKEN,
	};
}

const RUNTIME_PATHS = {
	serviceStateRoot: "/var/lib/iforge",
	configurationRoot: "/etc/iforge",
	runRoot: "/run/iforge",
};

interface FakePostizOptions {
	/** Polls answered with a refused connection before the backend answers 401. */
	downPolls?: number;
	/** The backend never comes up: is-connected answers 502 forever. */
	neverReady?: boolean;
	/** What `/` would answer. It must never be asked. */
	rootStatus?: number;
	/** Organizations that exist before the runtime does anything. */
	organizations?: number;
	/** Force the register answer (default: 200 on an empty database, else 400). */
	registerStatus?: number;
	/** How many organizations one register call leaves behind (default 1). */
	organizationsAfterRegister?: number;
	/** Make the key-seed psql fail, echoing the script it was given (as a real psql error can). */
	psqlFailsEchoingScript?: boolean;
	/**
	 * A local process registered first (Postiz admits the first registrant even with
	 * DISABLE_REGISTRATION, research Pitfall 3): the only organization belongs to this user, not
	 * to the platform's admin credential (plan 06-38, CR-02).
	 */
	foreignRegistrant?: { email: string; password: string; id: string };
}

interface FakePostizUser {
	id: string;
	password: string;
}

interface FakeRequest {
	method: string;
	url: string;
	headers: Record<string, string>;
	body: string | null;
}

function unquotePsql(value: string): string {
	return value.replace(/''/g, "'").replace(/\\\\/g, "\\");
}

/**
 * Postiz v2.24.0 as research verified it, behind one fetch and one psql seam. The clock is
 * virtual: `sleep` advances it, so the 300-second budget costs nothing.
 */
class FakePostiz {
	readonly events: string[];
	readonly requests: FakeRequest[] = [];
	readonly scripts: string[] = [];
	organizations: number;
	apiKey: string | null = null;
	/** Postiz's `User` rows by email: the id Postiz generated and the password it accepts. */
	users = new Map<string, FakePostizUser>();
	clock = 0;
	private polls = 0;

	constructor(
		events: string[],
		private readonly options: FakePostizOptions = {},
	) {
		this.events = events;
		const foreign = options.foreignRegistrant;
		this.organizations = options.organizations ?? (foreign ? 1 : 0);
		if (foreign) {
			this.users.set(foreign.email, { id: foreign.id, password: foreign.password });
		} else if (this.organizations > 0) {
			// A preset organization is the platform's own: its admin credential registered it.
			this.users.set(ADMIN_EMAIL, { id: ADMIN_USER_ID, password: ADMIN_PASSWORD });
		}
	}

	loopback() {
		return {
			fetch: (input: string, init?: RequestInit) => this.fetch(input, init),
			sleep: async (ms: number) => {
				this.clock += ms;
			},
			now: () => this.clock,
		};
	}

	async fetch(input: string, init: RequestInit = {}): Promise<Response> {
		const url = new URL(input);
		const headers: Record<string, string> = {};
		new Headers(init.headers).forEach((value, key) => {
			headers[key] = value;
		});
		const method = (init.method ?? "GET").toUpperCase();
		const body = typeof init.body === "string" ? init.body : null;
		this.requests.push({ method, url: input, headers, body });
		const keyed = headers.authorization !== undefined;
		this.events.push(`fetch:${method} ${url.pathname}${keyed ? " +key" : ""}`);
		if (url.pathname === "/") return new Response(null, { status: this.options.rootStatus ?? 307 });
		if (url.pathname === "/api/public/v1/is-connected") {
			this.polls += 1;
			if (this.options.neverReady) return new Response("bad gateway", { status: 502 });
			if (this.polls <= (this.options.downPolls ?? 0)) {
				throw new TypeError("fetch failed: connect ECONNREFUSED 127.0.0.1:9130");
			}
			if (!keyed || this.apiKey === null || headers.authorization !== this.apiKey) {
				return new Response(JSON.stringify({ msg: "Invalid API key" }), { status: 401 });
			}
			return Response.json({ connected: true });
		}
		if (url.pathname === "/api/auth/register" && method === "POST") {
			const status = this.options.registerStatus ?? (this.organizations === 0 ? 200 : 400);
			if (status !== 200) return new Response("Registration is disabled", { status });
			const parsed = JSON.parse(body ?? "{}") as { email: string; password: string };
			this.organizations += this.options.organizationsAfterRegister ?? 1;
			this.users.set(parsed.email, { id: ADMIN_USER_ID, password: parsed.password });
			return Response.json({ register: true });
		}
		if (url.pathname === "/api/auth/login" && method === "POST") {
			// Postiz v2.24.0 `auth.controller.ts` `@Post('/login')`: 200 `{"login": true}` with an
			// `auth` cookie and header on success, 400 with a message on a wrong credential.
			const parsed = JSON.parse(body ?? "{}") as { email?: string; password?: string };
			const user = this.users.get(parsed.email ?? "");
			if (user === undefined || user.password !== parsed.password) {
				return new Response("Invalid user name or password", { status: 400 });
			}
			return Response.json(
				{ login: true },
				{
					status: 200,
					headers: {
						"set-cookie": `auth=${POSTIZ_SESSION_TOKEN}; Path=/; HttpOnly`,
						auth: POSTIZ_SESSION_TOKEN,
					},
				},
			);
		}
		return new Response("not found", { status: 404 });
	}

	/** Rows of `iforge_rehearsal.marker`, as the runtime inserted them (plan 06-15). */
	readonly markers: Array<{ id: number; token: string; writtenAt: string }> = [];
	markerClock = "2026-09-24T12:00:00.000000Z";

	psql = (script: string): ContainerHostCommandResult => {
		this.scripts.push(script);
		if (script.includes("iforge_rehearsal")) {
			this.events.push("psql:marker");
			const token = /^\\set token '([0-9a-f]*)'$/m.exec(script)?.[1] ?? "";
			const id = this.markers.length + 1;
			this.markers.push({ id, token, writtenAt: this.markerClock });
			return { status: 0, stdout: `${id}|${this.markerClock}\n`, stderr: "" };
		}
		if (this.options.psqlFailsEchoingScript && script.includes('UPDATE "Organization"')) {
			return { status: 3, stdout: "", stderr: `ERROR:  syntax error\nLINE 1: ${script}` };
		}
		const variable = (name: string): string | null => {
			const match = new RegExp(`^\\\\set ${name} '((?:[^'\\\\]|''|\\\\.)*)'$`, "m").exec(script);
			return match ? unquotePsql(match[1] ?? "") : null;
		};
		if (script.includes('UPDATE "Organization"')) {
			this.events.push("psql:key-seed");
			if (this.organizations !== 1) return { status: 0, stdout: "", stderr: "" };
			this.apiKey = variable("key");
			return { status: 0, stdout: "org_1\n", stderr: "" };
		}
		if (script.includes('FROM "User"')) {
			const wantedId = variable("id");
			if (wantedId !== null) {
				// The recorded-owner proof (plan 06-38): does a User row with exactly this id exist?
				this.events.push("psql:owner-id");
				const found = [...this.users.values()].some((user) => user.id === wantedId);
				return { status: 0, stdout: found ? `${wantedId}\n` : "", stderr: "" };
			}
			this.events.push("psql:admin-user");
			const id = this.users.get(variable("email") ?? "")?.id;
			return { status: 0, stdout: id ? `${id}\n` : "", stderr: "" };
		}
		if (script.includes('FROM "Organization"')) {
			this.events.push("psql:org-count");
			return { status: 0, stdout: `${this.organizations}\n`, stderr: "" };
		}
		return { status: 1, stdout: "", stderr: "unexpected script" };
	};
}

class RecordingReporter implements CompanionReportSink {
	readonly reports: CompanionInstallReport[] = [];

	constructor(private readonly events: string[]) {}

	async send(report: CompanionInstallReport): Promise<CompanionReportResult> {
		this.reports.push(structuredClone(report));
		this.events.push(`report:${report.phase}`);
		return { outcome: "recorded", status: 200 };
	}

	last(): CompanionInstallReport | undefined {
		return this.reports[this.reports.length - 1];
	}
}

/** Assert `expected` appears in `events` in this order (other events may interleave). */
function expectInOrder(events: readonly string[], expected: readonly string[]): void {
	let from = 0;
	for (const wanted of expected) {
		const at = events.indexOf(wanted, from);
		if (at < 0) {
			throw new Error(`expected "${wanted}" after index ${from} in:\n${events.join("\n")}`);
		}
		from = at + 1;
	}
}

interface BootstrapRun {
	entry: PostizCompanionManifest;
	secrets: Record<string, string>;
	host: FakeContainerHost;
	postiz: FakePostiz;
	reporter: RecordingReporter;
	events: string[];
}

function installedPostiz(
	options: FakePostizOptions = {},
	hostOptions: FakeHostOptions = {},
	entry: PostizCompanionManifest = fixtureEntry(),
): BootstrapRun {
	const secrets = bootstrapSecrets(entry);
	const events: string[] = [];
	const postiz = new FakePostiz(events, options);
	const host = new FakeContainerHost({ ...hostOptions, psql: postiz.psql });
	applyContainerCompanion(planFor(host, entry, secrets), host, HOSTED_PATHS);
	return { entry, secrets, host, postiz, reporter: new RecordingReporter(events), events };
}

function runReadiness(run: BootstrapRun) {
	return convergePostizReadiness({
		manifest: manifestWith({ postiz: run.entry }),
		secretValues: run.secrets,
		paths: RUNTIME_PATHS,
		host: run.host,
		loopback: run.postiz.loopback(),
		reporter: run.reporter,
	});
}

/** Every argv array the runtime handed to a command, flattened, with the command name. */
function everyArgv(host: FakeContainerHost): string[] {
	return host.commands.flatMap((call) => [call.command, ...call.args]);
}

describe("postiz bootstrap tracer: a fresh Postiz is bootstrapped, proven and reported ready", () => {
	it("waits on is-connected, registers over loopback, seeds the key via stdin, asserts on data, reports ready", async () => {
		const run = installedPostiz({ downPolls: 2 });
		const outcome = await runReadiness(run);

		expect(outcome).toEqual({ outcome: "ready", step: "ready" });
		// readiness -> register -> key seed -> admin id -> is-connected with the key -> org count -> report
		expectInOrder(run.events, [
			"fetch:GET /api/public/v1/is-connected",
			"fetch:POST /api/auth/register",
			"psql:key-seed",
			"psql:admin-user",
			"fetch:GET /api/public/v1/is-connected +key",
			"psql:org-count",
			"report:ready",
		]);
		const last = run.reporter.last();
		expect(last).toEqual({
			operation: "install",
			phase: "ready",
			step: "ready",
			assertion: { connected: true, organizations: 1 },
			applied_digest: run.entry.artifact.sha256,
			admin_user_id: ADMIN_USER_ID,
		});

		// Everything the runtime asked Postiz went to loopback, and `/` was never consulted.
		for (const request of run.postiz.requests) {
			expect(new URL(request.url).origin).toBe("http://127.0.0.1:9130");
			expect(new URL(request.url).pathname).not.toBe("/");
		}
		// The key is sent bare (no Bearer prefix), exactly as Postiz compares it.
		const keyed = run.postiz.requests.find((request) => request.headers.authorization);
		expect(keyed?.headers.authorization).toBe(API_KEY_TOKEN);

		// The register body carries the admin credential; the key reaches psql only on stdin.
		const register = run.postiz.requests.find((request) => request.url.endsWith("/register"));
		expect(JSON.parse(register?.body ?? "{}")).toEqual({
			email: ADMIN_EMAIL,
			password: ADMIN_PASSWORD,
			provider: "LOCAL",
			company: "Acme Agent",
		});
		const psqlCalls = run.host.commands.filter(
			(call) => call.command === "podman" && call.args[0] === "exec",
		);
		expect(psqlCalls.length).toBeGreaterThanOrEqual(3);
		for (const call of psqlCalls) {
			expect(call.args.slice(0, 3)).toEqual(["exec", "-i", "iforge-postiz-db"]);
			expect(call.args).toContain("ON_ERROR_STOP=1");
			expect(typeof call.input).toBe("string");
		}
		expect(psqlCalls.some((call) => call.input?.includes(API_KEY_TOKEN))).toBe(true);
		expect(run.postiz.scripts.some((script) => script.includes(":'key'"))).toBe(true);

		// No secret ever reaches an argv array or a URL.
		const argv = everyArgv(run.host);
		for (const secret of [API_KEY_TOKEN, ADMIN_PASSWORD, DB_PASSWORD_TOKEN, APP_SECRET_TOKEN]) {
			for (const arg of argv) expect(arg).not.toContain(secret);
			for (const request of run.postiz.requests) expect(request.url).not.toContain(secret);
		}
	});

	it("collects every composed value and every env-file value as a secret to redact", () => {
		const entry = fixtureEntry();
		const secrets = bootstrapSecrets(entry);
		const values = installationSecretValues(Object.values(entry.credentials), secrets);
		for (const token of [API_KEY_TOKEN, ADMIN_PASSWORD, DB_PASSWORD_TOKEN, APP_SECRET_TOKEN]) {
			expect(values).toContain(token);
		}
		for (const ref of Object.values(entry.credentials)) {
			expect(values).toContain(secrets[ref]);
		}
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-12 Task 2: idempotent re-convergence, every failure path, and redaction.
// ---------------------------------------------------------------------------------------------

function reportsOf(run: BootstrapRun, phase: CompanionInstallReport["phase"]) {
	return run.reporter.reports.filter((report) => report.phase === phase);
}

function registerCalls(run: BootstrapRun): FakeRequest[] {
	return run.postiz.requests.filter((request) => request.url.endsWith("/api/auth/register"));
}

function keySeedScripts(run: BootstrapRun): string[] {
	return run.postiz.scripts.filter((script) => script.includes('UPDATE "Organization"'));
}

function loginCalls(run: BootstrapRun): FakeRequest[] {
	return run.postiz.requests.filter((request) => request.url.endsWith("/api/auth/login"));
}

describe("postiz bootstrap: re-convergence and failure paths", () => {
	it("skips registration over an already-bootstrapped database, re-seeds the key and reports ready", async () => {
		const run = installedPostiz({ organizations: 1 });
		const outcome = await runReadiness(run);

		expect(registerCalls(run)).toEqual([]);
		expect(keySeedScripts(run)).toHaveLength(1);
		expect(outcome).toEqual({ outcome: "ready", step: "ready" });
		expect(run.reporter.last()).toMatchObject({
			phase: "ready",
			assertion: { connected: true, organizations: 1 },
			admin_user_id: ADMIN_USER_ID,
		});
		// Adoption is proven first (plan 06-38, CR-02): exactly one loopback sign-in with the
		// platform's credential, before the key is seeded.
		const logins = loginCalls(run);
		expect(logins).toHaveLength(1);
		expect(new URL(logins[0]?.url ?? "").origin).toBe("http://127.0.0.1:9130");
		expect(JSON.parse(logins[0]?.body ?? "{}")).toEqual({
			email: ADMIN_EMAIL,
			password: ADMIN_PASSWORD,
			provider: "LOCAL",
		});
		expectInOrder(run.events, ["fetch:POST /api/auth/login", "psql:key-seed"]);
		// Neither the sign-in body nor the session Postiz handed back reaches any report.
		const wire = JSON.stringify(run.reporter.reports);
		expect(wire).not.toContain(logins[0]?.body ?? "<no login body>");
		expect(wire).not.toContain(ADMIN_PASSWORD);
		expect(wire).not.toContain(POSTIZ_SESSION_TOKEN);
	});

	for (const [label, email] of [
		["holding the owner's email", ADMIN_EMAIL],
		["under another email", "agent@example.test"],
	] as const) {
		it(`a foreign registrant ${label} fails the install and never receives the key`, async () => {
			const run = installedPostiz({
				foreignRegistrant: { email, password: AGENT_CHOSEN_PASSWORD, id: FOREIGN_USER_ID },
			});
			const outcome = await runReadiness(run);

			expect(outcome).toEqual({ outcome: "failed", step: "data-assertion" });
			expect(registerCalls(run)).toEqual([]);
			expect(keySeedScripts(run)).toEqual([]);
			expect(run.postiz.apiKey).toBeNull();
			expect(reportsOf(run, "ready")).toEqual([]);
			const failed = reportsOf(run, "failed");
			expect(failed).toHaveLength(1);
			expect(failed[0]).toMatchObject({ operation: "install", step: "data-assertion" });
			const wire = JSON.stringify(run.reporter.reports);
			expect(wire).not.toContain(ADMIN_PASSWORD);
			expect(wire).not.toContain(AGENT_CHOSEN_PASSWORD);
			expect(wire).not.toContain(FOREIGN_USER_ID);
		});
	}

	// Once an installation reached ready, the control plane recorded the admin user id the
	// platform's registration produced and projects it as `adminUserId` (plan 06-38). CK-D lets
	// the tenant replace the generated password, so from then on the id is the only proof.
	function recordedOwner(adminUserId: string): PostizCompanionManifest {
		return { ...fixtureEntry(), adminUserId };
	}

	it("a tenant who set their own password still re-bootstraps by the recorded owner id", async () => {
		const run = installedPostiz({ organizations: 1 }, {}, recordedOwner(ADMIN_USER_ID));
		// The CK-D set-password link replaced the platform-generated password.
		run.postiz.users.set(ADMIN_EMAIL, { id: ADMIN_USER_ID, password: "tenant-chosen-password" });
		const outcome = await runReadiness(run);

		expect(outcome).toEqual({ outcome: "ready", step: "ready" });
		expect(loginCalls(run)).toEqual([]);
		expect(registerCalls(run)).toEqual([]);
		expect(keySeedScripts(run)).toHaveLength(1);
		expectInOrder(run.events, ["psql:owner-id", "psql:key-seed", "report:ready"]);
		expect(run.reporter.last()).toMatchObject({ phase: "ready", admin_user_id: ADMIN_USER_ID });
	});

	it("a recorded owner id missing from the database fails the install", async () => {
		const run = installedPostiz(
			{ organizations: 1 },
			{},
			recordedOwner("0d0d0d0d-0000-4000-8000-0000000000aa"),
		);
		const outcome = await runReadiness(run);

		expect(outcome).toEqual({ outcome: "failed", step: "data-assertion" });
		expect(loginCalls(run)).toEqual([]);
		expect(keySeedScripts(run)).toEqual([]);
		expect(run.postiz.apiKey).toBeNull();
		expect(reportsOf(run, "ready")).toEqual([]);
	});

	it("registers a fresh database even when an earlier owner id is recorded, and reports the new id", async () => {
		const run = installedPostiz({}, {}, recordedOwner("0d0d0d0d-0000-4000-8000-0000000000aa"));
		const outcome = await runReadiness(run);

		expect(outcome).toEqual({ outcome: "ready", step: "ready" });
		expect(registerCalls(run)).toHaveLength(1);
		expect(loginCalls(run)).toEqual([]);
		expect(run.reporter.last()).toMatchObject({ phase: "ready", admin_user_id: ADMIN_USER_ID });
	});

	it("reports failed at step readiness when is-connected never answers 401 within 300 s", async () => {
		const run = installedPostiz({ neverReady: true });
		const outcome = await runReadiness(run);

		expect(outcome).toEqual({ outcome: "failed", step: "readiness" });
		const failed = reportsOf(run, "failed");
		expect(failed).toHaveLength(1);
		expect(failed[0]).toMatchObject({ operation: "install", step: "readiness" });
		// The whole budget was spent polling every 5 s, and not a second more.
		expect(run.postiz.clock).toBeGreaterThanOrEqual(300_000);
		expect(run.postiz.clock).toBeLessThanOrEqual(305_000);
		const polls = run.postiz.requests.filter((request) =>
			request.url.endsWith("/api/public/v1/is-connected"),
		);
		expect(polls.length).toBeGreaterThanOrEqual(60);
		expect(polls.length).toBeLessThanOrEqual(62);
		expect(registerCalls(run)).toEqual([]);
		expect(reportsOf(run, "ready")).toEqual([]);
	});

	it("reports failed at step bootstrap when register answers non-200 on a fresh database, and never seeds the key", async () => {
		const run = installedPostiz({ registerStatus: 500 });
		const outcome = await runReadiness(run);

		expect(outcome).toEqual({ outcome: "failed", step: "bootstrap" });
		expect(reportsOf(run, "failed")[0]).toMatchObject({ step: "bootstrap" });
		expect(keySeedScripts(run)).toEqual([]);
		expect(run.postiz.apiKey).toBeNull();
		expect(reportsOf(run, "ready")).toEqual([]);
	});

	it("reports failed at step data-assertion when two organizations exist after bootstrap", async () => {
		const run = installedPostiz({ organizationsAfterRegister: 2 });
		const outcome = await runReadiness(run);

		expect(outcome).toEqual({ outcome: "failed", step: "data-assertion" });
		expect(reportsOf(run, "failed")[0]).toMatchObject({ step: "data-assertion" });
		// The guarded UPDATE seeded nothing into an organization the platform may not own.
		expect(run.postiz.apiKey).toBeNull();
		expect(reportsOf(run, "ready")).toEqual([]);
	});

	it("never reports ready off `/` answering 200 or 307 while is-connected fails", async () => {
		for (const rootStatus of [200, 307]) {
			const run = installedPostiz({ neverReady: true, rootStatus });
			const outcome = await runReadiness(run);

			expect(outcome.outcome).toBe("failed");
			expect(reportsOf(run, "ready")).toEqual([]);
			for (const request of run.postiz.requests) {
				expect(new URL(request.url).pathname).not.toBe("/");
			}
		}
	});

	it("redacts every known secret from reported lines and errors, including a psql error echoing the key", async () => {
		const run = installedPostiz(
			{ psqlFailsEchoingScript: true },
			{
				journal: [
					`prisma: connecting to postgresql://postiz:${DB_PASSWORD_TOKEN}@iforge-postiz-db:5432/postiz`,
					`nest: JWT_SECRET=${APP_SECRET_TOKEN} loaded`,
					`nest: login ${ADMIN_PASSWORD} rejected`,
					"nest: listening on 3000",
					"",
				].join("\n"),
			},
		);
		const outcome = await runReadiness(run);

		expect(outcome.outcome).toBe("failed");
		const failed = reportsOf(run, "failed")[0];
		expect(failed?.phase).toBe("failed");
		if (failed?.phase !== "failed") return;
		// The failure carries the unit's journal tail, so the tenant can see why.
		expect(failed.lines?.map((line) => line.line)).toContain("nest: listening on 3000");
		const wire = JSON.stringify(run.reporter.reports);
		for (const secret of [
			API_KEY_TOKEN,
			ADMIN_PASSWORD,
			DB_PASSWORD_TOKEN,
			APP_SECRET_TOKEN,
			RUNTIME_TOKEN,
		]) {
			expect(wire).not.toContain(secret);
		}
		expect(failed.error).toContain("[redacted]");
		expect(failed.lines?.some((line) => line.line.includes("[redacted]"))).toBe(true);
	});

	it("refuses to register or seed over a database that already holds two organizations", async () => {
		const run = installedPostiz({ organizations: 2 });
		const outcome = await runReadiness(run);

		expect(outcome).toEqual({ outcome: "failed", step: "data-assertion" });
		expect(registerCalls(run)).toEqual([]);
		expect(keySeedScripts(run)).toEqual([]);
	});

	it("reports once per applied receipt: a second converge over the same receipt does nothing", async () => {
		const run = installedPostiz();
		expect(await runReadiness(run)).toEqual({ outcome: "ready", step: "ready" });
		const requestsBefore = run.postiz.requests.length;
		const reportsBefore = run.reporter.reports.length;

		expect((await runReadiness(run)).outcome).toBe("skipped");
		expect(run.postiz.requests.length).toBe(requestsBefore);
		expect(run.reporter.reports.length).toBe(reportsBefore);
		// A fresh apply (a credential rotation re-renders the units) bootstraps and reports again,
		// idempotently: no second registration.
		const rotated = bootstrapSecrets(run.entry);
		rotated[run.entry.credentials.appEnvSecretRef] = `${
			rotated[run.entry.credentials.appEnvSecretRef]
		}API_LIMIT=91\n`;
		applyContainerCompanion(planFor(run.host, run.entry, rotated), run.host, HOSTED_PATHS);
		const again = await convergePostizReadiness({
			manifest: manifestWith({ postiz: run.entry }),
			secretValues: rotated,
			paths: RUNTIME_PATHS,
			host: run.host,
			loopback: run.postiz.loopback(),
			reporter: run.reporter,
		});
		expect(again).toEqual({ outcome: "ready", step: "ready" });
		expect(registerCalls(run)).toHaveLength(1);
	});

	it("retries on the next converge when the terminal report could not be delivered", async () => {
		const run = installedPostiz();
		const down: CompanionReportSink = {
			send: async () => {
				throw new Error("control plane unreachable");
			},
		};
		const first = await convergePostizReadiness({
			manifest: manifestWith({ postiz: run.entry }),
			secretValues: run.secrets,
			paths: RUNTIME_PATHS,
			host: run.host,
			loopback: run.postiz.loopback(),
			reporter: down,
		});
		expect(first.outcome).toBe("unreportable");
		expect(await runReadiness(run)).toEqual({ outcome: "ready", step: "ready" });
		expect(registerCalls(run)).toHaveLength(1);
	});

	it("treats an already-terminal (409) ready answer as reported", async () => {
		const run = installedPostiz({ organizations: 1 });
		const conflict: CompanionReportSink = {
			send: async () => ({ outcome: "already-terminal", status: 409 }),
		};
		const outcome = await convergePostizReadiness({
			manifest: manifestWith({ postiz: run.entry }),
			secretValues: run.secrets,
			paths: RUNTIME_PATHS,
			host: run.host,
			loopback: run.postiz.loopback(),
			reporter: conflict,
		});
		expect(outcome).toEqual({ outcome: "ready", step: "ready" });
		expect((await runReadiness(run)).outcome).toBe("skipped");
	});
});

// ---------------------------------------------------------------------------------------------
// Task 2: every host or entry the runtime cannot run safely is refused by name.
// ---------------------------------------------------------------------------------------------

function writesOf(host: FakeContainerHost): string[] {
	return host.events.filter((event) => event.startsWith("write:"));
}

function applyFresh(host: FakeContainerHost, entry: PostizCompanionManifest = fixtureEntry()) {
	return applyContainerCompanion(planFor(host, entry), host, HOSTED_PATHS);
}

describe("container companion refusals", () => {
	it("refuses a host without podman's Quadlet generator and writes nothing", () => {
		const host = new FakeContainerHost({ quadlet: false });
		expect(() => applyFresh(host)).toThrow(
			"container companions require podman's Quadlet generator",
		);
		expect(writesOf(host)).toEqual([]);
		expect(host.identities).toEqual([]);
		expect(host.sources().size).toBe(0);
	});

	it("refuses systemd 249, naming the version and the 250 floor", () => {
		const host = new FakeContainerHost({ systemdBanner: "systemd 249 (249.11-0ubuntu3)\n" });
		let message = "";
		try {
			applyFresh(host);
		} catch (error) {
			message = error instanceof Error ? error.message : String(error);
		}
		expect(message).toContain("249");
		expect(message).toContain("250");
		expect(writesOf(host)).toEqual([]);
		expect(host.sources().size).toBe(0);
	});

	it("refuses a host with no usable credential key and never renders a plain LoadCredential= fallback", () => {
		for (const credentialSecret of [null, { mode: 0o644, uid: 0 }, { mode: 0o600, uid: 1000 }]) {
			const host = new FakeContainerHost({ tpm2: false, credentialSecret });
			let message = "";
			try {
				applyFresh(host);
			} catch (error) {
				message = error instanceof Error ? error.message : String(error);
			}
			expect(message).toContain("TPM2");
			expect(message).toContain("/var/lib/systemd/credential.secret");
			expect(writesOf(host)).toEqual([]);
			expect(host.sources().size).toBe(0);
		}
		// No source this module can render ever carries the plain directive.
		for (const source of planFor(new FakeContainerHost()).sources) {
			for (const line of source.content.split("\n")) {
				expect(line.startsWith("LoadCredential=")).toBe(false);
			}
		}
		// A root-only host secret is a usable key without a TPM2 device.
		const hostSecret = new FakeContainerHost({
			tpm2: false,
			credentialSecret: { mode: 0o600, uid: 0 },
		});
		expect(applyFresh(hostSecret).kind).toBe("install");
	});

	it("refuses an image whose RepoDigests lack the exact reference and leaves no Quadlet source", () => {
		const entry = fixtureEntry();
		const redis = reference(entry.dependencies.redis.image, entry.dependencies.redis.sha256);
		const host = new FakeContainerHost({
			repoDigests: (ref) =>
				ref === redis ? [`${entry.dependencies.redis.image}@sha256:${"0".repeat(64)}`] : [ref],
		});
		expect(() => applyFresh(host, entry)).toThrow(entry.dependencies.redis.image);
		expect(host.sources().size).toBe(0);
		expect(host.events.some((event) => event.startsWith("write:/etc/containers/systemd/"))).toBe(
			false,
		);
		expect(host.files.has("/var/lib/iforge/companions/postiz/applied.json")).toBe(false);
		expect(
			host.commands.some((call) => call.command === "systemd-creds" && call.args[0] === "encrypt"),
		).toBe(false);
	});

	it("refuses an overlapping /etc/subuid range owned by another account, naming the account", () => {
		const host = new FakeContainerHost({
			subuid: "iforge:100000:65536\nintruder:1000060000:10000\n",
		});
		expect(() => applyFresh(host)).toThrow("intruder");
		expect(host.sources().size).toBe(0);
		expect(host.files.get("/etc/subuid")?.content).not.toContain("iforge-postiz");
		const subgid = new FakeContainerHost({ subgid: "squatter:1000100000:1\n" });
		expect(() => applyFresh(subgid)).toThrow("squatter");
		expect(subgid.sources().size).toBe(0);
		// A non-overlapping neighbour is fine, and the companion's own line is recorded once.
		const ok = new FakeContainerHost({ subuid: "neighbour:1000131072:65536\n" });
		applyFresh(ok);
		applyFresh(ok);
		const lines = ok.files.get("/etc/subuid")?.content.split("\n") ?? [];
		expect(lines.filter((line) => line === "iforge-postiz:1000065536:65536")).toHaveLength(1);
	});

	it("refuses the dedicated and shared tiers before any command", () => {
		for (const tier of ["dedicated", "shared"] as const) {
			const host = new FakeContainerHost();
			const entry = { ...fixtureEntry(), isolationTier: tier };
			expect(() => planFor(host, entry)).toThrow(tier);
			expect(host.commands).toEqual([]);
			expect(writesOf(host)).toEqual([]);
		}
	});

	it("applies nothing over a matching receipt: no pull, no write, no restart", () => {
		const host = new FakeContainerHost();
		applyFresh(host);
		const commandsBefore = host.commands.length;
		const eventsBefore = host.events.length;
		const plan = planFor(host);
		expect(plan.kind).toBe("unchanged");
		const result = applyContainerCompanion(plan, host, HOSTED_PATHS);
		expect(result.kind).toBe("unchanged");
		const later = host.commands.slice(commandsBefore);
		expect(later.some((call) => call.command === "podman" && call.args[0] === "pull")).toBe(false);
		expect(
			later.some(
				(call) =>
					call.command === "systemctl" &&
					["start", "restart", "stop", "daemon-reload", "reload-or-restart"].includes(
						call.args[0] ?? "",
					),
			),
		).toBe(false);
		const laterEvents = host.events.slice(eventsBefore);
		expect(laterEvents.filter((event) => /^(write|remove|mkdir):/.test(event))).toEqual([]);
	});

	it("plans a changed digest over a receipt as an update, and the converge's apply leaves the applied units alone for the backup-gated path", () => {
		const host = new FakeContainerHost();
		applyFresh(host);
		const commandsBefore = host.commands.length;
		const eventsBefore = host.events.length;
		const changed = fixtureEntry();
		changed.artifact = { ...changed.artifact, sha256: "f".repeat(64) };
		const plan = planFor(host, changed);
		expect(plan.kind).toBe("update");
		// Plan 06-18: the swap belongs to `applyPostizUpdate`, behind its backup gate.
		expect(applyContainerCompanion(plan, host, HOSTED_PATHS)).toEqual({
			kind: "update",
			pulled: [],
			written: [],
			started: [],
		});
		const later = host.commands.slice(commandsBefore);
		expect(later.some((call) => call.command === "podman" && call.args[0] === "pull")).toBe(false);
		expect(later.some((call) => call.command === "systemctl" && call.args[0] === "start")).toBe(
			false,
		);
		expect(host.events.slice(eventsBefore).filter((event) => event.startsWith("write:"))).toEqual(
			[],
		);
		// The applied digest is still the one on disk.
		expect(host.sources().get("/etc/containers/systemd/iforge-postiz.container")).toContain(
			fixtureEntry().artifact.sha256,
		);
	});

	it("removes every file it wrote when a later step fails (atomic from the host's view)", () => {
		const host = new FakeContainerHost({
			failCommand: (command, args) =>
				command === "systemctl" && args[0] === "start" && args[1] === "iforge-postiz.service",
		});
		expect(() => applyFresh(host)).toThrow("iforge-postiz.service");
		expect(host.sources().size).toBe(0);
		expect(host.files.has(GUARD_PATH)).toBe(false);
		for (const id of ["postiz.env", "postiz-db.env"]) {
			expect(host.files.has(`/var/lib/iforge/companions/postiz/credentials/${id}.cred`)).toBe(
				false,
			);
		}
		expect(host.files.has("/var/lib/iforge/companions/postiz/applied.json")).toBe(false);
		// Plaintext never survives either.
		expect([...host.files.keys()].some((path) => path.startsWith("/run/iforge/"))).toBe(false);
	});
});

// ---------------------------------------------------------------------------------------------
// Task 3: the converge runs, keeps and withdraws postiz, and never destroys its data.
// ---------------------------------------------------------------------------------------------

const convergeRoots: string[] = [];
afterEach(() => {
	for (const root of convergeRoots.splice(0)) rmSync(root, { recursive: true, force: true });
});

/**
 * Runtime paths whose platform roots are the hosted defaults, so the container kind addresses the
 * fake host's `/etc/containers/systemd` and `/var/lib/iforge…`, while every systemd directory the
 * ordinary unit writer scans points into an empty temporary directory on the real filesystem.
 */
function convergePaths(): RuntimePaths {
	const root = mkdtempSync(join(tmpdir(), "iforge-container-converge-"));
	convergeRoots.push(root);
	return {
		...getRuntimePaths({ mode: "hosted" }),
		serviceStateRoot: "/var/lib/iforge",
		configurationRoot: "/etc/iforge",
		runRoot: "/run/iforge",
		systemdSystemRoot: join(root, "system"),
		systemdUserRoot: join(root, "user"),
		systemdEnvRoot: join(root, "env"),
	};
}

function manifestWith(companions?: Record<string, unknown>): RuntimeManifest {
	return {
		schemaVersion: "clawdi.runtimeDesiredState.v1",
		deploymentId: "hdep_postiz_converge",
		environmentId: "env_postiz_converge",
		instanceId: "hri_postiz_converge",
		generation: 1,
		issuedAt: "2026-09-23T00:00:00.000Z",
		controlPlane: { apiUrl: "https://cloud-api.example.test" },
		runtimes: {},
		recovery: {},
		...(companions ? { companions } : {}),
	} as unknown as RuntimeManifest;
}

function converge(
	host: FakeContainerHost,
	manifest: RuntimeManifest,
	paths: RuntimePaths = convergePaths(),
	options: { daemonAuthTokenFile?: string | null; secretValues?: Record<string, string> } = {},
): { containerCompanionServices?: string[]; systemUnits?: string[] } {
	const entry = fixtureEntry();
	const input = {
		runtimePrograms: [],
		egressProgram: null,
		egressIdentity: null,
		// Required since upstream #1197 (tenant home ownership returns to the runtime user).
		runtimeIdentity: { uid: process.getuid?.() ?? 1_000, gid: process.getgid?.() ?? 1_000 },
		manifest,
		paths,
		workspaceRoot: paths.userHome,
		daemonAuthTokenFile: options.daemonAuthTokenFile ?? null,
		secretValues: options.secretValues ?? fixtureSecrets(entry),
		providerProjectionRevisions: {},
		runtimeRevision: () => "rev",
		commonEnvironment: {},
		containerHost: host,
	};
	return writeRuntimeSystemdState(input) as {
		containerCompanionServices?: string[];
		systemUnits?: string[];
	};
}

const GENERATED_SERVICES = [
	"iforge-postiz-network.service",
	"iforge-postiz-db.service",
	"iforge-postiz-redis.service",
	"iforge-postiz.service",
];

function seedData(host: FakeContainerHost): void {
	host.files.set("/var/lib/iforge-postiz/db/PG_VERSION", {
		content: "18\n",
		mode: 0o600,
		uid: 1_000_065_536 + 999,
		gid: 1_000_065_536 + 999,
	});
}

describe("the converge wires the container companion kind", () => {
	it("plans and applies a present postiz entry and counts its generated services as desired", () => {
		const host = new FakeContainerHost();
		const result = converge(host, manifestWith({ postiz: fixtureEntry() }));
		expect(result.containerCompanionServices).toEqual(GENERATED_SERVICES);
		expect(host.sources().size).toBe(4);
		expect(host.events).toContain("run:systemctl start iforge-postiz.service");
		// A second converge over the same manifest keeps it without touching anything.
		const eventsBefore = host.events.length;
		const again = converge(host, manifestWith({ postiz: fixtureEntry() }));
		expect(again.containerCompanionServices).toEqual(GENERATED_SERVICES);
		expect(
			host.events.slice(eventsBefore).filter((event) => /^(write|remove|mkdir):/.test(event)),
		).toEqual([]);
	});

	it("withdraws postiz when it disappears: units stopped, sources and credentials removed, data kept", () => {
		const host = new FakeContainerHost();
		converge(host, manifestWith({ postiz: fixtureEntry() }));
		seedData(host);
		const eventsBefore = host.events.length;
		const result = converge(host, manifestWith({}));
		expect(result.containerCompanionServices ?? []).toEqual([]);
		const later = host.events.slice(eventsBefore);
		expect(
			later.some(
				(event) =>
					event.startsWith("run:systemctl stop") && event.includes("iforge-postiz.service"),
			),
		).toBe(true);
		expect(host.sources().size).toBe(0);
		for (const id of ["postiz.env", "postiz-db.env"]) {
			expect(host.files.has(`/var/lib/iforge/companions/postiz/credentials/${id}.cred`)).toBe(
				false,
			);
		}
		const lastRemove = later
			.map((event, index) => (event.startsWith("remove:") ? index : -1))
			.reduce((a, b) => Math.max(a, b), -1);
		const reload = later.lastIndexOf("run:systemctl daemon-reload");
		expect(reload).toBeGreaterThan(lastRemove);
		// Data untouched, and the receipt keeps a data-retention marker.
		expect(host.files.get("/var/lib/iforge-postiz/db/PG_VERSION")?.content).toBe("18\n");
		expect(later.some((event) => event.startsWith("remove:/var/lib/iforge-postiz"))).toBe(false);
		expect(host.directories.has("/var/lib/iforge-postiz/db")).toBe(true);
		const receipt = JSON.parse(
			host.files.get("/var/lib/iforge/companions/postiz/applied.json")?.content ?? "{}",
		);
		expect(receipt.state).toBe("withdrawn");
		expect(receipt.dataRetained).toBe(true);
		expect(receipt.dataRoot).toBe("/var/lib/iforge-postiz");
		// Withdrawing again is a no-op.
		const commandsBefore = host.commands.length;
		converge(host, manifestWith({}));
		expect(host.commands.slice(commandsBefore)).toEqual([]);
	});

	it("still withdraws postiz when the manifest carries no companions at all (Pitfall 12)", () => {
		const host = new FakeContainerHost();
		converge(host, manifestWith({ postiz: fixtureEntry() }));
		// Precondition: there is something to withdraw.
		expect(host.sources().size).toBe(4);
		seedData(host);
		converge(host, manifestWith(undefined));
		expect(host.sources().size).toBe(0);
		expect(host.events).toContain(
			"run:systemctl stop iforge-postiz.service iforge-postiz-redis.service iforge-postiz-db.service iforge-postiz-network.service",
		);
		expect(host.files.has("/var/lib/iforge-postiz/db/PG_VERSION")).toBe(true);
	});

	it("exports a withdrawal that never touches the data directory", () => {
		const withdraw = (containerCompanionModule as Record<string, unknown>)
			.withdrawContainerCompanion;
		expect(typeof withdraw).toBe("function");
		if (typeof withdraw !== "function") return;
		const host = new FakeContainerHost();
		applyFresh(host);
		seedData(host);
		withdraw("postiz", host, HOSTED_PATHS);
		expect(host.sources().size).toBe(0);
		expect(host.files.has("/var/lib/iforge-postiz/db/PG_VERSION")).toBe(true);
	});

	it("manifestSecretRefs includes every postiz credential ref present in the entry", () => {
		const entry = fixtureEntry();
		const refs = manifestSecretRefs(manifestWith({ postiz: entry }));
		for (const ref of Object.values(entry.credentials)) expect(refs).toContain(ref);
	});

	it("makes postiz reconcilable alongside filebrowser", () => {
		expect(reconcilableCompanionKinds(["filebrowser", "postiz"])).toEqual([
			"filebrowser",
			"postiz",
		]);
		expect(reconcilableCompanionKinds(["postiz"])).toEqual(["postiz"]);
	});

	it("documents the container kind in docs/managed-runtime.md", () => {
		const doc = readFileSync(join(REPO_ROOT, "docs", "managed-runtime.md"), "utf8");
		const section = doc.split("### Container Companions")[1]?.split(/\n### /)[0] ?? "";
		expect(section.length).toBeGreaterThan(0);
		for (const phrase of [
			"Pull=never",
			"LoadCredentialEncrypted=",
			"127.0.0.1",
			"user namespace",
			"withdrawal",
			"nftables",
		]) {
			expect(section).toContain(phrase);
		}
		// Images are referred to generically in docs; no upstream organisation name.
		expect(section).not.toContain("gitroomhq");
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-20: the runtime destroys a companion's data only on an explicit `remove` instruction.
// ---------------------------------------------------------------------------------------------

const DATA_ROOT = "/var/lib/iforge-postiz";
const RECEIPT_PATH = "/var/lib/iforge/companions/postiz/applied.json";

type RemoveFn = (kind: "postiz", host: ContainerHost, paths: ContainerCompanionPaths) => unknown;
type RemovedReportFn = (input: {
	manifest: RuntimeManifest;
	secretValues: Record<string, string> | undefined;
	paths: Pick<RuntimePaths, "serviceStateRoot" | "configurationRoot" | "runRoot">;
	host?: ContainerHost;
	reporter?: CompanionReportSink;
}) => Promise<{ outcome: string }>;

/** Looked up by name, so a missing export fails the test that needs it, not the whole file. */
function removalApi(): { remove: RemoveFn; reportRemoved: RemovedReportFn } {
	const module = containerCompanionModule as Record<string, unknown>;
	expect(typeof module.removeContainerCompanion).toBe("function");
	expect(typeof module.reportPostizRemoved).toBe("function");
	return {
		remove: module.removeContainerCompanion as RemoveFn,
		reportRemoved: module.reportPostizRemoved as RemovedReportFn,
	};
}

function removeEntry(): PostizCompanionManifest {
	return { ...fixtureEntry(), lifecycle: "remove" };
}

function dataPaths(host: FakeContainerHost): string[] {
	return [...host.files.keys(), ...host.directories.keys()].filter(
		(path) => path === DATA_ROOT || path.startsWith(`${DATA_ROOT}/`),
	);
}

async function reportRemovedOver(
	host: FakeContainerHost,
	manifest: RuntimeManifest,
): Promise<{ outcome: { outcome: string }; reporter: RecordingReporter }> {
	const { reportRemoved } = removalApi();
	const reporter = new RecordingReporter([]);
	const outcome = await reportRemoved({
		manifest,
		secretValues: fixtureSecrets(fixtureEntry()),
		paths: {
			serviceStateRoot: "/var/lib/iforge",
			configurationRoot: "/etc/iforge",
			runRoot: "/run/iforge",
		},
		host,
		reporter,
	});
	return { outcome, reporter };
}

describe("the runtime removes a companion only on an explicit instruction (plan 06-20)", () => {
	it("lifecycle remove stops the units, removes sources, credentials, data and receipt, keeps the account, and reports removed", async () => {
		removalApi();
		const host = new FakeContainerHost();
		converge(host, manifestWith({ postiz: fixtureEntry() }));
		seedData(host);
		expect(dataPaths(host).length).toBeGreaterThan(0);
		const subuidBefore = host.files.get("/etc/subuid")?.content;
		const subgidBefore = host.files.get("/etc/subgid")?.content;
		expect(subuidBefore).toContain("iforge-postiz:");
		const eventsBefore = host.events.length;

		const result = converge(host, manifestWith({ postiz: removeEntry() }));

		expect(result.containerCompanionServices ?? []).toEqual([]);
		const later = host.events.slice(eventsBefore);
		// Stopped, sources and credentials removed and the generator reloaded -- all before the
		// data directory goes.
		const stop = later.findIndex(
			(event) => event.startsWith("run:systemctl stop") && event.includes("iforge-postiz.service"),
		);
		const reload = later.lastIndexOf("run:systemctl daemon-reload");
		const rmtree = later.indexOf(`rmtree:${DATA_ROOT}`);
		expect(stop).toBeGreaterThanOrEqual(0);
		expect(reload).toBeGreaterThan(stop);
		expect(rmtree).toBeGreaterThan(reload);
		expect(host.sources().size).toBe(0);
		expect(
			[...host.files.keys()].filter((path) =>
				path.startsWith("/var/lib/iforge/companions/postiz/credentials/"),
			),
		).toEqual([]);
		// The data directory is gone, recursively, and nothing else was deleted that way.
		expect(host.removedTrees).toEqual([DATA_ROOT]);
		expect(dataPaths(host)).toEqual([]);
		expect(host.files.has(RECEIPT_PATH)).toBe(false);
		// The account and its subordinate ranges are never reclaimed (D-13).
		expect(host.files.get("/etc/subuid")?.content).toBe(subuidBefore);
		expect(host.files.get("/etc/subgid")?.content).toBe(subgidBefore);
		expect(
			host.commands.some((entry) => ["userdel", "groupdel", "usermod"].includes(entry.command)),
		).toBe(false);

		const { outcome, reporter } = await reportRemovedOver(
			host,
			manifestWith({ postiz: removeEntry() }),
		);
		expect(outcome.outcome).toBe("reported");
		expect(reporter.reports).toEqual([{ operation: "uninstall", phase: "removed" }]);
	});

	it("remove when the data directory is already absent still reports removed", async () => {
		const { remove } = removalApi();
		const host = new FakeContainerHost();
		expect(dataPaths(host)).toEqual([]);

		remove("postiz", host, HOSTED_PATHS);
		// Twice: removing what is already gone is not an error.
		remove("postiz", host, HOSTED_PATHS);
		const result = converge(host, manifestWith({ postiz: removeEntry() }));
		expect(result.containerCompanionServices ?? []).toEqual([]);

		const { outcome, reporter } = await reportRemovedOver(
			host,
			manifestWith({ postiz: removeEntry() }),
		);
		expect(outcome.outcome).toBe("reported");
		expect(reporter.reports).toEqual([{ operation: "uninstall", phase: "removed" }]);
	});

	it("an absent entry, with no remove instruction, is withdrawal only: the data directory remains", async () => {
		removalApi();
		const host = new FakeContainerHost();
		converge(host, manifestWith({ postiz: fixtureEntry() }));
		seedData(host);

		converge(host, manifestWith(undefined));

		expect(host.sources().size).toBe(0);
		expect(host.removedTrees).toEqual([]);
		expect(host.files.get(`${DATA_ROOT}/db/PG_VERSION`)?.content).toBe("18\n");
		const receipt = JSON.parse(host.files.get(RECEIPT_PATH)?.content ?? "{}");
		expect(receipt.state).toBe("withdrawn");
		// Nothing was removed, so nothing is reported removed.
		const { outcome, reporter } = await reportRemovedOver(host, manifestWith(undefined));
		expect(outcome.outcome).toBe("skipped");
		expect(reporter.reports).toEqual([]);
	});

	it("refuses the recursive delete unless the data path resolves to exactly itself (symlink and traversal guard)", () => {
		const { remove } = removalApi();

		// A data directory that is a symlink to somewhere else is never followed.
		const linked = new FakeContainerHost();
		applyFresh(linked);
		seedData(linked);
		linked.symlinks.set(DATA_ROOT, "/srv/somewhere-else");
		expect(() => remove("postiz", linked, HOSTED_PATHS)).toThrow(/refus/i);
		expect(linked.removedTrees).toEqual([]);
		expect(linked.files.has(`${DATA_ROOT}/db/PG_VERSION`)).toBe(true);

		// A data path that is not exactly the kind's own directory under the platform root.
		for (const dataRoot of [
			"/var/lib/iforge/../iforge-postiz",
			"/var/lib/iforge-postiz/..",
			"/var/lib",
			"/",
			"/var/lib/iforge-postiz-other",
		]) {
			const host = new FakeContainerHost();
			applyFresh(host);
			seedData(host);
			host.directories.set(dataRoot, { mode: 0o755, uid: 0, gid: 0 });
			expect(() => remove("postiz", host, { ...HOSTED_PATHS, dataRoot })).toThrow(/refus/i);
			expect(host.removedTrees).toEqual([]);
			expect(host.files.has(`${DATA_ROOT}/db/PG_VERSION`)).toBe(true);
		}
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-14: the edge tunnel runs as a digest-pinned container with an encrypted token.
// ---------------------------------------------------------------------------------------------

// The catalog's cloudflared pin (plan 06-09), and a distinctive token so a leak is unambiguous.
const CLOUDFLARED_SHA256 = "b269e8abd07a5bf6f3f4be65d5050b2174eca89c56a0241a8ff32a16aec454e4";
const CLOUDFLARED_REFERENCE = `docker.io/cloudflare/cloudflared@sha256:${CLOUDFLARED_SHA256}`;
const TUNNEL_TOKEN = "tunnel-token-8e41b7c2d05f";
const TUNNEL_SOURCE = "/etc/containers/systemd/iforge-postiz-tunnel.container";
const TUNNEL_CREDENTIAL = "/var/lib/iforge/companions/postiz/credentials/postiz-tunnel.env.cred";

function tunnelRef(entry: PostizCompanionManifest): string {
	return `secret://companions/postiz/${entry.installationId}/tunnel-env`;
}

function tunnelEntry(): PostizCompanionManifest {
	const entry = fixtureEntry();
	return postizCompanionSchema.parse({
		...entry,
		dependencies: {
			...entry.dependencies,
			cloudflared: { image: "docker.io/cloudflare/cloudflared", sha256: CLOUDFLARED_SHA256 },
		},
		credentials: { ...entry.credentials, tunnelTokenSecretRef: tunnelRef(entry) },
	});
}

function tunnelSecrets(entry: PostizCompanionManifest): Record<string, string> {
	return { ...fixtureSecrets(entry), [tunnelRef(entry)]: `TUNNEL_TOKEN=${TUNNEL_TOKEN}\n` };
}

describe("postiz edge tunnel unit", () => {
	it("renders the tunnel as a digest-pinned container on the companion network with an encrypted token and no published port", () => {
		const entry = tunnelEntry();
		const plan = planFor(new FakeContainerHost(), entry, tunnelSecrets(entry));
		const tunnel = plan.sources.find((source) => source.path === TUNNEL_SOURCE)?.content ?? "";
		expect(tunnel).toContain(`Image=${CLOUDFLARED_REFERENCE}`);
		expect(tunnel).toContain("Pull=never");
		// Inside the installation's own network: it reaches Postiz by name, never through the host.
		expect(tunnel).toContain("Network=iforge-postiz.network");
		expect(tunnel).toContain("Exec=tunnel --no-autoupdate run");
		expect(tunnel).toContain("EnvironmentFile=%d/postiz-tunnel.env");
		expect(tunnel).toContain(`LoadCredentialEncrypted=postiz-tunnel.env:${TUNNEL_CREDENTIAL}`);
		expect(tunnel).toContain("UIDMap=0:1000065536:65536");
		expect(tunnel).toContain("GIDMap=0:1000065536:65536");
		expect(tunnel).toContain("NoNewPrivileges=true");
		// After the app, and back up if it falls over.
		expect(tunnel).toContain("Requires=iforge-postiz.service");
		expect(tunnel).toContain("After=iforge-postiz.service");
		expect(tunnel).toContain("Restart=on-failure");
		expect(tunnel).toContain("WantedBy=multi-user.target");
		// Outbound only: no port is published, and the token is in no rendered source.
		expect(tunnel.split("\n").filter((line) => line.startsWith("PublishPort"))).toEqual([]);
		for (const source of plan.sources) expect(source.content).not.toContain(TUNNEL_TOKEN);
		expect(plan.images.map((image) => image.reference)).toContain(CLOUDFLARED_REFERENCE);
		expect(plan.credentials.map((credential) => credential.id)).toContain("postiz-tunnel.env");
		expect(plan.services.all.at(-1)).toBe("iforge-postiz-tunnel.service");
	});

	it("renders no tunnel unit for an entry without the tunnel", () => {
		const plan = planFor(new FakeContainerHost());
		expect(plan.sources.map((source) => source.path)).not.toContain(TUNNEL_SOURCE);
		expect(plan.credentials.map((credential) => credential.id)).not.toContain("postiz-tunnel.env");
		expect(plan.images.map((image) => image.slot)).not.toContain("cloudflared");
		expect(plan.services.all).not.toContain("iforge-postiz-tunnel.service");
	});

	it("refuses an entry carrying only one half of the tunnel, naming it", () => {
		const entry = tunnelEntry();
		const withoutImage = postizCompanionSchema.parse({
			...entry,
			dependencies: { postgres: entry.dependencies.postgres, redis: entry.dependencies.redis },
		});
		expect(() =>
			planFor(new FakeContainerHost(), withoutImage, tunnelSecrets(withoutImage)),
		).toThrow(/tunnel/);
		const withoutToken = postizCompanionSchema.parse({
			...entry,
			credentials: { ...fixtureEntry().credentials },
		});
		expect(() => planFor(new FakeContainerHost(), withoutToken)).toThrow(/tunnel/);
	});

	it("refuses to plan the tunnel without its token, naming the reference only", () => {
		const entry = tunnelEntry();
		expect(() => planFor(new FakeContainerHost(), entry, fixtureSecrets(entry))).toThrow(
			tunnelRef(entry),
		);
	});

	it("applies the tunnel: pulls the digest, encrypts the token, starts the tunnel after the app", () => {
		const entry = tunnelEntry();
		const host = new FakeContainerHost();
		const result = applyContainerCompanion(
			planFor(host, entry, tunnelSecrets(entry)),
			host,
			HOSTED_PATHS,
		);
		expect(result.kind).toBe("install");
		expect(
			host.commands.some(
				(call) =>
					call.command === "podman" &&
					call.args[0] === "pull" &&
					call.args[1] === CLOUDFLARED_REFERENCE,
			),
		).toBe(true);
		const credential = host.files.get(TUNNEL_CREDENTIAL);
		expect(credential?.mode).toBe(0o600);
		expect(credential?.content).toContain("CIPHERTEXT[postiz-tunnel.env]");
		expect(credential?.content).not.toContain(TUNNEL_TOKEN);
		const app = host.events.indexOf("run:systemctl start iforge-postiz.service");
		const tunnel = host.events.indexOf("run:systemctl start iforge-postiz-tunnel.service");
		expect(app).toBeGreaterThanOrEqual(0);
		expect(tunnel).toBeGreaterThan(app);
		// The only published port on the host is still the app's loopback one.
		const publishLines = [...host.sources().values()].flatMap((content) =>
			content.split("\n").filter((line) => line.startsWith("PublishPort")),
		);
		expect(publishLines).toEqual(["PublishPort=127.0.0.1:9130:5000"]);
		for (const content of host.sources().values()) expect(content).not.toContain(TUNNEL_TOKEN);
		// Re-planning the same entry over the applied host changes nothing.
		expect(planFor(host, entry, tunnelSecrets(entry)).kind).toBe("unchanged");
	});

	it("withdraws the tunnel with the rest: stopped first, source and credential removed", () => {
		const entry = tunnelEntry();
		const host = new FakeContainerHost();
		applyContainerCompanion(planFor(host, entry, tunnelSecrets(entry)), host, HOSTED_PATHS);
		const result = withdrawContainerCompanion("postiz", host, HOSTED_PATHS);
		expect(result.withdrawn).toBe(true);
		expect(result.stopped[0]).toBe("iforge-postiz-tunnel.service");
		expect(host.files.has(TUNNEL_SOURCE)).toBe(false);
		expect(host.files.has(TUNNEL_CREDENTIAL)).toBe(false);
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-15: continuous archiving and a first encrypted backup, restore-on-bootstrap, and
// scheduled backups preceded by rehearsal markers.
// ---------------------------------------------------------------------------------------------

const BACKUP_STANZA = "postiz-abcdefghij12";
const BACKUP_REPO_PATH =
	"/0b6d2c1e-7f3a-4e59-8c14-9a2b3c4d5e6f/5f0c6d2e-8a41-4b7f-9e3c-2d1a7b6e4c90";
const BACKUP_KEY_SECRET = "K005-backup-key-secret-61d0e3a9";
const BACKUP_CIPHER_PASS = "cipher-pass-0f9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d9c8b7a6f5e4d3c";
const BACKUP_SOURCE = "/etc/containers/systemd/iforge-postiz-db.container";
const BACKUP_CREDENTIAL = "/var/lib/iforge/companions/postiz/credentials/postiz-backup.env.cred";

function backupRef(entry: PostizCompanionManifest): string {
	return `secret://companions/postiz/${entry.installationId}/backup-env`;
}

function backupEntry(): PostizCompanionManifest {
	const entry = fixtureEntry();
	return postizCompanionSchema.parse({
		...entry,
		backup: { stanza: BACKUP_STANZA, repoPath: BACKUP_REPO_PATH },
		credentials: { ...entry.credentials, backupEnvSecretRef: backupRef(entry) },
	});
}

function backupEnv(): string {
	return [
		"PGBACKREST_EXPIRE_AUTO=n",
		"PGBACKREST_PG1_PATH=/var/lib/postgresql/18/docker",
		`PGBACKREST_REPO1_CIPHER_PASS=${BACKUP_CIPHER_PASS}`,
		`PGBACKREST_REPO1_PATH=${BACKUP_REPO_PATH}`,
		"PGBACKREST_REPO1_S3_KEY=005backupkeyid0001",
		`PGBACKREST_REPO1_S3_KEY_SECRET=${BACKUP_KEY_SECRET}`,
		`PGBACKREST_STANZA=${BACKUP_STANZA}`,
		"",
	].join("\n");
}

function backupSecrets(entry: PostizCompanionManifest): Record<string, string> {
	return { ...bootstrapSecrets(entry), [backupRef(entry)]: backupEnv() };
}

interface FakeBackup {
	label: string;
	type: "full" | "diff";
	start: number;
	stop: number;
	size: number;
	/** The backup's pgBackRest annotations (plan 06-48, D-14), reported under `annotation`. */
	annotation?: Record<string, string>;
}

interface FakePgBackRestOptions {
	/** Whether the stanza already exists in the repository. */
	stanza?: boolean;
	/** Backups the repository already holds, oldest first. */
	backups?: FakeBackup[];
	/** Make `backup` fail, echoing the cipher pass as a careless error might. */
	failBackup?: boolean;
	/** Make `restore` fail. */
	failRestore?: boolean;
	/** The clock backups are stamped with (epoch seconds). */
	now?: () => number;
}

function backupLabel(epochSeconds: number): string {
	return new Date(epochSeconds * 1000)
		.toISOString()
		.replace(/[-:]/g, "")
		.replace("T", "-")
		.slice(0, 15);
}

/** pgBackRest against one repository, as its CLI answers (`info --output=json`). */
class FakePgBackRest {
	stanza: boolean;
	failBackup: boolean;
	/** Make `annotate` fail for the invocations it matches (plan 06-48). */
	failAnnotate: ((argv: readonly string[]) => boolean) | null = null;
	readonly backups: FakeBackup[];
	readonly calls: string[][] = [];

	constructor(
		private readonly events: string[],
		private readonly options: FakePgBackRestOptions = {},
	) {
		this.stanza = options.stanza ?? false;
		this.failBackup = options.failBackup ?? false;
		this.backups = [...(options.backups ?? [])];
	}

	handle = (argv: readonly string[]): ContainerHostCommandResult => {
		this.calls.push([...argv]);
		const sub = ["stanza-create", "backup", "info", "restore", "annotate"].find((name) =>
			argv.includes(name),
		);
		const type = argv.find((arg) => arg.startsWith("--type="));
		const annotations = Object.fromEntries(
			argv
				.filter((arg) => arg.startsWith("--annotation="))
				.map((arg) => {
					const pair = arg.slice("--annotation=".length);
					const at = pair.indexOf("=");
					return [pair.slice(0, at), pair.slice(at + 1)];
				}),
		);
		this.events.push(
			sub === "annotate"
				? `pgbackrest:annotate ${Object.entries(annotations)
						.map(([key, value]) => `${key}=${value}`)
						.join(" ")}`
				: `pgbackrest:${sub}${type ? ` ${type}` : ""}`,
		);
		const stanzaArg = argv.find((arg) => arg.startsWith("--stanza="))?.slice("--stanza=".length);
		if (stanzaArg !== BACKUP_STANZA) {
			return { status: 28, stdout: "", stderr: "ERROR: [028]: wrong stanza" };
		}
		const now = this.options.now?.() ?? Date.parse("2026-09-23T12:00:00.000Z") / 1000;
		if (sub === "stanza-create") {
			this.stanza = true;
			return { status: 0, stdout: "", stderr: "" };
		}
		if (sub === "backup") {
			if (!this.stanza) return { status: 55, stdout: "", stderr: "ERROR: [055]: no stanza" };
			if (this.failBackup) {
				return {
					status: 82,
					stdout: "",
					stderr: `ERROR: [082]: WAL segment was not archived (cipher ${BACKUP_CIPHER_PASS})`,
				};
			}
			const kind = type === "--type=diff" ? "diff" : "full";
			const lastFull = [...this.backups].reverse().find((backup) => backup.type === "full");
			const stamp = backupLabel(now);
			this.backups.push({
				label: kind === "full" ? `${stamp}F` : `${lastFull?.label}_${stamp}D`,
				type: kind,
				start: now,
				stop: now + 30,
				size: 48_000_000 + this.backups.length,
				...(Object.keys(annotations).length > 0 ? { annotation: annotations } : {}),
			});
			return { status: 0, stdout: "", stderr: "" };
		}
		if (sub === "annotate") {
			const set = argv.find((arg) => arg.startsWith("--set="))?.slice("--set=".length);
			const backup = this.backups.find((candidate) => candidate.label === set);
			if (backup === undefined) {
				return { status: 31, stdout: "", stderr: `ERROR: [031]: backup set ${set} is not valid` };
			}
			if (this.failAnnotate?.(argv)) {
				return { status: 49, stdout: "", stderr: "ERROR: [049]: unable to write backup.info" };
			}
			backup.annotation = { ...(backup.annotation ?? {}), ...annotations };
			return { status: 0, stdout: "", stderr: "" };
		}
		if (sub === "info") {
			const info = [
				{
					name: BACKUP_STANZA,
					status: this.stanza
						? this.backups.length > 0
							? { code: 0, message: "ok" }
							: { code: 2, message: "no valid backups" }
						: { code: 1, message: "missing stanza path" },
					backup: this.backups.map((backup) => ({
						...(backup.annotation !== undefined ? { annotation: { ...backup.annotation } } : {}),
						label: backup.label,
						type: backup.type,
						timestamp: { start: backup.start, stop: backup.stop },
						info: { size: backup.size, delta: backup.size },
						error: false,
					})),
				},
			];
			return { status: 0, stdout: `${JSON.stringify(info)}\n`, stderr: "" };
		}
		if (sub === "restore") {
			if (this.options.failRestore) {
				return { status: 40, stdout: "", stderr: "ERROR: [040]: restore failed" };
			}
			return { status: 0, stdout: "", stderr: "" };
		}
		return { status: 1, stdout: "", stderr: "unexpected pgbackrest command" };
	};
}

interface BackupRun extends BootstrapRun {
	pgbackrest: FakePgBackRest;
}

function installedBackupPostiz(
	options: { postiz?: FakePostizOptions; pgbackrest?: FakePgBackRestOptions } = {},
	seed?: (host: FakeContainerHost) => void,
): BackupRun {
	const entry = backupEntry();
	const secrets = backupSecrets(entry);
	const events: string[] = [];
	const postiz = new FakePostiz(events, options.postiz);
	const pgbackrest = new FakePgBackRest(events, options.pgbackrest);
	const host = new FakeContainerHost({ psql: postiz.psql, pgbackrest: pgbackrest.handle });
	seed?.(host);
	applyContainerCompanion(planFor(host, entry, secrets), host, HOSTED_PATHS);
	return {
		entry,
		secrets,
		host,
		postiz,
		pgbackrest,
		reporter: new RecordingReporter(events),
		events,
	};
}

function pgbackrestCalls(host: FakeContainerHost) {
	return host.commands.filter(
		(call) => call.command === "podman" && call.args.includes("pgbackrest"),
	);
}

describe("postiz backup tracer: an install ends with a first encrypted backup", () => {
	it("renders both encrypted env files and the archive settings, then creates the stanza, takes a full backup and reports it before the bootstrap", async () => {
		const entry = backupEntry();
		const plan = planFor(new FakeContainerHost(), entry, backupSecrets(entry));
		const database = plan.sources.find((source) => source.path === BACKUP_SOURCE)?.content ?? "";
		// Both credentials, each encrypted and delivered as its own env file.
		expect(database).toContain("EnvironmentFile=%d/postiz-db.env");
		expect(database).toContain("EnvironmentFile=%d/postiz-backup.env");
		expect(database).toContain(
			"LoadCredentialEncrypted=postiz-db.env:/var/lib/iforge/companions/postiz/credentials/postiz-db.env.cred",
		);
		expect(database).toContain(`LoadCredentialEncrypted=postiz-backup.env:${BACKUP_CREDENTIAL}`);
		// Continuous archiving: `%%p` is systemd's escape for the literal `%p` PostgreSQL expands.
		expect(database).toContain(
			`Exec=postgres -c archive_mode=on -c "archive_command=pgbackrest --stanza=${BACKUP_STANZA} archive-push %%p" -c archive_timeout=60`,
		);
		expect(plan.credentials.map((credential) => credential.id)).toContain("postiz-backup.env");
		for (const source of plan.sources) {
			expect(source.content).not.toContain(BACKUP_KEY_SECRET);
			expect(source.content).not.toContain(BACKUP_CIPHER_PASS);
		}

		const run = installedBackupPostiz();
		const outcome = await runReadiness(run);
		expect(outcome).toEqual({ outcome: "ready", step: "ready" });

		// After the database is up: stanza, first full backup, its report -- then the bootstrap.
		expectInOrder(run.events, [
			"pgbackrest:stanza-create",
			"pgbackrest:backup --type=full",
			"report:backup",
			"fetch:POST /api/auth/register",
			"report:ready",
		]);
		const backupReports = reportsOf(run, "backup");
		expect(backupReports).toHaveLength(1);
		expect(backupReports[0]).toEqual({
			operation: "install",
			phase: "backup",
			backup: {
				ok: true,
				label: "20260923-120000F",
				started_at: "2026-09-23T12:00:00.000Z",
				completed_at: "2026-09-23T12:00:00.000Z",
				db_bytes: 48_000_000,
				marker: null,
			},
		});

		// Every pgBackRest command against the running database ran inside its container as
		// postgres; no secret reached an argv array.
		for (const call of pgbackrestCalls(run.host).filter((call) => call.args[0] === "exec")) {
			expect(call.args.slice(0, 4)).toEqual(["exec", "--user", "postgres", "iforge-postiz-db"]);
			expect(call.args).toContain(`--stanza=${BACKUP_STANZA}`);
		}
		for (const arg of everyArgv(run.host)) {
			expect(arg).not.toContain(BACKUP_KEY_SECRET);
			expect(arg).not.toContain(BACKUP_CIPHER_PASS);
		}
		const credential = run.host.files.get(BACKUP_CREDENTIAL);
		expect(credential?.mode).toBe(0o600);
		expect(credential?.content).toContain("CIPHERTEXT[postiz-backup.env]");
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-15 Task 3: restore-on-bootstrap, and scheduled backups preceded by rehearsal markers.
// ---------------------------------------------------------------------------------------------

const PGDATA_VERSION_FILE = "/var/lib/iforge-postiz/db/18/docker/PG_VERSION";
const RESTORE_INCOMPLETE_MARKER = "/var/lib/iforge-postiz/db/.iforge-restore-incomplete";
const TRANSIENT_ROOT = "/run/iforge/companions/postiz/";
const HOUR_MS = 3_600_000;
const INSTALLED_AT = Date.parse("2026-09-23T12:00:00.000Z");

function existingFullBackup(): FakeBackup {
	const stop = INSTALLED_AT / 1000 - 3600;
	return { label: "20260923-110000F", type: "full", start: stop - 60, stop, size: 51_000_000 };
}

function transientFiles(host: FakeContainerHost): string[] {
	return [...host.files.keys()].filter((path) => path.startsWith(TRANSIENT_ROOT));
}

function eventIndex(host: FakeContainerHost, predicate: (event: string) => boolean): number {
	return host.events.findIndex(predicate);
}

const isRestoreRun = (event: string) =>
	event.startsWith("run:podman run ") && / restore( |$)/.test(event);
const APP_START = "run:systemctl start iforge-postiz.service";

function maintain(run: BackupRun, now: Date) {
	return runContainerCompanionMaintenance({
		paths: RUNTIME_PATHS,
		manifest: manifestWith({ postiz: run.entry }),
		secretValues: run.secrets,
		host: run.host,
		reporter: run.reporter,
		now,
	});
}

describe("postiz restore-on-bootstrap", () => {
	it("restores an empty data directory from a stanza holding a backup before the database first starts, then the bootstrap finds it already bootstrapped", async () => {
		const run = installedBackupPostiz({
			// What the restore brings back: the platform's own organization.
			postiz: { organizations: 1 },
			pgbackrest: { stanza: true, backups: [existingFullBackup()] },
		});

		// The repository was asked, then restored to the end of the archive, both before the
		// database unit (a dependency of the app unit) could start.
		const restoreAt = eventIndex(run.host, isRestoreRun);
		const startAt = run.host.events.indexOf(APP_START);
		expect(restoreAt).toBeGreaterThanOrEqual(0);
		expect(startAt).toBeGreaterThan(restoreAt);
		expectInOrder(run.events, ["pgbackrest:info", "pgbackrest:restore --type=default"]);

		// A one-shot container from the pinned image, in the same user namespace, over the data.
		const restore = pgbackrestCalls(run.host).find((call) => call.args.includes("restore"));
		expect(restore?.args[0]).toBe("run");
		expect(restore?.args).toContain("--rm");
		expect(restore?.args).toContain(
			"ghcr.io/clawdi-ai/postgres-pgbackrest@sha256:db8d1af12ebac7fe10c57730502a31e9b34586ac1ed32852639007dd77eda449",
		);
		expect(restore?.args).toContain("--uidmap=0:1000065536:65536");
		expect(restore?.args).toContain("--gidmap=0:1000065536:65536");
		expect(restore?.args).toContain("--volume=/var/lib/iforge-postiz/db:/var/lib/postgresql");
		expect(restore?.args).toContain(`--stanza=${BACKUP_STANZA}`);
		expect(restore?.args).toContain("--type=default");
		for (const arg of everyArgv(run.host)) {
			expect(arg).not.toContain(BACKUP_KEY_SECRET);
			expect(arg).not.toContain(BACKUP_CIPHER_PASS);
		}

		// The report path: a progress line says a backup was restored, the bootstrap takes its
		// already-bootstrapped branch, and no first backup is taken over a restored repository.
		const outcome = await runReadiness(run);
		expect(outcome).toEqual({ outcome: "ready", step: "ready" });
		const lines = reportsOf(run, "progress").flatMap((report) =>
			(report.lines ?? []).map((line) => line.line),
		);
		expect(lines.some((line) => /restor/i.test(line) && line.includes("20260923-110000F"))).toBe(
			true,
		);
		expect(lines).toContain("already bootstrapped; registration skipped");
		expect(registerCalls(run)).toHaveLength(0);
		expect(run.events).not.toContain("pgbackrest:backup --type=full");
		expect(reportsOf(run, "backup")).toHaveLength(0);
	});

	it("starts fresh over an empty data directory when the repository has no stanza", async () => {
		const run = installedBackupPostiz({ pgbackrest: { stanza: false } });

		expect(run.events).toContain("pgbackrest:info");
		expect(run.events.some((event) => event.startsWith("pgbackrest:restore"))).toBe(false);
		expect(run.host.events).toContain(APP_START);
		expect(transientFiles(run.host)).toEqual([]);

		const outcome = await runReadiness(run);
		expect(outcome).toEqual({ outcome: "ready", step: "ready" });
		expectInOrder(run.events, [
			"pgbackrest:stanza-create",
			"pgbackrest:backup --type=full",
			"fetch:POST /api/auth/register",
		]);
	});

	it("never consults the repository for a populated data directory", () => {
		const entry = backupEntry();
		const pgbackrest = new FakePgBackRest([], { stanza: true, backups: [existingFullBackup()] });
		const host = new FakeContainerHost({ pgbackrest: pgbackrest.handle });
		host.files.set(PGDATA_VERSION_FILE, {
			content: "18\n",
			mode: 0o600,
			uid: 1_000_065_536 + 999,
			gid: 1_000_065_536 + 999,
		});

		// 06-REVIEW WR-02: nothing on this host (no receipt, no schema marker, no generated app unit)
		// names the digest that wrote the data, so the pin is refused over it rather than started.
		expect(() => planFor(host, entry, backupSecrets(entry))).toThrow(
			ContainerCompanionConditionError,
		);
		expect(pgbackrestCalls(host)).toEqual([]);
		expect(host.events).not.toContain(APP_START);
	});

	it("removes the transient env files after the one-shot restore even when it fails, and never starts the database", () => {
		const entry = backupEntry();
		const secrets = backupSecrets(entry);
		const events: string[] = [];
		const pgbackrest = new FakePgBackRest(events, {
			stanza: true,
			backups: [existingFullBackup()],
			failRestore: true,
		});
		const host = new FakeContainerHost({ pgbackrest: pgbackrest.handle });

		expect(() =>
			applyContainerCompanion(planFor(host, entry, secrets), host, HOSTED_PATHS),
		).toThrow(/restore/);

		// Written, used, removed: nothing plaintext is left on the run root.
		const written = host.events.filter(
			(event) => event.startsWith(`write:${TRANSIENT_ROOT}`) && event.includes("backup"),
		);
		expect(written.length).toBeGreaterThanOrEqual(1);
		for (const event of written) {
			expect(host.events).toContain(event.replace(/^write:/, "remove:"));
		}
		expect(transientFiles(host)).toEqual([]);
		// The database never started over a partial restore, and the next converge knows the
		// directory holds one: it restores again rather than starting what is there.
		expect(host.events).not.toContain(APP_START);
		expect(host.files.has(RESTORE_INCOMPLETE_MARKER)).toBe(true);
	});
});

describe("postiz scheduled backups with rehearsal markers", () => {
	async function installedAndReady(
		pgbackrestOptions: FakePgBackRestOptions = {},
	): Promise<BackupRun & { clock: { now: number } }> {
		const clock = { now: INSTALLED_AT };
		const run = installedBackupPostiz({
			pgbackrest: { ...pgbackrestOptions, now: () => clock.now / 1000 },
		});
		const outcome = await runReadiness(run);
		expect(outcome).toEqual({ outcome: "ready", step: "ready" });
		return { ...run, clock };
	}

	it("does nothing 23 hours after the last backup", async () => {
		const run = await installedAndReady();
		const before = run.reporter.reports.length;

		const outcome = await maintain(run, new Date(INSTALLED_AT + 23 * HOUR_MS));

		expect(outcome.outcome).toBe("skipped");
		expect(run.events).not.toContain("psql:marker");
		expect(run.events).not.toContain("pgbackrest:backup --type=diff");
		expect(run.reporter.reports.length).toBe(before);
	});

	it("writes a marker, then takes a differential backup, and reports both 24 hours after the last one", async () => {
		const run = await installedAndReady();
		const now = new Date(INSTALLED_AT + 24 * HOUR_MS);
		run.clock.now = now.getTime();

		const outcome = await maintain(run, now);

		expect(outcome.outcome).toBe("backed-up");
		expectInOrder(run.events, ["psql:marker", "pgbackrest:backup --type=diff", "report:backup"]);
		// The marker went in through psql's standard input, into its own schema, never `public`.
		const script = run.postiz.scripts.find((candidate) => candidate.includes("iforge_rehearsal"));
		expect(script).toContain("CREATE SCHEMA IF NOT EXISTS iforge_rehearsal");
		expect(script).toContain("iforge_rehearsal.marker");
		expect(script).not.toMatch(/\bpublic\./);
		const [marker] = run.postiz.markers;
		expect(marker?.token).toMatch(/^[0-9a-f]{32}$/);
		for (const arg of everyArgv(run.host)) expect(arg).not.toContain(marker?.token ?? "?");

		const report = reportsOf(run, "backup").at(-1);
		const newest = run.pgbackrest.backups.at(-1);
		expect(newest?.type).toBe("diff");
		expect(report).toEqual({
			operation: "install",
			phase: "backup",
			backup: {
				ok: true,
				label: newest?.label ?? "?",
				started_at: now.toISOString(),
				completed_at: now.toISOString(),
				db_bytes: newest?.size ?? -1,
				// The database's own clock, at the precision it keeps.
				marker: { id: 1, token: marker?.token ?? "?", written_at: "2026-09-24T12:00:00.000000Z" },
			},
		});

		// Recorded: the next pass, an hour later, has nothing to do.
		const later = await maintain(run, new Date(now.getTime() + HOUR_MS));
		expect(later.outcome).toBe("skipped");
		expect(run.postiz.markers).toHaveLength(1);
	});

	it("takes a full backup when the newest full one is seven days old", async () => {
		const run = await installedAndReady();
		const now = new Date(INSTALLED_AT + 7 * 24 * HOUR_MS);
		run.clock.now = now.getTime();

		const outcome = await maintain(run, now);

		expect(outcome.outcome).toBe("backed-up");
		expectInOrder(run.events, ["psql:marker", "pgbackrest:backup --type=full"]);
		expect(run.pgbackrest.backups.at(-1)?.type).toBe("full");
	});

	it("reports a failed backup as ok false with the marker, and keeps the error redacted", async () => {
		const run = await installedAndReady();
		run.pgbackrest.failBackup = true;
		const now = new Date(INSTALLED_AT + 25 * HOUR_MS);

		const outcome = await maintain(run, now);

		expect(outcome.outcome).toBe("failed");
		if (outcome.outcome !== "failed") throw new Error("the backup did not fail");
		expect(outcome.error).toContain("[redacted]");
		expect(outcome.error).not.toContain(BACKUP_CIPHER_PASS);
		// Retried after an hour, not on every watch iteration.
		expect((await maintain(run, new Date(now.getTime() + HOUR_MS / 2))).outcome).toBe("skipped");
		expect(run.postiz.markers).toHaveLength(1);
		const report = reportsOf(run, "backup").at(-1);
		expect(report?.phase).toBe("backup");
		if (report?.phase !== "backup") throw new Error("no backup report");
		expect(report.backup.ok).toBe(false);
		expect(report.backup.label).toBeNull();
		expect(report.backup.marker?.token).toBe(run.postiz.markers[0]?.token);
		expect(JSON.stringify(run.reporter.reports)).not.toContain(BACKUP_CIPHER_PASS);
	});

	it("gives a backup or a restore an hour on the real host, and every other command two minutes", () => {
		const exec = ["exec", "--user", "postgres", "iforge-postiz-db", "pgbackrest"];
		expect(containerCommandTimeoutMs("podman", [...exec, "--stanza=s", "backup"])).toBe(3_600_000);
		expect(containerCommandTimeoutMs("podman", ["run", "pgbackrest", "restore"])).toBe(3_600_000);
		expect(containerCommandTimeoutMs("podman", [...exec, "--stanza=s", "info"])).toBe(120_000);
		expect(containerCommandTimeoutMs("podman", ["pull", "x@sha256:0"])).toBe(1_800_000);
		expect(containerCommandTimeoutMs("systemctl", ["start", "backup"])).toBe(120_000);
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-18: the backup-gated update, and its rollback (APP-06, OPS-02).
// ---------------------------------------------------------------------------------------------

const NEWER_APP_DIGEST = "e".repeat(64);
const APP_SOURCE = "/etc/containers/systemd/iforge-postiz.container";
const APP_RESTART = "run:systemctl restart iforge-postiz.service";

/** An installed, backed-up and READY Postiz, with host and loopback events on one timeline. */
async function readyForUpdate(
	options: { postiz?: FakePostizOptions; pgbackrest?: FakePgBackRestOptions } = {},
): Promise<BackupRun> {
	const entry = backupEntry();
	const secrets = backupSecrets(entry);
	const events: string[] = [];
	const postiz = new FakePostiz(events, options.postiz);
	const pgbackrest = new FakePgBackRest(events, options.pgbackrest);
	const host = new FakeContainerHost({ psql: postiz.psql, pgbackrest: pgbackrest.handle, events });
	applyContainerCompanion(planFor(host, entry, secrets), host, HOSTED_PATHS);
	const run: BackupRun = {
		entry,
		secrets,
		host,
		postiz,
		pgbackrest,
		reporter: new RecordingReporter(events),
		events,
	};
	expect(await runReadiness(run)).toEqual({ outcome: "ready", step: "ready" });
	return run;
}

/** The entry after the tenant accepted `sha256`: the pin moved, and the transition is announced. */
function withAppDigest(entry: PostizCompanionManifest, sha256: string): PostizCompanionManifest {
	return postizCompanionSchema.parse({
		...entry,
		artifact: { ...entry.artifact, sha256 },
		update: { fromDigest: entry.artifact.sha256, toDigest: sha256 },
	});
}

function runUpdate(run: BootstrapRun, entry: PostizCompanionManifest) {
	return containerCompanionModule.applyPostizUpdate({
		manifest: manifestWith({ postiz: entry }),
		secretValues: run.secrets,
		paths: RUNTIME_PATHS,
		host: run.host,
		loopback: run.postiz.loopback(),
		reporter: run.reporter,
	});
}

describe("postiz update tracer: an accepted digest is backed up, swapped and asserted", () => {
	it("backs up before the pull, pulls before rewriting the unit, restarts, asserts on data, then reports ready", async () => {
		expect(typeof containerCompanionModule.applyPostizUpdate).toBe("function");
		const run = await readyForUpdate();
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const newerReference = reference(newer.artifact.image, NEWER_APP_DIGEST);
		const from = run.events.length;

		const outcome = await runUpdate(run, newer);

		expect(outcome).toMatchObject({ outcome: "ready", step: "ready" });
		const timeline = run.events.slice(from);
		expectInOrder(timeline, [
			"report:progress",
			"pgbackrest:backup --type=diff",
			"report:backup",
			"psql:org-count",
			`run:podman pull ${newerReference}`,
			`write:${APP_SOURCE}`,
			APP_RESTART,
			"fetch:GET /api/public/v1/is-connected +key",
			"psql:org-count",
			"report:ready",
		]);
		// Nothing was pulled, written or restarted before the backup finished.
		const backupAt = timeline.indexOf("pgbackrest:backup --type=diff");
		expect(
			timeline
				.slice(0, backupAt)
				.filter((event) => /^(run:podman pull|write:\/etc|run:systemctl restart)/.test(event)),
		).toEqual([]);

		const updateReports = run.reporter.reports.filter((report) => report.operation === "update");
		expect(updateReports[0]).toMatchObject({
			phase: "progress",
			lines: [{ stream: "stdout", line: "Taking a backup before updating." }],
		});
		expect(updateReports.find((report) => report.phase === "backup")).toMatchObject({
			backup: { ok: true, label: expect.stringMatching(/^20260923-120000F_/) },
		});
		expect(run.reporter.last()).toEqual({
			operation: "update",
			phase: "ready",
			step: "ready",
			assertion: { connected: true, organizations: 1, previous_organizations: 1 },
			applied_digest: NEWER_APP_DIGEST,
			admin_user_id: ADMIN_USER_ID,
			to_digest: NEWER_APP_DIGEST,
		});
		// The unit now runs the new digest, and the receipt records it as applied and reported.
		expect(run.host.sources().get(APP_SOURCE)).toContain(newerReference);
		const receipt = JSON.parse(run.host.readFile(HOSTED_PATHS.receiptPath) ?? "{}");
		expect(receipt.images.app).toBe(newerReference);
		expect(receipt.bootstrap?.phase).toBe("ready");
		// The readiness pass that follows every converge has nothing left to report.
		expect(await runReadiness({ ...run, entry: newer })).toMatchObject({ outcome: "skipped" });
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-18 Task 3: the runtime's failure paths and idempotency.
// ---------------------------------------------------------------------------------------------

const QUADLET_WRITE_PREFIX = "write:/etc/containers/systemd/";
const APP_STOP_THEN_DB = "run:systemctl stop iforge-postiz.service iforge-postiz-db.service";

/** The update's outcome, or what it threw -- so a throw fails on an assertion, not an exception. */
async function settle(promise: Promise<unknown>): Promise<unknown> {
	return promise.catch((error: unknown) => ({
		thrown: error instanceof Error ? error.message : String(error),
	}));
}

function updateReports(run: BootstrapRun): CompanionInstallReport[] {
	return run.reporter.reports.filter((report) => report.operation === "update");
}

function swapEvents(timeline: readonly string[]): string[] {
	return timeline.filter(
		(event) =>
			event.startsWith("run:podman pull") ||
			event.startsWith(QUADLET_WRITE_PREFIX) ||
			event.startsWith("run:systemctl restart"),
	);
}

describe("postiz update: failure paths, rollback and idempotency", () => {
	it("aborts on a failed backup: no pull, no unit write, no restart, and a failed report at step backup", async () => {
		const run = await readyForUpdate();
		run.pgbackrest.failBackup = true;
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({ outcome: "failed", step: "backup" });
		const timeline = run.events.slice(from);
		expect(timeline).toContain("pgbackrest:backup --type=diff");
		expect(swapEvents(timeline)).toEqual([]);
		expect(run.host.sources().get(APP_SOURCE)).toContain(run.entry.artifact.sha256);
		expect(run.reporter.last()).toMatchObject({
			operation: "update",
			phase: "failed",
			step: "backup",
		});
		// The failed backup itself is reported, and the receipt still runs the previous digest.
		expect(updateReports(run).find((report) => report.phase === "backup")).toMatchObject({
			backup: { ok: false },
		});
		const receipt = JSON.parse(run.host.readFile(HOSTED_PATHS.receiptPath) ?? "{}");
		expect(receipt.images.app).toBe(reference(run.entry.artifact.image, run.entry.artifact.sha256));
	});

	it("rolls back a failed data assertion: stops the app, restores the pre-update set, restarts the previous digest, re-asserts", async () => {
		const run = await readyForUpdate();
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const previousReference = reference(run.entry.artifact.image, run.entry.artifact.sha256);
		// The new digest's start-time schema push loses the organization; the restore brings it back.
		const realRun = run.host.run.bind(run.host);
		run.host.run = (command, args, options) => {
			const result = realRun(command, args, options);
			if (command === "systemctl" && args[0] === "restart") run.postiz.organizations = 0;
			if (command === "podman" && args[0] === "run" && args.includes("restore")) {
				run.postiz.organizations = 1;
			}
			return result;
		};
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({ outcome: "failed", step: "post-update-assertion" });
		const timeline = run.events.slice(from);
		expectInOrder(timeline, [
			APP_RESTART,
			"psql:org-count",
			APP_STOP_THEN_DB,
			`write:${APP_SOURCE}`,
			"pgbackrest:restore --type=immediate",
			"run:systemctl start iforge-postiz-db.service iforge-postiz-redis.service iforge-postiz.service",
			"psql:org-count",
			"report:failed",
		]);
		// The restore is the pre-update backup set, applied in place, to its end, then promoted.
		const backup = updateReports(run).find((report) => report.phase === "backup");
		const label = backup?.phase === "backup" ? backup.backup.label : null;
		expect(label).toMatch(/^20260923-120000F_/);
		const restore = run.pgbackrest.calls.find((argv) => argv.includes("restore")) ?? [];
		expect(restore.slice(0, 2)).toEqual(["run", "--rm"]);
		for (const arg of [
			"--delta",
			`--set=${label}`,
			"--type=immediate",
			"--target-action=promote",
		]) {
			expect(restore).toContain(arg);
		}
		// The previous digest runs again, and the receipt never moved.
		expect(run.host.sources().get(APP_SOURCE)).toContain(previousReference);
		const receipt = JSON.parse(run.host.readFile(HOSTED_PATHS.receiptPath) ?? "{}");
		expect(receipt.images.app).toBe(previousReference);
		expect(run.reporter.last()).toMatchObject({
			operation: "update",
			phase: "failed",
			step: "post-update-assertion",
		});
		const last = run.reporter.last();
		expect(last?.phase === "failed" ? last.error : "").toContain("previous version passed");
		// No transient credential file outlives the one-shot restore.
		expect(transientFiles(run.host)).toEqual([]);
	});

	it("does nothing when the entry's digest is the applied one: no backup, no pull, no restart", async () => {
		const run = await readyForUpdate();
		const from = run.events.length;
		const reportsBefore = run.reporter.reports.length;

		const outcome = await settle(runUpdate(run, run.entry));

		expect(outcome).toEqual({ outcome: "skipped", reason: "no digest change" });
		const timeline = run.events.slice(from);
		expect(timeline.filter((event) => event.startsWith("pgbackrest:"))).toEqual([]);
		expect(swapEvents(timeline)).toEqual([]);
		expect(run.reporter.reports.length).toBe(reportsBefore);
	});

	it("takes a fresh backup after a converge interrupted between its backup and the swap", async () => {
		const clock = { seconds: INSTALLED_AT / 1000 };
		const run = await readyForUpdate({ pgbackrest: { now: () => clock.seconds } });
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		run.host.throwOn = (command, args) => command === "podman" && args[0] === "pull";

		const interrupted = await settle(runUpdate(run, newer));
		expect(interrupted).toMatchObject({ thrown: expect.stringContaining("interrupted") });
		const firstLabel = run.pgbackrest.backups.at(-1)?.label;
		expect(firstLabel).toMatch(/D$/);
		expect(run.host.sources().get(APP_SOURCE)).toContain(run.entry.artifact.sha256);

		run.host.throwOn = null;
		clock.seconds += 600;
		const from = run.events.length;
		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({ outcome: "ready" });
		const timeline = run.events.slice(from);
		expectInOrder(timeline, ["pgbackrest:backup --type=diff", `write:${APP_SOURCE}`]);
		const secondLabel = run.pgbackrest.backups.at(-1)?.label;
		expect(secondLabel).not.toBe(firstLabel);
		// The gate this converge passed is its own backup, never the interrupted one's.
		const gateLines = updateReports(run)
			.flatMap((report) => (report.phase === "progress" ? (report.lines ?? []) : []))
			.map((line) => line.line)
			.filter((line) => line.startsWith("Backup "));
		expect(gateLines.at(-1)).toContain(secondLabel ?? "?");
		expect(gateLines.at(-1)).not.toContain(firstLabel ?? "?");
	});

	it("passes every update report line through the redactor", async () => {
		const run = await readyForUpdate();
		run.pgbackrest.failBackup = true;
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);

		await settle(runUpdate(run, newer));

		const reports = updateReports(run);
		expect(reports.some((report) => report.phase === "failed")).toBe(true);
		const text = JSON.stringify(reports);
		for (const secret of [BACKUP_CIPHER_PASS, BACKUP_KEY_SECRET, API_KEY_TOKEN, ADMIN_PASSWORD]) {
			expect(text).not.toContain(secret);
		}
		const failed = reports.find((report) => report.phase === "failed");
		expect(failed?.phase === "failed" ? failed.error : "").toContain("[redacted]");
	});

	it("does not retry a refused digest while the manifest still names it, and treats it as new once the pin came back", async () => {
		const run = await readyForUpdate();
		run.pgbackrest.failBackup = true;
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "failed" });
		run.pgbackrest.failBackup = false;

		// Until the control plane returns the pin, the same digest is not tried again.
		const from = run.events.length;
		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "skipped" });
		expect(run.events.slice(from).filter((event) => event.startsWith("pgbackrest:"))).toEqual([]);

		// The pin came back; a later acceptance of the same digest is a fresh update.
		expect(await settle(runUpdate(run, run.entry))).toMatchObject({ outcome: "skipped" });
		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "ready" });
	});

	it("keeps an undelivered terminal report and delivers it next time without updating again", async () => {
		const run = await readyForUpdate();
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const realSend = run.reporter.send.bind(run.reporter);
		let down = true;
		run.reporter.send = async (report) => {
			if (down && report.phase === "ready") throw new Error("control plane unreachable");
			return realSend(report);
		};

		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "unreportable" });
		down = false;
		const from = run.events.length;
		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "ready" });
		const timeline = run.events.slice(from);
		expect(timeline.filter((event) => event.startsWith("pgbackrest:"))).toEqual([]);
		expect(swapEvents(timeline)).toEqual([]);
		expect(run.reporter.last()).toMatchObject({ operation: "update", phase: "ready" });
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-40 (CR-03): a ready report the control plane refuses rolls the update back.
// ---------------------------------------------------------------------------------------------

type ScriptedAnswer = CompanionReportResult | Error;

/**
 * A recording reporter whose answer is chosen per report phase: `recorded` unless scripted. An
 * `Error` answer is thrown, as a transport failure would be.
 */
class ScriptedReporter extends RecordingReporter {
	constructor(
		events: string[],
		readonly answers: Partial<Record<CompanionInstallReport["phase"], ScriptedAnswer>> = {},
	) {
		super(events);
	}

	override async send(report: CompanionInstallReport): Promise<CompanionReportResult> {
		const recorded = await super.send(report);
		const answer = this.answers[report.phase];
		if (answer instanceof Error) throw answer;
		return answer ?? recorded;
	}
}

function refusedAnswer(code: string | null, terminalStatus: string | null): CompanionReportResult {
	return { outcome: "refused", status: 409, code, terminalStatus };
}

const REFUSED_AS_FAILED = refusedAnswer("companion_install_terminal", "failed");
const APP_START_PREVIOUS =
	"run:systemctl start iforge-postiz-db.service iforge-postiz-redis.service iforge-postiz.service";
const NEWER_POSTGRES_DIGEST = "f".repeat(64);

function withPostgresDigest(
	entry: PostizCompanionManifest,
	sha256: string,
): PostizCompanionManifest {
	return postizCompanionSchema.parse({
		...entry,
		dependencies: {
			...entry.dependencies,
			postgres: { ...entry.dependencies.postgres, sha256 },
		},
	});
}

function receiptOf(run: BootstrapRun): Record<string, unknown> & {
	images: Record<string, string>;
	refusedUpdate?: { images: Record<string, string>; step: string; label: string | null };
	bootstrap?: { phase: string };
} {
	return JSON.parse(run.host.readFile(HOSTED_PATHS.receiptPath) ?? "{}");
}

function preUpdateLabel(run: BootstrapRun): string | null {
	const backup = updateReports(run).find((report) => report.phase === "backup");
	return backup?.phase === "backup" ? backup.backup.label : null;
}

describe("postiz update: a ready report the control plane refuses (plan 06-40, CR-03)", () => {
	it("a ready report the control plane refuses rolls the update back from the pre-update backup", async () => {
		const run = await readyForUpdate();
		const previousImages = { ...receiptOf(run).images };
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const newerReference = reference(newer.artifact.image, NEWER_APP_DIGEST);
		const previousReference = reference(run.entry.artifact.image, run.entry.artifact.sha256);
		run.reporter = new ScriptedReporter(run.events, { ready: REFUSED_AS_FAILED });
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, newer));

		const label = preUpdateLabel(run);
		expect(label).toMatch(/^20260923-120000F_/);
		expect(outcome).toMatchObject({
			outcome: "rolled-back",
			step: "control-plane-refused",
			label,
			code: "companion_install_terminal",
		});
		const timeline = run.events.slice(from);
		expectInOrder(timeline, [
			APP_RESTART,
			"report:ready",
			APP_STOP_THEN_DB,
			`write:${APP_SOURCE}`,
			"pgbackrest:restore --type=immediate",
			APP_START_PREVIOUS,
		]);
		const restore = run.pgbackrest.calls.find((argv) => argv.includes("restore")) ?? [];
		expect(restore).toContain(`--set=${label}`);
		// The host runs the previous digest again, and the receipt records exactly that.
		expect(run.host.sources().get(APP_SOURCE)).toContain(previousReference);
		const receipt = receiptOf(run);
		expect(receipt.images).toEqual(previousImages);
		expect(receipt.refusedUpdate?.images.app).toBe(newerReference);
		expect(receipt.refusedUpdate?.step).toBe("control-plane-refused");
		expect(receipt.refusedUpdate?.label).toBe(label);
		expect(receipt.pendingUpdateReport).toBeUndefined();
		expect(receipt.pendingUpdateRollback).toBeUndefined();
		expect(transientFiles(run.host)).toEqual([]);
	});

	it("after a refused update the reverted pin plans nothing", async () => {
		const run = await readyForUpdate();
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		run.reporter = new ScriptedReporter(run.events, { ready: REFUSED_AS_FAILED });
		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "rolled-back" });
		const from = run.events.length;
		const reportsBefore = run.reporter.reports.length;

		// The control plane reverted the pin to the previous digest: nothing to pull, back up or restart.
		const outcome = await settle(runUpdate(run, run.entry));

		expect(outcome).toEqual({ outcome: "skipped", reason: "no digest change" });
		const timeline = run.events.slice(from);
		expect(timeline.filter((event) => event.startsWith("pgbackrest:"))).toEqual([]);
		expect(swapEvents(timeline)).toEqual([]);
		expect(timeline.filter((event) => event.startsWith("run:systemctl"))).toEqual([]);
		expect(run.reporter.reports.length).toBe(reportsBefore);
	});

	it("a refused ready delivered by a later converge still rolls back", async () => {
		const run = await readyForUpdate();
		const previousImages = { ...receiptOf(run).images };
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const scripted = new ScriptedReporter(run.events, {
			ready: new Error("control plane unreachable"),
		});
		run.reporter = scripted;

		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "unreportable" });
		expect(receiptOf(run).pendingUpdateReport).toMatchObject({ phase: "ready" });
		expect(receiptOf(run).pendingUpdateRollback).toBeDefined();

		scripted.answers.ready = REFUSED_AS_FAILED;
		const from = run.events.length;
		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({ outcome: "rolled-back", step: "control-plane-refused" });
		const timeline = run.events.slice(from);
		// Delivered, then rolled back -- never re-backed-up or re-pulled.
		expect(timeline.filter((event) => event.startsWith("run:podman pull"))).toEqual([]);
		expectInOrder(timeline, [
			"report:ready",
			APP_STOP_THEN_DB,
			"pgbackrest:restore --type=immediate",
			APP_START_PREVIOUS,
		]);
		const receipt = receiptOf(run);
		expect(receipt.images).toEqual(previousImages);
		expect(receipt.pendingUpdateReport).toBeUndefined();
		expect(receipt.pendingUpdateRollback).toBeUndefined();
	});

	it("a re-sent ready answered already-terminal settles without rollback", async () => {
		const run = await readyForUpdate();
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const newerReference = reference(newer.artifact.image, NEWER_APP_DIGEST);
		run.reporter = new ScriptedReporter(run.events, {
			ready: { outcome: "already-terminal", status: 409 },
		});
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({ outcome: "ready", step: "ready" });
		const timeline = run.events.slice(from);
		expect(timeline.filter((event) => event.startsWith("pgbackrest:restore"))).toEqual([]);
		const receipt = receiptOf(run);
		expect(receipt.images.app).toBe(newerReference);
		expect(receipt.bootstrap?.phase).toBe("ready");
		expect(receipt.pendingUpdateReport).toBeUndefined();
		expect(receipt.pendingUpdateRollback).toBeUndefined();
	});

	it("a refused ready for a dependency-only change is settled, not rolled back", async () => {
		const run = await readyForUpdate();
		const dependencyOnly = withPostgresDigest(run.entry, NEWER_POSTGRES_DIGEST);
		const newerPostgres = reference(
			dependencyOnly.dependencies.postgres.image,
			NEWER_POSTGRES_DIGEST,
		);
		run.reporter = new ScriptedReporter(run.events, {
			ready: refusedAnswer("companion_report_operation_mismatch", null),
		});
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, dependencyOnly));

		expect(outcome).toMatchObject({ outcome: "ready", step: "ready" });
		const timeline = run.events.slice(from);
		expect(timeline.filter((event) => event.startsWith("pgbackrest:restore"))).toEqual([]);
		const receipt = receiptOf(run);
		expect(receipt.images.postgres).toBe(newerPostgres);
		expect(receipt.bootstrap?.phase).toBe("ready");
		expect(receipt.refusedUpdate).toBeUndefined();
		expect(receipt.pendingUpdateReport).toBeUndefined();
		expect(receipt.pendingUpdateRollback).toBeUndefined();
	});
});

describe("every caller of send answers a refusal (plan 06-40, WR-01, CR-03)", () => {
	it("a ready refused as a digest mismatch rolls back", async () => {
		const run = await readyForUpdate();
		const previousImages = { ...receiptOf(run).images };
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		run.reporter = new ScriptedReporter(run.events, {
			ready: refusedAnswer("companion_ready_digest_mismatch", null),
		});
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({
			outcome: "rolled-back",
			step: "control-plane-refused",
			code: "companion_ready_digest_mismatch",
		});
		expectInOrder(run.events.slice(from), [
			"report:ready",
			APP_STOP_THEN_DB,
			"pgbackrest:restore --type=immediate",
			APP_START_PREVIOUS,
		]);
		expect(receiptOf(run).images).toEqual(previousImages);
	});

	it("an install that failed at apply and later applied is not settled ready when its ready is refused", async () => {
		const entry = backupEntry();
		const secrets = backupSecrets(entry);
		const events: string[] = [];
		const postiz = new FakePostiz(events);
		const pgbackrest = new FakePgBackRest(events);
		const host = new FailingPullHost({ psql: postiz.psql, pgbackrest: pgbackrest.handle, events });
		const manifest = manifestWith({ postiz: entry });
		const paths = convergePaths();
		const reporter = new ScriptedReporter(events);

		// Pass 1: the pull fails; the converge records it and the install is failed at step apply.
		const first = reconcileContainerCompanions({ manifest, paths, secretValues: secrets, host });
		expect(first.failure?.lifecycle).toBe("present");
		const failure = await applyFailureApi()({
			manifest,
			secretValues: secrets,
			paths: RUNTIME_PATHS,
			host,
			reporter,
		});
		expect(failure.outcome).toBe("reported");
		expect(reporter.last()).toMatchObject({ phase: "failed", step: "apply" });

		// Pass 2: the pulls succeed and the converge applies -- but the control plane already failed
		// this install, so it refuses the ready report.
		host.pullsFail = false;
		const second = reconcileContainerCompanions({ manifest, paths, secretValues: secrets, host });
		expect(second.failure).toBeNull();
		reporter.answers.ready = REFUSED_AS_FAILED;
		const run: BackupRun = { entry, secrets, host, postiz, pgbackrest, reporter, events };

		const outcome = await runReadiness(run);

		expect(outcome).toEqual({
			outcome: "refused",
			step: "ready",
			code: "companion_install_terminal",
			terminalStatus: "failed",
		});
		expect(receiptOf(run).bootstrap).toMatchObject({
			phase: "failed",
			refused: { code: "companion_install_terminal", terminalStatus: "failed" },
		});
		// The same apply is not bootstrapped or reported again.
		const reportsBefore = reporter.reports.length;
		expect((await runReadiness(run)).outcome).toBe("skipped");
		expect(reporter.reports.length).toBe(reportsBefore);
		// Everything gated on a ready installation skips it.
		expect(await maintain(run, new Date(INSTALLED_AT + 25 * HOUR_MS))).toEqual({
			outcome: "skipped",
			reason: "installation not ready",
		});
	});

	it("an install ready refused for anything but an operation mismatch is never settled ready", async () => {
		const refusals: Array<[string | null, string | null]> = [
			["companion_install_terminal", "failed"],
			["companion_ready_digest_mismatch", null],
			["companion_install_not_running", "queued"],
			[null, null],
		];
		for (const [code, terminalStatus] of refusals) {
			const run = installedPostiz();
			run.reporter = new ScriptedReporter(run.events, {
				ready: refusedAnswer(code, terminalStatus),
			});

			const outcome = await runReadiness(run);

			expect(outcome).toEqual({ outcome: "refused", step: "ready", code, terminalStatus });
			const bootstrap = receiptOf(run).bootstrap;
			expect(bootstrap).toMatchObject({
				phase: "failed",
				step: "ready",
				refused: { code, terminalStatus },
			});
			expect(bootstrap?.phase).not.toBe("ready");
		}
	});

	it("a re-bootstrap refused as an operation mismatch settles ready as before", async () => {
		const clock = { now: INSTALLED_AT };
		const run = installedBackupPostiz({ pgbackrest: { now: () => clock.now / 1000 } });
		expect(await runReadiness(run)).toEqual({ outcome: "ready", step: "ready" });
		// A credential rotation re-renders the units: a new apply, bootstrapped and reported again.
		const rotated = { ...run.secrets };
		rotated[run.entry.credentials.appEnvSecretRef] =
			`${rotated[run.entry.credentials.appEnvSecretRef]}API_LIMIT=91\n`;
		applyContainerCompanion(planFor(run.host, run.entry, rotated), run.host, HOSTED_PATHS);
		const rotatedRun: BackupRun = {
			...run,
			secrets: rotated,
			reporter: new ScriptedReporter(run.events, {
				ready: refusedAnswer("companion_report_operation_mismatch", null),
			}),
		};

		const outcome = await runReadiness(rotatedRun);

		expect(outcome).toEqual({ outcome: "ready", step: "ready" });
		const bootstrap = receiptOf(run).bootstrap;
		expect(bootstrap?.phase).toBe("ready");
		expect(bootstrap).not.toHaveProperty("refused");
		const now = new Date(INSTALLED_AT + 25 * HOUR_MS);
		clock.now = now.getTime();
		expect((await maintain(rotatedRun, now)).outcome).toBe("backed-up");
	});

	it("a refused apply-failure report is answered refused and not re-sent", async () => {
		const host = new FailingPullHost();
		const manifest = manifestWith({ postiz: fixtureEntry() });
		converge(host, manifest);
		const reporter = new ScriptedReporter([], { failed: REFUSED_AS_FAILED });

		const first: unknown = await reportApplyFailureOver(host, manifest, reporter);

		expect(first).toEqual({
			outcome: "refused",
			code: "companion_install_terminal",
			terminalStatus: "failed",
		});
		expect(applyFailureRecord(host)?.reportedAt).toBeString();
		expect((await reportApplyFailureOver(host, manifest, reporter)).outcome).toBe("skipped");
		expect(reporter.reports).toHaveLength(1);
	});

	it("refused progress, backup and removed answers are never counted as delivered", async () => {
		const clock = { now: INSTALLED_AT };
		const run = installedBackupPostiz({ pgbackrest: { now: () => clock.now / 1000 } });
		run.reporter = new ScriptedReporter(run.events, {
			progress: REFUSED_AS_FAILED,
			backup: REFUSED_AS_FAILED,
		});

		// Progress and the first backup's report are best effort: the terminal report decides.
		expect(await runReadiness(run)).toEqual({ outcome: "ready", step: "ready" });
		expect(reportsOf(run, "progress").length).toBeGreaterThan(0);
		expect(reportsOf(run, "backup").length).toBeGreaterThan(0);

		// A refused scheduled backup report is not counted as reported.
		const now = new Date(INSTALLED_AT + 25 * HOUR_MS);
		clock.now = now.getTime();
		expect(await maintain(run, now)).toMatchObject({ outcome: "backed-up", reported: false });

		// A refused `removed` report is answered refused, never reported.
		const removalHost = new FakeContainerHost();
		converge(removalHost, manifestWith({ postiz: fixtureEntry() }));
		seedData(removalHost);
		const removal = manifestWith({ postiz: removeEntry() });
		converge(removalHost, removal);
		const removalReporter = new ScriptedReporter([], {
			removed: refusedAnswer("companion_report_operation_mismatch", null),
		});
		const removed: unknown = await removalApi().reportRemoved({
			manifest: removal,
			secretValues: fixtureSecrets(fixtureEntry()),
			paths: RUNTIME_PATHS,
			host: removalHost,
			reporter: removalReporter,
		});
		expect(removed).toEqual({ outcome: "refused", code: "companion_report_operation_mismatch" });
		expect(removalReporter.reports).toHaveLength(1);
	});
});

/** A recording reporter that also puts each progress report's step on the timeline. */
class ProgressStepReporter extends ScriptedReporter {
	override async send(report: CompanionInstallReport): Promise<CompanionReportResult> {
		const result = await super.send(report);
		if (report.phase === "progress") this.timeline.push(`progress:${report.step ?? ""}`);
		return result;
	}

	constructor(
		private readonly timeline: string[],
		answers: Partial<Record<CompanionInstallReport["phase"], ScriptedAnswer>> = {},
	) {
		super(timeline, answers);
	}
}

describe("postiz update: progress keeps a healthy update inside its deadline (plan 06-40)", () => {
	it("the update reports progress after every image pull and before waiting for Postiz", async () => {
		const run = await readyForUpdate();
		const newer = withPostgresDigest(
			withAppDigest(run.entry, NEWER_APP_DIGEST),
			NEWER_POSTGRES_DIGEST,
		);
		run.reporter = new ProgressStepReporter(run.events);
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({ outcome: "ready" });
		const timeline = run.events.slice(from);
		const pulls = timeline
			.map((event, index) => ({ event, index }))
			.filter(({ event }) => event.startsWith("run:podman pull"));
		expect(pulls).toHaveLength(2);
		for (const { event, index } of pulls) {
			const nextReport = timeline.findIndex(
				(candidate, at) => at > index && candidate.startsWith("report:"),
			);
			expect(timeline[nextReport]).toBe("report:progress");
			expect(timeline[nextReport + 1]).toBe("progress:swap");
			expect(event).toMatch(/^run:podman pull /);
		}
		const restart = timeline.findIndex((event) => event.startsWith("run:systemctl restart"));
		const firstAsk = timeline.findIndex(
			(event, at) => at > restart && event.startsWith("fetch:GET /api/public/v1/is-connected"),
		);
		const waiting = timeline.indexOf("progress:post-update-assertion", restart);
		expect(restart).toBeGreaterThanOrEqual(0);
		expect(firstAsk).toBeGreaterThan(restart);
		expect(waiting).toBeGreaterThan(restart);
		expect(waiting).toBeLessThan(firstAsk);
	});

	it("a progress send that throws does not stop the update", async () => {
		const run = await readyForUpdate();
		const newer = withPostgresDigest(
			withAppDigest(run.entry, NEWER_APP_DIGEST),
			NEWER_POSTGRES_DIGEST,
		);
		run.reporter = new ScriptedReporter(run.events, {
			progress: new Error("control plane unreachable"),
		});

		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({ outcome: "ready", step: "ready" });
		expect(run.reporter.last()).toMatchObject({ operation: "update", phase: "ready" });
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-41 (CR-03): the host changes the app digest only on the transition the control plane
// announced. Digests carry no order, so "older" is never decided here: any app-digest change the
// entry's `update` does not name exactly is refused before anything runs.
// ---------------------------------------------------------------------------------------------

/** A digest neither the host nor the entry's pin names. */
const FOREIGN_APP_DIGEST = "c".repeat(64);

/** Every event that would back up, pull, rewrite a unit, or start, stop or restart one. */
function mutatingEvents(timeline: readonly string[]): string[] {
	return timeline.filter(
		(event) =>
			event.startsWith("pgbackrest:") ||
			event.startsWith("run:podman pull") ||
			event.startsWith("run:systemctl") ||
			event.startsWith(QUADLET_WRITE_PREFIX),
	);
}

describe("postiz update: only an announced app-digest transition is applied (plan 06-41, CR-03)", () => {
	it("a reverted pin never downgrades a host running the newer images (CR-03)", async () => {
		const run = await readyForUpdate();
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "ready" });
		expect(receiptOf(run).images.app).toBe(reference(newer.artifact.image, NEWER_APP_DIGEST));
		// The control plane failed the update (its deadline passed) and returned the pin: the entry
		// names the older digest again and announces nothing.
		const reverted = run.entry;
		expect(reverted.artifact.sha256).not.toBe(NEWER_APP_DIGEST);
		const receiptBefore = run.host.readFile(HOSTED_PATHS.receiptPath);
		const reportsBefore = run.reporter.reports.length;
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, reverted));

		expect(outcome).toMatchObject({ outcome: "unrequested" });
		expect(mutatingEvents(run.events.slice(from))).toEqual([]);
		expect(run.host.readFile(HOSTED_PATHS.receiptPath)).toBe(receiptBefore);
		expect(run.reporter.reports.length).toBe(reportsBefore);
	});

	it("an announcement from a digest this host does not run is refused", async () => {
		const run = await readyForUpdate();
		const foreign = postizCompanionSchema.parse({
			...withAppDigest(run.entry, NEWER_APP_DIGEST),
			update: { fromDigest: FOREIGN_APP_DIGEST, toDigest: NEWER_APP_DIGEST },
		});
		const receiptBefore = run.host.readFile(HOSTED_PATHS.receiptPath);
		const reportsBefore = run.reporter.reports.length;
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, foreign));

		expect(outcome).toEqual({
			outcome: "unrequested",
			reason: `the control plane did not announce a change from ${run.entry.artifact.sha256} to ${NEWER_APP_DIGEST}`,
		});
		expect(mutatingEvents(run.events.slice(from))).toEqual([]);
		expect(run.host.readFile(HOSTED_PATHS.receiptPath)).toBe(receiptBefore);
		expect(run.reporter.reports.length).toBe(reportsBefore);
	});

	it("an announcement toward a digest other than the pin is refused", async () => {
		const run = await readyForUpdate();
		const elsewhere = postizCompanionSchema.parse({
			...withAppDigest(run.entry, NEWER_APP_DIGEST),
			update: { fromDigest: run.entry.artifact.sha256, toDigest: FOREIGN_APP_DIGEST },
		});
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, elsewhere));

		expect(outcome).toMatchObject({ outcome: "unrequested" });
		expect(mutatingEvents(run.events.slice(from))).toEqual([]);
	});

	it("a dependency-only digest change applies without an announcement", async () => {
		const run = await readyForUpdate();
		const dependencyOnly = withPostgresDigest(run.entry, NEWER_POSTGRES_DIGEST);
		expect(dependencyOnly.update).toBeNull();
		const newerPostgres = reference(
			dependencyOnly.dependencies.postgres.image,
			NEWER_POSTGRES_DIGEST,
		);
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, dependencyOnly));

		expect(outcome).toMatchObject({ outcome: "ready", step: "ready" });
		expectInOrder(run.events.slice(from), [
			"pgbackrest:backup --type=diff",
			`run:podman pull ${newerPostgres}`,
			"report:ready",
		]);
		const receipt = receiptOf(run);
		expect(receipt.images.postgres).toBe(newerPostgres);
		expect(receipt.images.app).toBe(reference(run.entry.artifact.image, run.entry.artifact.sha256));
		expect(receipt.bootstrap?.phase).toBe("ready");
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-19: the credential-stripped Postiz database dump and its upload for a tenant export.
// ---------------------------------------------------------------------------------------------

const EXPORT_JOB_ID = "0b6f2d1e-4c3a-4e8b-9a7d-5f1e2c3b4a60";
const SECOND_EXPORT_JOB_ID = "7e1d2c3b-4a59-4f68-8e7d-6c5b4a392817";
const CONTROL_PLANE = "https://cloud-api.example.test";
const RECEIPT_SCHEMA = "iforge.containerCompanionApplied.v1";

/** The public tables the fake database holds, with their columns in ordinal order. */
const DUMP_TABLES: Record<string, string[]> = {
	Organization: ["id", "name", "apiKey", "createdAt"],
	User: ["id", "email", "password"],
	Post: ["id", "organizationId", "content"],
};

interface StreamCall {
	command: string;
	args: string[];
	input?: string;
}

/**
 * The streaming seam's fake: answers `pg_dump` with a schema, the column listing with JSON, and
 * each `COPY (SELECT …) TO STDOUT` with rows -- carrying the API key ONLY when the query selects
 * the `apiKey` column itself, exactly as the database would.
 */
class FakeDumpStream implements ContainerStreamHost {
	readonly calls: StreamCall[] = [];
	failDump = false;

	async stream(
		command: string,
		args: readonly string[],
		options: { input?: string; onStdout: (chunk: Uint8Array) => Promise<void> | void },
	): Promise<{ status: number; stderr: string }> {
		const argv = [...args];
		this.calls.push({
			command,
			args: argv,
			...(options.input === undefined ? {} : { input: options.input }),
		});
		const emit = async (text: string) => {
			// Two chunks, so the dump is proven to be streamed rather than taken whole.
			const bytes = new TextEncoder().encode(text);
			const middle = Math.floor(bytes.length / 2);
			await options.onStdout(bytes.slice(0, middle));
			await options.onStdout(bytes.slice(middle));
		};
		if (argv.includes("pg_dump")) {
			if (this.failDump) return { status: 1, stderr: "pg_dump: connection refused" };
			await emit(
				Object.keys(DUMP_TABLES)
					.map((table) => `CREATE TABLE public."${table}" ();\n`)
					.join(""),
			);
			return { status: 0, stderr: "" };
		}
		const script = options.input ?? "";
		if (argv.includes("psql") && script.includes("information_schema.columns")) {
			const pairs = Object.entries(DUMP_TABLES).flatMap(([table, columns]) =>
				columns.map((column) => [table, column]),
			);
			await emit(`${JSON.stringify(pairs)}\n`);
			return { status: 0, stderr: "" };
		}
		const copied = /FROM "public"\."([^"]+)"\) TO STDOUT/.exec(script);
		if (argv.includes("psql") && copied) {
			const table = copied[1] ?? "";
			if (table === "Organization") {
				const selectsKey = script.replace('NULL AS "apiKey"', "").includes('"apiKey"');
				await emit(`org-1\tAcme\t${selectsKey ? API_KEY_TOKEN : "\\N"}\t2026-09-23 12:00:00+00\n`);
			} else {
				await emit(`${table.toLowerCase()}-1\tvalue\tmore\n`);
			}
			return { status: 0, stderr: "" };
		}
		return { status: 127, stderr: `unexpected command ${command} ${argv.join(" ")}` };
	}
}

interface FakeUpload {
	url: string;
	method: string;
	headers: Record<string, string>;
	sql: string;
}

/** A control plane that records each upload, gunzipped, and answers from a script. */
function fakeExportControlPlane(answers: Array<number | "network-error"> = []) {
	const uploads: FakeUpload[] = [];
	let attempts = 0;
	const fetchImpl = async (input: string, init?: RequestInit): Promise<Response> => {
		attempts += 1;
		const answer = answers.shift() ?? 200;
		const raw = init?.body ? new Uint8Array(await new Response(init.body).arrayBuffer()) : null;
		if (answer === "network-error") throw new TypeError("fetch failed: connection reset");
		const headers = Object.fromEntries(new Headers(init?.headers).entries());
		uploads.push({
			url: input,
			method: init?.method ?? "GET",
			headers,
			sql: raw ? gunzipSync(raw).toString("utf8") : "",
		});
		return new Response(JSON.stringify({ state: "uploaded" }), { status: answer });
	};
	return {
		uploads,
		fetch: fetchImpl,
		attempts: () => attempts,
	};
}

function exportEntry(jobIds: string[] = [EXPORT_JOB_ID]): PostizCompanionManifest {
	return postizCompanionSchema.parse({
		...fixtureEntry(),
		exportRequests: jobIds.map((jobId) => ({ jobId })),
	});
}

/** A host whose receipt says the installation is applied and its bootstrap reported ready. */
function readyExportHost(
	entry: PostizCompanionManifest,
	extra: Record<string, unknown> = {},
): FakeContainerHost {
	const host = new FakeContainerHost();
	host.writeFile(
		HOSTED_PATHS.receiptPath,
		`${JSON.stringify({
			schema: RECEIPT_SCHEMA,
			kind: "postiz",
			state: "applied",
			installationId: entry.installationId,
			images: {},
			sourcesSha256: "s",
			credentialsSha256: "c",
			appliedAt: "2026-09-23T12:00:00.000Z",
			dataRoot: "/var/lib/iforge-postiz",
			dataRetained: true,
			bootstrap: {
				receipt: "r",
				phase: "ready",
				step: "ready",
				reportedAt: "2026-09-23T12:00:00.000Z",
			},
			...extra,
		})}\n`,
		{ mode: 0o600 },
	);
	return host;
}

const exportRoots: string[] = [];
afterEach(() => {
	for (const root of exportRoots.splice(0)) rmSync(root, { recursive: true, force: true });
});

function exportRoot(): string {
	const root = mkdtempSync(join(tmpdir(), "iforge-postiz-export-"));
	exportRoots.push(root);
	return root;
}

async function runExport(options: {
	entry?: PostizCompanionManifest;
	host?: FakeContainerHost;
	stream?: FakeDumpStream;
	answers?: Array<number | "network-error">;
	sleeps?: number[];
	root?: string;
}) {
	const entry = options.entry ?? exportEntry();
	const host = options.host ?? readyExportHost(entry);
	const stream = options.stream ?? new FakeDumpStream();
	const plane = fakeExportControlPlane(options.answers);
	const sleeps = options.sleeps ?? [];
	const root = options.root ?? exportRoot();
	const outcome = await deliverPostizExportRequests({
		manifest: manifestWith({ postiz: entry }),
		secretValues: bootstrapSecrets(entry),
		paths: RUNTIME_PATHS,
		host,
		stream,
		fetch: plane.fetch,
		sleep: async (ms: number) => {
			sleeps.push(ms);
		},
		exportRoot: root,
	});
	return { outcome, host, stream, plane, sleeps, root, entry };
}

describe("postiz export: the runtime dumps Postiz without its API key and uploads it", () => {
	it("dumps the schema, one COPY per public table, gzips and PUTs it to the job's upload path", async () => {
		const run = await runExport({});

		expect(run.outcome).toMatchObject({ outcome: "delivered", delivered: [EXPORT_JOB_ID] });
		expect(run.plane.uploads).toHaveLength(1);
		const [upload] = run.plane.uploads;
		expect(upload?.method).toBe("PUT");
		expect(upload?.url).toBe(
			`${CONTROL_PLANE}/v1/runtime/companion-installations/${run.entry.installationId}/exports/${EXPORT_JOB_ID}`,
		);
		expect(upload?.headers.authorization).toBe(`Bearer ${RUNTIME_TOKEN}`);
		expect(upload?.headers["content-type"]).toBe("application/gzip");

		const sql = upload?.sql ?? "";
		expect(sql.startsWith("SET session_replication_role = replica;\n")).toBe(true);
		const dump = run.stream.calls.find((call) => call.args.includes("pg_dump"));
		expect(dump?.command).toBe("podman");
		expect(dump?.args).toEqual([
			"exec",
			"-i",
			"iforge-postiz-db",
			"pg_dump",
			"-U",
			"postiz",
			"-d",
			"postiz",
			"--schema=public",
			"--schema-only",
			"--no-owner",
			"--no-privileges",
		]);
		for (const table of Object.keys(DUMP_TABLES)) {
			expect(sql).toContain(`CREATE TABLE public."${table}" ();`);
			const columns = (DUMP_TABLES[table] ?? []).map((column) => `"${column}"`).join(", ");
			expect(sql).toContain(`COPY "public"."${table}" (${columns}) FROM stdin;\n`);
		}
		expect(sql.match(/\n\\\.\n/g)).toHaveLength(Object.keys(DUMP_TABLES).length);
		// The spool file is gone once the upload is recorded.
		expect(readdirSync(run.root)).toEqual([]);
	});

	it('selects NULL AS "apiKey" for Organization, so the dump carries no API key value', async () => {
		const run = await runExport({});

		const organizationQuery = run.stream.calls
			.map((call) => call.input ?? "")
			.find((script) => script.includes('FROM "public"."Organization"'));
		expect(organizationQuery).toContain('NULL AS "apiKey"');
		const sql = run.plane.uploads[0]?.sql ?? "";
		expect(sql).toContain("org-1\tAcme\t\\N\t");
		expect(sql).not.toContain(API_KEY_TOKEN);
		// No secret reaches a command line or a psql script either.
		for (const call of run.stream.calls) {
			for (const secret of [API_KEY_TOKEN, RUNTIME_TOKEN, DB_PASSWORD_TOKEN]) {
				expect(call.args.join(" ")).not.toContain(secret);
				expect(call.input ?? "").not.toContain(secret);
			}
		}
	});

	it("never queries the iforge_rehearsal schema", async () => {
		const run = await runExport({});

		expect(run.stream.calls.length).toBeGreaterThan(Object.keys(DUMP_TABLES).length);
		for (const call of run.stream.calls) {
			expect(call.args.join(" ")).not.toContain("iforge_rehearsal");
			expect(call.input ?? "").not.toContain("iforge_rehearsal");
		}
		const listing = run.stream.calls.find((call) =>
			(call.input ?? "").includes("information_schema.columns"),
		);
		expect(listing?.input).toContain("table_schema = 'public'");
		expect(run.plane.uploads[0]?.sql ?? "").not.toContain("iforge_rehearsal");
	});

	it("treats an upload answered 409 as done and records it", async () => {
		const run = await runExport({ answers: [409] });

		expect(run.outcome).toMatchObject({ outcome: "delivered", delivered: [EXPORT_JOB_ID] });
		expect(run.plane.attempts()).toBe(1);
		const receipt = JSON.parse(run.host.readFile(HOSTED_PATHS.receiptPath) ?? "{}");
		expect(receipt.deliveredExports).toContain(EXPORT_JOB_ID);
	});

	it("retries a transport failure with bounded backoff, then reports it", async () => {
		const retried = await runExport({ answers: ["network-error", 503, 200] });
		expect(retried.outcome).toMatchObject({ outcome: "delivered", delivered: [EXPORT_JOB_ID] });
		expect(retried.plane.attempts()).toBe(3);
		expect(retried.sleeps).toEqual([1_000, 2_000]);

		const failing = await runExport({
			answers: Array.from({ length: 10 }, () => "network-error" as const),
		});
		expect(failing.outcome).toMatchObject({ outcome: "delivered", delivered: [] });
		const [failure] = failing.outcome.outcome === "delivered" ? failing.outcome.failed : [];
		expect(failure?.jobId).toBe(EXPORT_JOB_ID);
		expect(failure?.error).toContain("5 attempts");
		expect(failing.plane.attempts()).toBe(5);
		expect(Math.max(...failing.sleeps)).toBeLessThanOrEqual(30_000);
		// Not recorded: the next pass tries again.
		const receipt = JSON.parse(failing.host.readFile(HOSTED_PATHS.receiptPath) ?? "{}");
		expect(receipt.deliveredExports ?? []).not.toContain(EXPORT_JOB_ID);
		expect(readdirSync(failing.root)).toEqual([]);
	});

	it("does not re-dump a job already uploaded in this runtime's receipt", async () => {
		const entry = exportEntry([EXPORT_JOB_ID, SECOND_EXPORT_JOB_ID]);
		const host = readyExportHost(entry, { deliveredExports: [EXPORT_JOB_ID] });

		const run = await runExport({ entry, host });

		expect(run.outcome).toMatchObject({ outcome: "delivered", delivered: [SECOND_EXPORT_JOB_ID] });
		expect(run.plane.uploads.map((upload) => upload.url)).toEqual([
			`${CONTROL_PLANE}/v1/runtime/companion-installations/${entry.installationId}/exports/${SECOND_EXPORT_JOB_ID}`,
		]);
		expect(run.stream.calls.filter((call) => call.args.includes("pg_dump"))).toHaveLength(1);
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-25: a regenerated database password reaches the running database.
//
// `POSTGRES_PASSWORD` only matters when PostgreSQL initialises an EMPTY data directory, so a
// password the control plane regenerated would otherwise leave Postiz's new `DATABASE_URL`
// pointing at a role that still has the old one. The runtime records the SHA-256 of the password
// it last applied and, when the composed one differs, sets it with `ALTER ROLE` -- bound as a psql
// variable read from standard input, never on a command line -- before restarting the app.
// ---------------------------------------------------------------------------------------------

describe("postiz database password reconciliation (plan 06-25)", () => {
	const REGENERATED_PASSWORD = "db-password-regenerated-51f0c3e9";
	const sha256Hex = (value: string) => createHash("sha256").update(value).digest("hex");

	function withDatabasePassword(
		entry: PostizCompanionManifest,
		password: string,
	): Record<string, string> {
		const secrets = fixtureSecrets(entry);
		for (const ref of [entry.credentials.appEnvSecretRef, entry.credentials.dbEnvSecretRef]) {
			secrets[ref] = (secrets[ref] ?? "").split(DB_PASSWORD_TOKEN).join(password);
		}
		return secrets;
	}

	function recordingHost(scripts: string[]): FakeContainerHost {
		return new FakeContainerHost({
			psql: (script) => {
				scripts.push(script);
				return { status: 0, stdout: "", stderr: "" };
			},
		});
	}

	function receiptOf(host: FakeContainerHost): Record<string, unknown> {
		return JSON.parse(host.readFile(HOSTED_PATHS.receiptPath) ?? "{}");
	}

	it("alters the role through standard input before restarting the app, and records the new hash", () => {
		const scripts: string[] = [];
		const host = recordingHost(scripts);
		const entry = fixtureEntry();
		applyFresh(host, entry);
		expect(receiptOf(host).databasePasswordSha256).toBe(sha256Hex(DB_PASSWORD_TOKEN));
		const eventsBefore = host.events.length;

		const plan = planFor(host, entry, withDatabasePassword(entry, REGENERATED_PASSWORD));
		expect(plan.kind).toBe("install");
		applyContainerCompanion(plan, host, HOSTED_PATHS);

		const alters = scripts.filter((script) => script.includes("ALTER ROLE"));
		expect(alters).toHaveLength(1);
		expect(alters[0]).toContain(`\\set pw '${REGENERATED_PASSWORD}'`);
		expect(alters[0]).toContain(`ALTER ROLE postiz WITH PASSWORD :'pw';`);
		const later = host.events.slice(eventsBefore);
		const alterAt = later.findIndex((event) =>
			/^run:podman exec -i iforge-postiz-db psql/.test(event),
		);
		const restartAt = later.indexOf("run:systemctl restart iforge-postiz.service");
		expect(alterAt).toBeGreaterThanOrEqual(0);
		expect(restartAt).toBeGreaterThan(alterAt);
		expect(receiptOf(host).databasePasswordSha256).toBe(sha256Hex(REGENERATED_PASSWORD));
	});

	it("runs no ALTER ROLE when the database password did not change", () => {
		const scripts: string[] = [];
		const host = recordingHost(scripts);
		const entry = fixtureEntry();
		applyFresh(host, entry);
		const rotated = fixtureSecrets(entry);
		rotated[entry.credentials.appEnvSecretRef] =
			`${rotated[entry.credentials.appEnvSecretRef]}API_LIMIT=91\n`;

		const plan = planFor(host, entry, rotated);
		expect(plan.kind).toBe("install");
		applyContainerCompanion(plan, host, HOSTED_PATHS);

		expect(scripts.filter((script) => script.includes("ALTER ROLE"))).toEqual([]);
		expect(receiptOf(host).databasePasswordSha256).toBe(sha256Hex(DB_PASSWORD_TOKEN));
	});

	it("never puts the regenerated password on any command line", () => {
		const scripts: string[] = [];
		const host = recordingHost(scripts);
		const entry = fixtureEntry();
		applyFresh(host, entry);

		applyContainerCompanion(
			planFor(host, entry, withDatabasePassword(entry, REGENERATED_PASSWORD)),
			host,
			HOSTED_PATHS,
		);

		expect(scripts.some((script) => script.includes(REGENERATED_PASSWORD))).toBe(true);
		for (const call of host.commands) {
			for (const arg of [call.command, ...call.args]) {
				expect(arg).not.toContain(REGENERATED_PASSWORD);
			}
		}
	});

	// The database's WAL archiving runs inside the RUNNING database with the stanza on its command
	// line and the backup env it started with. After the control plane abandons a repository (a
	// lost cipher pass) or re-mints the backup key, the database must restart onto the new
	// settings -- `start` on a running unit would leave it archiving to the old repository with a
	// revoked key.
	const DB_RESTART = "run:systemctl restart iforge-postiz-db.service";

	it("restarts the database before the app when its backup settings changed", () => {
		const run = installedBackupPostiz();
		// A running companion's database is populated, so no restore probe runs on a re-apply.
		run.host.files.set(PGDATA_VERSION_FILE, { content: "18\n", mode: 0o600, uid: 0, gid: 0 });
		const eventsBefore = run.host.events.length;
		const entry = postizCompanionSchema.parse({
			...run.entry,
			backup: { ...run.entry.backup, stanza: `${BACKUP_STANZA}-r1` },
		});
		const secrets = {
			...run.secrets,
			[backupRef(entry)]: backupEnv()
				.replace(`PGBACKREST_STANZA=${BACKUP_STANZA}`, `PGBACKREST_STANZA=${BACKUP_STANZA}-r1`)
				.replace(BACKUP_CIPHER_PASS, "cipher-pass-regenerated-after-abandon"),
		};

		applyContainerCompanion(planFor(run.host, entry, secrets), run.host, HOSTED_PATHS);

		const later = run.host.events.slice(eventsBefore);
		const dbRestartAt = later.indexOf(DB_RESTART);
		expect(dbRestartAt).toBeGreaterThanOrEqual(0);
		expect(later.indexOf(APP_RESTART)).toBeGreaterThan(dbRestartAt);
	});

	it("restarts the database when only its backup key was re-minted", () => {
		const run = installedBackupPostiz();
		run.host.files.set(PGDATA_VERSION_FILE, { content: "18\n", mode: 0o600, uid: 0, gid: 0 });
		const eventsBefore = run.host.events.length;
		const secrets = {
			...run.secrets,
			[backupRef(run.entry)]: backupEnv().replace(BACKUP_KEY_SECRET, "K005-reminted-key-7a1c"),
		};

		applyContainerCompanion(planFor(run.host, run.entry, secrets), run.host, HOSTED_PATHS);

		expect(run.host.events.slice(eventsBefore)).toContain(DB_RESTART);
	});

	it("leaves the database running when only the app's credentials changed", () => {
		const run = installedBackupPostiz();
		const eventsBefore = run.host.events.length;
		const secrets = { ...run.secrets };
		secrets[run.entry.credentials.appEnvSecretRef] =
			`${secrets[run.entry.credentials.appEnvSecretRef]}API_LIMIT=91\n`;

		applyContainerCompanion(planFor(run.host, run.entry, secrets), run.host, HOSTED_PATHS);

		const later = run.host.events.slice(eventsBefore);
		expect(later).not.toContain(DB_RESTART);
		expect(later).toContain(APP_RESTART);
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-37 (CR-04): one container companion's failure never costs the agent its converge, and
// an install that cannot be applied fails by name.
// ---------------------------------------------------------------------------------------------

const APPLY_FAILURE_PATH = "/var/lib/iforge/companions/postiz/apply-failure.json";
// A real file: since upstream #1197 the daemon unit's content digest reads the token file.
const DAEMON_TOKEN_FILE = (() => {
	const path = join(mkdtempSync(join(tmpdir(), "iforge-daemon-token-")), "daemon-auth-token");
	writeFileSync(path, "daemon-token\n", { mode: 0o600 });
	return path;
})();

type ApplyFailureReportFn = (input: {
	manifest: RuntimeManifest;
	secretValues: Record<string, string> | undefined;
	paths: Pick<RuntimePaths, "serviceStateRoot" | "configurationRoot" | "runRoot">;
	host?: ContainerHost;
	reporter?: CompanionReportSink;
}) => Promise<{ outcome: string; status?: number; reason?: string; message?: string }>;

/** Looked up by name, so a missing export fails the test that needs it, not the whole file. */
function applyFailureApi(): ApplyFailureReportFn {
	const module = containerCompanionModule as Record<string, unknown>;
	expect(typeof module.reportContainerCompanionApplyFailure).toBe("function");
	return module.reportContainerCompanionApplyFailure as ApplyFailureReportFn;
}

function reportApplyFailureOver(
	host: FakeContainerHost,
	manifest: RuntimeManifest,
	reporter: CompanionReportSink,
) {
	return applyFailureApi()({
		manifest,
		secretValues: fixtureSecrets(fixtureEntry()),
		paths: RUNTIME_PATHS,
		host,
		reporter,
	});
}

/**
 * A host whose registry refuses every pull while `pullsFail` is set, with an error that echoes a
 * composed secret -- as a misbehaving registry or proxy can -- so redaction is tested on a real
 * leak rather than on text that never carried one.
 */
class FailingPullHost extends FakeContainerHost {
	pullsFail = true;

	override run(
		command: string,
		args: readonly string[],
		options?: { input?: string },
	): ContainerHostCommandResult {
		if (this.pullsFail && command === "podman" && args[0] === "pull") {
			this.commands.push({ command, args: [...args] });
			this.events.push(`run:${command} ${args.join(" ")}`);
			return {
				status: 125,
				stdout: "",
				stderr: `registry refused the pull: credential ${APP_SECRET_TOKEN} rejected`,
			};
		}
		return super.run(command, args, options);
	}
}

/**
 * `convergePaths` with the run root moved into the temporary directory too, so the ordinary unit
 * writer can write the watch and daemon units (and their environment files) for real. The
 * container kind's state root is unaffected: it derives from `serviceStateRoot` alone.
 */
function unitWritingPaths(): RuntimePaths {
	const paths = convergePaths();
	const runRoot = join(dirname(paths.systemdSystemRoot), "run");
	mkdirSync(runRoot, { recursive: true });
	mkdirSync(paths.systemdSystemRoot, { recursive: true });
	return {
		...paths,
		runRoot,
		systemdRuntimeRoot: join(runRoot, "systemd"),
		systemdEnvRoot: join(runRoot, "systemd", "env"),
	};
}

function applyFailureRecord(host: FakeContainerHost): Record<string, unknown> | null {
	const raw = host.readFile(APPLY_FAILURE_PATH);
	return raw === null ? null : (JSON.parse(raw) as Record<string, unknown>);
}

describe("the container kind is isolated from the agent's converge (CR-04)", () => {
	it("a failing container apply leaves the rest of the converge intact and reports failed at step apply", async () => {
		const paths = unitWritingPaths();
		const withoutCompanion = converge(new FakeContainerHost(), manifestWith(), paths, {
			daemonAuthTokenFile: DAEMON_TOKEN_FILE,
		});
		expect(withoutCompanion.systemUnits?.length ?? 0).toBeGreaterThan(0);

		const host = new FailingPullHost();
		const manifest = manifestWith({ postiz: fixtureEntry() });
		let result: ReturnType<typeof converge> | null = null;
		expect(() => {
			result = converge(host, manifest, paths, { daemonAuthTokenFile: DAEMON_TOKEN_FILE });
		}).not.toThrow();
		const converged = result as ReturnType<typeof converge> | null;
		// Every non-container unit is written exactly as with no companions entry at all.
		expect(converged?.systemUnits).toEqual(withoutCompanion.systemUnits);
		// Nothing was applied before, so nothing of the kind is kept running.
		expect(converged?.containerCompanionServices ?? []).toEqual([]);

		// The failure is recorded root-only, already redacted.
		const record = host.files.get(APPLY_FAILURE_PATH);
		expect(record?.mode).toBe(0o600);
		expect(record?.uid).toBe(0);
		for (const token of SECRET_TOKENS) expect(record?.content ?? "").not.toContain(token);

		const reporter = new RecordingReporter([]);
		const first = await reportApplyFailureOver(host, manifest, reporter);
		expect(first.outcome).toBe("reported");
		expect(reporter.reports).toHaveLength(1);
		const report = reporter.reports[0] as CompanionInstallReport & { error?: string };
		expect(report).toMatchObject({ operation: "install", phase: "failed", step: "apply" });
		expect(report.error).toContain("pull");
		for (const token of SECRET_TOKENS) expect(report.error ?? "").not.toContain(token);

		// Delivered once: neither a second pass nor a later converge that fails again resends it.
		expect((await reportApplyFailureOver(host, manifest, reporter)).outcome).toBe("skipped");
		converge(host, manifest, paths, { daemonAuthTokenFile: DAEMON_TOKEN_FILE });
		expect((await reportApplyFailureOver(host, manifest, reporter)).outcome).toBe("skipped");
		expect(reporter.reports).toHaveLength(1);
	});

	it("a later successful apply clears the recorded failure", async () => {
		const host = new FailingPullHost();
		const manifest = manifestWith({ postiz: fixtureEntry() });
		expect(() => converge(host, manifest)).not.toThrow();
		expect(applyFailureRecord(host)).not.toBeNull();

		host.pullsFail = false;
		const result = converge(host, manifest);
		expect(result.containerCompanionServices).toEqual(GENERATED_SERVICES);
		expect(host.files.has(APPLY_FAILURE_PATH)).toBe(false);

		const reporter = new RecordingReporter([]);
		const outcome = await reportApplyFailureOver(host, manifest, reporter);
		expect(outcome.outcome).toBe("skipped");
		expect(reporter.reports).toEqual([]);
	});
});

/** A report sink whose first `failures` sends throw, as an unreachable control plane does. */
class FlakyReporter implements CompanionReportSink {
	readonly reports: CompanionInstallReport[] = [];

	constructor(private failures: number) {}

	async send(report: CompanionInstallReport): Promise<CompanionReportResult> {
		if (this.failures > 0) {
			this.failures -= 1;
			throw new Error("control plane unreachable");
		}
		this.reports.push(structuredClone(report));
		return { outcome: "recorded", status: 200 };
	}
}

describe("every container companion branch is isolated, and only an install is failed by name (CR-04)", () => {
	it("a withdrawn receipt for another installation fails the new install by name without breaking the converge", async () => {
		const host = new FakeContainerHost();
		const installationA = fixtureEntry();
		converge(host, manifestWith({ postiz: installationA }));
		converge(host, manifestWith({}));
		expect(JSON.parse(host.readFile(RECEIPT_PATH) ?? "{}").state).toBe("withdrawn");

		const installationB: PostizCompanionManifest = {
			...fixtureEntry(),
			installationId: "0b9e4c1a-6d2f-4e8b-a7c3-5f1d2e3a4b6c",
		};
		const manifest = manifestWith({ postiz: installationB });
		let result: ReturnType<typeof converge> | null = null;
		expect(() => {
			result = converge(host, manifest);
		}).not.toThrow();
		expect((result as ReturnType<typeof converge> | null)?.containerCompanionServices).toEqual([]);

		const reporter = new RecordingReporter([]);
		const outcome = await reportApplyFailureOver(host, manifest, reporter);
		expect(outcome.outcome).toBe("reported");
		expect(reporter.reports).toHaveLength(1);
		const report = reporter.reports[0] as CompanionInstallReport & { error?: string };
		expect(report).toMatchObject({ operation: "install", phase: "failed", step: "apply" });
		expect(report.error).toContain(installationA.installationId);
		expect(report.error).toContain("refused");

		// Every later converge of the agent still succeeds, and the failure is not reported again.
		expect(() => converge(host, manifest)).not.toThrow();
		expect((await reportApplyFailureOver(host, manifest, reporter)).outcome).toBe("skipped");
		expect(reporter.reports).toHaveLength(1);
	});

	it("a failure after this installation reported ready is recorded but not reported as an install failure", async () => {
		let probeRefuses = false;
		const host = new FakeContainerHost({
			failCommand: (command, args) =>
				probeRefuses && command === "systemctl" && args[0] === "--version",
		});
		const manifest = manifestWith({ postiz: fixtureEntry() });
		converge(host, manifest);
		const receipt = JSON.parse(host.readFile(RECEIPT_PATH) ?? "{}");
		host.writeFile(
			RECEIPT_PATH,
			`${JSON.stringify({
				...receipt,
				bootstrap: {
					receipt: "reported-apply",
					phase: "ready",
					step: "ready",
					reportedAt: "2026-09-23T12:00:00.000Z",
				},
			})}\n`,
			{ mode: 0o600 },
		);

		probeRefuses = true;
		const result = converge(host, manifest);
		// The running installation's services are still what this converge keeps.
		expect(result.containerCompanionServices).toEqual(GENERATED_SERVICES);
		expect(applyFailureRecord(host)?.lifecycle).toBe("present");

		const reporter = new RecordingReporter([]);
		const outcome = await reportApplyFailureOver(host, manifest, reporter);
		expect(outcome.outcome).toBe("logged-only");
		expect(outcome.message).toContain("systemd");
		expect(reporter.reports).toEqual([]);
	});

	it("an undeliverable failure report is retried by the next converge", async () => {
		const host = new FailingPullHost();
		const manifest = manifestWith({ postiz: fixtureEntry() });
		converge(host, manifest);

		const reporter = new FlakyReporter(1);
		const first = await reportApplyFailureOver(host, manifest, reporter);
		expect(first.outcome).toBe("unreportable");
		expect(first.reason).toContain("unreachable");
		expect(applyFailureRecord(host)?.reportedAt).toBeUndefined();

		// The next converge fails the same way; its report pass delivers the kept failure.
		converge(host, manifest);
		const second = await reportApplyFailureOver(host, manifest, reporter);
		expect(second.outcome).toBe("reported");
		expect(reporter.reports).toHaveLength(1);
		expect(reporter.reports[0]).toMatchObject({ phase: "failed", step: "apply" });
		expect(applyFailureRecord(host)?.reportedAt).toBeString();
	});

	it("a refused remove leaves the converge intact and reports no install failure", async () => {
		const host = new FakeContainerHost();
		converge(host, manifestWith({ postiz: fixtureEntry() }));
		seedData(host);
		host.symlinks.set(DATA_ROOT, "/srv/somewhere-else");
		const manifest = manifestWith({ postiz: removeEntry() });

		expect(() => converge(host, manifest)).not.toThrow();
		const result = reconcileContainerCompanions({
			manifest,
			paths: convergePaths(),
			secretValues: fixtureSecrets(fixtureEntry()),
			host,
		});
		expect(result.failure?.lifecycle).toBe("remove");
		expect(result.failure?.message).toMatch(/refus/i);
		// Nothing was deleted, and the units the refusal left running are still counted.
		expect(host.removedTrees).toEqual([]);
		expect(result.desiredServices).toEqual(GENERATED_SERVICES);

		const reporter = new RecordingReporter([]);
		const outcome = await reportApplyFailureOver(host, manifest, reporter);
		expect(outcome.outcome).toBe("skipped");
		expect(reporter.reports).toEqual([]);
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-39 (CR-02): no non-root local socket reaches a container companion, before or after it
// is bootstrapped.
// ---------------------------------------------------------------------------------------------

type GuardApi = {
	subnet: (uid: number) => string;
	table: (kind: "postiz") => string;
};

/** Looked up by name, so a missing export fails the test that needs it, not the whole file. */
function guardApi(): GuardApi {
	const module = containerCompanionModule as Record<string, unknown>;
	expect(typeof module.containerCompanionSubnet).toBe("function");
	expect(typeof module.containerCompanionGuardTable).toBe("function");
	return {
		subnet: module.containerCompanionSubnet as GuardApi["subnet"],
		table: module.containerCompanionGuardTable as GuardApi["table"],
	};
}

const NETWORK_INSPECT =
	"run:podman network inspect iforge-postiz --format {{range .Subnets}}{{.Subnet}} {{end}}";

describe("the loopback and network guard (plan 06-39, CR-02)", () => {
	it("the loopback and network guard is loaded before any postiz container can start", () => {
		const host = new FakeContainerHost();
		applyFresh(host);
		expectInOrder(host.events, [
			`write:${GUARD_PATH}`,
			"run:systemctl daemon-reload",
			"run:systemctl start iforge-postiz-network.service",
			NETWORK_INSPECT,
			`run:/usr/sbin/nft -f ${GUARD_PATH}`,
			"run:systemctl start iforge-postiz.service",
		]);
		// Nothing of the kind starts before the guard is loaded.
		const load = host.events.indexOf(`run:/usr/sbin/nft -f ${GUARD_PATH}`);
		const firstContainerStart = host.events.findIndex(
			(event) =>
				/^run:systemctl (start|restart) /.test(event) &&
				!event.endsWith("iforge-postiz-network.service"),
		);
		expect(firstContainerStart).toBeGreaterThan(load);
		expect(host.nftTables.has(GUARD_TABLE)).toBe(true);

		const guard = host.files.get(GUARD_PATH);
		expect(guard?.mode).toBe(0o600);
		expect(guard?.uid).toBe(0);
		const ruleset = guard?.content ?? "";
		expect(ruleset).toContain("meta skuid != 0 ip daddr 127.0.0.1 tcp dport 9130 reject");
		expect(ruleset).toContain(`meta skuid != 0 ip daddr ${POSTIZ_SUBNET} reject`);
		expect(ruleset.match(/reject/g)).toHaveLength(2);
		// One transaction that replaces the table whether or not it exists.
		expect(ruleset).toContain(`add table inet ${GUARD_TABLE}`);
		expect(ruleset).toContain(`delete table inet ${GUARD_TABLE}`);
		expect(ruleset.indexOf(`add table inet ${GUARD_TABLE}`)).toBeLessThan(
			ruleset.indexOf(`delete table inet ${GUARD_TABLE}`),
		);
		expect(ruleset).toContain("type filter hook output priority 0; policy accept;");
		// No non-root UID is exempted: the only uid the ruleset names is root's.
		expect([...ruleset.matchAll(/skuid\s*(?:!=|==)?\s*(\d+)/g)].map((m) => m[1])).toEqual([
			"0",
			"0",
		]);
		// The guard is not a Quadlet source: the four sources stay four.
		expect(host.sources().size).toBe(4);
	});

	it("every postiz container source installs the guard before it starts", () => {
		const entry = tunnelEntry();
		const plan = planFor(new FakeContainerHost(), entry, tunnelSecrets(entry));
		const containers = plan.sources.filter((source) => source.path.endsWith(".container"));
		expect(containers).toHaveLength(4);
		for (const source of containers) {
			const service = source.content.split("\n[Service]\n")[1]?.split("\n[")[0] ?? "";
			const lines = service.split("\n");
			const pre = lines.indexOf(`ExecStartPre=/usr/sbin/nft -f ${GUARD_PATH}`);
			expect(pre).toBeGreaterThanOrEqual(0);
			const credential = lines.findIndex((line) => line.startsWith("LoadCredentialEncrypted="));
			if (credential >= 0) expect(pre).toBeLessThan(credential);
		}
		const network = plan.sources.find((source) => source.path.endsWith(".network"))?.content;
		expect(network).toContain(`Subnet=${POSTIZ_SUBNET}`);
		expect(network).not.toContain("ExecStartPre=");
	});

	it("pins the subnet from the registry uid and refuses a uid outside the reserved block", () => {
		const { subnet, table } = guardApi();
		expect(subnet(10101)).toBe(POSTIZ_SUBNET);
		expect(subnet(10100)).toBe("10.213.0.0/24");
		expect(subnet(10199)).toBe("10.213.99.0/24");
		for (const uid of [10099, 10200, 1000, 10101.5]) expect(() => subnet(uid)).toThrow(/reserved/);
		expect(table("postiz")).toBe(GUARD_TABLE);
	});

	it("an applied host whose guard file went missing is re-applied, not left unguarded", () => {
		const host = new FakeContainerHost();
		applyFresh(host);
		expect(planFor(host).kind).toBe("unchanged");
		host.files.delete(GUARD_PATH);
		expect(planFor(host).kind).toBe("install");
	});
});

describe("the guard's refusals and removal (plan 06-39, CR-02)", () => {
	it("a host without nftables is refused before anything is written", () => {
		const host = new FakeContainerHost({ nft: false });
		let message = "";
		try {
			applyFresh(host);
		} catch (error) {
			message = error instanceof Error ? error.message : String(error);
		}
		expect(message).toContain("nftables");
		expect(message).toContain("/usr/sbin/nft");
		expect(writesOf(host)).toEqual([]);
		expect(host.identities).toEqual([]);
		expect(host.sources().size).toBe(0);
		expect(host.commands.some((call) => call.command === "podman")).toBe(false);
	});

	it("a companion network that keeps another subnet refuses the apply before the app starts", () => {
		// Plan 06-49: the apply migrates a network with another subnet; this one comes back wrong.
		const host = new FakeContainerHost({
			networkSubnet: "10.89.0.0/24",
			recreatedSubnet: "10.89.0.0/24",
		});
		let message = "";
		try {
			applyFresh(host);
		} catch (error) {
			message = error instanceof Error ? error.message : String(error);
		}
		expect(message).toContain("network iforge-postiz");
		expect(message).toContain("10.89.0.0/24");
		expect(message).toContain(POSTIZ_SUBNET);
		expect(host.events).not.toContain("run:systemctl start iforge-postiz.service");
		expect(host.events).not.toContain(`run:/usr/sbin/nft -f ${GUARD_PATH}`);
		// The refused apply leaves nothing behind, and no receipt, so the next converge repeats it.
		expect(host.sources().size).toBe(0);
		expect(host.files.has(GUARD_PATH)).toBe(false);
		expect(host.files.has(RECEIPT_PATH)).toBe(false);
	});

	it("remove deletes the guard table and file; withdraw keeps them", () => {
		const { remove } = removalApi();
		const removed = new FakeContainerHost();
		applyFresh(removed);
		seedData(removed);
		expect(removed.nftTables.has(GUARD_TABLE)).toBe(true);
		const eventsBefore = removed.events.length;
		remove("postiz", removed, HOSTED_PATHS);
		const later = removed.events.slice(eventsBefore);
		const rmtree = later.indexOf(`rmtree:${DATA_ROOT}`);
		const drop = later.indexOf(`run:/usr/sbin/nft delete table inet ${GUARD_TABLE}`);
		expect(rmtree).toBeGreaterThanOrEqual(0);
		expect(drop).toBeGreaterThan(rmtree);
		expect(removed.nftTables.has(GUARD_TABLE)).toBe(false);
		expect(removed.files.has(GUARD_PATH)).toBe(false);
		// Removing again finds no table and deletes nothing.
		const commandsBefore = removed.commands.length;
		remove("postiz", removed, HOSTED_PATHS);
		expect(
			removed.commands
				.slice(commandsBefore)
				.some((call) => call.command === "/usr/sbin/nft" && call.args[0] === "delete"),
		).toBe(false);

		const withdrawn = new FakeContainerHost();
		applyFresh(withdrawn);
		const withdrawCommandsBefore = withdrawn.commands.length;
		withdrawContainerCompanion("postiz", withdrawn, HOSTED_PATHS);
		expect(
			withdrawn.commands
				.slice(withdrawCommandsBefore)
				.some((call) => call.command === "/usr/sbin/nft"),
		).toBe(false);
		expect(withdrawn.files.has(GUARD_PATH)).toBe(true);
		expect(withdrawn.nftTables.has(GUARD_TABLE)).toBe(true);
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-42 (CR-04): an uninstall the tenant confirmed but the host never confirmed leaves a
// receipt for installation A. The next installation's entry names A in `retiredInstallations`, and
// only then does the host remove A's retained data -- through the guarded remove path -- and apply
// the new installation.
// ---------------------------------------------------------------------------------------------

const REINSTALL_ID = "0b9e4c1a-6d2f-4e8b-a7c3-5f1d2e3a4b6c";

/** Installation B's present entry, naming `retired` as retired installations. */
function reinstallEntry(retired: string[]): PostizCompanionManifest {
	return {
		...fixtureEntry(),
		installationId: REINSTALL_ID,
		retiredInstallations: retired.map((installationId) => ({ installationId })),
	} as PostizCompanionManifest;
}

/** A host that ran installation A, then lost its entry: units withdrawn, data kept. */
function hostWithWithdrawnInstallation(): FakeContainerHost {
	const host = new FakeContainerHost();
	converge(host, manifestWith({ postiz: fixtureEntry() }));
	seedData(host);
	converge(host, manifestWith({}));
	expect(JSON.parse(host.readFile(RECEIPT_PATH) ?? "{}").state).toBe("withdrawn");
	return host;
}

function reconcileEntry(host: FakeContainerHost, entry: PostizCompanionManifest) {
	return reconcileContainerCompanions({
		manifest: manifestWith({ postiz: entry }),
		paths: convergePaths(),
		secretValues: fixtureSecrets(fixtureEntry()),
		host,
	});
}

describe("a retired installation's retained data is removed before the reinstall (plan 06-42, CR-04)", () => {
	it("a retired installation's retained data is removed before the reinstall is applied", () => {
		const installationA = fixtureEntry().installationId;
		const host = hostWithWithdrawnInstallation();
		expect(host.files.get(`${DATA_ROOT}/db/PG_VERSION`)?.content).toBe("18\n");

		const result = reconcileEntry(host, reinstallEntry([installationA]));

		expect(result.failure).toBeNull();
		expect(result.retiredRemoved).toBe(installationA);
		// A's data went through the one guarded recursive delete, and only that.
		expect(host.removedTrees).toEqual([DATA_ROOT]);
		expect(host.files.has(`${DATA_ROOT}/db/PG_VERSION`)).toBe(false);
		// B is applied: the receipt names it, and its services are what this converge keeps.
		const receipt = JSON.parse(host.readFile(RECEIPT_PATH) ?? "{}");
		expect(receipt.installationId).toBe(REINSTALL_ID);
		expect(receipt.state).toBe("applied");
		expect(result.desiredServices).toEqual(GENERATED_SERVICES);
		expect(result.applied).not.toBeNull();
	});

	it("an applied receipt for a retired installation is removed too", () => {
		// The host never saw A's entry vanish: A is still applied and running when B arrives.
		const installationA = fixtureEntry().installationId;
		const host = new FakeContainerHost();
		converge(host, manifestWith({ postiz: fixtureEntry() }));
		seedData(host);
		expect(JSON.parse(host.readFile(RECEIPT_PATH) ?? "{}").state).toBe("applied");
		const eventsBefore = host.events.length;

		const result = reconcileEntry(host, reinstallEntry([installationA]));

		expect(result.failure).toBeNull();
		expect(result.retiredRemoved).toBe(installationA);
		// A's units are stopped before its data goes, exactly as on `lifecycle: "remove"`.
		const later = host.events.slice(eventsBefore);
		const stop = later.findIndex(
			(event) => event.startsWith("run:systemctl stop") && event.includes("iforge-postiz.service"),
		);
		const rmtree = later.indexOf(`rmtree:${DATA_ROOT}`);
		expect(stop).toBeGreaterThanOrEqual(0);
		expect(rmtree).toBeGreaterThan(stop);
		expect(host.removedTrees).toEqual([DATA_ROOT]);
		expect(host.files.has(`${DATA_ROOT}/db/PG_VERSION`)).toBe(false);
		const receipt = JSON.parse(host.readFile(RECEIPT_PATH) ?? "{}");
		expect(receipt.installationId).toBe(REINSTALL_ID);
		expect(receipt.state).toBe("applied");
	});

	it("a receipt for an unlisted installation still refuses the install and keeps its data", async () => {
		const installationA = fixtureEntry().installationId;
		const unrelated = "7c3e9a14-2b5d-4f60-8e1a-9d4c3b2a1f07";
		const host = hostWithWithdrawnInstallation();
		const manifest = manifestWith({ postiz: reinstallEntry([unrelated]) });

		expect(() => converge(host, manifest)).not.toThrow();
		const result = reconcileEntry(host, reinstallEntry([unrelated]));

		expect(result.retiredRemoved).toBeNull();
		expect(result.failure?.message).toContain(installationA);
		expect(result.failure?.message).toContain(REINSTALL_ID);
		expect(result.failure?.message).toContain("refused");
		// Pitfall 12: nothing the control plane did not name is deleted.
		expect(host.removedTrees).toEqual([]);
		expect(host.files.get(`${DATA_ROOT}/db/PG_VERSION`)?.content).toBe("18\n");
		const receipt = JSON.parse(host.readFile(RECEIPT_PATH) ?? "{}");
		expect(receipt.installationId).toBe(installationA);
		expect(receipt.state).toBe("withdrawn");

		// Plan 06-37's failed report still names the refusal.
		const reporter = new RecordingReporter([]);
		const outcome = await reportApplyFailureOver(host, manifest, reporter);
		expect(outcome.outcome).toBe("reported");
		const report = reporter.reports[0] as CompanionInstallReport & { error?: string };
		expect(report).toMatchObject({ operation: "install", phase: "failed", step: "apply" });
		expect(report.error).toContain(installationA);
	});

	it("an entry naming its own installation as retired never removes that installation's data", () => {
		const host = new FakeContainerHost();
		converge(host, manifestWith({ postiz: fixtureEntry() }));
		seedData(host);
		const own = {
			...fixtureEntry(),
			retiredInstallations: [{ installationId: fixtureEntry().installationId }],
		} as PostizCompanionManifest;

		const result = reconcileEntry(host, own);

		expect(result.failure).toBeNull();
		expect(result.retiredRemoved).toBeNull();
		expect(host.removedTrees).toEqual([]);
		expect(host.files.get(`${DATA_ROOT}/db/PG_VERSION`)?.content).toBe("18\n");
	});

	it("a listed retired installation on a host with no receipt deletes nothing", () => {
		const host = new FakeContainerHost();
		seedData(host);

		const result = reconcileEntry(host, reinstallEntry([fixtureEntry().installationId]));

		expect(result.failure).toBeNull();
		expect(result.retiredRemoved).toBeNull();
		expect(host.removedTrees).toEqual([]);
		expect(host.files.get(`${DATA_ROOT}/db/PG_VERSION`)?.content).toBe("18\n");
	});

	it("a symlink planted at the retired installation's data directory is refused, not followed", () => {
		const host = hostWithWithdrawnInstallation();
		host.symlinks.set(DATA_ROOT, "/srv/somewhere-else");

		const result = reconcileEntry(host, reinstallEntry([fixtureEntry().installationId]));

		expect(result.retiredRemoved).toBeNull();
		expect(result.failure?.message).toMatch(/refus/i);
		expect(host.removedTrees).toEqual([]);
		expect(host.files.get(`${DATA_ROOT}/db/PG_VERSION`)?.content).toBe("18\n");
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-45 (G-06-2): a host condition raised or cleared reaches the control plane once per state
// ---------------------------------------------------------------------------------------------

const CONDITIONS_PATH = "/var/lib/iforge/companions/postiz/conditions.json";

type HostConditionCode =
	| "update_rollback_incomplete"
	| "schema_digest_mismatch"
	| "network_subnet_unverified";

interface ConditionsReportOutcome {
	outcome: string;
	reason?: string;
	sent?: Array<{ code: string; state: string; result: string }>;
}

interface ConditionsApi {
	read(host: ContainerHost, paths: ContainerCompanionPaths): Array<Record<string, unknown>>;
	raise(
		host: ContainerHost,
		paths: ContainerCompanionPaths,
		input: { installationId: string; code: HostConditionCode; detail: string | null },
	): void;
	clear(
		host: ContainerHost,
		paths: ContainerCompanionPaths,
		input: { installationId: string; code: HostConditionCode },
	): void;
	report(input: {
		manifest: RuntimeManifest;
		secretValues: Record<string, string> | undefined;
		paths: Pick<RuntimePaths, "serviceStateRoot" | "configurationRoot" | "runRoot">;
		host?: ContainerHost;
		reporter?: CompanionReportSink;
	}): Promise<ConditionsReportOutcome>;
}

/** Looked up by name, so a missing export fails the test that needs it, not the whole file. */
function conditionsApi(): ConditionsApi {
	const module = containerCompanionModule as Record<string, unknown>;
	const names = {
		read: "readContainerCompanionConditions",
		raise: "raiseContainerCompanionCondition",
		clear: "clearContainerCompanionCondition",
		report: "reportContainerCompanionConditions",
	} as const;
	for (const name of Object.values(names)) {
		expect(`${name}: ${typeof module[name]}`).toBe(`${name}: function`);
	}
	return {
		read: module[names.read] as ConditionsApi["read"],
		raise: module[names.raise] as ConditionsApi["raise"],
		clear: module[names.clear] as ConditionsApi["clear"],
		report: module[names.report] as ConditionsApi["report"],
	};
}

function reportConditionsOver(
	host: FakeContainerHost,
	manifest: RuntimeManifest,
	reporter: CompanionReportSink | undefined,
	secretValues: Record<string, string> | undefined = fixtureSecrets(fixtureEntry()),
): Promise<ConditionsReportOutcome> {
	return conditionsApi().report({
		manifest,
		secretValues,
		paths: RUNTIME_PATHS,
		host,
		...(reporter ? { reporter } : {}),
	});
}

function conditionsRecord(host: FakeContainerHost): Record<string, unknown> | null {
	const raw = host.readFile(CONDITIONS_PATH);
	return raw === null ? null : (JSON.parse(raw) as Record<string, unknown>);
}

describe("host conditions tracer: a raised condition is reported once, and so is its clearing (plan 06-45)", () => {
	it("a raised host condition is reported once and its clearing once", async () => {
		const api = conditionsApi();
		const entry = fixtureEntry();
		const manifest = manifestWith({ postiz: entry });
		const host = new FakeContainerHost();
		const reporter = new RecordingReporter([]);
		const key = {
			installationId: entry.installationId,
			code: "update_rollback_incomplete",
		} as const;

		api.raise(host, HOSTED_PATHS, { ...key, detail: "restore failed" });

		// Root-only, under the companion's state root.
		const file = host.files.get(CONDITIONS_PATH);
		expect(file?.mode).toBe(0o600);
		expect(file?.uid).toBe(0);
		expect(host.directories.get(HOSTED_PATHS.companionStateRoot)).toEqual({
			mode: 0o700,
			uid: 0,
			gid: 0,
		});
		expect(conditionsRecord(host)).toMatchObject({
			schema: "iforge.containerCompanionConditions.v1",
			kind: "postiz",
		});

		const first = await reportConditionsOver(host, manifest, reporter);
		expect(first).toEqual({
			outcome: "reported",
			sent: [{ code: "update_rollback_incomplete", state: "raised", result: "recorded" }],
		});
		expect(reporter.reports).toEqual([
			{
				operation: "update",
				phase: "condition",
				condition: {
					code: "update_rollback_incomplete",
					state: "raised",
					detail: "restore failed",
				},
			},
		]);
		expect(api.read(host, HOSTED_PATHS)[0]?.reportedAt).toBeString();

		// Reported once: a second pass sends nothing.
		expect((await reportConditionsOver(host, manifest, reporter)).outcome).toBe("skipped");
		expect(reporter.reports).toHaveLength(1);

		api.clear(host, HOSTED_PATHS, key);
		const cleared = await reportConditionsOver(host, manifest, reporter);
		expect(cleared).toEqual({
			outcome: "reported",
			sent: [{ code: "update_rollback_incomplete", state: "cleared", result: "recorded" }],
		});
		expect(reporter.reports[1]).toEqual({
			operation: "update",
			phase: "condition",
			condition: { code: "update_rollback_incomplete", state: "cleared", detail: null },
		});
		// A delivered clear is forgotten.
		expect(api.read(host, HOSTED_PATHS)).toEqual([]);

		expect((await reportConditionsOver(host, manifest, reporter)).outcome).toBe("skipped");
		expect(reporter.reports).toHaveLength(2);
	});

	it("a raised schema_digest_mismatch is sent with operation install", async () => {
		const api = conditionsApi();
		const entry = fixtureEntry();
		const host = new FakeContainerHost();
		const reporter = new RecordingReporter([]);

		api.raise(host, HOSTED_PATHS, {
			installationId: entry.installationId,
			code: "schema_digest_mismatch",
			detail: "schema written by another digest",
		});
		await reportConditionsOver(host, manifestWith({ postiz: entry }), reporter);

		expect(reporter.reports).toEqual([
			{
				operation: "install",
				phase: "condition",
				condition: {
					code: "schema_digest_mismatch",
					state: "raised",
					detail: "schema written by another digest",
				},
			},
		]);
	});
});

const OTHER_INSTALLATION_ID = "0b7e2c4a-1d3f-4e5a-8b6c-9d0e1f2a3b4c";

/** A recording reporter whose `throwAt`-th send (1-based) throws, as an unreachable plane does. */
class ThrowAtReporter extends RecordingReporter {
	private calls = 0;

	constructor(private readonly throwAt: number) {
		super([]);
	}

	override async send(report: CompanionInstallReport): Promise<CompanionReportResult> {
		this.calls += 1;
		if (this.calls === this.throwAt) throw new Error("control plane unreachable");
		return super.send(report);
	}
}

function conditionWrites(host: FakeContainerHost): number {
	return host.events.filter((event) => event === `write:${CONDITIONS_PATH}`).length;
}

function conditionReports(reporter: RecordingReporter): Array<Record<string, unknown>> {
	return reporter.reports.map(
		(report) =>
			(report as CompanionInstallReport & { condition: Record<string, unknown> }).condition,
	);
}

function plantConditions(host: FakeContainerHost, content: string): void {
	host.files.set(CONDITIONS_PATH, { content, mode: 0o600, uid: 0, gid: 0 });
}

describe("host conditions: the record's edges (plan 06-45)", () => {
	const entry = fixtureEntry();
	const rollback = {
		installationId: entry.installationId,
		code: "update_rollback_incomplete",
	} as const;
	const schema = { installationId: entry.installationId, code: "schema_digest_mismatch" } as const;
	const network = {
		installationId: entry.installationId,
		code: "network_subnet_unverified",
	} as const;

	it("raising an entry exactly as it is raised and reported writes nothing and re-sends nothing", async () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		const reporter = new RecordingReporter([]);
		const manifest = manifestWith({ postiz: entry });
		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });
		await reportConditionsOver(host, manifest, reporter);
		const reportedAt = api.read(host, HOSTED_PATHS)[0]?.reportedAt;
		expect(reportedAt).toBeString();
		const writes = conditionWrites(host);

		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });

		expect(conditionWrites(host)).toBe(writes);
		expect(api.read(host, HOSTED_PATHS)[0]?.reportedAt).toBe(reportedAt);
		expect((await reportConditionsOver(host, manifest, reporter)).outcome).toBe("skipped");
		expect(reporter.reports).toHaveLength(1);
	});

	it("raising with a different detail replaces the entry unreported, and it is sent once more", async () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		const reporter = new RecordingReporter([]);
		const manifest = manifestWith({ postiz: entry });
		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });
		await reportConditionsOver(host, manifest, reporter);

		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed again" });

		const [stored] = api.read(host, HOSTED_PATHS);
		expect(stored).toMatchObject({ state: "raised", detail: "restore failed again" });
		expect(stored?.reportedAt).toBeUndefined();
		await reportConditionsOver(host, manifest, reporter);
		await reportConditionsOver(host, manifest, reporter);
		expect(conditionReports(reporter)).toEqual([
			{ code: "update_rollback_incomplete", state: "raised", detail: "restore failed" },
			{ code: "update_rollback_incomplete", state: "raised", detail: "restore failed again" },
		]);
	});

	it("clearing an absent or already-cleared entry writes nothing", () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();

		api.clear(host, HOSTED_PATHS, rollback);
		expect(host.files.has(CONDITIONS_PATH)).toBe(false);
		expect(conditionWrites(host)).toBe(0);

		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });
		api.clear(host, HOSTED_PATHS, rollback);
		const writes = conditionWrites(host);
		const record = host.readFile(CONDITIONS_PATH);
		api.clear(host, HOSTED_PATHS, rollback);

		expect(conditionWrites(host)).toBe(writes);
		expect(host.readFile(CONDITIONS_PATH)).toBe(record);
		expect(api.read(host, HOSTED_PATHS)).toEqual([
			expect.objectContaining({ state: "cleared", detail: null }),
		]);
	});

	it("raising over a cleared, unreported entry raises it again and only the raised state is sent", async () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		const reporter = new RecordingReporter([]);
		const manifest = manifestWith({ postiz: entry });
		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });
		await reportConditionsOver(host, manifest, reporter);
		api.clear(host, HOSTED_PATHS, rollback);

		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });

		expect(api.read(host, HOSTED_PATHS)).toEqual([
			expect.objectContaining({ state: "raised", detail: "restore failed" }),
		]);
		await reportConditionsOver(host, manifest, reporter);
		expect(conditionReports(reporter)).toEqual([
			{ code: "update_rollback_incomplete", state: "raised", detail: "restore failed" },
			{ code: "update_rollback_incomplete", state: "raised", detail: "restore failed" },
		]);
	});

	it("two codes for one installation are kept and reported independently", async () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		const reporter = new RecordingReporter([]);
		const manifest = manifestWith({ postiz: entry });
		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });
		api.raise(host, HOSTED_PATHS, { ...schema, detail: "schema from another digest" });

		const first = await reportConditionsOver(host, manifest, reporter);
		expect(first.sent).toEqual([
			{ code: "update_rollback_incomplete", state: "raised", result: "recorded" },
			{ code: "schema_digest_mismatch", state: "raised", result: "recorded" },
		]);

		api.clear(host, HOSTED_PATHS, schema);
		const second = await reportConditionsOver(host, manifest, reporter);

		expect(second.sent).toEqual([
			{ code: "schema_digest_mismatch", state: "cleared", result: "recorded" },
		]);
		expect(api.read(host, HOSTED_PATHS)).toEqual([
			expect.objectContaining({ code: "update_rollback_incomplete", state: "raised" }),
		]);
		expect(reporter.reports.map((report) => report.operation)).toEqual([
			"update",
			"install",
			"install",
		]);
	});

	it("a refused condition report is marked delivered and never sent again", async () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		const reporter = new ScriptedReporter([], {
			condition: refusedAnswer("companion_not_found", null),
		});
		const manifest = manifestWith({ postiz: entry });
		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });

		const first = await reportConditionsOver(host, manifest, reporter);

		expect(first).toEqual({
			outcome: "reported",
			sent: [{ code: "update_rollback_incomplete", state: "raised", result: "refused" }],
		});
		expect(api.read(host, HOSTED_PATHS)[0]?.reportedAt).toBeString();
		expect((await reportConditionsOver(host, manifest, reporter)).outcome).toBe("skipped");
		expect(reporter.reports).toHaveLength(1);
	});

	it("an already-terminal answer marks the condition delivered", async () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		const reporter = new ScriptedReporter([], {
			condition: { outcome: "already-terminal", status: 409 },
		});
		const manifest = manifestWith({ postiz: entry });
		api.raise(host, HOSTED_PATHS, { ...schema, detail: "schema from another digest" });

		const first = await reportConditionsOver(host, manifest, reporter);

		expect(first.sent).toEqual([
			{ code: "schema_digest_mismatch", state: "raised", result: "already-terminal" },
		]);
		expect(api.read(host, HOSTED_PATHS)[0]?.reportedAt).toBeString();
		expect((await reportConditionsOver(host, manifest, reporter)).outcome).toBe("skipped");
		expect(reporter.reports).toHaveLength(1);
	});

	it("a thrown send leaves that condition and every later one for the next pass, and never resends an earlier one", async () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		const manifest = manifestWith({ postiz: entry });
		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });
		api.raise(host, HOSTED_PATHS, { ...schema, detail: "schema from another digest" });
		api.raise(host, HOSTED_PATHS, { ...network, detail: "subnet unverified" });
		const flaky = new ThrowAtReporter(2);

		const first = await reportConditionsOver(host, manifest, flaky);

		expect(first.outcome).toBe("unreportable");
		expect(first.reason).toContain("unreachable");
		expect(api.read(host, HOSTED_PATHS).map((stored) => stored.reportedAt !== undefined)).toEqual([
			true,
			false,
			false,
		]);

		const reporter = new RecordingReporter([]);
		const second = await reportConditionsOver(host, manifest, reporter);

		expect(second.sent?.map((sent) => sent.code)).toEqual([
			"schema_digest_mismatch",
			"network_subnet_unverified",
		]);
		expect(conditionReports(flaky).map((condition) => condition.code)).toEqual([
			"update_rollback_incomplete",
		]);
	});

	it.each([400, 422])(
		"a condition the control plane rejects with %i is its final answer: marked delivered, and the next condition still goes (06-REVIEW WR-02)",
		async (status) => {
			const api = conditionsApi();
			const host = new FakeContainerHost();
			const manifest = manifestWith({ postiz: entry });
			api.raise(host, HOSTED_PATHS, { ...schema, detail: "schema from another digest" });
			api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });
			const sentCodes: string[] = [];
			const reporter = new CompanionProgressReporter({
				apiUrl: "https://cloud-api.example.test",
				token: "runtime-token",
				installationId: entry.installationId,
				secrets: [],
				fetch: async (_url, init = {}) => {
					const body = JSON.parse(String(init.body)) as { condition: { code: string } };
					sentCodes.push(body.condition.code);
					return body.condition.code === "schema_digest_mismatch"
						? new Response("unknown phase", { status })
						: Response.json({ status: "running", step: null, stored_lines: 0 });
				},
				sleep: async () => {},
			});

			const first = await reportConditionsOver(host, manifest, reporter);

			expect(first).toEqual({
				outcome: "reported",
				sent: [
					{ code: "schema_digest_mismatch", state: "raised", result: "refused" },
					{ code: "update_rollback_incomplete", state: "raised", result: "recorded" },
				],
			});
			expect(api.read(host, HOSTED_PATHS).map((stored) => stored.reportedAt !== undefined)).toEqual(
				[true, true],
			);
			// A refused report is never re-sent.
			expect((await reportConditionsOver(host, manifest, reporter)).outcome).toBe("skipped");
			expect(sentCodes).toEqual(["schema_digest_mismatch", "update_rollback_incomplete"]);
		},
	);

	it("a condition answered 404 stays due, the next condition still goes, and the next pass sends it again (06-REVIEW WR-04)", async () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		const manifest = manifestWith({ postiz: entry });
		api.raise(host, HOSTED_PATHS, { ...schema, detail: "schema from another digest" });
		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });
		// The runtime key is not bound to its agent yet: the control plane answers 404 for it.
		let bound = false;
		const sentCodes: string[] = [];
		const reporter = new CompanionProgressReporter({
			apiUrl: "https://cloud-api.example.test",
			token: "runtime-token",
			installationId: entry.installationId,
			secrets: [],
			fetch: async (_url, init = {}) => {
				const body = JSON.parse(String(init.body)) as { condition: { code: string } };
				sentCodes.push(body.condition.code);
				return body.condition.code === "schema_digest_mismatch" && !bound
					? new Response("Installation not found", { status: 404 })
					: Response.json({ status: "running", step: null, stored_lines: 0 });
			},
			sleep: async () => {},
		});

		const first = await reportConditionsOver(host, manifest, reporter);

		expect(first).toEqual({
			outcome: "reported",
			sent: [
				{ code: "schema_digest_mismatch", state: "raised", result: "kept-due" },
				{ code: "update_rollback_incomplete", state: "raised", result: "recorded" },
			],
		});
		expect(api.read(host, HOSTED_PATHS).map((stored) => stored.reportedAt !== undefined)).toEqual([
			false,
			true,
		]);

		bound = true;
		const second = await reportConditionsOver(host, manifest, reporter);

		expect(second).toEqual({
			outcome: "reported",
			sent: [{ code: "schema_digest_mismatch", state: "raised", result: "recorded" }],
		});
		expect(api.read(host, HOSTED_PATHS).map((stored) => stored.reportedAt !== undefined)).toEqual([
			true,
			true,
		]);
		expect(sentCodes).toEqual([
			"schema_digest_mismatch",
			"update_rollback_incomplete",
			"schema_digest_mismatch",
		]);
	});

	it("a condition answered 401 is not a final answer: it and every later one wait for the next pass", async () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		const manifest = manifestWith({ postiz: entry });
		api.raise(host, HOSTED_PATHS, { ...schema, detail: "schema from another digest" });
		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });
		const reporter = new CompanionProgressReporter({
			apiUrl: "https://cloud-api.example.test",
			token: "runtime-token",
			installationId: entry.installationId,
			secrets: [],
			fetch: async () => new Response("unauthorized", { status: 401 }),
			sleep: async () => {},
		});

		const outcome = await reportConditionsOver(host, manifest, reporter);

		expect(outcome.outcome).toBe("unreportable");
		expect(api.read(host, HOSTED_PATHS).map((stored) => stored.reportedAt !== undefined)).toEqual([
			false,
			false,
		]);
	});

	it("another installation's condition is neither sent nor changed", async () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		const reporter = new RecordingReporter([]);
		api.raise(host, HOSTED_PATHS, {
			installationId: OTHER_INSTALLATION_ID,
			code: "update_rollback_incomplete",
			detail: "another installation's rollback",
		});
		api.raise(host, HOSTED_PATHS, { ...schema, detail: "schema from another digest" });
		const other = api.read(host, HOSTED_PATHS)[0];

		const outcome = await reportConditionsOver(host, manifestWith({ postiz: entry }), reporter);

		expect(outcome.sent?.map((sent) => sent.code)).toEqual(["schema_digest_mismatch"]);
		expect(api.read(host, HOSTED_PATHS)[0]).toEqual(other as Record<string, unknown>);
		expect(
			(await reportConditionsOver(host, manifestWith({ postiz: entry }), reporter)).outcome,
		).toBe("skipped");
		expect(reporter.reports).toHaveLength(1);
	});

	it("with no present entry, or one being removed, the pass is skipped and sends nothing", async () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		const reporter = new RecordingReporter([]);
		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });
		const record = host.readFile(CONDITIONS_PATH);

		const absent = await reportConditionsOver(host, manifestWith(), reporter);
		const removing = await reportConditionsOver(
			host,
			manifestWith({ postiz: { ...entry, lifecycle: "remove" } }),
			reporter,
		);

		expect(absent.outcome).toBe("skipped");
		expect(removing.outcome).toBe("skipped");
		expect(reporter.reports).toEqual([]);
		expect(host.readFile(CONDITIONS_PATH)).toBe(record);
	});

	it("with no runtime credential the pass is unreportable and the conditions stay unreported", async () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });

		// No injected sink, and the secret values carry no runtime credential to report with.
		const outcome = await reportConditionsOver(host, manifestWith({ postiz: entry }), undefined);

		expect(outcome.outcome).toBe("unreportable");
		expect(api.read(host, HOSTED_PATHS)[0]?.reportedAt).toBeUndefined();
	});

	it("a detail carrying the database password reaches the control plane redacted", async () => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		const bodies: string[] = [];
		const original = globalThis.fetch;
		globalThis.fetch = (async (_input: unknown, init?: RequestInit) => {
			bodies.push(String(init?.body ?? ""));
			return new Response("{}", { status: 200 });
		}) as unknown as typeof fetch;
		try {
			// A caller that forgot to redact.
			api.raise(host, HOSTED_PATHS, {
				...rollback,
				detail: `pg_restore: password ${DB_PASSWORD_TOKEN} rejected`,
			});

			// No injected sink: the installation's own reporter, with the runtime credential.
			const outcome = await reportConditionsOver(
				host,
				manifestWith({ postiz: entry }),
				undefined,
				bootstrapSecrets(entry),
			);

			expect(outcome.outcome).toBe("reported");
			expect(bodies).toHaveLength(1);
			const body = JSON.parse(bodies[0] ?? "{}") as {
				phase?: string;
				condition?: { detail?: string };
			};
			expect(body.phase).toBe("condition");
			expect(body.condition?.detail).toContain("[redacted]");
			for (const token of [DB_PASSWORD_TOKEN, RUNTIME_TOKEN]) {
				expect(bodies[0] ?? "").not.toContain(token);
			}
		} finally {
			globalThis.fetch = original;
		}
	});

	it.each([
		["a corrupt file", "{not json"],
		[
			"a foreign schema",
			JSON.stringify({ schema: "iforge.somethingElse.v1", kind: "postiz", conditions: [] }),
		],
		[
			"a foreign kind",
			JSON.stringify({
				schema: "iforge.containerCompanionConditions.v1",
				kind: "other",
				conditions: [],
			}),
		],
		[
			"an unknown code",
			JSON.stringify({
				schema: "iforge.containerCompanionConditions.v1",
				kind: "postiz",
				conditions: [
					{
						installationId: "5f0c6d2e-8a41-4b7f-9e3c-2d1a7b6e4c90",
						code: "disk_on_fire",
						state: "raised",
						detail: "planted",
						at: "2026-09-23T12:00:00.000Z",
					},
				],
			}),
		],
		[
			"a wrongly typed field",
			JSON.stringify({
				schema: "iforge.containerCompanionConditions.v1",
				kind: "postiz",
				conditions: [
					{
						installationId: "5f0c6d2e-8a41-4b7f-9e3c-2d1a7b6e4c90",
						code: "update_rollback_incomplete",
						state: "raised",
						detail: 42,
						at: "2026-09-23T12:00:00.000Z",
					},
				],
			}),
		],
	])("%s reads as no conditions, and the next raise rewrites it", async (_label, content) => {
		const api = conditionsApi();
		const host = new FakeContainerHost();
		const reporter = new RecordingReporter([]);
		plantConditions(host, content);

		expect(api.read(host, HOSTED_PATHS)).toEqual([]);
		expect(
			(await reportConditionsOver(host, manifestWith({ postiz: entry }), reporter)).outcome,
		).toBe("skipped");
		expect(reporter.reports).toEqual([]);

		api.raise(host, HOSTED_PATHS, { ...rollback, detail: "restore failed" });

		expect(conditionsRecord(host)).toMatchObject({
			schema: "iforge.containerCompanionConditions.v1",
			kind: "postiz",
			conditions: [
				expect.objectContaining({ code: "update_rollback_incomplete", state: "raised" }),
			],
		});
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-46 (WR-07, G-06-2): a rollback that fails is recorded, reported and never planned over.
// ---------------------------------------------------------------------------------------------

const HOST_NOW = "2026-09-23T12:00:00.000Z";

interface RollbackIncompleteRecord {
	previous: { images: Record<string, string>; installationId: string; state: string };
	label: string;
	step: string;
	organizations: number;
	error: string;
	failedAt: string;
	attempts: number;
	lastAttemptAt: string;
}

function incompleteOf(run: BootstrapRun): RollbackIncompleteRecord | undefined {
	return (receiptOf(run) as { rollbackIncomplete?: RollbackIncompleteRecord }).rollbackIncomplete;
}

/**
 * Make the pre-update restore fail while `failing.restore` is set, echoing the backup cipher pass
 * as a careless error might, so the recorded and reported text is proven redacted.
 */
function failingRestore(run: BootstrapRun): FailingRestore {
	const failing: FailingRestore = { restore: true, message: "repository unreachable" };
	const realRun = run.host.run.bind(run.host);
	run.host.run = (command, args, options) => {
		if (failing.restore && command === "podman" && args[0] === "run" && args.includes("restore")) {
			realRun(command, args, options);
			return {
				status: 40,
				stdout: "",
				stderr: `ERROR: [040]: ${failing.message} (cipher ${BACKUP_CIPHER_PASS})`,
			};
		}
		return realRun(command, args, options);
	};
	return failing;
}

interface FailingRestore {
	restore: boolean;
	message: string;
}

/** A ready Postiz whose update's ready is refused and whose rollback's restore fails. */
async function refusedWithFailingRestore(): Promise<{
	run: BackupRun;
	newer: PostizCompanionManifest;
	previousImages: Record<string, string>;
	failing: FailingRestore;
	outcome: unknown;
}> {
	const run = await readyForUpdate();
	const previousImages = { ...receiptOf(run).images };
	const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
	run.reporter = new ScriptedReporter(run.events, { ready: REFUSED_AS_FAILED });
	const failing = failingRestore(run);
	const outcome = await settle(runUpdate(run, newer));
	return { run, newer, previousImages, failing, outcome };
}

/** Commands a converge must never issue over an unknown host state. */
function stateChangingCommands(host: FakeContainerHost, from: number): string[] {
	return host.commands
		.slice(from)
		.filter(
			(call) =>
				call.command === "podman" ||
				call.command === "/usr/sbin/nft" ||
				(call.command === "systemctl" && call.args[0] !== "--version"),
		)
		.map((call) => `${call.command} ${call.args.join(" ")}`);
}

describe("postiz update: a failed rollback is recorded and never planned over (plan 06-46, WR-07)", () => {
	it("a refused ready whose rollback fails is recorded as rollback-incomplete, not settled as previous (WR-07)", async () => {
		const { run, newer, previousImages, outcome } = await refusedWithFailingRestore();
		const label = preUpdateLabel(run);
		const newerReference = reference(newer.artifact.image, NEWER_APP_DIGEST);

		expect(label).toMatch(/^20260923-120000F_/);
		expect(outcome).toMatchObject({
			outcome: "rollback-incomplete",
			step: "control-plane-refused",
			label,
			code: "companion_install_terminal",
			attempts: 1,
		});
		// The receipt keeps the images the swap rendered: it never claims the previous version runs.
		const receipt = receiptOf(run);
		expect(receipt.images.app).toBe(newerReference);
		expect(receipt.images).not.toEqual(previousImages);
		expect(receipt.pendingUpdateReport).toBeUndefined();
		expect(receipt.pendingUpdateRollback).toBeUndefined();
		expect(receipt.bootstrap).toBeUndefined();
		expect(receipt.refusedUpdate).toBeUndefined();
		const incomplete = incompleteOf(run);
		expect(incomplete).toMatchObject({
			previous: { images: previousImages, installationId: run.entry.installationId },
			label,
			step: "control-plane-refused",
			organizations: 1,
			attempts: 1,
			failedAt: HOST_NOW,
			lastAttemptAt: HOST_NOW,
		});
		expect(incomplete?.error).toContain("repository unreachable");
		expect(incomplete?.error).not.toContain(BACKUP_CIPHER_PASS);
		expect(incomplete?.error).toContain("[redacted]");
		// The stored previous receipt carries nothing pending of its own.
		expect(incomplete?.previous).not.toHaveProperty("pendingUpdateReport");
		expect(incomplete?.previous).not.toHaveProperty("pendingUpdateRollback");
		expect(incomplete?.previous).not.toHaveProperty("rollbackIncomplete");

		// The condition is raised on the host record, with the same redacted detail.
		expect(conditionsApi().read(run.host, HOSTED_PATHS)).toEqual([
			{
				installationId: run.entry.installationId,
				code: "update_rollback_incomplete",
				state: "raised",
				detail: incomplete?.error ?? "?",
				at: HOST_NOW,
			},
		]);
	});

	it("the incomplete rollback is reported once through the condition phase", async () => {
		const { run, newer } = await refusedWithFailingRestore();
		const reporter = new RecordingReporter([]);
		const manifest = manifestWith({ postiz: newer });

		const first = await reportConditionsOver(run.host, manifest, reporter, run.secrets);

		expect(first).toEqual({
			outcome: "reported",
			sent: [{ code: "update_rollback_incomplete", state: "raised", result: "recorded" }],
		});
		expect(reporter.reports).toEqual([
			{
				operation: "update",
				phase: "condition",
				condition: {
					code: "update_rollback_incomplete",
					state: "raised",
					detail: expect.stringContaining("repository unreachable"),
				},
			},
		]);
		expect(JSON.stringify(reporter.reports)).not.toContain(BACKUP_CIPHER_PASS);
		// Once per state: the second pass sends nothing.
		expect((await reportConditionsOver(run.host, manifest, reporter, run.secrets)).outcome).toBe(
			"skipped",
		);
		expect(reporter.reports).toHaveLength(1);
	});

	it.each([
		["the pin the control plane reverted to", false],
		["the refused pin, not yet reverted", true],
	])("the next converge over %s plans repair and runs nothing", async (_label, keepNewer) => {
		const { run, newer } = await refusedWithFailingRestore();
		const entry = keepNewer ? newer : run.entry;
		const receiptBefore = run.host.readFile(HOSTED_PATHS.receiptPath);
		const pgbackrestBefore = run.pgbackrest.calls.length;

		expect(planFor(run.host, entry, run.secrets).kind).toBe("repair");
		const from = run.host.commands.length;
		const applied = applyContainerCompanion(
			planFor(run.host, entry, run.secrets),
			run.host,
			HOSTED_PATHS,
		);
		converge(run.host, manifestWith({ postiz: entry }), convergePaths(), {
			secretValues: run.secrets,
		});

		expect(applied).toEqual({ kind: "repair", pulled: [], written: [], started: [] });
		expect(stateChangingCommands(run.host, from)).toEqual([]);
		expect(run.pgbackrest.calls.length).toBe(pgbackrestBefore);
		expect(run.host.readFile(HOSTED_PATHS.receiptPath)).toBe(receiptBefore);
	});
});

/** Lose every organization at the update's restart, as a destructive schema push would. */
function loseOrganizationsOnRestart(run: BootstrapRun, restoreBringsThemBack: boolean): void {
	const realRun = run.host.run.bind(run.host);
	run.host.run = (command, args, options) => {
		const result = realRun(command, args, options);
		if (command === "systemctl" && args[0] === "restart") run.postiz.organizations = 0;
		if (
			restoreBringsThemBack &&
			result.status === 0 &&
			command === "podman" &&
			args[0] === "run" &&
			args.includes("restore")
		) {
			run.postiz.organizations = 1;
		}
		return result;
	};
}

/** Add an incomplete rollback to the host's receipt, keeping everything else (its ready marker too). */
function recordIncompleteRollback(host: FakeContainerHost): void {
	const receipt = JSON.parse(host.readFile(HOSTED_PATHS.receiptPath) ?? "{}");
	const { bootstrap: _marker, ...previous } = receipt;
	host.writeFile(
		HOSTED_PATHS.receiptPath,
		`${JSON.stringify({
			...receipt,
			rollbackIncomplete: {
				previous,
				label: "20260923-120000F",
				step: "control-plane-refused",
				organizations: 1,
				error: "the rollback failed: pgbackrest restore failed",
				failedAt: HOST_NOW,
				attempts: 1,
				lastAttemptAt: HOST_NOW,
			},
		})}\n`,
		{ mode: 0o600 },
	);
}

describe("postiz update: both rollback paths record an incomplete rollback, and every consumer waits (plan 06-46, WR-07)", () => {
	it("a post-update-assertion rollback that fails is recorded as rollback-incomplete", async () => {
		const run = await readyForUpdate();
		const previousImages = { ...receiptOf(run).images };
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const newerReference = reference(newer.artifact.image, NEWER_APP_DIGEST);
		failingRestore(run);
		loseOrganizationsOnRestart(run, false);

		const outcome = await settle(runUpdate(run, newer));

		const label = preUpdateLabel(run);
		expect(label).toMatch(/^20260923-120000F_/);
		expect(outcome).toMatchObject({
			outcome: "rollback-incomplete",
			step: "post-update-assertion",
			label,
			code: null,
			attempts: 1,
		});
		// The update's failed report is still sent, naming the failed rollback.
		expect(run.reporter.last()).toMatchObject({
			operation: "update",
			phase: "failed",
			step: "post-update-assertion",
		});
		const last = run.reporter.last();
		expect(last?.phase === "failed" ? last.error : "").toContain("the rollback failed");
		expect(JSON.stringify(run.reporter.reports)).not.toContain(BACKUP_CIPHER_PASS);
		// The receipt carries the swapped images and the incomplete rollback, never the previous images.
		const receipt = receiptOf(run);
		expect(receipt.images.app).toBe(newerReference);
		expect(receipt.bootstrap).toBeUndefined();
		expect(receipt.pendingUpdateReport).toBeUndefined();
		const incomplete = incompleteOf(run);
		expect(incomplete).toMatchObject({
			previous: { images: previousImages, installationId: run.entry.installationId },
			label,
			step: "post-update-assertion",
			organizations: 1,
			attempts: 1,
			failedAt: HOST_NOW,
			lastAttemptAt: HOST_NOW,
		});
		expect(incomplete?.error).toContain("repository unreachable");
		expect(incomplete?.error).not.toContain(BACKUP_CIPHER_PASS);
		expect(incomplete?.previous).not.toHaveProperty("rollbackIncomplete");
		expect(conditionsApi().read(run.host, HOSTED_PATHS)).toMatchObject([
			{ code: "update_rollback_incomplete", state: "raised", detail: incomplete?.error ?? "?" },
		]);
		// And the next converge plans nothing over it.
		expect(planFor(run.host, run.entry, run.secrets).kind).toBe("repair");
	});

	it("a withdrawn installation keeps its incomplete rollback, and its return plans repair", async () => {
		const { run, newer } = await refusedWithFailingRestore();
		const incomplete = incompleteOf(run);

		withdrawContainerCompanion("postiz", run.host, HOSTED_PATHS);

		const withdrawn = receiptOf(run);
		expect(withdrawn.state).toBe("withdrawn");
		expect(incompleteOf(run)).toEqual(incomplete);
		for (const entry of [run.entry, newer]) {
			expect(planFor(run.host, entry, run.secrets).kind).toBe("repair");
		}
		const from = run.host.commands.length;
		converge(run.host, manifestWith({ postiz: run.entry }), convergePaths(), {
			secretValues: run.secrets,
		});
		expect(stateChangingCommands(run.host, from)).toEqual([]);
		expect(receiptOf(run).state).toBe("withdrawn");
		expect(incompleteOf(run)).toEqual(incomplete);
	});

	it("the readiness pass skips an incomplete rollback by name and never re-bootstraps over it", async () => {
		const { run, newer } = await refusedWithFailingRestore();
		const from = run.host.commands.length;
		const reportsBefore = run.reporter.reports.length;

		for (const entry of [newer, run.entry]) {
			const outcome = await runReadiness({ ...run, entry });
			expect(outcome).toMatchObject({ outcome: "skipped" });
			expect(outcome.outcome === "skipped" ? outcome.reason : "").toContain("rollback");
		}
		expect(run.host.commands.slice(from)).toEqual([]);
		expect(run.reporter.reports.length).toBe(reportsBefore);
	});

	it("maintenance skips an incomplete rollback by name, even with a ready marker: no backup", async () => {
		const run = await readyForUpdate();
		recordIncompleteRollback(run.host);
		const pgbackrestBefore = run.pgbackrest.calls.length;

		const outcome = await runContainerCompanionMaintenance({
			paths: RUNTIME_PATHS,
			manifest: manifestWith({ postiz: run.entry }),
			secretValues: run.secrets,
			host: run.host,
			reporter: run.reporter,
			now: new Date(INSTALLED_AT + 3 * 24 * 60 * 60_000),
		});

		expect(outcome).toMatchObject({ outcome: "skipped" });
		expect(outcome.outcome === "skipped" ? outcome.reason : "").toContain("rollback");
		expect(run.pgbackrest.calls.length).toBe(pgbackrestBefore);
	});

	it("export delivery skips an incomplete rollback by name, even with a ready marker: no dump", async () => {
		const entry = exportEntry();
		const host = readyExportHost(entry);
		recordIncompleteRollback(host);

		const run = await runExport({ entry, host });

		expect(run.outcome).toMatchObject({ outcome: "skipped" });
		expect(run.outcome.outcome === "skipped" ? run.outcome.reason : "").toContain("rollback");
		expect(run.plane.uploads).toEqual([]);
		expect(run.plane.attempts()).toBe(0);
	});

	it("the update path's swap skips an incomplete rollback by name: no backup, no pull, no restart", async () => {
		const { run, newer } = await refusedWithFailingRestore();
		const from = run.events.length;
		const reportsBefore = run.reporter.reports.length;

		for (const entry of [newer, withAppDigest(run.entry, "d".repeat(64))]) {
			const outcome = await settle(runUpdate(run, entry));
			expect(outcome).toMatchObject({ outcome: "skipped" });
			expect((outcome as { reason?: string }).reason ?? "").toContain("rollback");
		}
		const timeline = run.events.slice(from);
		expect(timeline.filter((event) => event.startsWith("pgbackrest:"))).toEqual([]);
		expect(swapEvents(timeline)).toEqual([]);
		expect(run.reporter.reports.length).toBe(reportsBefore);
	});

	it("a refused ready whose rollback completes but fails its data check settles on the previous version", async () => {
		const run = await readyForUpdate();
		const previousImages = { ...receiptOf(run).images };
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		run.reporter = new ScriptedReporter(run.events, { ready: REFUSED_AS_FAILED });
		// The restore runs, but the previous version comes back without its organization.
		const realRun = run.host.run.bind(run.host);
		run.host.run = (command, args, options) => {
			const result = realRun(command, args, options);
			if (command === "podman" && args[0] === "run" && args.includes("restore")) {
				run.postiz.organizations = 0;
			}
			return result;
		};

		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({ outcome: "rolled-back", step: "control-plane-refused" });
		expect((outcome as { rollback?: string }).rollback).toContain("did not pass its check");
		const receipt = receiptOf(run);
		expect(receipt.images).toEqual(previousImages);
		expect(receipt.refusedUpdate?.step).toBe("control-plane-refused");
		expect(incompleteOf(run)).toBeUndefined();
		expect(conditionsApi().read(run.host, HOSTED_PATHS)).toEqual([]);
	});

	it("a post-update rollback that completes but fails its data check settles on the previous version", async () => {
		const run = await readyForUpdate();
		const previousImages = { ...receiptOf(run).images };
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		loseOrganizationsOnRestart(run, false);

		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({ outcome: "failed", step: "post-update-assertion" });
		const last = run.reporter.last();
		expect(last?.phase === "failed" ? last.error : "").toContain("did not pass its check");
		const receipt = receiptOf(run);
		expect(receipt.images).toEqual(previousImages);
		expect(incompleteOf(run)).toBeUndefined();
		expect(conditionsApi().read(run.host, HOSTED_PATHS)).toEqual([]);
	});
});

/** Move the incomplete rollback's last attempt `minutes` before the host's clock. */
function ageLastAttempt(run: BootstrapRun, minutes: number): string {
	const receipt = JSON.parse(run.host.readFile(HOSTED_PATHS.receiptPath) ?? "{}");
	const lastAttemptAt = new Date(Date.parse(HOST_NOW) - minutes * 60_000).toISOString();
	receipt.rollbackIncomplete.lastAttemptAt = lastAttemptAt;
	run.host.writeFile(HOSTED_PATHS.receiptPath, `${JSON.stringify(receipt)}\n`, { mode: 0o600 });
	return lastAttemptAt;
}

describe("postiz update: an incomplete rollback is repaired with its own pre-update backup (plan 06-46, WR-07)", () => {
	it("the incomplete rollback is retried with the stored label after the backoff and settles on the previous version", async () => {
		const { run, newer, previousImages, failing } = await refusedWithFailingRestore();
		const label = preUpdateLabel(run);
		const swappedImages = { ...receiptOf(run).images };
		const incomplete = incompleteOf(run);
		const previousReference = reference(run.entry.artifact.image, run.entry.artifact.sha256);
		const reporter = new RecordingReporter([]);
		const manifest = manifestWith({ postiz: run.entry });
		await reportConditionsOver(run.host, manifest, reporter, run.secrets);
		expect(reporter.reports).toHaveLength(1);
		ageLastAttempt(run, 31);
		failing.restore = false;
		const from = run.events.length;
		const pgbackrestFrom = run.pgbackrest.calls.length;
		const updateReportsBefore = run.reporter.reports.length;

		// The control plane reverted the pin; the host goes back to it with the same backup.
		const outcome = await settle(runUpdate(run, run.entry));

		expect(outcome).toMatchObject({ outcome: "repaired", label });
		expectInOrder(run.events.slice(from), [
			APP_STOP_THEN_DB,
			`write:${APP_SOURCE}`,
			"pgbackrest:restore --type=immediate",
			APP_START_PREVIOUS,
		]);
		const calls = run.pgbackrest.calls.slice(pgbackrestFrom);
		// The restore, then (plan 06-48) the reset of the set's next-schema annotation.
		expect(calls).toHaveLength(2);
		const restore = calls[0] ?? [];
		for (const arg of ["restore", `--set=${label}`, "--type=immediate"]) {
			expect(restore).toContain(arg);
		}
		expect(calls[1]).toEqual(
			expect.arrayContaining([
				"annotate",
				`--set=${label}`,
				`--annotation=iforge-schema-next=${run.entry.artifact.sha256}`,
			]),
		);
		// Never the newest backup to the end of the archive, and never a look for the newest backup.
		expect(calls.flat()).not.toContain("--type=default");
		expect(calls.flat()).not.toContain("info");
		// The repair sends no update report: that operation is already terminal on the control plane.
		expect(run.reporter.reports.length).toBe(updateReportsBefore);
		// The receipt is the stored previous one again, with the refused images recorded.
		expect(receiptOf(run) as Record<string, unknown>).toEqual({
			...(incomplete?.previous ?? {}),
			refusedUpdate: {
				images: swappedImages,
				step: "control-plane-refused",
				label,
				refusedAt: HOST_NOW,
			},
		});
		expect(receiptOf(run).images).toEqual(previousImages);
		expect(run.host.sources().get(APP_SOURCE)).toContain(previousReference);
		expect(transientFiles(run.host)).toEqual([]);
		// The condition is cleared, and the clearing is reported once.
		const cleared = await reportConditionsOver(run.host, manifest, reporter, run.secrets);
		expect(cleared).toEqual({
			outcome: "reported",
			sent: [{ code: "update_rollback_incomplete", state: "cleared", result: "recorded" }],
		});
		expect((await reportConditionsOver(run.host, manifest, reporter, run.secrets)).outcome).toBe(
			"skipped",
		);
		// And the next converge over the reverted pin plans nothing to do.
		expect(planFor(run.host, run.entry, run.secrets).kind).toBe("unchanged");
		expect(newer.artifact.sha256).toBe(NEWER_APP_DIGEST);
	});

	it("the repair waits for its backoff", async () => {
		const { run, failing } = await refusedWithFailingRestore();
		const lastAttemptAt = ageLastAttempt(run, 10);
		failing.restore = false;
		const receiptBefore = run.host.readFile(HOSTED_PATHS.receiptPath);
		const from = run.host.commands.length;

		const outcome = await settle(runUpdate(run, run.entry));

		const nextAttempt = new Date(
			Date.parse(lastAttemptAt) + containerCompanionModule.ROLLBACK_REPAIR_RETRY_AFTER_MS,
		).toISOString();
		expect(nextAttempt).toBe("2026-09-23T12:20:00.000Z");
		expect(outcome).toMatchObject({ outcome: "skipped" });
		expect((outcome as { reason?: string }).reason).toContain(nextAttempt);
		expect(run.host.commands.slice(from)).toEqual([]);
		expect(run.host.readFile(HOSTED_PATHS.receiptPath)).toBe(receiptBefore);
	});

	it("a repair that fails again counts the attempt", async () => {
		const { run, failing } = await refusedWithFailingRestore();
		const label = preUpdateLabel(run);
		const reporter = new RecordingReporter([]);
		const manifest = manifestWith({ postiz: run.entry });
		await reportConditionsOver(run.host, manifest, reporter, run.secrets);
		const firstError = incompleteOf(run)?.error;
		ageLastAttempt(run, 31);
		failing.message = "access denied to the repository";

		const outcome = await settle(runUpdate(run, run.entry));

		expect(outcome).toMatchObject({
			outcome: "rollback-incomplete",
			step: "control-plane-refused",
			label,
			attempts: 2,
		});
		const incomplete = incompleteOf(run);
		expect(incomplete).toMatchObject({
			label,
			step: "control-plane-refused",
			attempts: 2,
			lastAttemptAt: HOST_NOW,
			failedAt: HOST_NOW,
		});
		expect(incomplete?.error).toContain("access denied to the repository");
		expect(incomplete?.error).not.toBe(firstError);
		expect(incomplete?.error).not.toContain(BACKUP_CIPHER_PASS);
		// The receipt still claims nothing about the previous version.
		expect(receiptOf(run).refusedUpdate).toBeUndefined();
		expect(receiptOf(run).images.app).toContain(NEWER_APP_DIGEST);
		// The new detail is raised and reported once.
		const again = await reportConditionsOver(run.host, manifest, reporter, run.secrets);
		expect(again).toEqual({
			outcome: "reported",
			sent: [{ code: "update_rollback_incomplete", state: "raised", result: "recorded" }],
		});
		expect(reporter.reports.at(-1)).toMatchObject({
			phase: "condition",
			condition: {
				state: "raised",
				detail: expect.stringContaining("access denied to the repository"),
			},
		});
		expect((await reportConditionsOver(run.host, manifest, reporter, run.secrets)).outcome).toBe(
			"skipped",
		);
		// And the next retry waits for a fresh backoff.
		expect(await settle(runUpdate(run, run.entry))).toMatchObject({ outcome: "skipped" });
		expect(incompleteOf(run)?.attempts).toBe(2);
	});

	it("a withdrawn receipt with an incomplete rollback is repaired when the present entry names its installation", async () => {
		const { run, previousImages, failing } = await refusedWithFailingRestore();
		const label = preUpdateLabel(run);
		withdrawContainerCompanion("postiz", run.host, HOSTED_PATHS);
		expect(run.host.stat(BACKUP_CREDENTIAL)).toBeNull();
		ageLastAttempt(run, 31);
		failing.restore = false;
		const pgbackrestFrom = run.pgbackrest.calls.length;

		const outcome = await settle(runUpdate(run, run.entry));

		expect(outcome).toMatchObject({ outcome: "repaired", label });
		const restore = run.pgbackrest.calls.slice(pgbackrestFrom)[0] ?? [];
		expect(restore).toContain(`--set=${label}`);
		expect(restore).toContain("--type=immediate");
		const receipt = receiptOf(run);
		expect(receipt.state).toBe("applied");
		expect(receipt.images).toEqual(previousImages);
		expect(incompleteOf(run)).toBeUndefined();
		// The previous units and their credentials are back, so the previous version can start.
		expect(run.host.stat(BACKUP_CREDENTIAL)?.isFile).toBe(true);
		expect(run.host.sources().get(APP_SOURCE)).toContain(run.entry.artifact.sha256);
		expect(conditionsApi().read(run.host, HOSTED_PATHS)).toMatchObject([
			{ code: "update_rollback_incomplete", state: "cleared" },
		]);
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-47 (G-06-1, WR-02): the converge holds the digests that wrote the data; only the update
// path moves them.
// ---------------------------------------------------------------------------------------------

/** The app digest the app's Quadlet source names, or `none` when it is absent. */
function appSourceDigest(host: FakeContainerHost): string {
	return /@sha256:([a-f0-9]{64})/.exec(host.readFile(APP_SOURCE) ?? "")?.[1] ?? "none";
}

/**
 * Put every change of the app's Quadlet source on the shared timeline as `app-source:<digest>`, so
 * a test can tell which app a unit start would have run.
 */
function traceAppSource(run: BootstrapRun): void {
	const realWrite = run.host.writeFile.bind(run.host);
	run.host.writeFile = (path, content, options) => {
		realWrite(path, content, options);
		if (path === APP_SOURCE) run.events.push(`app-source:${appSourceDigest(run.host)}`);
	};
	const realRemove = run.host.removeFile.bind(run.host);
	run.host.removeFile = (path) => {
		realRemove(path);
		if (path === APP_SOURCE) run.events.push("app-source:none");
	};
}

/** Every unit start or restart in `timeline[0, until)` while the app source named `digest`. */
function startsWhileAppSourceIs(
	timeline: readonly string[],
	initial: string,
	digest: string,
	until: number,
): string[] {
	let current = initial;
	const starts: string[] = [];
	for (const [index, event] of timeline.entries()) {
		if (index >= until) break;
		if (event.startsWith("app-source:")) current = event.slice("app-source:".length);
		// The network unit holds no data: the plan 06-49 rollback gate starts it to verify its subnet.
		else if (event === "run:systemctl start iforge-postiz-network.service") continue;
		else if (current === digest && /^run:systemctl (start|restart) /.test(event))
			starts.push(event);
	}
	return starts;
}

/** The data directory a real host keeps across a withdrawal: an initialised PGDATA. */
function retainData(host: FakeContainerHost): void {
	host.files.set(PGDATA_VERSION_FILE, {
		content: "18\n",
		mode: 0o600,
		uid: 1_000_065_536 + 999,
		gid: 1_000_065_536 + 999,
	});
}

describe("the converge holds the digests that wrote the data (plan 06-47, WR-02)", () => {
	it("a withdrawn host with a pending ready comes back on its own digest and the refused ready rolls it back (WR-02)", async () => {
		const run = await readyForUpdate();
		retainData(run.host);
		const previousImages = { ...receiptOf(run).images };
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const newerReference = reference(newer.artifact.image, NEWER_APP_DIGEST);
		const previousReference = reference(run.entry.artifact.image, run.entry.artifact.sha256);
		const scripted = new ScriptedReporter(run.events, {
			ready: new Error("control plane unreachable"),
		});
		run.reporter = scripted;
		// Y is swapped in and its ready is left undelivered.
		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "unreportable" });
		const label = preUpdateLabel(run);
		const pendingReport = receiptOf(run).pendingUpdateReport;
		const pendingRollback = receiptOf(run).pendingUpdateRollback;
		expect(pendingReport).toMatchObject({ phase: "ready" });
		expect(pendingRollback).toBeDefined();

		// The entry is briefly omitted: the host withdraws, keeping the data and the receipt's images.
		const paths = convergePaths();
		converge(run.host, manifestWith(), paths, { secretValues: run.secrets });
		expect(receiptOf(run).state).toBe("withdrawn");

		// It returns with the pin reverted to X and nothing announced.
		expect(run.entry.update).toBeNull();
		traceAppSource(run);
		const initial = appSourceDigest(run.host);
		const from = run.events.length;
		converge(run.host, manifestWith({ postiz: run.entry }), paths, { secretValues: run.secrets });

		// Y, the digest that wrote the schema, runs again -- never X over Y's data.
		expect(run.host.sources().get(APP_SOURCE)).toContain(newerReference);
		expect(run.host.sources().get(APP_SOURCE)).not.toContain(previousReference);
		const reapplied = receiptOf(run);
		expect(reapplied.state).toBe("applied");
		expect(reapplied.images.app).toBe(newerReference);
		// The pending update survives withdrawn -> install, so it can still be settled or rolled back.
		expect(reapplied.pendingUpdateReport).toEqual(pendingReport);
		expect(reapplied.pendingUpdateRollback).toEqual(pendingRollback);
		expect(await runReadiness(run)).toMatchObject({ outcome: "skipped" });

		// The control plane refuses the ready: its deadline already failed the update.
		scripted.answers.ready = REFUSED_AS_FAILED;
		const outcome = await settle(runUpdate(run, run.entry));

		expect(outcome).toMatchObject({
			outcome: "rolled-back",
			step: "control-plane-refused",
			label,
			code: "companion_install_terminal",
		});
		const timeline = run.events.slice(from);
		expectInOrder(timeline, [
			"report:ready",
			APP_STOP_THEN_DB,
			"pgbackrest:restore --type=immediate",
			APP_START_PREVIOUS,
		]);
		expect(timeline).not.toContain("pgbackrest:restore --type=default");
		const restore = run.pgbackrest.calls.filter((argv) => argv.includes("restore")).at(-1) ?? [];
		expect(restore).toContain(`--set=${label}`);
		// No unit started while X's app source was on disk, until the pre-update backup was restored.
		const restoredAt = timeline.indexOf("pgbackrest:restore --type=immediate");
		expect(
			startsWhileAppSourceIs(timeline, initial, run.entry.artifact.sha256, restoredAt),
		).toEqual([]);
		const receipt = receiptOf(run);
		expect(receipt.images).toEqual(previousImages);
		expect(receipt.refusedUpdate?.images.app).toBe(newerReference);
		expect(receipt.refusedUpdate?.step).toBe("control-plane-refused");
		expect(receipt.pendingUpdateReport).toBeUndefined();
		expect(receipt.pendingUpdateRollback).toBeUndefined();
		expect(run.host.sources().get(APP_SOURCE)).toContain(previousReference);
	});
});

const SCHEMA_DIGEST_MARKER_PATH = "/var/lib/iforge-postiz/db/.iforge-schema-digest";

/** Put every write of the schema marker on the shared timeline as `schema-marker:<digest>`. */
function traceSchemaMarker(host: FakeContainerHost): void {
	const realWrite = host.writeFile.bind(host);
	host.writeFile = (path, content, options) => {
		realWrite(path, content, options);
		if (path === SCHEMA_DIGEST_MARKER_PATH) host.events.push(`schema-marker:${content.trim()}`);
	};
}

const isAppStartOrRestart = (event: string) =>
	/^run:systemctl (start|restart) (.* )?iforge-postiz\.service( |$)/.test(event);

/**
 * A ready Postiz whose update to Y crashed at its restart: the host's files are put back exactly
 * as they were when the restart was issued, as a machine that died there would find them.
 */
async function interruptedSwap(): Promise<{
	run: BackupRun;
	newer: PostizCompanionManifest;
	previousImages: Record<string, string>;
	label: string;
}> {
	const run = await readyForUpdate();
	const previousImages = { ...receiptOf(run).images };
	const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
	const crash: { files: Map<string, FakeFile> | null } = { files: null };
	const realRun = run.host.run.bind(run.host);
	run.host.run = (command, args, options) => {
		if (crash.files === null && command === "systemctl" && args[0] === "restart") {
			crash.files = new Map([...run.host.files].map(([path, file]) => [path, { ...file }]));
		}
		return realRun(command, args, options);
	};
	await settle(runUpdate(run, newer));
	run.host.run = realRun;
	const label = preUpdateLabel(run);
	if (crash.files === null || label === null)
		throw new Error("the update never reached its restart");
	run.host.files.clear();
	for (const [path, file] of crash.files) run.host.files.set(path, file);
	run.reporter.reports.length = 0;
	return { run, newer, previousImages, label };
}

describe("the schema digest marker and the write-ahead swap (plan 06-47, D-28)", () => {
	it("an install writes the schema marker with the app digest before the app first starts", () => {
		const events: string[] = [];
		const host = new FakeContainerHost({ events });
		traceSchemaMarker(host);
		const entry = fixtureEntry();

		applyContainerCompanion(planFor(host, entry), host, HOSTED_PATHS);

		const markerAt = events.indexOf(`schema-marker:${entry.artifact.sha256}`);
		expect(markerAt).toBeGreaterThanOrEqual(0);
		expect(events.findIndex(isAppStartOrRestart)).toBeGreaterThan(markerAt);
		expect(host.readFile(SCHEMA_DIGEST_MARKER_PATH)).toBe(`${entry.artifact.sha256}\n`);
		expect(host.files.get(SCHEMA_DIGEST_MARKER_PATH)).toMatchObject({ mode: 0o600, uid: 0 });
		const receipt = JSON.parse(host.readFile(HOSTED_PATHS.receiptPath) ?? "{}");
		expect(receipt.schemaDigest).toBe(entry.artifact.sha256);
	});

	it("an update writes the marker and its write-ahead receipt before it renders or restarts", async () => {
		const run = await readyForUpdate();
		const previousImages = { ...receiptOf(run).images };
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const newerReference = reference(newer.artifact.image, NEWER_APP_DIGEST);
		traceSchemaMarker(run.host);
		const atRestart: { receipt: Record<string, unknown> | null; marker: string | null } = {
			receipt: null,
			marker: null,
		};
		const realRun = run.host.run.bind(run.host);
		run.host.run = (command, args, options) => {
			if (atRestart.receipt === null && command === "systemctl" && args[0] === "restart") {
				atRestart.receipt = receiptOf(run);
				atRestart.marker = run.host.readFile(SCHEMA_DIGEST_MARKER_PATH);
			}
			return realRun(command, args, options);
		};
		const from = run.events.length;

		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "ready" });

		const label = preUpdateLabel(run);
		expectInOrder(run.events.slice(from), [
			`run:podman pull ${newerReference}`,
			`schema-marker:${NEWER_APP_DIGEST}`,
			`write:${RECEIPT_PATH}`,
			`write:${APP_SOURCE}`,
			APP_RESTART,
		]);
		expect(atRestart.marker).toBe(`${NEWER_APP_DIGEST}\n`);
		expect(atRestart.receipt).toMatchObject({
			images: previousImages,
			schemaDigest: NEWER_APP_DIGEST,
			pendingUpdateRollback: { previous: { images: previousImages }, label, organizations: 1 },
		});
		expect(atRestart.receipt?.pendingUpdateReport).toBeUndefined();
		const receipt = receiptOf(run);
		expect(receipt.images.app).toBe(newerReference);
		expect(receipt.schemaDigest).toBe(NEWER_APP_DIGEST);
	});

	it.each([
		["the post-update check fails", "post-update-assertion"],
		["the control plane refuses the ready", "control-plane-refused"],
		["an incomplete rollback is repaired", "repair"],
	])(
		"when %s, the rollback writes the previous digest after the restore and before the previous units start",
		async (_case, path) => {
			const refused = path === "repair" ? await refusedWithFailingRestore() : null;
			const run = refused?.run ?? (await readyForUpdate());
			const previousDigest = run.entry.artifact.sha256;
			const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
			if (path === "post-update-assertion") loseOrganizationsOnRestart(run, true);
			if (path === "control-plane-refused") {
				run.reporter = new ScriptedReporter(run.events, { ready: REFUSED_AS_FAILED });
			}
			if (refused !== null) {
				ageLastAttempt(run, 31);
				refused.failing.restore = false;
			}
			traceSchemaMarker(run.host);
			const from = run.events.length;

			const outcome = await settle(runUpdate(run, path === "repair" ? run.entry : newer));

			expect(outcome).toMatchObject({
				outcome:
					path === "post-update-assertion"
						? "failed"
						: path === "repair"
							? "repaired"
							: "rolled-back",
			});
			expectInOrder(run.events.slice(from), [
				"pgbackrest:restore --type=immediate",
				`schema-marker:${previousDigest}`,
				APP_START_PREVIOUS,
			]);
			expect(run.host.readFile(SCHEMA_DIGEST_MARKER_PATH)).toBe(`${previousDigest}\n`);
			expect(receiptOf(run).schemaDigest).toBe(previousDigest);
		},
	);

	it.each([
		["still names the new digest", true],
		["was already reverted", false],
	])(
		"an update interrupted after its write-ahead is rolled back from the stored label, never re-backed-up (the pin %s)",
		async (_case, stillNewer) => {
			const { run, newer, previousImages, label } = await interruptedSwap();
			const previousDigest = run.entry.artifact.sha256;
			expect(receiptOf(run).pendingUpdateRollback).toMatchObject({ label, organizations: 1 });
			expect(receiptOf(run).pendingUpdateReport).toBeUndefined();
			expect(run.host.readFile(SCHEMA_DIGEST_MARKER_PATH)).toBe(`${NEWER_APP_DIGEST}\n`);
			traceSchemaMarker(run.host);
			const from = run.events.length;
			const pgbackrestFrom = run.pgbackrest.calls.length;

			const outcome = await settle(runUpdate(run, stillNewer ? newer : run.entry));

			expect(outcome).toMatchObject({ outcome: "failed", step: "post-update-assertion", label });
			const timeline = run.events.slice(from);
			// No fresh "pre-update" backup of a schema the new digest may already have written.
			expect(timeline.filter((event) => event.startsWith("pgbackrest:backup"))).toEqual([]);
			expect(
				run.pgbackrest.calls.slice(pgbackrestFrom).some((argv) => argv.includes("backup")),
			).toBe(false);
			expectInOrder(timeline, [
				APP_STOP_THEN_DB,
				"pgbackrest:restore --type=immediate",
				`schema-marker:${previousDigest}`,
				APP_START_PREVIOUS,
				"report:failed",
			]);
			const restore = run.pgbackrest.calls
				.slice(pgbackrestFrom)
				.find((argv) => argv.includes("restore"));
			expect(restore).toContain(`--set=${label}`);
			expect(run.reporter.last()).toMatchObject({
				operation: "update",
				phase: "failed",
				step: "post-update-assertion",
			});
			const last = run.reporter.last();
			expect(last?.phase === "failed" ? last.error : "").toContain("interrupted");
			const receipt = receiptOf(run);
			expect(receipt.images).toEqual(previousImages);
			expect(receipt.schemaDigest).toBe(previousDigest);
			expect(receipt.pendingUpdateRollback).toBeUndefined();
			expect(receipt.pendingUpdateReport).toBeUndefined();
			expect(run.host.sources().get(APP_SOURCE)).toContain(previousDigest);
		},
	);

	it("an interrupted swap whose rollback does not complete is recorded as rollback-incomplete", async () => {
		const { run, newer, previousImages, label } = await interruptedSwap();
		failingRestore(run);
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({
			outcome: "rollback-incomplete",
			step: "post-update-assertion",
			label,
			attempts: 1,
		});
		expect(run.events.slice(from).filter((event) => event.startsWith("pgbackrest:backup"))).toEqual(
			[],
		);
		expect(incompleteOf(run)).toMatchObject({
			previous: { images: previousImages },
			label,
			step: "post-update-assertion",
			organizations: 1,
			attempts: 1,
		});
		expect(receiptOf(run).pendingUpdateRollback).toBeUndefined();
		expect(run.reporter.last()).toMatchObject({
			operation: "update",
			phase: "failed",
			step: "post-update-assertion",
		});
		expect(conditionsApi().read(run.host, HOSTED_PATHS)).toMatchObject([
			{ code: "update_rollback_incomplete", state: "raised" },
		]);
	});

	it("an interrupted swap rolled back while the entry announces a different, later update settles without failing that update (06-REVIEW WR-01)", async () => {
		const { run, newer, previousImages, label } = await interruptedSwap();
		const newerReference = reference(newer.artifact.image, NEWER_APP_DIGEST);
		// The host stayed down past N's deadline: the pin went back, and the tenant accepted M.
		const later = withAppDigest(run.entry, FOREIGN_APP_DIGEST);
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, later));

		expect(outcome).toMatchObject({ outcome: "failed", step: "post-update-assertion", label });
		expectInOrder(run.events.slice(from), [
			"pgbackrest:restore --type=immediate",
			APP_START_PREVIOUS,
		]);
		// N's failure is not M's: M was never tried, so nothing fails it on the control plane.
		expect(run.reporter.reports.filter((report) => report.phase === "failed")).toEqual([]);
		const receipt = receiptOf(run);
		expect(receipt.images).toEqual(previousImages);
		expect(receipt.pendingUpdateReport).toBeUndefined();
		expect(receipt.pendingUpdateRollback).toBeUndefined();
		// The refusal records what the swap really moved to, never the entry's later pin.
		expect(receipt.refusedUpdate?.images.app).toBe(newerReference);

		// So the next pass runs M rather than skipping it as already refused.
		expect(await settle(runUpdate(run, later))).toMatchObject({ outcome: "ready" });
		expect(run.reporter.last()).toMatchObject({
			operation: "update",
			phase: "ready",
			applied_digest: FOREIGN_APP_DIGEST,
		});
	});

	it("the install ready names the app digest the host runs, not the pin", async () => {
		const run = await readyForUpdate();
		retainData(run.host);
		const paths = convergePaths();
		converge(run.host, manifestWith(), paths, { secretValues: run.secrets });
		const announced = withAppDigest(run.entry, NEWER_APP_DIGEST);
		converge(run.host, manifestWith({ postiz: announced }), paths, { secretValues: run.secrets });
		const running = reference(run.entry.artifact.image, run.entry.artifact.sha256);
		expect(receiptOf(run).images.app).toBe(running);

		expect(await runReadiness({ ...run, entry: announced })).toMatchObject({ outcome: "ready" });

		expect(run.reporter.last()).toMatchObject({
			operation: "install",
			phase: "ready",
			applied_digest: run.entry.artifact.sha256,
		});
	});
});

/** A ready Postiz at S whose entry was briefly omitted, so the host withdrew it and kept its data. */
async function withdrawnAtSchemaDigest(): Promise<{ run: BackupRun; paths: RuntimePaths }> {
	const run = await readyForUpdate();
	retainData(run.host);
	const paths = convergePaths();
	converge(run.host, manifestWith(), paths, { secretValues: run.secrets });
	expect(receiptOf(run).state).toBe("withdrawn");
	return { run, paths };
}

/** The entry pinned at `sha256` with nothing announced. */
function pinnedWithoutAnnouncement(
	entry: PostizCompanionManifest,
	sha256: string,
): PostizCompanionManifest {
	return postizCompanionSchema.parse({
		...entry,
		artifact: { ...entry.artifact, sha256 },
		update: null,
	});
}

function schemaConditions(host: FakeContainerHost): Array<Record<string, unknown>> {
	return conditionsApi()
		.read(host, HOSTED_PATHS)
		.filter((condition) => condition.code === "schema_digest_mismatch");
}

describe("the install path's rule table and schema_digest_mismatch (plan 06-47, WR-02)", () => {
	it("withdrawn at S with the pin at S is a normal install: no hold, no condition", async () => {
		const { run, paths } = await withdrawnAtSchemaDigest();
		const images = { ...receiptOf(run).images };
		const plan = planFor(run.host, run.entry, run.secrets);
		expect(plan.kind).toBe("install");
		expect(plan).not.toHaveProperty("hold");

		converge(run.host, manifestWith({ postiz: run.entry }), paths, { secretValues: run.secrets });

		expect(receiptOf(run)).toMatchObject({
			state: "applied",
			images,
			schemaDigest: run.entry.artifact.sha256,
		});
		expect(schemaConditions(run.host)).toEqual([]);
	});

	it("an announced transition from the schema digest is applied by the update path, not the converge", async () => {
		const { run, paths } = await withdrawnAtSchemaDigest();
		const schemaDigest = run.entry.artifact.sha256;
		const announced = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const newerReference = reference(announced.artifact.image, NEWER_APP_DIGEST);
		expect(planFor(run.host, announced, run.secrets).hold).toEqual({
			schemaDigest,
			pinned: NEWER_APP_DIGEST,
			covered: true,
		});
		traceAppSource(run);
		const initial = appSourceDigest(run.host);
		const from = run.events.length;

		converge(run.host, manifestWith({ postiz: announced }), paths, { secretValues: run.secrets });

		// The converge starts S, the digest that wrote the schema.
		expect(appSourceDigest(run.host)).toBe(schemaDigest);
		// Its install ready is answered as an operation mismatch (the installation runs an update) and settles ready.
		const scripted = new ScriptedReporter(run.events, {
			ready: refusedAnswer("companion_report_operation_mismatch", null),
		});
		run.reporter = scripted;
		expect(await runReadiness({ ...run, entry: announced })).toMatchObject({ outcome: "ready" });
		delete scripted.answers.ready;

		// The NEXT update pass carries the announced change out behind its own pre-update backup.
		expect(await settle(runUpdate(run, announced))).toMatchObject({ outcome: "ready" });

		const timeline = run.events.slice(from);
		const backupAt = timeline.indexOf("pgbackrest:backup --type=diff");
		expect(backupAt).toBeGreaterThan(-1);
		expect(timeline.slice(0, backupAt)).not.toContain(`app-source:${NEWER_APP_DIGEST}`);
		expect(startsWhileAppSourceIs(timeline, initial, NEWER_APP_DIGEST, backupAt)).toEqual([]);
		expectInOrder(timeline, [
			"pgbackrest:backup --type=diff",
			`app-source:${NEWER_APP_DIGEST}`,
			APP_RESTART,
			"report:ready",
		]);
		expect(receiptOf(run).images.app).toBe(newerReference);
		expect(receiptOf(run).schemaDigest).toBe(NEWER_APP_DIGEST);
		expect(schemaConditions(run.host)).toEqual([]);
	});

	it("an unannounced difference keeps the schema digest running and raises schema_digest_mismatch", async () => {
		const { run, paths } = await withdrawnAtSchemaDigest();
		const schemaDigest = run.entry.artifact.sha256;
		const unannounced = pinnedWithoutAnnouncement(run.entry, NEWER_APP_DIGEST);
		expect(planFor(run.host, unannounced, run.secrets).hold).toEqual({
			schemaDigest,
			pinned: NEWER_APP_DIGEST,
			covered: false,
		});

		converge(run.host, manifestWith({ postiz: unannounced }), paths, {
			secretValues: run.secrets,
		});

		expect(appSourceDigest(run.host)).toBe(schemaDigest);
		expect(receiptOf(run).state).toBe("applied");
		const raised = schemaConditions(run.host);
		expect(raised).toMatchObject([{ installationId: run.entry.installationId, state: "raised" }]);
		expect(String(raised[0]?.detail)).toContain(schemaDigest);
		expect(String(raised[0]?.detail)).toContain(NEWER_APP_DIGEST);
		const reporter = new RecordingReporter(run.events);
		const manifest = manifestWith({ postiz: unannounced });
		expect(await reportConditionsOver(run.host, manifest, reporter, run.secrets)).toEqual({
			outcome: "reported",
			sent: [{ code: "schema_digest_mismatch", state: "raised", result: "recorded" }],
		});
		expect((await reportConditionsOver(run.host, manifest, reporter, run.secrets)).outcome).toBe(
			"skipped",
		);
		expect(reporter.reports.filter((report) => report.phase === "condition")).toHaveLength(1);
	});

	it("a dependency-only difference on a withdrawn receipt is held and applied behind a backup, with no condition", async () => {
		const { run, paths } = await withdrawnAtSchemaDigest();
		const schemaDigest = run.entry.artifact.sha256;
		const heldPostgres = receiptOf(run).images.postgres;
		const dependencyOnly = withPostgresDigest(run.entry, NEWER_POSTGRES_DIGEST);
		const newerPostgres = reference(
			dependencyOnly.dependencies.postgres.image,
			NEWER_POSTGRES_DIGEST,
		);
		expect(planFor(run.host, dependencyOnly, run.secrets).hold).toEqual({
			schemaDigest,
			pinned: schemaDigest,
			covered: true,
		});

		converge(run.host, manifestWith({ postiz: dependencyOnly }), paths, {
			secretValues: run.secrets,
		});

		expect(receiptOf(run).images.postgres).toBe(heldPostgres);
		expect(run.host.sources().get(BACKUP_SOURCE)).toContain(heldPostgres ?? "?");
		expect(schemaConditions(run.host)).toEqual([]);
		expect(await runReadiness({ ...run, entry: dependencyOnly })).toMatchObject({
			outcome: "ready",
		});
		const from = run.events.length;
		expect(await settle(runUpdate(run, dependencyOnly))).toMatchObject({ outcome: "ready" });
		expectInOrder(run.events.slice(from), [
			"pgbackrest:backup --type=diff",
			`write:${BACKUP_SOURCE}`,
		]);
		expect(receiptOf(run).images.postgres).toBe(newerPostgres);
		expect(schemaConditions(run.host)).toEqual([]);
	});

	it("an applied receipt with a pending ready re-applied for a credential rotation keeps its pending update", async () => {
		const run = await readyForUpdate();
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		run.reporter = new ScriptedReporter(run.events, {
			ready: new Error("control plane unreachable"),
		});
		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "unreportable" });
		const before = receiptOf(run);
		const rotated = { ...run.secrets };
		rotated[run.entry.credentials.appEnvSecretRef] =
			`${rotated[run.entry.credentials.appEnvSecretRef]}API_LIMIT=91\n`;
		const plan = planFor(run.host, newer, rotated);
		expect(plan.kind).toBe("install");
		expect(plan).not.toHaveProperty("hold");
		// A re-apply over a running database: its data directory is populated, so nothing is
		// restored under it (plan 06-48 refuses to restore a backup whose schema another app wrote).
		retainData(run.host);

		applyContainerCompanion(plan, run.host, HOSTED_PATHS);

		const after = receiptOf(run);
		expect(after.credentialsSha256).not.toBe(before.credentialsSha256);
		expect(after.pendingUpdateReport).toEqual(before.pendingUpdateReport);
		expect(after.pendingUpdateRollback).toEqual(before.pendingUpdateRollback);
	});

	it("the condition clears once the announced update's ready settles, and the clear is reported once", async () => {
		const { run, paths } = await withdrawnAtSchemaDigest();
		const unannounced = pinnedWithoutAnnouncement(run.entry, NEWER_APP_DIGEST);
		converge(run.host, manifestWith({ postiz: unannounced }), paths, {
			secretValues: run.secrets,
		});
		const reporter = new RecordingReporter(run.events);
		await reportConditionsOver(
			run.host,
			manifestWith({ postiz: unannounced }),
			reporter,
			run.secrets,
		);
		expect(await runReadiness({ ...run, entry: unannounced })).toMatchObject({ outcome: "ready" });

		// The control plane announces exactly the change from what the data was written by.
		const announced = withAppDigest(run.entry, NEWER_APP_DIGEST);
		expect(await settle(runUpdate(run, announced))).toMatchObject({ outcome: "ready" });

		expect(schemaConditions(run.host)).toMatchObject([{ state: "cleared" }]);
		const manifest = manifestWith({ postiz: announced });
		expect(await reportConditionsOver(run.host, manifest, reporter, run.secrets)).toEqual({
			outcome: "reported",
			sent: [{ code: "schema_digest_mismatch", state: "cleared", result: "recorded" }],
		});
		expect((await reportConditionsOver(run.host, manifest, reporter, run.secrets)).outcome).toBe(
			"skipped",
		);
	});

	it("the condition clears when the pin returns to the schema digest", async () => {
		const { run, paths } = await withdrawnAtSchemaDigest();
		const unannounced = pinnedWithoutAnnouncement(run.entry, NEWER_APP_DIGEST);
		converge(run.host, manifestWith({ postiz: unannounced }), paths, {
			secretValues: run.secrets,
		});
		expect(schemaConditions(run.host)).toMatchObject([{ state: "raised" }]);

		converge(run.host, manifestWith({ postiz: run.entry }), paths, { secretValues: run.secrets });
		expect(await settle(runUpdate(run, run.entry))).toMatchObject({ outcome: "skipped" });

		expect(schemaConditions(run.host)).toMatchObject([{ state: "cleared" }]);
		const reporter = new RecordingReporter(run.events);
		const manifest = manifestWith({ postiz: run.entry });
		expect(await reportConditionsOver(run.host, manifest, reporter, run.secrets)).toMatchObject({
			outcome: "reported",
			sent: [{ code: "schema_digest_mismatch", state: "cleared" }],
		});
	});

	it("an unrequested app-digest change on the update path raises schema_digest_mismatch", async () => {
		const run = await readyForUpdate();
		const running = run.entry.artifact.sha256;
		const unannounced = pinnedWithoutAnnouncement(run.entry, NEWER_APP_DIGEST);

		expect(await settle(runUpdate(run, unannounced))).toMatchObject({ outcome: "unrequested" });

		const raised = schemaConditions(run.host);
		expect(raised).toMatchObject([{ state: "raised" }]);
		expect(String(raised[0]?.detail)).toContain(running);
		expect(String(raised[0]?.detail)).toContain(NEWER_APP_DIGEST);
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-48 (G-06-1, WR-02, D-14): the repository records which app wrote the schema of each
// backup, and a replaced instance restores and starts accordingly.
// ---------------------------------------------------------------------------------------------

const SCHEMA_ANNOTATION = "iforge-schema-digest";
const NEXT_SCHEMA_ANNOTATION = "iforge-schema-next";

/** The newest backup of a repository that outlived its instance, annotated as plan 06-48 writes it. */
function annotatedBackup(schemaDigest: string, nextSchemaDigest?: string): FakeBackup {
	return {
		...existingFullBackup(),
		annotation: {
			[SCHEMA_ANNOTATION]: schemaDigest,
			...(nextSchemaDigest !== undefined ? { [NEXT_SCHEMA_ANNOTATION]: nextSchemaDigest } : {}),
		},
	};
}

/**
 * A replacement instance: an empty data directory, no receipt and no marker, over a repository
 * that holds `backups`. The schema marker's writes are on the host's timeline.
 */
function replacementHost(backups: FakeBackup[]): {
	host: FakeContainerHost;
	pgbackrest: FakePgBackRest;
	events: string[];
} {
	const events: string[] = [];
	const pgbackrest = new FakePgBackRest(events, { stanza: true, backups });
	const host = new FakeContainerHost({ pgbackrest: pgbackrest.handle, events });
	traceSchemaMarker(host);
	return { host, pgbackrest, events };
}

function convergeReplacement(host: FakeContainerHost, entry: PostizCompanionManifest): void {
	converge(host, manifestWith({ postiz: entry }), convergePaths(), {
		secretValues: backupSecrets(entry),
	});
}

function restoreArgv(pgbackrest: FakePgBackRest): string[] {
	return pgbackrest.calls.filter((argv) => argv.includes("restore")).at(-1) ?? [];
}

function receiptOnHost(host: FakeContainerHost): Record<string, unknown> & {
	images: Record<string, string>;
} {
	return JSON.parse(host.readFile(HOSTED_PATHS.receiptPath) ?? "{}");
}

describe("postiz restore on a replaced instance follows the backup's schema digest (plan 06-48, WR-02)", () => {
	it("a replaced instance after a reverted pin restores the pre-update set and starts the previous version (WR-02)", () => {
		const entry = backupEntry();
		const previous = entry.artifact.sha256;
		const newest = annotatedBackup(previous, NEWER_APP_DIGEST);
		const { host, pgbackrest, events } = replacementHost([newest]);
		// The pin was reverted to X; nothing is announced; the host is new.
		expect(entry.update).toBeNull();

		convergeReplacement(host, entry);

		// The repository was asked first, then the pre-update set was restored to its own end.
		expectInOrder(events, ["pgbackrest:info", "pgbackrest:restore --type=immediate"]);
		expect(events).not.toContain("pgbackrest:restore --type=default");
		const restore = restoreArgv(pgbackrest);
		expect(restore).toContain(`--set=${newest.label}`);
		expect(restore).toContain("--type=immediate");
		expect(restore).toContain("--target-action=promote");
		// The data directory says X wrote the schema before the app could start on it.
		const markerAt = host.events.indexOf(`schema-marker:${previous}`);
		const startAt = host.events.indexOf(APP_START);
		expect(markerAt).toBeGreaterThan(-1);
		expect(startAt).toBeGreaterThan(markerAt);
		expect(host.events).not.toContain(`schema-marker:${NEWER_APP_DIGEST}`);
		// X runs -- never X over Y's end-of-archive schema, and never Y unannounced.
		const source = host.sources().get(APP_SOURCE) ?? "";
		expect(source).toContain(reference(entry.artifact.image, previous));
		expect(source).not.toContain(NEWER_APP_DIGEST);
		expect(receiptOnHost(host)).toMatchObject({
			state: "applied",
			schemaDigest: previous,
			restore: { label: newest.label },
		});
	});

	it("the same repository with the pin at the new version restores to the end of the archive and starts it", () => {
		const entry = pinnedWithoutAnnouncement(backupEntry(), NEWER_APP_DIGEST);
		const previous = backupEntry().artifact.sha256;
		const { host, pgbackrest, events } = replacementHost([
			annotatedBackup(previous, NEWER_APP_DIGEST),
		]);

		convergeReplacement(host, entry);

		expectInOrder(events, ["pgbackrest:info", "pgbackrest:restore --type=default"]);
		expect(events).not.toContain("pgbackrest:restore --type=immediate");
		expect(restoreArgv(pgbackrest).some((arg) => arg.startsWith("--set="))).toBe(false);
		const source = host.sources().get(APP_SOURCE) ?? "";
		expect(source).toContain(reference(entry.artifact.image, NEWER_APP_DIGEST));
		const markerAt = host.events.indexOf(`schema-marker:${NEWER_APP_DIGEST}`);
		expect(markerAt).toBeGreaterThan(-1);
		expect(host.events.indexOf(APP_START)).toBeGreaterThan(markerAt);
		expect(host.events).not.toContain(`schema-marker:${previous}`);
		expect(receiptOnHost(host)).toMatchObject({ schemaDigest: NEWER_APP_DIGEST });
	});
});

function annotateEvent(key: string, digest: string): string {
	return `pgbackrest:annotate ${key}=${digest}`;
}

function backupCalls(pgbackrest: FakePgBackRest): string[][] {
	return pgbackrest.calls.filter((argv) => argv.includes("backup"));
}

function annotateCalls(pgbackrest: FakePgBackRest): string[][] {
	return pgbackrest.calls.filter((argv) => argv.includes("annotate"));
}

/** The index of the last image pull or digest check in `timeline`, or -1. */
function lastImageStep(timeline: readonly string[]): number {
	let last = -1;
	for (const [index, event] of timeline.entries()) {
		if (event.startsWith("run:podman pull") || event.startsWith("run:podman image inspect")) {
			last = index;
		}
	}
	return last;
}

/** The new digest's schema push loses the organization; the restore brings it back. */
function failTheDataAssertion(run: BackupRun): void {
	const realRun = run.host.run.bind(run.host);
	run.host.run = (command, args, options) => {
		const result = realRun(command, args, options);
		if (command === "systemctl" && args[0] === "restart") run.postiz.organizations = 0;
		if (command === "podman" && args[0] === "run" && args.includes("restore")) {
			run.postiz.organizations = 1;
		}
		return result;
	};
}

describe("postiz backups carry the schema digest, and a swap records where it goes (plan 06-48, D-14)", () => {
	it("the first full, the scheduled and the pre-update backup are each annotated with the schema digest", async () => {
		const run = await readyForUpdate();
		const schema = run.entry.artifact.sha256;
		const annotation = `--annotation=${SCHEMA_ANNOTATION}=${schema}`;
		const [first] = backupCalls(run.pgbackrest);
		expect(first).toContain("--type=full");
		expect(first).toContain(annotation);

		const scheduled = await maintain(run, new Date(INSTALLED_AT + 25 * HOUR_MS));
		expect(scheduled.outcome).toBe("backed-up");
		expect(backupCalls(run.pgbackrest).at(-1)).toContain(annotation);

		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "ready" });
		const preUpdate = backupCalls(run.pgbackrest).at(-1) ?? [];
		expect(preUpdate).toContain(annotation);
		expect(backupCalls(run.pgbackrest)).toHaveLength(3);

		// The repository reports every one of them back with the digest that wrote its schema.
		const info = run.pgbackrest.handle([
			"exec",
			`--stanza=${BACKUP_STANZA}`,
			"info",
			"--output=json",
		]);
		const listed = containerCompanionModule.parsePgBackRestInfo(info.stdout, BACKUP_STANZA);
		expect(listed.backups.map((backup) => backup.schemaDigest)).toEqual([schema, schema, schema]);
	});

	it("the swap is preceded by the next-schema annotation", async () => {
		const run = await readyForUpdate();
		traceSchemaMarker(run.host);
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const from = run.events.length;

		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "ready" });

		const label = preUpdateLabel(run);
		const timeline = run.events.slice(from);
		const annotate = annotateEvent(NEXT_SCHEMA_ANNOTATION, NEWER_APP_DIGEST);
		expectInOrder(timeline, [
			"pgbackrest:backup --type=diff",
			annotate,
			`schema-marker:${NEWER_APP_DIGEST}`,
			`write:${APP_SOURCE}`,
			APP_RESTART,
		]);
		// After every pull, before anything is rendered or restarted.
		expect(timeline.indexOf(annotate)).toBeGreaterThan(lastImageStep(timeline));
		expect(annotateCalls(run.pgbackrest)).toEqual([
			expect.arrayContaining([
				"exec",
				"annotate",
				`--set=${label}`,
				`--annotation=${NEXT_SCHEMA_ANNOTATION}=${NEWER_APP_DIGEST}`,
			]),
		]);
		const preUpdate = run.pgbackrest.backups.find((backup) => backup.label === label);
		expect(preUpdate?.annotation).toEqual({
			[SCHEMA_ANNOTATION]: run.entry.artifact.sha256,
			[NEXT_SCHEMA_ANNOTATION]: NEWER_APP_DIGEST,
		});
	});

	it("a dependency-only update runs no annotate", async () => {
		const run = await readyForUpdate();
		const from = run.events.length;

		await settle(runUpdate(run, withPostgresDigest(run.entry, NEWER_POSTGRES_DIGEST)));

		const timeline = run.events.slice(from);
		expect(timeline).toContain("pgbackrest:backup --type=diff");
		expect(timeline.filter((event) => event.startsWith("pgbackrest:annotate"))).toEqual([]);
	});

	it("a failed next-schema annotation refuses the update before anything restarts", async () => {
		const run = await readyForUpdate();
		traceSchemaMarker(run.host);
		run.pgbackrest.failAnnotate = () => true;
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const previousReference = reference(run.entry.artifact.image, run.entry.artifact.sha256);
		const markerBefore = run.host.readFile(SCHEMA_DIGEST_MARKER_PATH);
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({ outcome: "failed", step: "pre-update" });
		const label = preUpdateLabel(run);
		const timeline = run.events.slice(from);
		expect(timeline).toContain(annotateEvent(NEXT_SCHEMA_ANNOTATION, NEWER_APP_DIGEST));
		expect(timeline.filter((event) => event.startsWith("schema-marker:"))).toEqual([]);
		expect(timeline.filter((event) => event.startsWith(QUADLET_WRITE_PREFIX))).toEqual([]);
		expect(timeline.filter((event) => /^run:systemctl (start|restart|stop) /.test(event))).toEqual(
			[],
		);
		expect(run.host.readFile(SCHEMA_DIGEST_MARKER_PATH)).toBe(markerBefore);
		expect(run.host.sources().get(APP_SOURCE)).toContain(previousReference);
		expect(run.reporter.last()).toMatchObject({
			operation: "update",
			phase: "failed",
			step: "pre-update",
		});
		const receipt = receiptOf(run);
		expect(receipt.images.app).toBe(previousReference);
		expect(receipt.refusedUpdate).toMatchObject({ step: "pre-update", label });
		expect(receipt.pendingUpdateRollback).toBeUndefined();
	});

	it.each([
		["a refused ready", "rolled-back"],
		["a post-update assertion", "failed"],
		["the incomplete-rollback repair", "repaired"],
	] as const)(
		"a completed rollback after %s resets the next-schema annotation",
		async (path, expected) => {
			let run: BackupRun;
			let from: number;
			let outcome: unknown;
			if (path === "the incomplete-rollback repair") {
				const refused = await refusedWithFailingRestore();
				run = refused.run;
				ageLastAttempt(run, 31);
				refused.failing.restore = false;
				// The failed first attempt never reset anything.
				expect(
					run.events.filter(
						(event) => event === annotateEvent(NEXT_SCHEMA_ANNOTATION, run.entry.artifact.sha256),
					),
				).toEqual([]);
				from = run.events.length;
				outcome = await settle(runUpdate(run, run.entry));
			} else {
				run = await readyForUpdate();
				if (path === "a refused ready") {
					run.reporter = new ScriptedReporter(run.events, { ready: REFUSED_AS_FAILED });
				} else {
					failTheDataAssertion(run);
				}
				from = run.events.length;
				outcome = await settle(runUpdate(run, withAppDigest(run.entry, NEWER_APP_DIGEST)));
			}

			expect(outcome).toMatchObject({ outcome: expected });
			const label = preUpdateLabel(run);
			const previous = run.entry.artifact.sha256;
			const reset = annotateEvent(NEXT_SCHEMA_ANNOTATION, previous);
			expectInOrder(run.events.slice(from), [
				"pgbackrest:restore --type=immediate",
				APP_START_PREVIOUS,
				reset,
			]);
			expect(annotateCalls(run.pgbackrest).at(-1)).toEqual(
				expect.arrayContaining([
					"exec",
					"annotate",
					`--set=${label}`,
					`--annotation=${NEXT_SCHEMA_ANNOTATION}=${previous}`,
				]),
			);
			const preUpdate = run.pgbackrest.backups.find((backup) => backup.label === label);
			expect(preUpdate?.annotation?.[NEXT_SCHEMA_ANNOTATION]).toBe(previous);
			expect(receiptOf(run).rollbackIncomplete).toBeUndefined();
		},
	);

	it("a rollback whose annotation reset fails still completes, and its sentence says so", async () => {
		const run = await readyForUpdate();
		const previous = run.entry.artifact.sha256;
		const previousReference = reference(run.entry.artifact.image, previous);
		failTheDataAssertion(run);
		run.pgbackrest.failAnnotate = (argv) =>
			argv.includes(`--annotation=${NEXT_SCHEMA_ANNOTATION}=${previous}`);

		const outcome = await settle(runUpdate(run, withAppDigest(run.entry, NEWER_APP_DIGEST)));

		expect(outcome).toMatchObject({ outcome: "failed", step: "post-update-assertion" });
		const receipt = receiptOf(run);
		expect(receipt.images.app).toBe(previousReference);
		expect(receipt.rollbackIncomplete).toBeUndefined();
		const last = run.reporter.last();
		const error = last?.phase === "failed" ? last.error : "";
		expect(error).toContain("previous version passed");
		expect(error).toContain("not reset");
	});

	it("parsePgBackRestInfo reads the two digests per backup and treats anything but 64 lowercase hex as absent", () => {
		const schema = "a".repeat(64);
		const next = "b".repeat(64);
		const backup = (label: string, type: string, annotation?: unknown) => ({
			label,
			type,
			timestamp: { start: 1, stop: 2 },
			...(annotation !== undefined ? { annotation } : {}),
		});
		const stdout = JSON.stringify([
			{
				name: BACKUP_STANZA,
				status: { code: 0 },
				backup: [
					backup("20260923-110000F", "full", {
						[SCHEMA_ANNOTATION]: schema,
						[NEXT_SCHEMA_ANNOTATION]: next,
					}),
					backup("20260923-110000F_20260923-120000D", "diff", {
						[SCHEMA_ANNOTATION]: schema.toUpperCase(),
						[NEXT_SCHEMA_ANNOTATION]: "b".repeat(63),
					}),
					backup("20260923-110000F_20260923-130000D", "diff", {
						[SCHEMA_ANNOTATION]: 42,
						[NEXT_SCHEMA_ANNOTATION]: null,
					}),
					backup("20260923-110000F_20260923-140000I", "incr", ["not", "an", "object"]),
					backup("20260923-110000F_20260923-150000I", "incr"),
				],
			},
		]);

		const info = containerCompanionModule.parsePgBackRestInfo(stdout, BACKUP_STANZA);

		expect(info.backups.map((item) => [item.schemaDigest, item.nextSchemaDigest])).toEqual([
			[schema, next],
			[null, null],
			[null, null],
			[null, null],
			[null, null],
		]);
	});
});

const SCHEMA_X = "1".repeat(64);
const SCHEMA_Y = "2".repeat(64);
const SCHEMA_Z = "3".repeat(64);
const NEWEST_LABEL = "20260923-110000F_20260923-113000D";

function newestBackup(schemaDigest: string | null, nextSchemaDigest: string | null = null) {
	return {
		label: NEWEST_LABEL,
		type: "diff" as const,
		stoppedAt: INSTALLED_AT - HOUR_MS,
		dbBytes: 51_000_000,
		schemaDigest,
		nextSchemaDigest,
	};
}

function pinnedAt(
	sha256: string,
	update: { fromDigest: string; toDigest: string } | null = null,
): { artifact: { sha256: string }; update: { fromDigest: string; toDigest: string } | null } {
	return { artifact: { sha256 }, update };
}

describe("decidePostizRestore: the replacement host's rule table (plan 06-48, WR-02)", () => {
	const decide = containerCompanionModule.decidePostizRestore;

	it("rule 1: no backup is a fresh start", () => {
		expect(decide(undefined, pinnedAt(SCHEMA_X))).toEqual({ mode: "fresh" });
	});

	it("rule 2: an unannotated newest backup restores to the end of the archive at the pin", () => {
		expect(decide(newestBackup(null, SCHEMA_Y), pinnedAt(SCHEMA_X))).toEqual({
			mode: "archive-end",
			label: NEWEST_LABEL,
			appDigest: SCHEMA_X,
			covered: true,
		});
	});

	it("rule 3: a pending swap with the pin back at the schema digest restores the set at its own point", () => {
		expect(decide(newestBackup(SCHEMA_X, SCHEMA_Y), pinnedAt(SCHEMA_X))).toEqual({
			mode: "set-immediate",
			label: NEWEST_LABEL,
			appDigest: SCHEMA_X,
			covered: true,
		});
	});

	it("rule 3: a pending swap the entry still announces restores the set at its own point, and the update path redoes it", () => {
		const announced = { fromDigest: SCHEMA_X, toDigest: SCHEMA_Y };
		expect(decide(newestBackup(SCHEMA_X, SCHEMA_Y), pinnedAt(SCHEMA_Y, announced))).toEqual({
			mode: "set-immediate",
			label: NEWEST_LABEL,
			appDigest: SCHEMA_X,
			covered: true,
		});
	});

	it("rule 4: the end of the archive written by the pin restores to the end at the pin", () => {
		expect(decide(newestBackup(SCHEMA_X), pinnedAt(SCHEMA_X))).toEqual({
			mode: "archive-end",
			label: NEWEST_LABEL,
			appDigest: SCHEMA_X,
			covered: true,
		});
		// A swap to the pin that nobody announces any more: it completed, and the pin is its digest.
		expect(decide(newestBackup(SCHEMA_X, SCHEMA_Y), pinnedAt(SCHEMA_Y))).toEqual({
			mode: "archive-end",
			label: NEWEST_LABEL,
			appDigest: SCHEMA_Y,
			covered: true,
		});
	});

	it("rule 5: an announced change from the end of the archive to the pin restores at the archive's digest, covered", () => {
		expect(
			decide(
				newestBackup(SCHEMA_X),
				pinnedAt(SCHEMA_Y, { fromDigest: SCHEMA_X, toDigest: SCHEMA_Y }),
			),
		).toEqual({ mode: "archive-end", label: NEWEST_LABEL, appDigest: SCHEMA_X, covered: true });
		expect(
			decide(
				newestBackup(SCHEMA_X, SCHEMA_Y),
				pinnedAt(SCHEMA_Z, { fromDigest: SCHEMA_Y, toDigest: SCHEMA_Z }),
			),
		).toEqual({ mode: "archive-end", label: NEWEST_LABEL, appDigest: SCHEMA_Y, covered: true });
	});

	it("rule 6: anything else restores at the archive's digest, not covered", () => {
		expect(decide(newestBackup(SCHEMA_X), pinnedAt(SCHEMA_Y))).toEqual({
			mode: "archive-end",
			label: NEWEST_LABEL,
			appDigest: SCHEMA_X,
			covered: false,
		});
		// An announcement of some other transition covers nothing.
		expect(
			decide(
				newestBackup(SCHEMA_X, SCHEMA_Y),
				pinnedAt(SCHEMA_Z, { fromDigest: SCHEMA_X, toDigest: SCHEMA_Z }),
			),
		).toEqual({ mode: "archive-end", label: NEWEST_LABEL, appDigest: SCHEMA_Y, covered: false });
	});

	it("a next digest equal to the schema digest is no pending change", () => {
		expect(decide(newestBackup(SCHEMA_X, SCHEMA_X), pinnedAt(SCHEMA_X))).toEqual({
			mode: "archive-end",
			label: NEWEST_LABEL,
			appDigest: SCHEMA_X,
			covered: true,
		});
		expect(decide(newestBackup(SCHEMA_X, SCHEMA_X), pinnedAt(SCHEMA_Y))).toEqual({
			mode: "archive-end",
			label: NEWEST_LABEL,
			appDigest: SCHEMA_X,
			covered: false,
		});
	});
});

/** A replacement instance with a Postiz that answers, so readiness and updates can run over it. */
function replacementRun(entry: PostizCompanionManifest, backups: FakeBackup[]): BackupRun {
	const events: string[] = [];
	const postiz = new FakePostiz(events, { organizations: 1 });
	const pgbackrest = new FakePgBackRest(events, { stanza: true, backups });
	const host = new FakeContainerHost({ psql: postiz.psql, pgbackrest: pgbackrest.handle, events });
	return {
		entry,
		secrets: backupSecrets(entry),
		host,
		postiz,
		pgbackrest,
		reporter: new RecordingReporter(events),
		events,
	};
}

describe("a replaced instance starts the digest that wrote the restored data (plan 06-48, WR-02)", () => {
	it("a restore whose schema digest is announced to the pin restores that digest and hands the change to the update path", async () => {
		const previous = backupEntry().artifact.sha256;
		const announced = withAppDigest(backupEntry(), NEWER_APP_DIGEST);
		const run = replacementRun(announced, [annotatedBackup(previous)]);
		traceAppSource(run);
		const paths = convergePaths();

		converge(run.host, manifestWith({ postiz: announced }), paths, { secretValues: run.secrets });

		// The end of the archive, under the app that wrote it; nothing held against an announcement.
		expect(run.events).toContain("pgbackrest:restore --type=default");
		expect(appSourceDigest(run.host)).toBe(previous);
		expect(receiptOf(run).schemaDigest).toBe(previous);
		expect(schemaConditions(run.host)).toEqual([]);
		// The restored installation reports ready at the digest it runs (its install is recorded).
		const scripted = new ScriptedReporter(run.events, {
			ready: refusedAnswer("companion_report_operation_mismatch", null),
		});
		run.reporter = scripted;
		expect(await runReadiness(run)).toMatchObject({ outcome: "ready" });
		delete scripted.answers.ready;
		const from = run.events.length;

		// The next update pass carries the announced change out behind its own backup.
		expect(await settle(runUpdate(run, announced))).toMatchObject({ outcome: "ready" });

		const timeline = run.events.slice(from);
		expectInOrder(timeline, [
			"pgbackrest:backup --type=diff",
			annotateEvent(NEXT_SCHEMA_ANNOTATION, NEWER_APP_DIGEST),
			`app-source:${NEWER_APP_DIGEST}`,
			APP_RESTART,
			"report:ready",
		]);
		expect(appSourceDigest(run.host)).toBe(NEWER_APP_DIGEST);
		expect(receiptOf(run).schemaDigest).toBe(NEWER_APP_DIGEST);
		expect(schemaConditions(run.host)).toEqual([]);
	});

	it("a restore whose schema digest differs from the pin without an announcement runs that digest and reports schema_digest_mismatch", async () => {
		const previous = backupEntry().artifact.sha256;
		const unannounced = pinnedWithoutAnnouncement(backupEntry(), NEWER_APP_DIGEST);
		const run = replacementRun(unannounced, [annotatedBackup(previous)]);

		converge(run.host, manifestWith({ postiz: unannounced }), convergePaths(), {
			secretValues: run.secrets,
		});

		expect(run.events).toContain("pgbackrest:restore --type=default");
		expect(appSourceDigest(run.host)).toBe(previous);
		const raised = schemaConditions(run.host);
		expect(raised).toMatchObject([{ state: "raised" }]);
		expect(String(raised[0]?.detail)).toContain(previous);
		expect(String(raised[0]?.detail)).toContain(NEWER_APP_DIGEST);
		// Sent once.
		const reporter = new RecordingReporter([]);
		const manifest = manifestWith({ postiz: unannounced });
		expect(await reportConditionsOver(run.host, manifest, reporter, run.secrets)).toMatchObject({
			outcome: "reported",
			sent: [{ code: "schema_digest_mismatch", state: "raised" }],
		});
		expect((await reportConditionsOver(run.host, manifest, reporter, run.secrets)).outcome).toBe(
			"skipped",
		);
	});

	it("an unfinished earlier restore with an annotated newest backup is re-decided and restored with --delta", () => {
		const entry = backupEntry();
		const newest = annotatedBackup(entry.artifact.sha256, NEWER_APP_DIGEST);
		const { host, pgbackrest, events } = replacementHost([newest]);
		// A partial restore: PGDATA exists, and so does the marker that says it is not finished.
		host.files.set(RESTORE_INCOMPLETE_MARKER, {
			content: `${newest.label}\n`,
			mode: 0o600,
			uid: 0,
			gid: 0,
		});
		host.files.set(PGDATA_VERSION_FILE, {
			content: "18\n",
			mode: 0o600,
			uid: 1_000_065_536 + 999,
			gid: 1_000_065_536 + 999,
		});

		convergeReplacement(host, entry);

		expectInOrder(events, ["pgbackrest:info", "pgbackrest:restore --type=immediate"]);
		const restore = restoreArgv(pgbackrest);
		for (const arg of ["--delta", `--set=${newest.label}`, "--type=immediate"]) {
			expect(restore).toContain(arg);
		}
		expect(host.files.has(RESTORE_INCOMPLETE_MARKER)).toBe(false);
		expect(host.sources().get(APP_SOURCE)).toContain(
			reference(entry.artifact.image, entry.artifact.sha256),
		);
	});
});

// ---------------------------------------------------------------------------------------------
// G-06-5 (review IN-03): the hold is keyed on the data directory, not on the receipt. A receipt
// that is absent, corrupt or written by a newer CLI parses to nothing, and over populated data the
// schema marker alone decides which app digest may start.
// ---------------------------------------------------------------------------------------------

describe("the hold survives a missing or unreadable receipt (G-06-5, review IN-03)", () => {
	it("an absent receipt over populated data marked S keeps S and raises schema_digest_mismatch (repro A)", async () => {
		const { run, paths } = await withdrawnAtSchemaDigest();
		const schemaDigest = run.entry.artifact.sha256;
		run.host.files.delete(HOSTED_PATHS.receiptPath);
		const unannounced = pinnedWithoutAnnouncement(run.entry, NEWER_APP_DIGEST);
		const plan = planFor(run.host, unannounced, run.secrets);
		expect(plan.kind).toBe("install");
		expect(plan.hold).toEqual({ schemaDigest, pinned: NEWER_APP_DIGEST, covered: false });
		traceSchemaMarker(run.host);
		traceAppSource(run);
		const initial = appSourceDigest(run.host);
		const from = run.events.length;

		converge(run.host, manifestWith({ postiz: unannounced }), paths, {
			secretValues: run.secrets,
		});

		const timeline = run.events.slice(from);
		expect(timeline).not.toContain(`schema-marker:${NEWER_APP_DIGEST}`);
		expect(startsWhileAppSourceIs(timeline, initial, NEWER_APP_DIGEST, timeline.length)).toEqual(
			[],
		);
		expect(appSourceDigest(run.host)).toBe(schemaDigest);
		expect(run.host.readFile(SCHEMA_DIGEST_MARKER_PATH)).toBe(`${schemaDigest}\n`);
		expect(receiptOf(run)).toMatchObject({ state: "applied", schemaDigest });
		const raised = schemaConditions(run.host);
		expect(raised).toMatchObject([{ installationId: run.entry.installationId, state: "raised" }]);
		expect(String(raised[0]?.detail)).toContain(schemaDigest);
		expect(String(raised[0]?.detail)).toContain(NEWER_APP_DIGEST);
	});

	it("a corrupt receipt over running units at S keeps S with a plain start and raises schema_digest_mismatch (repro B)", async () => {
		const run = await readyForUpdate();
		retainData(run.host);
		const schemaDigest = run.entry.artifact.sha256;
		replaceReceipt(run.host, "{ not json");
		const unannounced = pinnedWithoutAnnouncement(run.entry, NEWER_APP_DIGEST);
		expect(planFor(run.host, unannounced, run.secrets).hold).toEqual({
			schemaDigest,
			pinned: NEWER_APP_DIGEST,
			covered: false,
		});
		traceSchemaMarker(run.host);
		traceAppSource(run);
		const initial = appSourceDigest(run.host);
		const from = run.events.length;

		converge(run.host, manifestWith({ postiz: unannounced }), convergePaths(), {
			secretValues: run.secrets,
		});

		const timeline = run.events.slice(from);
		const appVerbs = timeline.filter(isAppStartOrRestart);
		expect(appVerbs.length).toBeGreaterThan(0);
		expect(appVerbs.every((event) => event.startsWith("run:systemctl start "))).toBe(true);
		expect(timeline).not.toContain(`schema-marker:${NEWER_APP_DIGEST}`);
		expect(startsWhileAppSourceIs(timeline, initial, NEWER_APP_DIGEST, timeline.length)).toEqual(
			[],
		);
		expect(run.host.readFile(SCHEMA_DIGEST_MARKER_PATH)).toBe(`${schemaDigest}\n`);
		expect(appSourceDigest(run.host)).toBe(schemaDigest);
		const raised = schemaConditions(run.host);
		expect(raised).toMatchObject([{ state: "raised" }]);
		expect(String(raised[0]?.detail)).toContain(schemaDigest);
		expect(String(raised[0]?.detail)).toContain(NEWER_APP_DIGEST);
	});

	it("a receipt written by a newer CLI reads as no receipt and holds at S exactly as an absent one (repro C)", async () => {
		const run = await readyForUpdate();
		retainData(run.host);
		const schemaDigest = run.entry.artifact.sha256;
		replaceReceipt(
			run.host,
			JSON.stringify({ ...receiptOf(run), schema: "iforge.containerCompanionApplied.v99" }),
		);
		expect(readContainerCompanionFacts(run.host, HOSTED_PATHS).receipt).toBeNull();
		const unannounced = pinnedWithoutAnnouncement(run.entry, NEWER_APP_DIGEST);

		const plan = planFor(run.host, unannounced, run.secrets);

		expect(plan.kind).toBe("install");
		expect(plan.hold).toEqual({ schemaDigest, pinned: NEWER_APP_DIGEST, covered: false });
		expect(plan.images.find((image) => image.slot === "app")?.sha256).toBe(schemaDigest);
	});

	it("an absent receipt with the change from S announced is held at S with no condition, and the next update pass carries it out behind a backup", async () => {
		const { run, paths } = await withdrawnAtSchemaDigest();
		const schemaDigest = run.entry.artifact.sha256;
		run.host.files.delete(HOSTED_PATHS.receiptPath);
		const announced = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const newerReference = reference(announced.artifact.image, NEWER_APP_DIGEST);
		expect(planFor(run.host, announced, run.secrets).hold).toEqual({
			schemaDigest,
			pinned: NEWER_APP_DIGEST,
			covered: true,
		});
		traceAppSource(run);
		const initial = appSourceDigest(run.host);
		const from = run.events.length;

		converge(run.host, manifestWith({ postiz: announced }), paths, { secretValues: run.secrets });

		expect(appSourceDigest(run.host)).toBe(schemaDigest);
		expect(schemaConditions(run.host)).toEqual([]);
		const scripted = new ScriptedReporter(run.events, {
			ready: refusedAnswer("companion_report_operation_mismatch", null),
		});
		run.reporter = scripted;
		expect(await runReadiness({ ...run, entry: announced })).toMatchObject({ outcome: "ready" });
		delete scripted.answers.ready;

		expect(await settle(runUpdate(run, announced))).toMatchObject({ outcome: "ready" });

		const timeline = run.events.slice(from);
		const backupAt = timeline.indexOf("pgbackrest:backup --type=diff");
		expect(backupAt).toBeGreaterThan(-1);
		expect(timeline.slice(0, backupAt)).not.toContain(`app-source:${NEWER_APP_DIGEST}`);
		expect(startsWhileAppSourceIs(timeline, initial, NEWER_APP_DIGEST, backupAt)).toEqual([]);
		expectInOrder(timeline, [
			"pgbackrest:backup --type=diff",
			`app-source:${NEWER_APP_DIGEST}`,
			APP_RESTART,
			"report:ready",
		]);
		expect(receiptOf(run).images.app).toBe(newerReference);
		expect(receiptOf(run).schemaDigest).toBe(NEWER_APP_DIGEST);
		expect(schemaConditions(run.host)).toEqual([]);
	});

	it("an absent receipt whose marker equals the pin is a normal install: no hold, no condition", async () => {
		const { run, paths } = await withdrawnAtSchemaDigest();
		const images = { ...receiptOf(run).images };
		run.host.files.delete(HOSTED_PATHS.receiptPath);
		const plan = planFor(run.host, run.entry, run.secrets);
		expect(plan.kind).toBe("install");
		expect(plan).not.toHaveProperty("hold");

		converge(run.host, manifestWith({ postiz: run.entry }), paths, { secretValues: run.secrets });

		expect(receiptOf(run)).toMatchObject({
			state: "applied",
			images,
			schemaDigest: run.entry.artifact.sha256,
		});
		expect(schemaConditions(run.host)).toEqual([]);
	});

	it("a corrupt receipt over running units keeps PostgreSQL at the digest its generated unit on disk names, with no condition", async () => {
		const run = await readyForUpdate();
		retainData(run.host);
		const schemaDigest = run.entry.artifact.sha256;
		const heldPostgres = receiptOf(run).images.postgres ?? "?";
		replaceReceipt(run.host, "{ not json");
		const dependencyOnly = withPostgresDigest(run.entry, NEWER_POSTGRES_DIGEST);
		const plan = planFor(run.host, dependencyOnly, run.secrets);
		expect(plan.hold).toEqual({ schemaDigest, pinned: schemaDigest, covered: true });
		expect(plan.images.find((image) => image.slot === "postgres")?.reference).toBe(heldPostgres);

		converge(run.host, manifestWith({ postiz: dependencyOnly }), convergePaths(), {
			secretValues: run.secrets,
		});

		expect(receiptOf(run).images.postgres).toBe(heldPostgres);
		expect(run.host.sources().get(BACKUP_SOURCE)).toContain(heldPostgres);
		expect(run.host.sources().get(BACKUP_SOURCE)).not.toContain(NEWER_POSTGRES_DIGEST);
		expect(schemaConditions(run.host)).toEqual([]);
	});

	it("an absent receipt with the units withdrawn takes PostgreSQL from the entry pin, because no unit is on disk", async () => {
		const { run } = await withdrawnAtSchemaDigest();
		expect(run.host.sources().get(BACKUP_SOURCE)).toBeUndefined();
		run.host.files.delete(HOSTED_PATHS.receiptPath);
		const dependencyOnly = withPostgresDigest(run.entry, NEWER_POSTGRES_DIGEST);

		const plan = planFor(run.host, dependencyOnly, run.secrets);

		expect(plan).not.toHaveProperty("hold");
		expect(plan.images.find((image) => image.slot === "postgres")?.sha256).toBe(
			NEWER_POSTGRES_DIGEST,
		);
	});

	it("a generated unit that names another repository is never read back: the entry pin is used", async () => {
		const run = await readyForUpdate();
		retainData(run.host);
		const onDisk = receiptOf(run).images.postgres ?? "?";
		const foreign = `docker.io/library/postgres@sha256:${digestOfReference(onDisk)}`;
		const source = run.host.files.get(BACKUP_SOURCE);
		if (source === undefined) throw new Error("the database unit is not on disk");
		run.host.files.set(BACKUP_SOURCE, {
			...source,
			content: source.content.replace(`Image=${onDisk}`, `Image=${foreign}`),
		});
		expect(run.host.sources().get(BACKUP_SOURCE)).toContain(`Image=${foreign}`);
		replaceReceipt(run.host, "{ not json");
		const dependencyOnly = withPostgresDigest(run.entry, NEWER_POSTGRES_DIGEST);

		const plan = planFor(run.host, dependencyOnly, run.secrets);

		expect(plan).not.toHaveProperty("hold");
		const postgres = plan.images.find((image) => image.slot === "postgres");
		expect(postgres?.sha256).toBe(NEWER_POSTGRES_DIGEST);
		expect(postgres?.reference).not.toContain("docker.io/library/postgres");
	});

	it("an interrupted swap whose receipt was lost keeps Y and never starts X over Y's schema (repro D)", async () => {
		const { run } = await interruptedSwap();
		retainData(run.host);
		const previousDigest = run.entry.artifact.sha256;
		expect(run.host.readFile(SCHEMA_DIGEST_MARKER_PATH)).toBe(`${NEWER_APP_DIGEST}\n`);
		run.host.files.delete(HOSTED_PATHS.receiptPath);
		// The pin reverted to X, and nothing is announced.
		expect(run.entry.update).toBeNull();
		expect(planFor(run.host, run.entry, run.secrets).hold).toEqual({
			schemaDigest: NEWER_APP_DIGEST,
			pinned: previousDigest,
			covered: false,
		});
		traceSchemaMarker(run.host);
		traceAppSource(run);
		const initial = appSourceDigest(run.host);
		const from = run.events.length;

		converge(run.host, manifestWith({ postiz: run.entry }), convergePaths(), {
			secretValues: run.secrets,
		});

		const timeline = run.events.slice(from);
		expect(timeline).not.toContain(`schema-marker:${previousDigest}`);
		expect(startsWhileAppSourceIs(timeline, initial, previousDigest, timeline.length)).toEqual([]);
		expect(run.host.readFile(SCHEMA_DIGEST_MARKER_PATH)).toBe(`${NEWER_APP_DIGEST}\n`);
		expect(appSourceDigest(run.host)).toBe(NEWER_APP_DIGEST);
		const raised = schemaConditions(run.host);
		expect(raised).toMatchObject([{ state: "raised" }]);
		expect(String(raised[0]?.detail)).toContain(NEWER_APP_DIGEST);
		expect(String(raised[0]?.detail)).toContain(previousDigest);
	});

	it("an empty data directory with a stale marker and a backup is decided by the repository, never held on the marker", () => {
		const entry = backupEntry();
		const schemaDigest = entry.artifact.sha256;
		const run = replacementRun(entry, [annotatedBackup(schemaDigest)]);
		writeSchemaMarker(run.host, FOREIGN_APP_DIGEST);
		expect(readContainerCompanionFacts(run.host, HOSTED_PATHS).dataPopulated).toBe(false);
		expect(planFor(run.host, entry, run.secrets)).not.toHaveProperty("hold");
		traceSchemaMarker(run.host);
		traceAppSource(run);

		converge(run.host, manifestWith({ postiz: entry }), convergePaths(), {
			secretValues: run.secrets,
		});

		expectInOrder(run.events, ["pgbackrest:info", "pgbackrest:restore --type=default"]);
		expect(run.events).not.toContain(`schema-marker:${FOREIGN_APP_DIGEST}`);
		expect(run.events).not.toContain(`app-source:${FOREIGN_APP_DIGEST}`);
		expect(appSourceDigest(run.host)).toBe(schemaDigest);
		expect(run.host.readFile(SCHEMA_DIGEST_MARKER_PATH)).toBe(`${schemaDigest}\n`);
		expect(receiptOf(run)).toMatchObject({ state: "applied", schemaDigest });
		expect(schemaConditions(run.host)).toEqual([]);
	});

	it("an empty data directory with a stale marker and nothing to restore starts the pin and rewrites the marker to it", () => {
		const entry = backupEntry();
		const pinned = entry.artifact.sha256;
		const run = replacementRun(entry, []);
		writeSchemaMarker(run.host, FOREIGN_APP_DIGEST);
		traceSchemaMarker(run.host);
		traceAppSource(run);

		converge(run.host, manifestWith({ postiz: entry }), convergePaths(), {
			secretValues: run.secrets,
		});

		expect(run.events.some((event) => event.startsWith("pgbackrest:restore"))).toBe(false);
		expect(run.events).not.toContain(`app-source:${FOREIGN_APP_DIGEST}`);
		expect(appSourceDigest(run.host)).toBe(pinned);
		expect(run.host.readFile(SCHEMA_DIGEST_MARKER_PATH)).toBe(`${pinned}\n`);
		expect(receiptOf(run)).toMatchObject({ state: "applied", schemaDigest: pinned });
		expect(schemaConditions(run.host)).toEqual([]);
	});

	it("an unfinished restore under a stale marker is not populated data: the restore path re-decides it", () => {
		const entry = backupEntry();
		const newest = annotatedBackup(entry.artifact.sha256, NEWER_APP_DIGEST);
		const { host, pgbackrest, events } = replacementHost([newest]);
		host.files.set(RESTORE_INCOMPLETE_MARKER, {
			content: `${newest.label}\n`,
			mode: 0o600,
			uid: 0,
			gid: 0,
		});
		host.files.set(PGDATA_VERSION_FILE, {
			content: "18\n",
			mode: 0o600,
			uid: 1_000_065_536 + 999,
			gid: 1_000_065_536 + 999,
		});
		writeSchemaMarker(host, FOREIGN_APP_DIGEST);
		expect(readContainerCompanionFacts(host, HOSTED_PATHS).dataPopulated).toBe(false);
		expect(planFor(host, entry, backupSecrets(entry))).not.toHaveProperty("hold");

		convergeReplacement(host, entry);

		expectInOrder(events, ["pgbackrest:info", "pgbackrest:restore --type=immediate"]);
		for (const arg of ["--delta", `--set=${newest.label}`, "--type=immediate"]) {
			expect(restoreArgv(pgbackrest)).toContain(arg);
		}
		expect(host.files.has(RESTORE_INCOMPLETE_MARKER)).toBe(false);
		expect(host.readFile(SCHEMA_DIGEST_MARKER_PATH)).toBe(`${entry.artifact.sha256}\n`);
		expect(host.sources().get(APP_SOURCE)).toContain(
			reference(entry.artifact.image, entry.artifact.sha256),
		);
		expect(schemaConditions(host)).toEqual([]);
	});

	it("an absent receipt over populated data with no marker keeps PostgreSQL at the digest its generated unit on disk names (06-REVIEW WR-02)", async () => {
		const run = await readyForUpdate();
		retainData(run.host);
		const schemaDigest = run.entry.artifact.sha256;
		const heldPostgres = receiptOf(run).images.postgres ?? "?";
		run.host.files.delete(HOSTED_PATHS.receiptPath);
		run.host.files.delete(SCHEMA_DIGEST_MARKER_PATH);
		const facts = readContainerCompanionFacts(run.host, HOSTED_PATHS);
		expect(facts.receipt).toBeNull();
		expect(facts.schemaDigestOnDisk).toBeNull();
		expect(facts.dataPopulated).toBe(true);
		const dependencyOnly = withPostgresDigest(run.entry, NEWER_POSTGRES_DIGEST);

		const plan = planFor(run.host, dependencyOnly, run.secrets);

		expect(plan.hold).toEqual({ schemaDigest, pinned: schemaDigest, covered: true });
		expect(plan.images.find((image) => image.slot === "postgres")?.reference).toBe(heldPostgres);

		converge(run.host, manifestWith({ postiz: dependencyOnly }), convergePaths(), {
			secretValues: run.secrets,
		});

		expect(receiptOf(run).images.postgres).toBe(heldPostgres);
		expect(run.host.sources().get(BACKUP_SOURCE)).toContain(heldPostgres);
		expect(run.host.sources().get(BACKUP_SOURCE)).not.toContain(NEWER_POSTGRES_DIGEST);
		expect(schemaConditions(run.host)).toEqual([]);
	});

	it("an absent receipt over populated data with no marker keeps the app its generated unit on disk names and raises schema_digest_mismatch (06-REVIEW WR-02)", async () => {
		const run = await readyForUpdate();
		retainData(run.host);
		const schemaDigest = run.entry.artifact.sha256;
		run.host.files.delete(HOSTED_PATHS.receiptPath);
		run.host.files.delete(SCHEMA_DIGEST_MARKER_PATH);
		const facts = readContainerCompanionFacts(run.host, HOSTED_PATHS);
		expect(facts.receipt).toBeNull();
		expect(facts.schemaDigestOnDisk).toBeNull();
		expect(facts.dataPopulated).toBe(true);
		const unannounced = pinnedWithoutAnnouncement(run.entry, NEWER_APP_DIGEST);

		const plan = planFor(run.host, unannounced, run.secrets);

		expect(plan.hold).toEqual({ schemaDigest, pinned: NEWER_APP_DIGEST, covered: false });
		expect(plan.images.find((image) => image.slot === "app")?.sha256).toBe(schemaDigest);
		traceSchemaMarker(run.host);
		traceAppSource(run);
		const initial = appSourceDigest(run.host);
		const from = run.events.length;

		converge(run.host, manifestWith({ postiz: unannounced }), convergePaths(), {
			secretValues: run.secrets,
		});

		const timeline = run.events.slice(from);
		expect(timeline).not.toContain(`schema-marker:${NEWER_APP_DIGEST}`);
		expect(startsWhileAppSourceIs(timeline, initial, NEWER_APP_DIGEST, timeline.length)).toEqual(
			[],
		);
		expect(appSourceDigest(run.host)).toBe(schemaDigest);
		expect(run.host.readFile(SCHEMA_DIGEST_MARKER_PATH)).toBe(`${schemaDigest}\n`);
		const raised = schemaConditions(run.host);
		expect(raised).toMatchObject([{ installationId: run.entry.installationId, state: "raised" }]);
		expect(String(raised[0]?.detail)).toContain(schemaDigest);
		expect(String(raised[0]?.detail)).toContain(NEWER_APP_DIGEST);
	});

	it("populated data that names no schema digest is refused: nothing starts and schema_digest_mismatch is raised (06-REVIEW WR-02)", async () => {
		const { run, paths } = await withdrawnAtSchemaDigest();
		run.host.files.delete(HOSTED_PATHS.receiptPath);
		run.host.files.delete(SCHEMA_DIGEST_MARKER_PATH);
		expect(run.host.sources().size).toBe(0);
		const facts = readContainerCompanionFacts(run.host, HOSTED_PATHS);
		expect(facts.receipt).toBeNull();
		expect(facts.schemaDigestOnDisk).toBeNull();
		expect(facts.dataPopulated).toBe(true);

		let refusal: unknown = null;
		try {
			planFor(run.host, run.entry, run.secrets);
		} catch (error) {
			refusal = error;
		}
		expect(refusal).toBeInstanceOf(ContainerCompanionConditionError);
		expect((refusal as ContainerCompanionConditionError).code).toBe("schema_digest_mismatch");
		traceAppSource(run);
		const from = run.events.length;

		converge(run.host, manifestWith({ postiz: run.entry }), paths, { secretValues: run.secrets });

		const timeline = run.events.slice(from);
		expect(timeline.filter((event) => event.startsWith("run:podman pull"))).toEqual([]);
		expect(
			timeline.filter((event) => /^run:systemctl (start|restart) .*iforge-postiz/.test(event)),
		).toEqual([]);
		expect(timeline.filter((event) => event.startsWith("app-source:"))).toEqual([]);
		expect(run.host.sources().size).toBe(0);
		expect(run.host.readFile(HOSTED_PATHS.receiptPath)).toBeNull();
		expect(String(applyFailureRecord(run.host)?.message)).toContain(
			"nothing names the digest that wrote it",
		);
		expect(schemaConditions(run.host)).toMatchObject([
			{ installationId: run.entry.installationId, state: "raised" },
		]);
	});
});

/** Replace the receipt on disk with `content`, as a corrupt file or a newer CLI would leave it. */
function replaceReceipt(host: FakeContainerHost, content: string): void {
	host.files.set(HOSTED_PATHS.receiptPath, { content, mode: 0o600, uid: 0, gid: 0 });
}

/** A schema marker naming `digest`, as a previous installation would have left it. */
function writeSchemaMarker(host: FakeContainerHost, digest: string): void {
	host.files.set(SCHEMA_DIGEST_MARKER_PATH, {
		content: `${digest}\n`,
		mode: 0o600,
		uid: 0,
		gid: 0,
	});
}

/** The digest of an `image@sha256:<digest>` reference. */
function digestOfReference(value: string): string {
	return /@sha256:([a-f0-9]{64})$/.exec(value)?.[1] ?? "none";
}

// ---------------------------------------------------------------------------------------------
// Plan 06-49 (G-06-2 WR-06): an installation whose network predates the pinned subnet (plan 06-39)
// sits on Podman's default pool, which the guard's subnet rule does not cover. The converge
// migrates it -- stop the kind's units, `podman network rm` (never forced), recreate through the
// network unit, verify, guard -- before anything of the kind starts, and records the verified
// subnet so no later plan is `unchanged` over an unverified network.
// ---------------------------------------------------------------------------------------------

const OLD_SUBNET = "10.89.0.0/24";
const INSPECT_NETWORK =
	"run:podman network inspect iforge-postiz --format {{range .Subnets}}{{.Subnet}} {{end}}";
const STOP_KIND =
	"run:systemctl stop iforge-postiz.service iforge-postiz-redis.service iforge-postiz-db.service";
const REMOVE_NETWORK = "run:podman network rm iforge-postiz";
const RESTART_NETWORK = "run:systemctl restart iforge-postiz-network.service";
const LOAD_GUARD = `run:/usr/sbin/nft -f ${GUARD_PATH}`;
/** A start or restart of any unit of the kind other than its network. */
const KIND_START = /^run:systemctl (start|restart) .*iforge-postiz(-db|-redis|-tunnel)?\.service/;

function hostReceipt(host: FakeContainerHost): Record<string, unknown> {
	return JSON.parse(host.readFile(RECEIPT_PATH) ?? "{}") as Record<string, unknown>;
}

function writeHostReceipt(host: FakeContainerHost, receipt: Record<string, unknown>): void {
	host.files.set(RECEIPT_PATH, {
		content: `${JSON.stringify(receipt, null, 2)}\n`,
		mode: 0o600,
		uid: 0,
		gid: 0,
	});
}

/** The indices of every start or restart of a unit of the kind in `events`. */
function kindStarts(events: readonly string[]): number[] {
	return events.flatMap((event, index) => (KIND_START.test(event) ? [index] : []));
}

/**
 * A host that ran an installation applied before plan 06-39: no guard file or table, no recorded
 * subnet, and a network from Podman's default pool. Its data directory holds a database.
 */
function preGuardInstallation(options: FakeHostOptions = {}): FakeContainerHost {
	const host = new FakeContainerHost(options);
	converge(host, manifestWith({ postiz: fixtureEntry() }));
	seedData(host);
	host.files.delete(GUARD_PATH);
	host.nftTables.clear();
	const receipt = hostReceipt(host);
	delete receipt.networkSubnet;
	writeHostReceipt(host, receipt);
	host.networkSubnet = OLD_SUBNET;
	return host;
}

describe("a network that predates the pinned subnet is migrated before anything starts (plan 06-49, WR-06)", () => {
	it("a pre-06-39 installation's network is migrated to the pinned subnet and guarded before anything starts (WR-06)", () => {
		const host = preGuardInstallation();
		const before = host.events.length;

		converge(host, manifestWith({ postiz: fixtureEntry() }));

		const later = host.events.slice(before);
		expectInOrder(later, [
			"run:systemctl daemon-reload",
			"run:systemctl start iforge-postiz-network.service",
			INSPECT_NETWORK,
			STOP_KIND,
			REMOVE_NETWORK,
			RESTART_NETWORK,
			INSPECT_NETWORK,
			LOAD_GUARD,
			"run:systemctl start iforge-postiz.service",
		]);
		const firstInspect = later.indexOf(INSPECT_NETWORK);
		const secondInspect = later.indexOf(INSPECT_NETWORK, firstInspect + 1);
		const guardAt = later.indexOf(LOAD_GUARD);
		expect(secondInspect).toBeGreaterThan(firstInspect);
		expect(guardAt).toBeGreaterThan(secondInspect);
		const starts = kindStarts(later);
		expect(starts.length).toBeGreaterThan(0);
		expect(Math.min(...starts)).toBeGreaterThan(guardAt);
		// Never forced: an attached container is something this module does not own.
		expect(
			host.commands.some(
				(call) =>
					call.command === "podman" &&
					call.args[0] === "network" &&
					call.args[1] === "rm" &&
					call.args.some((arg) => arg.startsWith("-")),
			),
		).toBe(false);
		expect(host.networkSubnet).toBe(POSTIZ_SUBNET);
		expect(host.nftTables.has(GUARD_TABLE)).toBe(true);
		// The data directory is untouched.
		expect(host.removedTrees).toEqual([]);
		expect(host.files.has(`${DATA_ROOT}/db/PG_VERSION`)).toBe(true);
		expect(later.some((event) => event.startsWith(`remove:${DATA_ROOT}`))).toBe(false);
		expect(hostReceipt(host).networkSubnet).toBe(POSTIZ_SUBNET);

		const commandsBefore = host.commands.length;
		converge(host, manifestWith({ postiz: fixtureEntry() }));
		expect(stateChangingCommands(host, commandsBefore)).toEqual([]);
	});

	it("a fresh install on a network created with the pinned subnet records it and migrates nothing", () => {
		const host = new FakeContainerHost();
		converge(host, manifestWith({ postiz: fixtureEntry() }));
		expect(hostReceipt(host).networkSubnet).toBe(POSTIZ_SUBNET);
		expect(host.events).not.toContain(STOP_KIND);
		expect(host.events).not.toContain(REMOVE_NETWORK);
		expect(host.events.filter((event) => event === INSPECT_NETWORK)).toHaveLength(1);
	});

	it("a receipt written after 06-39 but with no recorded subnet re-applies exactly once, with no migration", () => {
		const host = new FakeContainerHost();
		converge(host, manifestWith({ postiz: fixtureEntry() }));
		seedData(host);
		const receipt = hostReceipt(host);
		delete receipt.networkSubnet;
		writeHostReceipt(host, receipt);
		const before = host.events.length;

		converge(host, manifestWith({ postiz: fixtureEntry() }));

		const later = host.events.slice(before);
		expect(later.filter((event) => event === INSPECT_NETWORK)).toHaveLength(1);
		expect(later).not.toContain(STOP_KIND);
		expect(later).not.toContain(REMOVE_NETWORK);
		expect(hostReceipt(host).networkSubnet).toBe(POSTIZ_SUBNET);

		const commandsBefore = host.commands.length;
		converge(host, manifestWith({ postiz: fixtureEntry() }));
		expect(stateChangingCommands(host, commandsBefore)).toEqual([]);
	});
});

/** Drop the receipt's verified subnet, keeping everything else (its ready marker too). */
function dropVerifiedSubnet(host: FakeContainerHost): void {
	const receipt = hostReceipt(host);
	delete receipt.networkSubnet;
	writeHostReceipt(host, receipt);
}

/** Turn a host into one whose installation predates plan 06-39, keeping its receipt otherwise. */
function makePreGuard(host: FakeContainerHost): void {
	host.files.delete(GUARD_PATH);
	host.nftTables.clear();
	dropVerifiedSubnet(host);
	host.networkSubnet = OLD_SUBNET;
}

function conditionCodes(host: FakeContainerHost): Array<{ code: string; state: string }> {
	return conditionsApi()
		.read(host, HOSTED_PATHS)
		.map((condition) => ({ code: String(condition.code), state: String(condition.state) }));
}

describe("neither an update nor a rollback starts a unit over an unverified network (plan 06-49, WR-06)", () => {
	it("an update accepted before the network was migrated waits for the migration (WR-06 second case)", async () => {
		const run = await readyForUpdate();
		makePreGuard(run.host);
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const newerReference = reference(newer.artifact.image, NEWER_APP_DIGEST);
		const from = run.events.length;

		// The converge over the accepted pin: the install is held at the receipt's digests.
		const plan = planFor(run.host, newer, run.secrets);
		expect(plan.kind).toBe("install");
		expect(plan.hold).toMatchObject({ pinned: NEWER_APP_DIGEST, covered: true });
		applyContainerCompanion(plan, run.host, HOSTED_PATHS);

		const later = run.events.slice(from);
		expectInOrder(later, [STOP_KIND, REMOVE_NETWORK, RESTART_NETWORK, INSPECT_NETWORK, LOAD_GUARD]);
		expect(run.host.sources().get(APP_SOURCE)).not.toContain(newerReference);
		expect(later.some((event) => event.includes(NEWER_APP_DIGEST))).toBe(false);
		expect(hostReceipt(run.host).networkSubnet).toBe(POSTIZ_SUBNET);

		// Once the installation is ready again, the update pass backs up, swaps and reports ready.
		expect(await runReadiness(run)).toMatchObject({ outcome: "ready" });
		const updateFrom = run.events.length;
		const outcome = await runUpdate(run, newer);
		expect(outcome).toMatchObject({ outcome: "ready", step: "ready" });
		expectInOrder(run.events.slice(updateFrom), [
			INSPECT_NETWORK,
			"pgbackrest:backup --type=diff",
			`run:podman pull ${newerReference}`,
			APP_RESTART,
		]);
		expect(run.host.sources().get(APP_SOURCE)).toContain(newerReference);
		expect(hostReceipt(run.host).networkSubnet).toBe(POSTIZ_SUBNET);
	});

	it("the update path checks the live subnet before any backup", async () => {
		const run = await readyForUpdate();
		expect(hostReceipt(run.host).networkSubnet).toBe(POSTIZ_SUBNET);
		run.host.networkSubnet = OLD_SUBNET;
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const from = run.events.length;
		const pgbackrestFrom = run.pgbackrest.calls.length;

		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({
			outcome: "skipped",
			reason: expect.stringContaining("network iforge-postiz"),
		});
		const later = run.events.slice(from);
		expect(run.pgbackrest.calls.length).toBe(pgbackrestFrom);
		expect(
			later.filter((event) =>
				/^(run:podman pull|write:\/etc|run:systemctl (start|restart|stop)|report:)/.test(event),
			),
		).toEqual([]);
		expect(later.filter((event) => event.startsWith("write:/etc/containers"))).toEqual([]);
		expect(hostReceipt(run.host).networkSubnet).toBeUndefined();
		expect(conditionCodes(run.host)).toEqual([
			{ code: "network_subnet_unverified", state: "raised" },
		]);
		// The next converge plans the install that migrates the network.
		expect(planFor(run.host, newer, run.secrets).kind).toBe("install");
	});

	it("a matching live subnet with no recorded one is recorded, and the update continues", async () => {
		const run = await readyForUpdate();
		dropVerifiedSubnet(run.host);
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		const from = run.events.length;

		const outcome = await runUpdate(run, newer);

		expect(outcome).toMatchObject({ outcome: "ready", step: "ready" });
		const later = run.events.slice(from);
		expect(later.filter((event) => event === INSPECT_NETWORK)).toHaveLength(1);
		expect(later.indexOf(INSPECT_NETWORK)).toBeLessThan(
			later.indexOf("pgbackrest:backup --type=diff"),
		);
		expect(later).not.toContain(REMOVE_NETWORK);
		expect(hostReceipt(run.host).networkSubnet).toBe(POSTIZ_SUBNET);
	});

	it("the repair of an incomplete rollback verifies the network before it starts anything (WR-06 with WR-07)", async () => {
		const { run, failing } = await refusedWithFailingRestore();
		const label = preUpdateLabel(run);
		ageLastAttempt(run, 31);
		failing.restore = false;
		run.host.networkSubnet = OLD_SUBNET;
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, run.entry));

		expect(outcome).toMatchObject({ outcome: "repaired", label });
		const later = run.events.slice(from);
		expectInOrder(later, [
			APP_STOP_THEN_DB,
			`write:${APP_SOURCE}`,
			"run:systemctl daemon-reload",
			INSPECT_NETWORK,
			STOP_KIND,
			REMOVE_NETWORK,
			RESTART_NETWORK,
			INSPECT_NETWORK,
			"pgbackrest:restore --type=immediate",
			APP_START_PREVIOUS,
		]);
		const firstInspect = later.indexOf(INSPECT_NETWORK);
		const pinnedInspect = later.indexOf(INSPECT_NETWORK, firstInspect + 1);
		expect(kindStarts(later).every((index) => index > pinnedInspect)).toBe(true);
		expect(run.host.networkSubnet).toBe(POSTIZ_SUBNET);
		expect(incompleteOf(run)).toBeUndefined();
	});

	it("a repair whose migration fails starts nothing and stays incomplete", async () => {
		const { run, failing } = await refusedWithFailingRestore();
		ageLastAttempt(run, 31);
		failing.restore = false;
		run.host.networkSubnet = OLD_SUBNET;
		run.host.networkRemovalFails = "Error: network iforge-postiz is being used";
		const attemptsBefore = incompleteOf(run)?.attempts ?? 0;
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, run.entry));

		expect(outcome).toMatchObject({ outcome: "rollback-incomplete" });
		const later = run.events.slice(from);
		expect(later).toContain(REMOVE_NETWORK);
		expect(kindStarts(later)).toEqual([]);
		expect(later.some((event) => event.startsWith("pgbackrest:restore"))).toBe(false);
		expect(incompleteOf(run)?.attempts).toBe(attemptsBefore + 1);
		expect(conditionCodes(run.host)).toEqual(
			expect.arrayContaining([
				{ code: "update_rollback_incomplete", state: "raised" },
				{ code: "network_subnet_unverified", state: "raised" },
			]),
		);
	});

	it("a rollback over a verified network runs one inspect and no migration", async () => {
		const { run, failing } = await refusedWithFailingRestore();
		ageLastAttempt(run, 31);
		failing.restore = false;
		const from = run.events.length;

		const outcome = await settle(runUpdate(run, run.entry));

		expect(outcome).toMatchObject({ outcome: "repaired" });
		const later = run.events.slice(from);
		expect(later.filter((event) => event === INSPECT_NETWORK)).toHaveLength(1);
		expect(later).not.toContain(REMOVE_NETWORK);
		expect(later).not.toContain(STOP_KIND);
	});
});

/** Mark the host's installation as having reported ready (its apply failures are then logged only). */
function markReportedReady(host: FakeContainerHost): void {
	writeHostReceipt(host, {
		...hostReceipt(host),
		bootstrap: {
			receipt: "reported-apply",
			phase: "ready",
			step: "ready",
			reportedAt: "2026-09-23T12:00:00.000Z",
		},
	});
}

const MASK_PREFIX = "run:systemctl mask ";
const UNMASK_PREFIX = "run:systemctl unmask ";
const UNITS_MASKED_PATH = "/var/lib/iforge/companions/postiz/units-masked.json";

/** A ready pre-06-39 installation whose network cannot be removed: something still holds it. */
function heldNetworkInstallation(): { host: FakeContainerHost; manifest: RuntimeManifest } {
	const host = preGuardInstallation();
	markReportedReady(host);
	// The refusal echoes a composed secret, as careless tooling can, so redaction is proven.
	host.networkRemovalFails = `Error: network iforge-postiz is being used (${DB_PASSWORD_TOKEN})`;
	return { host, manifest: manifestWith({ postiz: fixtureEntry() }) };
}

function subnetCondition(host: FakeContainerHost): Record<string, unknown> | undefined {
	return conditionsApi()
		.read(host, HOSTED_PATHS)
		.find((condition) => condition.code === "network_subnet_unverified");
}

describe("a failed network migration keeps the kind stopped and is reported (plan 06-49, WR-06)", () => {
	it("a migration that cannot remove the network leaves every unit stopped and reports network_subnet_unverified", async () => {
		const { host, manifest } = heldNetworkInstallation();
		const before = host.events.length;

		converge(host, manifest);

		const later = host.events.slice(before);
		const stopAt = later.indexOf(STOP_KIND);
		expect(stopAt).toBeGreaterThanOrEqual(0);
		expect(later.slice(stopAt)).toContain(REMOVE_NETWORK);
		// Nothing of the kind starts on the unverified network after the migration's stop.
		expect(kindStarts(later.slice(stopAt))).toEqual([]);
		expect(later).not.toContain(LOAD_GUARD);
		// The journal restored the sources: the guard file this apply wrote is gone again.
		expect(host.files.has(GUARD_PATH)).toBe(false);
		expect(hostReceipt(host).networkSubnet).toBeUndefined();
		expect(String(applyFailureRecord(host)?.message)).toContain("network iforge-postiz");
		expect(String(applyFailureRecord(host)?.message)).not.toContain(DB_PASSWORD_TOKEN);

		const condition = subnetCondition(host);
		expect(condition).toMatchObject({ state: "raised" });
		expect(String(condition?.detail)).toContain("being used");
		expect(String(condition?.detail)).not.toContain(DB_PASSWORD_TOKEN);

		// The apply failure of this ready installation is only logged; the condition is reported.
		const failureReporter = new RecordingReporter([]);
		expect((await reportApplyFailureOver(host, manifest, failureReporter)).outcome).toBe(
			"logged-only",
		);
		expect(failureReporter.reports).toEqual([]);
		const reporter = new RecordingReporter([]);
		expect(await reportConditionsOver(host, manifest, reporter)).toEqual({
			outcome: "reported",
			sent: [{ code: "network_subnet_unverified", state: "raised", result: "recorded" }],
		});
		expect(JSON.stringify(reporter.reports)).not.toContain(DB_PASSWORD_TOKEN);
		expect((await reportConditionsOver(host, manifest, reporter)).outcome).toBe("skipped");
	});

	it("a later converge whose removal succeeds migrates, records the subnet and clears the condition", async () => {
		const { host, manifest } = heldNetworkInstallation();
		converge(host, manifest);
		const reporter = new RecordingReporter([]);
		await reportConditionsOver(host, manifest, reporter);
		host.networkRemovalFails = null;
		const before = host.events.length;

		converge(host, manifest);

		const later = host.events.slice(before);
		expectInOrder(later, [STOP_KIND, REMOVE_NETWORK, RESTART_NETWORK, LOAD_GUARD]);
		expect(hostReceipt(host).networkSubnet).toBe(POSTIZ_SUBNET);
		expect(subnetCondition(host)).toMatchObject({ state: "cleared" });
		expect(await reportConditionsOver(host, manifest, reporter)).toEqual({
			outcome: "reported",
			sent: [{ code: "network_subnet_unverified", state: "cleared", result: "recorded" }],
		});
	});

	it("a failed migration persistently masks every unit a reboot would start, so the unguarded network never serves one (06-REVIEW WR-03)", () => {
		const { host, manifest } = heldNetworkInstallation();
		const before = host.events.length;

		converge(host, manifest);

		const later = host.events.slice(before);
		const maskAt = later.findIndex((event) => event.startsWith(MASK_PREFIX));
		expect(maskAt).toBeGreaterThan(later.indexOf(REMOVE_NETWORK));
		const masked = new Set(later[maskAt]?.slice(MASK_PREFIX.length).split(" "));
		// Every unit whose source systemd starts at boot is masked (Quadlet: `<name>.container` is
		// `<name>.service`); the network unit, which starts no container, is left alone.
		const wantedAtBoot = [...host.sources()]
			.filter(([, content]) => content.includes("WantedBy=multi-user.target"))
			.map(
				([path]) => `${path.slice(path.lastIndexOf("/") + 1).replace(/\.container$/, "")}.service`,
			);
		expect(wantedAtBoot.length).toBeGreaterThan(0);
		for (const unit of wantedAtBoot) expect(masked.has(unit)).toBe(true);
		expect(masked.has("iforge-postiz-network.service")).toBe(false);
		// The mask outlives the journal's rollback of the sources, and is recorded for the unmask.
		expect(later.some((event) => event.startsWith(UNMASK_PREFIX))).toBe(false);
		expect(host.files.get(UNITS_MASKED_PATH)).toMatchObject({ mode: 0o600, uid: 0 });
	});

	it("the converge that completes the migration unmasks the kind before it starts anything, once", () => {
		const { host, manifest } = heldNetworkInstallation();
		converge(host, manifest);
		host.networkRemovalFails = null;
		const before = host.events.length;

		converge(host, manifest);

		const later = host.events.slice(before);
		const unmaskAt = later.findIndex((event) => event.startsWith(UNMASK_PREFIX));
		expect(unmaskAt).toBeGreaterThan(later.indexOf(RESTART_NETWORK));
		expect(Math.min(...kindStarts(later))).toBeGreaterThan(unmaskAt);
		expect(host.files.has(UNITS_MASKED_PATH)).toBe(false);

		const again = host.events.length;
		converge(host, manifest);
		expect(host.events.slice(again).some((event) => event.startsWith(UNMASK_PREFIX))).toBe(false);
	});

	it("a removal after a failed network migration unmasks the kind and deletes the mask record before the data directory goes (06-REVIEW WR-03)", async () => {
		const { host, manifest } = heldNetworkInstallation();
		const failedAt = host.events.length;
		converge(host, manifest);
		seedData(host);
		const maskEvent = host.events.slice(failedAt).find((event) => event.startsWith(MASK_PREFIX));
		const masked = maskEvent?.slice(MASK_PREFIX.length).split(" ") ?? [];
		expect(masked.length).toBeGreaterThan(0);
		expect(host.files.has(UNITS_MASKED_PATH)).toBe(true);
		const before = host.events.length;

		converge(host, manifestWith({ postiz: removeEntry() }));

		const later = host.events.slice(before);
		const unmaskAt = later.findIndex((event) => event.startsWith(UNMASK_PREFIX));
		expect(unmaskAt).toBeGreaterThanOrEqual(0);
		const unmasked = new Set(later[unmaskAt]?.slice(UNMASK_PREFIX.length).split(" "));
		for (const unit of masked) expect(unmasked.has(unit)).toBe(true);
		const stopAt = later.findIndex(
			(event) => event.startsWith("run:systemctl stop") && event.includes("iforge-postiz.service"),
		);
		expect(stopAt).toBeGreaterThanOrEqual(0);
		expect(unmaskAt).toBeGreaterThan(stopAt);
		expect(unmaskAt).toBeLessThan(later.indexOf(`rmtree:${DATA_ROOT}`));
		expect(host.files.has(UNITS_MASKED_PATH)).toBe(false);
		expect(host.files.has(RECEIPT_PATH)).toBe(false);
		expect(host.removedTrees).toEqual([DATA_ROOT]);
		const { outcome, reporter } = await reportRemovedOver(
			host,
			manifestWith({ postiz: removeEntry() }),
		);
		expect(outcome.outcome).toBe("reported");
		expect(reporter.reports).toEqual([{ operation: "uninstall", phase: "removed" }]);
	});

	it("a removal whose unmask fails keeps the data, the receipt and the mask record, and the next converge completes it (06-REVIEW WR-03)", async () => {
		const { host, manifest } = heldNetworkInstallation();
		const failedAt = host.events.length;
		converge(host, manifest);
		seedData(host);
		expect(host.events.slice(failedAt).some((event) => event.startsWith(MASK_PREFIX))).toBe(true);
		expect(host.files.has(UNITS_MASKED_PATH)).toBe(true);
		host.throwOn = (command, args) => command === "systemctl" && args[0] === "unmask";

		converge(host, manifestWith({ postiz: removeEntry() }));

		expect(host.removedTrees).toEqual([]);
		expect(dataPaths(host).length).toBeGreaterThan(0);
		expect(host.files.has(RECEIPT_PATH)).toBe(true);
		expect(host.files.has(UNITS_MASKED_PATH)).toBe(true);
		const unfinished = await reportRemovedOver(host, manifestWith({ postiz: removeEntry() }));
		expect(unfinished.outcome.outcome).toBe("skipped");
		expect(unfinished.reporter.reports).toEqual([]);

		host.throwOn = null;
		converge(host, manifestWith({ postiz: removeEntry() }));

		expect(host.files.has(UNITS_MASKED_PATH)).toBe(false);
		expect(host.files.has(RECEIPT_PATH)).toBe(false);
		expect(dataPaths(host)).toEqual([]);
		const finished = await reportRemovedOver(host, manifestWith({ postiz: removeEntry() }));
		expect(finished.outcome.outcome).toBe("reported");
	});

	it("a removal with no mask record issues no unmask command", () => {
		const host = new FakeContainerHost();
		converge(host, manifestWith({ postiz: fixtureEntry() }));
		seedData(host);
		expect(host.files.has(UNITS_MASKED_PATH)).toBe(false);

		converge(host, manifestWith({ postiz: removeEntry() }));

		expect(host.removedTrees).toEqual([DATA_ROOT]);
		expect(host.events.some((event) => event.startsWith(UNMASK_PREFIX))).toBe(false);
	});

	it("a recreated network that still reports another subnet raises the same condition, units stopped", () => {
		const host = preGuardInstallation({ recreatedSubnet: OLD_SUBNET });
		markReportedReady(host);
		const before = host.events.length;

		converge(host, manifestWith({ postiz: fixtureEntry() }));

		const later = host.events.slice(before);
		const stopAt = later.indexOf(STOP_KIND);
		expectInOrder(later, [STOP_KIND, REMOVE_NETWORK, RESTART_NETWORK, INSPECT_NETWORK]);
		expect(kindStarts(later.slice(stopAt))).toEqual([]);
		expect(later).not.toContain(LOAD_GUARD);
		const condition = subnetCondition(host);
		expect(condition).toMatchObject({ state: "raised" });
		expect(String(condition?.detail)).toContain(OLD_SUBNET);
	});
});

// ---------------------------------------------------------------------------------------------
// Plan 06-52 (G-06-4): every update report names the update it belongs to.
// ---------------------------------------------------------------------------------------------

/**
 * The production reporter over a control plane that refuses every report as another update's
 * (plan 06-50's 409), recording each POST body exactly as it went on the wire.
 */
function identityRefusingReporter(installationId: string): {
	reporter: CompanionProgressReporter;
	bodies: Array<Record<string, unknown>>;
} {
	const bodies: Array<Record<string, unknown>> = [];
	const reporter = new CompanionProgressReporter({
		apiUrl: "https://cloud-api.example.test",
		token: "runtime-token",
		installationId,
		secrets: [],
		fetch: async (_url, init = {}) => {
			bodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
			return Response.json(
				{
					detail: {
						code: "companion_update_identity_mismatch",
						message: "This report names another update than the one in flight.",
						operation: "update",
						status: "running",
					},
				},
				{ status: 409 },
			);
		},
		sleep: async () => {},
	});
	return { reporter, bodies };
}

/** `runUpdate` over another report sink than the run's own. */
function runUpdateWith(
	run: BootstrapRun,
	entry: PostizCompanionManifest,
	reporter: CompanionReportSink,
) {
	return containerCompanionModule.applyPostizUpdate({
		manifest: manifestWith({ postiz: entry }),
		secretValues: run.secrets,
		paths: RUNTIME_PATHS,
		host: run.host,
		loopback: run.postiz.loopback(),
		reporter,
	});
}

describe("update reports name the update they belong to (G-06-4)", () => {
	it("a failed report kept through an outage names its own update, whatever the entry names when it is sent", async () => {
		const run = await readyForUpdate();
		run.pgbackrest.failBackup = true;
		run.reporter = new ScriptedReporter(run.events, {
			failed: new Error("control plane unreachable"),
		});
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);

		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "unreportable" });
		expect(receiptOf(run).pendingUpdateReport).toMatchObject({
			operation: "update",
			phase: "failed",
			step: "backup",
			to_digest: NEWER_APP_DIGEST,
		});

		// The host stayed away past N's deadline: the pin went back, and the tenant accepted M.
		const later = withAppDigest(run.entry, FOREIGN_APP_DIGEST);
		const wire = identityRefusingReporter(later.installationId);

		const outcome = await settle(runUpdateWith(run, later, wire.reporter));

		// The control plane refuses N's report as another update's, and the host settles it.
		expect(outcome).toMatchObject({ outcome: "failed", step: "backup" });
		expect(wire.bodies).toHaveLength(1);
		expect(wire.bodies[0]).toMatchObject({
			operation: "update",
			phase: "failed",
			step: "backup",
			to_digest: NEWER_APP_DIGEST,
		});
		expect(JSON.stringify(wire.bodies)).not.toContain(FOREIGN_APP_DIGEST);
		expect(receiptOf(run).pendingUpdateReport).toBeUndefined();
	});

	it("a successful update names it on every progress report and on its ready", async () => {
		const run = await readyForUpdate();
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);

		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "ready" });

		const reports = updateReports(run);
		const progress = reports.filter((report) => report.phase === "progress");
		expect(progress.length).toBeGreaterThan(0);
		for (const report of progress) {
			expect(report).toMatchObject({ to_digest: NEWER_APP_DIGEST });
		}
		expect(reports.find((report) => report.phase === "ready")).toMatchObject({
			applied_digest: NEWER_APP_DIGEST,
			to_digest: NEWER_APP_DIGEST,
		});
	});

	it("a failed data assertion's failed report names the update it rolled back", async () => {
		const run = await readyForUpdate();
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		// The new digest's start-time schema push loses the organization; the restore brings it back.
		const realRun = run.host.run.bind(run.host);
		run.host.run = (command, args, options) => {
			const result = realRun(command, args, options);
			if (command === "systemctl" && args[0] === "restart") run.postiz.organizations = 0;
			if (command === "podman" && args[0] === "run" && args.includes("restore")) {
				run.postiz.organizations = 1;
			}
			return result;
		};

		const outcome = await settle(runUpdate(run, newer));

		expect(outcome).toMatchObject({ outcome: "failed", step: "post-update-assertion" });
		expect(updateReports(run).filter((report) => report.phase === "failed")).toMatchObject([
			{ step: "post-update-assertion", to_digest: NEWER_APP_DIGEST },
		]);
	});

	it("an interrupted swap rolled back with the pin reverted and nothing announced names the swap's own target", async () => {
		const { run, label } = await interruptedSwap();
		expect(receiptOf(run).pendingUpdateRollback).toMatchObject({
			to: { app: reference(run.entry.artifact.image, NEWER_APP_DIGEST) },
		});

		const outcome = await settle(runUpdate(run, run.entry));

		expect(outcome).toMatchObject({ outcome: "failed", step: "post-update-assertion", label });
		expect(updateReports(run).filter((report) => report.phase === "failed")).toMatchObject([
			{ step: "post-update-assertion", to_digest: NEWER_APP_DIGEST },
		]);
	});

	it("an interrupted swap recorded without its target names the update from the write-ahead's schema digest, never the entry's pin", async () => {
		const { run, label } = await interruptedSwap();
		dropFromReceipt(run, "pendingUpdateRollback", "to");
		expect(receiptOf(run).pendingUpdateRollback).not.toHaveProperty("to");
		expect(receiptOf(run).schemaDigest).toBe(NEWER_APP_DIGEST);
		// Nothing is announced, so the report is sent; the entry pins a digest this swap never ran.
		const foreign = pinnedWithoutAnnouncement(run.entry, FOREIGN_APP_DIGEST);

		const outcome = await settle(runUpdate(run, foreign));

		expect(outcome).toMatchObject({ outcome: "failed", step: "post-update-assertion", label });
		const failed = updateReports(run).filter((report) => report.phase === "failed");
		expect(failed).toMatchObject([{ step: "post-update-assertion", to_digest: NEWER_APP_DIGEST }]);
		expect(failed[0]).not.toMatchObject({ to_digest: FOREIGN_APP_DIGEST });
	});

	it("an interrupted swap whose write-ahead names neither its target nor a readable schema digest sends its failed report with no to_digest, never the entry's pin (06-REVIEW WR-05)", async () => {
		const { run, label } = await interruptedSwap();
		dropFromReceipt(run, "pendingUpdateRollback", "to");
		dropReceiptSchemaDigest(run);
		expect(receiptOf(run).pendingUpdateRollback).not.toHaveProperty("to");
		expect(receiptOf(run)).not.toHaveProperty("schemaDigest");
		// Nothing is announced, so the report is sent; the entry pins a digest this swap never ran.
		const foreign = pinnedWithoutAnnouncement(run.entry, FOREIGN_APP_DIGEST);

		const outcome = await settle(runUpdate(run, foreign));

		expect(outcome).toMatchObject({ outcome: "failed", step: "post-update-assertion", label });
		const failed = updateReports(run).filter((report) => report.phase === "failed");
		expect(failed).toHaveLength(1);
		expect(failed[0]).not.toHaveProperty("to_digest");
		expect(JSON.stringify(failed)).not.toContain(FOREIGN_APP_DIGEST);
	});

	it("a failed report deliberately left without to_digest stays so through an outage and is sent without one (06-REVIEW WR-05)", async () => {
		const { run } = await interruptedSwap();
		dropFromReceipt(run, "pendingUpdateRollback", "to");
		dropReceiptSchemaDigest(run);
		const scripted = new ScriptedReporter(run.events, {
			failed: new Error("control plane unreachable"),
		});
		run.reporter = scripted;
		const foreign = pinnedWithoutAnnouncement(run.entry, FOREIGN_APP_DIGEST);

		expect(await settle(runUpdate(run, foreign))).toMatchObject({ outcome: "unreportable" });
		expect(receiptOf(run).pendingUpdateReport).toMatchObject({ phase: "failed", to_digest: null });

		delete scripted.answers.failed;
		await settle(runUpdate(run, foreign));

		const last = scripted.last();
		expect(last).toMatchObject({ operation: "update", phase: "failed" });
		expect(last).not.toHaveProperty("to_digest");
		expect(receiptOf(run).pendingUpdateReport).toBeUndefined();
	});

	it("a queued failed report written without to_digest is sent with the refused update's digest, never the entry's", async () => {
		const run = await readyForUpdate();
		run.pgbackrest.failBackup = true;
		const scripted = new ScriptedReporter(run.events, {
			failed: new Error("control plane unreachable"),
		});
		run.reporter = scripted;
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "unreportable" });
		// As an older CLI queued it: no identity on the report, only on the refusal record.
		dropFromReceipt(run, "pendingUpdateReport", "to_digest");
		expect(receiptOf(run).refusedUpdate?.images.app).toBe(
			reference(run.entry.artifact.image, NEWER_APP_DIGEST),
		);
		const later = withAppDigest(run.entry, FOREIGN_APP_DIGEST);
		scripted.reports.length = 0;

		// Still unreachable: the identity is sent, never written back into the receipt.
		expect(await settle(runUpdate(run, later))).toMatchObject({ outcome: "unreportable" });
		expect(scripted.reports).toMatchObject([
			{ phase: "failed", step: "backup", to_digest: NEWER_APP_DIGEST },
		]);
		expect(receiptOf(run).pendingUpdateReport).not.toHaveProperty("to_digest");

		delete scripted.answers.failed;
		expect(await settle(runUpdate(run, later))).toMatchObject({
			outcome: "failed",
			step: "backup",
		});
		expect(scripted.last()).toMatchObject({
			operation: "update",
			phase: "failed",
			to_digest: NEWER_APP_DIGEST,
		});
		expect(receiptOf(run).pendingUpdateReport).toBeUndefined();
	});

	it("a queued ready written without to_digest is sent with its applied digest", async () => {
		const run = await readyForUpdate();
		const scripted = new ScriptedReporter(run.events, {
			ready: new Error("control plane unreachable"),
		});
		run.reporter = scripted;
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "unreportable" });
		dropFromReceipt(run, "pendingUpdateReport", "to_digest");
		expect(receiptOf(run).pendingUpdateReport).toMatchObject({
			phase: "ready",
			applied_digest: NEWER_APP_DIGEST,
		});
		delete scripted.answers.ready;

		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "ready" });

		expect(scripted.last()).toMatchObject({
			operation: "update",
			phase: "ready",
			applied_digest: NEWER_APP_DIGEST,
			to_digest: NEWER_APP_DIGEST,
		});
	});

	it("a pending ready kept across withdrawal and re-apply still names its update when it is delivered", async () => {
		const run = await readyForUpdate();
		retainData(run.host);
		const scripted = new ScriptedReporter(run.events, {
			ready: new Error("control plane unreachable"),
		});
		run.reporter = scripted;
		const newer = withAppDigest(run.entry, NEWER_APP_DIGEST);
		expect(await settle(runUpdate(run, newer))).toMatchObject({ outcome: "unreportable" });
		expect(receiptOf(run).pendingUpdateReport).toMatchObject({
			phase: "ready",
			to_digest: NEWER_APP_DIGEST,
		});

		// The entry is briefly omitted, then returns with the pin reverted and nothing announced.
		const paths = convergePaths();
		converge(run.host, manifestWith(), paths, { secretValues: run.secrets });
		expect(receiptOf(run).state).toBe("withdrawn");
		converge(run.host, manifestWith({ postiz: run.entry }), paths, { secretValues: run.secrets });
		expect(receiptOf(run).state).toBe("applied");
		expect(receiptOf(run).pendingUpdateReport).toMatchObject({
			phase: "ready",
			to_digest: NEWER_APP_DIGEST,
		});
		delete scripted.answers.ready;

		expect(await settle(runUpdate(run, run.entry))).toMatchObject({ outcome: "ready" });

		expect(scripted.last()).toMatchObject({
			operation: "update",
			phase: "ready",
			to_digest: NEWER_APP_DIGEST,
		});
	});
});

/** Remove the receipt's top-level schema digest, as a lost or rewritten receipt would lack it. */
function dropReceiptSchemaDigest(run: BootstrapRun): void {
	const raw = JSON.parse(run.host.readFile(HOSTED_PATHS.receiptPath) ?? "{}") as Record<
		string,
		unknown
	>;
	delete raw.schemaDigest;
	replaceReceipt(run.host, `${JSON.stringify(raw, null, 2)}\n`);
}

/** Remove one field from a record in the receipt, as a receipt an older CLI wrote would lack it. */
function dropFromReceipt(
	run: BootstrapRun,
	record: "pendingUpdateReport" | "pendingUpdateRollback",
	field: string,
): void {
	const raw = JSON.parse(run.host.readFile(HOSTED_PATHS.receiptPath) ?? "{}") as Record<
		string,
		Record<string, unknown> | undefined
	>;
	const target = raw[record];
	if (target === undefined) throw new Error(`the receipt has no ${record}`);
	delete target[field];
	replaceReceipt(run.host, `${JSON.stringify(raw, null, 2)}\n`);
}
