import { realpathSync } from "node:fs";
import { basename, dirname, resolve, sep } from "node:path";

/**
 * Runtime containment guards for agent install roots.
 *
 * Two Hermes-lineage runtimes coexist — IronHermes at `$IRONHERMES_HOME` and
 * upstream Hermes at `$HERMES_HOME` — and neither may ever write into the
 * other's tree. Both variables are user-controlled path inputs, so CORRECT code
 * still writes to the wrong place when one is pointed at the other's install.
 * A test can only prove the code is right; only a runtime check catches the
 * misconfiguration (D-21, T-02-11).
 *
 * Both assertions THROW. They never log-and-skip: a silent skip would leave the
 * caller believing a Skill was written when it was not, which is a worse
 * failure than a loud stop, and the stated strategy is to fail closed.
 *
 * Depends on `node:path` and `node:fs` only — no new packages.
 */

/**
 * Absolute path with the longest EXISTING prefix resolved through symlinks and
 * the remainder appended literally.
 *
 * Plain `realpathSync` throws for a path that does not exist yet, and every
 * interesting target here is a Skill directory about to be created. Plain
 * `resolve` cannot see through a symlink, and macOS hands out `/var/...` temp
 * paths that are really `/private/var/...` — comparing those two forms
 * naively produces a false refusal of a genuinely contained path.
 *
 * `resolve` runs first, so `..` segments are collapsed BEFORE anything is
 * compared. That is what makes traversal refusal a resolution result rather
 * than a string check (T-02-12).
 */
function canonicalize(inputPath: string): string {
	const absolute = resolve(inputPath);
	let head = absolute;
	const tail: string[] = [];
	for (;;) {
		try {
			const real = realpathSync(head);
			return tail.length === 0 ? real : [real, ...tail].join(sep);
		} catch {
			const parent = dirname(head);
			// Filesystem root reached without finding anything that exists.
			if (parent === head) return absolute;
			tail.unshift(basename(head));
			head = parent;
		}
	}
}

/** Path segments, empty components dropped. */
function segments(canonicalPath: string): string[] {
	return canonicalPath.split(sep).filter(Boolean);
}

/**
 * True when `target` IS `root` or lives beneath it.
 *
 * Compares resolved path SEGMENTS. A raw string-prefix comparison would accept
 * a sibling whose name merely starts with the root's name — `/a/skills-evil`
 * "starts with" `/a/skills`.
 */
function rootContains(rootCanonical: string, targetCanonical: string): boolean {
	const rootParts = segments(rootCanonical);
	const targetParts = segments(targetCanonical);
	if (targetParts.length < rootParts.length) return false;
	return rootParts.every((part, index) => targetParts[index] === part);
}

/**
 * Refuse a mutation target that does not resolve inside `agentRoot`.
 *
 * `context` names the write so a refusal message says which operation stopped.
 * The message carries BOTH the offending target and the root it escaped —
 * a refusal the operator cannot locate is barely better than a silent one.
 */
export function assertAgentRootContains(
	agentRoot: string,
	targetPath: string,
	context: string,
): void {
	const rootCanonical = canonicalize(agentRoot);
	const targetCanonical = canonicalize(targetPath);
	if (rootContains(rootCanonical, targetCanonical)) return;
	throw new Error(
		`${context}: refusing to write ${targetCanonical} — it resolves outside the agent install root ${rootCanonical}`,
	);
}

/**
 * The isolation rule itself, stated ONCE.
 *
 * Returns the offending pair in canonical form, or `null` when the root is
 * isolated. Both public forms below are thin wrappers: the throwing assertion
 * the write paths need, and the predicate the read paths need. Two callers with
 * two answer shapes, but only one definition of what "isolated" means — which
 * is what stops the read half drifting into a re-derived rule of its own
 * (T-02-05).
 *
 * Directional on purpose. Only "my root is inside theirs" is a violation,
 * because that is the arrangement in which this runtime's own legitimate
 * writes land in the other's tree. The mirror case — a foreign root nested
 * inside this one — is not reported here: this runtime operating inside its own
 * tree is correct, and the foreign runtime detects that same collision from
 * its own side, where ITS root is the nested one.
 *
 * `assertAgentRootContains` alone cannot catch this: when `$IRONHERMES_HOME`
 * points at the Hermes tree, the target IS inside the (wrong) root, so
 * containment passes. Only comparing against the other runtime's root sees it.
 */
function findContainingForeignRoot(
	agentRoot: string,
	foreignRoots: readonly string[],
): { rootCanonical: string; foreignCanonical: string } | null {
	const rootCanonical = canonicalize(agentRoot);
	for (const foreignRoot of foreignRoots) {
		const foreignCanonical = canonicalize(foreignRoot);
		if (rootContains(foreignCanonical, rootCanonical)) {
			return { rootCanonical, foreignCanonical };
		}
	}
	return null;
}

/**
 * Refuse an agent root that has been pointed at, or inside, another live
 * runtime's install root.
 *
 * Used by every mutation, and by the READ paths that collect sessions and
 * skills — collecting from the wrong tree uploads another runtime's data under
 * this identity, which is the harm itself, so those fail closed too.
 */
export function assertAgentRootIsolated(
	agentRoot: string,
	foreignRoots: readonly string[],
	context: string,
): void {
	const violation = findContainingForeignRoot(agentRoot, foreignRoots);
	if (!violation) return;
	throw new Error(
		`${context}: refusing to continue — the agent install root ${violation.rootCanonical} resolves inside another runtime's install root ${violation.foreignCanonical}; check $IRONHERMES_HOME and $HERMES_HOME`,
	);
}

/**
 * The non-throwing form of {@link assertAgentRootIsolated}, for the one caller
 * that must not throw: `detect()`.
 *
 * `src/lib/select-adapter.ts` fans every adapter's `detect()` into a single
 * `Promise.all`, and no `detect()` call site wraps it in try/catch. A throw
 * from one runtime's detection would reject that whole batch — turning a
 * mis-set `$IRONHERMES_HOME` into `iforge push` failing for Claude Code, Codex
 * and OpenClaw as well. Answering `false` is also the truthful answer to the
 * question `detect()` asks: a root that fails containment is not a correctly
 * installed instance of this runtime.
 */
export function isAgentRootIsolated(agentRoot: string, foreignRoots: readonly string[]): boolean {
	return findContainingForeignRoot(agentRoot, foreignRoots) === null;
}
