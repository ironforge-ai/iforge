import type { StripeCheckoutStatus } from "@stripe/stripe-js";
import type { CheckoutOperationResult } from "@/hosted/billing/billing-client";
import { isCardPaymentsConfigured } from "@/hosted/billing/card-payments-config"; // iforge (06.3 WR-04)

export { checkoutSessionClientSecret } from "@/hosted/billing/stripe-client-secret";

export const CHECKOUT_ELEMENTS_UI_MODE = "custom";
export const HOSTED_CHECKOUT_UI_MODE = "hosted";

export type StripeCheckoutPaymentStatus = Extract<
	StripeCheckoutStatus,
	{ type: "complete" }
>["paymentStatus"];

export function completedCheckoutPaymentStatus(
	status: StripeCheckoutStatus,
): StripeCheckoutPaymentStatus | null {
	return status.type === "complete" ? status.paymentStatus : null;
}

export function checkoutUiModeForPublishableKey(
	publishableKey: string | undefined,
): typeof CHECKOUT_ELEMENTS_UI_MODE | typeof HOSTED_CHECKOUT_UI_MODE {
	// iforge (06.3 WR-04): the one card-payments predicate, so a blank key takes the hosted redirect.
	return isCardPaymentsConfigured(publishableKey)
		? CHECKOUT_ELEMENTS_UI_MODE
		: HOSTED_CHECKOUT_UI_MODE; // iforge (06.3 WR-04)
}

export function checkoutRedirectUrl(result: CheckoutOperationResult): string | null {
	return result.flow_type === "checkout_session"
		? result.action_url || result.checkout_url || null
		: result.checkout_url || null;
}

// iforge (06.3 UAT): a Checkout Elements session created without customer_email keeps
// canConfirm false until the session's own email is set; Link's optional email does not
// count. Fill it from the signed-in account, never overwriting one the session carries.
export function checkoutEmailToApply(
	sessionEmail: string | null,
	accountEmail: string | null | undefined,
): string | null {
	if (sessionEmail) return null;
	const email = accountEmail?.trim();
	return email ? email : null;
}

// iforge (06.3 UAT): ask the payer for an email when the session still has none and no
// account email is being applied to it.
// iforge (06.3 WR-04): once this client owns the session's email (the session arrived
// without one), keep the field for the life of the session. Unmounting it as soon as
// Stripe accepted an address left a typo like owner@gmial.com, or the auto-applied account
// email, invisible and impossible to change inside this checkout. It hides only while the
// account email is being applied. A session created with customer_email is never owned,
// so it never shows the field.
export function showCheckoutEmailField({
	sessionEmail,
	applying,
	ownsEmail,
}: {
	sessionEmail: string | null;
	applying: boolean;
	ownsEmail: boolean;
}): boolean {
	if (applying) return false;
	return ownsEmail || !sessionEmail;
}

export const CHECKOUT_EMAIL_INVALID_MESSAGE = "Enter a valid email address."; // iforge (06.3 WR-05)
export const CHECKOUT_EMAIL_UNREACHABLE_MESSAGE =
	"We could not reach Stripe. Check your connection and try again."; // iforge (06.3 WR-05)

type CheckoutEmailUpdateResult =
	| { type: "success" }
	| { type: "error"; error: { message?: string | null } };

// iforge (06.3 WR-05): the one place that turns Stripe's updateEmail outcome into the text
// the payer sees: null when Stripe took the address, otherwise the message to show. It
// never rejects. The account-email auto-apply swallowed both a Stripe refusal and a
// rejected call, leaving Subscribe disabled with no explanation; it now shows the same
// message the manual field does.
export async function updateCheckoutEmail(
	update: (email: string) => Promise<CheckoutEmailUpdateResult>,
	email: string,
): Promise<string | null> {
	try {
		const result = await update(email);
		if (result.type === "error") return result.error.message || CHECKOUT_EMAIL_INVALID_MESSAGE;
		return null;
	} catch {
		return CHECKOUT_EMAIL_UNREACHABLE_MESSAGE;
	}
}

// iforge (06.3 WR-04): what a blur should send. Nothing for an empty draft or one that
// already matches the session email, so a blur without an edit makes no Stripe call.
export function checkoutEmailToSubmit(draft: string, sessionEmail: string | null): string | null {
	const email = draft.trim();
	if (!email || email === sessionEmail) return null;
	return email;
}
