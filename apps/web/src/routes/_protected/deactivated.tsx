import { createFileRoute, redirect } from "@tanstack/react-router";
import { deactivatedGateDecision } from "@/lib/access-gate";
import { getAccessOutcome } from "@/lib/access-gate-server-fn";
import { routeHeadTitle } from "@/lib/document-title";
import { AccessUnavailablePage } from "@/pages/gate/access-unavailable-page";
import { DeactivatedPage } from "@/pages/gate/deactivated-page";

export const Route = createFileRoute("/_protected/deactivated")({
	beforeLoad: async () => {
		// The S2 fenced screen renders ONLY when the backend actually resolved
		// `terminated`. This closes an INVERTED risk (T-03-38): the previous bare
		// catch rendered "This account is deactivated" for any failure, so an
		// unrelated 401 told a healthy user their account was closed.
		// `03-UAT.md` test 2's setup_note shows S2 was reached during UAT through
		// this very branch rather than by a genuinely terminated principal.
		//
		// Unresolved outcomes BLOCK rather than redirect, on purpose: redirecting
		// to `/` against a flapping backend could oscillate between the two
		// routes. Both non-redirect paths return `accessBlockedStatus` so the
		// context shape is uniform.
		const decision = deactivatedGateDecision(await getAccessOutcome());
		if (decision.kind === "redirect") throw redirect({ to: decision.to });
		if (decision.kind === "blocked") return { accessBlockedStatus: decision.status };
		return { accessBlockedStatus: null };
	},
	head: () => routeHeadTitle("Account deactivated"),
	component: DeactivatedRoute,
});

function DeactivatedRoute() {
	const { accessBlockedStatus } = Route.useRouteContext();
	// Loose `!= null`: a missing key degrades to the fenced screen's normal
	// render rather than to a false "couldn't verify" lockout.
	if (accessBlockedStatus != null) return <AccessUnavailablePage status={accessBlockedStatus} />;
	return <DeactivatedPage />;
}
