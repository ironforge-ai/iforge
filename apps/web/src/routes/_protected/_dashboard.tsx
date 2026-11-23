import { createFileRoute, Outlet, redirect, retainSearchParams } from "@tanstack/react-router";
import { dashboardGateDecision } from "@/lib/access-gate";
import { getAccessOutcome } from "@/lib/access-gate-server-fn";
import {
	forgetProvisionedVerdict,
	hasProvisionedVerdict,
	rememberProvisionedVerdict,
} from "@/lib/access-verdict-memo";
import { BillingLockProvider } from "@/lib/billing-lock";
import { validateDashboardSettingsSearch } from "@/lib/settings-routes";
import DashboardLayout from "@/pages/dashboard/layout";
import { AccessUnavailablePage } from "@/pages/gate/access-unavailable-page";
import { BillingLockedPanel } from "@/pages/gate/billing-locked-panel";

export const Route = createFileRoute("/_protected/_dashboard")({
	validateSearch: validateDashboardSettingsSearch,
	beforeLoad: async ({ location, context }) => {
		// Access-state gate (D-06). This is UX routing ONLY (threat T-03-13) — it
		// grants no data access. Enforcement is server-side: every gated /v1
		// route answers 403 `billing_required` for a locked account.
		//
		// An outcome the backend NEVER PRODUCED fails open: nothing was resolved,
		// so there is no gated state to mask, which is what review LOW #7 asked
		// for. An outcome it DID produce is obeyed — a 401, a 403 or the
		// fail-closed 503 `billing_unavailable` blocks here rather than rendering
		// the dashboard shell (G-03-3).
		//
		// 06.3 D-03: a RESOLVED billing_required is neither redirected nor
		// blocked. `dashboardGateDecision` answers `render-locked`, and the
		// layout below renders the normal shell with the billing locked panel
		// in the content region on every dashboard path. The one redirect is a
		// card checkout returning to /deploy (L10.1), forwarded to / with
		// settings=billing-plan and only the allow-listed checkout marker.
		//
		// EVERY return path below must carry BOTH `accessBlockedStatus` and
		// `billingLocked`, including the memo short-circuit: a missing key would
		// leave `useRouteContext()` undefined on client navigations and lock out
		// a healthy user (or leave a locked one on a page).
		//
		// G-06.3-1: the provisioned memo is keyed on `_protected`'s
		// `authIdentity` (user and session). Clerk's sign-out, sign-in and
		// sign-up are client-side navigations, so module state outlives an
		// identity; an identity-blind memo let a new unpaid user inherit the
		// previous user's unlocked verdict. A different identity always
		// re-asks. The memo module records nothing during SSR or for a null
		// (desktop) identity, and every answer other than a resolved
		// `provisioned` clears it, so an older verdict can never mask a newer
		// locked or blocked one.
		const identity = context.authIdentity ?? null;
		if (hasProvisionedVerdict(identity)) return { accessBlockedStatus: null, billingLocked: false };
		const outcome = await getAccessOutcome();
		if (outcome.outcome === "resolved" && outcome.state === "provisioned") {
			rememberProvisionedVerdict(identity);
		} else {
			forgetProvisionedVerdict();
		}
		const decision = dashboardGateDecision(outcome, {
			pathname: location.pathname,
			search: location.search,
		});
		if (decision.kind === "redirect") {
			// L10.1: the locked checkout-return forward carries its allow-listed
			// search to Settings → Compute; terminated redirects with no search.
			if ("search" in decision) throw redirect({ to: decision.to, search: decision.search });
			throw redirect({ to: decision.to });
		}
		if (decision.kind === "blocked") {
			return { accessBlockedStatus: decision.status, billingLocked: false };
		}
		if (decision.kind === "render-locked")
			return { accessBlockedStatus: null, billingLocked: true };
		return { accessBlockedStatus: null, billingLocked: false };
	},
	search: { middlewares: [retainSearchParams(["deploy_profile", "utm_source"])] },
	component: DashboardRouteLayout,
});

function DashboardRouteLayout() {
	const { accessBlockedStatus, billingLocked } = Route.useRouteContext();
	// Loose `!= null` on purpose: it treats a missing key and an explicit null
	// identically, so a future return path that forgets the key degrades to
	// rendering the dashboard rather than to a false lockout.
	if (accessBlockedStatus != null) return <AccessUnavailablePage status={accessBlockedStatus} />;
	// One tree shape for locked and unlocked (06.3 L1): the provider always
	// wraps the layout, so an unlock swaps only the content child and never
	// remounts the shell or closes an open Settings dialog.
	const locked = billingLocked === true;
	return (
		<BillingLockProvider locked={locked}>
			<DashboardLayout>{locked ? <BillingLockedPanel /> : <Outlet />}</DashboardLayout>
		</BillingLockProvider>
	);
}
