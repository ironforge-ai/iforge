import { afterEach, describe, expect, test } from "bun:test";
import { join } from "node:path";

import { stateRoot } from "../lib/state-root";
import { getDaemonControlDir, getServeLogPath, getServeStateDir } from "./paths";

/**
 * The serve-state override and the state root are two different knobs, and the order between them
 * is a security property, not a convenience (T-03.3-07). `CLAWDI_STATE_DIR` is the NARROWER of the
 * two: a pod mounting a writable scratch volume uses it to move queue.jsonl and health off a
 * read-only image filesystem while auth and config stay in the credential root. If the collapse
 * onto the state-root authority had swallowed it, that same pod would have relocated the
 * credential root along with its scratch volume — silently, and with no test to notice.
 *
 * Nothing in this package asserted any of these three resolvers before this file existed.
 */

const originalEnv = { ...process.env };

afterEach(() => {
	process.env = { ...originalEnv };
});

describe("serve path resolution", () => {
	test("applies the serve-only override ahead of the state root", () => {
		process.env.HOME = "/tmp/serve-paths-home";
		delete process.env.CLAWDI_HOME;
		process.env.CLAWDI_STATE_DIR = "/mnt/scratch";

		expect(getServeStateDir("codex")).toBe(join("/mnt/scratch", "codex"));
		expect(getDaemonControlDir()).toBe(join("/mnt/scratch", "control"));
	});

	test("falls back to the state-root authority when the serve-only override is unset", () => {
		process.env.HOME = "/tmp/serve-paths-home";
		delete process.env.CLAWDI_HOME;
		delete process.env.CLAWDI_STATE_DIR;

		expect(getServeStateDir("codex")).toBe(join(stateRoot(), "serve", "codex"));
		expect(getDaemonControlDir()).toBe(join(stateRoot(), "daemon"));
	});

	test("keeps the credential root where it is when serve state is redirected", () => {
		// The negative half, and the reason the override is layered rather than merged: a redirect
		// of serve state must not move the tree holding auth.json.
		process.env.HOME = "/tmp/serve-paths-home";
		delete process.env.CLAWDI_HOME;
		process.env.CLAWDI_STATE_DIR = "/mnt/scratch";

		expect(getServeStateDir("codex").startsWith(stateRoot())).toBe(false);
		expect(stateRoot()).toBe(join("/tmp/serve-paths-home", ".iforge"));
	});

	test("resolves the whole-tree override through the authority, not around it", () => {
		process.env.HOME = "/tmp/serve-paths-home";
		process.env.CLAWDI_HOME = "/tmp/isolated-tree";
		delete process.env.CLAWDI_STATE_DIR;

		expect(getServeStateDir("codex")).toBe(join("/tmp/isolated-tree", "serve", "codex"));
		expect(getServeLogPath("codex", "stderr")).toBe(
			join("/tmp/isolated-tree", "serve", "logs", "codex.stderr.log"),
		);
	});

	test("logs follow the state root and ignore the serve-only override", () => {
		// Deliberately asymmetric and pinned as such: `getServeLogPath` does NOT read
		// `CLAWDI_STATE_DIR`, while `getServeStateDir` and `getDaemonControlDir` do. The installer
		// bakes this same log path into the unit file, so the two must agree by construction —
		// which is what resolving both through the one authority buys.
		process.env.HOME = "/tmp/serve-paths-home";
		delete process.env.CLAWDI_HOME;
		process.env.CLAWDI_STATE_DIR = "/mnt/scratch";

		expect(getServeLogPath("codex", "stderr")).toBe(
			join(stateRoot(), "serve", "logs", "codex.stderr.log"),
		);
	});
});
