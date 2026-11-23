import { describe, expect, test } from "bun:test";
import { DESTINATIONS } from "@clawdi/shared";
import { renderToStaticMarkup } from "react-dom/server";
import { AccessUnavailablePage } from "@/pages/gate/access-unavailable-page";

/**
 * S2b — the fail-closed gate screen the `refused` branch renders (G-03-3).
 *
 * The "no dashboard chrome" assertions below are what make "the gate is not
 * decorative" checkable: UAT test 3 reported a 401 rendering the full dashboard
 * shell, sidebar and greeting included. A screen that quietly grew nav would
 * reintroduce the exact exposure without any other test noticing.
 */

/** renderToStaticMarkup escapes quotes/ampersands — decode the handful of
 * entities the copy can produce so assertions read as prose. */
function decode(markup: string): string {
	return markup.replaceAll("&#x27;", "'").replaceAll("&quot;", '"').replaceAll("&amp;", "&");
}

function render(status: number | null): string {
	return decode(renderToStaticMarkup(<AccessUnavailablePage status={status} />));
}

/** Sidebar entries from `pages/dashboard/layout` — none may appear here. */
const DASHBOARD_NAV_LABELS = [
	"Overview",
	"Agents",
	"Sessions",
	"Memories",
	"Projects",
	"Skills",
	"Vaults",
	"Connectors",
];

const UNVERIFIED_TITLE = "We couldn't verify your access";
const EXPIRED_TITLE = "Your session expired";

describe("AccessUnavailablePage copy", () => {
	test("a 401 reads as an expired session with a sign-in affordance", () => {
		const markup = render(401);
		expect(markup).toContain(EXPIRED_TITLE);
		expect(markup).toContain("Sign in again");
	});

	test("a 503 reads as unable-to-verify with a retry affordance, not the 401 copy", () => {
		const markup = render(503);
		expect(markup).toContain(UNVERIFIED_TITLE);
		expect(markup).toContain("Retry");
		expect(markup).not.toContain(EXPIRED_TITLE);
		expect(markup).not.toContain("Sign in again");
	});

	test("a null status (RPC unreachable, used by /deactivated) offers the same retry surface", () => {
		const markup = render(null);
		expect(markup).toContain(UNVERIFIED_TITLE);
		expect(markup).toContain("Retry");
		expect(markup).not.toContain(EXPIRED_TITLE);
	});
});

describe("the screen is a gate, not decorated dashboard chrome", () => {
	for (const status of [401, 403, 429, 500, 502, 503, null]) {
		test(`status ${String(status)} renders no dashboard nav and no sidebar`, () => {
			const markup = render(status);
			expect(markup).not.toContain('data-testid="app-sidebar"');
			expect(markup).not.toContain("<nav");
			for (const label of DASHBOARD_NAV_LABELS) {
				expect(markup).not.toContain(`>${label}<`);
			}
		});

		test(`status ${String(status)} offers no invite code input`, () => {
			const markup = render(status);
			// A refused user is not offered a redemption form on this surface.
			expect(markup).not.toContain("<input");
			expect(markup).not.toContain("Invite code");
			expect(markup).not.toContain("Redeem invite");
		});
	}
});

/*
 * UI-REVIEW blocker 4 — S2b was a dead end for any persistent non-401 refusal.
 *
 * `ApiErrorPanel` offers "Sign in again" only when `isApiAuthError` (401). A
 * 403 or a durable 503 got Retry and nothing else, and `onRetry` calls
 * `router.invalidate()` — which re-asks the same gate that just refused. S2
 * (`deactivated-page.tsx`) has a support `mailto:` escape hatch; S2b —
 * the screen a HEALTHY user lands on when the backend flaps — had none, so a
 * user in that state could press Retry forever.
 *
 * These assertions run through the same `renderToStaticMarkup` harness above,
 * which is itself the proof that the new affordances need neither a
 * RouterProvider nor a ClerkProvider: this file constructs neither.
 */

/** The same address S2 uses — the two gate screens must not diverge. */
const SUPPORT_MAILTO = `mailto:${DESTINATIONS.supportEmail}`;
const SIGN_IN_HREF = '"/sign-in"';
const ALL_STATUSES = [401, 403, 404, 429, 500, 502, 503, null] as const;

describe("S2b is not a dead end (UI-REVIEW blocker 4)", () => {
	for (const status of ALL_STATUSES) {
		test(`status ${String(status)} offers a support path`, () => {
			expect(render(status)).toContain(SUPPORT_MAILTO);
		});
	}

	for (const status of ALL_STATUSES.filter((s) => s !== 401)) {
		test(`status ${String(status)} offers an identity escape hatch alongside Retry`, () => {
			const markup = render(status);
			expect(markup).toContain(SIGN_IN_HREF);
			expect(markup).toContain("Sign in as someone else");
			expect(markup).toContain("Retry");
		});
	}

	test("a 401 keeps the panel's own re-auth control and does not duplicate it", () => {
		const markup = render(401);
		expect(markup).toContain("Sign in again");
		// The panel is already offering re-auth; a second sign-in control would
		// read as two different actions for one intent.
		expect(markup).not.toContain(SIGN_IN_HREF);
		expect(markup).not.toContain("Sign in as someone else");
		expect(markup).toContain(SUPPORT_MAILTO);
	});

	test("the escape hatches are affordances, not a way past the gate", () => {
		// A refused identity still gets no dashboard chrome and no invite form;
		// the chassis rule survives the addition (assertions above cover every
		// status, this one states the intent for the reader).
		for (const status of ALL_STATUSES) {
			const markup = render(status);
			expect(markup).not.toContain("<nav");
			expect(markup).not.toContain("<input");
		}
	});
});
