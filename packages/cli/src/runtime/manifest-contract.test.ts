import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { z } from "zod";
import {
	assertOfferedIsolationTier,
	COMPANION_ISOLATION_TIERS,
	FILE_BROWSER_PORT,
	fileBrowserCompanionSchema,
	OFFERED_ISOLATION_TIERS,
	officialInstallArgs,
	POSTIZ_POSTGRES_IMAGE,
	postizCompanionSchema,
	runtimeCompanionsSchema,
} from "./manifest-contract";

describe("official runtime installer arguments", () => {
	test("keeps Hermes on the official installer's latest release", () => {
		expect(officialInstallArgs("hermes", "/home/clawdi")).toEqual([
			"--skip-setup",
			"--skip-browser",
			"--non-interactive",
		]);
	});

	test("keeps OpenClaw on the official installer's latest release", () => {
		expect(officialInstallArgs("openclaw", "/srv/tenant")).toEqual([
			"--json",
			"--no-onboard",
			"--prefix",
			"/srv/tenant/.local",
		]);
	});
});

const SHA256_HEX = "a".repeat(64);

// The Files release the fixtures below describe. #1025 (804dd1e9) removed these pins from
// `manifest-contract.ts`: the hosted runtime authority now chooses the release, and the schema
// checks only that each asset URL names the version and architecture it claims. The values are
// the release the shipped manifests carry, restated here as test data, as upstream's own
// `manifest-reconciliation.test.ts` does.
const FILE_BROWSER_VERSION = "v1.5.0-stable";
const FILE_BROWSER_COMMIT = "79552f8adb27c3e29934c4001660eb98f4aab5d6";
const FILE_BROWSER_AMD64_SHA256 =
	"8d51d1718d576d22e73e1f41a5194b451d152ddab0df97697cabe839cf59524e";
const FILE_BROWSER_ARM64_SHA256 =
	"3e18838ae33750a25da434dc6156a359968bf7935e01bdd884711f47f08ad92f";

// Both helpers THROW when the envelope accepted a payload the caller expected it to reject.
// Returning an empty list instead would let a `toContain` assertion fail with a message about
// the wrong thing, and an `expect(paths).toEqual([])` style assertion pass while proving nothing.
function rejectionIssues(payload: unknown) {
	const result = runtimeCompanionsSchema.safeParse(payload);
	if (result.success) {
		throw new Error(
			`expected the companions envelope to reject ${JSON.stringify(payload)}, but it parsed`,
		);
	}
	return result.error.issues;
}

function rejectionIssuePaths(payload: unknown): string[] {
	return rejectionIssues(payload).map((issue) => issue.path.join("."));
}

function rejectionIssueCodesAt(payload: unknown, path: string): string[] {
	return rejectionIssues(payload)
		.filter((issue) => issue.path.join(".") === path)
		.map((issue) => String(issue.code));
}

// A complete, valid `companions.filebrowser` wire object. Every value is a literal the schema
// accepts, so this is the exact payload a provisioning image emits -- which is what makes
// the wire-parity assertion below meaningful rather than a round-trip of a synthetic shape.
function fileBrowserWireEntry(accessRevision = "a".repeat(64)) {
	const audience = "clawdi-files:hdep_files_envelope";
	const release = `https://github.com/gtsteffaniak/filebrowser/releases/download/${FILE_BROWSER_VERSION}`;
	return {
		version: FILE_BROWSER_VERSION,
		commit: FILE_BROWSER_COMMIT,
		listen: "0.0.0.0",
		port: FILE_BROWSER_PORT,
		baseURL: "/",
		healthPath: "/health",
		sourceRoot: "/home/iforge",
		assets: {
			amd64: {
				url: `${release}/linux-amd64-filebrowser`,
				sha256: FILE_BROWSER_AMD64_SHA256,
			},
			arm64: {
				url: `${release}/linux-arm64-filebrowser`,
				sha256: FILE_BROWSER_ARM64_SHA256,
			},
		},
		auth: {
			method: "jwt",
			algorithm: "HS256",
			header: "X-JWT-Assertion",
			userIdentifier: "sub",
			groupsClaim: "groups",
			secret: accessRevision.slice(0, 43),
			audience,
			subject: "deployment:hdep_files_envelope:owner",
			requiredGroup: `${audience}:${accessRevision}`,
			accessRevision,
		},
	};
}

