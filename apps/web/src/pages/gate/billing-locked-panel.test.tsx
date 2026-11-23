import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { uiSpecCopy } from "@/lib/ui-spec-contract";
import {
	BillingLockedPanelView,
	createPlanArrivalTracker,
	shouldOpenPlanOnArrival,
} from "@/pages/gate/billing-locked-panel";

/**
 * 06.3 L2 — the billing locked panel. Every expected string is read from the
 * 06.3 UI-SPEC Copywriting Contract, never duplicated here, so the shipped copy
 * and the contract row cannot drift apart unnoticed.
 */
const UI_SPEC_06_3 = fileURLToPath(
	new URL(
		"../../../../../.planning/phases/06.3-align-access-and-billing-gating-with-upstream-clerk-managed/06.3-UI-SPEC.md",
		import.meta.url,
	),
);

const spec = (element: string) => uiSpecCopy(element, UI_SPEC_06_3);

/** renderToStaticMarkup escapes quotes and ampersands; decode them so the
 * assertions compare prose with prose. */
function decode(markup: string): string {
	return markup.replaceAll("&#x27;", "'").replaceAll("&quot;", '"').replaceAll("&amp;", "&");
}

function render(hosted: boolean): string {
	return decode(
		renderToStaticMarkup(<BillingLockedPanelView hosted={hosted} onChoosePlan={() => {}} />),
	);
}

/** Text of every <button> element in the markup. */
function buttonTexts(markup: string): string[] {
	return [...markup.matchAll(/<button\b[^>]*>([\s\S]*?)<\/button>/g)].map((match) =>
		(match[1] as string).replace(/<[^>]+>/g, "").trim(),
	);
}

describe("BillingLockedPanelView — hosted build (L2)", () => {
	test("renders the hosted title", () => {
		expect(render(true)).toContain(spec("L2 title (hosted)"));
	});

	test("renders the hosted description", () => {
		expect(render(true)).toContain(spec("L2 description (hosted)"));
	});

	test("renders exactly one button, labelled with the Choose a plan CTA", () => {
		expect(buttonTexts(render(true))).toEqual([spec("L2 locked panel (hosted)")]);
	});
});

describe("BillingLockedPanelView — OSS build (L2)", () => {
	test("renders the OSS title", () => {
		expect(render(false)).toContain(spec("L2 title (OSS build)"));
	});

	test("renders the OSS description", () => {
		expect(render(false)).toContain(spec("L2 description (OSS build)"));
	});

	test("renders no button and none of the hosted copy", () => {
		const markup = render(false);
		expect(markup).not.toContain("<button");
		expect(markup).not.toContain(spec("L2 title (hosted)"));
	});
});

describe("shouldOpenPlanOnArrival — land on Choose a plan once (L3)", () => {
	const arrival = { hosted: true, isBrowser: true, alreadyHandled: false, settings: undefined };

	test("opens on the first hosted browser arrival with no settings param", () => {
		expect(shouldOpenPlanOnArrival(arrival)).toBe(true);
	});

	test("never opens in the OSS build (L3.4)", () => {
		expect(shouldOpenPlanOnArrival({ ...arrival, hosted: false })).toBe(false);
	});

	test("never opens during SSR (L3.2)", () => {
		expect(shouldOpenPlanOnArrival({ ...arrival, isBrowser: false })).toBe(false);
	});

	test("opens only once per document load (L3.2)", () => {
		expect(shouldOpenPlanOnArrival({ ...arrival, alreadyHandled: true })).toBe(false);
	});

	test("leaves an existing settings param untouched (L3.3)", () => {
		expect(shouldOpenPlanOnArrival({ ...arrival, settings: "billing-wallet" })).toBe(false);
		expect(shouldOpenPlanOnArrival({ ...arrival, settings: "billing-plan" })).toBe(false);
	});
});

