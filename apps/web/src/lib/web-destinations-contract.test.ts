import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { DESTINATIONS } from "@clawdi/shared";

import { uiSpecCopyRow } from "./ui-spec-contract";

/**
 * The web half of the phase 03.3 destination contract (plan 03.3-07, S5 / S6).
 *
 * Three claims, none of which any other test in this app makes:
 *
 *   1. **Authority-sourcing.** No component that names a destination restates its URL. The
 *      `DESTINATIONS` record in `packages/shared` is the single place a destination moves,
 *      and a component that spells a host defeats that by construction — the next move
 *      edits the record, the component keeps its stale literal, and nothing fails.
 *   2. **The labels did not move with the targets.** Plan 03.3-07 changed `href` and const
 *      values at eight sites. The UI-SPEC declares which visible strings must be
 *      byte-identical afterwards, and this reads BOTH sides — the spec row through
 *      `uiSpecCopyRow`, the shipped string out of the component — so "no label changed"
 *      is a gate rather than a claim a human made while reading a diff.
 *   3. **The Help menu's shape.** Its items appear in the declared order and every anchor
 *      carries a non-empty href. The app cannot observe an external destination's failure,
 *      so a well-formed, non-empty href at render is the strongest assertion available
 *      (UI-SPEC S5, `error` → backstop).
 *
 * Plus a fourth, which is the record of a decision rather than a property of the code: the
 * Telegram destination is **intentionally absent**. See `TELEGRAM_ROW_KEY` below.
 *
 * **Sources are read as TEXT, never imported.** Rendering `app-sidebar.tsx` would pull in
 * the router, the sidebar context, dnd-kit and a query client to assert something about a
 * string literal. Every claim here is a claim about source, so source is what is read.
 *
 * **Nothing here softens `uiSpecCopyRow`.** It throws on a missing file, a missing row, and
 * a row carrying no quoted copy. That loudness is the point: a parity assertion that passes
 * by comparing nothing to nothing reads as evidence while proving nothing.
 */

const REPO_ROOT = fileURLToPath(new URL("../../../../", import.meta.url));

const SPEC_PATH = join(
	REPO_ROOT,
	".planning",
	"phases",
	"03.3-brand-migration-deferred-identities-cli-strings-artwork-inse",
	"03.3-UI-SPEC.md",
);

const SIDEBAR = "apps/web/src/components/app-sidebar.tsx";
const NOT_FOUND = "apps/web/src/components/app-not-found.tsx";
const NOT_FOUND_TEST = "apps/web/src/components/app-not-found.test.tsx";
const ADD_AGENT_SETUP = "apps/web/src/components/dashboard/add-agent-setup.tsx";
const ENTITY_ICON = "apps/web/src/components/entity-icon.tsx";
const DUNNING = "apps/web/src/hosted/billing/components/compute-dunning-banner.tsx";
const ACCESS_UNAVAILABLE = "apps/web/src/pages/gate/access-unavailable-page.tsx";
const ACCESS_UNAVAILABLE_TEST = "apps/web/src/pages/gate/access-unavailable-page.test.tsx";
const DEACTIVATED = "apps/web/src/pages/gate/deactivated-page.tsx";

/**
 * Every site in plan 03.3-07's interfaces table, written out rather than discovered.
 *
 * The two `.test.tsx` files are here on purpose: both pin a destination, and a pinned
 * literal is exactly as stale-able as a shipped one — worse, because it goes green while
 * asserting the wrong thing.
 */
const DESTINATION_SITES = [
	SIDEBAR,
	NOT_FOUND,
	NOT_FOUND_TEST,
	ADD_AGENT_SETUP,
	ENTITY_ICON,
	DUNNING,
	ACCESS_UNAVAILABLE,
	ACCESS_UNAVAILABLE_TEST,
	DEACTIVATED,
] as const;

/**
 * Markers of a destination the authority owns, or of one it deliberately does not.
 *
 * `t.me/` is in the list although `DESTINATIONS.telegramUrl` is `null`: the whole point of
 * the null is that no component may name a Telegram destination, so a `t.me` literal
 * appearing anywhere in these files is the regression this marker catches.
 */
const AUTHORITY_HOST_MARKERS = [
	"iforge.dev",
	"clawdi.ai",
	"t.me/",
	"deepwiki.com",
	"github.com/",
	"mailto:",
] as const;

/**
 * The one form in which a source may spell `mailto:` — the address itself comes from the
 * authority, so the scheme prefix is structure rather than a restated destination.
 *
 * Assembled from two pieces so no literal here contains `${`. Written whole, it is a plain
 * string that looks exactly like a template placeholder someone forgot to make a template,
 * which is `lint/suspicious/noTemplateCurlyInString` and a warning this file would be adding
 * to the repo rather than removing.
 */
