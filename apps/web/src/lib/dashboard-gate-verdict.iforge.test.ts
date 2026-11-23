import { beforeEach, describe, expect, mock, spyOn, test } from "bun:test";
import type { AccessStatusOutcome } from "@/lib/access-gate";
import { forgetProvisionedVerdict } from "@/lib/access-verdict-memo";
import { ApiNetworkError } from "@/lib/api-errors";
import {
	createBillingRequiredSignal,
	createGateRecheckController,
	GATE_RECHECK_MIN_INTERVAL_MS,
	type GateRecheckResult,
} from "@/lib/billing-required-signal";
import { createAuthStateFetch } from "@/lib/protected-admission";
import { requireRouteIdentity } from "@/lib/route-auth";

/**
 * Fork-owned regression suite for G-06.3-1 (06.3 D-03): a different identity
 * signing in client-side, in a browser document that already resolved
 * `provisioned` for someone else, must be re-asked and, when unpaid, get the
 * locked shell.
 *
 * Clerk's sign-out, sign-in and sign-up are client-side TanStack navigations,
 * so `_dashboard`'s module state outlives an identity switch. Before this fix
 * the route kept an identity-blind "provisioned seen" boolean that
 * short-circuited the gate for whoever signed in next
 * (.planning/debug/new-user-locked-shell-landing.md).
 *
 * The suite drives the REAL `/_protected/_dashboard` `beforeLoad`. Only the
 * server-fn module is swapped, for a queue of canned outcomes plus a call
 * counter, so the route's own memo, decision and return shapes are what is
 * under test. An empty queue answers `unreachable` (the fail-open path).
 *
 * Bun keeps module mocks for the rest of the process. No other test imports
 * `@/lib/access-gate-server-fn`; the route is imported after the mock is
 * registered so it binds to the stub.
 *
 * `bun test` has no `window`. A case that needs the browser defines
 * `globalThis.window` and deletes it in `finally`; a case without it is SSR.
 *
 * The file lives beside the memo, not in an upstream-tracked test, so an
 * upstream merge cannot drop it (03.1 D-02). Plan 06.3-15 extends it with the
 * gated-403 re-entry (G-06.3-1b).
 */

let queue: AccessStatusOutcome[] = [];
let lookups = 0;

function nextOutcome(): AccessStatusOutcome {
	lookups += 1;
	return queue.shift() ?? { outcome: "unreachable" };
}

mock.module("@/lib/access-gate-server-fn", () => ({
	getAccessState: async () => nextOutcome(),
	getAccessOutcome: async () => nextOutcome(),
}));

const { Route } = await import("@/routes/_protected/_dashboard");

type GateContext = { accessBlockedStatus: number | null; billingLocked: boolean };

const PROVISIONED: AccessStatusOutcome = { outcome: "resolved", state: "provisioned" };
const BILLING_REQUIRED: AccessStatusOutcome = { outcome: "resolved", state: "billing_required" };

const IDENTITY_A = JSON.stringify(["user_a", "sess_a"]);
const IDENTITY_B = JSON.stringify(["user_b", "sess_b"]);

async function gate(authIdentity: string | null): Promise<GateContext> {
	const beforeLoad = Route.options.beforeLoad as unknown as (args: {
		location: { pathname: string; search: Record<string, unknown> };
		context: { authIdentity: string | null };
	}) => Promise<GateContext>;
	return beforeLoad({ location: { pathname: "/", search: {} }, context: { authIdentity } });
}

async function inBrowser(run: () => Promise<void>): Promise<void> {
	const hadWindow = "window" in globalThis;
	if (!hadWindow) (globalThis as Record<string, unknown>).window = globalThis;
	try {
		await run();
	} finally {
		if (!hadWindow) delete (globalThis as Record<string, unknown>).window;
	}
}

beforeEach(() => {
	forgetProvisionedVerdict();
	queue = [];
	lookups = 0;
});

