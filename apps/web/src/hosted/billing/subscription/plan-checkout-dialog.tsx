"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CreditCard, TriangleAlert, WalletCards } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Spinner } from "@/components/ui/spinner";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useBillingClient } from "@/hosted/billing/billing-client";
import {
	CHECKOUT_ELEMENTS_UI_MODE,
	checkoutRedirectUrl,
	checkoutSessionClientSecret,
	checkoutUiModeForPublishableKey,
} from "@/hosted/billing/components/stripe-checkout.logic";
import { StripeCheckoutDialog } from "@/hosted/billing/components/stripe-checkout-dialog";
import { TermSwitcher } from "@/hosted/billing/components/term-switcher";
import { WalletDebitEquation } from "@/hosted/billing/components/wallet-debit-equation";
import type { ComputePlanSlug, Plan, SubscriptionSelection } from "@/hosted/billing/contracts";
import {
	cardTrialPricePresentation,
	computePricePresentation,
} from "@/hosted/billing/deploy/deploy-price-presentation";
import {
	billingErrorDetail,
	billingErrorNormalizer,
	isIdempotencyKeyReusedError,
	normalizeBillingError,
} from "@/hosted/billing/errors";
import { billingTermLabel } from "@/hosted/billing/format";
import { useSubscriptionCreateQuote } from "@/hosted/billing/hooks";
import {
	forgetIdempotencyAttempt,
	type IdempotencyAttempt,
	idempotencyAttemptFor,
	idempotencyFingerprint,
	newIdempotencyKey,
} from "@/hosted/billing/idempotency";
import { billingKeys } from "@/hosted/billing/query-keys";
import {
	PLAN_CHECKOUT_BACK_LABEL,
	PLAN_CHECKOUT_DESCRIPTION,
	planCheckoutSubmitLabel,
	planCheckoutTitle,
	planOnlyCheckoutOutcome,
	planStripeDescription,
	planStripeSubmitLabel,
} from "@/hosted/billing/subscription/plan-checkout.logic";
import {
	type SubscriptionCreateRequestView,
	type SubscriptionCreateSelection,
	type SubscriptionFundingSource,
	subscriptionCreateRequest,
	supportedBillingTerm,
} from "@/hosted/billing/subscription/subscription-create-adapter";
import {
	computeTierLabel,
	explicitPlanOffers,
	planOffers,
	resolveBasicPlan,
	resolvePerformancePlan,
} from "@/hosted/billing/subscription/subscription-utils";
import { useActionLock } from "@/hosted/billing/use-action-lock";
import { TopUpDialog } from "@/hosted/billing/wallet/top-up-dialog";
import { walletDebitShortfallUsd } from "@/hosted/billing/wallet/wallet-debit-summary";
import {
	SUBSCRIPTION_WALLET_FUNDING_ERROR_COPY,
	useWalletTopUpDialog,
} from "@/hosted/billing/wallet/wallet-funding";
import { useWalletSnapshot } from "@/hosted/billing/wallet/wallet-query";
import { env } from "@/lib/env";
import { useProductAccess } from "@/lib/product-access";
import { shouldBlockQueryError } from "@/lib/query-state";
import { useSensitiveAction } from "@/lib/use-sensitive-action";

/**
 * iforge (06.3 D-04, UI-SPEC L7 + L8): buy one fixed plan without deploying
 * anything. The request is `POST /v2/subscription/checkout` with a `plan_only`
 * target, so it carries no deploy config, upgrade target or owner field and the
 * hosted service accepts it with compute unset.
 *
 * Chassis copied from the removed upstream `SubscriptionCreateDialog` (its
 * `upgrade_deployment_id` checkout is refused by the hosted service); differences: the plan is fixed by `planSlug` (no plan select, no
 * source picker, always `subscription_selection: { mode: "new" }`), the
 * Elements card step (L8) is supported, and the dialog toasts no success itself.
 * It reports wallet activation through `onWalletActivated` and a settled card
 * confirm through `onCardConfirmed`; the caller decides what to show.
 *
 * Keep this component mounted while closed: the L8 Stripe step lives in its
 * state and outlives the L7 dialog it replaces.
 */

const IDEMPOTENCY_SCOPE = "subscription-plan-only";

/** A new plan-only subscription, ever: no included slot, no reusable subscription. */
const NEW_SUBSCRIPTION: SubscriptionSelection = { mode: "new" };
const PLAN_ONLY_TARGET = { kind: "plan_only" } as const;

function planForSlug(plans: Plan[], planSlug: ComputePlanSlug): Plan | undefined {
	return planSlug === "compute_performance"
		? resolvePerformancePlan(plans)
		: resolveBasicPlan(plans);
}

function offersForPlan(plan: Plan | undefined, planSlug: ComputePlanSlug) {
	if (!plan) return [];
	return planSlug === "compute_basic" ? explicitPlanOffers(plan) : planOffers(plan);
}

