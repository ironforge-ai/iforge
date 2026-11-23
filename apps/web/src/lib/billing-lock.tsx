"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import {
	createContext,
	type ReactNode,
	useCallback,
	useContext,
	useEffect,
	useMemo,
	useRef,
	useState,
} from "react";
import { toast } from "sonner";
import { fetchAccessStatusOutcome } from "@/lib/access-gate";
import { forgetProvisionedVerdict } from "@/lib/access-verdict-memo";
import { useAuthToken } from "@/lib/auth-client";
import {
	createGateRecheckController,
	useBillingRequiredSignal,
} from "@/lib/billing-required-signal";
import {
	runUnlockPoll,
	sleepUnlessAborted,
	UNLOCKED_TOAST_DESCRIPTION,
	UNLOCKED_TOAST_TITLE,
} from "@/lib/billing-unlock-poll";

/**
 * Billing lock for the dashboard shell (06.3 D-03, UI-SPEC L1).
 *
 * `_dashboard` mounts `BillingLockProvider` around `DashboardLayout` for BOTH
 * locked and unlocked renders (value `false` when unlocked), so an unlock swaps
 * only the content child and never remounts the layout or closes an open
 * Settings dialog.
 *
 * The provider also hosts the unlock poll (06.3 D-04, UI-SPEC L9). It lives
 * here, in the shell, rather than in Settings → Compute, so closing Settings
 * or unmounting the Compute panel mid-poll still unlocks the dashboard.
 *
 * It also turns a gated 403 `billing_required` into a gate re-run while
 * unlocked (G-06.3-1b): a stale unlocked verdict (the provisioned memo, or an
 * exemption revoked mid-session) would otherwise leave every gated surface on a
 * generic load error. The identity scope's `BillingRequiredSignal` reports the
 * refusal; the bounded recheck controller forgets the provisioned verdict and
 * calls `router.invalidate()`, so the gate renders the locked shell. While
 * locked nothing re-runs: the unlock poll owns re-reads there.
 *
 * UX only (T-03-13): nothing here grants or withholds data. Every gated `/v1`
 * route still answers 403 `billing_required` server-side, and an unlock only
 * re-runs the `_dashboard` gate (`router.invalidate()`), which re-reads access
 * status from the server (T-06.3-09-04).
 *
 * OSS-safe: imports nothing from the hosted source tree.
 */
const BillingLockedContext = createContext(false);

/** L9: idle (no poll), confirming (poll running), still confirming (timed out). */
export type UnlockPollState = "idle" | "confirming" | "still_confirming";

export type BillingUnlockPoll = {
	state: UnlockPollState;
	/** Start the poll after a payment. No-op unless locked and not already confirming. */
	start(): void;
	/** "Check again": restart the 3-minute poll at the 3 s cadence. */
	retry(): void;
};

const NOOP = () => {};

/** Outside a provider (OSS pages, tests, unlocked trees): idle, nothing to start. */
const IDLE_POLL: BillingUnlockPoll = { state: "idle", start: NOOP, retry: NOOP };

const BillingUnlockPollContext = createContext<BillingUnlockPoll>(IDLE_POLL);

export function BillingLockProvider({
	locked,
	children,
}: {
	locked: boolean;
	children: ReactNode;
}) {
	const poll = useUnlockPollHost(locked);
	useBillingRequiredRecheck(locked);
	return (
		<BillingLockedContext.Provider value={locked}>
			<BillingUnlockPollContext.Provider value={poll}>{children}</BillingUnlockPollContext.Provider>
		</BillingLockedContext.Provider>
	);
}

/** Whether the current dashboard render is billing-locked. Defaults to `false`. */
export function useBillingLocked(): boolean {
	return useContext(BillingLockedContext);
}

/** The L9 unlock poll hosted by `BillingLockProvider`. Defaults to idle no-ops. */
export function useBillingUnlockPoll(): BillingUnlockPoll {
	return useContext(BillingUnlockPollContext);
}

