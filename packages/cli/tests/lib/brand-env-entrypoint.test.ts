import { describe, expect, it } from "bun:test";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";

/**
 * `index.ts` applies the environment boundary on its first executable statement, but ES static
 * imports are hoisted: the entire transitive graph of the imports above that line is evaluated
 * first. The boundary therefore cannot protect a module-scope read inside that graph.
 *
 * Today the CLI is safe only by accident -- its one module-scope contract read
 * (`serve/log.ts`'s `DEBUG_ON`) sits behind a dynamic `await import("./serve.js")` inside a
 * command action, i.e. after the boundary. `runtime/paths.ts` is one static import away and
 * already reads `CLAWDI_RUNTIME_HOME` / `CLAWDI_RUNTIME_MODE` inside functions; hoisting either
 * into a module-level const would silently un-alias it with nothing to say so.
 *
 * This test states the real invariant mechanically: **no module in `index.ts`'s static import
 * closure may read a contract name at module scope.**
 *
 * Known limit, stated rather than papered over: it matches a literal `process.env.X` /
 * `process.env["X"]` access. A module-scope read routed through an indirection helper --
 * `paths.ts`'s own `envPath("CLAWDI_RUNTIME_HOME")`, say -- would not be flagged. Widening to
 * "any contract name in a string literal" is not available: `brand-env.ts` is itself in this
 * closure and holds all 44 names as string literals at module scope, so that rule would fire on
 * the boundary module every time. The literal-access form is the one the reviewed hazard takes
 * and the one a careless hoist produces.
 *
 * A SECOND limit used to go unstated (WR-09): the scan matched only `const`/`let`/`var`
 * declarations, so a module-scope read that is not a declaration was invisible to it --
 * `if (process.env.CLAWDI_DEBUG) enableTrace();` or `configure(process.env.CLAWDI_RUNTIME_MODE)`
 * at column 0 in any module of the closure. Those run before the boundary exactly as a hoisted
 * `const` does, and the comment here told the reader the invariant was enforced for a class that
 * was in fact narrower than stated. The scan now reads every top-level STATEMENT. Function and
 * class declarations are the one column-0 form still skipped, because their bodies run when
 * called rather than when imported -- which is the whole distinction being drawn. A top-level
 * `if`/`for`/`try` block is NOT skipped: it executes at import time, so its body is module scope
 * for this purpose.
 */

const SRC = join(import.meta.dir, "..", "..", "src");
const ENTRYPOINT = join(SRC, "index.ts");
const CONTRACT_PATH = join(SRC, "..", "..", "..", "config", "rebrand-env-contract.txt");

/** `import ... from "./x.js"` and `export ... from "./x.js"` -- the hoisted, relative edges. */
const STATIC_EDGE = /^\s*(?:import|export)\s[^;]*?from\s+["'](\.\.?\/[^"']+)["']/gm;

/**
 * A column-0 line whose body runs when CALLED, not when imported: a function or class
 * declaration. It is the one top-level form the scan skips, and the only one it may skip --
 * everything else at column 0 executes during module evaluation, before the boundary.
 */
const DEFERRED_BODY_OPENER =
	/^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?(?:function\b|class\b)/;

/** A column-0 line that is a comment, or a closer belonging to the statement above it. */
const NOT_A_STATEMENT_START = /^(?:\/\/|\/\*|\*|[})\];])/;

/**
 * Where a top-level statement stops running at import time and starts describing a callback.
 *
 * `index.ts` is built almost entirely out of one top-level statement per command --
 * `program.command("mcp")…​.action(async (opts) => { … })` -- so scanning whole top-level
 * statements without this cut reports every `process.env` access inside every command action as
 * a module-scope read. It found `index.ts:1332` on the first run, which is a real access but
 * runs when the `mcp` command is invoked, long after the boundary. Everything up to the first
 * `=>` or `function` keyword evaluates during module evaluation; everything after it is a body
 * that evaluates when called.
 *
 * The limit this leaves, stated rather than papered over: a genuine module-scope read written
 * AFTER a callback on the same physical line -- `const h = () => {}; const d = process.env.X;` --
 * is missed. Biome puts one statement per line here, so that shape does not occur, and the
 * conservative direction for a boundary guard is to miss a contrived line rather than to report
 * every command action in the entrypoint.
 */