const PERMITTED_MAILTO = ["mailto:$", "{DESTINATIONS.supportEmail}"].join("");

/** Block comments, which carry prose. A host named in prose is documentation, not a destination. */
const BLOCK_COMMENT = /\/\*[\s\S]*?\*\//g;

/**
 * Line comments — but NOT a `//` that follows a colon, which is a URL scheme separator.
 *
 * Without that exception the scanner truncates `"https://t.me/…"` at the URL's own slashes
 * and reports the file clean: a scanner that fails open on the exact literal it exists to
 * catch. The colon rule is the whole difference, and it is why this is not the obvious
 * `/\/\/.*$/gm`.
 */
const LINE_COMMENT = /(^|[^:])\/\/[^\n]*/gm;

const sourceCache = new Map<string, string>();

function readSource(repoRelative: string): string {
	const cached = sourceCache.get(repoRelative);
	if (cached !== undefined) return cached;
	const text = readFileSync(join(REPO_ROOT, repoRelative), "utf8");
	sourceCache.set(repoRelative, text);
	return text;
}

/** JSX wraps a long sentence across lines; the contract row is one line. Compare collapsed. */
function collapse(text: string): string {
	return text.replace(/\s+/g, " ").trim();
}

/** One source with its comments removed — the code a browser actually runs. */
function strippedSource(repoRelative: string): string {
	return readSource(repoRelative)
		.replace(BLOCK_COMMENT, " ")
		.replace(LINE_COMMENT, (_match, prefix: string) => prefix);
}

/**
 * Markers of a destination the authority owns, found in one source's CODE.
 *
 * Reported as the marker rather than the surrounding line: a failure here names which
 * destination was restated, which is the actionable half. The line is one grep away.
 */
function restatedDestinations(repoRelative: string): string[] {
	const code = strippedSource(repoRelative).split(PERMITTED_MAILTO).join("");
	return AUTHORITY_HOST_MARKERS.filter((marker) => code.includes(marker));
}

/** The `HelpMenuItems` function body — the menu group S5 governs. */
function helpMenuSource(): string {
	const source = readSource(SIDEBAR);
	const start = source.indexOf("function HelpMenuItems()");
	expect(start).toBeGreaterThan(-1);
	const end = source.indexOf("\n}", start);
	expect(end).toBeGreaterThan(start);
	return source.slice(start, end);
}

/**
 * Every `href` value in a JSX block, in source order, as written.
 *
 * A regex cannot do this: `href={`mailto:${DESTINATIONS.supportEmail}`}` closes an inner
 * brace before the attribute's own, and `[^}]*` stops at the wrong one. Counting braces is
 * the difference between reading the whole expression and reading half of it.
 */
function hrefValues(block: string): string[] {
	const values: string[] = [];
	const attribute = "href=";
	let index = block.indexOf(attribute);
	while (index !== -1) {
		const valueStart = index + attribute.length;
		const opener = block[valueStart];
		if (opener === '"' || opener === "'") {
			const close = block.indexOf(opener, valueStart + 1);
			values.push(block.slice(valueStart + 1, close));
		} else if (opener === "{") {
			let depth = 0;
			let cursor = valueStart;
			for (; cursor < block.length; cursor += 1) {
				if (block[cursor] === "{") depth += 1;
				else if (block[cursor] === "}") {
					depth -= 1;
					if (depth === 0) break;
				}
			}
			values.push(block.slice(valueStart + 1, cursor));
		}
		index = block.indexOf(attribute, valueStart);
	}
	return values;
}

interface LabelRow {
	/** The Element-column key in the spec's Copywriting Contract, verbatim. */
	element: string;
	/** Repo-relative path of the file that must ship the row's string unchanged. */
	source: string;
}

/**
 * The S5 / S6 rows the UI-SPEC declares as labels that must NOT change.
 *
 * Written out rather than discovered by walking the spec's tables. The same file carries
 * rows written WITHOUT quotes — instructions to the implementer, which the reader throws on
 * by design — so an enumerating test would fail on a row it does not own. `TELEGRAM_ROW_KEY`
 * below is one of those, and it is asserted separately and deliberately.
 *
 * `help menu — support label` is absent here for a different reason: it is the ONE visible
 * string plan 03.3-07 changed, because that label *is* the support address. It gets its own
 * assertion against the authority rather than against the component's text, since the
 * component renders it as an expression.
 */
