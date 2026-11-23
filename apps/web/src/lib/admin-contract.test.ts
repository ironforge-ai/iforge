import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/**
 * TEST-ONLY drift gate over the hand-mirrored admin client (T-03-75).
 *
 * `admin-client.ts` names this risk in its response-contracts comment: the
 * admin router is `include_in_schema=False`, so `bun run generate-api` emits
 * nothing for it and NOTHING generated checks that the mirrored shapes still match the
 * server. A renamed backend field fails silently at runtime as `undefined` —
 * the comment was the only safeguard, and a comment is not a gate. Nineteen
 * plans of green tests coexisted with a capability reachable only through
 * `psql` partly because of that (G-03-62).
 *
 * Paths resolve from `import.meta.url`, never `process.cwd()`, so this behaves
 * identically under `bun test --cwd apps/web` and a whole-suite run — the
 * property `ui-spec-contract.ts` documents at its `UI_SPEC_PATH`.
 *
 * VACUITY IS THE FAILURE MODE THIS FILE GUARDS AGAINST. A regex that starts
 * matching nothing would turn every assertion below into `[] == []` and stay
 * green forever. Hence: both extractors THROW when their target is not found,
 * every extracted set is asserted non-empty, and one known set is pinned
 * exactly.
 */

function schemaSource(moduleName: string): string {
	const path = fileURLToPath(
		new URL(`../../../../backend/app/schemas/${moduleName}.py`, import.meta.url),
	);
	try {
		return readFileSync(path, "utf8");
	} catch (cause) {
		throw new Error(
			`admin-contract: could not read ${path}. A parity assertion cannot pass without it.`,
			{ cause },
		);
	}
}

const ADMIN_CLIENT_SRC = readFileSync(
	fileURLToPath(new URL("./admin-client.ts", import.meta.url)),
	"utf8",
);

const SCHEMA_SOURCES: Record<string, string> = {
	admin_export: schemaSource("admin_export"),
	admin_tenant_lifecycle: schemaSource("admin_tenant_lifecycle"),
};

/**
 * Top-level annotated field names of one `class X(BaseModel):` block.
 *
 * Only lines indented exactly four spaces matching `name:` count, so nested
 * bodies, `model_config = ConfigDict(...)` (an assignment, not an annotation),
 * comments and blank lines are all skipped. Docstrings are tracked explicitly
 * rather than pattern-matched away — a prose line inside one must never be
 * mistaken for a field.
 */
