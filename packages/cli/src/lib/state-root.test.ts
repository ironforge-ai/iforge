import { afterEach, describe, expect, it } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { basename, join, sep } from "node:path";

import { stateRoot, stateRootFor } from "./state-root";

/**
 * The state root is the one directory this CLI keeps credentials, tokens, environments and
 * daemon state in, so these are assertions about where a user's secrets land. The authority had
 * no direct unit test before this file: every existing assertion reached it through a caller and
 * pinned the finished path as a literal, which is why the legacy segment was restated in three
 * test files at once.
 *
 * `.clawdi` appears below only as the thing being asserted ABSENT. Nothing here may derive an
 * expectation from it.
 */

const LEGACY_SEGMENT = ".clawdi";
const originalEnv = { ...process.env };
const tempDirs: string[] = [];

function tempHome(prefix: string): string {
	const dir = mkdtempSync(join(tmpdir(), prefix));
	tempDirs.push(dir);
	return dir;
}

afterEach(() => {
	process.env = { ...originalEnv };
	while (tempDirs.length > 0) {
		const dir = tempDirs.pop();
		if (dir) rmSync(dir, { recursive: true, force: true });
	}
});

describe("stateRoot", () => {
	it("resolves under HOME when no override is set", () => {
		const home = tempHome("iforge-state-root-");
		process.env.HOME = home;
		delete process.env.CLAWDI_HOME;

		const resolved = stateRoot();

		expect(resolved.startsWith(home + sep)).toBe(true);
		expect(basename(resolved)).toBe(".iforge");
		expect(resolved).toBe(join(home, ".iforge"));
	});

	it("returns the CLAWDI_HOME override verbatim, ignoring HOME", () => {
		// Kept, not removed: RESEARCH M-1 measured all five pre-collapse resolvers honouring this
		// override, so dropping it here would be a behaviour change to the sibling dev wrapper,
		// the test harnesses and the multi-tenant service accounts — not a cleanup.
		const home = tempHome("iforge-state-root-home-");
		const isolated = tempHome("iforge-state-root-override-");
		process.env.HOME = home;
		process.env.CLAWDI_HOME = isolated;

		expect(stateRoot()).toBe(isolated);
	});

	it("treats an empty override as absence rather than a redirect to the filesystem root", () => {
		// An empty string is unset everywhere else in this CLI's env handling. Letting it win here
		// would resolve the credential tree to `""` — i.e. relative to the process's cwd — for any
		// caller that exported the name without a value.
		const home = tempHome("iforge-state-root-empty-");
		process.env.HOME = home;
		process.env.CLAWDI_HOME = "";

		expect(stateRoot()).toBe(join(home, ".iforge"));
	});

	it("falls back to the runtime home directory when HOME is unset, rather than throwing", () => {
		delete process.env.HOME;
		delete process.env.CLAWDI_HOME;

		expect(stateRoot()).toBe(join(homedir(), ".iforge"));
	});

	it("is computed on every call, so a reassigned HOME changes the result", () => {
		// The reason this matters is not theoretical: every test harness in this package overrides
		// HOME per run, and a value frozen at module import would silently point them all at the
		// real user's tree.
		const first = tempHome("iforge-state-root-first-");
		const second = tempHome("iforge-state-root-second-");
		delete process.env.CLAWDI_HOME;

		process.env.HOME = first;
		const before = stateRoot();
		process.env.HOME = second;
		const after = stateRoot();

		expect(before).toBe(join(first, ".iforge"));
		expect(after).toBe(join(second, ".iforge"));
		expect(after).not.toBe(before);
	});

	it("never resolves under the legacy dot-directory for any home it derives from", () => {
		// P33-04: the resolver is unconditional. There is no fallback read of upstream's root, no
		// deprecation branch and no dual path.
		delete process.env.CLAWDI_HOME;
		const homes = [
			tempHome("iforge-state-root-a-"),
			tempHome("iforge-state-root-b-"),
			"/tmp/iforge-state-root-c",
			"/",
		];

		for (const home of homes) {
			process.env.HOME = home;
			expect(stateRoot().split(sep)).not.toContain(LEGACY_SEGMENT);
		}

		delete process.env.HOME;
		expect(stateRoot().split(sep)).not.toContain(LEGACY_SEGMENT);
	});
});

describe("stateRootFor", () => {
	it("resolves the named home to the same root stateRoot would, without needing HOME set first", () => {
		// The reason this accessor exists: a fixture builds its sandbox home BEFORE assigning it to
		// HOME, and `stateRoot()` a line too early resolves under the developer's real home. Both
		// answers must agree once the assignment lands, or the fixture and the code under test are
		// seeding two different trees.
		const home = tempHome("iforge-state-root-for-");
		delete process.env.CLAWDI_HOME;

		const derived = stateRootFor(home);

		process.env.HOME = home;
		expect(derived).toBe(stateRoot());
		expect(basename(derived)).toBe(".iforge");
	});

	it("ignores both HOME and the override, answering only about the home it was given", () => {
		const named = tempHome("iforge-state-root-for-named-");
		process.env.HOME = tempHome("iforge-state-root-for-ambient-");
		process.env.CLAWDI_HOME = tempHome("iforge-state-root-for-override-");

		expect(stateRootFor(named)).toBe(join(named, ".iforge"));
	});

	it("never resolves under the legacy dot-directory for any home it is given", () => {
		for (const home of [
			tempHome("iforge-state-root-for-legacy-"),
			"/tmp/iforge-state-root-d",
			"/",
		]) {
			expect(stateRootFor(home).split(sep)).not.toContain(LEGACY_SEGMENT);
		}
	});
});
