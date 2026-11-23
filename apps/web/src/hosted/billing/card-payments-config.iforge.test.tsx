import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import {
	CARD_PAYMENTS_UNCONFIGURED_DESCRIPTION,
	CARD_PAYMENTS_UNCONFIGURED_TITLE,
	CardPaymentsUnconfiguredNotice,
	isCardPaymentsConfigured,
} from "@/hosted/billing/card-payments-config";
import { checkoutUiModeForPublishableKey } from "@/hosted/billing/components/stripe-checkout.logic";
import { uiSpecCopy } from "@/lib/ui-spec-contract";

/**
 * Fork-owned pins for G-06.3-3 (D-04): a hosted build with no
 * VITE_STRIPE_PUBLISHABLE_KEY says card payments are not configured. It never
 * creates a Wallet top-up Checkout Session the browser cannot complete, and it
 * never offers a retry that cannot succeed.
 *
 * Expected copy is read from the 06.3 UI-SPEC Copywriting Contract, never
 * duplicated here. Source pins read the shipped upstream dialogs with comments
 * stripped, so a comment cannot satisfy a pin, and every extracted slice is
 * asserted non-empty and unique, so a pin that matches nothing fails.
 *
 * WR-04 (06.3-20): one predicate, isCardPaymentsConfigured, decides whether
 * card payments are configured, for the checkout UI mode and for the payment
 * dialog's loading effect alike, so a blank or whitespace-only key falls back to
 * Stripe's hosted redirect instead of dead-ending on the not-configured notice.
 *
 * This file lives beside the upstream billing code, not in an upstream-tracked
 * test, so an upstream merge cannot drop it (03.1 D-02).
 */

const UI_SPEC_06_3 = fileURLToPath(
	new URL(
		"../../../../../.planning/phases/06.3-align-access-and-billing-gating-with-upstream-clerk-managed/06.3-UI-SPEC.md",
		import.meta.url,
	),
);

const TITLE_ROW = "Card payments unconfigured title (L8, top-up)";
const DESCRIPTION_ROW = "Card payments unconfigured description (L8, top-up)";

const spec = (element: string) => uiSpecCopy(element, UI_SPEC_06_3);

/** renderToStaticMarkup escapes quotes and ampersands; decode them so the
 * assertions compare prose with prose. */
function decode(markup: string): string {
	return markup.replaceAll("&#x27;", "'").replaceAll("&quot;", '"').replaceAll("&amp;", "&");
}

/** Source text with block and line comments removed. Neither upstream dialog
 * carries `//` inside a string literal, so the line strip is safe here. */
