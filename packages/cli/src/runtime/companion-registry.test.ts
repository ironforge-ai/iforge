import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
	COMPANION_KEY_PATTERN,
	COMPANION_REGISTRY,
	COMPANION_VERBS,
	type CompanionRegistryEntry,
	type CompanionVerbEntry,
	deriveAnnotations,
	FILE_BROWSER_COMPANION_KIND,
	KNOWN_COMPANION_KINDS,
	RESERVED_COMPANION_UID_BLOCK,
	recognisedCompanionKinds,
	reconcilableCompanionKinds,
} from "./companion-registry";
import { resolveCompanionServiceUid } from "./file-browser-isolation";

// Resolved from `import.meta.dir`, never from the process working directory, so this behaves
// identically under `bun test --cwd packages/cli` and a whole-suite run from the repository root.
const REPO_ROOT = join(import.meta.dir, "..", "..", "..", "..");
const CONTRACT_PATH = join(REPO_ROOT, "config", "companion-registry.txt");
const CONTRACT_COLUMNS = 6;
const VERB_CONTRACT_COLUMNS = 9;
/** The first column of a verb row, and therefore a key no companion kind may ever take. */
const VERB_ROW_MARKER = "verb";
const TOOL_CLASSES = new Set(["read", "create", "mutate", "destroy"]);
const CAP_UNITS = new Set(["calls", "records", "bytes"]);
/** Classes whose rows are malformed without a read probe (D-13). */
const PROBE_REQUIRED_CLASSES = new Set(["mutate", "destroy"]);
/** Where a verb's arguments travel (05-13, WR-06b). Closed, with no default. */
const ARGS_IN_PLACEMENTS = new Set(["query", "body"]);
/**
 * How a kind's digest pin is located (06-03). Exactly two values: `binary-sha256` for a native
 * binary and `oci-digest` for a container image located by its OCI content digest -- the Quadlet
 * seam Phase 4 D-02 left open. Written as a literal here rather than imported, so a widened embed
 * cannot make its own parser agree with it.
 */
const ARTIFACT_KINDS: ReadonlySet<string> = new Set(["binary-sha256", "oci-digest"]);

function isArtifactKind(value: string): value is CompanionRegistryEntry["artifactKind"] {
	return ARTIFACT_KINDS.has(value);
}

const KIND_ROW = "filebrowser|binary-sha256|0.0.0|preallocated|9120|bypass";
const VERB_ROW = "verb|filebrowser|list_dir|read|GET|/api/resources|-|calls|query";
const MINIMAL_CONTRACT = `${KIND_ROW}\n${VERB_ROW}\n`;
// Well-formed rows used to build the 05-02 negative cases, written as literals rather than
// rendered from `COMPANION_VERBS` so a wrong value in the embed cannot make a negative case pass.
const CREATE_DIR_ROW =
	"verb|filebrowser|create_dir|create|POST|/api/resources|list_dir|calls|query";
const WRITE_FILE_ROW =
	"verb|filebrowser|write_file|create|POST|/api/resources|list_dir|bytes|query";

interface CompanionContract {
	kinds: Record<string, CompanionRegistryEntry>;
	verbs: Record<string, CompanionVerbEntry>;
}

/** The Hermes dashboard's port. It is not a companion, so it owns no row -- but nothing may collide with it. */
const HERMES_DASHBOARD_PORT = 9119;

/**
 * Parse the contract file.
 *
 * Every failure THROWS, naming the path or the offending line. Returning an empty record or
 * skipping a malformed row would let the parity assertion below pass because it compared nothing
 * to nothing -- a green test that reads as evidence while proving nothing, which is strictly
 * worse than having no test (the rationale `apps/web/src/lib/ui-spec-contract.ts` records).
 */
