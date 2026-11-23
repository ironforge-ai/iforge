import { describe, expect, test } from "bun:test";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { uiSpecCopyRow } from "./ui-spec-contract";

/**
 * Parity between the phase 03.3 Copywriting Contract and the strings the CLI actually ships.
 *
 * The claim "the shipped string and the contract row are the same string" was made by three
 * plans in the phase-03 remediation and checked by none of them; the only gate was a human
 * reading a diff, which is how a CTA naming a destination it does not present reached
 * production. This file is that gate for the S1 surface (its `migrate` rows retired 2026-09-30).
 *
 * Two deliberate mechanics:
 *
 *   - The CLI source is read as TEXT, never imported. `apps/web` and `packages/cli` are separate
 *     packages; an import would create a dependency edge between them and pull the CLI's module
 *     graph into this app's typecheck, to assert something about a string literal.
 *   - Nothing here softens `uiSpecCopyRow`. It throws on a missing file, a missing row, and a
 *     row carrying no quoted copy, and that loudness is the point — a parity assertion that
 *     passes by comparing nothing to nothing reads as evidence while proving nothing. There is
 *     no try/catch around the reader and no empty-string default.
 *
 * SCOPED TO S1, BY AN EXPLICIT LIST. The element keys below are written out rather than
 * discovered, and the spec's tables are never enumerated or iterated. S1 is the only surface
 * this plan ships copy for, and the same spec file deliberately carries at least one row written
 * WITHOUT quotes — an instruction to the implementer rather than copy, which the reader throws
 * on by design. A test that walked the tables would fail on a row this plan does not own. The
 * explicit list is the second guard on that, so the scoping survives a change in plan ordering.
 *
 * A later plan extends this file by appending its own `CopyRowCheck[]` and reusing
 * `assertRowShipped`; nothing here needs restructuring to make room for one.
 */

const REPO_ROOT = fileURLToPath(new URL("../../../../", import.meta.url));

const SPEC_PATH = join(
	REPO_ROOT,
	".planning",
	"phases",
	"03.3-brand-migration-deferred-identities-cli-strings-artwork-inse",
	"03.3-UI-SPEC.md",
);

// Upstream #1229 (sync step 3, 06.1-08) moved Hermes MCP removal, and this notice with it, from
// `commands/teardown.ts` into the adapter lifecycle.
const TEARDOWN_SOURCE = "packages/cli/src/adapters/mcp-lifecycle.ts";

interface CopyRowCheck {
	/** The Element-column key, verbatim. */
	element: string;
	/** Repo-relative path of the file that must ship the row's string. */
	source: string;
	/**
	 * Substrings of the ROW that the source renders through an expression rather than spelling out.
	 *
	 * One row needs this: `teardown — scope note` says "Removing iforge entries only", and the
	 * command spells the product name as `${MCP_SERVER_NAME}` because the same identifier is the
	 * MCP server name it removes from three agents' config files. The rendered line matches the
	 * contract exactly; the source text cannot, and asserting the row verbatim would force the
	 * command to restate a name it already holds in a constant — the duplication P33-11 exists to
	 * prevent, introduced by the test meant to prevent it.
	 *
	 * The fixed text either side is still asserted. Only the interpolated span is exempt.
	 */
	renderedFromCode?: string[];
}

/**
 * The runtime-interpolated spans of a copy row.
 *
 * `{count} files` is ONE span rather than a brace followed by a literal noun: contract rule N8
 * routes every printed cardinality through a single formatter that is singular at 1, so the
 * shipped text renders `1 file` and `2 files` and the row's `files` never appears beside the
 * brace in source. Comparing the fixed parts either side of each span is the strongest claim
 * available without executing the command.
 *
 * `{total} units` is the S2 row of the same shape and is listed for the same reason, explicitly
 * rather than as a general "brace followed by any plural noun" pattern. A general rule would
 * silently stop asserting a literal noun the day someone wrote one after a placeholder, and the
 * whole value of this file is that it fails loudly.
 *
 * Placeholder names are matched case-INsensitively (`{newUnit}`, `{envVar}`). The S1 rows happened
 * to be all-lowercase and a lowercase-only class passed against them; S2's camelCase names are
 * what showed the class was measuring the rows that existed rather than the shape of a
 * placeholder. An unmatched placeholder does not weaken the assertion quietly — it makes the
 * literal `{newUnit}` part of a segment the source can never contain, so the row fails — but it
 * fails for a reason that has nothing to do with the copy.
 */
