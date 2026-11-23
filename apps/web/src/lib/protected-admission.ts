import { ApiNetworkError } from "@/lib/api-errors";

/**
 * Fork-owned admission helpers for `_protected` (06.3 D-03, G-06.3-R2-2).
 *
 * Root cause (.planning/debug/new-user-error-page-before-locked-shell.md):
 * `_protected`'s beforeLoad turned ANY rejected getAuthState server-function
 * fetch into `ApiNetworkError("offline")`, with no retry. That error reached
 * `_protected`'s errorComponent (RootError), and because `_protected` is the
 * parent match, "Page unavailable" replaced the whole tree before the
 * `_dashboard` gate could render the locked shell. A same-tab sign-out then
 * sign-up runs getAuthState up to three times in about a second, so one
 * dropped socket or dev-server restart blip was enough.
 *
 * `_dashboard`'s gate fails open on the identical hop failure, because it only
 * produces a verdict (`getAccessOutcome` answers `unreachable`). `_protected`
 * cannot: it decides whether there is an identity at all, so it stays
 * fail-closed. The fix is therefore a bounded retry of the transport, never a
 * fallback identity:
 *
 * - Only a rejected fetch is retried. Any HTTP answer, including an error
 *   status, is the server's decision and passes through unchanged.
 * - Three attempts with waits of 250 ms then 750 ms fit inside about a second.
 *   That covers a dropped socket or a dev-server restart blip; a longer outage
 *   still ends on RootError, whose Try again recovers.
 * - An aborted request is never retried: the caller has given up on it. Its
 *   abort is rethrown as is, not classified as offline, and a wait between
 *   attempts ends as soon as the signal aborts (06.3 IN-05).
 * - Every retry re-sends the same input and init, so the server sees the same
 *   request and requireRouteIdentity still decides on its answer alone.
 */

type FetchImpl = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export const AUTH_STATE_RETRY_DELAYS_MS: readonly number[] = [250, 750];

/** Waits `ms`, or less: an abort of `signal` ends the wait at once (06.3 IN-05). */
function defaultSleep(ms: number, signal?: AbortSignal | null): Promise<void> {
	return new Promise<void>((resolve) => {
		if (signal?.aborted) {
			resolve();
			return;
		}
		const done = () => {
			clearTimeout(timer);
			signal?.removeEventListener("abort", done);
			resolve();
		};
		const timer = setTimeout(done, ms);
		signal?.addEventListener("abort", done, { once: true });
	});
}

export function createAuthStateFetch(
	options: {
		fetchImpl?: FetchImpl;
		delaysMs?: readonly number[];
		sleep?: (ms: number, signal?: AbortSignal | null) => Promise<void>;
	} = {},
): FetchImpl {
	// Resolve the global fetch at call time, so a later polyfill or test stub is honored.
	const fetchImpl: FetchImpl = options.fetchImpl ?? ((input, init) => fetch(input, init));
	const delaysMs = options.delaysMs ?? AUTH_STATE_RETRY_DELAYS_MS;
	const sleep = options.sleep ?? defaultSleep;
	return async (input, init) => {
		for (let attempt = 0; ; attempt += 1) {
			try {
				return await fetchImpl(input, init);
			} catch (cause: unknown) {
				// iforge (06.3 IN-05): a superseded request is an abort, not an outage.
				if (init?.signal?.aborted) throw cause;
				const delay = delaysMs[attempt];
				if (delay === undefined) throw new ApiNetworkError("offline", { cause });
				await sleep(delay, init?.signal);
				init?.signal?.throwIfAborted();
			}
		}
	};
}

export const authStateFetch = createAuthStateFetch();

/**
 * True when the loaded `/_protected` match (that exact routeId, not a child)
 * finished in status success with `context.authIdentity` strictly equal to
 * `identity`. That match was admitted by a server-verified getAuthState for
 * this identity, so re-running `_protected` would only repeat that read.
 * A null identity (signed out) is never admitted.
 */
export function protectedMatchAdmits(
	matches: ReadonlyArray<{ routeId: string; status: string; context?: unknown }>,
	identity: string | null,
): boolean {
	if (typeof identity !== "string") return false;
	const layout = matches.find((match) => match.routeId === "/_protected");
	if (layout?.status !== "success") return false;
	const context = layout.context;
	if (typeof context !== "object" || context === null) return false;
	const admitted = (context as { authIdentity?: unknown }).authIdentity;
	return typeof admitted === "string" && admitted === identity;
}