describe("_dashboard gate verdict memo (G-06.3-1)", () => {
	test("a different identity in the same document is re-asked and lands locked", async () => {
		await inBrowser(async () => {
			queue = [PROVISIONED, BILLING_REQUIRED];
			expect(await gate(IDENTITY_A)).toEqual({ accessBlockedStatus: null, billingLocked: false });
			expect(await gate(IDENTITY_B)).toEqual({ accessBlockedStatus: null, billingLocked: true });
			expect(lookups).toBe(2);
		});
	});

	test("the same identity keeps the memo and skips the second lookup", async () => {
		await inBrowser(async () => {
			queue = [PROVISIONED];
			expect(await gate(IDENTITY_A)).toEqual({ accessBlockedStatus: null, billingLocked: false });
			expect(await gate(IDENTITY_A)).toEqual({ accessBlockedStatus: null, billingLocked: false });
			expect(lookups).toBe(1);
		});
	});

	test("SSR never memoizes: with no window every call asks", async () => {
		expect("window" in globalThis).toBe(false);
		queue = [PROVISIONED, PROVISIONED];
		expect(await gate(IDENTITY_A)).toEqual({ accessBlockedStatus: null, billingLocked: false });
		expect(await gate(IDENTITY_A)).toEqual({ accessBlockedStatus: null, billingLocked: false });
		expect(lookups).toBe(2);
	});

	test("a null identity (desktop build) is never memoized", async () => {
		await inBrowser(async () => {
			queue = [PROVISIONED, PROVISIONED];
			await gate(null);
			await gate(null);
			expect(lookups).toBe(2);
		});
	});

	test("a non-provisioned outcome clears the memo so an older provisioned verdict cannot mask it", async () => {
		await inBrowser(async () => {
			queue = [PROVISIONED, BILLING_REQUIRED, BILLING_REQUIRED];
			await gate(IDENTITY_A);
			await gate(IDENTITY_B);
			expect(await gate(IDENTITY_A)).toEqual({ accessBlockedStatus: null, billingLocked: true });
			expect(lookups).toBe(3);
		});
	});
});

/**
 * G-06.3-1b (06.3 D-03): a stale unlocked verdict (the memo above, or an
 * exemption revoked mid-session) must not degrade to per-card load errors. A
 * gated `/v1` request refused with 403 `billing_required` while the shell is
 * unlocked forgets the provisioned verdict and re-runs the REAL gate, which
 * then renders the locked shell. `rerunGate` stands in for `router.invalidate()`
 * by re-invoking the same `beforeLoad` the router would.
 */
function billingRequiredRefusal(): Response {
	return new Response(JSON.stringify({ detail: { code: "billing_required" } }), {
		status: 403,
		headers: { "content-type": "application/json" },
	});
}

describe("gated 403 billing_required re-runs the dashboard gate (G-06.3-1b)", () => {
	test("a refusal while unlocked forgets the verdict and the re-run gate lands locked", async () => {
		await inBrowser(async () => {
			queue = [PROVISIONED];
			expect(await gate(IDENTITY_A)).toEqual({ accessBlockedStatus: null, billingLocked: false });
			expect(await gate(IDENTITY_A)).toEqual({ accessBlockedStatus: null, billingLocked: false });
			expect(lookups).toBe(1);

			let kept: GateContext | null = null;
			const controller = createGateRecheckController({
				forgetVerdict: forgetProvisionedVerdict,
				rerunGate: async () => {
					kept = await gate(IDENTITY_A);
				},
				now: () => 0,
			});
			const signal = createBillingRequiredSignal();
			let stored: Promise<GateRecheckResult> | null = null;
			signal.subscribe(() => {
				stored = controller.onRefusal(false);
			});

			queue = [BILLING_REQUIRED];
			expect(await signal.observeResponse(billingRequiredRefusal())).toBe(true);
			expect(stored).not.toBeNull();
			expect(await (stored as Promise<GateRecheckResult> | null)).toBe("rechecked");
			expect(kept as GateContext | null).toEqual({
				accessBlockedStatus: null,
				billingLocked: true,
			});
			expect(lookups).toBe(2);
		});
	});

	test("while locked a refusal is skipped and the gate is not re-asked", async () => {
		await inBrowser(async () => {
			queue = [PROVISIONED];
			await gate(IDENTITY_A);
			expect(lookups).toBe(1);

			const controller = createGateRecheckController({
				forgetVerdict: forgetProvisionedVerdict,
				rerunGate: async () => {
					await gate(IDENTITY_A);
				},
				now: () => 0,
			});
			const signal = createBillingRequiredSignal();
			let stored: Promise<GateRecheckResult> | null = null;
			signal.subscribe(() => {
				stored = controller.onRefusal(true);
			});

			queue = [BILLING_REQUIRED];
			expect(await signal.observeResponse(billingRequiredRefusal())).toBe(true);
			expect(stored).not.toBeNull();
			expect(await (stored as Promise<GateRecheckResult> | null)).toBe("skipped_locked");
			expect(lookups).toBe(1);
		});
	});

	test("a failed re-run resolves failed without throwing, and a later refusal runs again", async () => {
		const errors = spyOn(console, "error").mockImplementation(() => {});
		try {
			let clock = 0;
			let runs = 0;
			const controller = createGateRecheckController({
				forgetVerdict: forgetProvisionedVerdict,
				rerunGate: async () => {
					runs += 1;
					if (runs === 1) throw new Error("router could not re-run the gate");
				},
				now: () => clock,
			});

			expect(await controller.onRefusal(false)).toBe("failed");
			expect(runs).toBe(1);
			// The failure log is a fixed sentence: no error object, no body.
			expect(errors).toHaveBeenCalledTimes(1);
			expect(errors.mock.calls[0]).toHaveLength(1);
			expect(typeof errors.mock.calls[0]?.[0]).toBe("string");

			clock += GATE_RECHECK_MIN_INTERVAL_MS;
			expect(await controller.onRefusal(false)).toBe("rechecked");
			expect(runs).toBe(2);
		} finally {
			errors.mockRestore();
		}
	});
});

