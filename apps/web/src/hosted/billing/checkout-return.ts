"use client";

import { useLocation, useRouter } from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import { toast } from "sonner";
import { useCheckoutReturnRefresh } from "@/hosted/billing/hooks";

const DEPLOYMENT_PARAMS = ["deployment_id", "upgrade_deployment_id"] as const;
const MARKER_PARAMS = [
	"session_id",
	"checkout_session_id",
	"deploy_request_id",
	...DEPLOYMENT_PARAMS,
	"mockCheckout",
] as const;

export type CheckoutReturnNavigationTarget =
	| { kind: "deploy_request"; deployRequestId: string }
	| { kind: "deployment"; deploymentId: string };
export type CheckoutReturnNavigationResult = boolean | "handled";

export function checkoutReturnWasCanceled(searchStr: string): boolean {
	return new URLSearchParams(searchStr).get("checkout") === "cancel";
}

export function checkoutReturnMarker(searchStr: string): string | null {
	const params = new URLSearchParams(searchStr);
	const values = MARKER_PARAMS.flatMap((key) => {
		const value = params.get(key);
		return value ? [`${key}=${value}`] : [];
	});
	if (checkoutReturnWasCanceled(searchStr)) values.push("checkout=cancel");
	return values.length > 0 ? values.join("&") : null;
}

export function checkoutReturnDeploymentId(searchStr: string): string | null {
	const params = new URLSearchParams(searchStr);
	for (const key of DEPLOYMENT_PARAMS) {
		const value = params.get(key);
		if (value) return value;
	}
	return null;
}

export function checkoutReturnNavigationTarget(
	searchStr: string,
): CheckoutReturnNavigationTarget | null {
	const params = new URLSearchParams(searchStr);
	const deployRequestId = params.get("deploy_request_id")?.trim();
	if (deployRequestId) return { kind: "deploy_request", deployRequestId };
	const deploymentId = checkoutReturnDeploymentId(searchStr);
	return deploymentId ? { kind: "deployment", deploymentId } : null;
}

type CheckoutReturnNavigationOwner = (
	target: CheckoutReturnNavigationTarget,
) => CheckoutReturnNavigationResult | Promise<CheckoutReturnNavigationResult>;

export async function checkoutReturnNavigationResult(
	searchStr: string,
	onNavigate: CheckoutReturnNavigationOwner,
): Promise<CheckoutReturnNavigationResult> {
	if (checkoutReturnWasCanceled(searchStr)) return false;
	const target = checkoutReturnNavigationTarget(searchStr);
	return target === null ? false : onNavigate(target);
}

export async function checkoutReturnHasNavigationOwner(
	searchStr: string,
	onNavigate: CheckoutReturnNavigationOwner,
): Promise<boolean> {
	return (await checkoutReturnNavigationResult(searchStr, onNavigate)) === true;
}

export function checkoutSearchAfterConsume(
	search: Record<string, unknown>,
): Record<string, unknown> {
	return search.settings === "billing-plan" ? { settings: "billing-plan" } : {};
}

/** iforge (06.3 G-06.3-R2-3): the sinks a checkout return reports to (sonner's `toast` in the app). */
type CheckoutReturnNotify = {
	error: (title: string, options: { description: string }) => unknown;
	message: (title: string, options: { description: string }) => unknown;
};

/**
 * iforge (06.3 G-06.3-R2-3, D-04): the checkout-return flow, extracted from the
 * useCheckoutReturnHandler effect with its collaborators injected (refresh,
 * marker cleanup, toaster), so a test can drive it against the real
 * refreshCheckoutReturnQueries.
 *
 * A cancel charged nothing, so its toast never depends on a refresh: the
 * refresh still starts, in the background with its errors ignored, to keep
 * cached billing data current, and "Checkout canceled" follows the marker
 * cleanup whatever the refresh does.
 *
 * compute_unavailable on /v2/deployments is a supported hosted state ("Your
 * Wallet and subscriptions still work"), so a caller with no deployment target
 * may pass includeDeployments: false to leave deployments out of the required
 * set. An owned return always leaves them out, as before.
 *
 * Never rejects: any throw ends in the "Couldn’t open the checkout result" error.
 */