function pydanticFields(source: string, className: string): string[] {
	const lines = source.split("\n");
	const start = lines.findIndex((line) => line.startsWith(`class ${className}(`));
	if (start === -1) {
		throw new Error(
			`admin-contract: no \`class ${className}(...)\` in the schema source. ` +
				"Renamed or deleted? Update the pair table rather than deleting the assertion.",
		);
	}

	const fields: string[] = [];
	let inDocstring = false;
	for (let i = start + 1; i < lines.length; i++) {
		const line = lines[i] as string;
		if (line.trim() === "") continue;
		if (!inDocstring && /^\S/.test(line)) break; // dedent to module level ends the class
		const tripleQuotes = (line.match(/"""/g) ?? []).length;
		if (inDocstring) {
			if (tripleQuotes > 0) inDocstring = false;
			continue;
		}
		if (tripleQuotes === 1) {
			inDocstring = true;
			continue;
		}
		if (tripleQuotes > 1) continue; // single-line docstring
		const match = /^ {4}([A-Za-z_][A-Za-z0-9_]*)\s*:/.exec(line);
		if (match) fields.push(match[1] as string);
	}
	return fields;
}

/**
 * Property names of one `export interface Y {` block, tolerating an optional
 * marker. JSDoc and `//` comments are stripped before matching.
 */
function interfaceFields(source: string, interfaceName: string): string[] {
	const header = `export interface ${interfaceName} {`;
	const start = source.indexOf(header);
	if (start === -1) {
		throw new Error(
			`admin-contract: no \`${header}\` in admin-client.ts. ` +
				"Renamed or deleted? Update the pair table rather than deleting the assertion.",
		);
	}
	const end = source.indexOf("\n}", start);
	if (end === -1) {
		throw new Error(`admin-contract: unterminated \`${header}\` block in admin-client.ts.`);
	}
	const body = source
		.slice(start + header.length, end)
		.replace(/\/\*[\s\S]*?\*\//g, "")
		.replace(/\/\/.*$/gm, "");
	const fields: string[] = [];
	for (const line of body.split("\n")) {
		const match = /^\t([A-Za-z_][A-Za-z0-9_]*)\??\s*:/.exec(line);
		if (match) fields.push(match[1] as string);
	}
	return fields;
}

/**
 * Members of one Python `X = Literal["a", "b"]` alias, in declaration order.
 *
 * Throws on an alias it cannot find, so a regex that stops matching fails the
 * suite instead of quietly comparing two empty sets (the vacuity rule the
 * field extractors above already follow).
 */
function pydanticLiteralMembers(source: string, aliasName: string): string[] {
	const match = new RegExp(`^${aliasName}\\s*=\\s*Literal\\[([^\\]]*)\\]`, "m").exec(source);
	if (match === null) {
		throw new Error(`no \`${aliasName} = Literal[...]\` in this schema module`);
	}
	return [...(match[1] as string).matchAll(/"([^"]*)"/g)].map((m) => m[1] as string);
}

/**
 * Members of one TypeScript `export type X = "a" | "b";` alias, in declaration
 * order. Throws on an absent alias, for the same reason as above.
 */
function tsUnionMembers(source: string, aliasName: string): string[] {
	const match = new RegExp(`^export type ${aliasName}\\s*=\\s*([^;]*);`, "m").exec(source);
	if (match === null) {
		throw new Error(`no \`export type ${aliasName} = ...\` in the admin client`);
	}
	return [...(match[1] as string).matchAll(/"([^"]*)"/g)].map((m) => m[1] as string);
}

/**
 * The mirror map. EXPLICIT rather than derived by name, because one pair does
 * not match by name (`AdminTenantExportDownloadResponse` ↔
 * `AdminTenantExportDownload`) and a name-matching heuristic would silently
 * drop it.
 */
const PAIRS: ReadonlyArray<readonly [string, string, string]> = [
	["admin_export", "AdminTenantExportCreatedResponse", "AdminTenantExportCreatedResponse"],
	["admin_export", "AdminTenantExportListItem", "AdminTenantExportListItem"],
	["admin_export", "AdminTenantExportListResponse", "AdminTenantExportListResponse"],
	["admin_export", "AdminTenantExportDownloadResponse", "AdminTenantExportDownload"],
	["admin_export", "AdminUserListItem", "AdminUserListItem"],
	["admin_export", "AdminUserListResponse", "AdminUserListResponse"],
	["admin_tenant_lifecycle", "AdminBillingExemptionUpdate", "AdminBillingExemptionUpdate"],
	["admin_tenant_lifecycle", "AdminBillingExemptionResponse", "AdminBillingExemptionResponse"],
	["admin_tenant_lifecycle", "AdminTenantDeleteResponse", "AdminTenantDeleteResponse"],
	["admin_tenant_lifecycle", "AdminPurgeCancelResponse", "AdminPurgeCancelResponse"],
	["admin_tenant_lifecycle", "AdminTenantPurgeInfo", "AdminTenantPurgeInfo"],
	["admin_tenant_lifecycle", "AdminTenantLifecycleResponse", "AdminTenantLifecycleResponse"],
] as const;

describe("the hand-mirrored admin client cannot drift from the Pydantic schemas", () => {
	for (const [moduleName, pyClass, tsInterface] of PAIRS) {
		test(`${moduleName}.${pyClass} ↔ ${tsInterface}`, () => {
			const py = pydanticFields(SCHEMA_SOURCES[moduleName] as string, pyClass);
			const ts = interfaceFields(ADMIN_CLIENT_SRC, tsInterface);
			expect(py.length).toBeGreaterThan(0);
			expect(ts.length).toBeGreaterThan(0);
			expect([...ts].sort()).toEqual([...py].sort());
		});
	}
});

describe("vacuity guards — a parser matching nothing must fail loudly", () => {
	test("an absent Pydantic class throws rather than yielding an empty set", () => {
		expect(() => pydanticFields(SCHEMA_SOURCES.admin_export as string, "AdminNoSuchModel")).toThrow(
			/no `class AdminNoSuchModel/,
		);
	});

	test("an absent TS interface throws rather than yielding an empty set", () => {
		expect(() => interfaceFields(ADMIN_CLIENT_SRC, "AdminNoSuchInterface")).toThrow(
			/no `export interface AdminNoSuchInterface/,
		);
	});

	test("one known field set is pinned exactly, so a regex that stops matching fails", () => {
		expect(
			pydanticFields(SCHEMA_SOURCES.admin_tenant_lifecycle as string, "AdminTenantPurgeInfo"),
		).toEqual(["status", "purge_after", "completed_at", "error", "attempt_count", "claimed_at"]);
	});

	test("a docstring line is never mistaken for a field", () => {
		const source = [
			"class AdminFake(BaseModel):",
			'    """A docstring whose second line reads like an annotation:',
			"    status: not a field at all.",
			'    """',
			"    # a comment: also not a field",
			"    real_field: str",
			"",
			"",
			"class AdminOther(BaseModel):",
			"    other: int",
		].join("\n");
		expect(pydanticFields(source, "AdminFake")).toEqual(["real_field"]);
	});
});