function strippedSource(relative: string): string {
	const code = readFileSync(new URL(relative, import.meta.url), "utf8");
	return code
		.replace(/\/\*[\s\S]*?\*\//g, "")
		.split("\n")
		.map((line) => {
			const comment = line.indexOf("//");
			return comment === -1 ? line : line.slice(0, comment);
		})
		.join("\n");
}

function escapeRegExp(text: string): string {
	return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Index of the one occurrence of `needle`; fails unless it occurs exactly once. */
function uniqueIndex(code: string, needle: string): number {
	expect(code.split(needle)).toHaveLength(2);
	return code.indexOf(needle);
}

/** From the opening bracket at `open` to its matching close, inclusive. */
function balanced(code: string, open: number, pair: "()" | "{}"): string {
	const [opener, closer] = pair;
	expect(code[open]).toBe(opener);
	let depth = 0;
	for (let i = open; i < code.length; i++) {
		if (code[i] === opener) depth++;
		else if (code[i] === closer) {
			depth--;
			if (depth === 0) return code.slice(open, i + 1);
		}
	}
	throw new Error("unbalanced brackets");
}

/** The name bound by the one `const <name> = isCardPaymentsConfigured(<arg>)`. */
function configuredFlag(code: string, arg: string): string {
	const pattern = new RegExp(
		`const\\s+(\\w+)\\s*=\\s*isCardPaymentsConfigured\\(\\s*${escapeRegExp(arg)}\\s*\\)`,
		"g",
	);
	const matches = [...code.matchAll(pattern)];
	expect(matches).toHaveLength(1);
	const name = matches[0]?.[1] ?? "";
	expect(name.length).toBeGreaterThan(0);
	return name;
}

describe("isCardPaymentsConfigured (G-06.3-3)", () => {
	test("an absent key is not configured", () => {
		expect(isCardPaymentsConfigured(undefined)).toBe(false);
	});

	test("an empty key is not configured", () => {
		expect(isCardPaymentsConfigured("")).toBe(false);
	});

	test("a whitespace-only key is not configured", () => {
		expect(isCardPaymentsConfigured("   ")).toBe(false);
	});

	test("a non-blank key is configured", () => {
		expect(isCardPaymentsConfigured("pk_test_placeholder")).toBe(true);
	});
});

describe("checkoutUiModeForPublishableKey uses the one card-payments predicate (WR-04)", () => {
	const cases: Array<[string | undefined, "custom" | "hosted"]> = [
		[undefined, "hosted"],
		["", "hosted"],
		["   ", "hosted"],
		["\t\n ", "hosted"],
		["pk_test_browser", "custom"],
	];

	for (const [input, mode] of cases) {
		test(`${JSON.stringify(input) ?? "undefined"} answers ${mode}`, () => {
			expect(checkoutUiModeForPublishableKey(input)).toBe(mode);
		});
	}

	test("every input agrees with isCardPaymentsConfigured", () => {
		for (const [input] of cases) {
			expect(checkoutUiModeForPublishableKey(input)).toBe(
				isCardPaymentsConfigured(input) ? "custom" : "hosted",
			);
		}
	});

	test("its body calls isCardPaymentsConfigured(", () => {
		const code = strippedSource("./components/stripe-checkout.logic.ts");
		const start = uniqueIndex(code, "function checkoutUiModeForPublishableKey(");
		const paramsAt = start + "function checkoutUiModeForPublishableKey".length;
		const params = balanced(code, paramsAt, "()");
		const body = balanced(code, code.indexOf("{", paramsAt + params.length), "{}");
		expect(body.trim().length).toBeGreaterThan(2);
		expect(body).toContain("isCardPaymentsConfigured(");
	});
});

describe("card payments unconfigured copy (06.3 UI-SPEC, G-06.3-3)", () => {
	test("the title constant is the UI-SPEC title row", () => {
		expect(CARD_PAYMENTS_UNCONFIGURED_TITLE).toBe(spec(TITLE_ROW));
	});

	test("the description constant is the UI-SPEC description row", () => {
		expect(CARD_PAYMENTS_UNCONFIGURED_DESCRIPTION).toBe(spec(DESCRIPTION_ROW));
	});
});

describe("CardPaymentsUnconfiguredNotice (G-06.3-3)", () => {
	const markup = () => decode(renderToStaticMarkup(<CardPaymentsUnconfiguredNotice />));

	test("renders the UI-SPEC title and description", () => {
		const html = markup();
		expect(html).toContain(spec(TITLE_ROW));
		expect(html).toContain(spec(DESCRIPTION_ROW));
	});

	test("is marked as hosted UI", () => {
		expect(markup()).toContain('data-hosted="true"');
	});

	test("offers no button, because no action can fix a missing key", () => {
		expect(markup()).not.toMatch(/<button\b/);
	});
});

describe("TopUpDialog keyless guard (G-06.3-3)", () => {
	const code = strippedSource("./wallet/top-up-dialog.tsx");

	/** onContinue, from its declaration to the next function declaration. */
	function onContinueSlice(): string {
		const start = uniqueIndex(code, "async function onContinue(");
		const rest = code.slice(start + "async function onContinue(".length);
		const next = rest.search(/\bfunction\s+\w+\s*\(/);
		expect(next).toBeGreaterThan(0);
		const slice = code.slice(start, start + "async function onContinue(".length + next);
		expect(slice.trim().length).toBeGreaterThan("async function onContinue(".length);
		return slice;
	}

	test("the flag is computed from isCardPaymentsConfigured(env.VITE_STRIPE_PUBLISHABLE_KEY)", () => {
		configuredFlag(code, "env.VITE_STRIPE_PUBLISHABLE_KEY");
	});

	test("onContinue returns early on the flag before minting a key or calling the top-up endpoint", () => {
		const flag = configuredFlag(code, "env.VITE_STRIPE_PUBLISHABLE_KEY");
		const slice = onContinueSlice();
		const guard = new RegExp(`if\\s*\\(\\s*!${escapeRegExp(flag)}\\s*\\)\\s*return\\s*;`, "g");
		const guards = [...slice.matchAll(guard)];
		expect(guards).toHaveLength(1);
		const guardAt = guards[0]?.index ?? -1;
		const keyAt = uniqueIndex(slice, "newIdempotencyKey(");
		const executeAt = uniqueIndex(slice, "topUp.execute(");
		expect(guardAt).toBeGreaterThanOrEqual(0);
		expect(guardAt).toBeLessThan(keyAt);
		expect(guardAt).toBeLessThan(executeAt);
	});

	test("the Continue button is disabled when card payments are not configured", () => {
		const flag = configuredFlag(code, "env.VITE_STRIPE_PUBLISHABLE_KEY");
		const button = uniqueIndex(code, "runAction(onContinue)");
		const disabledAt = code.indexOf("disabled={", button);
		expect(disabledAt).toBeGreaterThan(button);
		const expression = balanced(code, disabledAt + "disabled=".length, "{}");
		expect(expression).toMatch(new RegExp(`!${escapeRegExp(flag)}\\b`));
	});

	test("the amount form renders the notice when card payments are not configured", () => {
		const flag = configuredFlag(code, "env.VITE_STRIPE_PUBLISHABLE_KEY");
		const formAt = uniqueIndex(code, '<div className="space-y-4">');
		const noticeAt = uniqueIndex(code, "<CardPaymentsUnconfiguredNotice");
		const buttonAt = uniqueIndex(code, "runAction(onContinue)");
		expect(noticeAt).toBeGreaterThan(formAt);
		expect(noticeAt).toBeLessThan(buttonAt);
		const before = code.slice(formAt, noticeAt);
		expect(before).toMatch(new RegExp(`!${escapeRegExp(flag)}\\s*&&\\s*$`));
	});
});

describe("StripeCheckoutDialog keyless state (G-06.3-3)", () => {
	const code = strippedSource("./components/stripe-checkout-dialog.tsx");

	/** The one useEffect that loads Stripe.js, from `useEffect(` to its closing paren. */
	function loadingEffect(): string {
		const loadAt = uniqueIndex(code, "getStripe(key)");
		const effectAt = code.lastIndexOf("useEffect(", loadAt);
		expect(effectAt).toBeGreaterThanOrEqual(0);
		const effect = balanced(code, effectAt + "useEffect".length, "()");
		expect(effect).toContain("getStripe(key)");
		return effect;
	}

	test("the flag is computed from isCardPaymentsConfigured(key)", () => {
		configuredFlag(code, "key");
	});

	test("the keyless branch of the loading effect sets no error state and no message", () => {
		// WR-04: the guard negates the flag computed by isCardPaymentsConfigured(key).
		// It may add `|| !key` to narrow the key for getStripe; a guard that tests
		// only the raw key lets a whitespace-only key load Stripe.js, and fails here.
		const flag = configuredFlag(code, "key");
		const effect = loadingEffect();
		const loadAt = uniqueIndex(effect, "getStripe(key)");
		const beforeLoad = effect.slice(0, loadAt);
		const keyGuards = [...beforeLoad.matchAll(/if\s*\(([^()]*)\)\s*return\s*;/g)].filter((guard) =>
			new RegExp(`\\b(?:key|${escapeRegExp(flag)})\\b`).test(guard[1] ?? ""),
		);
		expect(keyGuards).toHaveLength(1);
		const condition = keyGuards[0]?.[1] ?? "";
		expect(condition).toMatch(
			new RegExp(`^\\s*!${escapeRegExp(flag)}\\s*(?:\\|\\|\\s*!key\\s*)?$`),
		);
		const branch = keyGuards[0]?.[0] ?? "";
		expect(branch.trim().length).toBeGreaterThan(0);
		expect(branch).not.toContain("setState(");
		expect(branch).not.toContain("setMessage(");
		const depsAt = effect.lastIndexOf("[");
		expect(depsAt).toBeGreaterThan(loadAt);
		const deps = effect.slice(depsAt, effect.indexOf("]", depsAt) + 1);
		expect(deps).toMatch(new RegExp(`\\b${escapeRegExp(flag)}\\b`));
	});

	test("the notice renders once, chosen by the flag, ahead of the payment form and the Retry", () => {
		const noticeAt = uniqueIndex(code, "<CardPaymentsUnconfiguredNotice");
		const flag = configuredFlag(code, "key");
		const providerAt = uniqueIndex(code, "<CheckoutElementsProvider");
		const retryAt = uniqueIndex(code, "Retry payment form");
		expect(noticeAt).toBeLessThan(providerAt);
		expect(noticeAt).toBeLessThan(retryAt);
		const before = code.slice(code.lastIndexOf("{", noticeAt), noticeAt);
		expect(before).toMatch(new RegExp(`^\\{\\s*!${escapeRegExp(flag)}\\s*(?:\\?|&&)\\s*\\(?\\s*$`));
	});

	test("real Stripe.js and Elements load failures keep the load-failure copy and the Retry", () => {
		const failure = uiSpecCopy("Secure form failed to load (L8)", UI_SPEC_06_3);
		const handlerAt = uniqueIndex(code, "const handleProviderLoadError =");
		const handler = balanced(code, code.indexOf("(", handlerAt), "()");
		expect(handler).toContain(failure);
		const effect = loadingEffect();
		const catchAt = uniqueIndex(effect, "catch {");
		const catchBlock = balanced(effect, catchAt + "catch ".length, "{}");
		expect(catchBlock).toContain('setState("error")');
		expect(catchBlock).toContain(failure);
		uniqueIndex(code, "Retry payment form");
	});
});

describe("env.ts VITE_STRIPE_PUBLISHABLE_KEY comment (G-06.3-3)", () => {
	const lines = readFileSync(new URL("../../lib/env.ts", import.meta.url), "utf8").split("\n");

	/** The contiguous `//` comment lines directly above the schema line. */
	function commentAbove(): string {
		const schemaAt = lines.findIndex((line) =>
			line.includes("VITE_STRIPE_PUBLISHABLE_KEY: z.string().min(1).optional()"),
		);
		expect(schemaAt).toBeGreaterThan(0);
		const comment: string[] = [];
		for (let i = schemaAt - 1; i >= 0 && (lines[i] ?? "").trim().startsWith("//"); i--) {
			comment.unshift((lines[i] ?? "").trim().replace(/^\/\/\s?/, ""));
		}
		expect(comment.length).toBeGreaterThan(0);
		return comment.join(" ");
	}

	test("describes the hosted redirect fallback for card subscriptions", () => {
		expect(commentAbove()).toMatch(/hosted redirect/i);
		expect(commentAbove()).toMatch(/subscription/i);
	});

	test("describes the top-up dialog's not-configured state", () => {
		expect(commentAbove()).toMatch(/top-up/i);
		expect(commentAbove()).toContain(spec(TITLE_ROW));
	});

	test("no longer describes PaymentIntent confirmation", () => {
		expect(commentAbove()).not.toMatch(/PaymentIntent/);
	});
});
