import { createContext, useContext } from "react";
import { apiErrorCode } from "@/lib/api-errors";

/**
 * "Billing required" signal for the dashboard gate (06.3 D-03, G-06.3-1b).
 *
 * Why it exists: the billing lock is derived only from the `_dashboard`
 * `beforeLoad` answer. A stale unlocked verdict (the identity-keyed provisioned
 * memo in `access-verdict-memo.ts`, or an exemption revoked mid-session) used
 * to leave the shell unlocked while every gated surface showed a generic
 * per-card load error, because nothing turned a gated 403 `billing_required`
 * back into the locked shell. The cloud-api fetch path offers every response to
 * this signal; `BillingLockProvider` listens and, while unlocked, forgets the
 * provisioned verdict and re-runs the gate (`createGateRecheckController`).
 *
 * UX only (T-03-13): the signal grants and withholds nothing. Every gated `/v1`
 * route still answers 403 `billing_required` server-side; a refusal only makes
 * the client re-read `GET /v1/access/status` through the existing gate.
 *
 * Per identity scope: `AuthRouterBridge` creates a new signal with each
 * identity scope, beside the account-suspension store, so one identity's
 * refusals never re-run the gate for another.
 *
 * Only the status and the detail code are read. A refusal body is parsed from
 * `response.clone()` (the caller can still read the original) and is never
 * stored or logged.
 *
 * OSS-safe: imports nothing from the hosted source tree.
 */

export const BILLING_REQUIRED_CODE = "billing_required";

/** True only for a 403 whose JSON body carries `billing_required` as its code. */
export async function isBillingRequiredRefusal(response: Response): Promise<boolean> {
	if (response.status !== 403) return false;
	try {
		const body: unknown = await response.clone().json();
		return apiErrorCode(body) === BILLING_REQUIRED_CODE;
	} catch {
		return false;
	}
}

export type BillingRequiredSignal = {
	/** Notifies every listener when `response` is a billing refusal; resolves whether it was. */
	observeResponse(response: Response): Promise<boolean>;
	subscribe(listener: () => void): () => void;
};

export function createBillingRequiredSignal(): BillingRequiredSignal {
	const listeners = new Set<() => void>();
	return {
		async observeResponse(response) {
			if (!(await isBillingRequiredRefusal(response))) return false;
			for (const listener of listeners) listener();
			return true;
		},
		subscribe(listener) {
			listeners.add(listener);
			return () => {
				listeners.delete(listener);
			};
		},
	};
}

export const BillingRequiredSignalContext = createContext<BillingRequiredSignal | null>(null);

/** The identity scope's signal, or `null` outside `AuthRouterBridge` (tests, bare trees). */
export function useBillingRequiredSignal(): BillingRequiredSignal | null {
	return useContext(BillingRequiredSignalContext);
}

/**
 * Minimum time between two gate re-runs for one identity scope (T-06.3-15-01).
 * It bounds a disagreement between the gate and a gated route (for example a
 * backend process still holding a cached negative answer while
 * `GET /v1/access/status` reads provisioned) to one re-run per 30 s instead of
 * a loop. A new identity scope gets a new controller, so it starts fresh.
 */
export const GATE_RECHECK_MIN_INTERVAL_MS = 30_000;

export type GateRecheckResult =
	| "rechecked"
	| "skipped_locked"
	| "skipped_in_flight"
	| "skipped_recent"
	| "failed";

/**
 * Bounds the gate re-run a billing refusal triggers. While locked nothing
 * re-runs (the unlock poll owns re-reads there), at most one re-run is in
 * flight, and at most one starts per `minIntervalMs`. A failed re-run never
 * throws into React and frees the slot.
 */
export function createGateRecheckController({
	forgetVerdict,
	rerunGate,
	now,
	minIntervalMs = GATE_RECHECK_MIN_INTERVAL_MS,
}: {
	forgetVerdict: () => void;
	rerunGate: () => Promise<unknown>;
	now: () => number;
	minIntervalMs?: number;
}): { onRefusal(locked: boolean): Promise<GateRecheckResult> } {
	let inFlight = false;
	let lastStartedAt: number | null = null;
	return {
		async onRefusal(locked) {
			if (locked) return "skipped_locked";
			if (inFlight) return "skipped_in_flight";
			const startedAt = now();
			if (lastStartedAt !== null && startedAt - lastStartedAt < minIntervalMs) {
				return "skipped_recent";
			}
			inFlight = true;
			lastStartedAt = startedAt;
			try {
				forgetVerdict();
				await rerunGate();
				return "rechecked";
			} catch {
				console.error("Failed to re-run the dashboard access gate");
				return "failed";
			} finally {
				inFlight = false;
			}
		},
	};
}