const UNCHANGED_LABELS: LabelRow[] = [
	{ element: "help menu — docs label", source: SIDEBAR },
	{ element: "help menu — github label", source: SIDEBAR },
	{ element: "help menu — support aria-label", source: SIDEBAR },
	{ element: "sidebar logo — aria-label", source: SIDEBAR },
	{ element: "dunning — support cta", source: DUNNING },
	{ element: "access unavailable — support cta", source: ACCESS_UNAVAILABLE },
	{ element: "deactivated — support cta", source: DEACTIVATED },
	{ element: "deactivated — body", source: DEACTIVATED },
	{ element: "404 — heading", source: NOT_FOUND },
	{ element: "404 — body", source: NOT_FOUND },
	{ element: "404 — external cta", source: NOT_FOUND },
];

/**
 * The Help menu's items, in the order S5 declares, keyed by the aria-label each carries.
 *
 * Three items, not four. The fourth was Telegram, and it was REMOVED by decision — see
 * `TELEGRAM_ROW_KEY`.
 */
const HELP_MENU_ARIA_LABELS = ["Open Docs", "Open GitHub", "Email support"] as const;

/**
 * The S5 row that records the removed Telegram item.
 *
 * The fork owns no Telegram destination; the operator was asked and declined to create one.
 * The row therefore carries NO quoted copy, and `uiSpecCopyRow` throws on it — which is the
 * honest outcome, because there is no shipped string to be in parity with. A row carrying a
 * placeholder would let a parity assertion pass against copy nobody chose, and would imply a
 * link that does not exist.
 */
const TELEGRAM_ROW_KEY = "help menu — telegram";

describe("phase 03.3 web destinations contract", () => {
	test("the reader is pointed at a spec that exists and carries S5", () => {
		// A path typo would otherwise make every assertion below fail for one reason while
		// reading as a dozen independent failures.
		expect(uiSpecCopyRow("help menu — docs label", SPEC_PATH).copy).toBe("Docs");
	});

	test("no destination site restates a URL the authority owns", () => {
		for (const site of DESTINATION_SITES) {
			expect({ site, restated: restatedDestinations(site) }).toEqual({ site, restated: [] });
		}
	});

	test("every site that names a destination imports the authority", () => {
		// The scan above proves no site spells a host. It cannot, on its own, prove a site
		// still names the destination at all — a deleted href passes it too. Pairing the two
		// is what makes "reads the authority" the only way to stay green.
		for (const site of [
			SIDEBAR,
			NOT_FOUND,
			NOT_FOUND_TEST,
			ADD_AGENT_SETUP,
			DUNNING,
			ACCESS_UNAVAILABLE,
			ACCESS_UNAVAILABLE_TEST,
			DEACTIVATED,
		]) {
			expect(`${site}:${readSource(site).includes("DESTINATIONS")}`).toBe(`${site}:true`);
		}
	});

	test("the labels the UI-SPEC declares unchanged are still shipped verbatim", () => {
		for (const row of UNCHANGED_LABELS) {
			const { copy } = uiSpecCopyRow(row.element, SPEC_PATH);
			expect(copy.length).toBeGreaterThan(0);
			const source = collapse(readSource(row.source));
			// Compare a computed marker rather than `expect(source).toContain(copy)`: the
			// latter prints an entire component as the received value on failure.
			const found = source.includes(copy) ? copy : `${row.source} no longer ships this string`;
			expect(found).toBe(copy);
		}
	});

	test("the Help-menu support label is the authority's address, rendered from it", () => {
		// The one visible string the repoint changed, because the label IS the address.
		expect(uiSpecCopyRow("help menu — support label", SPEC_PATH).copy).toBe(
			DESTINATIONS.supportEmail,
		);
		expect(helpMenuSource()).toContain("{DESTINATIONS.supportEmail}");
	});

	test("the Help menu's items appear in the declared order", () => {
		const block = helpMenuSource();
		const ariaLabels = [...block.matchAll(/aria-label="([^"]*)"/g)].map((match) => match[1]);
		expect(ariaLabels).toEqual([...HELP_MENU_ARIA_LABELS]);
	});

	test("every anchor in the Help menu carries a non-empty href", () => {
		const hrefs = hrefValues(helpMenuSource());
		expect(hrefs.length).toBe(HELP_MENU_ARIA_LABELS.length);
		for (const href of hrefs) {
			expect(href.trim().length).toBeGreaterThan(0);
			// Non-empty is not enough on its own: a literal is non-empty too, and the point of
			// this file is that the destination comes from one place.
			expect(`${href} reads the authority: ${href.includes("DESTINATIONS")}`).toBe(
				`${href} reads the authority: true`,
			);
		}
	});

	test("the Telegram destination is intentionally absent, in the authority and in the menu", () => {
		// Explicit null, not an omitted key: an omitted key is indistinguishable from an
		// oversight, and this is a decision. Restoring it is one constant and one menu item.
		expect("telegramUrl" in DESTINATIONS).toBe(true);
		expect(DESTINATIONS.telegramUrl).toBeNull();

		// No component names a Telegram destination, and the menu's icon import went with the
		// item — an unused import is a lint failure, and a used one would mean a live row.
		//
		// `t.me/` carries its slash on purpose: bare `t.me` also matches `import.meta.env`,
		// which appears in this file for an unrelated reason and would fail this assertion
		// while proving nothing about Telegram.
		//
		// Both are reported as a computed marker rather than through `not.toContain`, which
		// prints the whole 1400-line component as its received value on failure.
		const sidebar = strippedSource(SIDEBAR);
		for (const absent of ["t.me/", "MessageCircle"]) {
			const state = sidebar.includes(absent) ? `${SIDEBAR} still names ${absent}` : "absent";
			expect(state).toBe("absent");
		}
	});

	test("the UI-SPEC's Telegram row records an absence rather than a placeholder copy", () => {
		// `uiSpecCopyRow` throws on a row with no quoted string. That is the correct outcome
		// here and is asserted rather than avoided: there is no shipped Telegram string, so a
		// row that resolved to one would be a placeholder implying a link nobody owns.
		expect(() => uiSpecCopyRow(TELEGRAM_ROW_KEY, SPEC_PATH)).toThrow(/carries no quoted copy/);

		// The row must still EXIST. Deleting it would erase the decision; the reader's other
		// failure mode ("no Copywriting Contract row") is what deletion would produce.
		expect(() => uiSpecCopyRow(TELEGRAM_ROW_KEY, SPEC_PATH)).not.toThrow(
			/no Copywriting Contract row/,
		);
	});
});