describe("the field that crossed the seam for G-03-62 is on BOTH sides", () => {
	test("AdminUserListItem carries clerk_id in Python and TypeScript", () => {
		expect(pydanticFields(SCHEMA_SOURCES.admin_export as string, "AdminUserListItem")).toContain(
			"clerk_id",
		);
		expect(interfaceFields(ADMIN_CLIENT_SRC, "AdminUserListItem")).toContain("clerk_id");
	});
});

/**
 * The mirrored `Literal` unions. Field NAMES were gated from the start; the
 * status VALUES were not, so adding a backend status left every assertion and
 * `tsc` green and shipped as a status-tone lookup (`PURGE_STATUS_TONE[status]`)
 * returning `undefined` — a blank badge, the exact silent-`undefined` failure
 * this gate exists to stop (WR-03).
 */
const UNION_PAIRS: ReadonlyArray<readonly [string, string, string]> = [
	["admin_export", "TenantExportStatus", "TenantExportStatus"],
	["admin_tenant_lifecycle", "TenantPurgeStatus", "TenantPurgeStatus"],
] as const;

describe("the mirrored status unions cannot drift either", () => {
	for (const [moduleName, pyAlias, tsAlias] of UNION_PAIRS) {
		test(`${moduleName}.${pyAlias} ↔ ${tsAlias}`, () => {
			const py = pydanticLiteralMembers(SCHEMA_SOURCES[moduleName] as string, pyAlias);
			const ts = tsUnionMembers(ADMIN_CLIENT_SRC, tsAlias);
			expect(py.length).toBeGreaterThan(0);
			expect(ts.length).toBeGreaterThan(0);
			expect([...ts].sort()).toEqual([...py].sort());
		});
	}

	test("an absent Python alias throws rather than yielding an empty set", () => {
		expect(() =>
			pydanticLiteralMembers(SCHEMA_SOURCES.admin_export as string, "NoSuchStatus"),
		).toThrow(/NoSuchStatus/);
	});

	test("an absent TS alias throws rather than yielding an empty set", () => {
		expect(() => tsUnionMembers(ADMIN_CLIENT_SRC, "NoSuchStatus")).toThrow(/NoSuchStatus/);
	});

	test("one known union is pinned exactly, so a regex that stops matching fails", () => {
		expect(
			pydanticLiteralMembers(SCHEMA_SOURCES.admin_export as string, "TenantExportStatus"),
		).toEqual(["queued", "running", "complete", "failed"]);
	});
});

describe("the gate's own coverage is asserted, not assumed", () => {
	test("every Literal alias in the mirrored schema modules is in UNION_PAIRS", () => {
		// PAIRS and UNION_PAIRS are hand-maintained. Without this, a new mirrored
		// union is simply absent from the gate and nothing says so.
		const covered = new Set(UNION_PAIRS.map(([moduleName, pyAlias]) => `${moduleName}.${pyAlias}`));
		const declared: string[] = [];
		for (const [moduleName, source] of Object.entries(SCHEMA_SOURCES)) {
			for (const match of (source as string).matchAll(/^(\w+)\s*=\s*Literal\[/gm)) {
				declared.push(`${moduleName}.${match[1]}`);
			}
		}
		expect(declared.length).toBeGreaterThan(0);
		expect(declared.filter((alias) => !covered.has(alias))).toEqual([]);
	});
});