describe("createPlanArrivalTracker — once per identity, not per document (L3, G-06.3-1)", () => {
	test("a fresh tracker has handled no identity", () => {
		expect(createPlanArrivalTracker().handled("A")).toBe(false);
	});

	test("marking an identity handles it and no other", () => {
		const tracker = createPlanArrivalTracker();
		tracker.mark("A");
		expect(tracker.handled("A")).toBe(true);
		expect(tracker.handled("B")).toBe(false);
	});

	test("it tracks the latest identity only: a second locked identity is handled afresh", () => {
		const tracker = createPlanArrivalTracker();
		tracker.mark("A");
		tracker.mark("B");
		expect(tracker.handled("B")).toBe(true);
		expect(tracker.handled("A")).toBe(false);
	});

	test("a null identity (desktop build) starts unhandled and can be marked", () => {
		const tracker = createPlanArrivalTracker();
		expect(tracker.handled(null)).toBe(false);
		tracker.mark(null);
		expect(tracker.handled(null)).toBe(true);
		expect(tracker.handled("A")).toBe(false);
	});
});

/**
 * Source pins for the BillingLockedPanel wiring. Comments are stripped so a
 * comment cannot satisfy a pin, and every extracted slice is asserted
 * non-empty and unique, so a pin that matches nothing fails (the same helpers
 * as billing-lock-shell.iforge.test.ts).
 */
function stripComments(code: string): string {
	return code
		.replace(/\/\*[\s\S]*?\*\//g, "")
		.split("\n")
		.map((line) => {
			const comment = line.indexOf("//");
			return comment === -1 ? line : line.slice(0, comment);
		})
		.join("\n");
}

/** The one occurrence of `marker` in `code`; fails unless it is unique. */
function uniqueIndex(code: string, marker: string): number {
	const first = code.indexOf(marker);
	expect(first).toBeGreaterThanOrEqual(0);
	expect(code.indexOf(marker, first + 1)).toBe(-1);
	return first;
}

/** From the opening `(` at `open` to its matching `)`, inclusive. */
function balancedCall(code: string, open: number): string {
	expect(code[open]).toBe("(");
	let depth = 0;
	for (let i = open; i < code.length; i++) {
		if (code[i] === "(") depth++;
		else if (code[i] === ")") {
			depth--;
			if (depth === 0) return code.slice(open, i + 1);
		}
	}
	throw new Error("unbalanced call");
}

const panelSource = stripComments(
	readFileSync(new URL("./billing-locked-panel.tsx", import.meta.url), "utf8"),
);

/** The BillingLockedPanel component, from its declaration to the end of the module. */
function panelComponent(): string {
	const slice = panelSource.slice(uniqueIndex(panelSource, "export function BillingLockedPanel()"));
	expect(slice.length).toBeGreaterThan(0);
	return slice;
}

/** The component's one `useLayoutEffect(...)` call. */
function arrivalEffect(): string {
	const component = panelComponent();
	const marker = "useLayoutEffect(";
	const call = balancedCall(component, uniqueIndex(component, marker) + marker.length - 1);
	expect(call.length).toBeGreaterThan(2);
	return call;
}

describe("BillingLockedPanel arrival wiring (L3, G-06.3-1)", () => {
	test("reads authIdentity from the /_protected/_dashboard route context", () => {
		expect(panelComponent()).toMatch(
			/const identity = useRouteContext\(\{\s*from: "\/_protected\/_dashboard",\s*select: \(context\) => context\.authIdentity \?\? null,?\s*\}\)/,
		);
	});

	test("the arrival effect asks the tracker whether this identity was handled", () => {
		expect(arrivalEffect()).toMatch(/alreadyHandled: planArrival\.handled\(identity\)/);
	});

	test("the open path marks this identity before opening Settings → Compute", () => {
		expect(arrivalEffect()).toMatch(/planArrival\.mark\(identity\);\s*openPlan\(\);/);
	});

	test("the settings-present path marks this identity", () => {
		expect(arrivalEffect()).toMatch(/if \(settings !== undefined\) planArrival\.mark\(identity\);/);
	});

	test("the effect re-runs when the identity changes", () => {
		expect(arrivalEffect()).toMatch(/\[[^\]]*\bidentity\b[^\]]*\]\s*\)$/);
	});

	test("the module holds no once-per-document boolean", () => {
		expect(panelSource).not.toMatch(/^let /m);
	});
});

describe("OSS-safe sources (T-06.3-02-04)", () => {
	for (const file of ["../../lib/billing-lock.tsx", "./billing-locked-panel.tsx"]) {
		test(`${file} imports nothing from @/hosted/`, () => {
			const src = readFileSync(new URL(file, import.meta.url), "utf8");
			expect(src.length).toBeGreaterThan(0);
			expect(src).not.toContain("@/hosted/");
		});
	}
});