function parseContractFile(source = readFileSync(CONTRACT_PATH, "utf-8")): CompanionContract {
	const kinds: Record<string, CompanionRegistryEntry> = {};
	const verbs: Record<string, CompanionVerbEntry> = {};
	const verbRows: string[][] = [];
	for (const raw of source.split("\n")) {
		const line = raw.trim();
		if (!line || line.startsWith("#")) continue;
		const columns = line.split("|");
		if (columns[0] === VERB_ROW_MARKER) {
			// The marker with a KIND row's column count is the reserved-key mistake, not a
			// miscounted verb row, and saying so is the difference between a one-line fix and
			// an afternoon.
			if (columns.length === CONTRACT_COLUMNS) {
				throw new Error(
					`contract key "${VERB_ROW_MARKER}" in ${CONTRACT_PATH} is reserved as the verb-row marker and cannot be a companion kind: ${line}`,
				);
			}
			if (columns.length !== VERB_CONTRACT_COLUMNS) {
				throw new Error(
					`malformed verb line in ${CONTRACT_PATH}: expected ${VERB_CONTRACT_COLUMNS} columns, got ${columns.length}: ${line}`,
				);
			}
			verbRows.push(columns);
			continue;
		}
		if (columns.length !== CONTRACT_COLUMNS) {
			throw new Error(
				`malformed contract line in ${CONTRACT_PATH}: expected ${CONTRACT_COLUMNS} columns, got ${columns.length}: ${line}`,
			);
		}
		const [key, artifactKind, minCliVersion, uid, port, egress] = columns as [
			string,
			string,
			string,
			string,
			string,
			string,
		];
		if (!isArtifactKind(artifactKind)) {
			throw new Error(
				`unknown artifactKind for ${key} in ${CONTRACT_PATH}: ${JSON.stringify(artifactKind)}: ${line}`,
			);
		}
		if (egress !== "redirect" && egress !== "bypass") {
			throw new Error(`unknown egress for ${key} in ${CONTRACT_PATH}: ${egress}`);
		}
		if (!/^\d+$/.test(port)) {
			throw new Error(`port for ${key} in ${CONTRACT_PATH} is not a decimal: ${port}`);
		}
		if (uid !== "preallocated" && !/^\d+$/.test(uid)) {
			throw new Error(
				`uid for ${key} in ${CONTRACT_PATH} must be "preallocated" or a decimal: ${uid}`,
			);
		}
		kinds[key] = {
			artifactKind,
			minCliVersion,
			uid: uid === "preallocated" ? "preallocated" : Number(uid),
			port: Number(port),
			egress,
		};
	}
	// Verb rows are resolved in a SECOND pass so a row may reference a kind declared anywhere in
	// the file, and so a read probe can be checked against the whole verb table rather than
	// against whatever happened to be parsed already.
	for (const columns of verbRows) {
		const [, kind, verb, toolClass, method, pathTemplate, readProbe, capUnit, argsIn] = columns as [
			string,
			string,
			string,
			string,
			string,
			string,
			string,
			string,
			string,
		];
		const name = `${kind}__${verb}`;
		if (!Object.hasOwn(kinds, kind)) {
			throw new Error(`unknown companion kind for ${name} in ${CONTRACT_PATH}: ${kind}`);
		}
		if (!TOOL_CLASSES.has(toolClass)) {
			throw new Error(`unknown tool class for ${name} in ${CONTRACT_PATH}: ${toolClass}`);
		}
		if (!CAP_UNITS.has(capUnit)) {
			throw new Error(`unknown capUnit for ${name} in ${CONTRACT_PATH}: ${capUnit}`);
		}
		if (!ARGS_IN_PLACEMENTS.has(argsIn)) {
			// Names the offending value AND the closed vocabulary. There is no default: an absent
			// placement would reintroduce the invisible method-derived rule this column removes.
			throw new Error(
				`unknown argsIn for ${name} in ${CONTRACT_PATH}: "${argsIn}": must be one of ${[
					...ARGS_IN_PLACEMENTS,
				]
					.sort()
					.map((value) => `"${value}"`)
					.join(", ")}`,
			);
		}
		if (readProbe === "-" && PROBE_REQUIRED_CLASSES.has(toolClass)) {
			throw new Error(
				`${name} in ${CONTRACT_PATH} is class ${toolClass} and must declare a read probe (D-13)`,
			);
		}
		if (Object.hasOwn(verbs, name)) {
			throw new Error(`duplicate verb row in ${CONTRACT_PATH}: ${name}`);
		}
		verbs[name] = {
			toolClass: toolClass as CompanionVerbEntry["toolClass"],
			method: method as CompanionVerbEntry["method"],
			pathTemplate,
			readProbe: readProbe === "-" ? null : readProbe,
			capUnit: capUnit as CompanionVerbEntry["capUnit"],
			argsIn: argsIn as CompanionVerbEntry["argsIn"],
		};
	}
	assertVerbRowsSorted(verbRows);
	resolveReadProbes(verbs);
	if (Object.keys(kinds).length === 0 && Object.keys(verbs).length === 0) {
		throw new Error(`${CONTRACT_PATH} has no data rows; the registry contract cannot be empty`);
	}
	return { kinds, verbs };
}

/**
 * Enforce ascending `(kind, verb)` order over the verb rows, naming the FIRST offending pair.
 *
 * Set equality alone leaves the exposed order unspecified: two rows that compare equal on kind
 * would have no defined position, and a client that renders the catalog positionally could watch
 * it reshuffle. The file's order is therefore part of the contract, and this is where it is held.
 */
function assertVerbRowsSorted(verbRows: string[][]): void {
	const names = verbRows.map((columns) => `${columns[1]}__${columns[2]}`);
	for (let index = 1; index < names.length; index += 1) {
		const previous = names[index - 1] as string;
		const current = names[index] as string;
		if (current < previous) {
			throw new Error(
				`verb rows in ${CONTRACT_PATH} must be sorted by kind then verb: ${current} follows ${previous}`,
			);
		}
	}
}

/**
 * Resolve every declared read probe against the FULL verb table, after all rows are parsed.
 *
 * A probe must name a verb of the same kind AND that verb must be `read` class. A probe that
 * itself writes is not a preview path -- it is a second mutation (T-5-13), and D-13's requirement
 * would otherwise be satisfiable by naming another `create` verb.
 */
