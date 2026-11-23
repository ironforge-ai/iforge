import type { CheckoutOperationResult } from "@/hosted/billing/billing-client";
import type { ComputePlanSlug } from "@/hosted/billing/contracts";
import { billingTermLabel, formatUsdExact } from "@/hosted/billing/format";
import type { SubscriptionFundingSource } from "@/hosted/billing/subscription/subscription-create-adapter";
import { computeTierLabel } from "@/hosted/billing/subscription/subscription-utils";

/**
 * iforge (06.3 D-04, UI-SPEC L6/L7/L8): pure logic and copy for the plan
 * checkout dialog — buy a plan without deploying anything. Every string below
 * is a 06.3-UI-SPEC Copywriting Contract row; plan-checkout.logic.test.ts reads
 * both sides.
 */

export type PlanOnlyCheckoutOutcome =
	| {
			flowType: "checkout";
			checkout: Extract<CheckoutOperationResult, { flow_type: "checkout_session" }>;
	  }
	| {
			flowType: "subscription_activation";
			currentPeriodEnd: string | null;
			entitledUntil: string | null;
	  };

/** UI-SPEC "L7 description". */
export const PLAN_CHECKOUT_DESCRIPTION =
	"Pay by card or from your Wallet. You can assign this plan to an agent when you deploy.";
/** UI-SPEC "L7 secondary". */
export const PLAN_CHECKOUT_BACK_LABEL = "Back to plans";
/** UI-SPEC "Not-locked success toast, wallet" / "…, card" title. Plan 06.3-13 fires it. */
export const PLAN_STARTED_TOAST_TITLE = "Subscription started";
/** UI-SPEC "Not-locked success toast, wallet" description. */
export const PLAN_STARTED_WALLET_DESCRIPTION = "Your new plan is ready to assign to an agent.";
/**
 * UI-SPEC "Not-locked success toast, card" description. Card activation is
 * asynchronous, so this never claims the plan is active (RESEARCH Pitfall 12).
 */
export const PLAN_STARTED_CARD_DESCRIPTION =
	"Card payment was confirmed. Your plan appears here once payment is processed.";

/**
 * Mirrors `subscriptionCreateOutcome` without a target: a plan-only activation
 * names no agent (deployment_id and deploy_request_id are both null), which is
 * the expected result here rather than an error.
 */
export function planOnlyCheckoutOutcome(result: CheckoutOperationResult): PlanOnlyCheckoutOutcome {
	if (result.flow_type !== "subscription_activation") {
		return { flowType: "checkout", checkout: result };
	}
	return {
		flowType: "subscription_activation",
		currentPeriodEnd: result.current_period_end ?? null,
		entitledUntil: result.entitled_until ?? null,
	};
}

/** UI-SPEC "L7 submit, card" / "L7 submit, wallet, no quote yet" / "L7 submit, wallet, quoted". */
export function planCheckoutSubmitLabel({
	fundingSource,
	walletDebitAmountUsd,
}: {
	fundingSource: SubscriptionFundingSource;
	walletDebitAmountUsd: string | null;
}): string {
	if (fundingSource !== "wallet") return "Continue to card checkout";
	if (walletDebitAmountUsd === null) return "Review wallet quote";
	return `Pay ${formatUsdExact(walletDebitAmountUsd)} from Wallet`;
}

/** UI-SPEC "L7 title" (also the L8 title). */
export function planCheckoutTitle(planSlug: ComputePlanSlug): string {
	return `Subscribe to Compute ${computeTierLabel(planSlug)}`;
}

/** UI-SPEC "L6 Basic card" / "L6 Performance card". */
export function planChooseLabel(planSlug: ComputePlanSlug): string {
	return `Choose ${computeTierLabel(planSlug)}`;
}

/** UI-SPEC "L8 Stripe submit". */
export function planStripeSubmitLabel(planSlug: ComputePlanSlug): string {
	return `Subscribe to ${computeTierLabel(planSlug)}`;
}

/** UI-SPEC "L8 description": "{Billing term label} · {price}". */
export function planStripeDescription({
	billingTermMonths,
	primaryPrice,
}: {
	billingTermMonths: number;
	primaryPrice: string;
}): string {
	return `${billingTermLabel(billingTermMonths)} · ${primaryPrice}`;
}