const INTERPOLATED_SPAN = /\{count\} files|\{total\} units|\{[A-Za-z]+\}/g;

/** Fixed segments worth asserting on. A one-character joiner proves nothing about parity. */
function literalSegments(copy: string, renderedFromCode: string[] = []): string[] {
	let segments = copy.split(INTERPOLATED_SPAN);
	for (const rendered of renderedFromCode) {
		segments = segments.flatMap((segment) => segment.split(rendered));
	}
	return segments.filter((segment) => segment.trim().length >= 2);
}

const sourceCache = new Map<string, string>();

function readSource(repoRelative: string): string {
	const cached = sourceCache.get(repoRelative);
	if (cached !== undefined) return cached;
	const text = readFileSync(join(REPO_ROOT, repoRelative), "utf8");
	sourceCache.set(repoRelative, text);
	return text;
}

/**
 * Assert one contract row is the string its file ships.
 *
 * A row with no braces must appear verbatim. A row with braces is checked segment by segment,
 * because the interpolation itself only exists at runtime.
 */
function assertRowShipped(check: CopyRowCheck): void {
	const row = uiSpecCopyRow(check.element, SPEC_PATH);
	const source = readSource(check.source);

	expect(row.copy.length).toBeGreaterThan(0);
	for (const segment of literalSegments(row.copy, check.renderedFromCode)) {
		expectShipped(check, source, segment);
	}
	if (row.body !== null) {
		expect(row.body.length).toBeGreaterThan(0);
		for (const segment of literalSegments(row.body, check.renderedFromCode)) {
			expectShipped(check, source, segment);
		}
	}
}

/**
 * Assert one segment is present, and say so in a line rather than a file.
 *
 * `expect(source).toContain(segment)` would be the obvious spelling, and on failure it prints
 * the whole CLI source as the "received" value — several hundred lines of unrelated code around
 * the one string that is missing. Comparing a computed marker keeps the diff to the segment.
 */
function expectShipped(check: CopyRowCheck, source: string, segment: string): void {
	const found = source.includes(segment) ? segment : `${check.source} is missing this segment`;
	expect(found).toBe(segment);
}

/**
 * S1 and most of S2 were the `iforge migrate` command's copy. The command was removed on
 * 2026-09-30 with every other old-brand alias and migration path (user directive: there is no
 * production deployment of iforge to migrate), so its rows are retired rather than checked, and
 * the retirement itself is asserted below so the command cannot come back without its contract.
 *
 * `teardown — scope note` is the one surviving row. `mcp-lifecycle.ts` spells the product name as
 * `${MCP_SERVER_NAME}` — the same identifier it uses to name the MCP server it removes — which is
 * why it needs `renderedFromCode`.
 */
const SURVIVING_ROWS: CopyRowCheck[] = [
	{
		element: "teardown — scope note",
		source: TEARDOWN_SOURCE,
		renderedFromCode: ["iforge"],
	},
];

describe("phase 03.3 copy contract", () => {
	test("the reader is pointed at a spec that exists and carries the surviving row", () => {
		// A path typo would otherwise read as an unrelated failure of the row below.
		expect(uiSpecCopyRow("teardown — scope note", SPEC_PATH).copy.length).toBeGreaterThan(0);
	});

	test("the migrate command whose rows were retired is gone from the CLI", () => {
		expect(existsSync(join(REPO_ROOT, "packages/cli/src/commands/migrate.ts"))).toBe(false);
		expect(readSource("packages/cli/src/index.ts")).not.toContain('.command("migrate")');
	});

	for (const check of SURVIVING_ROWS) {
		test(`ships the contract copy for ${check.element}`, () => {
			assertRowShipped(check);
		});
	}
});