function resolveReadProbes(verbs: Record<string, CompanionVerbEntry>): void {
	for (const [name, entry] of Object.entries(verbs)) {
		if (entry.readProbe === null) continue;
		const kind = name.split("__")[0] as string;
		const probeName = `${kind}__${entry.readProbe}`;
		if (!Object.hasOwn(verbs, probeName)) {
			throw new Error(
				`${name} in ${CONTRACT_PATH} names a read probe that is not a verb of ${kind}: ${entry.readProbe}`,
			);
		}
		const probe = verbs[probeName] as CompanionVerbEntry;
		if (probe.toolClass !== "read") {
			throw new Error(
				`${name} in ${CONTRACT_PATH} names a read probe that is not a read-class verb: ${entry.readProbe} is class ${probe.toolClass}`,
			);
		}
	}
}

function contractKeysInFileOrder(source = readFileSync(CONTRACT_PATH, "utf-8")): string[] {
	return source
		.split("\n")
		.map((raw) => raw.trim())
		.filter((line) => line && !line.startsWith("#") && !line.startsWith(`${VERB_ROW_MARKER}|`))
		.map((line) => line.split("|")[0] as string);
}

function contractVerbNamesInFileOrder(source = readFileSync(CONTRACT_PATH, "utf-8")): string[] {
	return source
		.split("\n")
		.map((raw) => raw.trim())
		.filter((line) => line.startsWith(`${VERB_ROW_MARKER}|`))
		.map((line) => {
			const columns = line.split("|");
			return `${columns[1]}__${columns[2]}`;
		});
}

describe("the companion registry contract file", () => {
	it("throws on a line that does not have exactly six columns, rather than skipping it", () => {
		expect(() => parseContractFile("filebrowser|binary-sha256|0.0.0|preallocated|9120")).toThrow(
			/expected 6 columns, got 5/,
		);
		expect(() =>
			parseContractFile("filebrowser|binary-sha256|0.0.0|preallocated|9120|bypass|extra"),
		).toThrow(/expected 6 columns, got 7/);
	});

	it("throws on a contract with zero data rows rather than parsing to an empty registry", () => {
		// Without this, deleting every row would turn the parity assertion below into
		// `expect({}).toEqual({})` -- green, and proving nothing.
		expect(() => parseContractFile("# only a header\n\n")).toThrow(/no data rows/);
	});

	it("throws on a column whose value is outside its declared vocabulary", () => {
		// `oci-tag` is the floating reference D-01 forbids, so it is the value that must never parse.
		expect(() => parseContractFile("filebrowser|oci-tag|0.0.0|preallocated|9120|bypass")).toThrow(
			/unknown artifactKind/,
		);
		expect(() =>
			parseContractFile("filebrowser|binary-sha256|0.0.0|preallocated|9120|drop"),
		).toThrow(/unknown egress/);
		expect(() =>
			parseContractFile("filebrowser|binary-sha256|0.0.0|preallocated|http|bypass"),
		).toThrow(/is not a decimal/);
	});

	it("lists its rows sorted by key", () => {
		const keys = contractKeysInFileOrder();
		expect(keys).toEqual([...keys].sort());
	});
});

describe("COMPANION_REGISTRY", () => {
	it("equals config/companion-registry.txt exactly, column for column", () => {
		expect(COMPANION_REGISTRY).toEqual(parseContractFile().kinds);
	});

	it("has KNOWN_COMPANION_KINDS as its sorted key set", () => {
		// Widened to `readonly string[]` deliberately: KNOWN_COMPANION_KINDS is a literal tuple so
		// callers can narrow against it, and comparing it to `string[]` without widening is a
		// TS2769 rather than a test failure.
		const known: readonly string[] = KNOWN_COMPANION_KINDS;
		const keys = Object.keys(COMPANION_REGISTRY).sort();
		expect([...known]).toEqual(keys);
		expect(known.length).toBeGreaterThan(0);
	});

	it("bounds every key so it is safe as a unit name, a UID lookup name and a path component", () => {
		for (const key of Object.keys(COMPANION_REGISTRY)) {
			expect(COMPANION_KEY_PATTERN.test(key)).toBe(true);
		}
	});

	it("gives every kind a unique port that does not collide with the Hermes dashboard", () => {
		const ports = Object.values(COMPANION_REGISTRY).map((entry) => entry.port);
		expect(new Set(ports).size).toBe(ports.length);
		expect(ports).not.toContain(HERMES_DASHBOARD_PORT);
	});

	it("answers to its own keys only, never to an inherited Object.prototype member", () => {
		// `COMPANION_KEY_PATTERN` accepts exactly one `Object.prototype` member -- `constructor` --
		// and the manifest parser accepts a companion key on that same pattern (its catchall
		// preserves the key). On a plain object literal `COMPANION_REGISTRY["constructor"]` therefore
		// resolved the inherited `Object` function instead of `undefined`, so every consumer that
		// reads a row and tests for `undefined` accepted a kind no row owns.
		//
		// Asserted through a bare index read deliberately: `Object.hasOwn` would pass on a registry
		// that still carried the chain, which is the assertion that would NOT have caught this.
		const registry: Record<string, unknown> = COMPANION_REGISTRY;
		expect(COMPANION_KEY_PATTERN.test("constructor")).toBe(true);
		expect(registry.constructor).toBeUndefined();
		expect(Object.getPrototypeOf(COMPANION_REGISTRY)).toBeNull();
		// The complement: stripping the prototype must not have cost a real row.
		for (const key of KNOWN_COMPANION_KINDS) {
			expect(registry[key]).toBeDefined();
		}
	});

	it("gives every kind a uid that is either preallocated or inside the reserved block", () => {
		for (const [key, entry] of Object.entries(COMPANION_REGISTRY)) {
			if (entry.uid === "preallocated") continue;
			expect(entry.uid, `${key} uid`).toBeGreaterThanOrEqual(RESERVED_COMPANION_UID_BLOCK.first);
			expect(entry.uid, `${key} uid`).toBeLessThanOrEqual(RESERVED_COMPANION_UID_BLOCK.last);
		}
	});
});

