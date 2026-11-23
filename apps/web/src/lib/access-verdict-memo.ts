/**
 * Identity-keyed, browser-only memo of a provisioned dashboard gate verdict
 * (06.3 D-03, G-06.3-1).
 *
 * Once the `_dashboard` gate has resolved `provisioned` for an identity in this
 * browser document, later client navigations by THAT identity skip the
 * `GET /v1/access/status` round trip.
 *
 * Why it is keyed: Clerk's sign-out, sign-in and sign-up are client-side
 * TanStack navigations, so module state outlives an identity switch. The
 * identity-blind boolean this replaces let a brand-new unpaid user, signing up
 * in a tab that had already resolved `provisioned` for someone else, land on
 * the unlocked dashboard. The key is `_protected`'s `authIdentity` (the JSON of
 * `[userId, sessionId]`), resolved on every navigation, so a different user or
 * session never matches.
 *
 * Why it is browser-only: server module scope is shared across users and
 * requests, so a verdict recorded during SSR would leak into another user's
 * gate. `rememberProvisionedVerdict` is a no-op without `window`, by
 * construction rather than by caller discipline.
 *
 * A null identity (the desktop build, where `_protected` returns
 * `authIdentity: null`) is never recorded and never matches.
 *
 * UX only (T-03-13): the memo grants no data access. Every gated `/v1` route
 * still answers 403 `billing_required` server-side for a locked account. Plan
 * 06.3-15 calls `forgetProvisionedVerdict` when a gated request is refused, so
 * the next navigation re-asks.
 */

let provisionedIdentity: string | null = null;

/** Records `identity` as provisioned, in a browser document only. */
export function rememberProvisionedVerdict(identity: string | null): void {
	if (identity === null || typeof window === "undefined") return;
	provisionedIdentity = identity;
}

/** True only when `identity` is non-null and is the one last recorded. */
export function hasProvisionedVerdict(identity: string | null): boolean {
	return identity !== null && identity === provisionedIdentity;
}

/** Clears the memo, so the next gate run asks again. */
export function forgetProvisionedVerdict(): void {
	provisionedIdentity = null;
}
