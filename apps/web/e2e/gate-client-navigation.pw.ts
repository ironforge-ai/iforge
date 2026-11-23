import { expect, type Page, type Route, test } from "@playwright/test";

// Recurrence guard for G-03-4: a CLIENT-SIDE navigation INTO
// `/_protected/_dashboard` must commit.
//
// `apps/web/src/lib/access-gate.server.ts` matched TanStack Start's client
// import-protection deny glob `**/*.server.*`, so in the client bundle the whole
// module — including the `createServerFn` RPC stub — was replaced by the
// plugin's mock Proxy. Awaiting that Proxy resolves with itself, an undetectable
// foreign-thenable cycle that spins the microtask queue forever and permanently
// starves the browser's main thread. The `_dashboard` beforeLoad `try/catch`
// fail-open never ran, because the `await` never settled. See
// `.planning/debug/deploy-route-hangs-browser.md`.
//
// Three properties of this spec are load-bearing; drop any one and it silently
// stops guarding anything (each was observed passing against a wedged build
// while this guard was being written):
//
//   1. INTO, not within. `_dashboard` is a layout route, so its `beforeLoad`
//      runs on the client only when the router ENTERS it from outside. A sidebar
//      hop between two children (/sessions -> /projects) leaves the layout match
//      in place and never re-runs it. The entry point here is `/cli-authorize`,
//      a `_protected` sibling of `_dashboard` whose shell always renders a plain
//      `<Link to="/">` ("Back to dashboard", `src/pages/cli-authorize/page.tsx`).
//   2. Click only AFTER the link hydrates. Before React attaches, the anchor is
//      inert and the click performs a native document GET — which re-enters the
//      dashboard through SSR, where the real module is always used. So the whole
//      spec degrades into a second copy of `page.goto`. `waitForFunction` on the
//      anchor's React props is what makes the click a router-owned transition.
//   3. The `__gateNavProbe` marker. It is the proof that no document reload
//      happened, i.e. that property 2 actually held at click time.
//
// Nothing already in the repo could catch this: every existing web test renders
// with `renderToStaticMarkup`, and `page.goto` alone is not enough either.
//
// READ A FAILURE HERE AS THE WEDGE, NOT AS FLAKE. Against a wedged build this
// guard fails by TIMEOUT rather than by assertion: a starved main thread never
// answers, so the URL updates to "/" while `page.title()` and `page.evaluate()`
// both hang until the test times out.

const now = new Date("2026-07-04T12:00:00.000Z").toISOString();

const dashboardStats = {
	total_sessions: 0,
	total_messages: 0,
	total_tokens: 0,
	active_days: 0,
	current_streak: 0,
	longest_streak: 0,
	peak_hour: 12,
	favorite_model: null,
	skills_count: 0,
	memories_count: 0,
	vault_count: 0,
	vault_keys_count: 0,
	connectors_count: 0,
	manual_sessions_last_7_days: 0,
	contribution: [{ date: "2026-07-04", count: 0, level: 0 }],
};

const projects = [
	{
		id: "project-gate-nav",
		name: "Gate Nav Project",
		slug: "gate-nav-project",
		kind: "environment",
		origin_environment_id: "agent-gate-nav",
		archived_at: null,
		created_at: now,
		is_owner: true,
		owner_display: "Dev User",
		owner_handle: "dev-user",
	},
];

const emptyPage = { items: [], total: 0, page: 1, page_size: 25 };

async function fulfillJson(route: Route, body: unknown) {
	await route.fulfill({
		status: 200,
		contentType: "application/json",
		body: JSON.stringify(body),
	});
}

/**
 * Stubs the cloud-api reads the destination route makes, following the house
 * pattern in `pages-smoke.pw.ts`.
 *
 * `GET /v1/access/status` is deliberately NOT stubbed. The access lookup runs
 * inside the server fn on the dev server, so it is not interceptable from the
 * browser anyway; with no backend on 127.0.0.1:8000 it fails at the transport
 * layer and the gate fails open, and the dashboard renders. That is the correct
 * baseline both before and after 03-12 — it keeps this guard a test of
 * NAVIGATION LIVENESS, independent of whatever the gate decides. The wedge
 * reproduces regardless of backend reachability, so nothing is lost.
 */
async function stubApi(page: Page) {
	await page.route("**/v1/**", async (route) => {
		const p = new URL(route.request().url()).pathname;
		// Never fabricate an access answer — see the doc comment above.
		if (p === "/v1/access/status") return route.continue();
		if (p === "/v1/dashboard/stats") return fulfillJson(route, dashboardStats);
		if (p === "/v1/projects") return fulfillJson(route, projects);
		if (p === "/v1/agents") return fulfillJson(route, []);
		if (p === "/v1/sessions") return fulfillJson(route, emptyPage);
		return fulfillJson(route, {});
	});
}

const BACK_TO_DASHBOARD = 'a[href="/"]';

test("client-side <Link> navigation into the dashboard commits", async ({ page }) => {
	const messages: string[] = [];
	page.on("console", (m) => messages.push(`console.${m.type()}: ${m.text()}`));
	page.on("pageerror", (e) => messages.push(`pageerror: ${e.message}`));

	await stubApi(page);

	// SSR entry on a `_protected` route OUTSIDE `_dashboard`. This step passes
	// even against the defective build — beforeLoad has not run on the client yet.
	await page.goto("/cli-authorize");
	const backToDashboard = page.locator(BACK_TO_DASHBOARD);
	await expect(backToDashboard).toBeVisible();

	// Property 2: wait until React has attached to that specific anchor, so the
	// click is intercepted by the router instead of falling through to a native
	// document GET.
	await page.waitForFunction((selector) => {
		const a = document.querySelector(selector);
		return Boolean(a && Object.keys(a).some((k) => k.startsWith("__reactProps")));
	}, BACK_TO_DASHBOARD);

	// Property 3: marker that only survives a client-side transition.
	await page.evaluate(() => {
		(window as unknown as { __gateNavProbe?: string }).__gateNavProbe = "client";
	});

	// The step that wedges: the router enters `_dashboard` and its beforeLoad
	// awaits `getAccessState()` in the browser.
	await backToDashboard.click();

	// Commit signals. The title is the precise wedge signature: when wedged the
	// URL updates but the destination match never resolves, so the head never
	// re-renders — and `page.title()` cannot even be answered.
	await expect(page).toHaveURL(/^https?:\/\/[^/]+\/$/);
	await expect(page).toHaveTitle("Overview · iforge");
	await expect(page.getByTestId("app-sidebar")).toBeVisible();
	await expect(
		page.getByRole("heading", { level: 1, name: /^(Good (morning|afternoon|evening)|Welcome)/ }),
	).toBeVisible();

	// Direct liveness probe: a starved main thread cannot answer this.
	expect(await page.evaluate(() => 1 + 1)).toBe(2);

	// No document reload happened, so `_dashboard`'s beforeLoad really did run in
	// the browser — the exact code path that used to wedge.
	expect(
		await page.evaluate(() => (window as unknown as { __gateNavProbe?: string }).__gateNavProbe),
	).toBe("client");

	// Dev-mode signature of a denied module swapped for the mock Proxy.
	const denied = messages.filter((m) => m.includes("import-protection"));
	expect(denied, `import-protection reported: ${denied.join(" | ")}`).toEqual([]);
});