// 06-03 (D-01). `postiz` is the first container kind, the first GATED kind and the first numeric
// allocation from the reserved UID block. Each of those is one-way under D-13, so the row is pinned
// value for value here, not only by the parity assertion above.
const POSTIZ_ROW = "postiz|oci-digest|0.14.0|10101|9130|bypass";

describe("the postiz kind", () => {
	it("parses on its own through the widened artifactKind vocabulary", () => {
		expect(parseContractFile(POSTIZ_ROW).kinds).toEqual({
			postiz: {
				artifactKind: "oci-digest",
				minCliVersion: "0.14.0",
				uid: 10101,
				port: 9130,
				egress: "bypass",
			},
		});
	});

	it("refuses an artifactKind outside {binary-sha256, oci-digest}, naming the value and line", () => {
		const bad = "postiz|oci-tag|0.14.0|10101|9130|bypass";
		expect(() => parseContractFile(bad)).toThrow(/unknown artifactKind/);
		expect(() => parseContractFile(bad)).toThrow(/"oci-tag"/);
		expect(() => parseContractFile(bad)).toThrow(bad);
	});

	it("is shipped in COMPANION_REGISTRY exactly as the D-01 row", () => {
		expect(COMPANION_REGISTRY.postiz).toEqual({
			artifactKind: "oci-digest",
			minCliVersion: "0.14.0",
			uid: 10101,
			port: 9130,
			egress: "bypass",
		});
		expect(parseContractFile().kinds.postiz).toEqual(COMPANION_REGISTRY.postiz);
	});

	it("makes KNOWN_COMPANION_KINDS exactly filebrowser and postiz, sorted", () => {
		const known: readonly string[] = KNOWN_COMPANION_KINDS;
		expect([...known]).toEqual(["filebrowser", "postiz"]);
	});

	it("is reconcilable since its container kind shipped with its reconciler (06-08, D-13)", () => {
		// 06-03 landed postiz recognised but withheld (`postiz: false`) so no converge would
		// `useradd` an account nothing started. 06-08 ships the container kind and flips the value in
		// the same change, so allocating uid 10101 now provisions an account a unit actually runs as.
		expect(recognisedCompanionKinds(["filebrowser", "postiz"])).toEqual(["filebrowser", "postiz"]);
		expect(reconcilableCompanionKinds(["filebrowser", "postiz"])).toEqual([
			"filebrowser",
			"postiz",
		]);
		expect(reconcilableCompanionKinds(["postiz"])).toEqual(["postiz"]);
		// The explicit-false withholding the gate still honours, now proven on an injected table.
		expect(reconcilableCompanionKinds(["postiz"], { postiz: false })).toEqual([]);
		// An explicit `false` is withheld exactly as an absent key is: the gate admits `true` only,
		// so a present-but-false entry cannot be mistaken for membership.
		expect(reconcilableCompanionKinds(["filebrowser"], { filebrowser: false })).toEqual([]);
	});

	it("resolves to uid 10101, the first numeric allocation from the reserved block", () => {
		expect(COMPANION_REGISTRY.postiz?.uid).toBe(10101);
		expect(resolveCompanionServiceUid("postiz")).toBe(10101);
		expect(10101).toBeGreaterThanOrEqual(RESERVED_COMPANION_UID_BLOCK.first);
		expect(10101).toBeLessThanOrEqual(RESERVED_COMPANION_UID_BLOCK.last);
	});

	it("still refuses a duplicate or an out-of-block uid with the postiz row present", () => {
		const postiz = COMPANION_REGISTRY.postiz;
		expect(postiz).toBeDefined();
		if (postiz === undefined) return;
		// A second kind claiming 10101 is refused by name: companion UIDs are never reclaimed.
		expect(() =>
			resolveCompanionServiceUid("postiz", { postiz, twin: { ...postiz, port: 9131 } }),
		).toThrow(/already claimed by companion twin/);
		// A uid outside 10100-10199 is refused, whatever else the row says.
		expect(() =>
			resolveCompanionServiceUid("postiz", { postiz: { ...postiz, uid: 10200 } }),
		).toThrow(/outside the reserved companion UID block/);
	});
});

