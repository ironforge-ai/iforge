import type { AccessStatusOutcome } from "@/lib/access-gate";

/**
 * Unlock poll engine (06.3 D-04, UI-SPEC L9).
 *
 * After a payment made while billing-locked, the dashboard re-reads
 * `GET /v1/access/status` until it answers `provisioned`: every 3 s for the
 * first 30 s, then every 10 s, stopping at 3 minutes. A `refused` or
 * `unreachable` outcome, or a resolved `billing_required`, counts as "not yet"
 * and never stops the poll. A resolved `terminated` stops it so the gate can
 * send the user to `/deactivated`.
 *
 * Pure and injectable (fetch, sleep, clock, abort signal) so the cadence is
 * tested without real timers. OSS-safe: no React, nothing from the hosted
 * source tree. The poll only READS access status; unlock itself is decided
 * server-side and enforced on every gated route (T-06.3-09-04).
 */

export const UNLOCK_POLL_FAST_INTERVAL_MS = 3_000;
export const UNLOCK_POLL_FAST_WINDOW_MS = 30_000;
export const UNLOCK_POLL_SLOW_INTERVAL_MS = 10_000;
export const UNLOCK_POLL_TIMEOUT_MS = 180_000;

/** L9 unlocked toast, shown once when access status reads `provisioned`. */
export const UNLOCKED_TOAST_TITLE = "Plan active";
export const UNLOCKED_TOAST_DESCRIPTION =
	"iforge is unlocked. Deploy an agent whenever you’re ready.";

export type UnlockPollResult = "unlocked" | "terminated" | "timed_out" | "aborted";

export type UnlockPollDeps = {
	fetchOutcome: () => Promise<AccessStatusOutcome>;
	/** Resolves after `ms`, or early when `signal` aborts. Must not reject. */
	sleep: (ms: number, signal: AbortSignal) => Promise<void>;
	now: () => number;
	signal: AbortSignal;
};

/**
 * The wait before the next status read, given the time elapsed since the poll
 * started, or `null` once the 3-minute window has closed.
 */
export function unlockPollDelayMs(elapsedMs: number): number | null {
	if (elapsedMs >= UNLOCK_POLL_TIMEOUT_MS) return null;
	return elapsedMs < UNLOCK_POLL_FAST_WINDOW_MS
		? UNLOCK_POLL_FAST_INTERVAL_MS
		: UNLOCK_POLL_SLOW_INTERVAL_MS;
}

/**
 * Read access status until it is `provisioned`, `terminated`, the 3-minute
 * window closes, or `signal` aborts. Reads first, then waits.
 *
 * The final wait is clipped to the time left in the window, so the last read
 * lands at 3 minutes rather than up to 10 s past it (T-06.3-09-01).
 */
export async function runUnlockPoll({
	fetchOutcome,
	sleep,
	now,
	signal,
}: UnlockPollDeps): Promise<UnlockPollResult> {
	const startedAt = now();
	for (;;) {
		if (signal.aborted) return "aborted";
		let outcome: AccessStatusOutcome;
		try {
			outcome = await fetchOutcome();
		} catch {
			// `fetchAccessStatusOutcome` never throws; an injected fetch that does
			// is silence, which counts as "not yet" like `unreachable`.
			outcome = { outcome: "unreachable" };
		}
		if (signal.aborted) return "aborted";
		if (outcome.outcome === "resolved") {
			if (outcome.state === "provisioned") return "unlocked";
			if (outcome.state === "terminated") return "terminated";
		}
		const elapsed = now() - startedAt;
		const delay = unlockPollDelayMs(elapsed);
		if (delay === null) return "timed_out";
		await sleep(Math.min(delay, UNLOCK_POLL_TIMEOUT_MS - elapsed), signal);
	}
}

/**
 * Real-timer `sleep` for `runUnlockPoll`: resolves after `ms`, or as soon as
 * `signal` aborts, and never leaves a timer or listener behind.
 */
export function sleepUnlessAborted(ms: number, signal: AbortSignal): Promise<void> {
	return new Promise((resolve) => {
		if (signal.aborted) {
			resolve();
			return;
		}
		const done = () => {
			clearTimeout(timer);
			signal.removeEventListener("abort", done);
			resolve();
		};
		const timer = setTimeout(done, ms);
		signal.addEventListener("abort", done, { once: true });
	});
}
