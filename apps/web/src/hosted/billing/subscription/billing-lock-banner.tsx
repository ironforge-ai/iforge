"use client";

import { DESTINATIONS } from "@clawdi/shared";
import { LockKeyhole } from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

/**
 * Settings → Compute lock banner (06.3 UI-SPEC L5) and the unlock poll states
 * that replace it in the same slot (L9). Exactly one alert renders per state,
 * so alerts never stack. None of them uses the accent color.
 *
 * No state here claims success: the only success message is the provider's
 * unlocked toast after access status reads `provisioned` (T-06.3-09-02).
 * The still-confirming copy tells the user not to pay again (T-06.3-09-05).
 */

export const LOCK_BANNER_TITLE = "Choose a plan to unlock iforge";
export const LOCK_BANNER_DESCRIPTION =
	"Pick Basic or Performance below. iforge unlocks as soon as your payment is confirmed, and you can deploy an agent later.";
export const CONFIRMING_TITLE = "Confirming your payment";
export const CONFIRMING_DESCRIPTION =
	"This usually takes a few seconds. iforge unlocks automatically, so there’s no need to reload.";
export const STILL_CONFIRMING_TITLE = "Still confirming your payment";
export const STILL_CONFIRMING_DESCRIPTION =
	"We haven’t confirmed your plan yet. Don’t pay again. Check again in a minute, or contact support if it stays pending.";
export const CHECK_AGAIN_LABEL = "Check again";
export const CONTACT_SUPPORT_LABEL = "Contact support";

/** L10: the `onCancelCopy` for a plan checkout canceled while locked. */
export const PLAN_CHECKOUT_CANCEL_COPY = "No payment was made. Choose a plan when you’re ready.";

export type BillingLockBannerState = "locked" | "confirming" | "still_confirming";

export function BillingLockBanner({
	state,
	onCheckAgain,
}: {
	state: BillingLockBannerState;
	onCheckAgain: () => void;
}) {
	if (state === "confirming") {
		return (
			<Alert role="status" aria-live="polite">
				<Spinner aria-hidden="true" role="presentation" className="text-muted-foreground" />
				<AlertTitle>{CONFIRMING_TITLE}</AlertTitle>
				<AlertDescription>{CONFIRMING_DESCRIPTION}</AlertDescription>
			</Alert>
		);
	}
	if (state === "still_confirming") {
		return (
			<Alert variant="warning" role="alert">
				<AlertTitle>{STILL_CONFIRMING_TITLE}</AlertTitle>
				<AlertDescription>{STILL_CONFIRMING_DESCRIPTION}</AlertDescription>
				{/* A sibling of the description, not a child: AlertDescription
				 * underlines nested links, which would restyle the support button. */}
				<div className="mt-3 flex flex-wrap gap-2">
					<Button type="button" variant="outline" size="sm" onClick={onCheckAgain}>
						{CHECK_AGAIN_LABEL}
					</Button>
					<Button
						variant="outline"
						size="sm"
						render={<a href={`mailto:${DESTINATIONS.supportEmail}`} />}
						nativeButton={false}
					>
						{CONTACT_SUPPORT_LABEL}
					</Button>
				</div>
			</Alert>
		);
	}
	return (
		<Alert>
			<LockKeyhole aria-hidden="true" />
			<AlertTitle>{LOCK_BANNER_TITLE}</AlertTitle>
			<AlertDescription>{LOCK_BANNER_DESCRIPTION}</AlertDescription>
		</Alert>
	);
}
