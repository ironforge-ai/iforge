import { describe, expect, test } from "bun:test";
import {
	BillingApiError,
	deploySubmissionErrorPresentation,
	isPaymentRequiredError,
	isRetryableError,
	normalizeBillingError,
} from "@/hosted/billing/errors";
import { deploymentMutationErrorMessage } from "@/hosted/deployment-failure";

/**
 * Fork-owned pin for audit INT-2: the web classifies hosted compute's 402
 * `payment_required` instead of falling through to generic billing copy.
 *
 * The hosted gateway answers a create, start or restart that no live compute
 * subscription pays for with a flat problem body carrying
 * `code: payment_required`. This file lives beside `errors.ts`, not in the
 * upstream-tracked `errors.test.ts`, so an upstream merge cannot drop it
 * (03.1 D-02).
 */

const GENERIC_CODE_SENTENCE = "The billing request could not be completed. Refresh and try again.";

function paymentRequired(detail: string): BillingApiError {
	return new BillingApiError(402, detail, {
		type: "https://api.iforge.dev/problems/payment_required",
		title: "Payment required",
		status: 402,
		detail,
		code: "payment_required",
	});
}

const START_REFUSED = paymentRequired(
	"This agent's compute subscription is not paid. Subscribe, top up or update the payment method to start it.",
);
const CREATE_REFUSED = paymentRequired(
	"Creating an agent needs a paid compute subscription for this plan that no other agent uses.",
);

describe("INT-2: 402 payment_required", () => {
	test("is classified from the flat problem body", () => {
		expect(isPaymentRequiredError(START_REFUSED)).toBe(true);
		expect(isPaymentRequiredError(new BillingApiError(402, "Payment Required"))).toBe(false);
		expect(
			isPaymentRequiredError(
				new BillingApiError(403, "", { code: "payment_required", status: 403 }),
			),
		).toBe(false);
	});

	test("is not retried automatically", () => {
		expect(isRetryableError(START_REFUSED)).toBe(false);
	});

	test("normalizes to subscription copy, not the generic code sentence", () => {
		const message = normalizeBillingError(START_REFUSED);
		expect(message).not.toBe(GENERIC_CODE_SENTENCE);
		expect(message).toContain("active compute subscription");
		expect(message).toContain("Choose a subscription");
	});

	test("a refused start or restart points at the agent's subscription", () => {
		expect(deploymentMutationErrorMessage(START_REFUSED)).toBe(
			"This agent needs an active subscription to start. Open Agent settings and choose a subscription. Your saved data is kept.",
		);
	});

	test("a refused create says creation didn’t start and asks for a subscription", () => {
		for (const context of ["included_creation", "subscription_assignment"] as const) {
			const presentation = deploySubmissionErrorPresentation(CREATE_REFUSED, context);
			expect(presentation.title).toContain("didn’t start");
			expect(presentation.description).toContain("paid compute subscription");
			expect(presentation.description).toContain("Choose a subscription");
		}
	});

	test("insufficient wallet balance keeps its own copy", () => {
		const balance = new BillingApiError(402, "", {
			detail: { code: "insufficient_wallet_balance" },
		});
		expect(isPaymentRequiredError(balance)).toBe(false);
		expect(normalizeBillingError(balance)).toContain("Wallet balance is too low");
	});
});
