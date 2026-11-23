import type { paths } from "@clawdi/shared/api";
import createClient from "openapi-fetch";
import { env } from "@/lib/env";

/**
 * Typed client for `GET /v1/access/status` and the gate decisions over it.
 *
 * The endpoint answers for an authenticated-but-possibly-unprovisioned
 * identity, so this module stays free of the provisioning-coupled `useApi`
 * hook and of any server-only imports — it is safe to import from pages,
 * routes, and `bun test`.
 *
 * That last property is a hard constraint, not a convenience: it is also why
 * the access-status lookup does NOT reuse `lib/api-errors.ts`'s `ApiError`.
 * `api-errors.ts` imports `sonner` at line 1, which would pull a client-only
 * toast dependency into the SSR `beforeLoad` graph and into `bun test`.
 *
 * The status lookup returns a discriminated `AccessStatusOutcome` rather than
 * throwing (03-12 / G-03-3).
 *
 * The server answers one of three states (06.3 D-01: there is no invite step).
 * A resolved `billing_required` renders the dashboard shell with the billing
 * locked panel (`dashboardGateDecision` → `render-locked`) instead of
 * redirecting (06.3 D-03). Every gate here is UX only; enforcement is
 * server-side.
 */

export type AccessState = "provisioned" | "billing_required" | "terminated";

/**
 * Gate-screen target for a resolved access state. UX routing only (T-03-13) —
 * enforcement is server-side on the auth path (03-06).
 *
 * `_dashboard` does NOT route `billing_required` through this mapping: since
 * 06.3 D-03 `dashboardGateDecision` answers `render-locked` for it before this
 * function is consulted, so the dashboard shell renders with the locked panel.
 */
export function gateRedirectTarget(state: AccessState): "/deactivated" | null {
	if (state === "terminated") return "/deactivated";
	return null;
}

/**
 * Gate decision for `_protected/_dashboard`. `blocked` carries the status the
 * backend actually answered with, so the blocking screen can render
 * status-aware copy (a 401 reads as "sign in again", everything else as
 * "we couldn't verify your access"). `render-locked` renders the dashboard
 * shell with the billing locked panel in place of the page (06.3 D-03).
 */
export type DashboardGateDecision =
	| { kind: "render" }
	| { kind: "render-locked" }
	| { kind: "redirect"; to: "/deactivated" }
	| { kind: "redirect"; to: "/"; search: CheckoutReturnForward }
	| { kind: "blocked"; status: number };

/**
 * The search a billing-locked checkout return forwards to Settings → Compute
 * (06.3 L10.1). Built field by field from an allow-list, never copied from the
 * incoming search: only these two markers survive the hop, and the target path
 * is fixed (open-redirect guard, T-06.3-02-01).
 */
export type CheckoutReturnForward = {
	settings: "billing-plan";
	checkout_session_id?: string;
	checkout?: "cancel";
};

/** The `_dashboard` beforeLoad location the gate decides over. */
export type DashboardGateLocation = { pathname: string; search: Record<string, unknown> };

/**
 * Gate decision for `_protected/deactivated`. `status` is `null` when nothing
 * was resolved at all (dropped connection), because there is no status to show.
 */
export type DeactivatedGateDecision =
	| { kind: "render" }
	| { kind: "redirect"; to: "/" }
	| { kind: "blocked"; status: number | null };

function isDeployPath(pathname: string): boolean {
	return pathname === "/deploy" || pathname.startsWith("/deploy/");
}

/**
 * The allow-listed checkout-return forward for a billing-locked `/deploy` hit,
 * or `null` when there is nothing to forward. Only a non-empty string
 * `checkout_session_id` and `checkout` exactly `"cancel"` are recognised; every
 * other key in the incoming search is dropped.
 */
function lockedCheckoutReturnForward(
	location: DashboardGateLocation,
): CheckoutReturnForward | null {
	if (!isDeployPath(location.pathname)) return null;
	const sessionId = location.search.checkout_session_id;
	const hasSession = typeof sessionId === "string" && sessionId !== "";
	const canceled = location.search.checkout === "cancel";
	if (!hasSession && !canceled) return null;
	const forward: CheckoutReturnForward = { settings: "billing-plan" };
	if (hasSession) forward.checkout_session_id = sessionId;
	if (canceled) forward.checkout = "cancel";
	return forward;
}