// 04-08 Task 1. `FILE_BROWSER_COMPANION_KIND` was a module-local literal in
// `runtime-systemd-reconciliation.ts`, which the unit writer read as an unchecked string. Promoting
// it to a registry-typed export makes a registry that stopped carrying the key a `tsc --noEmit`
// failure; these two assertions are the RUNTIME half of the same claim.
describe("FILE_BROWSER_COMPANION_KIND", () => {
	it("is a member of the registry's own key set, not a literal beside it", () => {
		const known: readonly string[] = KNOWN_COMPANION_KINDS;
		expect(known).toContain(FILE_BROWSER_COMPANION_KIND);
	});

	it("has a row in COMPANION_REGISTRY", () => {
		expect(COMPANION_REGISTRY[FILE_BROWSER_COMPANION_KIND]).toBeDefined();
	});
});

describe("recognisedCompanionKinds", () => {
	it("intersects declared keys with the registry, deduplicated and sorted", () => {
		expect(recognisedCompanionKinds([])).toEqual([]);
		expect(recognisedCompanionKinds(Object.keys(COMPANION_REGISTRY))).toEqual([
			...KNOWN_COMPANION_KINDS,
		]);
		expect(recognisedCompanionKinds(["zzz-unknown", "filebrowser", "filebrowser"])).toEqual([
			"filebrowser",
		]);
	});

	it("excludes a prototype-inherited key, which the manifest parser accepts", () => {
		// The filter is prototype-safe because it goes through `Set.prototype.has`. That is a
		// property worth PINNING rather than leaving to the implementation's choice of container: a
		// future rewrite to a plain-object membership map would re-admit `constructor`, and this
		// filter is the last thing between a parser-approved manifest key and a privileged `useradd`.
		expect(COMPANION_KEY_PATTERN.test("constructor")).toBe(true);
		expect(recognisedCompanionKinds(["constructor"])).toEqual([]);
		expect(recognisedCompanionKinds(["constructor", "filebrowser"])).toEqual(["filebrowser"]);
	});

	it("is not the allocation gate — reconcilableCompanionKinds is", () => {
		// `recognisedCompanionKinds` answers "does a registry row exist", which is strictly weaker
		// than "can this build run this companion". The reconciler surface is filebrowser-only, so
		// gating privileged allocation on registry membership meant the commit adding the SECOND
		// registry row — before its unit writer, which is how this work is staged — would
		// `groupadd`/`useradd` an account nothing ever starts, permanently burning a UID under D-13.
		const declared = ["filebrowser", "zzz-unknown"];
		// A registry row with NO reconciler is INERT: recognised, and deliberately not allocated.
		expect(recognisedCompanionKinds(declared)).toEqual(["filebrowser"]);
		expect(reconcilableCompanionKinds(declared, {})).toEqual([]);
		// And with a reconciler it is allocated, so the gate is a gate and not a blanket refusal.
		expect(reconcilableCompanionKinds(declared, { filebrowser: true })).toEqual(["filebrowser"]);
	});

	it("the shipped reconciler table converges exactly the kinds this build has writers for", () => {
		// From 06-03 to 06-08 this was where "a registry row is not a reconciler" was visible in
		// production (`postiz` had a row and no reconciler). 06-08 shipped the container kind and
		// flipped `postiz` to `true`, so every shipped kind now has a writer.
		expect(reconcilableCompanionKinds(KNOWN_COMPANION_KINDS)).toEqual([
			FILE_BROWSER_COMPANION_KIND,
			"postiz",
		]);
		// Never wider than the registry: the gate narrows, it cannot admit.
		const declared = [...KNOWN_COMPANION_KINDS, "zzz-unknown", "constructor"];
		const recognised = new Set<string>(recognisedCompanionKinds(declared));
		for (const kind of reconcilableCompanionKinds(declared)) {
			expect(recognised.has(kind)).toBe(true);
		}
	});

	it("excludes a prototype-inherited key from the allocation gate too", () => {
		// `Object.hasOwn` on the reconciler table, not a bare index read: `constructor` is the one
		// `Object.prototype` member COMPANION_KEY_PATTERN accepts, and this gate stands in front of
		// `groupadd`/`useradd`.
		expect(reconcilableCompanionKinds(["constructor"])).toEqual([]);
		// `as const` because TypeScript widens a literal on the `constructor` property to `boolean`.
		// The point of the case is the KEY, not the value.
		expect(reconcilableCompanionKinds(["constructor"], { constructor: true } as const)).toEqual([]);
	});

	it("is unaffected by the verb table — kinds and verbs are separate row types", () => {
		// A verb row must never widen the recognised KIND set. The allocation gate stands in
		// front of `groupadd`/`useradd`; a tool-surface row has no business reaching it.
		expect(recognisedCompanionKinds(["list_dir", "filebrowser__list_dir"])).toEqual([]);
		expect(reconcilableCompanionKinds(["verb"])).toEqual([]);
	});

	it("takes keys only, so it is structurally unable to read an entry value", () => {
		// The `filebrowser` registry entry's manifest counterpart embeds a live HS256 secret at
		// `auth.secret` (T-4-07). An `Iterable<string>` parameter is what makes leaking one from here
		// impossible rather than merely discouraged — the same discipline
		// `collectUnsupportedCompanions` already uses.
		const source = readFileSync(join(import.meta.dir, "companion-registry.ts"), "utf-8");
		const declaration = source.indexOf("export function recognisedCompanionKinds(");
		// Asserted BEFORE the slice: a guard that slices from -1 asserts nothing.
		expect(declaration).toBeGreaterThan(0);
		expect(source.slice(declaration, declaration + 200)).toContain("Iterable<string>");
	});
});