/**
 * The retired upstream domain's support address, absent from every shipped web source.
 *
 * The residual rebrand guard's C-4 lookbehind refuses at-sign positions and leaves an address at
 * the retired upstream domain to the DEFERRED X-6 substring fence alone. So a support address at
 * that domain can arrive in a render site and every rebrand guard stays green. The 06.1 upstream
 * sync did exactly that, at the hosted agent detail's contact_support CTA (06.1-UI-REVIEW Top
 * fix 1, fixed by quick 261002-dx9).
 *
 * This block is the witness that does not depend on the allowlist. Like the rest of the file it
 * reads source as TEXT and never imports it. Its walk is FLOORED: a mis-rooted walk reads nothing
 * and reports clean, which is a guard that fails open on the exact regression it exists to catch.
 */

const WEB_SRC = "apps/web/src";
const HOSTED_AGENT_DETAIL = "apps/web/src/hosted/agents/hosted-agent-detail.tsx";
const RETIRED_ADDRESS_DOMAIN = "@clawdi.ai";
/** 580 non-test files were walked at planning time (2026-10-02); the floor leaves headroom. */
const MINIMUM_WALKED_FILES = 400;

/** Every non-test file under apps/web/src, of any extension, repo-relative and sorted. */
function nonTestWebSources(): string[] {
	const out: string[] = [];
	const walk = (current: string) => {
		for (const entry of readdirSync(current)) {
			const full = join(current, entry);
			const st = statSync(full);
			if (st.isDirectory()) walk(full);
			else if (st.isFile() && !/\.test\.tsx?$/.test(entry)) out.push(relative(REPO_ROOT, full));
		}
	};
	walk(join(REPO_ROOT, WEB_SRC));
	return out.sort();
}

describe("06.1 UI review: retired upstream support address", () => {
	test("the walk is rooted, floored, and excludes test files", () => {
		const walked = nonTestWebSources();
		expect(walked.length).toBeGreaterThanOrEqual(MINIMUM_WALKED_FILES);
		expect(walked.includes(HOSTED_AGENT_DETAIL)).toBe(true);
		expect(walked.filter((path) => /\.test\.tsx?$/.test(path))).toEqual([]);
	});

	test("no non-test web source names an address at the retired upstream domain", () => {
		// Offending paths only, never contents: the computed-marker convention of this file.
		const offenders = nonTestWebSources().filter((path) =>
			readFileSync(join(REPO_ROOT, path), "utf8").toLowerCase().includes(RETIRED_ADDRESS_DOMAIN),
		);
		expect(offenders).toEqual([]);
	});

	test("the hosted agent's support CTA reads the authority and restates no destination", () => {
		const site = HOSTED_AGENT_DETAIL;
		expect(`${site}:${readSource(site).includes(PERMITTED_MAILTO)}`).toBe(`${site}:true`);
		expect(`${site}:${readSource(site).includes("DESTINATIONS")}`).toBe(`${site}:true`);
		expect({ site, restated: restatedDestinations(site) }).toEqual({ site, restated: [] });
	});
});
