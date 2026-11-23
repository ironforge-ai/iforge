"use client";

import {
	CheckoutElementsProvider,
	ExpressCheckoutElement,
	PaymentElement,
	useCheckoutElements,
} from "@stripe/react-stripe-js/checkout";
import type {
	Stripe,
	StripeCheckoutElementsSdkOptions,
	StripeCheckoutStatus,
	StripeExpressCheckoutElementConfirmEvent,
} from "@stripe/stripe-js";
import { AlertCircle, CreditCard, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogHeader,
	DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input"; // iforge (06.3 UAT)
import { Label } from "@/components/ui/label"; // iforge (06.3 UAT)
import { Separator } from "@/components/ui/separator";
import { Spinner } from "@/components/ui/spinner";
import { useDialogExitLifecycle } from "@/components/ui/use-dialog-exit-lifecycle";
import {
	CardPaymentsUnconfiguredNotice, // iforge (06.3 G-06.3-3)
	isCardPaymentsConfigured, // iforge (06.3 G-06.3-3)
} from "@/hosted/billing/card-payments-config"; // iforge (06.3 G-06.3-3)
import { cardTrialPricePresentation } from "@/hosted/billing/deploy/deploy-price-presentation";
import { getStripe, resetStripeCache } from "@/hosted/billing/stripe";
import { useStripeAppearance } from "@/hosted/billing/stripe-appearance";
import type { CheckoutSessionClientSecret } from "@/hosted/billing/stripe-client-secret";
import { useCurrentUser } from "@/lib/auth-client"; // iforge (06.3 UAT)
import { env } from "@/lib/env";
import {
	checkoutEmailToApply, // iforge (06.3 UAT)
	checkoutEmailToSubmit, // iforge (06.3 WR-04)
	completedCheckoutPaymentStatus,
	type StripeCheckoutPaymentStatus,
	showCheckoutEmailField, // iforge (06.3 WR-04)
	updateCheckoutEmail, // iforge (06.3 WR-05)
} from "./stripe-checkout.logic";

export type StripeCheckoutSummary = {
	detail: string;
	planName: string;
	priceLabel: string;
	termLabel: string;
	trialDays: number | null;
};

type StripeCheckoutDialogProps = {
	clientSecret: CheckoutSessionClientSecret | null;
	description: string;
	onComplete: (paymentStatus: StripeCheckoutPaymentStatus) => void;
	onExpired: () => void;
	onOpenChange: (open: boolean) => void;
	open: boolean;
	summary: StripeCheckoutSummary | null;
	title: string;
	submitLabel?: string;
	onSubmittingChange?: (submitting: boolean) => void;
};

type DialogState = "loading" | "ready" | "error";
type RetainedCheckout = Pick<
	StripeCheckoutDialogProps,
	"clientSecret" | "description" | "summary" | "title"
>;

function CheckoutSummaryPanel({ summary }: { summary: StripeCheckoutSummary | null }) {
	if (!summary) return null;
	const trial = cardTrialPricePresentation(summary.priceLabel, summary.trialDays);

	return (
		<div className="rounded-lg border bg-muted/30 p-4">
			<div className="flex items-start gap-3">
				<div className="flex size-9 shrink-0 items-center justify-center rounded-md border bg-background text-primary">
					<CreditCard className="size-4" />
				</div>
				<div className="min-w-0 flex-1">
					<p className="text-xs text-muted-foreground">Order summary</p>
					<div className="mt-1 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
						<div className="min-w-0">
							<p className="truncate font-medium">{summary.planName}</p>
							<p className="text-xs text-muted-foreground">{summary.detail}</p>
						</div>
						<div className="text-left sm:text-right">
							<p className="text-sm font-medium tabular-nums sm:max-w-64">
								{trial?.summary ?? summary.priceLabel}
							</p>
							{trial ? null : <p className="text-xs text-muted-foreground">{summary.termLabel}</p>}
						</div>
					</div>
				</div>
			</div>
		</div>
	);
}

// iforge (06.3 WR-04): the session's email field. It stays mounted once shown, so the
// value is the payer's draft (synced to the address Stripe accepted) and stays editable.
export function CheckoutEmailField({
	value,
	sessionEmail,
	error,
	onChange,
	onBlur,
}: {
	value: string;
	sessionEmail: string | null;
	error: string | null;
	onChange: (value: string) => void;
	onBlur: () => void;
}) {
	return (
		<div className="flex flex-col gap-2">
			<Label htmlFor="checkout-email">Email</Label>
			<Input
				id="checkout-email"
				type="email"
				autoComplete="email"
				value={value}
				onChange={(event) => onChange(event.target.value)}
				onBlur={onBlur}
				aria-invalid={error ? true : undefined}
			/>
			{error ? (
				<p className="text-sm text-destructive" role="alert">
					{error}
				</p>
			) : null}
			{error && sessionEmail ? (
				<p className="text-xs text-muted-foreground">This checkout still uses {sessionEmail}.</p>
			) : null}
		</div>
	);
}

function CheckoutElementForm({
	submitLabel,
	onComplete,
	onExpired,
	onLoadError,
	onSubmittingChange,
}: {
	submitLabel: string;
	onComplete: (paymentStatus: StripeCheckoutPaymentStatus) => void;
	onExpired: () => void;
	onLoadError: (message: string) => void;
	onSubmittingChange: (submitting: boolean) => void;
}) {
	const checkoutState = useCheckoutElements();
	const [submitting, setSubmitting] = useState(false);
	const [takingLong, setTakingLong] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const submittingRef = useRef(false);
	const settledRef = useRef(false);
	const loadErrorMessage = checkoutState.type === "error" ? checkoutState.error.message : null;
	const checkout = checkoutState.type === "success" ? checkoutState.checkout : null;
	const settleOnce = useCallback(
		(status: StripeCheckoutStatus): boolean => {
			if (settledRef.current) return true;
			const paymentStatus = completedCheckoutPaymentStatus(status);
			if (!paymentStatus && status.type !== "expired") return false;
			settledRef.current = true;
			if (paymentStatus) onComplete(paymentStatus);
			else onExpired();
			return true;
		},
		[onComplete, onExpired],
	);

	function finishSubmitting() {
		submittingRef.current = false;
		setSubmitting(false);
		setTakingLong(false);
		onSubmittingChange(false);
	}

	useEffect(() => {
		if (!submitting) return;
		const timeout = window.setTimeout(() => setTakingLong(true), 5_000);
		return () => window.clearTimeout(timeout);
	}, [submitting]);

	useEffect(() => {
		if (loadErrorMessage) onLoadError(loadErrorMessage);
	}, [loadErrorMessage, onLoadError]);

	useEffect(() => {
		if (checkout) settleOnce(checkout.status);
	}, [checkout, settleOnce]);

	// iforge (06.3 UAT): a session created without customer_email keeps canConfirm false until
	// its own email is set (Link's optional email does not count), so Subscribe never enabled.
	const { user } = useCurrentUser(); // iforge (06.3 UAT)
	const accountEmail = user?.primaryEmailAddress?.emailAddress ?? null; // iforge (06.3 UAT)
	const [applyingEmail, setApplyingEmail] = useState(false); // iforge (06.3 UAT)
	const [emailDraft, setEmailDraft] = useState(""); // iforge (06.3 UAT)
	const [emailError, setEmailError] = useState<string | null>(null); // iforge (06.3 UAT)
	const [ownsEmail, setOwnsEmail] = useState(false); // iforge (06.3 WR-04)
	const accountEmailTriedRef = useRef(false); // iforge (06.3 UAT)
	const sessionEmail = checkout?.email ?? null; // iforge (06.3 WR-04)
	useEffect(() => {
		// iforge (06.3 WR-04): a session that arrives without an email is this client's to
		// fill, so its field stays mounted for the rest of the session.
		if (checkout && !sessionEmail) setOwnsEmail(true);
	}, [checkout, sessionEmail]);
	useEffect(() => {
		// iforge (06.3 UAT): fill the session email from the signed-in account, once.
		if (!checkout || accountEmailTriedRef.current) return;
		const email = checkoutEmailToApply(checkout.email, accountEmail);
		if (!email) return;
		accountEmailTriedRef.current = true;
		setApplyingEmail(true);
		// iforge (06.3 WR-05): a refusal or a failed call now shows its message, not just a
		// prefilled field beside a disabled Subscribe button.
		void updateCheckoutEmail((value) => checkout.updateEmail(value), email)
			.then((message) => {
				setEmailDraft(email); // iforge (06.3 WR-04): show what was applied
				setEmailError(message); // iforge (06.3 WR-05)
			})
			.finally(() => setApplyingEmail(false));
	}, [checkout, accountEmail]);

	if (checkoutState.type === "loading") {
		return (
			<div
				data-hosted="true"
				className="flex min-h-36 items-center gap-2 py-6 text-sm text-muted-foreground"
			>
				<Spinner />
				<span>Loading secure payment form…</span>
			</div>
		);
	}

	if (checkoutState.type === "error") {
		return (
			<Alert data-hosted="true" variant="destructive">
				<AlertCircle />
				<AlertDescription>
					We couldn’t load the secure payment form. Please try again.
				</AlertDescription>
			</Alert>
		);
	}

	const { checkout: readyCheckout } = checkoutState;

	// iforge (06.3 IN-02): once the payer types, an account email that loads late never
	// replaces their draft, so the auto-apply is spent on the first keystroke.
	function changeCheckoutEmail(value: string) {
		accountEmailTriedRef.current = true;
		setEmailDraft(value);
	}

	// iforge (06.3 UAT): the payer's own email, when the account had none or Stripe refused it.
	async function submitCheckoutEmail() {
		const email = checkoutEmailToSubmit(emailDraft, readyCheckout.email); // iforge (06.3 WR-04)
		if (!email) {
			// iforge (06.3 WR-04): an emptied or unchanged draft falls back to the session's email.
			if (readyCheckout.email) {
				setEmailDraft(readyCheckout.email);
				setEmailError(null);
			}
			return;
		}
		setEmailError(null);
		// iforge (06.3 WR-05): the same outcome-to-message mapping as the auto-apply.
		const message = await updateCheckoutEmail((value) => readyCheckout.updateEmail(value), email);
		if (message) setEmailError(message);
		else setEmailDraft(email); // iforge (06.3 WR-04)
	}

	async function confirmCheckout(
		expressCheckoutConfirmEvent?: StripeExpressCheckoutElementConfirmEvent,
	) {
		if (submittingRef.current || (!expressCheckoutConfirmEvent && !readyCheckout.canConfirm))
			return;
		submittingRef.current = true;
		setSubmitting(true);
		onSubmittingChange(true);
		setError(null);
		try {
			const result = await readyCheckout.confirm({
				redirect: "if_required",
				expressCheckoutConfirmEvent,
			});
			if (result.type === "error") {
				setError(result.error.message || "We could not confirm this payment. Please try again.");
				finishSubmitting();
				return;
			}
			if (settleOnce(result.session.status)) return;
			setError("Stripe needs another step before this checkout can finish.");
			finishSubmitting();
		} catch {
			setError("We could not reach Stripe. Check your connection and try again.");
			finishSubmitting();
		}
	}

	return (
		<div data-hosted="true" className="flex flex-col gap-4">
			<ExpressCheckoutElement
				options={{
					buttonHeight: 44,
					buttonTheme: {},
					buttonType: {},
					layout: { maxColumns: 2, maxRows: 1 },
					paymentMethodOrder: ["apple_pay", "google_pay", "link"],
					paymentMethods: {
						applePay: "auto",
						googlePay: "auto",
						link: "auto",
						amazonPay: "never",
						klarna: "never",
						paypal: "never",
					},
				}}
				onConfirm={confirmCheckout}
			/>
			{showCheckoutEmailField({
				sessionEmail: readyCheckout.email,
				applying: applyingEmail,
				ownsEmail,
			}) ? (
				// iforge (06.3 UAT): without a session email Stripe cannot confirm the checkout.
				// iforge (06.3 WR-04): it stays mounted, so an accepted typo can still be fixed.
				<CheckoutEmailField
					value={emailDraft}
					sessionEmail={readyCheckout.email}
					error={emailError}
					onChange={changeCheckoutEmail}
					onBlur={() => void submitCheckoutEmail()}
				/>
			) : null}
			<PaymentElement
				options={{
					layout: { type: "tabs", defaultCollapsed: false },
				}}
			/>
			{error ? (
				<Alert data-hosted="true" variant="destructive">
					<AlertCircle />
					<AlertDescription>{error}</AlertDescription>
				</Alert>
			) : null}
			{takingLong ? (
				<p className="text-sm text-muted-foreground" role="status">
					Your payment is still being confirmed. Keep this checkout open; we’ll continue as soon as
					Stripe responds.
				</p>
			) : null}
			<div className="flex justify-end">
				<Button
					type="button"
					onClick={() => void confirmCheckout()}
					disabled={submitting || !readyCheckout.canConfirm}
				>
					{submitting ? (
						<>
							<Spinner data-icon="inline-start" /> Confirming payment…
						</>
					) : (
						submitLabel
					)}
				</Button>
			</div>
		</div>
	);
}

export function StripeCheckoutDialog({
	submitLabel = "Subscribe",
	onSubmittingChange,
	clientSecret,
	description,
	onComplete,
	onExpired,
	onOpenChange,
	open,
	summary,
	title,
}: StripeCheckoutDialogProps) {
	const key = env.VITE_STRIPE_PUBLISHABLE_KEY;
	const cardPaymentsReady = isCardPaymentsConfigured(key); // iforge (06.3 G-06.3-3)
	const [stripe, setStripe] = useState<Stripe | null>(null);
	const [state, setState] = useState<DialogState>("loading");
	const [message, setMessage] = useState<string | null>(null);
	const [attempt, setAttempt] = useState(0);
	const [checkoutSubmitting, setCheckoutSubmitting] = useState(false);
	const checkout = { clientSecret, description, summary, title } satisfies RetainedCheckout;
	const exit = useDialogExitLifecycle({
		open,
		value: checkout,
		emptyValue: { clientSecret: null, description: "", summary: null, title: "" },
	});
	const appearance = useStripeAppearance(open);

	const renderedCheckout = exit.renderedValue;
	const renderedClientSecret = renderedCheckout.clientSecret;

	const completeCheckout = useCallback(
		(paymentStatus: StripeCheckoutPaymentStatus) => {
			exit.beginClose();
			onComplete(paymentStatus);
		},
		[exit.beginClose, onComplete],
	);
	const expireCheckout = useCallback(() => {
		exit.beginClose();
		onExpired();
	}, [exit.beginClose, onExpired]);

	const handleProviderLoadError = useCallback(() => {
		setState("error");
		setMessage("We couldn’t load the secure payment form. Please try again.");
	}, []);

	useEffect(() => {
		if (!open || !renderedClientSecret) return;
		let cancelled = false;
		setCheckoutSubmitting(false);
		setStripe(null);
		setMessage(null);
		setState("loading");
		void (async () => {
			// iforge (06.3 G-06.3-3): a missing key is a configuration state, not a
			// retryable failure; the body shows the not-configured notice instead.
			// iforge (06.3 WR-04): one predicate decides this, so a whitespace-only
			// key never loads Stripe.js; `!key` only narrows the type for getStripe.
			if (!cardPaymentsReady || !key) return; // iforge (06.3 WR-04)
			try {
				const nextStripe = await getStripe(key);
				if (!nextStripe) {
					throw new Error("Stripe couldn't load. Refresh and try again.");
				}
				if (cancelled) return;
				setStripe(nextStripe);
				setState("ready");
			} catch {
				resetStripeCache();
				if (!cancelled) {
					setState("error");
					setMessage("We couldn’t load the secure payment form. Please try again.");
				}
			}
		})();
		return () => {
			cancelled = true;
		};
	}, [attempt, cardPaymentsReady, key, open, renderedClientSecret]); // iforge (06.3 WR-04)

	const providerOptions = useMemo<StripeCheckoutElementsSdkOptions | null>(() => {
		if (!renderedClientSecret) return null;
		return {
			clientSecret: renderedClientSecret,
			elementsOptions: {
				appearance,
				loader: "auto",
			},
		};
	}, [appearance, renderedClientSecret]);

	function requestOpenChange(nextOpen: boolean) {
		if (!nextOpen && checkoutSubmitting) return;
		if (nextOpen) exit.beginOpen();
		else exit.beginClose();
		onOpenChange(nextOpen);
	}

	return (
		<Dialog
			open={open}
			onOpenChange={requestOpenChange}
			onOpenChangeComplete={(nextOpen) => {
				if (!nextOpen) {
					exit.completeClose();
					setStripe(null);
					setMessage(null);
					setState("loading");
					setCheckoutSubmitting(false);
				}
			}}
		>
			<DialogContent
				className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg"
				data-hosted="true"
				showCloseButton={!checkoutSubmitting}
			>
				<DialogHeader>
					<DialogTitle>{renderedCheckout.title}</DialogTitle>
					<DialogDescription>{renderedCheckout.description}</DialogDescription>
				</DialogHeader>
				<CheckoutSummaryPanel summary={renderedCheckout.summary} />
				<Separator />
				{!cardPaymentsReady ? ( // iforge (06.3 G-06.3-3)
					<CardPaymentsUnconfiguredNotice /> // iforge (06.3 G-06.3-3)
				) : state === "ready" && stripe && providerOptions ? ( // iforge (06.3 G-06.3-3)
					<CheckoutElementsProvider
						key={`${renderedClientSecret}:${attempt}`}
						stripe={stripe}
						options={providerOptions}
					>
						<CheckoutElementForm
							submitLabel={submitLabel}
							onComplete={completeCheckout}
							onExpired={expireCheckout}
							onLoadError={handleProviderLoadError}
							onSubmittingChange={(submitting) => {
								setCheckoutSubmitting(submitting);
								onSubmittingChange?.(submitting);
							}}
						/>
					</CheckoutElementsProvider>
				) : state === "error" ? (
					<Alert data-hosted="true" variant="destructive">
						<AlertCircle />
						<AlertDescription className="flex flex-col items-start gap-3">
							<span>{message ?? "We could not load the secure checkout."}</span>
							<div className="flex flex-wrap gap-2">
								<Button
									size="sm"
									variant="outline"
									onClick={() => {
										resetStripeCache();
										setAttempt((value) => value + 1);
									}}
								>
									<RefreshCw data-icon="inline-start" /> Retry payment form
								</Button>
							</div>
						</AlertDescription>
					</Alert>
				) : (
					<div
						data-hosted="true"
						className="flex min-h-36 items-center gap-2 py-6 text-sm text-muted-foreground"
					>
						<Spinner />
						<span>{message ?? "Loading secure checkout…"}</span>
					</div>
				)}
			</DialogContent>
		</Dialog>
	);
}