/**
 * `_dashboard` gate decision (D-06). Refusals BLOCK, silence fails OPEN.
 *
 * `refused` covers every authoritative non-2xx — 401, 403, 429, 5xx, and
 * specifically the fail-closed 503 `billing_unavailable` the backend
 * deliberately propagates (`access.py`, D-09). Blocking on all of them is the
 * G-03-3 fix: before 03-12 every one of them reached this gate as an
 * undiscriminated throw and rendered the dashboard shell.
 *
 * `unreachable` still renders (review LOW #7): the answer never arrived, so no
 * gated state was resolved and there is nothing to mask.
 *
 * A RESOLVED `billing_required` renders the locked shell on every path (06.3
 * D-03): sidebar and header stay, the content region shows the "Choose a plan"
 * panel, and no page component mounts. It is never sent to `/deploy` —
 * checkout lives in Settings → Compute (D-04), so the old `/deploy` carve-out
 * is gone. The lock is UX only; every gated `/v1` route still answers
 * 403 `billing_required` server-side.
 *
 * The one exception is a redirect-based card checkout returning to `/deploy`
 * (L10.1): a locked `/deploy` carrying a checkout marker is sent to `/` with
 * `settings=billing-plan` and only that marker, so the Compute page can finish
 * the return. See `lockedCheckoutReturnForward`.
 */
export function dashboardGateDecision(
	outcome: AccessStatusOutcome,
	location: DashboardGateLocation,
): DashboardGateDecision {
	if (outcome.outcome === "refused") return { kind: "blocked", status: outcome.status };
	if (outcome.outcome === "unreachable") return { kind: "render" };
	if (outcome.state === "billing_required") {
		const forward = lockedCheckoutReturnForward(location);
		return forward ? { kind: "redirect", to: "/", search: forward } : { kind: "render-locked" };
	}
	const target = gateRedirectTarget(outcome.state);
	return target ? { kind: "redirect", to: target } : { kind: "render" };
}

/**
 * `/deactivated` gate decision. The S2 fenced screen renders ONLY when the
 * backend actually resolved `terminated` (T-03-38) — an unrelated 401 or a
 * dropped connection must never tell a healthy user their account is closed.
 * Both unresolved outcomes block instead of redirecting: redirecting to `/`
 * against a flapping backend could oscillate between the two routes.
 */
export function deactivatedGateDecision(outcome: AccessStatusOutcome): DeactivatedGateDecision {
	if (outcome.outcome === "refused") return { kind: "blocked", status: outcome.status };
	if (outcome.outcome === "unreachable") return { kind: "blocked", status: null };
	if (outcome.state === "terminated") return { kind: "render" };
	// 06.3 X3: a billing-locked account belongs on the locked dashboard shell.
	return { kind: "redirect", to: "/" };
}

export type AccessTokenGetter = () => Promise<string | null | undefined>;

/** Injectable `fetch` seam (openapi-fetch's `fetch` option), used by tests to
 * drive real non-2xx `Response` objects through the real client. */
export type GateFetch = (input: Request) => Promise<Response>;

function gateApi(fetchImpl?: GateFetch) {
	return createClient<paths>({
		baseUrl: env.VITE_IFORGE_API_URL,
		...(fetchImpl ? { fetch: fetchImpl } : {}),
	});
}

async function bearerHeaders(getToken: AccessTokenGetter): Promise<Record<string, string>> {
	const token = await getToken();
	return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Discriminated result of `GET /v1/access/status`.
 *
 * MUST stay a plain JSON-serializable object — no class instances, no `Error`.
 * It crosses the server-fn RPC boundary on every client-side navigation, and a
 * class identity does not survive that trip: `instanceof` would be false on the
 * browser side and the gate would silently revert to fail-open (T-03-40). This
 * is why 03-12 carries the status in a RESULT rather than in a thrown
 * `ApiError`-style subclass as `03-UAT.md` originally prescribed.
 */
export type AccessStatusOutcome =
	| { outcome: "resolved"; state: AccessState }
	| { outcome: "refused"; status: number }
	| { outcome: "unreachable" };

/**
 * Fetch the caller's access state without creating a User row. Never throws.
 *
 * `refused` means the backend answered and the answer was no — that is an
 * authoritative signal a gate must obey. `unreachable` means nothing was
 * resolved: the request never produced a response, or produced a bodyless 2xx.
 *
 * The `response.ok` test is load-bearing. The pre-03-12 `data === undefined`
 * check conflated a bodyless 2xx with a real refusal, and `openapi-fetch`
 * RETURNS on non-2xx rather than throwing, so this function is the sole
 * producer of the refusal signal.
 */
export async function fetchAccessStatusOutcome(
	getToken: AccessTokenGetter,
	fetchImpl?: GateFetch,
): Promise<AccessStatusOutcome> {
	let headers: Record<string, string>;
	try {
		headers = await bearerHeaders(getToken);
	} catch {
		// The token source itself failed: nothing was asked, let alone answered.
		return { outcome: "unreachable" };
	}
	// `.catch` is scoped to the request alone so a bug in the branching below
	// cannot masquerade as a transport failure. A rejection here is connection
	// refused / DNS / abort, or an unparseable 2xx body — silence, not refusal.
	const result = await gateApi(fetchImpl)
		.GET("/v1/access/status", { headers })
		.catch(() => null);
	if (result === null) return { outcome: "unreachable" };
	if (!result.response.ok) return { outcome: "refused", status: result.response.status };
	if (result.data === undefined) return { outcome: "unreachable" };
	return { outcome: "resolved", state: result.data.state };
}
