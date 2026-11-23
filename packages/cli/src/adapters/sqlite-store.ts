import { existsSync } from "node:fs";

/**
 * The single dual-runtime SQLite shim for Hermes-lineage agent stores.
 *
 * Both `HermesAdapter` and `IronHermesAdapter` read `state.db` files with the
 * same access pattern, so the shim lives here once rather than being copied per
 * adapter. Anything that needs read-only SQLite access to an agent store
 * imports from this module.
 */

/**
 * Minimal SQLite shape that both `bun:sqlite` and Node's built-in
 * `node:sqlite` implement. Enough for our read-only access pattern.
 */
export interface SqliteStatement {
	all(...params: unknown[]): unknown[];
}
export interface SqliteDatabase {
	prepare(sql: string): SqliteStatement;
	close(): void;
}

/**
 * Open an agent SQLite store using the runtime's built-in binding:
 * - Under Bun: `bun:sqlite` (built-in, the dev/test default).
 * - Under Node 22.5+: `node:sqlite` (built-in; Node 22.x still emits an
 *   ExperimentalWarning at first import, harmless and one-shot).
 *
 * Neither cross-loads — Bun has no `node:sqlite` (oven-sh/bun#15561) and
 * Node has no `bun:sqlite`. The `readonly` (bun) vs `readOnly` (node) option
 * spelling asymmetry is load-bearing; do not "normalize" it.
 *
 * Importing lazily means users who never touch a Hermes-lineage agent don't pay
 * the load cost or hear the experimental warning. That laziness is why this is
 * async, and why the enumerator below has a synchronous companion for the
 * synchronous `getSessionsWatchPaths()` contract.
 *
 * No native binding (`better-sqlite3` et al) on purpose: both bindings here are
 * runtime built-ins, and a native module would add a build step to a CLI that
 * ships as a single bundle.
 */
export async function openAgentStore(path: string): Promise<SqliteDatabase> {
	if (typeof (globalThis as { Bun?: unknown }).Bun !== "undefined") {
		const { Database } = await import("bun:sqlite");
		return new Database(path, { readonly: true }) as unknown as SqliteDatabase;
	}
	const { DatabaseSync } = await import("node:sqlite");
	return new DatabaseSync(path, { readOnly: true }) as unknown as SqliteDatabase;
}

/**
 * True when `path` is a readable SQLite database carrying a `sessions` table.
 *
 * D-06 / Pitfall 1: a 0-byte `state.db` passes `existsSync` and then throws
 * `no such table: sessions` out of `prepare()`. `sqlite3 <path>` creates exactly
 * that artifact just by opening the path once. Detection must answer the
 * question it is actually being asked — "is there an agent store here" — so a
 * missing, empty or corrupt file returns false instead of crashing the caller.
 *
 * `SELECT 1 FROM sqlite_master …` is the cheapest sufficient probe: it reads the
 * schema page and nothing else.
 */
export async function storeHasSessionsTable(path: string): Promise<boolean> {
	if (!existsSync(path)) return false;
	let db: SqliteDatabase;
	try {
		db = await openAgentStore(path);
	} catch {
		// Unreadable or not a SQLite file at all.
		return false;
	}
	try {
		const found = db
			.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions' LIMIT 1")
			.all();
		return found.length > 0;
	} catch {
		return false;
	} finally {
		db.close();
	}
}