const DEFERRED_BODY_WITHIN_STATEMENT = /=>|\bfunction\b/;

/** Any `process.env` access, in either accessor form. Membership is decided by the contract. */
const ENV_ACCESS = /process\.env(?:\.([A-Z0-9_]+)|\[\s*["']([A-Z0-9_]+)["']\s*\])/g;

/**
 * The internal names the boundary writes, read from the frozen contract file.
 *
 * Keying on the contract rather than on a `CLAWDI_` prefix is what makes this precise. A
 * module-scope read of a `CLAWDI_*` name the boundary never writes -- `lib/config.ts`'s
 * `CLAWDI_DEFAULT_API_URL`, which `bun build --define` substitutes at build time -- is not a
 * violation, because no public spelling can ever alias into it. Conversely, if the contract is
 * ever widened, this guard widens with it on the same commit.
 */
function contractInternalNames(): Set<string> {
	const names = new Set<string>();
	for (const raw of readFileSync(CONTRACT_PATH, "utf-8").split("\n")) {
		const line = raw.trim();
		if (!line || line.startsWith("#")) continue;
		const separator = line.indexOf("=");
		if (separator === -1) continue;
		names.add(line.slice(separator + 1));
	}
	return names;
}

/** True when `line` begins a top-level statement that runs during module evaluation. */
function startsModuleScopeStatement(line: string): boolean {
	if (line.length === 0 || /^\s/.test(line)) return false; // indented: inside something above
	if (NOT_A_STATEMENT_START.test(line)) return false;
	return !DEFERRED_BODY_OPENER.test(line);
}

/**
 * Contract names read by a module-scope STATEMENT in `source`.
 *
 * Module scope is "starts at column 0", which holds under this repo's Biome formatting: anything
 * inside a function, class or block is indented. A statement's continuation lines are indented
 * too, so a multi-line initializer -- or the body of a top-level `if` -- is collected rather than
 * missed.
 */
function moduleScopeContractReads(source: string, internalNames: Set<string>): string[] {
	const lines = source.split("\n");
	const found: string[] = [];

	for (let index = 0; index < lines.length; index += 1) {
		if (!startsModuleScopeStatement(lines[index] ?? "")) continue;

		let statement = lines[index] ?? "";
		for (let next = index + 1; next < lines.length; next += 1) {
			const line = lines[next] ?? "";
			if (line.length > 0 && !/^\s/.test(line)) break; // a new column-0 statement
			statement += `\n${line}`;
		}

		const evaluatedAtImport = statement.split(DEFERRED_BODY_WITHIN_STATEMENT)[0] ?? statement;
		for (const match of evaluatedAtImport.matchAll(ENV_ACCESS)) {
			const name = match[1] ?? match[2];
			if (name !== undefined && internalNames.has(name)) found.push(name);
		}
	}

	return found;
}

/**
 * Resolve a relative specifier to a source file. This package mixes ESM-style `./x.js`
 * specifiers (in `index.ts`) with extensionless ones (everywhere below it), so both forms have to
 * resolve or the traversal silently under-reports and the invariant goes unchecked.
 */
function resolveModule(base: string): string | undefined {
	const candidates = [
		base.replace(/\.js$/, ".ts"),
		`${base}.ts`,
		join(base, "index.ts"),
		base.replace(/\.js$/, ".tsx"),
		`${base}.tsx`,
	];
	return candidates.find((candidate) => existsSync(candidate));
}

/** Every package-local module reachable from `entry` through hoisted static imports. */
function staticImportClosure(entry: string): string[] {
	const seen = new Set<string>();
	const queue = [entry];

	while (queue.length > 0) {
		const current = queue.pop();
		if (current === undefined || seen.has(current)) continue;
		seen.add(current);

		const source = readFileSync(current, "utf-8");
		for (const match of source.matchAll(STATIC_EDGE)) {
			const specifier = match[1];
			if (specifier === undefined) continue;
			const resolved = resolveModule(join(dirname(current), specifier));
			if (resolved !== undefined) queue.push(resolved);
		}
	}

	return [...seen].sort();
}

describe("the CLI entrypoint's static import closure", () => {
	const internalNames = contractInternalNames();
	const closure = staticImportClosure(ENTRYPOINT);

	// Fail closed, twice over. A specifier form this resolver misses would shrink the closure to
	// the handful of files `index.ts` names directly, and a contract parse that stopped matching
	// would empty the name set -- either way the assertion below passes while checking nothing.
	// That is precisely the failure mode this test exists to prevent, so it must not be
	// reintroduced here. (The bound below already caught one: an earlier resolver handled only
	// `./x.js` and silently dropped every extensionless edge, reaching 7 modules instead of 18.)
	it("is traversed, not merely attempted", () => {
		expect(internalNames.size).toBeGreaterThan(40);
		expect(closure.length).toBeGreaterThan(10);
		for (const expected of [
			"commands/serve-cli.ts",
			"lib/brand-env.ts",
			"lib/errors.ts",
			"runtime/host-policy.ts",
			"runtime/paths.ts",
		]) {
			expect(closure).toContain(join(SRC, expected));
		}
	});

	it("contains no module-scope read of a contract name", () => {
		const offenders = closure.flatMap((module) =>
			moduleScopeContractReads(readFileSync(module, "utf-8"), internalNames).map(
				(name) => `${relative(SRC, module)}: ${name}`,
			),
		);

		// Any hit here is un-aliased at read time: the boundary in index.ts has not run yet.
		// Move the read inside a function, or behind the dynamic import the command already uses.
		expect(offenders).toEqual([]);
	});

	// serve/log.ts used to be the one legitimate module-scope read, legitimate only because it sat
	// outside this closure. Upstream sync step 3 made `runtime/run-config.ts` import it, so it is
	// reachable now and its debug gate reads the register at call time instead. Pin both halves,
	// so the read cannot drift back to module scope unnoticed.
	it("reaches serve/log.ts, whose debug gate reads its register only at call time", () => {
		const log = join(SRC, "serve", "log.ts");
		expect(closure).toContain(log);
		expect(moduleScopeContractReads(readFileSync(log, "utf-8"), internalNames)).toEqual([]);
	});
});

describe("the module-scope detector", () => {
	const contract = new Set(["CLAWDI_DEBUG", "CLAWDI_RUNTIME_HOME", "CLAWDI_RUNTIME_MODE"]);

	// A detector that cannot return a hit would make the assertion above vacuous, which is the
	// same complaint WR-05 makes about an unbounded allowlist. These pin both directions.
	it("flags a contract read in a module-scope declaration", () => {
		expect(
			moduleScopeContractReads('const DEBUG = process.env.CLAWDI_DEBUG === "1";', contract),
		).toEqual(["CLAWDI_DEBUG"]);
		expect(
			moduleScopeContractReads("export const H = process.env.CLAWDI_RUNTIME_HOME;", contract),
		).toEqual(["CLAWDI_RUNTIME_HOME"]);
		expect(
			moduleScopeContractReads('let M = process.env["CLAWDI_RUNTIME_MODE"];', contract),
		).toEqual(["CLAWDI_RUNTIME_MODE"]);
	});

	it("flags a read split across a multi-line initializer", () => {
		const source = ["const home =", "\tprocess.env.CLAWDI_RUNTIME_HOME ??", '\t"/fallback";'].join(
			"\n",
		);
		expect(moduleScopeContractReads(source, contract)).toEqual(["CLAWDI_RUNTIME_HOME"]);
	});

	// WR-09. Every shape below is a module-scope read that is NOT a declaration, and every one
	// of them was invisible to the detector while its comment said the invariant was enforced.
	// They run before the boundary exactly as a hoisted `const` does.
	it("flags a bare top-level statement that is not a declaration", () => {
		expect(
			moduleScopeContractReads("if (process.env.CLAWDI_DEBUG) enableTrace();", contract),
		).toEqual(["CLAWDI_DEBUG"]);
		expect(
			moduleScopeContractReads("configure(process.env.CLAWDI_RUNTIME_MODE);", contract),
		).toEqual(["CLAWDI_RUNTIME_MODE"]);
		expect(
			moduleScopeContractReads('globalThis.mode = process.env["CLAWDI_RUNTIME_MODE"];', contract),
		).toEqual(["CLAWDI_RUNTIME_MODE"]);
	});

	it("flags a read inside the body of a top-level block, which runs at import time", () => {
		const source = [
			"if (process.env.CLAWDI_DEBUG) {",
			"\tconfigure(process.env.CLAWDI_RUNTIME_HOME);",
			"}",
		].join("\n");
		expect(moduleScopeContractReads(source, contract)).toEqual([
			"CLAWDI_DEBUG",
			"CLAWDI_RUNTIME_HOME",
		]);
	});

	it("ignores a read inside a callback of a top-level statement, which runs when called", () => {
		// The shape `index.ts` is built out of: one top-level statement per command, whose action
		// body runs on invocation, long after the boundary. Found by execution when the scan was
		// widened -- index.ts:1332 sets CLAWDI_API_URL inside the `mcp` command's action.
		const source = [
			"program",
			'\t.command("mcp")',
			"\t.action(async (opts: { apiUrl?: string }) => {",
			"\t\tif (opts.apiUrl) process.env.CLAWDI_RUNTIME_MODE = opts.apiUrl;",
			"\t});",
		].join("\n");
		expect(moduleScopeContractReads(source, contract)).toEqual([]);
	});

	it("still flags a read that sits before a callback in the same statement", () => {
		// The cut is at the first `=>`, not at the whole statement, so a read in the arguments
		// evaluated at import time is not hidden by a callback written after it.
		const source = [
			"register(process.env.CLAWDI_RUNTIME_HOME, () => {",
			"\tconfigure(process.env.CLAWDI_DEBUG);",
			"});",
		].join("\n");
		expect(moduleScopeContractReads(source, contract)).toEqual(["CLAWDI_RUNTIME_HOME"]);
	});

	it("ignores a contract name that only appears in a column-0 comment", () => {
		// Otherwise the detector reports the documentation of the hazard as the hazard, and the
		// first person to explain the rule in a comment turns the suite red.
		const source = [
			"// Never read process.env.CLAWDI_RUNTIME_HOME at module scope.",
			"/* process.env.CLAWDI_DEBUG is read inside serve/log.ts, after the boundary. */",
			" * process.env.CLAWDI_RUNTIME_MODE",
		].join("\n");
		expect(moduleScopeContractReads(source, contract)).toEqual([]);
	});

	it("ignores a read inside a function, which runs after the boundary", () => {
		const source = [
			"export function home(): string | undefined {",
			"\treturn process.env.CLAWDI_RUNTIME_HOME;",
			"}",
		].join("\n");
		expect(moduleScopeContractReads(source, contract)).toEqual([]);
	});

	it("ignores a public name, which is what the boundary consumes", () => {
		expect(moduleScopeContractReads("const url = process.env.IFORGE_API_URL;", contract)).toEqual(
			[],
		);
	});

	it("ignores a CLAWDI_ name outside the contract, which nothing can alias into", () => {
		// `lib/config.ts` really does read this at module scope. It is not a violation: the
		// contract has no `IFORGE_DEFAULT_API_URL`, so the boundary can never write this register.
		expect(
			moduleScopeContractReads(
				'const DEFAULT = process.env.CLAWDI_DEFAULT_API_URL || "http://localhost:8000";',
				contract,
			),
		).toEqual([]);
	});
});
