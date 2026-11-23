import { describe, expect, test } from "bun:test";
import { fileURLToPath } from "node:url";
import { DESTINATIONS } from "@clawdi/shared";
import { renderToStaticMarkup } from "react-dom/server";
import {
	BillingLockBanner,
	type BillingLockBannerState,
	CHECK_AGAIN_LABEL,
	CONFIRMING_DESCRIPTION,
	CONFIRMING_TITLE,
	CONTACT_SUPPORT_LABEL,
	LOCK_BANNER_DESCRIPTION,
	LOCK_BANNER_TITLE,
	PLAN_CHECKOUT_CANCEL_COPY,
	STILL_CONFIRMING_DESCRIPTION,
	STILL_CONFIRMING_TITLE,
} from "@/hosted/billing/subscription/billing-lock-banner";
import { uiSpecCopy, uiSpecRowQuotes } from "@/lib/ui-spec-contract";

/**
 * 06.3 UI-SPEC L5 (lock banner) and L9 (confirming / still confirming) on
 * Settings → Compute. Every expected string is read from the 06.3 UI-SPEC
 * Copywriting Contract, never duplicated here.
 */
const UI_SPEC_06_3 = fileURLToPath(
	new URL(
		"../../../../../../.planning/phases/06.3-align-access-and-billing-gating-with-upstream-clerk-managed/06.3-UI-SPEC.md",
		import.meta.url,
	),
);

const spec = (element: string) => uiSpecCopy(element, UI_SPEC_06_3);
const quotes = (element: string) => uiSpecRowQuotes(element, UI_SPEC_06_3);

/** renderToStaticMarkup escapes quotes and ampersands; decode them so the
 * assertions compare prose with prose. */
function decode(markup: string): string {
	return markup.replaceAll("&#x27;", "'").replaceAll("&quot;", '"').replaceAll("&amp;", "&");
}

function render(state: BillingLockBannerState): string {
	return decode(renderToStaticMarkup(<BillingLockBanner state={state} onCheckAgain={() => {}} />));
}

/** Text of every <button> element in the markup. */
function buttonTexts(markup: string): string[] {
	return [...markup.matchAll(/<button\b[^>]*>([\s\S]*?)<\/button>/g)].map((match) =>
		(match[1] as string).replace(/<[^>]+>/g, "").trim(),
	);
}

/** Every element carrying data-slot="alert". */
function alertCount(markup: string): number {
	return markup.match(/data-slot="alert"/g)?.length ?? 0;
}

describe("BillingLockBanner — locked (L5)", () => {
	test("renders one default alert with the L5 title and description", () => {
		const markup = render("locked");
		expect(alertCount(markup)).toBe(1);
		expect(markup).toContain('data-variant="default"');
		expect(markup).toContain(spec("L5 banner title"));
		expect(markup).toContain(spec("L5 banner description"));
	});

	test("renders no button", () => {
		expect(buttonTexts(render("locked"))).toEqual([]);
	});
});

describe("BillingLockBanner — confirming (L9)", () => {
	test("is a polite status region", () => {
		const markup = render("confirming");
		expect(alertCount(markup)).toBe(1);
		expect(markup).toContain('role="status"');
		expect(markup).toContain('aria-live="polite"');
	});

	test("renders the L9 confirming title and description", () => {
		const markup = render("confirming");
		expect(markup).toContain(spec("L9 confirming title"));
		expect(markup).toContain(spec("L9 confirming description"));
	});

	test("renders no button", () => {
		expect(buttonTexts(render("confirming"))).toEqual([]);
	});
});

describe("BillingLockBanner — still confirming (L9)", () => {
	test("is a warning alert with role=alert", () => {
		const markup = render("still_confirming");
		expect(alertCount(markup)).toBe(1);
		expect(markup).toContain('data-variant="warning"');
		expect(markup).toContain('role="alert"');
	});

	test("renders the L9 still-confirming title and description", () => {
		const markup = render("still_confirming");
		expect(markup).toContain(spec("L9 still confirming title"));
		expect(markup).toContain(spec("L9 still confirming description"));
	});

	test("offers Check again as a button", () => {
		expect(buttonTexts(render("still_confirming"))).toEqual([CHECK_AGAIN_LABEL]);
		expect(CHECK_AGAIN_LABEL).toBe(quotes("L9 still confirming")[0] as string);
	});

	test("offers Contact support as a mailto link to the support address", () => {
		const markup = render("still_confirming");
		const link = /<a\b[^>]*href="([^"]*)"[^>]*>([\s\S]*?)<\/a>/.exec(markup);
		expect(link?.[1]).toBe(`mailto:${DESTINATIONS.supportEmail}`);
		expect((link?.[2] ?? "").replace(/<[^>]+>/g, "").trim()).toBe(CONTACT_SUPPORT_LABEL);
		expect(CONTACT_SUPPORT_LABEL).toBe(quotes("L9 still confirming")[1] as string);
	});
});

describe("copy constants equal their UI-SPEC rows", () => {
	test("L5 and L9 alert copy", () => {
		expect([
			LOCK_BANNER_TITLE,
			LOCK_BANNER_DESCRIPTION,
			CONFIRMING_TITLE,
			CONFIRMING_DESCRIPTION,
			STILL_CONFIRMING_TITLE,
			STILL_CONFIRMING_DESCRIPTION,
		]).toEqual([
			spec("L5 banner title"),
			spec("L5 banner description"),
			spec("L9 confirming title"),
			spec("L9 confirming description"),
			spec("L9 still confirming title"),
			spec("L9 still confirming description"),
		]);
	});

	test("PLAN_CHECKOUT_CANCEL_COPY equals the L10 cancel copy row", () => {
		expect(PLAN_CHECKOUT_CANCEL_COPY).toBe(spec("L10 cancel copy (`onCancelCopy`)"));
	});
});
