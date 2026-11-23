import { describe, expect, test } from "bun:test";
import type { CheckoutOperationResult } from "@/hosted/billing/billing-client";
import type { ComputeSubscriptionQuoteResponse, DeployRequest } from "@/hosted/billing/contracts";
import {
	resolveSubscriptionSource,
	type SubscriptionCreateRequestView,
	subscriptionCreateOutcome,
	subscriptionCreateQuoteRequest,
	subscriptionCreateQuoteView,
	subscriptionCreateRequest,
} from "./subscription-create-adapter";

const deployConfig: DeployRequest = {
	compute_plan_slug: "compute_performance",
	runtime: "openclaw",
	ai_provider_auth_kind: "managed",
};

const walletQuote: ComputeSubscriptionQuoteResponse = {
	plan_slug: "compute_performance",
	billing_term_months: 12,
	funding_source: "wallet",
	currency: "usd",
	term_price_cents: 18_000,
	preview_invoice_id: "upcoming_in_annual",
	expires_at: "2026-07-16T00:05:00Z",
	debit_amount_usd: "180",
	balance_before_usd: "200.00025",
	balance_after_usd: "20.00025",
};

function createRequest(
	overrides: Partial<SubscriptionCreateRequestView> = {},
): SubscriptionCreateRequestView {
	const selection = {
		planSlug: "compute_performance" as const,
		billingTermMonths: 12 as const,
		fundingSource: "wallet" as const,
	};
	return {
		selection,
		subscriptionSelection: { mode: "new" },
		target: { kind: "new_deployment", deployConfig },
		uiMode: "custom",
		idempotencyKey: "subscription-create-test",
		quote: subscriptionCreateQuoteView(selection, walletQuote),
		...overrides,
	};
}

