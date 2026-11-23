import { describe, expect, test } from "bun:test";
import { fileURLToPath } from "node:url";
import type { CheckoutOperationResult } from "@/hosted/billing/billing-client";
import { formatUsdExact } from "@/hosted/billing/format";
import { uiSpecCopy, uiSpecRowQuotes } from "@/lib/ui-spec-contract";
import {
	PLAN_CHECKOUT_BACK_LABEL,
	PLAN_CHECKOUT_DESCRIPTION,
	PLAN_STARTED_CARD_DESCRIPTION,
	PLAN_STARTED_TOAST_TITLE,
	PLAN_STARTED_WALLET_DESCRIPTION,
	planCheckoutSubmitLabel,
	planCheckoutTitle,
	planChooseLabel,
	planOnlyCheckoutOutcome,
	planStripeDescription,
	planStripeSubmitLabel,
} from "./plan-checkout.logic";

/**
 * 06.3 D-04 (UI-SPEC L6, L7, L8) — the plan checkout dialog's pure logic. Every
 * expected string is read from the 06.3 UI-SPEC Copywriting Contract, never
 * duplicated here, so the shipped copy and the contract row cannot drift apart.
 */
const UI_SPEC_06_3 = fileURLToPath(
	new URL(
		"../../../../../../.planning/phases/06.3-align-access-and-billing-gating-with-upstream-clerk-managed/06.3-UI-SPEC.md",
		import.meta.url,
	),
);

const spec = (element: string) => uiSpecCopy(element, UI_SPEC_06_3);
const quotes = (element: string) => uiSpecRowQuotes(element, UI_SPEC_06_3);

const checkoutSession: Extract<CheckoutOperationResult, { flow_type: "checkout_session" }> = {
	flow_type: "checkout_session",
	funding_source: "stripe",
	action_url: null,
	checkout_url: "https://checkout.stripe.test/session",
	client_secret: "cs_test_secret",
};

const planOnlyActivation: Extract<
	CheckoutOperationResult,
	{ flow_type: "subscription_activation" }
> = {
	flow_type: "subscription_activation",
	funding_source: "wallet",
	checkout_url: "",
	subscription_id: "csub_plan_only",
	invoice_id: null,
	agent_id: null,
	deploy_request_id: null,
	deployment_id: null,
	deployment_name: null,
	metadata_generation: null,
	debited_usd: "29.00",
	balance_after_usd: "21.00",
	current_period_start: "2026-10-05T00:00:00Z",
	current_period_end: "2026-11-05T00:00:00Z",
	entitled_until: "2026-11-06T00:00:00Z",
};

describe("planOnlyCheckoutOutcome", () => {
	test("a checkout_session result maps to the checkout flow", () => {
		expect(planOnlyCheckoutOutcome(checkoutSession)).toEqual({
			flowType: "checkout",
			checkout: checkoutSession,
		});
	});

	test("an activation naming no agent maps to subscription_activation without throwing", () => {
		expect(planOnlyCheckoutOutcome(planOnlyActivation)).toEqual({
			flowType: "subscription_activation",
			currentPeriodEnd: "2026-11-05T00:00:00Z",
			entitledUntil: "2026-11-06T00:00:00Z",
		});
	});

	test("missing period fields map to null", () => {
		expect(
			planOnlyCheckoutOutcome({
				...planOnlyActivation,
				current_period_end: null,
				entitled_until: null,
			}),
		).toEqual({ flowType: "subscription_activation", currentPeriodEnd: null, entitledUntil: null });
	});
});

describe("planCheckoutSubmitLabel (L7 submit)", () => {
	test("card reads the L7 card submit row", () => {
		expect(planCheckoutSubmitLabel({ fundingSource: "stripe", walletDebitAmountUsd: null })).toBe(
			spec("L7 submit, card"),
		);
	});

	test("wallet without a quote reads the L7 wallet no-quote row", () => {
		expect(planCheckoutSubmitLabel({ fundingSource: "wallet", walletDebitAmountUsd: null })).toBe(
			spec("L7 submit, wallet, no quote yet"),
		);
	});

	test("wallet with a quote fills the L7 quoted template through formatUsdExact", () => {
		expect(
			planCheckoutSubmitLabel({ fundingSource: "wallet", walletDebitAmountUsd: "29.00" }),
		).toBe(spec("L7 submit, wallet, quoted").replace("{amount}", formatUsdExact("29.00")));
		expect(
			planCheckoutSubmitLabel({ fundingSource: "wallet", walletDebitAmountUsd: "29.00" }),
		).toBe("Pay $29.00 from Wallet");
	});
});

describe("plan titles and labels (L6, L7, L8)", () => {
	test("L7 title per plan", () => {
		const [basic, performance] = quotes("L7 title");
		expect(planCheckoutTitle("compute_basic")).toBe(basic as string);
		expect(planCheckoutTitle("compute_performance")).toBe(performance as string);
	});

	test("L6 choose label per plan", () => {
		expect(planChooseLabel("compute_basic")).toBe(spec("L6 Basic card"));
		expect(planChooseLabel("compute_performance")).toBe(spec("L6 Performance card"));
	});

	test("L8 Stripe submit label per plan", () => {
		const [basic, performance] = quotes("L8 Stripe submit");
		expect(planStripeSubmitLabel("compute_basic")).toBe(basic as string);
		expect(planStripeSubmitLabel("compute_performance")).toBe(performance as string);
	});

	test("L8 description joins the billing term label and the price", () => {
		expect(planStripeDescription({ billingTermMonths: 1, primaryPrice: "$29.00 / month" })).toBe(
			"Monthly · $29.00 / month",
		);
		expect(planStripeDescription({ billingTermMonths: 12, primaryPrice: "$290.00 / year" })).toBe(
			"Annual · $290.00 / year",
		);
	});
});

describe("plan checkout copy constants", () => {
	test("L7 description", () => {
		expect(PLAN_CHECKOUT_DESCRIPTION).toBe(spec("L7 description"));
	});

	test("L7 secondary", () => {
		expect(PLAN_CHECKOUT_BACK_LABEL).toBe(spec("L7 secondary"));
	});

	test("not-locked success toast, wallet", () => {
		const [title, description] = quotes("Not-locked success toast, wallet");
		expect(PLAN_STARTED_TOAST_TITLE).toBe(title as string);
		expect(PLAN_STARTED_WALLET_DESCRIPTION).toBe(description as string);
	});

	test("not-locked success toast, card", () => {
		const [title, description] = quotes("Not-locked success toast, card");
		expect(PLAN_STARTED_TOAST_TITLE).toBe(title as string);
		expect(PLAN_STARTED_CARD_DESCRIPTION).toBe(description as string);
	});

	test("no success copy claims the plan is active before payment settles", () => {
		for (const copy of [
			PLAN_STARTED_TOAST_TITLE,
			PLAN_STARTED_WALLET_DESCRIPTION,
			PLAN_STARTED_CARD_DESCRIPTION,
		]) {
			expect(copy.length).toBeGreaterThan(0);
			expect(copy).not.toContain("Subscribed");
			expect(copy).not.toContain("Plan active");
		}
	});
});
