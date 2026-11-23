import { useRouteContext, useRouter, useSearch } from "@tanstack/react-router";
import { LockKeyhole } from "lucide-react";
import { useCallback, useLayoutEffect } from "react";
import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";

/**
 * Billing locked panel (06.3 D-03, UI-SPEC L2): the content region of the
 * dashboard shell while the account is `billing_required`. Rendered by
 * `_dashboard` in place of `<Outlet />`, so no page component mounts.
 *
 * OSS-safe: imports nothing from the hosted source tree. The hosted build
 * offers one action, "Choose a plan", which opens Settings → Compute; the OSS
 * build has no billing UI to send the user to, so it explains and offers
 * nothing.
 *
 * Copy is the 06.3 UI-SPEC Copywriting Contract verbatim (typographic
 * apostrophe U+2019); `billing-locked-panel.test.tsx` pins every string
 * against its row.
 */

export const LOCKED_PANEL_TITLE_HOSTED = "Choose a plan to start using iforge";
export const LOCKED_PANEL_DESCRIPTION_HOSTED =
	"Your account is ready. Pick a compute plan and pay by card or from your Wallet. You don’t need to deploy an agent first.";
export const LOCKED_PANEL_CTA = "Choose a plan";
export const LOCKED_PANEL_TITLE_OSS = "This account needs an active plan";
export const LOCKED_PANEL_DESCRIPTION_OSS =
	"This iforge server requires a paid plan before you can use it. Ask the server’s operator to activate your account.";

const IS_HOSTED_BUILD = import.meta.env.VITE_IFORGE_HOSTED === "true";

/**
 * L3 arrival tracker: remembers the last identity whose arrival was handled,
 * so the arrival open fires once per identity rather than once per document
 * (G-06.3-1). Clerk's sign-out, sign-in and sign-up are client-side
 * navigations, so a document can see several identities; a second locked
 * identity in the same tab must still land on Settings → Compute.
 *
 * It starts at `undefined`, not `null`, so a null identity (the desktop build,
 * where `_protected` returns `authIdentity: null`) can be recorded too.
 */
export function createPlanArrivalTracker(): {
	handled(identity: string | null): boolean;
	mark(identity: string | null): void;
} {
	let last: string | null | undefined;
	return {
		handled: (identity) => last !== undefined && last === identity,
		mark: (identity) => {
			last = identity;
		},
	};
}

/**
 * The one arrival tracker for this document. Browser-only by construction: it
 * is only read and marked from the layout effect below, and layout effects
 * never run during SSR, where module scope is shared across users and
 * requests. Closing the dialog leaves the user on the panel, and client
 * navigation never reopens it for the same identity; only the CTA does.
 */
const planArrival = createPlanArrivalTracker();

/**
 * L3: open Settings → Compute on the first locked render for an identity,
 * hosted build only, and only when the URL carries no `settings` param (an
 * existing one, such as the wallet top-up return, is left untouched).
 */
export function shouldOpenPlanOnArrival({
	hosted,
	isBrowser,
	alreadyHandled,
	settings,
}: {
	hosted: boolean;
	isBrowser: boolean;
	alreadyHandled: boolean;
	settings: unknown;
}): boolean {
	return hosted && isBrowser && !alreadyHandled && settings === undefined;
}

export function BillingLockedPanelView({
	hosted,
	onChoosePlan,
}: {
	hosted: boolean;
	onChoosePlan: () => void;
}) {
	return (
		<div className="flex flex-1 items-center justify-center px-4 py-6 sm:py-12">
			<EmptyState
				variant="page"
				icon={LockKeyhole}
				title={hosted ? LOCKED_PANEL_TITLE_HOSTED : LOCKED_PANEL_TITLE_OSS}
				description={hosted ? LOCKED_PANEL_DESCRIPTION_HOSTED : LOCKED_PANEL_DESCRIPTION_OSS}
				action={
					hosted ? (
						<Button type="button" onClick={onChoosePlan}>
							{LOCKED_PANEL_CTA}
						</Button>
					) : undefined
				}
			/>
		</div>
	);
}

export function BillingLockedPanel() {
	const router = useRouter();
	const settings = useSearch({
		from: "/_protected/_dashboard",
		select: (search) => search.settings,
	});
	const identity = useRouteContext({
		from: "/_protected/_dashboard",
		select: (context) => context.authIdentity ?? null,
	});
	const openPlan = useCallback(() => {
		// Same path, Settings → Compute open; a client navigation, never a reload.
		void router.navigate({
			to: ".",
			search: (current) => ({ ...current, settings: "billing-plan" }),
			hash: true,
			replace: true,
			resetScroll: false,
		});
	}, [router]);

	// Layout effects never run during SSR, so the tracker is browser-only by
	// construction; the isBrowser input keeps the predicate honest regardless.
	// `identity` is a dependency: a new identity in the same tab re-runs the
	// effect and gets its own arrival open (G-06.3-1).
	useLayoutEffect(() => {
		if (
			shouldOpenPlanOnArrival({
				hosted: IS_HOSTED_BUILD,
				isBrowser: typeof window !== "undefined",
				alreadyHandled: planArrival.handled(identity),
				settings,
			})
		) {
			planArrival.mark(identity);
			openPlan();
			return;
		}
		// The URL already names a settings section (L3.3): leave it untouched,
		// and count this identity's arrival as handled.
		if (settings !== undefined) planArrival.mark(identity);
	}, [settings, identity, openPlan]);

	// The CTA always opens the section, regardless of the arrival flag.
	return <BillingLockedPanelView hosted={IS_HOSTED_BUILD} onChoosePlan={openPlan} />;
}