describe("companions envelope", () => {
	test("admits an unrecognised companion kind that presents a kind identity and a pinned artifact", () => {
		const wire = { future: { kind: "future", artifact: { sha256: SHA256_HEX } } };
		const result = runtimeCompanionsSchema.safeParse(wire);

		expect(result.success).toBe(true);
		// COMP-02: the entry survives the parse verbatim so a later CLI can act on it.
		expect(result.success && result.data).toEqual(wire);
	});

	test("rejects a typo key whose value is malformed, naming the offending key in every issue path", () => {
		const paths = rejectionIssuePaths({ filebrowzer: { a: 1 } });

		expect(paths).toContain("filebrowzer.kind");
		expect(paths).toContain("filebrowzer.artifact");
		expect(paths).toContain("filebrowzer");
		expect(rejectionIssueCodesAt({ filebrowzer: { a: 1 } }, "filebrowzer")).toContain(
			"unrecognized_keys",
		);
	});

	test("rejects an unrecognised kind whose artifact digest is not 64 hex characters", () => {
		// COMP-06: fail closed rather than fetch an artifact that is not digest-pinned.
		expect(
			rejectionIssuePaths({ future: { kind: "future", artifact: { sha256: "nope" } } }),
		).toContain("future.artifact.sha256");
	});

	test("rejects an unrecognised kind that carries no artifact at all", () => {
		// COMP-06: an absent pin is the same wire event as a malformed one.
		expect(rejectionIssuePaths({ future: { kind: "future" } })).toContain("future.artifact");
	});

	test("rejects a well-formed entry whose declared kind disagrees with its key", () => {
		// The hole D-05 omits: without the kind/key guard this input PARSES and the mis-keyed
		// entry is silently treated as a future kind to skip. Measured in 04-RESEARCH.md.
		const payload = {
			filebrowzer: { kind: "filebrowser", artifact: { sha256: SHA256_HEX } },
		};

		expect(rejectionIssuePaths(payload)).toContain("filebrowzer.kind");
		expect(rejectionIssueCodesAt(payload, "filebrowzer.kind")).toContain("custom");
	});

	test("admits a companion-less manifest", () => {
		const result = runtimeCompanionsSchema.safeParse({});

		expect(result.success).toBe(true);
		expect(result.success && result.data).toEqual({});
	});

	test("rejects a key outside the registry charset before it can become a unit or path component", () => {
		// T-4-01: the key becomes a systemd unit name, a UID lookup name, a `StateDirectory=`
		// value and a path component. It is bounded here, at the trust boundary, not downstream.
		expect(
			rejectionIssuePaths({ "../etc": { kind: "../etc", artifact: { sha256: SHA256_HEX } } }),
		).toContain("../etc");
	});

	test("leaves the serialized filebrowser entry byte-identical across the envelope change", () => {
		// D-04 binds the WIRE JSON, not the Zod source. This is the pre-change envelope shape,
		// reconstructed literally, parsed beside the shipped one on the same payload.
		const preChangeEnvelope = z
			.object({ filebrowser: fileBrowserCompanionSchema.optional() })
			.strict();
		const wire = { filebrowser: fileBrowserWireEntry() };

		const before = JSON.stringify(preChangeEnvelope.parse(wire));
		const after = JSON.stringify(runtimeCompanionsSchema.parse(wire));

		expect(after).toBe(before);
		expect(after).toBe(JSON.stringify(wire));
	});
});

// The cross-language golden entry. `backend/tests/test_runtime_companions_envelope.py` parses the
// SAME file, so the Zod half and the Pydantic half are pinned to each other by one wire payload
// rather than by two hand-written copies that can drift apart.
const POSTIZ_FIXTURE_PATH = resolve(
	import.meta.dir,
	"../../../../test-fixtures/companions/postiz-manifest-entry.json",
);

function postizFixture(): Record<string, unknown> {
	return JSON.parse(readFileSync(POSTIZ_FIXTURE_PATH, "utf-8")) as Record<string, unknown>;
}

