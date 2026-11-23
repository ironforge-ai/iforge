import { createServerFn } from "@tanstack/react-start";
import { type AccessStatusOutcome, fetchAccessStatusOutcome } from "@/lib/access-gate";
import { env } from "@/lib/env";

/**
 * Server-side access-state lookup for route `beforeLoad` gates.
 *
 * Lives apart from `access-gate.ts` so pages and tests can import the pure
 * client without pulling `createServerFn` / Clerk server modules into their
 * graph. Mirrors `_protected.tsx`'s auth pattern: Clerk server token (or the
 * dev-bypass bearer) → backend `GET /v1/access/status`.
 *
 * NAMING CONSTRAINT — do not rename this module to anything containing a
 * dot-delimited `server` segment (`*.server.ts`, `*.server.tsx`, …). TanStack
 * Start's client import protection denies `**\/*.server.*` by glob, and route
 * files import this module from the client bundle. Under `vite build` that
 * denial is a hard error (the whole web build fails); under `vite dev` it is
 * worse — the module is silently replaced by a mock Proxy whose `then` trap
 * resolves with itself, so `await getAccessState()` spins an unbounded
 * microtask cycle and permanently starves the browser's main thread on every
 * client-side navigation into the dashboard. The route's `try/catch` fail-open
 * never runs because the `await` never settles. Recorded in
 * `.planning/debug/deploy-route-hangs-browser.md` (gap G-03-4, fixed in 03-11).
 * The Clerk server import below stays behind a dynamic `await import()` inside
 * the handler precisely so this module is safe for the client graph.
 *
 * Returns a discriminated `AccessStatusOutcome` and does not throw for either
 * failure mode (03-12 / G-03-3). A refusal the backend actually produced
 * arrives as `refused` carrying its status; a request that never produced a
 * response arrives as `unreachable`. Before 03-12 both collapsed into one
 * undiscriminated throw, and every gate `beforeLoad` swallowed it — so an
 * authoritative 401, 403 or fail-closed 503 rendered the dashboard shell. This
 * docstring's earlier "throws on transport failure" summary is exactly where
 * that contract silently narrowed (T-03-41).
 */
export const getAccessState = createServerFn({ method: "GET" }).handler(
	async (): Promise<AccessStatusOutcome> =>
		fetchAccessStatusOutcome(async () => {
			if (env.VITE_DEV_AUTH_BYPASS) return env.VITE_DEV_AUTH_TOKEN;
			const { auth } = await import("@clerk/tanstack-react-start/server");
			const { getToken } = await auth();
			return getToken();
		}),
);

/**
 * Route-facing access lookup: the outcome, with the browser-to-web-server hop
 * folded in.
 *
 * The server fn above covers the web-server-to-cloud-api hop. On a client-side
 * navigation there is a SECOND transport in front of it — the RPC call itself —
 * which can drop independently (the web server restarts, the tab goes offline
 * mid-navigation). That hop resolved nothing either, so it maps to
 * `unreachable` and fails open downstream, exactly as review LOW #7 asked.
 *
 * Keeping it here means that fail-open lives in ONE place with an honest
 * comment, instead of being re-derived by three bare `catch {}` blocks that
 * could not tell it apart from a refusal.
 */
export async function getAccessOutcome(): Promise<AccessStatusOutcome> {
	try {
		return await getAccessState();
	} catch {
		return { outcome: "unreachable" };
	}
}