function useUnlockPollHost(locked: boolean): BillingUnlockPoll {
	const { getToken } = useAuthToken();
	const queryClient = useQueryClient();
	const router = useRouter();
	const [state, setState] = useState<UnlockPollState>("idle");
	// The running poll's controller; `null` when no poll is in flight.
	const controllerRef = useRef<AbortController | null>(null);
	// L9.1: window focus only re-checks once a poll has started in this page load.
	const startedRef = useRef(false);
	const lockedRef = useRef(locked);
	const getTokenRef = useRef(getToken);

	useEffect(() => {
		lockedRef.current = locked;
	}, [locked]);
	useEffect(() => {
		getTokenRef.current = getToken;
	}, [getToken]);

	const begin = useCallback(() => {
		if (!lockedRef.current || controllerRef.current !== null) return;
		const controller = new AbortController();
		controllerRef.current = controller;
		startedRef.current = true;
		setState("confirming");
		void runUnlockPoll({
			fetchOutcome: () => fetchAccessStatusOutcome(() => getTokenRef.current()),
			sleep: sleepUnlessAborted,
			now: Date.now,
			signal: controller.signal,
		}).then(async (result) => {
			if (controllerRef.current === controller) controllerRef.current = null;
			if (result === "aborted") return;
			if (result === "timed_out") {
				setState("still_confirming");
				return;
			}
			try {
				if (result === "unlocked") {
					// The only success claim on any surface, and only after access
					// status reads `provisioned` (T-06.3-09-02). The state stays
					// `confirming` until the gate re-renders unlocked, which resets it.
					toast.success(UNLOCKED_TOAST_TITLE, { description: UNLOCKED_TOAST_DESCRIPTION });
					await queryClient.invalidateQueries();
					// beforeLoad resolves provisioned; the page replaces the locked panel.
					await router.invalidate();
					return;
				}
				// terminated: the gate re-runs and redirects to /deactivated.
				await router.invalidate();
			} catch {
				// The gate could not re-run; offer "Check again" rather than leaving
				// the CTAs disabled behind a confirming alert with no poll running.
				setState("still_confirming");
			}
		});
	}, [queryClient, router]);

	// Leaving the locked state (unlock, or a gate redirect) ends any poll.
	useEffect(() => {
		if (locked) return;
		controllerRef.current?.abort();
		controllerRef.current = null;
		setState("idle");
	}, [locked]);

	// Unmount ends any poll.
	useEffect(
		() => () => {
			controllerRef.current?.abort();
			controllerRef.current = null;
		},
		[],
	);

	// L9.1: coming back to the tab while locked re-checks, once a poll has
	// started in this page load (a returning payer, or a stalled confirmation).
	useEffect(() => {
		if (!locked) return;
		const onFocus = () => {
			if (startedRef.current) begin();
		};
		window.addEventListener("focus", onFocus);
		return () => window.removeEventListener("focus", onFocus);
	}, [locked, begin]);

	return useMemo(() => ({ state, start: begin, retry: begin }), [state, begin]);
}

/**
 * G-06.3-1b: re-run the `_dashboard` gate when a gated request is refused with
 * 403 `billing_required` while the shell renders unlocked. The controller is
 * keyed on the identity scope's signal (and the router), so a new identity gets
 * a fresh controller and one identity's refusals never re-run another's gate.
 */
function useBillingRequiredRecheck(locked: boolean): void {
	const signal = useBillingRequiredSignal();
	const router = useRouter();
	const lockedRef = useRef(locked);

	useEffect(() => {
		lockedRef.current = locked;
	}, [locked]);

	// `signal` is the identity scope: a new scope starts a fresh controller.
	const controller = useMemo(
		() =>
			createGateRecheckController({
				forgetVerdict: forgetProvisionedVerdict,
				rerunGate: () => router.invalidate(),
				now: Date.now,
			}),
		[signal, router],
	);

	useEffect(() => {
		if (!signal) return;
		return signal.subscribe(() => {
			void controller.onRefusal(lockedRef.current);
		});
	}, [signal, controller]);
}