describe("postiz manifest entry", () => {
	test("the envelope declares postiz, so a postiz entry is governed by its own schema", () => {
		// Before this property existed the envelope's superRefine SKIPPED every known kind, so a
		// postiz entry of any shape parsed unvalidated (06-03's recorded gap).
		expect(Object.keys(runtimeCompanionsSchema.shape)).toContain("postiz");
	});

	test("the envelope round-trips the shared fixture byte-identically", () => {
		const wire = { postiz: postizFixture() };

		expect(JSON.stringify(runtimeCompanionsSchema.parse(wire))).toBe(JSON.stringify(wire));
	});

	test("the envelope refuses the shared fixture once it carries an undeclared field", () => {
		const entry = { ...postizFixture(), tag: "latest" };

		expect(runtimeCompanionsSchema.safeParse({ postiz: entry }).success).toBe(false);
	});

	test("postizCompanionSchema parses the shared fixture and re-serializes it to the same object", () => {
		const fixture = postizFixture();
		const parsed: unknown = postizCompanionSchema.parse(fixture);

		expect(parsed).toEqual(fixture);
		expect(JSON.stringify(parsed)).toBe(JSON.stringify(fixture));
	});

	test("the dependency set is exactly postgres, redis and cloudflared (D-04, CK-A)", () => {
		// CK-A = own-redis-shared-temporal: Redis is a per-installation container, Temporal is
		// connection material with no image slot, and no search-engine dependency exists.
		expect(Object.keys(postizCompanionSchema.shape.dependencies.shape).sort()).toEqual([
			"cloudflared",
			"postgres",
			"redis",
		]);
		const { redis: _redis, ...withoutRedis } = postizFixture().dependencies as Record<
			string,
			unknown
		>;
		expect(
			postizCompanionSchema.safeParse({ ...postizFixture(), dependencies: withoutRedis }).success,
		).toBe(false);
	});

	test("the postgres slot names the house image repository from config/postgres-image.txt", () => {
		const authority = readFileSync(
			resolve(import.meta.dir, "../../../../config/postgres-image.txt"),
			"utf-8",
		).trim();

		expect(authority.split(":pg18@")[0]).toBe(POSTIZ_POSTGRES_IMAGE);
	});
});

// The refusal matrix. Its cases live in ONE shared file that
// `backend/tests/test_runtime_companions_envelope.py` also reads, so the Zod half and the Pydantic
// half are asserted against the same mutations by construction rather than by two hand-kept lists.
interface PostizRefusalCase {
	refusal: string;
	case: string;
	op: "set" | "delete";
	path: string[];
	value?: unknown;
	/** The dotted object path every rejection issue must sit under -- the intended reason. */
	at: string;
}

const POSTIZ_REFUSAL_CASES = JSON.parse(
	readFileSync(
		resolve(import.meta.dir, "../../../../test-fixtures/companions/postiz-refusal-cases.json"),
		"utf-8",
	),
) as PostizRefusalCase[];

function mutatedPostizFixture(mutation: PostizRefusalCase): Record<string, unknown> {
	const entry = postizFixture();
	let target = entry;
	for (const key of mutation.path.slice(0, -1)) target = target[key] as Record<string, unknown>;
	const last = mutation.path[mutation.path.length - 1] as string;
	if (mutation.op === "delete") delete target[last];
	else target[last] = mutation.value;
	return entry;
}

function isUnder(path: string, at: string): boolean {
	return at === "" || path === at || path.startsWith(`${at}.`);
}

describe("postiz refusal matrix", () => {
	test("covers every refusal the contract must make", () => {
		expect(new Set(POSTIZ_REFUSAL_CASES.map((mutation) => mutation.refusal))).toEqual(
			new Set([
				"missing-digest",
				"malformed-digest",
				"tag-field",
				"foreign-repository",
				"undeclared-dependency",
				"unknown-top-level-field",
				"non-canonical-credential-ref",
				"lifecycle-outside-vocabulary",
				"uppercase-hostname-label",
				"single-label-hostname",
				"too-many-export-requests",
				"wrong-port",
			]),
		);
	});

	for (const mutation of POSTIZ_REFUSAL_CASES) {
		test(`refuses ${mutation.refusal}: ${mutation.case}`, () => {
			const result = postizCompanionSchema.safeParse(mutatedPostizFixture(mutation));
			if (result.success) throw new Error(`expected ${mutation.case} to be refused, but it parsed`);

			const paths = result.error.issues.map((issue) => issue.path.join("."));
			// Every issue sits under the mutated object, so the refusal is for the intended reason
			// and not an unrelated field the mutation happened to disturb.
			expect(paths.length).toBeGreaterThan(0);
			expect(paths.filter((path) => !isUnder(path, mutation.at))).toEqual([]);
		});
	}

	test("the envelope refuses a postiz entry that fails postizCompanionSchema, naming postiz", () => {
		const [mutation] = POSTIZ_REFUSAL_CASES;
		const result = runtimeCompanionsSchema.safeParse({
			postiz: mutatedPostizFixture(mutation as PostizRefusalCase),
		});
		if (result.success) throw new Error("expected the envelope to refuse the entry");

		expect(result.error.issues.map((issue) => issue.path[0])).toContain("postiz");
	});

	for (const field of ["backupEnvSecretRef", "tunnelTokenSecretRef"]) {
		test(`an optional credential ref (${field}) must be omitted rather than null`, () => {
			// Parity with the Pydantic half, which refuses the explicit null by validator.
			const entry = postizFixture();
			(entry.credentials as Record<string, unknown>)[field] = null;
			const result = postizCompanionSchema.safeParse(entry);
			if (result.success) throw new Error(`expected a null ${field} to be refused`);

			expect(result.error.issues.map((issue) => issue.path.join("."))).toEqual([
				`credentials.${field}`,
			]);
		});
	}
});

