"use client";

import { AlertCircle } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

/**
 * iforge (06.3 G-06.3-3, D-04): an honest "not configured" state for in-page
 * card payments.
 *
 * Wallet top-ups are Elements-only on the hosted service: every top-up opens a
 * Checkout Session that only Stripe Checkout Elements can complete, and those
 * need VITE_STRIPE_PUBLISHABLE_KEY. Without the key a top-up can never finish,
 * so the top-up dialog checks this predicate BEFORE asking the hosted service
 * for a session, and the payment dialog shows this notice instead of a Retry
 * that cannot succeed.
 *
 * Card subscription checkout does not use this notice: without a key it falls
 * back to Stripe's hosted redirect (checkoutUiModeForPublishableKey), which
 * works without Stripe.js.
 *
 * The copy is the 06.3 UI-SPEC "Card payments unconfigured" rows, pinned by
 * card-payments-config.iforge.test.tsx. The notice never renders the key.
 */

/** True only for a key with at least one non-whitespace character. */
export function isCardPaymentsConfigured(publishableKey: string | undefined): boolean {
	return typeof publishableKey === "string" && publishableKey.trim().length > 0;
}

export const CARD_PAYMENTS_UNCONFIGURED_TITLE = "Card payments aren’t configured";

export const CARD_PAYMENTS_UNCONFIGURED_DESCRIPTION =
	"This site isn’t set up to take card payments yet. Contact support if you need to pay by card.";

/** A standing configuration state, not a transient failure: no Retry, no action. */
export function CardPaymentsUnconfiguredNotice() {
	return (
		<Alert data-hosted="true">
			<AlertCircle aria-hidden />
			<AlertTitle>{CARD_PAYMENTS_UNCONFIGURED_TITLE}</AlertTitle>
			<AlertDescription>{CARD_PAYMENTS_UNCONFIGURED_DESCRIPTION}</AlertDescription>
		</Alert>
	);
}