// ---------------------------------------------------------------------------
// The verb table (phase 05, D-04/D-05/D-07/D-13).
//
// The contract file now carries TWO row types discriminated by the first column. Every malformed
// shape THROWS here, for the same reason the kind rows do: a reader that skips a bad row lets the
// parity assertion below pass by comparing nothing to nothing.
// ---------------------------------------------------------------------------

describe("the companion registry contract file's verb rows", () => {
	it("parses a verb row into the verb table without touching the kind table", () => {
		const contract = parseContractFile(MINIMAL_CONTRACT);

		expect(contract.kinds).toEqual(parseContractFile(KIND_ROW).kinds);
		expect(Object.keys(contract.verbs)).toEqual(["filebrowser__list_dir"]);
		expect(contract.verbs.filebrowser__list_dir).toEqual({
			toolClass: "read",
			method: "GET",
			pathTemplate: "/api/resources",
			readProbe: null,
			capUnit: "calls",
			argsIn: "query",
		});
	});

	it("throws on a verb line that does not have exactly nine columns", () => {
		expect(() =>
			parseContractFile(`${KIND_ROW}\nverb|filebrowser|list_dir|read|GET|/api/resources|-|calls`),
		).toThrow(/expected 9 columns, got 8/);
		expect(() => parseContractFile(`${KIND_ROW}\n${VERB_ROW}|extra`)).toThrow(
			/expected 9 columns, got 10/,
		);
	});

	it("throws on a duplicate verb row rather than letting the last one win", () => {
		expect(() => parseContractFile(`${KIND_ROW}\n${VERB_ROW}\n${VERB_ROW}`)).toThrow(
			/duplicate verb row/,
		);
	});

	it("throws on a kind row whose key is the reserved verb marker", () => {
		expect(() =>
			parseContractFile(`${KIND_ROW}\nverb|binary-sha256|0.0.0|preallocated|9121|bypass`),
		).toThrow(/reserved/);
	});

	it("throws on a verb row naming a kind that owns no row", () => {
		expect(() => parseContractFile(VERB_ROW)).toThrow(/unknown companion kind/);
	});

	it("throws on a class or capUnit outside its declared vocabulary", () => {
		expect(() =>
			parseContractFile(
				`${KIND_ROW}\nverb|filebrowser|list_dir|browse|GET|/api/resources|-|calls|query`,
			),
		).toThrow(/unknown tool class/);
		expect(() =>
			parseContractFile(
				`${KIND_ROW}\nverb|filebrowser|list_dir|read|GET|/api/resources|-|joules|query`,
			),
		).toThrow(/unknown capUnit/);
	});

	it("throws on a mutating row declared without a read probe (D-13)", () => {
		expect(() =>
			parseContractFile(
				`${KIND_ROW}\nverb|filebrowser|move_path|mutate|PATCH|/api/resources|-|records|query`,
			),
		).toThrow(/must declare a read probe/);
		expect(() =>
			parseContractFile(
				`${KIND_ROW}\nverb|filebrowser|delete_path|destroy|DELETE|/api/resources|-|records|query`,
			),
		).toThrow(/must declare a read probe/);
	});

	it("throws on a read probe that names no verb of the same kind", () => {
		expect(() =>
			parseContractFile(
				`${KIND_ROW}\n${VERB_ROW}\nverb|filebrowser|move_path|mutate|PATCH|/api/resources|nosuchverb|records|query`,
			),
		).toThrow(/read probe/);
	});

	it("throws on a read probe whose own class is not read", () => {
		// A probe that itself writes is a second mutation, not a preview (T-5-13). Without this
		// rule, D-13 could be satisfied by naming another `create` verb.
		expect(() =>
			parseContractFile(
				`${KIND_ROW}\n${VERB_ROW}\nverb|filebrowser|move_path|mutate|PATCH|/api/resources|write_file|records|query\n${WRITE_FILE_ROW}`,
			),
		).toThrow(/read probe that is not a read-class verb: write_file is class create/);
	});

	it("throws on verb rows out of ascending kind-then-verb order, naming the first pair", () => {
		expect(() => parseContractFile(`${KIND_ROW}\n${VERB_ROW}\n${CREATE_DIR_ROW}`)).toThrow(
			/sorted by kind then verb: filebrowser__create_dir follows filebrowser__list_dir/,
		);
	});

	it("lists its verb rows sorted by kind then verb", () => {
		const names = contractVerbNamesInFileOrder();
		expect(names.length).toBeGreaterThan(0);
		expect(names).toEqual([...names].sort());
	});
});