describe("postiz isolation tiers (D-06)", () => {
	test("the contract admits all three tiers and offers only standard", () => {
		expect([...COMPANION_ISOLATION_TIERS]).toEqual(["standard", "dedicated", "shared"]);
		expect([...OFFERED_ISOLATION_TIERS]).toEqual(["standard"]);
	});

	for (const tier of ["dedicated", "shared"] as const) {
		test(`${tier} parses on the wire but assertOfferedIsolationTier refuses it by name`, () => {
			const entry = postizCompanionSchema.parse({ ...postizFixture(), isolationTier: tier });

			expect(entry.isolationTier).toBe(tier);
			expect(() => assertOfferedIsolationTier(entry)).toThrow(`isolation tier ${tier}`);
		});
	}

	test("standard is offered", () => {
		const entry = postizCompanionSchema.parse(postizFixture());

		expect(() => assertOfferedIsolationTier(entry)).not.toThrow();
	});
});

// CR-01 (plan 06-35): a backup repository path is the installation's own directory or one
// abandoned-backups generation inside it, `/r<n>` with no leading zero and at most six digits.
// These two lists are repeated value for value in
// `backend/tests/test_runtime_companions_envelope.py`, so a drift between the two parsers fails
// one side.
const BACKUP_REPO_PATHS_ACCEPTED = ["/t/i", "/t/i/r1", "/t/i/r999999"];
const BACKUP_REPO_PATHS_REFUSED = [
	"/t/i/r0",
	"/t/i/r01",
	"/t/i/x1",
	"/t/i/r1/extra",
	"/t/i/r1000000",
];

function postizWithBackupRepoPath(repoPath: string): Record<string, unknown> {
	return { ...postizFixture(), backup: { stanza: "postiz-abc", repoPath } };
}

describe("postiz backup repository path (CR-01)", () => {
	for (const repoPath of BACKUP_REPO_PATHS_ACCEPTED) {
		test(`admits ${repoPath}`, () => {
			const entry = postizCompanionSchema.parse(postizWithBackupRepoPath(repoPath));

			expect(entry.backup?.repoPath).toBe(repoPath);
		});
	}

	for (const repoPath of BACKUP_REPO_PATHS_REFUSED) {
		test(`refuses ${repoPath}`, () => {
			const result = postizCompanionSchema.safeParse(postizWithBackupRepoPath(repoPath));
			if (result.success) throw new Error(`expected ${repoPath} to be refused, but it parsed`);

			expect(result.error.issues.map((issue) => issue.path.join("."))).toEqual(["backup.repoPath"]);
		});
	}
});

// Plan 06-38: `adminUserId` is the Postiz admin user id the control plane recorded from the
// installation's ready report, null before one. The runtime proves ownership of an existing
// organization by it. These cases are repeated value for value in
// `backend/tests/test_companion_projection.py`, so a drift between the two parsers fails one side.
const ADMIN_USER_IDS_ACCEPTED: Array<string | null> = [
	null,
	"7c1d9a64-2b3e-4f5a-8c6d-0e1f2a3b4c5d",
	"a".repeat(64),
];
const ADMIN_USER_IDS_REFUSED = ["", "a".repeat(65)];

describe("postiz recorded admin user id (plan 06-38)", () => {
	for (const adminUserId of ADMIN_USER_IDS_ACCEPTED) {
		test(`admits ${JSON.stringify(adminUserId)}`, () => {
			const entry = postizCompanionSchema.parse({ ...postizFixture(), adminUserId });

			expect(entry).toMatchObject({ adminUserId });
		});
	}

	test("defaults an absent adminUserId to null", () => {
		const { adminUserId: _omitted, ...withoutId } = postizFixture();
		const entry = postizCompanionSchema.parse(withoutId);

		expect(entry).toMatchObject({ adminUserId: null });
	});

	for (const adminUserId of ADMIN_USER_IDS_REFUSED) {
		test(`refuses a ${adminUserId.length}-character adminUserId`, () => {
			const result = postizCompanionSchema.safeParse({ ...postizFixture(), adminUserId });
			if (result.success) throw new Error("expected the adminUserId to be refused, but it parsed");

			expect(result.error.issues.map((issue) => issue.path.join("."))).toEqual(["adminUserId"]);
		});
	}
});

