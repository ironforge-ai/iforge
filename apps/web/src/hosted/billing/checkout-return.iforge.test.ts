import { afterEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { QueryClient } from "@tanstack/react-query";
import { runCheckoutReturn } from "@/hosted/billing/checkout-return";
import { BillingApiError } from "@/hosted/billing/errors";
import { refreshCheckoutReturnQueries } from "@/hosted/billing/hooks";
import { billingKeys } from "@/hosted/billing/query-keys";
import { PLAN_CHECKOUT_CANCEL_COPY } from "@/hosted/billing/subscription/billing-lock-banner";

/**
 * Fork-owned pins for G-06.3-R2-3 (D-04): backing out of a checkout reads
 * "Checkout canceled", never "Couldn’t refresh checkout status", even while the
 * hosted service answers GET /v2/deployments with 503 compute_unavailable.
 *
 * Staging runs with compute unset, so every compute proxy route, including the
 * deployments read, answers 503 compute_unavailable by design ("Your Wallet and
 * subscriptions still work"). These cases drive the REAL
 * refreshCheckoutReturnQueries over a real QueryClient whose deployments query
 * fails exactly that way. Plan 06.3-09's check ran against the mock deploy API,
 * which answers /v2/deployments 200, which is why it missed the defect
 * (.planning/debug/checkout-cancel-refresh-error-toast.md).
 *
 * Source pins read the shipped code with comments stripped, so a comment cannot
 * satisfy a pin, and every extracted slice is asserted non-empty and unique.
 *
 * This file lives beside the upstream checkout-return code, not in the
 * upstream-tracked checkout-return.test.ts, so an upstream merge cannot drop it
 * (03.1 D-02).
 */

const DEPLOY_CANCEL_COPY = "You were not charged. Your Agent was not deployed.";
const REFRESHED_DESCRIPTION = "We checked your subscription and wallet.";
const REFRESH_FAILED_TITLE = "Couldn’t refresh checkout status";

/** subscription-page.tsx's owner for a plan-only return: it never navigates. */
const NO_NAVIGATION_OWNER = () => false;

type Notification = { kind: "error" | "message"; title: string; description: string };

function computeUnavailable(): BillingApiError {
	return new BillingApiError(503, "", {
		detail: {
			code: "compute_unavailable",
			message: "Agent compute is not available right now.",
		},
	});
}

/**
 * A real QueryClient (retry false) seeded the way hooks.test.ts seeds it:
 * wallet, plans and subscriptions resolve; deployments always throws the hosted
 * compute_unavailable error and counts its calls.
 */
async function hostedWithComputeUnset() {
	const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	let deploymentsCalls = 0;
	await qc.prefetchQuery({
		queryKey: billingKeys.wallet,
		queryFn: async () => ({ balance_cents: 0 }),
	});
	await qc.prefetchQuery({
		queryKey: billingKeys.plans,
		queryFn: async () => [{ id: "plan_basic" }],
	});
	await qc.prefetchQuery({
		queryKey: billingKeys.subscriptions,
		queryFn: async () => ({ pages: [[]], pageParams: [null] }),
	});
	await qc.prefetchQuery({
		queryKey: billingKeys.deployments,
		queryFn: async () => {
			deploymentsCalls += 1;
			throw computeUnavailable();
		},
	});
	const refreshes: Promise<unknown>[] = [];
	const refresh = (options?: { includeDeployments?: boolean }) => {
		const pending = refreshCheckoutReturnQueries(qc, options);
		refreshes.push(pending);
		return pending;
	};
	return {
		qc,
		refresh,
		deploymentsCalls: () => deploymentsCalls,
		/** Wait for every refresh the flow started, foreground or background. */
		settled: () => Promise.allSettled(refreshes),
	};
}

function recorder() {
	const notifications: Notification[] = [];
	let consumed = 0;
	return {
		notifications,
		consumed: () => consumed,
		consumeMarkers: async () => {
			consumed += 1;
		},
		notify: {
			error: (title: string, options: { description: string }) => {
				notifications.push({ kind: "error", title, description: options.description });
			},
			message: (title: string, options: { description: string }) => {
				notifications.push({ kind: "message", title, description: options.description });
			},
		},
	};
}

const macrotask = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("a checkout cancel never depends on a refresh (G-06.3-R2-3)", () => {
	test("locked cancel with deployments left out: exactly the plan cancel toast", async () => {
		expect(PLAN_CHECKOUT_CANCEL_COPY).toBe("No payment was made. Choose a plan when you’re ready.");
		const hosted = await hostedWithComputeUnset();
		const sink = recorder();
		await runCheckoutReturn({
			searchStr: "?settings=billing-plan&checkout=cancel",
			onCancelCopy: PLAN_CHECKOUT_CANCEL_COPY,
			onNavigate: NO_NAVIGATION_OWNER,
			includeDeployments: false,
			refresh: hosted.refresh,
			consumeMarkers: sink.consumeMarkers,
			notify: sink.notify,
		});
		await hosted.settled();
		expect(sink.notifications).toEqual([
			{ kind: "message", title: "Checkout canceled", description: PLAN_CHECKOUT_CANCEL_COPY },
		]);
		expect(sink.consumed()).toBe(1);
		hosted.qc.clear();
	});

	test("locked cancel with deployments required (the UAT reproduction): still exactly the cancel toast", async () => {
		const hosted = await hostedWithComputeUnset();
		const sink = recorder();
		await runCheckoutReturn({
			searchStr: "?settings=billing-plan&checkout=cancel",
			onCancelCopy: PLAN_CHECKOUT_CANCEL_COPY,
			onNavigate: NO_NAVIGATION_OWNER,
			includeDeployments: true,
			refresh: hosted.refresh,
			consumeMarkers: sink.consumeMarkers,
			notify: sink.notify,
		});
		await hosted.settled();
		expect(sink.notifications).toEqual([
			{ kind: "message", title: "Checkout canceled", description: PLAN_CHECKOUT_CANCEL_COPY },
		]);
		expect(sink.consumed()).toBe(1);
		hosted.qc.clear();
	});

	test("unlocked /deploy cancel: exactly the deploy cancel toast, and the owner is never asked", async () => {
		const hosted = await hostedWithComputeUnset();
		const sink = recorder();
		const ownerCalls: unknown[] = [];
		await runCheckoutReturn({
			searchStr: "?checkout=cancel&next=%2F",
			onCancelCopy: DEPLOY_CANCEL_COPY,
			onNavigate: (target) => {
				ownerCalls.push(target);
				return true;
			},
			includeDeployments: true,
			refresh: hosted.refresh,
			consumeMarkers: sink.consumeMarkers,
			notify: sink.notify,
		});
		await hosted.settled();
		expect(sink.notifications).toEqual([
			{ kind: "message", title: "Checkout canceled", description: DEPLOY_CANCEL_COPY },
		]);
		expect(ownerCalls).toEqual([]);
		expect(sink.consumed()).toBe(1);
		hosted.qc.clear();
	});

	test("a cancel whose refresh never settles still resolves with the cancel toast", async () => {
		const sink = recorder();
		const run = runCheckoutReturn({
			searchStr: "?settings=billing-plan&checkout=cancel",
			onCancelCopy: PLAN_CHECKOUT_CANCEL_COPY,
			onNavigate: NO_NAVIGATION_OWNER,
			includeDeployments: false,
			refresh: () => new Promise<never>(() => {}),
			consumeMarkers: sink.consumeMarkers,
			notify: sink.notify,
		});
		const outcome = await Promise.race([
			run.then(() => "resolved" as const),
			new Promise<"pending">((resolve) => setTimeout(() => resolve("pending"), 50)),
		]);
		expect(outcome).toBe("resolved");
		expect(sink.notifications).toEqual([
			{ kind: "message", title: "Checkout canceled", description: PLAN_CHECKOUT_CANCEL_COPY },
		]);
	});

	describe("a cancel whose refresh rejects", () => {
		const unhandled: unknown[] = [];
		const onUnhandled = (reason: unknown) => {
			unhandled.push(reason);
		};
		afterEach(() => {
			process.off("unhandledRejection", onUnhandled);
		});

		test("shows no error and leaves no unhandled rejection", async () => {
			process.on("unhandledRejection", onUnhandled);
			const sink = recorder();
			await runCheckoutReturn({
				searchStr: "?settings=billing-plan&checkout=cancel",
				onCancelCopy: PLAN_CHECKOUT_CANCEL_COPY,
				onNavigate: NO_NAVIGATION_OWNER,
				includeDeployments: false,
				refresh: () => Promise.reject(new Error("refresh failed")),
				consumeMarkers: sink.consumeMarkers,
				notify: sink.notify,
			});
			await macrotask();
			expect(sink.notifications.filter((entry) => entry.kind === "error")).toEqual([]);
			expect(sink.notifications).toEqual([
				{ kind: "message", title: "Checkout canceled", description: PLAN_CHECKOUT_CANCEL_COPY },
			]);
			expect(unhandled).toEqual([]);
		});
	});
});

describe("a locked plan-only return leaves deployments out of the required refresh (G-06.3-R2-3)", () => {
	test("session return with includeDeployments false: Checkout status refreshed, deployments not read", async () => {
		const hosted = await hostedWithComputeUnset();
		const sink = recorder();
		const before = hosted.deploymentsCalls();
		await runCheckoutReturn({
			searchStr: "?settings=billing-plan&checkout_session_id=cs_test_return",
			onCancelCopy: PLAN_CHECKOUT_CANCEL_COPY,
			onNavigate: NO_NAVIGATION_OWNER,
			includeDeployments: false,
			refresh: hosted.refresh,
			consumeMarkers: sink.consumeMarkers,
			notify: sink.notify,
		});
		await hosted.settled();
		expect(sink.notifications).toEqual([
			{ kind: "message", title: "Checkout status refreshed", description: REFRESHED_DESCRIPTION },
		]);
		expect(hosted.deploymentsCalls()).toBe(before);
		expect(sink.consumed()).toBe(1);
		hosted.qc.clear();
	});

	test("guard: with deployments required, a failed refresh still says so and shows no success toast", async () => {
		const hosted = await hostedWithComputeUnset();
		const sink = recorder();
		await runCheckoutReturn({
			searchStr: "?settings=billing-plan&checkout_session_id=cs_test_return",
			onCancelCopy: PLAN_CHECKOUT_CANCEL_COPY,
			onNavigate: NO_NAVIGATION_OWNER,
			includeDeployments: true,
			refresh: hosted.refresh,
			consumeMarkers: sink.consumeMarkers,
			notify: sink.notify,
		});
		await hosted.settled();
		expect(sink.notifications).toEqual([
			{
				kind: "error",
				title: REFRESH_FAILED_TITLE,
				description: "Refresh the page to check your agents, subscription, and wallet.",
			},
		]);
		hosted.qc.clear();
	});

	test("a plan-only refresh failure names only what it read (IN-01)", async () => {
		const sink = recorder();
		await runCheckoutReturn({
			searchStr: "?settings=billing-plan&checkout_session_id=cs_test_return",
			onCancelCopy: PLAN_CHECKOUT_CANCEL_COPY,
			onNavigate: NO_NAVIGATION_OWNER,
			includeDeployments: false,
			refresh: () => Promise.reject(new Error("refresh failed")),
			consumeMarkers: sink.consumeMarkers,
			notify: sink.notify,
		});
		expect(sink.notifications).toEqual([
			{
				kind: "error",
				title: REFRESH_FAILED_TITLE,
				description: "Refresh the page to check your subscription and wallet.",
			},
		]);
	});

	test("a refresh that read deployments says it checked agents (IN-01)", async () => {
		const sink = recorder();
		const seen: unknown[] = [];
		await runCheckoutReturn({
			searchStr: "?checkout_session_id=cs_test_return",
			onCancelCopy: DEPLOY_CANCEL_COPY,
			onNavigate: NO_NAVIGATION_OWNER,
			includeDeployments: true,
			refresh: async (options) => {
				seen.push(options);
			},
			consumeMarkers: sink.consumeMarkers,
			notify: sink.notify,
		});
		expect(seen).toEqual([{ includeDeployments: true }]);
		expect(sink.notifications).toEqual([
			{
				kind: "message",
				title: "Checkout status refreshed",
				description: "We checked your agents, subscription, and wallet.",
			},
		]);
	});

	test("an owner that throws ends in the open-result error and the flow resolves", async () => {
		const hosted = await hostedWithComputeUnset();
		const sink = recorder();
		await expect(
			runCheckoutReturn({
				searchStr: "?deployment_id=hdep_return",
				onCancelCopy: DEPLOY_CANCEL_COPY,
				onNavigate: () => {
					throw new Error("owner failed");
				},
				includeDeployments: true,
				refresh: hosted.refresh,
				consumeMarkers: sink.consumeMarkers,
				notify: sink.notify,
			}),
		).resolves.toBeUndefined();
		await hosted.settled();
		expect(sink.notifications).toEqual([
			{
				kind: "error",
				title: "Couldn’t open the checkout result",
				description: "Refresh the page to try again.",
			},
		]);
		hosted.qc.clear();
	});
});

/** Source text with block and line comments removed. Neither file carries `//`
 * inside a string literal, so the line strip is safe here. */
function strippedSource(relative: string): string {
	const code = readFileSync(new URL(relative, import.meta.url), "utf8");
	return code
		.replace(/\/\*[\s\S]*?\*\//g, "")
		.split("\n")
		.map((line) => {
			const comment = line.indexOf("//");
			return comment === -1 ? line : line.slice(0, comment);
		})
		.join("\n");
}

/** Index of the one occurrence of `needle`; fails unless it occurs exactly once. */
function uniqueIndex(code: string, needle: string): number {
	expect(code.split(needle)).toHaveLength(2);
	return code.indexOf(needle);
}

/** From the opening bracket at `open` to its matching close, inclusive. */
function balanced(code: string, open: number, pair: "()" | "{}"): string {
	const [opener, closer] = pair;
	expect(code[open]).toBe(opener);
	let depth = 0;
	for (let i = open; i < code.length; i++) {
		if (code[i] === opener) depth++;
		else if (code[i] === closer) {
			depth--;
			if (depth === 0) return code.slice(open, i + 1);
		}
	}
	throw new Error("unbalanced brackets");
}

/** The parameter list and body of the one `function <name>(` in `code`. */
function functionParts(code: string, name: string): { params: string; body: string } {
	const start = uniqueIndex(code, `function ${name}(`);
	const paramsAt = start + `function ${name}`.length;
	const params = balanced(code, paramsAt, "()");
	const bodyAt = code.indexOf("{", paramsAt + params.length);
	expect(bodyAt).toBeGreaterThan(paramsAt);
	const body = balanced(code, bodyAt, "{}");
	expect(params.trim().length).toBeGreaterThan(2);
	expect(body.trim().length).toBeGreaterThan(2);
	return { params, body };
}

describe("checkout-return wiring (G-06.3-R2-3)", () => {
	test("useCheckoutReturnHandler delegates to runCheckoutReturn once and passes its includeDeployments through", () => {
		const { body } = functionParts(
			strippedSource("./checkout-return.ts"),
			"useCheckoutReturnHandler",
		);
		const callAt = uniqueIndex(body, "runCheckoutReturn(");
		const args = balanced(body, callAt + "runCheckoutReturn".length, "()");
		// The caller's option, forwarded as is: shorthand or `includeDeployments: includeDeployments`.
		expect(args).toMatch(/\bincludeDeployments(?:\s*:\s*includeDeployments)?\s*[,}]/);
		expect(args).not.toMatch(/\bincludeDeployments\s*:\s*(?:true|false)\b/);
	});

	test("useCheckoutReturnHandler declares includeDeployments as optional", () => {
		const { params } = functionParts(
			strippedSource("./checkout-return.ts"),
			"useCheckoutReturnHandler",
		);
		expect(params).toMatch(/\bincludeDeployments\?\s*:\s*boolean\b/);
	});

	test("LockedCheckoutReturn opts out of the deployments refresh with the no-navigation owner", () => {
		const { body } = functionParts(
			strippedSource("./subscription/subscription-page.tsx"),
			"LockedCheckoutReturn",
		);
		const callAt = uniqueIndex(body, "useCheckoutReturnHandler(");
		const args = balanced(body, callAt + "useCheckoutReturnHandler".length, "()");
		expect(args).toMatch(/\bincludeDeployments:\s*false\b/);
		expect(args).toMatch(/\bonNavigate:\s*NO_NAVIGATION_OWNER\b/);
	});
});
