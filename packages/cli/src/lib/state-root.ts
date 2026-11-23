/**
 * The single authority for where this CLI's state tree lives.
 *
 * Auth, config, environments, share tokens, locks, the daemon queue and the daemon logs all sit
 * under the directory this module returns. Before phase 03.3 the same precedence was re-derived
 * independently at five call sites (`lib/config.ts`, `runtime/paths.ts`, `serve/installer.ts`,
 * `serve/paths.ts`, `share/tokens.ts`) with no owner — five chances for a move to be half-applied
 * (P33-05). Every one of them now imports this instead.
 *
 * The resolved root is unconditional (P33-04). There is no fallback read of the state root upstream
 * `clawdi` keeps, no deprecation branch, no dual path and no migration from it (iforge names only,
 * user directive 2026-09-30). A resolver that quietly fell back would point this product and upstream `clawdi` at
 * ONE credential tree on a machine carrying both, which is the collision the whole coexistence
 * ledger exists to prevent.
 *
 * Three properties are load-bearing and must survive any future edit:
 *
 *   1. The path is computed lazily, on every call, so tests can override HOME per run and module
 *      caching does not freeze it at first import.
 *   2. `$HOME` is honoured directly rather than through `os.homedir()`, because the runtime caches
 *      its home-directory lookup at process start and does not see HOME reassigned mid-process.
 *      `homedir()` remains the fallback for a process started with no HOME at all.
 *   3. `CLAWDI_HOME` precedence is KEPT, not dropped as part of the collapse. All five original
 *      resolvers already honoured it, each documenting its own reason — a sibling development
 *      wrapper, a test harness, a multi-tenant service account — so removing it would be a
 *      behaviour change for those callers rather than a cleanup. It is an internal register name
 *      with no public spelling in `brand-env.ts` (D-07/X-1), which is why it is read directly.
 *
 * Direct unit coverage lives in `state-root.test.ts`; it had none before this module existed.
 */

import { homedir } from "node:os";
import { join } from "node:path";

/**
 * The dot-directory appended to the resolved home. Deliberately not exported: the whole point of
 * this module is that callers ask for the resolved root rather than rebuilding it from parts.
 */
const STATE_DIR_NAME = ".iforge";

/**
 * The state root a NAMED home resolves to, with no override applied.
 *
 * `stateRoot()` answers "where does state live for this process". A fixture usually has to answer
 * a different question — "where will state live for the sandbox home I am about to hand a child
 * process, or about to assign to `HOME`" — and `stateRoot()` cannot answer that one before the
 * assignment lands: called a line too early it resolves under the developer's REAL home and the
 * fixture seeds credentials there. Both questions share this module's one directory name, which
 * is the entire point: a fixture that spells the segment out itself is a second place to forget
 * on the next move, and it disagrees with the implementation the moment an override is set.
 *
 * Deliberately override-free. A fixture that has already pointed the CLI at an explicit whole-tree
 * override knows the answer without asking, and folding `CLAWDI_HOME` in here would make the
 * result depend on ambient environment the caller did not name.
 */
export function stateRootFor(home: string): string {
	return join(home, STATE_DIR_NAME);
}

/** Absolute path to the root of this CLI's state tree. */
export function stateRoot(): string {
	// An empty override is absence, not a redirect to the filesystem root: every other env read in
	// this CLI treats "" as unset, and honouring it here would resolve the credential tree
	// relative to the process's working directory.
	const override = process.env.CLAWDI_HOME;
	if (override) return override;
	return stateRootFor(process.env.HOME || homedir());
}