/**
 * G-06.3-R2-2 (06.3 D-03): a same-tab sign-out then sign-up runs `_protected`'s
 * getAuthState several times in about a second. One dropped request used to
 * become RootError ("Page unavailable") before the locked shell. Composed here
 * because only this file may mock the gate server fn: the auth-state fetch
 * wrapper survives one transport failure, the identity it yields is the new
 * user's, and the REAL `_dashboard` gate then lands that unpaid user locked.
 */
describe("same-tab identity switch survives one auth-state transport failure (G-06.3-R2-2)", () => {
	function authStateAnswers(answers: Array<Response | Error>) {
		let calls = 0;
		const authFetch = createAuthStateFetch({
			fetchImpl: async () => {
				calls += 1;
				const answer = answers.shift();
				if (answer === undefined) throw new TypeError("Failed to fetch");
				if (answer instanceof Error) throw answer;
				return answer;
			},
			sleep: async () => {},
		});
		return { authFetch, calls: () => calls };
	}

	test("one dropped request then success admits the new identity and the gate lands locked", async () => {
		await inBrowser(async () => {
			queue = [PROVISIONED, BILLING_REQUIRED];
			expect(await gate(IDENTITY_A)).toEqual({ accessBlockedStatus: null, billingLocked: false });

			const { authFetch, calls } = authStateAnswers([
				new TypeError("Failed to fetch"),
				new Response(JSON.stringify({ userId: "user_b", sessionId: "sess_b" }), {
					headers: { "content-type": "application/json" },
				}),
			]);
			let response: Response | null = null;
			let thrown: unknown = null;
			try {
				response = await authFetch("/_serverFn/getAuthState", { method: "GET" });
			} catch (error) {
				thrown = error;
			}
			expect(thrown).toBeNull();
			expect(response).not.toBeNull();
			const body = (await (response as Response).json()) as {
				userId: string;
				sessionId: string;
			};
			expect(requireRouteIdentity(body, "/")).toBe(IDENTITY_B);
			expect(calls()).toBe(2);

			expect(await gate(IDENTITY_B)).toEqual({ accessBlockedStatus: null, billingLocked: true });
			expect(lookups).toBe(2);
		});
	});

	test("when every attempt fails the wrapper rejects offline, so no identity is produced", async () => {
		const { authFetch, calls } = authStateAnswers([]);
		let thrown: unknown = null;
		try {
			await authFetch("/_serverFn/getAuthState", { method: "GET" });
		} catch (error) {
			thrown = error;
		}
		expect(thrown).toBeInstanceOf(ApiNetworkError);
		expect((thrown as ApiNetworkError).kind).toBe("offline");
		expect(calls()).toBe(3);
	});
});