/**
 * Sensitive (step-up capable) plan-only checkout. Mirrors
 * `useSensitiveCreateSubscription` but maps the result with
 * `planOnlyCheckoutOutcome`, which accepts an activation naming no agent.
 */
function usePlanCheckoutAction() {
	const client = useBillingClient();
	const queryClient = useQueryClient();
	return useSensitiveAction(async (request: SubscriptionCreateRequestView) => {
		const apiRequest = subscriptionCreateRequest(request);
		const outcome = planOnlyCheckoutOutcome(
			await client.checkout(apiRequest.body, apiRequest.idempotencyKey),
		);
		for (const queryKey of [
			billingKeys.subscriptions,
			billingKeys.wallet,
			billingKeys.transactions,
			billingKeys.reusableSubscriptions,
		]) {
			void queryClient.invalidateQueries({ queryKey });
		}
		return outcome;
	});
}

type StripeStep = {
	clientSecret: NonNullable<ReturnType<typeof checkoutSessionClientSecret>>;
	planSlug: ComputePlanSlug;
	description: string;
	fingerprint: string;
};

export function PlanCheckoutDialog({
	open,
	onOpenChange,
	plans,
	planSlug,
	initialBillingTermMonths,
	onWalletActivated,
	onCardConfirmed,
}: {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	plans: Plan[];
	planSlug: ComputePlanSlug;
	initialBillingTermMonths: number;
	onWalletActivated: () => void;
	onCardConfirmed: () => void;
}) {
	const hostedAccess = useProductAccess();
	const checkoutAction = usePlanCheckoutAction();
	const runAction = useActionLock();
	const attemptRef = useRef<IdempotencyAttempt | null>(null);
	const [billingTermMonths, setBillingTermMonths] = useState(initialBillingTermMonths);
	const [fundingSource, setFundingSource] = useState<SubscriptionFundingSource>("stripe");
	const [stripeStep, setStripeStep] = useState<StripeStep | null>(null);
	const walletTopUp = useWalletTopUpDialog(SUBSCRIPTION_WALLET_FUNDING_ERROR_COPY);
	const selectedPlan = useMemo(() => planForSlug(plans, planSlug), [planSlug, plans]);
	const offers = useMemo(() => offersForPlan(selectedPlan, planSlug), [planSlug, selectedPlan]);
	const selectedOffer =
		offers.find((offer) => offer.billing_term_months === billingTermMonths) ?? null;
	const selectedPrice = selectedOffer ? computePricePresentation(selectedOffer, offers) : null;
	const cardTrial =
		fundingSource === "stripe" && selectedOffer && selectedPrice
			? cardTrialPricePresentation(selectedPrice.primary, selectedOffer.card_trial_period_days)
			: null;
	const supportedTerm = supportedBillingTerm(billingTermMonths);
	const selection: SubscriptionCreateSelection | null =
		supportedTerm && selectedOffer
			? { planSlug, billingTermMonths: supportedTerm, fundingSource }
			: null;
	const walletEnabled = open && hostedAccess.canCreateCloudAgents && fundingSource === "wallet";
	const wallet = useWalletSnapshot({ enabled: walletEnabled });
	const quote = useSubscriptionCreateQuote(selection, { enabled: walletEnabled });
	const walletDebit = quote.data?.walletDebit ?? null;
	const blockingQuoteError = shouldBlockQueryError(quote.error, quote.data) ? quote.error : null;
	const walletShortfallUsd = walletDebitShortfallUsd(walletDebit);
	const walletInsufficient = walletShortfallUsd !== null;
	const isPending = checkoutAction.isPending;
	const submitLabel = planCheckoutSubmitLabel({
		fundingSource,
		walletDebitAmountUsd: walletDebit?.debitAmountUsd ?? null,
	});
	const title = planCheckoutTitle(planSlug);

	useEffect(() => {
		if (!open) return;
		const initialOffers = offersForPlan(planForSlug(plans, planSlug), planSlug);
		const nextTerm =
			initialOffers.find((offer) => offer.billing_term_months === initialBillingTermMonths)
				?.billing_term_months ??
			initialOffers[0]?.billing_term_months ??
			initialBillingTermMonths;
		setBillingTermMonths(nextTerm);
		setFundingSource("stripe");
		walletTopUp.reset();
	}, [initialBillingTermMonths, open, planSlug, plans, walletTopUp.reset]);

	useEffect(() => {
		if (open && !hostedAccess.isLoading && !hostedAccess.canCreateCloudAgents) {
			onOpenChange(false);
		}
	}, [hostedAccess.canCreateCloudAgents, hostedAccess.isLoading, onOpenChange, open]);

	function forgetAttempt(fingerprint: string) {
		forgetIdempotencyAttempt(IDEMPOTENCY_SCOPE, fingerprint);
		attemptRef.current = null;
	}

	async function submit() {
		if (
			!hostedAccess.canCreateCloudAgents ||
			!selection ||
			!selectedPrice ||
			isPending ||
			(fundingSource === "wallet" && (!walletDebit || walletInsufficient))
		) {
			return;
		}
		const uiMode = checkoutUiModeForPublishableKey(env.VITE_STRIPE_PUBLISHABLE_KEY);
		const fingerprint = idempotencyFingerprint({
			selection,
			subscriptionSelection: NEW_SUBSCRIPTION,
			target: PLAN_ONLY_TARGET,
		});
		try {
			if (!(await hostedAccess.recheckCanCreateCloudAgents())) {
				onOpenChange(false);
				return;
			}
			attemptRef.current = idempotencyAttemptFor(
				attemptRef.current,
				IDEMPOTENCY_SCOPE,
				fingerprint,
				newIdempotencyKey,
			);
			const execute = (attempt: IdempotencyAttempt) =>
				checkoutAction.execute({
					selection,
					subscriptionSelection: NEW_SUBSCRIPTION,
					target: PLAN_ONLY_TARGET,
					uiMode,
					idempotencyKey: attempt.key,
					quote: fundingSource === "wallet" ? (quote.data ?? null) : null,
				});
			const outcome = await execute(attemptRef.current).catch((error: unknown) => {
				if (
					fundingSource !== "stripe" ||
					billingErrorDetail(error)?.code !== "checkout_attempt_expired"
				)
					throw error;
				forgetIdempotencyAttempt(IDEMPOTENCY_SCOPE, fingerprint);
				attemptRef.current = idempotencyAttemptFor(
					null,
					IDEMPOTENCY_SCOPE,
					fingerprint,
					newIdempotencyKey,
				);
				// Only this first failure is retried; a second failure reaches the outer handler.
				return execute(attemptRef.current);
			});

			if (outcome.flowType === "subscription_activation") {
				forgetAttempt(fingerprint);
				onOpenChange(false);
				onWalletActivated();
				return;
			}

			const clientSecret = checkoutSessionClientSecret(outcome.checkout);
			if (uiMode === CHECKOUT_ELEMENTS_UI_MODE && clientSecret) {
				onOpenChange(false);
				setStripeStep({
					clientSecret,
					planSlug,
					description: planStripeDescription({
						billingTermMonths: selection.billingTermMonths,
						primaryPrice: selectedPrice.primary,
					}),
					fingerprint,
				});
				return;
			}
			const checkoutUrl = checkoutRedirectUrl(outcome.checkout);
			if (checkoutUrl) {
				window.location.href = checkoutUrl;
				return;
			}
			throw new Error("No checkout URL was returned. Please try again.");
		} catch (error) {
			if (fundingSource === "wallet") {
				void quote.refetch();
				if (walletTopUp.handleFundingError(error)) return;
			}
			if (
				(isIdempotencyKeyReusedError(error) ||
					billingErrorDetail(error)?.code === "checkout_attempt_expired") &&
				attemptRef.current
			) {
				forgetAttempt(attemptRef.current.fingerprint);
			}
			toast.error("Couldn’t start subscription", {
				description: normalizeBillingError(error),
			});
		}
	}

	const submitDisabled =
		!hostedAccess.canCreateCloudAgents ||
		!selection ||
		!selectedPrice ||
		isPending ||
		(fundingSource === "wallet" &&
			(!walletDebit || blockingQuoteError !== null || walletInsufficient));

	return (
		<>
			<Dialog
				open={open}
				onOpenChange={(nextOpen) => {
					if (!isPending && !walletTopUp.dialogProps.open) onOpenChange(nextOpen);
				}}
			>
				<DialogContent
					data-hosted="true"
					className="max-h-[calc(100dvh-2rem)] min-w-0 overflow-x-hidden overflow-y-auto sm:max-w-lg [&>*]:min-w-0"
					showCloseButton={!isPending}
				>
					<DialogHeader>
						<DialogTitle>{title}</DialogTitle>
						<DialogDescription>{PLAN_CHECKOUT_DESCRIPTION}</DialogDescription>
					</DialogHeader>

					<div className="flex min-w-0 flex-col gap-5">
						<div className="grid gap-4 sm:grid-cols-2">
							<div className="flex flex-col gap-1.5">
								<Label>Billing term</Label>
								<div>
									<TermSwitcher
										offers={offers}
										value={billingTermMonths}
										onChange={setBillingTermMonths}
									/>
								</div>
							</div>
							<div className="flex flex-col gap-1.5">
								<Label id="plan-checkout-funding-label">Funding source</Label>
								<ToggleGroup
									value={[fundingSource]}
									onValueChange={(value) => {
										const next = value[0];
										if (next === "stripe" || next === "wallet") setFundingSource(next);
									}}
									variant="outline"
									className="grid w-full grid-cols-2"
									aria-labelledby="plan-checkout-funding-label"
									disabled={isPending}
								>
									<ToggleGroupItem value="stripe">
										<CreditCard data-icon="inline-start" /> Card
									</ToggleGroupItem>
									<ToggleGroupItem value="wallet">
										<WalletCards data-icon="inline-start" /> Wallet
									</ToggleGroupItem>
								</ToggleGroup>
							</div>
						</div>

						{selectedOffer && selectedPrice ? (
							<div className="flex flex-col gap-1 text-sm text-muted-foreground">
								<span className="text-xs">
									{computeTierLabel(planSlug)} · {billingTermLabel(billingTermMonths)}
								</span>
								<span className="text-base font-medium whitespace-nowrap text-foreground tabular-nums">
									{selectedPrice.primary}
								</span>
								<p className="text-xs tabular-nums">
									{cardTrial?.label ?? selectedPrice.secondary}
									{cardTrial && billingTermMonths > 1 ? <> · {selectedPrice.secondary}</> : null}
								</p>
							</div>
						) : (
							<Alert variant="destructive">
								<TriangleAlert aria-hidden />
								<AlertTitle>Plan price unavailable</AlertTitle>
								<AlertDescription>
									Refresh the page before starting a paid subscription.
								</AlertDescription>
							</Alert>
						)}

						{fundingSource === "wallet" ? (
							quote.isFetching && !quote.data ? (
								<p className="text-sm text-muted-foreground" role="status">
									Getting the exact wallet debit…
								</p>
							) : blockingQuoteError ? (
								<ApiErrorPanel
									normalizer={billingErrorNormalizer}
									error={blockingQuoteError}
									onRetry={() => void quote.refetch()}
									title="Couldn’t get subscription quote"
								/>
							) : walletDebit ? (
								<div className="flex flex-col gap-3">
									<WalletDebitEquation
										balanceBeforeUsd={walletDebit.balanceBeforeUsd}
										debitAmountUsd={walletDebit.debitAmountUsd}
										balanceAfterUsd={walletDebit.balanceAfterUsd}
									/>
									{walletInsufficient ? (
										<Alert variant="destructive">
											<TriangleAlert aria-hidden />
											<AlertTitle>Not enough Wallet balance</AlertTitle>
											<AlertDescription className="flex flex-col items-start gap-3">
												<span>Top up the shortfall, then review a fresh wallet quote.</span>
												<Button
													type="button"
													size="sm"
													variant="outline"
													disabled={!wallet.data || isPending}
													onClick={() => walletTopUp.show(walletShortfallUsd)}
												>
													<WalletCards data-icon="inline-start" /> Top up Wallet
												</Button>
											</AlertDescription>
										</Alert>
									) : null}
								</div>
							) : null
						) : null}

						<DialogFooter>
							<Button
								type="button"
								variant="ghost"
								disabled={isPending}
								onClick={() => onOpenChange(false)}
							>
								{PLAN_CHECKOUT_BACK_LABEL}
							</Button>
							<Button
								type="button"
								disabled={submitDisabled}
								onClick={() => void runAction(submit).catch(() => undefined)}
							>
								{isPending ? (
									<Spinner data-icon="inline-start" />
								) : fundingSource === "wallet" ? (
									<WalletCards data-icon="inline-start" />
								) : (
									<CreditCard data-icon="inline-start" />
								)}
								<span className="whitespace-nowrap tabular-nums">{submitLabel}</span>
							</Button>
						</DialogFooter>
					</div>
				</DialogContent>
			</Dialog>

			{fundingSource === "wallet" && wallet.data ? (
				<TopUpDialog {...walletTopUp.dialogProps} onComplete={() => void quote.refetch()} />
			) : null}

			<StripeCheckoutDialog
				open={stripeStep !== null}
				onOpenChange={(next) => {
					if (!next) setStripeStep(null);
				}}
				clientSecret={stripeStep?.clientSecret ?? null}
				title={stripeStep ? planCheckoutTitle(stripeStep.planSlug) : title}
				description={stripeStep?.description ?? ""}
				summary={null}
				submitLabel={planStripeSubmitLabel(stripeStep?.planSlug ?? planSlug)}
				onComplete={() => {
					if (stripeStep) forgetAttempt(stripeStep.fingerprint);
					setStripeStep(null);
					onCardConfirmed();
				}}
				onExpired={() => {
					if (stripeStep) forgetAttempt(stripeStep.fingerprint);
					setStripeStep(null);
					toast.error("Checkout expired", {
						description:
							"This secure checkout can no longer be used. Start a new checkout to try again.",
					});
				}}
			/>
		</>
	);
}