describe("COMPANION_VERBS", () => {
	it("equals the verb rows of config/companion-registry.txt exactly, column for column", () => {
		expect(COMPANION_VERBS).toEqual(parseContractFile().verbs);
	});

	it("has the contract file's verb names as its key set, in BOTH directions", () => {
		// A one-directional check cannot detect a verb that exists in one representation and not
		// the other, which is the drift that matters. Mutation-checked by deleting
		// `filebrowser__write_file` from the embed and the `delete_path` row from the contract
		// file in turn; each deletion reds exactly one of the two directions below.
		const embedded = new Set(Object.keys(COMPANION_VERBS));
		const contract = new Set(contractVerbNamesInFileOrder());
		expect(contract.size).toBeGreaterThan(0);

		const missingFromContract = [...embedded].filter((name) => !contract.has(name));
		const missingFromEmbed = [...contract].filter((name) => !embedded.has(name));
		expect(missingFromContract).toEqual([]);
		expect(missingFromEmbed).toEqual([]);
	});

	it("carries exactly the twelve curated verbs the two COVERAGE matrices integrate", () => {
		// Literals, not a set derived from the embed: derived, this would assert `x === x`. Six from
		// the Phase 5 matrix (`filebrowser`), six from the Phase 6 matrix (`postiz`, 06-06).
		expect(Object.keys(COMPANION_VERBS).sort()).toEqual([
			"filebrowser__create_dir",
			"filebrowser__delete_path",
			"filebrowser__list_dir",
			"filebrowser__move_path",
			"filebrowser__read_file",
			"filebrowser__write_file",
			"postiz__create_post",
			"postiz__is_connected",
			"postiz__list_groups",
			"postiz__list_integrations",
			"postiz__list_notifications",
			"postiz__list_posts",
		]);
	});

	it("gives every mutating verb a read-class probe of its own kind", () => {
		for (const [name, entry] of Object.entries(COMPANION_VERBS)) {
			if (entry.toolClass === "mutate" || entry.toolClass === "destroy") {
				expect(entry.readProbe, `${name} read probe`).not.toBeNull();
			}
			if (entry.readProbe === null) continue;
			const kind = name.split("__")[0] as string;
			const probe = COMPANION_VERBS[`${kind}__${entry.readProbe}`] as CompanionVerbEntry;
			expect(probe, `${name} probe row`).toBeDefined();
			expect(probe.toolClass, `${name} previews with`).toBe("read");
		}
	});

	it("names every verb `kind__verb` over a kind that owns a registry row", () => {
		const names = Object.keys(COMPANION_VERBS);
		expect(names.length).toBeGreaterThan(0);
		for (const name of names) {
			const [kind, verb, ...rest] = name.split("__");
			expect(rest).toEqual([]);
			expect(verb).toBeTruthy();
			expect(Object.hasOwn(COMPANION_REGISTRY, kind as string)).toBe(true);
		}
	});

	it("answers to its own keys only, never to an inherited Object.prototype member", () => {
		// Same hazard as the kind registry: a bare index read of `constructor` on a plain object
		// literal resolves the inherited `Object` function instead of `undefined`, and every
		// consumer that tests a row against `undefined` then accepts a verb no row owns.
		const verbs: Record<string, unknown> = COMPANION_VERBS;
		expect(verbs.constructor).toBeUndefined();
		expect(Object.getPrototypeOf(COMPANION_VERBS)).toBeNull();
		for (const name of Object.keys(COMPANION_VERBS)) {
			expect(verbs[name]).toBeDefined();
		}
	});
});

describe("deriveAnnotations", () => {
	it("derives the MCP annotation triple mechanically from the class (D-07)", () => {
		expect(deriveAnnotations("read")).toEqual({
			readOnlyHint: true,
			destructiveHint: false,
			idempotentHint: true,
		});
		expect(deriveAnnotations("create")).toEqual({
			readOnlyHint: false,
			destructiveHint: false,
			idempotentHint: false,
		});
		expect(deriveAnnotations("mutate")).toEqual({
			readOnlyHint: false,
			destructiveHint: true,
			idempotentHint: false,
		});
		expect(deriveAnnotations("destroy")).toEqual({
			readOnlyHint: false,
			destructiveHint: true,
			idempotentHint: false,
		});
	});

	it("agrees with the Python half for every class the registry actually uses", () => {
		// The Python mirror asserts the same triples in `backend/tests/test_companion_registry.py`.
		// This is the TypeScript end of that pair: derivation is a pure function of the class, so
		// a hand-authored annotation anywhere is a drift these two tests make impossible to keep.
		for (const entry of Object.values(COMPANION_VERBS)) {
			const annotations = deriveAnnotations(entry.toolClass);
			expect(annotations.readOnlyHint).toBe(entry.toolClass === "read");
			expect(annotations.destructiveHint).toBe(
				entry.toolClass === "mutate" || entry.toolClass === "destroy",
			);
		}
	});
});

