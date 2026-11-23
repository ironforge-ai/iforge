import { spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

// Install lefthook's git hooks.
//
// Phase 03.1 (WINDOWS #69/#74) found two ways this silently produced UNGATED
// commits, both specific to git worktrees:
//
//  1. Worktrees SHARE `.git/hooks` with the main checkout — a worktree has a
//     `.git` FILE pointing at the common dir, not its own hooks directory. So a
//     `lefthook install` run from inside a worktree writes that worktree's own
//     absolute `node_modules/...` path into the hook every checkout uses. When
//     the worktree is later removed, the shared hook is left pointing at a path
//     that no longer exists. Measured at the end of phase 03.1: the main tree's
//     pre-commit hook referenced `.claude/worktrees/agent-a0e46f0299fe6128d`,
//     a worktree deleted hours earlier.
//
//  2. When the hook cannot resolve a lefthook binary at all, lefthook's own
//     generated script ends with `echo "Can't find lefthook in PATH"` and
//     RETURNS ZERO. git reads that as success and commits. Nothing is printed
//     that looks like a failure, so an entire phase can commit past its own
//     gate without one visible error. That is exactly what happened: every
//     worktree commit in phase 03.1 ran ungated, and no executor used
//     `--no-verify` — the hooks were simply never found.
//
// The fix for (1) is to always install from the MAIN worktree, so the path
// baked into the shared hook belongs to a checkout that outlives every agent
// worktree. The fix for (2) is to verify afterwards and fail loudly, because a
// hook installer that cannot prove it installed a working hook is the same
// class of defect as the gate it is meant to protect.
//
// WR-02: that verification was written as INSPECTION and did not establish (2).
// It checked that the hook file exists and that its text carries no
// `.../worktrees/...` path -- neither of which says the hook can resolve a
// lefthook binary at commit time. Measured: copying the installed hook, pointing
// its baked path at /nonexistent/lefthook and running it under `env -i` printed
// "Can't find lefthook in PATH" and EXIT=0, while the installer had already said
// "hooks installed and verified". A lefthook version bump (the version is baked
// into that path), a pruned node_modules, or a fresh clone before `bun install`
// each restore silent ungated commits.
//
// So the verification is by EXECUTION now: run the hook the way git will, with a
// job filter that matches nothing so the real gate does not fire, and fail on the
// marker lefthook prints when its whole resolution chain falls through. And the
// worktree check keyed on the literal segment `worktrees/`, which catches
// `.claude/worktrees/...` by CONVENTION only -- `git worktree add ../iforge-wt`
// produces a hook it passes. It is a containment test now: every absolute path in
// the hook body must live under the main worktree.

/** Run a command, returning `{ status, stdout }` with stdout trimmed. */
function run(cmd, args, opts = {}) {
	const r = spawnSync(cmd, args, { encoding: "utf8", ...opts });
	return { status: r.status, stdout: (r.stdout ?? "").trim(), error: r.error };
}

const gitCheck = run("git", ["rev-parse", "--is-inside-work-tree"], { stdio: "ignore" });
if (gitCheck.status !== 0) {
	console.log("Skipping git hook install outside a git worktree.");
	process.exit(0);
}

// The common dir is `<main>/.git` from anywhere, including inside a worktree.
// Its parent is the main checkout — the stable place to install from.
const commonDir = run("git", ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout;
if (!commonDir) {
	console.error("install-git-hooks: could not resolve --git-common-dir; refusing to guess.");
	process.exit(1);
}
const mainWorktree = dirname(commonDir);
const hooksDir = join(commonDir, "hooks");

const here = run("git", ["rev-parse", "--show-toplevel"]).stdout;
const inWorktree = resolve(here) !== resolve(mainWorktree);
if (inWorktree) {
	console.log(
		`install-git-hooks: running from a linked worktree (${here}).\n` +
			`  Installing from the main worktree instead (${mainWorktree}) so the shared\n` +
			`  hook does not hardcode this worktree's node_modules path (WINDOWS #69).`,
	);
}

// Resolve the binary explicitly instead of trusting PATH. `lefthook` is only on
// PATH under `bun run`/`npm run` (which prepend node_modules/.bin); invoked any
// other way — a hook, a bare `node scripts/...`, CI — a bare name is ENOENT.
// Relying on an ambient PATH is the same defect this script exists to fix.
function resolveLefthook() {
	const candidates = [
		join(mainWorktree, "node_modules", ".bin", "lefthook"),
		join(mainWorktree, "node_modules", "lefthook-linux-x64", "bin", "lefthook"),
		join(mainWorktree, "node_modules", "lefthook-linux-arm64", "bin", "lefthook"),
		join(mainWorktree, "node_modules", "lefthook-darwin-arm64", "bin", "lefthook"),
		join(mainWorktree, "node_modules", "lefthook-darwin-x64", "bin", "lefthook"),
	];
	for (const c of candidates) {
		if (existsSync(c)) return c;
	}
	// Last resort: whatever PATH offers, so a globally-installed lefthook works.
	return "lefthook";
}

const lefthookBin = resolveLefthook();
const lefthook = spawnSync(lefthookBin, ["install"], { stdio: "inherit", cwd: mainWorktree });
if (lefthook.error) {
	console.error(
		`install-git-hooks: could not run lefthook (${lefthookBin}): ${lefthook.error.message}\n` +
			"  Install dependencies first (`bun install`) so node_modules/.bin/lefthook exists,\n" +
			"  or put a lefthook binary on PATH. Refusing to report success without hooks.",
	);
	process.exit(1);
}
if (lefthook.status !== 0) {
	process.exit(lefthook.status ?? 1);
}

// Verify, rather than assume. A generated hook that points into a linked
// worktree is the (1) failure above and will dangle the moment that worktree is
// removed; both checks below are cheap and catch a silently-degraded gate.
const preCommit = join(hooksDir, "pre-commit");
if (!existsSync(preCommit)) {
	console.error(`install-git-hooks: lefthook reported success but ${preCommit} does not exist.`);
	process.exit(1);
}

const body = readFileSync(preCommit, "utf8");

// Every absolute path baked into the hook must live under the main worktree. This
// replaces a substring test for `worktrees/`, which only caught this repository's
// `.claude/worktrees/...` convention: `git worktree add ../iforge-wt` produced a
// hook that passed it while pointing at a directory outside the main checkout.
// Containment is the property that actually matters, and it is convention-free.
//
// The shape deliberately requires a line start, whitespace or a quote before the
// leading slash, so it reads the hook's own `elif /abs/path/lefthook -h` and
// `"/abs/path/lefthook" "$@"` lines while ignoring `#!/bin/sh`, `>/dev/null`,
// `sed 's/x86_64/x64/'` and the `"$dir/node_modules/..."` templates, none of which
// is an installer-baked location.
const ABSOLUTE_PATH_SHAPE = /(?:^|[\s"'])(\/[^\s"'$]+)/gm;
const strayPaths = [...new Set([...body.matchAll(ABSOLUTE_PATH_SHAPE)].map((m) => m[1]))].filter(
	(p) => !p.startsWith(`${mainWorktree}/`),
);
if (strayPaths.length > 0) {
	console.error(
		"install-git-hooks: the shared pre-commit hook bakes in absolute paths that are not\n" +
			`  under the main worktree (${mainWorktree}):\n` +
			strayPaths.map((p) => `    ${p}`).join("\n") +
			"\n  A path outside the main checkout disappears when that directory does, leaving\n" +
			"  every checkout ungated. Re-run `bun install` from the main worktree.",
	);
	process.exit(1);
}

// Verify by EXECUTION (WR-02). Inspection cannot see the defect this installer
// exists to close: lefthook's generated script ends its resolution chain with
// `echo "Can't find lefthook in PATH"` and RETURNS ZERO, so git reads it as
// success and commits ungated with nothing that looks like an error.
//
// `--command` names a job that does not exist, so the resolution chain runs in
// full while the real gate does not fire -- measured at 0.01s. LEFTHOOK_BIN is
// cleared so the probe tests the chain the hook actually baked in rather than an
// override this process happens to carry; PATH is left alone, because a globally
// installed lefthook is a legitimate resolution and the hook tries it second.
const NO_SUCH_JOB = "__install_git_hooks_resolution_probe__";
const probeEnv = { ...process.env, LEFTHOOK_VERBOSE: "0" };
delete probeEnv.LEFTHOOK_BIN;
// `LEFTHOOK=0` makes the generated script exit 0 on its second line, before any
// resolution happens. A probe that inherited it would report success for a hook it
// never exercised -- the precise shape of the defect being closed.
delete probeEnv.LEFTHOOK;
const probe = spawnSync("sh", [preCommit, "--command", NO_SUCH_JOB], {
	cwd: mainWorktree,
	encoding: "utf8",
	env: probeEnv,
});
const probeOutput = `${probe.stdout ?? ""}${probe.stderr ?? ""}`;
if (probeOutput.includes("Can't find lefthook")) {
	console.error(
		"install-git-hooks: the installed hook cannot resolve a lefthook binary, so commits\n" +
			"  would run UNGATED -- lefthook's generated script prints that and exits 0, which\n" +
			"  git reads as success.\n" +
			`  Probe output: ${probeOutput.trim()}\n` +
			"  Run `bun install` from the main worktree so node_modules/.bin/lefthook exists,\n" +
			"  or put a lefthook binary on PATH.",
	);
	process.exit(1);
}
if (probe.error) {
	// Not fatal: a probe this installer could not RUN says nothing about the hook.
	// Reported rather than swallowed, because a verification that quietly did not
	// happen is the same silent gate as the one above.
	console.warn(
		`install-git-hooks: WARNING -- could not run the hook resolution probe: ${probe.error.message}`,
	);
} else if (probe.status !== 0) {
	console.warn(
		`install-git-hooks: WARNING -- the hook resolution probe exited ${probe.status}. Lefthook\n` +
			`  resolved (it did not print the not-found marker), so the gate is present, but the\n` +
			`  probe's own '--command ${NO_SUCH_JOB}' filter may no longer be a no-op.\n` +
			`  Probe output: ${probeOutput.trim()}`,
	);
}

console.log(`install-git-hooks: hooks installed and verified in ${hooksDir}`);