describe("subscription creation adapter", () => {
	test("selects the sole new-subscription source only after inventory resolves", () => {
		expect(
			resolveSubscriptionSource({
				selected: null,
				includedAvailable: undefined,
				reusableSubscriptions: [],
			}),
		).toBeNull();
		expect(
			resolveSubscriptionSource({
				selected: null,
				includedAvailable: false,
				reusableSubscriptions: undefined,
			}),
		).toBeNull();
		expect(
			resolveSubscriptionSource({
				selected: null,
				includedAvailable: false,
				reusableSubscriptions: [],
			}),
		).toEqual({ mode: "new" });
		expect(
			resolveSubscriptionSource({
				selected: { mode: "included" },
				includedAvailable: false,
				reusableSubscriptions: [],
			}),
		).toEqual({ mode: "new" });
		expect(
			resolveSubscriptionSource({
				selected: null,
				includedAvailable: true,
				reusableSubscriptions: [],
			}),
		).toBeNull();
	});

	test("presents the exact annual wallet quote and post-debit balance", () => {
		const selection = createRequest().selection;
		expect(subscriptionCreateQuoteRequest(selection)).toEqual({
			plan_slug: "compute_performance",
			billing_term_months: 12,
			funding_source: "wallet",
		});
		expect(subscriptionCreateQuoteView(selection, walletQuote)).toEqual({
			selection,
			termPriceCents: 18_000,
			currency: "usd",
			previewId: "upcoming_in_annual",
			expiresAt: "2026-07-16T00:05:00Z",
			serverQuote: walletQuote,
			walletDebit: {
				balanceBeforeUsd: "200.00025",
				debitAmountUsd: "180",
				balanceAfterUsd: "20.00025",
			},
		});
	});

	test("posts both rails to unified checkout and carries the exact wallet quote", () => {
		expect(subscriptionCreateRequest(createRequest())).toEqual({
			idempotencyKey: "subscription-create-test",
			body: {
				plan_slug: "compute_performance",
				billing_term_months: 12,
				funding_source: "wallet",
				ui_mode: "custom",
				deploy_config: {
					...deployConfig,
					deploy_request_id: "subscription-create-test",
				},
				quote: walletQuote,
				subscription_selection: { mode: "new" },
			},
		});

		// The hosted billing service answers 503 to any checkout naming
		// `upgrade_deployment_id`, so no create target can build one.
		createRequest({
			// @ts-expect-error the terminal-fallback upgrade target was removed.
			target: { kind: "terminal_fallback", deploymentId: "hdep_fallback" },
		});

		expect(
			subscriptionCreateRequest(
				createRequest({
					selection: {
						planSlug: "compute_performance",
						billingTermMonths: 12,
						fundingSource: "wallet",
					},
					subscriptionSelection: { mode: "existing", subscription_id: "csub_reusable" },
					quote: null,
				}),
			),
		).toMatchObject({
			body: {
				subscription_selection: { mode: "existing", subscription_id: "csub_reusable" },
			},
		});
	});

	// iforge (06.3 D-04): a plan-only checkout names neither a deploy config nor an
	// upgrade target, so hosted accepts it with compute unset.
	test("a plan_only target posts a body with no deploy or upgrade target", () => {
		const result = subscriptionCreateRequest(
			createRequest({
				target: { kind: "plan_only" },
				idempotencyKey: "subscription-plan-only-test",
			}),
		);
		expect(result).toEqual({
			idempotencyKey: "subscription-plan-only-test",
			body: {
				plan_slug: "compute_performance",
				billing_term_months: 12,
				funding_source: "wallet",
				ui_mode: "custom",
				quote: walletQuote,
				subscription_selection: { mode: "new" },
			},
		});
		expect(Object.keys(result.body)).not.toContain("deploy_config");
		expect(Object.keys(result.body)).not.toContain("upgrade_deployment_id");
		expect(Object.keys(result.body)).not.toContain("owner_user_id");
	});

	test("a plan_only card target posts no quote and no upgrade target", () => {
		const result = subscriptionCreateRequest(
			createRequest({
				selection: { planSlug: "compute_basic", billingTermMonths: 1, fundingSource: "stripe" },
				target: { kind: "plan_only" },
				quote: null,
			}),
		);
		expect(Object.keys(result.body).sort()).toEqual([
			"billing_term_months",
			"funding_source",
			"plan_slug",
			"subscription_selection",
			"ui_mode",
		]);
	});

	test("projects activation navigation target and entitlement fields consumed by the UI", () => {
		const activation: Extract<CheckoutOperationResult, { flow_type: "subscription_activation" }> = {
			flow_type: "subscription_activation",
			funding_source: "wallet",
			checkout_url: "",
			subscription_id: "csub_contract",
			invoice_id: null,
			agent_id: "33333333-3333-4333-8333-333333333333",
			deploy_request_id: "subscription-create-test",
			deployment_id: "hdep_created",
			deployment_name: null,
			metadata_generation: null,
			debited_usd: null,
			balance_after_usd: null,
			current_period_start: null,
			current_period_end: "2027-07-15T00:00:00Z",
			entitled_until: "2027-07-16T00:00:00Z",
		};
		expect(subscriptionCreateOutcome(activation)).toEqual({
			flowType: "subscription_activation",
			target: {
				kind: "deployment",
				deploymentId: "hdep_created",
			},
			currentPeriodEnd: "2027-07-15T00:00:00Z",
			entitledUntil: "2027-07-16T00:00:00Z",
		});
		expect(subscriptionCreateOutcome({ ...activation, deployment_id: null })).toEqual({
			flowType: "subscription_activation",
			target: { kind: "deploy_request", deployRequestId: "subscription-create-test" },
			currentPeriodEnd: "2027-07-15T00:00:00Z",
			entitledUntil: "2027-07-16T00:00:00Z",
		});
		expect(() =>
			subscriptionCreateOutcome({
				...activation,
				deployment_id: null,
				deploy_request_id: null,
			}),
		).toThrow("Activation did not return an agent request.");
	});
});
