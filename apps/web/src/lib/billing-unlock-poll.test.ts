import { describe, expect, test } from "bun:test";
import { fileURLToPath } from "node:url";
import type { AccessStatusOutcome } from "@/lib/access-gate";
import {
	runUnlockPoll,
	UNLOCK_POLL_SLOW_INTERVAL_MS,
	UNLOCK_POLL_TIMEOUT_MS,
	UNLOCKED_TOAST_DESCRIPTION,
	UNLOCKED_TOAST_TITLE,
	unlockPollDelayMs,
} from "@/lib/billing-unlock-poll";
import { uiSpecRowQuotes } from "@/lib/ui-spec-contract";

/**
 * 06.3 D-04 (UI-SPEC L9) — the unlock poll engine. Driven by an injected fake
 * clock and a recording sleep: no real timers run in this file.
 */
const UI_SPEC_06_3 = fileURLToPath(
	new URL(
		"../../../../.planning/phases/06.3-align-access-and-billing-gating-with-upstream-clerk-managed/06.3-UI-SPEC.md",
		import.meta.url,
	),
);

const billingRequired: AccessStatusOutcome = { outcome: "resolved", state: "billing_required" };
const provisioned: AccessStatusOutcome = { outcome: "resolved", state: "provisioned" };
const terminated: AccessStatusOutcome = { outcome: "resolved", state: "terminated" };

/**
 * A fake clock whose `sleep` records each delay and advances time by it.
 * `fetchOutcome` replays `outcomes` (the last one repeats) and records the
 * elapsed time of every fetch.
 */
function harness(outcomes: AccessStatusOutcome[], controller = new AbortController()) {
	let clock = 1_000_000;
	const start = clock;
	const sleeps: number[] = [];
	const fetchTimes: number[] = [];
	let onSleep: ((ms: number) => void) | null = null;
	const deps = {
		fetchOutcome: async () => {
			fetchTimes.push(clock - start);
			const next = outcomes[Math.min(fetchTimes.length - 1, outcomes.length - 1)];
			return next as AccessStatusOutcome;
		},
		sleep: async (ms: number) => {
			sleeps.push(ms);
			onSleep?.(ms);
			clock += ms;
		},
		now: () => clock,
		signal: controller.signal,
	};
	return {
		deps,
		sleeps,
		fetchTimes,
		controller,
		setOnSleep(fn: (ms: number) => void) {
			onSleep = fn;
		},
	};
}

describe("unlockPollDelayMs (L9 cadence)", () => {
	test("polls every 3 s at the start", () => {
		expect(unlockPollDelayMs(0)).toBe(3000);
	});

	test("polls every 3 s until 30 s", () => {
		expect(unlockPollDelayMs(29_999)).toBe(3000);
	});

	test("polls every 10 s from 30 s", () => {
		expect(unlockPollDelayMs(30_000)).toBe(10000);
	});

	test("polls every 10 s until 3 minutes", () => {
		expect(unlockPollDelayMs(179_999)).toBe(10000);
	});

	test("stops at 3 minutes", () => {
		expect(unlockPollDelayMs(180_000)).toBeNull();
	});
});

describe("runUnlockPoll (L9)", () => {
	test("refused, unreachable and billing_required count as not yet; provisioned unlocks", async () => {
		const h = harness([
			billingRequired,
			{ outcome: "refused", status: 503 },
			{ outcome: "unreachable" },
			provisioned,
		]);
		const result = await runUnlockPoll(h.deps);
		expect(result).toBe("unlocked");
		expect(h.fetchTimes).toHaveLength(4);
		expect(h.sleeps).toEqual([3000, 3000, 3000]);
	});

	test("never provisioned times out after a last fetch at 3 minutes", async () => {
		const h = harness([billingRequired]);
		const result = await runUnlockPoll(h.deps);
		expect(result).toBe("timed_out");
		expect(h.fetchTimes.at(-1)).toBe(UNLOCK_POLL_TIMEOUT_MS);
		expect(Math.max(...h.sleeps)).toBeLessThanOrEqual(UNLOCK_POLL_SLOW_INTERVAL_MS);
	});

	test("terminated stops the poll after one fetch", async () => {
		const h = harness([terminated]);
		const result = await runUnlockPoll(h.deps);
		expect(result).toBe("terminated");
		expect(h.fetchTimes).toHaveLength(1);
	});

	test("aborting during a sleep ends the poll with no further fetch", async () => {
		const h = harness([billingRequired]);
		h.setOnSleep(() => h.controller.abort());
		const result = await runUnlockPoll(h.deps);
		expect(result).toBe("aborted");
		expect(h.fetchTimes).toHaveLength(1);
	});
});

describe("unlocked toast copy (L9)", () => {
	test("title and description equal the UI-SPEC row 'L9 unlocked toast'", () => {
		expect([UNLOCKED_TOAST_TITLE, UNLOCKED_TOAST_DESCRIPTION]).toEqual(
			uiSpecRowQuotes("L9 unlocked toast", UI_SPEC_06_3),
		);
	});
});