// Plan 06-41 (CR-03): the announced app-digest transition, `{fromDigest, toDigest}` while an
// accepted update runs and null otherwise. These lists are repeated value for value in
// `backend/tests/test_runtime_companions_envelope.py`, so a drift between the two parsers fails
// one side. Absent is accepted too, and defaults to null on both sides.
const UPDATE_ANNOUNCEMENTS_ACCEPTED: Array<{ fromDigest: string; toDigest: string } | null> = [
	null,
	{ fromDigest: "a".repeat(64), toDigest: "b".repeat(64) },
];
const UPDATE_ANNOUNCEMENTS_REFUSED: Array<Record<string, string>> = [
	{ fromDigest: "A".repeat(64), toDigest: "b".repeat(64) },
	{ fromDigest: "a".repeat(63), toDigest: "b".repeat(64) },
	{ fromDigest: "a".repeat(64), toDigest: "b".repeat(64), version: "v2.25.0" },
];

describe("postiz update announcement (plan 06-41)", () => {
	for (const update of UPDATE_ANNOUNCEMENTS_ACCEPTED) {
		test(`admits ${JSON.stringify(update)}`, () => {
			const entry = postizCompanionSchema.parse({ ...postizFixture(), update });

			expect(entry.update).toEqual(update);
		});
	}

	test("an absent announcement defaults to null", () => {
		const { update: _absent, ...fixture } = postizFixture() as Record<string, unknown>;

		expect(postizCompanionSchema.parse(fixture).update).toBeNull();
	});

	for (const update of UPDATE_ANNOUNCEMENTS_REFUSED) {
		test(`refuses ${JSON.stringify(update)}`, () => {
			const result = postizCompanionSchema.safeParse({ ...postizFixture(), update });
			if (result.success) {
				throw new Error(`expected ${JSON.stringify(update)} to be refused, but it parsed`);
			}

			const paths = result.error.issues.map((issue) => issue.path.join("."));
			expect(paths.length).toBeGreaterThan(0);
			expect(paths.filter((path) => path !== "update" && !path.startsWith("update."))).toEqual([]);
		});
	}
});

// Plan 06-42 (CR-04): the agent's retired installations of the kind, at most 8 `{installationId}`
// objects with a lowercase UUID. These lists are repeated value for value in
// `backend/tests/test_runtime_companions_envelope.py`, so a drift between the two parsers fails
// one side. Absent is accepted too, and defaults to [] on both sides.
function retiredInstallations(count: number): Array<{ installationId: string }> {
	return Array.from({ length: count }, (_, index) => ({
		installationId: `${index.toString(16).padStart(8, "0")}-6d2f-4e8b-a7c3-5f1d2e3a4b6c`,
	}));
}

const RETIRED_INSTALLATIONS_ACCEPTED: Array<Array<Record<string, string>>> = [
	[],
	retiredInstallations(1),
	retiredInstallations(8),
];
const RETIRED_INSTALLATIONS_REFUSED: Array<Array<Record<string, string>>> = [
	retiredInstallations(9),
	[{ installationId: "0B9E4C1A-6D2F-4E8B-A7C3-5F1D2E3A4B6C" }],
	[{ installationId: "0b9e4c1a-6d2f-4e8b-a7c3-5f1d2e3a4b6c", kind: "postiz" }],
];

describe("postiz retired installations (plan 06-42)", () => {
	for (const retired of RETIRED_INSTALLATIONS_ACCEPTED) {
		test(`admits ${retired.length} retired installation(s)`, () => {
			const entry = postizCompanionSchema.parse({
				...postizFixture(),
				retiredInstallations: retired,
			});

			expect(entry.retiredInstallations).toEqual(retired as Array<{ installationId: string }>);
		});
	}

	test("absent retired installations default to []", () => {
		const { retiredInstallations: _absent, ...fixture } = postizFixture() as Record<
			string,
			unknown
		>;

		expect(postizCompanionSchema.parse(fixture).retiredInstallations).toEqual([]);
	});

	for (const retired of RETIRED_INSTALLATIONS_REFUSED) {
		test(`refuses ${JSON.stringify(retired).slice(0, 80)}`, () => {
			const result = postizCompanionSchema.safeParse({
				...postizFixture(),
				retiredInstallations: retired,
			});
			if (result.success) {
				throw new Error(`expected ${JSON.stringify(retired)} to be refused, but it parsed`);
			}

			const paths = result.error.issues.map((issue) => issue.path.join("."));
			expect(paths.length).toBeGreaterThan(0);
			expect(
				paths.filter(
					(path) => path !== "retiredInstallations" && !path.startsWith("retiredInstallations."),
				),
			).toEqual([]);
		});
	}
});
