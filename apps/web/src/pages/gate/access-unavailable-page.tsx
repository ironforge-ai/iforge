"use client";

import { DESTINATIONS } from "@clawdi/shared";
import { useRouter } from "@tanstack/react-router";
import { ShieldAlert } from "lucide-react";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { Button } from "@/components/ui/button";
import { ApiError, ApiNetworkError } from "@/lib/api-errors";
import { GateShell } from "@/pages/gate/gate-shell";

/**
 * S2b — access could not be verified.
 *
 * The third gate screen (03-12, G-03-3). It renders when `GET /v1/access/status`
 * answered with an authoritative refusal, or when `/deactivated` could not
 * resolve anything at all. Same S1/S2 chassis as the redeem and fenced screens,
 * so it carries no sidebar, no nav and no invite input: a refused identity must
 * never see dashboard chrome, which is the exposure UAT test 3 reported.
 *
 * Copy comes from `ApiErrorPanel`'s existing normalizer rather than new house
 * strings — it already reads a 401 as "Your session expired" with a re-auth
 * button and everything else as a retryable fault, which is precisely the
 * status-aware split this screen needs. Importing `@/lib/api-errors` here is
 * correct: the no-`sonner` constraint applies only to `lib/access-gate.ts`,
 * which must stay importable from the SSR `beforeLoad` graph and `bun test`.
 */
export function AccessUnavailablePage({ status }: { status: number | null }) {
	// `warn: false`: this screen is also rendered by `renderToStaticMarkup` in
	// its unit test, outside any RouterProvider, where the context is absent and
	// Retry is inert. Inside the app the router is always present.
	const router = useRouter({ warn: false });
	// A refusal carries a real status; a dropped RPC carries none, so it maps to
	// the transport-failure error the normalizer already has copy for.
	const error =
		status === null ? new ApiNetworkError("offline") : new ApiError(status, "access_check_failed");
	// The panel offers "Sign in again" only for a 401 (`isApiAuthError`, the
	// same predicate it branches on). Suppressing our own sign-in link in that
	// case keeps one intent to one control — two would read as two different
	// actions for the same thing.
	const panelOffersReauth = status === 401;
	return (
		<GateShell>
			<ApiErrorPanel
				error={error}
				title="We couldn't verify your access"
				icon={ShieldAlert}
				// `invalidate` re-runs `beforeLoad`, so Retry re-asks the gate
				// instead of reloading the document and losing client state.
				onRetry={() => void router?.invalidate()}
			/>
			{/* UI-REVIEW blocker 4: without these, a persistent 403 or a durable
			    503 leaves Retry as the only control — and Retry re-asks the gate
			    that just refused, so the user can press it forever. S2
			    (`deactivated-page.tsx`) has had a support mailto since 03-05;
			    S2b, the screen a HEALTHY user lands on when the backend flaps,
			    had nothing. Plain anchors on purpose: this component renders
			    under `renderToStaticMarkup` in its own unit test, outside any
			    RouterProvider and any ClerkProvider, so `useAuthActions()` /
			    `useClerk()` would throw at render and take the screen's only
			    test with it. `ApiErrorPanel.reauthenticate()` is the codebase's
			    existing hook-free way back through Clerk, and this is the same
			    target. Neither control is navigation into the product, so the
			    no-nav chassis rule still holds. */}
			<div className="mt-4 flex flex-wrap gap-2">
				<Button
					variant="outline"
					render={<a href={`mailto:${DESTINATIONS.supportEmail}`} />}
					nativeButton={false}
				>
					Contact support
				</Button>
				{panelOffersReauth ? null : (
					<Button variant="outline" render={<a href="/sign-in" />} nativeButton={false}>
						Sign in as someone else
					</Button>
				)}
			</div>
		</GateShell>
	);
}
