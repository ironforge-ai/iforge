import { describe, expect, test } from "bun:test";
import {
	BILLING_REQUIRED_CODE,
	createBillingRequiredSignal,
	createGateRecheckController,
	GATE_RECHECK_MIN_INTERVAL_MS,
	isBillingRequiredRefusal,
} from "./billing-required-signal";

/**
 * G-06.3-1b (06.3 D-03): only a real billing refusal re-runs the dashboard
 * gate, and the re-run never loops. Real `Response` objects, a fake clock and
 * recording fakes; no real timers.
 */

function jsonResponse(status: number, body: unknown): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { "content-type": "application/json" },
	});
}

const REFUSAL_BODY = { detail: { code: BILLING_REQUIRED_CODE } };

function refusal(): Response {
	return jsonResponse(403, REFUSAL_BODY);
}

describe("isBillingRequiredRefusal", () => {
	test("403 with detail.code billing_required is a refusal", async () => {
		expect(await isBillingRequiredRefusal(refusal())).toBe(true);
	});

	test("403 with a top-level code billing_required is a refusal", async () => {
		expect(await isBillingRequiredRefusal(jsonResponse(403, { code: "billing_required" }))).toBe(
			true,
		);
	});

	test("403 with another detail code is not", async () => {
		expect(
			await isBillingRequiredRefusal(jsonResponse(403, { detail: { code: "another_code" } })),
		).toBe(false);
	});

	test("403 with a non-JSON body is not", async () => {
		const response = new Response("<html>forbidden</html>", {
			status: 403,
			headers: { "content-type": "text/html" },
		});
		expect(await isBillingRequiredRefusal(response)).toBe(false);
	});

	test("401 account_suspended is not", async () => {
		const response = new Response(
			JSON.stringify({
				type: "urn:clawdi:problem:account-suspended",
				title: "Account suspended",
				status: 401,
				detail: "Account is suspended",
				code: "account_suspended",
			}),
			{ status: 401, headers: { "content-type": "application/problem+json" } },
		);
		expect(await isBillingRequiredRefusal(response)).toBe(false);
	});

	test("503 billing_unavailable is not", async () => {
		expect(
			await isBillingRequiredRefusal(
				jsonResponse(503, { detail: { code: "billing_unavailable" } }),
			),
		).toBe(false);
	});

	test("200 is not", async () => {
		expect(await isBillingRequiredRefusal(jsonResponse(200, { ok: true }))).toBe(false);
	});
});

describe("createBillingRequiredSignal", () => {
	test("observing a refusal leaves the caller's response body readable", async () => {
		const response = refusal();
		expect(await createBillingRequiredSignal().observeResponse(response)).toBe(true);
		expect(await response.json()).toEqual(REFUSAL_BODY);
	});

	test("every subscriber is called once per refusal", async () => {
		const signal = createBillingRequiredSignal();
		let first = 0;
		let second = 0;
		signal.subscribe(() => {
			first += 1;
		});
		signal.subscribe(() => {
			second += 1;
		});
		expect(await signal.observeResponse(refusal())).toBe(true);
		expect([first, second]).toEqual([1, 1]);
	});

	test("an unsubscribed listener is not called", async () => {
		const signal = createBillingRequiredSignal();
		let calls = 0;
		const unsubscribe = signal.subscribe(() => {
			calls += 1;
		});
		unsubscribe();
		expect(await signal.observeResponse(refusal())).toBe(true);
		expect(calls).toBe(0);
	});

	test("a non-refusal notifies nobody and resolves false", async () => {
		const signal = createBillingRequiredSignal();
		let calls = 0;
		signal.subscribe(() => {
			calls += 1;
		});
		expect(await signal.observeResponse(jsonResponse(500, { detail: "boom" }))).toBe(false);
		expect(calls).toBe(0);
	});
});

function recordingController(options: { pending?: boolean } = {}) {
	let clock = 0;
	const calls: string[] = [];
	let release: () => void = () => {};
	const controller = createGateRecheckController({
		forgetVerdict: () => {
			calls.push("forget");
		},
		rerunGate: () => {
			calls.push("rerun");
			if (!options.pending) return Promise.resolve();
			return new Promise<void>((resolve) => {
				release = resolve;
			});
		},
		now: () => clock,
		minIntervalMs: 30_000,
	});
	return {
		controller,
		calls,
		setClock: (value: number) => {
			clock = value;
		},
		release: () => release(),
	};
}

describe("createGateRecheckController (T-06.3-15-01)", () => {
	test("the minimum interval is 30 s", () => {
		expect(GATE_RECHECK_MIN_INTERVAL_MS).toBe(30_000);
	});

	test("the first unlocked refusal forgets the verdict, then re-runs the gate", async () => {
		const { controller, calls } = recordingController();
		expect(await controller.onRefusal(false)).toBe("rechecked");
		expect(calls).toEqual(["forget", "rerun"]);
	});

	test("a second refusal inside the interval is skipped", async () => {
		const { controller, calls, setClock } = recordingController();
		setClock(0);
		expect(await controller.onRefusal(false)).toBe("rechecked");
		setClock(29_999);
		expect(await controller.onRefusal(false)).toBe("skipped_recent");
		expect(calls).toEqual(["forget", "rerun"]);
	});

	test("a refusal once the interval has elapsed re-runs again", async () => {
		const { controller, calls, setClock } = recordingController();
		setClock(0);
		expect(await controller.onRefusal(false)).toBe("rechecked");
		setClock(30_000);
		expect(await controller.onRefusal(false)).toBe("rechecked");
		expect(calls).toEqual(["forget", "rerun", "forget", "rerun"]);
	});

	test("a refusal while a re-run is still pending is skipped", async () => {
		const { controller, calls, release } = recordingController({ pending: true });
		const first = controller.onRefusal(false);
		expect(await controller.onRefusal(false)).toBe("skipped_in_flight");
		release();
		expect(await first).toBe("rechecked");
		expect(calls).toEqual(["forget", "rerun"]);
	});

	test("while locked nothing is forgotten or re-run", async () => {
		const { controller, calls } = recordingController();
		expect(await controller.onRefusal(true)).toBe("skipped_locked");
		expect(calls).toEqual([]);
	});
});
