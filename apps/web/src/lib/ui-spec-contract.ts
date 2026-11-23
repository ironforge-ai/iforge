import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/**
 * TEST-ONLY reader over the `## Copywriting Contract` table in
 * `.planning/phases/03-invite-gated-access-tenant-data-rights/03-UI-SPEC.md`.
 *
 * Imported only from `*.test.*` files. Plan 03-19 reuses it for the S3 rows.
 *
 * **Its contract is: the shipped string and the contract row are the same
 * string.** That claim was made by three plans in this remediation and checked
 * by none of them — the only gate was a human reading a diff, which is exactly
 * how UI-REVIEW blocker 2 ("Continue to billing", naming a task its
 * destination does not present) and finding 10 (`REDEEM_ERROR_COPY.network`
 * shipping with no row at all) both got through. A test that reads BOTH sides
 * turns the claim into a gate.
 *
 * Two deliberate properties:
 *
 * - The spec file is read INSIDE the function, never at module scope, so
 *   importing this module has no side effect and no build that accidentally
 *   includes it can fail on a missing `.planning/` path.
 * - Every failure THROWS, naming the path or the key. Returning `undefined` or
 *   `""` would let a parity assertion pass because it compared nothing to
 *   nothing — a green test that reads as evidence while proving nothing, which
 *   is strictly worse than having no test.
 *
 * The path is resolved from `import.meta.url` rather than `process.cwd()`, so
 * it behaves identically under `bun test --cwd apps/web` and a whole-suite run.
 */

/** Default location of the phase-03 UI spec, relative to this module. */
export const UI_SPEC_PATH = fileURLToPath(
	new URL(
		"../../../../.planning/phases/03-invite-gated-access-tenant-data-rights/03-UI-SPEC.md",
		import.meta.url,
	),
);

export interface UiSpecCopyRow {
	/** The primary quoted string in the Copy column. */
	copy: string;
	/** The quoted string after a trailing `— body: "…"` clause, or `null`. */
	body: string | null;
}

function readSpec(specPath: string): string {
	try {
		return readFileSync(specPath, "utf8");
	} catch (cause) {
		throw new Error(
			`ui-spec-contract: could not read the UI spec at ${specPath}. ` +
				"A copy-parity assertion cannot pass without it.",
			{ cause },
		);
	}
}

/**
 * Find one `| Element | Copy |` row and split its Copy cell.
 *
 * Rows sometimes carry a trailing `— body: "…"` clause (the S1 post-redeem CTA
 * is one), so this extracts the QUOTED strings rather than the whole cell —
 * comparing a component literal against `"Set up your subscription" — body: "…"`
 * would never match anything.
 *
 * Throws when the file is unreadable, when no row carries `element` in its
 * Element column, or when the matched row's Copy cell contains no quoted string
 * at all (some rows are instructions, not copy — "S1 error — revoked" tells the
 * implementer to render the *invalid* copy and quotes nothing).
 */
export function uiSpecCopyRow(element: string, specPath: string = UI_SPEC_PATH): UiSpecCopyRow {
	const spec = readSpec(specPath);
	for (const line of spec.split("\n")) {
		if (!line.startsWith("|")) continue;
		const cells = line.split("|").map((cell) => cell.trim());
		// cells[0] is the empty string before the leading pipe.
		if (cells[1] !== element) continue;
		const cell = cells[2] ?? "";
		const quoted = [...cell.matchAll(/"([^"]*)"/g)].map((match) => match[1] as string);
		const copy = quoted[0];
		if (copy === undefined || copy === "") {
			throw new Error(
				`ui-spec-contract: the row "${element}" in ${specPath} carries no quoted copy ` +
					`(cell: ${cell}). It is an instruction, not a copy string.`,
			);
		}
		const bodyMatch = /body:\s*"([^"]*)"/.exec(cell);
		return { copy, body: bodyMatch?.[1] ?? null };
	}
	throw new Error(
		`ui-spec-contract: no Copywriting Contract row for "${element}" in ${specPath}. ` +
			"Every customer-facing string needs a row — add one rather than deleting the assertion.",
	);
}

/** The primary quoted copy string for an Element-column key. See `uiSpecCopyRow`. */
export function uiSpecCopy(element: string, specPath: string = UI_SPEC_PATH): string {
	return uiSpecCopyRow(element, specPath).copy;
}

/**
 * Every cell of the first table row whose FIRST cell is `element`, trimmed,
 * the leading empty cell dropped (so index 0 is the element itself).
 *
 * For tables whose copy is not in the second column — the empty-state table
 * carries Heading / Body / Action in columns three to five, and the status
 * token tables carry the chip label in column three. Throws when no row
 * matches, for the same reason `uiSpecCopyRow` does.
 */
export function uiSpecRowCells(element: string, specPath: string = UI_SPEC_PATH): string[] {
	const spec = readSpec(specPath);
	for (const line of spec.split("\n")) {
		if (!line.startsWith("|")) continue;
		const cells = line
			.split("|")
			.slice(1, -1)
			.map((cell) => cell.trim());
		if (cells[0] === element) return cells;
	}
	throw new Error(
		`ui-spec-contract: no table row whose first cell is "${element}" in ${specPath}.`,
	);
}

/**
 * Every double-quoted string across a row's cells after the element cell, in
 * order. For rows that quote more than one string — "Installed" / "Available",
 * the three tier options, an empty state's heading, body and action — where
 * `uiSpecCopy` returns only the first. Throws when the row quotes nothing.
 */
export function uiSpecRowQuotes(element: string, specPath: string = UI_SPEC_PATH): string[] {
	const quoted = uiSpecRowCells(element, specPath)
		.slice(1)
		.flatMap((cell) => [...cell.matchAll(/"([^"]*)"/g)].map((match) => match[1] as string));
	if (quoted.length === 0) {
		throw new Error(
			`ui-spec-contract: the row "${element}" in ${specPath} quotes no copy. ` +
				"A parity assertion against it would compare nothing to nothing.",
		);
	}
	return quoted;
}