// ---------------------------------------------------------------------------
// 05-13 (WR-06b): the ninth column, `argsIn`. Parameter placement used to be derived from the
// HTTP method inside `companion-executor.ts`, so three verbs sent their target path somewhere
// their sibling verbs did not and no reader could see the decision in a diff. It is now DECLARED
// per row, with a closed vocabulary and no default.
// ---------------------------------------------------------------------------

/** A well-formed row declaring the OTHER placement, so `body` stays expressible and enforced. */
const BODY_PLACEMENT_ROW = "verb|filebrowser|list_dir|read|GET|/api/resources|-|calls|body";

describe("the verb row's declared argument placement", () => {
	it("throws naming an argsIn value outside the closed vocabulary", () => {
		expect(() =>
			parseContractFile(
				`${KIND_ROW}\nverb|filebrowser|list_dir|read|GET|/api/resources|-|calls|sidecar`,
			),
		).toThrow(/unknown argsIn.*"sidecar".*"body", "query"/);
	});

	it("throws on an empty argsIn column rather than falling back to a default", () => {
		// An absent placement is not a default. A default here would be the invisible rule the
		// column exists to remove, written one layer further out.
		expect(() =>
			parseContractFile(`${KIND_ROW}\nverb|filebrowser|list_dir|read|GET|/api/resources|-|calls|`),
		).toThrow(/unknown argsIn.*""/);
	});

	it("parses a row declaring body placement", () => {
		const contract = parseContractFile(`${KIND_ROW}\n${BODY_PLACEMENT_ROW}`);

		expect(contract.verbs.filebrowser__list_dir?.argsIn).toBe("body");
	});

	it("gives every shipped verb a declared placement inside the vocabulary", () => {
		for (const [name, entry] of Object.entries(COMPANION_VERBS)) {
			expect(ARGS_IN_PLACEMENTS.has(entry.argsIn), `${name} argsIn`).toBe(true);
		}
	});

	it("agrees with the contract file on every verb's placement", () => {
		const contract = parseContractFile().verbs;
		for (const [name, entry] of Object.entries(COMPANION_VERBS)) {
			expect(contract[name]?.argsIn, `${name} argsIn`).toBe(entry.argsIn);
		}
	});
});

// ---------------------------------------------------------------------------
// 06-06: the curated `postiz` toolset. Six rows, one per INTEGRATE capability of the Phase 6
// COVERAGE matrix, decided against the Postiz v2.24.0 public controller. Pinned as LITERAL LINES:
// parity alone would pass on a row that was wrong identically in every representation.
// ---------------------------------------------------------------------------

const POSTIZ_VERB_ROWS = [
	"verb|postiz|create_post|create|POST|/api/public/v1/posts|list_posts|records|body",
	"verb|postiz|is_connected|read|GET|/api/public/v1/is-connected|-|calls|query",
	"verb|postiz|list_groups|read|GET|/api/public/v1/groups|-|calls|query",
	"verb|postiz|list_integrations|read|GET|/api/public/v1/integrations|-|calls|query",
	"verb|postiz|list_notifications|read|GET|/api/public/v1/notifications|-|calls|query",
	"verb|postiz|list_posts|read|GET|/api/public/v1/posts|-|calls|query",
];

describe("the postiz verb rows", () => {
	it("are the last six verb rows of the contract file, after every filebrowser row", () => {
		const lines = readFileSync(CONTRACT_PATH, "utf-8")
			.split("\n")
			.map((raw) => raw.trim())
			.filter((line) => line.startsWith(`${VERB_ROW_MARKER}|`));
		expect(lines.slice(-6)).toEqual(POSTIZ_VERB_ROWS);
		expect(lines.slice(0, -6).every((line) => line.startsWith("verb|filebrowser|"))).toBe(true);
	});

	it("ships postiz__create_post as create class, previewed by list_posts, capped in records", () => {
		expect(COMPANION_VERBS.postiz__create_post).toEqual({
			toolClass: "create",
			method: "POST",
			pathTemplate: "/api/public/v1/posts",
			readProbe: "list_posts",
			capUnit: "records",
			argsIn: "body",
		});
		expect(COMPANION_VERBS.postiz__list_posts?.toolClass).toBe("read");
	});

	it("carries no destroy-class postiz verb this phase", () => {
		const postiz = Object.entries(COMPANION_VERBS).filter(([name]) => name.startsWith("postiz__"));
		expect(postiz.length).toBe(6);
		for (const [name, entry] of postiz) {
			expect(entry.toolClass, `${name} class`).not.toBe("destroy");
			expect(entry.method, `${name} method`).not.toBe("DELETE");
		}
	});
});