export async function runCheckoutReturn({
	searchStr,
	onCancelCopy,
	onNavigate,
	includeDeployments,
	refresh,
	consumeMarkers,
	notify,
}: {
	searchStr: string;
	onCancelCopy: string;
	onNavigate: CheckoutReturnNavigationOwner;
	includeDeployments: boolean;
	refresh: (options?: { includeDeployments?: boolean }) => Promise<unknown>;
	consumeMarkers: () => Promise<unknown>;
	notify: CheckoutReturnNotify;
}): Promise<void> {
	try {
		const canceled = checkoutReturnWasCanceled(searchStr);
		const navigationResult = await checkoutReturnNavigationResult(searchStr, onNavigate);
		const owned = navigationResult === true;
		const refreshOptions = owned ? { includeDeployments: false } : { includeDeployments };
		if (canceled) {
			// Background refresh: a sync throw or a rejection is swallowed here.
			void Promise.resolve()
				.then(() => refresh(refreshOptions))
				.catch(() => undefined);
			await consumeMarkers();
			notify.message("Checkout canceled", { description: onCancelCopy });
			return;
		}
		// iforge (06.3 IN-01): name only what the refresh read, so a return that
		// left deployments out never claims it checked agents.
		const checked = refreshOptions.includeDeployments
			? "agents, subscription, and wallet"
			: "subscription and wallet";
		let refreshFailed = false;
		try {
			await refresh(refreshOptions);
		} catch {
			refreshFailed = true;
			notify.error("Couldn’t refresh checkout status", {
				description: `Refresh the page to check your ${checked}.`,
			});
		}
		if (owned) return;
		await consumeMarkers();
		if (refreshFailed) return;
		if (navigationResult === "handled") return;
		notify.message("Checkout status refreshed", {
			description: `We checked your ${checked}.`,
		});
	} catch {
		notify.error("Couldn’t open the checkout result", {
			description: "Refresh the page to try again.",
		});
	}
}

export function useCheckoutReturnHandler({
	onCancelCopy,
	onNavigate,
	includeDeployments = true, // iforge (06.3 G-06.3-R2-3)
}: {
	onCancelCopy: string;
	/** Return true when the callback owns navigation and deployment hydration. */
	onNavigate: CheckoutReturnNavigationOwner;
	/** iforge (06.3 G-06.3-R2-3): false leaves deployments out of the required refresh. */
	includeDeployments?: boolean;
}): void {
	const refreshCheckoutReturn = useCheckoutReturnRefresh();
	const router = useRouter();
	const searchStr = useLocation({ select: (location) => location.searchStr });
	const handledMarkerRef = useRef<string | null>(null);

	useEffect(() => {
		const marker = checkoutReturnMarker(searchStr);
		if (!marker || handledMarkerRef.current === marker) return;
		handledMarkerRef.current = marker;
		// iforge (06.3 G-06.3-R2-3): the flow lives in runCheckoutReturn.
		void runCheckoutReturn({
			searchStr, // iforge (06.3 G-06.3-R2-3)
			onCancelCopy, // iforge (06.3 G-06.3-R2-3)
			onNavigate, // iforge (06.3 G-06.3-R2-3)
			includeDeployments, // iforge (06.3 G-06.3-R2-3)
			refresh: refreshCheckoutReturn, // iforge (06.3 G-06.3-R2-3)
			consumeMarkers: () =>
				router.navigate({
					to: ".",
					search: checkoutSearchAfterConsume,
					hash: true,
					replace: true,
					resetScroll: false,
				}), // iforge (06.3 G-06.3-R2-3)
			notify: toast, // iforge (06.3 G-06.3-R2-3)
		});
	}, [includeDeployments, onCancelCopy, onNavigate, refreshCheckoutReturn, router, searchStr]); // iforge (06.3 G-06.3-R2-3)
}
