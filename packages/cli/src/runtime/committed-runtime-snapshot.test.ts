import { afterEach, expect, spyOn, test } from "bun:test";
import {
	chmodSync,
	cpSync,
	existsSync,
	mkdirSync,
	mkdtempSync,
	readdirSync,
	readFileSync,
	renameSync,
	rmSync,
	statSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { z } from "zod";
import { commitRuntimeAppliedState, runtimeAppliedContentIdentity } from "../commands/runtime";
import { runtimeVerify } from "../commands/runtime-doctor";
import { readRuntimeAppliedState, writeRuntimeAppliedState } from "./applied-state";
import { type RuntimeApplyContext, resolveRuntimeApplyGeneration } from "./apply-identity";
import { applyRuntimeBundleChannelsToManifestLoad } from "./channels";
import { runtimeConvergenceWithoutApply } from "./manifest-planning";
import { cacheRuntimeLastGoodManifest } from "./manifest-secrets";
import {
	loadCommittedRuntimeManifest,
	loadRuntimeManifest,
	migrateCommittedRuntimeSnapshot,
	parseHostedRuntimeBundleV2,
	runtimeSnapshotExists,
	runtimeSnapshotPath,
} from "./manifest-source";
import { getRuntimePaths, type RuntimePaths } from "./paths";
import { ensureRuntimeStateDirs } from "./state";

const originalEnv = { ...process.env };
const originalFetch = globalThis.fetch;
const roots: string[] = [];
afterEach(() => {
	process.env = { ...originalEnv };
	globalThis.fetch = originalFetch;
	for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

function fixture(allowOfflineBoot = false) {
	const root = mkdtempSync(join(tmpdir(), "clawdi-committed-snapshot-"));
	roots.push(root);
	process.env.CLAWDI_SERVICE_STATE_DIR = join(root, "var/lib/clawdi");
	process.env.CLAWDI_RUN_DIR = join(root, "run/clawdi");
	process.env.CLAWDI_RUNTIME_HOME = join(root, "home/clawdi");
	const paths = getRuntimePaths({ mode: "hosted" });
	ensureRuntimeStateDirs(paths);
	const raw = z
		.record(z.string(), z.unknown())
		.parse(
			JSON.parse(
				readFileSync(
					join(import.meta.dir, "../../../../test-fixtures/runtime-bundle-v2.golden.json"),
					"utf8",
				),
			),
		);
	const manifest = z.record(z.string(), z.unknown()).parse(raw.manifest);
	raw.manifest = { ...manifest, recovery: { cacheManifest: true, allowOfflineBoot } };
	const parsed = parseHostedRuntimeBundleV2(raw, "test://committed-snapshot");
	const context: RuntimeApplyContext = {
		kind: "context-file",
		backend: "incus",
		identity: {
			generation: resolveRuntimeApplyGeneration(parsed.manifest),
			manifestETag: '"snapshot"',
			applyReceiptId: "snapshot-apply-receipt-0001",
			bootNonce: "snapshot-boot-nonce-000001",
		},
		manifestSource: {
			type: "http",
			url: "https://runtime.test/v1/runtime/manifest",
			auth: { type: "bearer", token: "test-token" },
		},
	};
	const load = applyRuntimeBundleChannelsToManifestLoad(
		{ ...parsed, applyContext: context },
		paths,
	);
	if (!load.sourceRevision) throw new Error("fixture has no source revision");
	// The durable compatibility pair alone: the state a host is in after losing its
	// SHA-selected snapshot, which convergence migrates back.
	cacheRuntimeLastGoodManifest(load.sourceBundle, paths, load.secretValues, load.manifest);
	removeHashedSnapshots(paths);
	writeRuntimeAppliedState(
		{
			schemaVersion: "clawdi.runtimeAppliedState.v2",
			appliedAt: new Date().toISOString(),
			instanceId: load.manifest.instanceId,
			generation: load.manifest.generation,
			applyGeneration: resolveRuntimeApplyGeneration(load.manifest),
			manifestETag: context.identity.manifestETag,
			applyReceiptId: context.identity.applyReceiptId,
			bootNonce: context.identity.bootNonce,
			etag: `"sha256:${load.sourceRevision}"`,
			sourceRevision: load.sourceRevision,
			contentIdentity: runtimeAppliedContentIdentity(load),
			activated: {},
			providerIds: [],
			projectedProviderIds: {},
		},
		paths,
	);
	return { root, paths, load, context };
}

function removeHashedSnapshots(paths: RuntimePaths) {
	const directory = dirname(paths.manifestLastGood);
	for (const name of readdirSync(directory)) {
		if (/^[0-9a-f]{64}\.json$/.test(name)) rmSync(join(directory, name));
	}
}

function copyPair(from: RuntimePaths, to: RuntimePaths) {
	mkdirSync(dirname(to.manifestLastGood), { recursive: true, mode: 0o700 });
	cpSync(from.manifestLastGood, to.manifestLastGood);
	cpSync(from.managedSecretCacheFile, to.managedSecretCacheFile);
}

function appliedSnapshot(paths: RuntimePaths, load: ReturnType<typeof fixture>["load"]) {
	return runtimeSnapshotPath(paths, runtimeAppliedContentIdentity(load).sha256);
}

test("migrates an exact durable pair into the SHA-selected snapshot, then survives pair loss", async () => {
	const { paths, context, load } = fixture();
	const authority = readFileSync(paths.appliedState, "utf8");
	const pair = [paths.manifestLastGood, paths.managedSecretCacheFile].map((path) =>
		readFileSync(path, "utf8"),
	);
	const snapshot = appliedSnapshot(paths, load);
	expect(existsSync(snapshot)).toBe(false);
	const nextContext = {
		...context,
		identity: { ...context.identity, generation: context.identity.generation + 1 },
	};
	expect(migrateCommittedRuntimeSnapshot(paths, nextContext)).toBe(true);
	const committed = loadCommittedRuntimeManifest(paths, nextContext);
	expect("manifest" in committed).toBe(true);
	if (!("manifest" in committed)) throw new Error("expected committed snapshot");
	expect(committed.sourcePath).toBe(snapshot);
	expect(committed.manifest.projection?.channels).toEqual(load.manifest.projection?.channels);
	expect(readFileSync(paths.appliedState, "utf8")).toBe(authority);
	expect(
		[paths.manifestLastGood, paths.managedSecretCacheFile].map((path) =>
			readFileSync(path, "utf8"),
		),
	).toEqual(pair);
	for (const [path, mode] of [
		[dirname(paths.manifestLastGood), 0o700],
		[snapshot, 0o600],
		[paths.manifestLastGood, 0o600],
		[paths.managedSecretCacheFile, 0o600],
	] as const) {
		const stat = statSync(path);
		expect(stat.mode & 0o777).toBe(mode);
		if (process.getuid) expect(stat.uid).toBe(process.getuid());
		if (process.getgid) expect(stat.gid).toBe(process.getgid());
	}
	globalThis.fetch = Object.assign(
		async () => {
			throw new Error("offline");
		},
		{ preconnect: () => undefined },
	);
	const offline = await loadRuntimeManifest(paths, { applyContext: context });
	expect("errors" in offline && offline.errors.join(" ")).toContain("does not allow offline boot");
	// The compatibility pair is a mirror; the SHA-selected snapshot alone is enough.
	rmSync(paths.manifestLastGood);
	rmSync(paths.managedSecretCacheFile);
	expect("manifest" in loadCommittedRuntimeManifest(paths, nextContext)).toBe(true);
});

test.each(["stale", "corrupt", "mixed"])(
	"does not migrate a %s durable pair or invent a snapshot",
	(candidate) => {
		const { paths, context, load } = fixture();
		if (candidate === "stale") {
			const raw = JSON.parse(readFileSync(paths.manifestLastGood, "utf8"));
			raw.manifest.generation -= 1;
			writeFileSync(paths.manifestLastGood, JSON.stringify(raw));
		} else if (candidate === "corrupt") writeFileSync(paths.manifestLastGood, "not json");
		else writeFileSync(paths.managedSecretCacheFile, "{}");
		expect(migrateCommittedRuntimeSnapshot(paths, context)).toBe(false);
		expect(existsSync(appliedSnapshot(paths, load))).toBe(false);
		expect("errors" in loadCommittedRuntimeManifest(paths, context)).toBe(true);
	},
);

// Hosted CLIs through 0.14.82 kept the pair under `cacheRoot`, the location local mode still
// uses. No iforge host ever ran one of them, so hosted mode has no reader for it.
test("hosted mode never reads a committed pair left under cacheRoot", async () => {
	const { paths, context, load } = fixture();
	const local = getRuntimePaths({ mode: "local" });
	expect(dirname(local.manifestLastGood)).toBe(paths.cacheRoot);
	cacheRuntimeLastGoodManifest(load.sourceBundle, local, load.secretValues, load.manifest);
	rmSync(paths.manifestLastGood);
	rmSync(paths.managedSecretCacheFile);
	expect(runtimeSnapshotExists(paths)).toBe(false);
	expect("errors" in loadCommittedRuntimeManifest(paths, context)).toBe(true);
	expect(migrateCommittedRuntimeSnapshot(paths, context)).toBe(false);
	expect(existsSync(paths.manifestLastGood)).toBe(false);
	expect(existsSync(appliedSnapshot(paths, load))).toBe(false);
	process.env.CLAWDI_RUNTIME_MODE = "hosted";
	const output = spyOn(console, "log").mockImplementation(() => undefined);
	try {
		await runtimeVerify({ json: true });
		expect(JSON.parse(output.mock.calls[0]?.[0])).toMatchObject({
			status: "ok",
			manifestCache: { path: paths.manifestLastGood, exists: false, valid: null, storage: null },
		});
	} finally {
		output.mockRestore();
	}
});

test.each([
	"manifest",
	"secrets",
	"missing-secrets",
	"mixed",
	"instance",
	"generation",
	"apply-generation",
	"authority",
	"permissions",
	"symlink",
])("rejects a durable pair with %s failure without inventing committed authority", (fault) => {
	const { root, paths, context, load } = fixture();
	const sensitive = "do-not-expose-secret-bytes";
	if (fault === "manifest") writeFileSync(paths.manifestLastGood, sensitive);
	else if (fault === "secrets") writeFileSync(paths.managedSecretCacheFile, sensitive);
	else if (fault === "missing-secrets") rmSync(paths.managedSecretCacheFile);
	else if (fault === "mixed") writeFileSync(paths.managedSecretCacheFile, "{}");
	else if (fault === "authority") rmSync(paths.appliedState);
	else if (fault === "permissions") chmodSync(paths.managedSecretCacheFile, 0o644);
	else if (fault === "symlink") {
		const target = join(root, "linked-secrets.json");
		cpSync(paths.managedSecretCacheFile, target);
		rmSync(paths.managedSecretCacheFile);
		symlinkSync(target, paths.managedSecretCacheFile);
	} else {
		const raw = JSON.parse(readFileSync(paths.manifestLastGood, "utf8"));
		if (fault === "instance") raw.manifest.instanceId = "different-instance";
		else if (fault === "generation") raw.manifest.generation += 1;
		else raw.applyGeneration = 999;
		writeFileSync(paths.manifestLastGood, JSON.stringify(raw));
	}
	const rejected = loadCommittedRuntimeManifest(paths, context);
	expect("errors" in rejected).toBe(true);
	expect(JSON.stringify(rejected)).not.toContain(sensitive);
	expect(existsSync(appliedSnapshot(paths, load))).toBe(false);
});

test("removes the pair and every snapshot when caching is disabled", () => {
	const { paths, context, load } = fixture();
	migrateCommittedRuntimeSnapshot(paths, context);
	expect("manifest" in loadCommittedRuntimeManifest(paths, context)).toBe(true);
	const disabled = {
		...load.manifest,
		recovery: { ...load.manifest.recovery, cacheManifest: false },
	};
	cacheRuntimeLastGoodManifest(load.sourceBundle, paths, load.secretValues, disabled);
	expect(existsSync(paths.manifestLastGood)).toBe(false);
	expect(existsSync(paths.managedSecretCacheFile)).toBe(false);
	expect(existsSync(appliedSnapshot(paths, load))).toBe(false);
	expect(readRuntimeAppliedState(paths)).not.toBeNull();
	expect("errors" in loadCommittedRuntimeManifest(paths, context)).toBe(true);
});

test("offline boot after migration requires the exact boot/apply identity, even after pair loss", async () => {
	const { paths, context } = fixture(true);
	migrateCommittedRuntimeSnapshot(paths, context);
	globalThis.fetch = Object.assign(
		async () => {
			throw new Error("offline");
		},
		{ preconnect: () => undefined },
	);
	expect("manifest" in (await loadRuntimeManifest(paths, { applyContext: context }))).toBe(true);
	const nextContext = {
		...context,
		identity: { ...context.identity, bootNonce: "different-boot-nonce-000001" },
	};
	const rejected = await loadRuntimeManifest(paths, { applyContext: nextContext });
	expect("errors" in rejected && rejected.errors.join(" ")).toContain(
		"does not match the current runtime apply identity",
	);
	expect("manifest" in loadCommittedRuntimeManifest(paths, nextContext)).toBe(true);
	rmSync(paths.manifestLastGood);
	rmSync(paths.managedSecretCacheFile);
	expect("manifest" in (await loadRuntimeManifest(paths, { applyContext: context }))).toBe(true);
});

test("readers stay pure and staged content cannot replace the previous applied authority", () => {
	const { paths, context, load } = fixture();
	expect("manifest" in loadCommittedRuntimeManifest(paths, context)).toBe(true);
	expect(existsSync(appliedSnapshot(paths, load))).toBe(false);
	const before = readRuntimeAppliedState(paths);
	if (!before) throw new Error("missing fixture authority");
	const next = z.record(z.string(), z.unknown()).parse(structuredClone(load.sourceBundle));
	const manifest = z.record(z.string(), z.unknown()).parse(next.manifest);
	next.manifest = { ...manifest, generation: load.manifest.generation + 1 };
	cacheRuntimeLastGoodManifest(next, paths, load.secretValues, load.manifest);
	// Simulate termination after staging/mirror replacement, before applied-state commit.
	const restored = loadCommittedRuntimeManifest(paths, context);
	expect("manifest" in restored && restored.sourceBundle).toEqual(load.sourceBundle);
	const snapshot = runtimeSnapshotPath(paths, before.contentIdentity.sha256);
	expect(statSync(snapshot).mode & 0o777).toBe(0o600);
	// A damaged mirror cannot affect the exact one-file snapshot.
	writeFileSync(paths.managedSecretCacheFile, "{}");
	expect("manifest" in loadCommittedRuntimeManifest(paths, context)).toBe(true);
});

test("migration persistence failure is explicit and leaves the durable pair readable", () => {
	const { paths, context, load } = fixture();
	// A directory at the snapshot destination fails even in the root isolated runner.
	mkdirSync(appliedSnapshot(paths, load), { mode: 0o700 });
	expect(() => migrateCommittedRuntimeSnapshot(paths, context)).toThrow(
		"could not persist verified committed runtime snapshot",
	);
	expect(loadCommittedRuntimeManifest(paths, context)).toMatchObject({
		sourcePath: paths.manifestLastGood,
	});
});

test("cache-disabled commit validates authority before deleting recoverable history", () => {
	const { paths, context, load } = fixture();
	migrateCommittedRuntimeSnapshot(paths, context);
	const disabled = {
		...load,
		manifest: { ...load.manifest, recovery: { ...load.manifest.recovery, cacheManifest: false } },
	};
	const convergence = runtimeConvergenceWithoutApply({
		load: disabled,
		paths,
		workspaceRoot: paths.workspaceRoot,
		enabledRuntimes: [],
		installErrors: [],
		projectedProviderIds: {},
	});
	const input = {
		load: disabled,
		paths,
		convergence,
		etag: "",
		sourceRevision: "invalid",
		applyIdentity: context.identity,
	};
	expect(() => commitRuntimeAppliedState(input)).toThrow();
	expect("manifest" in loadCommittedRuntimeManifest(paths, context)).toBe(true);
});

test.each(["content", "permissions", "symlink"])(
	"rejects an atomic snapshot with %s damage after pair loss",
	(fault) => {
		const { root, paths, context } = fixture();
		migrateCommittedRuntimeSnapshot(paths, context);
		const applied = readRuntimeAppliedState(paths);
		if (!applied) throw new Error("missing fixture authority");
		const snapshot = runtimeSnapshotPath(paths, applied.contentIdentity.sha256);
		if (fault === "content") writeFileSync(snapshot, '{"secretValues":"never-expose-this-value"}');
		else if (fault === "permissions") chmodSync(snapshot, 0o644);
		else {
			const target = join(root, "snapshot-target.json");
			cpSync(snapshot, target);
			rmSync(snapshot);
			symlinkSync(target, snapshot);
		}
		expect(loadCommittedRuntimeManifest(paths, context)).toMatchObject({
			sourcePath: paths.manifestLastGood,
		});
		rmSync(paths.manifestLastGood);
		rmSync(paths.managedSecretCacheFile);
		const rejected = loadCommittedRuntimeManifest(paths, context);
		expect("errors" in rejected).toBe(true);
		expect(JSON.stringify(rejected)).not.toContain("never-expose-this-value");
		expect(existsSync(paths.manifestLastGood)).toBe(false);
	},
);

// Exact content still needs the private Hosted storage contract on every candidate.
test.each(["manifest", "secrets", "directory"])(
	"canonical %s rejects unsafe mode and symlink, with no other candidate to fall back to",
	(target) => {
		const { root, paths, context, load } = fixture();
		const path =
			target === "manifest"
				? paths.manifestLastGood
				: target === "secrets"
					? paths.managedSecretCacheFile
					: dirname(paths.manifestLastGood);
		const expectedMode = target === "directory" ? 0o700 : 0o600;
		const baseline = readFileSync(paths.appliedState, "utf8");
		const selected = () => loadCommittedRuntimeManifest(paths, context);
		expect(selected()).toMatchObject({ sourcePath: paths.manifestLastGood });
		chmodSync(path, target === "directory" ? 0o755 : 0o644);
		expect("errors" in selected()).toBe(true);
		expect("manifest" in loadCommittedRuntimeManifest({ ...paths, mode: "local" }, context)).toBe(
			true,
		);
		chmodSync(path, expectedMode);
		expect(selected()).toMatchObject({ sourcePath: paths.manifestLastGood });
		const moved = join(root, `linked-${target}`);
		renameSync(path, moved);
		symlinkSync(moved, path);
		expect("errors" in selected()).toBe(true);
		// Local mode retains its prior filesystem behavior, with the same SHA fence.
		const local = loadCommittedRuntimeManifest({ ...paths, mode: "local" }, context);
		expect("manifest" in local && local.sourceBundle).toEqual(load.sourceBundle);
		expect(readFileSync(paths.appliedState, "utf8")).toBe(baseline);
	},
);

test.each(["getuid", "getgid"] as const)(
	"canonical pair rejects a different platform %s without changing content",
	(identity) => {
		const { paths, context } = fixture();
		const current = process[identity]?.();
		if (current === undefined) throw new Error("POSIX isolated lane required");
		const mocked = spyOn(process, identity).mockReturnValue(current + 1);
		try {
			expect("errors" in loadCommittedRuntimeManifest(paths, context)).toBe(true);
			expect("manifest" in loadCommittedRuntimeManifest({ ...paths, mode: "local" }, context)).toBe(
				true,
			);
		} finally {
			mocked.mockRestore();
		}
	},
);

test.each(["canonical", "hashed", "local"])(
	"runtime verify reports the selected %s storage without migrating",
	async (candidate) => {
		const { paths, context, load } = fixture();
		process.env.CLAWDI_RUNTIME_MODE = candidate === "local" ? "local" : "hosted";
		const baseline = readFileSync(paths.appliedState, "utf8");
		let expectedPath = paths.manifestLastGood;
		if (candidate === "local") {
			const local = getRuntimePaths({ mode: "local" });
			copyPair(paths, local);
			expectedPath = local.manifestLastGood;
		} else if (candidate === "hashed") {
			migrateCommittedRuntimeSnapshot(paths, context);
			expectedPath = appliedSnapshot(paths, load);
			rmSync(paths.manifestLastGood);
			rmSync(paths.managedSecretCacheFile);
		}
		const output = spyOn(console, "log").mockImplementation(() => undefined);
		try {
			await runtimeVerify({ json: true });
			const report = JSON.parse(output.mock.calls[0]?.[0]);
			expect(report).toMatchObject({
				status: "ok",
				manifestCache: {
					path: expectedPath,
					exists: true,
					valid: true,
					storage: candidate === "local" ? "local" : "durable",
				},
			});
			expect(existsSync(expectedPath)).toBe(true);
			expect(existsSync(appliedSnapshot(paths, load))).toBe(candidate === "hashed");
			expect(readFileSync(paths.appliedState, "utf8")).toBe(baseline);
		} finally {
			output.mockRestore();
		}
	},
);
