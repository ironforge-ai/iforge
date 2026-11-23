"use client";

import { useLocation } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import type { AgentTile } from "@/components/dashboard/agents-card";
import { SettingsPanelHeader } from "@/components/settings/settings-panel-header";
import {
	checkoutReturnMarker,
	checkoutReturnWasCanceled,
	useCheckoutReturnHandler,
} from "@/hosted/billing/checkout-return";
import type { ComputePlanSlug } from "@/hosted/billing/contracts";
import { usePlans } from "@/hosted/billing/hooks";
import {
	BillingLockBanner,
	PLAN_CHECKOUT_CANCEL_COPY,
} from "@/hosted/billing/subscription/billing-lock-banner"; // iforge (06.3 L5/L9/L10)
import {
	PLAN_STARTED_CARD_DESCRIPTION,
	PLAN_STARTED_TOAST_TITLE,
	PLAN_STARTED_WALLET_DESCRIPTION,
} from "@/hosted/billing/subscription/plan-checkout.logic";
import { PlanCheckoutDialog } from "@/hosted/billing/subscription/plan-checkout-dialog";
import { PlanComparison } from "@/hosted/billing/subscription/plan-comparison";
import { SubscriptionsSection } from "@/hosted/billing/subscription/subscriptions-section";
import { useBillingLocked, useBillingUnlockPoll } from "@/lib/billing-lock"; // iforge (06.3 L9)

const DESCRIPTION = "Subscriptions, plans, and account billing for hosted agents.";
const SUBSCRIPTION_PAGE_CLASS = "flex flex-col gap-8 px-5 sm:px-6 lg:px-8";

/** iforge (06.3 L6): the plan and term a "Choose {plan}" click opened checkout for. */
type PlanChoice = { planSlug: ComputePlanSlug; billingTermMonths: number };

/** iforge (06.3 L10): a plan-only return has no deployment target to hand off. */
const NO_NAVIGATION_OWNER = () => false;

/**
 * iforge (06.3 L10.2): checkout-return handling for a billing-locked account,
 * mounted only while locked so the unlocked Compute page is unchanged. The
 * existing handler refreshes billing, strips the markers (keeping
 * settings=billing-plan) and toasts; a session marker also starts the L9 poll.
 * A forged marker only starts a read-only poll (T-06.3-09-03).
 *
 * iforge (06.3 G-06.3-R2-3): a plan-only return has no deployment target, so
 * the deployments read is not required. With compute unset the hosted service
 * answers it 503 compute_unavailable, which must not turn a plan return into
 * "Couldn’t refresh checkout status".
 */
function LockedCheckoutReturn({ onSessionReturn }: { onSessionReturn: () => void }) {
	useCheckoutReturnHandler({
		onCancelCopy: PLAN_CHECKOUT_CANCEL_COPY,
		onNavigate: NO_NAVIGATION_OWNER,
		includeDeployments: false, // iforge (06.3 G-06.3-R2-3)
	});
	const searchStr = useLocation({ select: (location) => location.searchStr });
	useEffect(() => {
		if (checkoutReturnWasCanceled(searchStr)) return;
		if (!checkoutReturnMarker(searchStr)?.includes("checkout_session_id=")) return;
		onSessionReturn();
	}, [onSessionReturn, searchStr]);
	return null;
}

export function SubscriptionPage({ agentTiles }: { agentTiles: readonly AgentTile[] }) {
	const [term, setTerm] = useState(1);
	// iforge (06.3 L6/L7): `openPlan` is the dialog's open state; `checkoutPlan`
	// keeps the last choice so the dialog stays mounted while closed (its L8
	// Stripe step outlives L7 and still reads the plan).
	const [openPlan, setOpenPlan] = useState<PlanChoice | null>(null);
	const [checkoutPlan, setCheckoutPlan] = useState<PlanChoice>({
		planSlug: "compute_basic",
		billingTermMonths: term,
	});
	const plansQuery = usePlans();
	// iforge (06.3 L9): while locked a payment starts the unlock poll instead of
	// claiming success; card activation is asynchronous (RESEARCH Pitfall 12),
	// so only the provider's toast after access status reads provisioned says so.
	const locked = useBillingLocked();
	const poll = useBillingUnlockPoll();

	/** iforge (06.3 L7.8): wallet activation. */
	function onPlanWalletActivated() {
		if (locked) {
			poll.start();
			return;
		}
		toast.success(PLAN_STARTED_TOAST_TITLE, { description: PLAN_STARTED_WALLET_DESCRIPTION });
	}

	/** iforge (06.3 L8): a settled card confirm. */
	function onPlanCardConfirmed() {
		if (locked) {
			poll.start();
			return;
		}
		toast.success(PLAN_STARTED_TOAST_TITLE, { description: PLAN_STARTED_CARD_DESCRIPTION });
	}

	function choosePlan(planSlug: ComputePlanSlug, billingTermMonths: number) {
		const choice = { planSlug, billingTermMonths };
		setCheckoutPlan(choice);
		setOpenPlan(choice);
	}

	return (
		<div data-hosted="true" className={SUBSCRIPTION_PAGE_CLASS}>
			{/* iforge (06.3 L5/L9): one lock or confirming alert, first in the stack. */}
			{locked ? (
				<BillingLockBanner
					state={poll.state === "idle" ? "locked" : poll.state}
					onCheckAgain={poll.retry}
				/>
			) : null}
			{locked ? <LockedCheckoutReturn onSessionReturn={poll.start} /> : null}

			<SettingsPanelHeader title="Compute" description={DESCRIPTION} />

			<SubscriptionsSection agentTiles={agentTiles} />

			{/* iforge (06.3 L6/L9): CTAs emphasized while locked, held while confirming. */}
			<PlanComparison
				term={term}
				onTermChange={setTerm}
				onChoosePlan={choosePlan}
				locked={locked}
				choosePlanDisabled={poll.state === "confirming"}
			/>

			{/* iforge (06.3 L7): rendered unconditionally, see `checkoutPlan`. */}
			<PlanCheckoutDialog
				open={openPlan !== null}
				onOpenChange={(open) => {
					if (!open) setOpenPlan(null);
				}}
				plans={plansQuery.data ?? []}
				planSlug={checkoutPlan.planSlug}
				initialBillingTermMonths={checkoutPlan.billingTermMonths}
				onWalletActivated={onPlanWalletActivated}
				onCardConfirmed={onPlanCardConfirmed}
			/>
		</div>
	);
}
