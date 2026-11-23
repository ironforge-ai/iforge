import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import * as accessGate from "@/lib/access-gate";
import {
	type AccessState,
	type AccessStatusOutcome,
	dashboardGateDecision,
	deactivatedGateDecision,
	fetchAccessStatusOutcome,
} from "@/lib/access-gate";

/**
 * G-03-3 regression suite. Before 03-12 `fetchAccessStatus` threw a bare,
 * undiscriminated `Error` for every non-2xx, so a 401, a 403 and a fail-closed
 * 503 were indistinguishable from a dropped connection — and every gate
 * `beforeLoad` swallowed all four with a bare `catch {}`, rendering the
 * dashboard shell to a refused identity.
 *
 * These are the first tests in this repo to touch the access-status fetch at
 * all: the symbol had zero test references, which is why the defect was
 * unreachable by the suite by construction. They drive REAL non-2xx `Response`
 * objects through the real openapi-fetch layer via the client's `fetch` seam —
 * mocking that layer would defeat the purpose.
 */

const ACCESS_STATES: readonly AccessState[] = [
	"provisioned",
	"billing_required",
	"terminated",
] as const;

/** Every authoritative refusal the gate must obey, not just the ones the
 * backend documents today — an unexpected status is still an answer. */
const REFUSAL_STATUSES = [401, 403, 404, 429, 500, 502, 503] as const;

function jsonResponse(body: unknown, status = 200): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { "content-type": "application/json" },
	});
}

/** Injected `fetch` that answers every request identically, recording them. */
function stubFetch(make: () => Response) {
	const requests: Request[] = [];
	return {
		requests,
		impl: async (request: Request): Promise<Response> => {
			requests.push(request);
			return make();
		},
	};
}

const withToken = async () => "gate-test-token";
const withoutToken = async () => null;

describe("fetchAccessStatusOutcome — resolved answers", () => {
	for (const state of ACCESS_STATES) {
		test(`a 200 carrying "${state}" resolves to that state`, async () => {
			const fetcher = stubFetch(() => jsonResponse({ state }));
			expect(await fetchAccessStatusOutcome(withToken, fetcher.impl)).toEqual({
				outcome: "resolved",
				state,
			});
		});
	}
});

describe("fetchAccessStatusOutcome — authoritative refusals", () => {
	for (const status of REFUSAL_STATUSES) {
		test(`a ${status} is a refusal carrying its status, not silence`, async () => {
			const fetcher = stubFetch(() => new Response(null, { status }));
			expect(await fetchAccessStatusOutcome(withToken, fetcher.impl)).toEqual({
				outcome: "refused",
				status,
			});
		});
	}

	test("a 503 with a billing_unavailable problem body stays a refusal (D-09, fail closed)", async () => {
		const fetcher = stubFetch(() => jsonResponse({ detail: { code: "billing_unavailable" } }, 503));
		expect(await fetchAccessStatusOutcome(withToken, fetcher.impl)).toEqual({
			outcome: "refused",
			status: 503,
		});
	});

	test("a refusal is distinguishable from a dropped connection", async () => {
		const refused = await fetchAccessStatusOutcome(
			withToken,
			stubFetch(() => new Response(null, { status: 401 })).impl,
		);
		const dropped = await fetchAccessStatusOutcome(withToken, async () => {
			throw new TypeError("fetch failed: connection refused");
		});
		expect(refused).not.toEqual(dropped);
		expect(refused.outcome).toBe("refused");
		expect(dropped.outcome).toBe("unreachable");
	});
});

describe("fetchAccessStatusOutcome — nothing was resolved", () => {
	test("a rejecting fetch (connection refused / DNS / abort) is unreachable", async () => {
		expect(
			await fetchAccessStatusOutcome(withToken, async () => {
				throw new TypeError("fetch failed");
			}),
		).toEqual({ outcome: "unreachable" });
	});

	test("a 2xx with an empty body is unreachable, NOT a refusal", async () => {
		// The pre-03-12 check was `data === undefined`, which conflated this
		// bodyless 2xx with a real refusal. Branching on `response.ok` is what
		// separates them; swap the two and this assertion fails.
		const fetcher = stubFetch(
			() => new Response("", { status: 200, headers: { "content-length": "0" } }),
		);
		expect(await fetchAccessStatusOutcome(withToken, fetcher.impl)).toEqual({
			outcome: "unreachable",
		});
	});

	test("the outcome never throws and is always plain JSON (survives the RPC boundary)", async () => {
		const cases: AccessStatusOutcome[] = [
			await fetchAccessStatusOutcome(
				withToken,
				stubFetch(() => jsonResponse({ state: "provisioned" })).impl,
			),
			await fetchAccessStatusOutcome(
				withToken,
				stubFetch(() => new Response(null, { status: 403 })).impl,
			),
			await fetchAccessStatusOutcome(withToken, async () => {
				throw new Error("down");
			}),
		];
		for (const outcome of cases) {
			expect(outcome).not.toBeInstanceOf(Error);
			// A class instance would be erased crossing the server-fn RPC (T-03-40).
			expect(JSON.parse(JSON.stringify(outcome))).toEqual(outcome);
		}
	});
});

describe("fetchAccessStatusOutcome — bearer header", () => {
	test("the Authorization header is present when a token is yielded", async () => {
		const fetcher = stubFetch(() => jsonResponse({ state: "provisioned" }));
		await fetchAccessStatusOutcome(withToken, fetcher.impl);
		expect(fetcher.requests).toHaveLength(1);
		expect(fetcher.requests[0]?.headers.get("Authorization")).toBe("Bearer gate-test-token");
	});

	test("no Authorization header is sent when the token getter yields null", async () => {
		const fetcher = stubFetch(() => jsonResponse({ state: "provisioned" }));
		await fetchAccessStatusOutcome(withoutToken, fetcher.impl);
		expect(fetcher.requests[0]?.headers.get("Authorization")).toBeNull();
	});
});

const resolved = (state: AccessState): AccessStatusOutcome => ({ outcome: "resolved", state });
const refused = (status: number): AccessStatusOutcome => ({ outcome: "refused", status });
const unreachable: AccessStatusOutcome = { outcome: "unreachable" };

/** The `_dashboard` beforeLoad location: a pathname plus the validated search. */
const at = (pathname: string, search: Record<string, unknown> = {}) => ({ pathname, search });

describe("dashboardGateDecision", () => {
	test("a provisioned identity renders", () => {
		expect(dashboardGateDecision(resolved("provisioned"), at("/"))).toEqual({ kind: "render" });
	});

	test("a resolved terminated state redirects to the fenced screen", () => {
		expect(dashboardGateDecision(resolved("terminated"), at("/"))).toEqual({
			kind: "redirect",
			to: "/deactivated",
		});
	});

	// 06.3 D-03: a billing-locked user sees the dashboard shell with the
	// "Choose a plan" panel, never a bounce to /deploy.
	for (const pathname of ["/", "/agents/abc", "/deploy"]) {
		test(`a resolved billing_required renders the locked shell at ${pathname}`, () => {
			expect(dashboardGateDecision(resolved("billing_required"), at(pathname))).toEqual({
				kind: "render-locked",
			});
		});
	}

	test("a dropped connection still fails OPEN (review LOW #7)", () => {
		expect(dashboardGateDecision(unreachable, at("/"))).toEqual({ kind: "render" });
	});

	test("EVERY authoritative refusal blocks, carrying its status", () => {
		for (const status of REFUSAL_STATUSES) {
			expect(dashboardGateDecision(refused(status), at("/"))).toEqual({ kind: "blocked", status });
		}
	});
});

describe("dashboardGateDecision — checkout return forward for locked users (L10.1)", () => {
	const locked = resolved("billing_required");

	test("a session marker on /deploy forwards ONLY settings and checkout_session_id", () => {
		const decision = dashboardGateDecision(
			locked,
			at("/deploy", {
				checkout_session_id: "cs_test_1",
				deploy_profile: "x",
				next: "https://evil.example",
			}),
		);
		// toEqual on the whole object: an injected `next` (or any other key)
		// must never be forwarded (open-redirect guard, T-06.3-02-01).
		expect(decision).toEqual({
			kind: "redirect",
			to: "/",
			search: { settings: "billing-plan", checkout_session_id: "cs_test_1" },
		});
	});

	test("a cancel marker on /deploy forwards ONLY settings and checkout", () => {
		expect(
			dashboardGateDecision(locked, at("/deploy", { checkout: "cancel", utm_source: "y" })),
		).toEqual({
			kind: "redirect",
			to: "/",
			search: { settings: "billing-plan", checkout: "cancel" },
		});
	});

	test("a marker on a /deploy sub-path forwards too", () => {
		expect(dashboardGateDecision(locked, at("/deploy/hermes", { checkout: "cancel" }))).toEqual({
			kind: "redirect",
			to: "/",
			search: { settings: "billing-plan", checkout: "cancel" },
		});
	});

	test("both markers present forward both, and nothing else", () => {
		expect(
			dashboardGateDecision(
				locked,
				at("/deploy", {
					checkout_session_id: "cs_test_2",
					checkout: "cancel",
					settings: "general",
				}),
			),
		).toEqual({
			kind: "redirect",
			to: "/",
			search: { settings: "billing-plan", checkout_session_id: "cs_test_2", checkout: "cancel" },
		});
	});

	test("an unrecognised checkout value or an empty / non-string session id does not forward", () => {
		for (const search of [
			{ checkout: "other" },
			{ checkout_session_id: "" },
			{ checkout_session_id: 42 },
			{},
		]) {
			expect(dashboardGateDecision(locked, at("/deploy", search))).toEqual({
				kind: "render-locked",
			});
		}
	});

	test("only /deploy forwards: other paths stay on the locked shell", () => {
		expect(
			dashboardGateDecision(locked, at("/agents/abc", { checkout_session_id: "cs_test_1" })),
		).toEqual({ kind: "render-locked" });
		expect(dashboardGateDecision(locked, at("/deployments", { checkout: "cancel" }))).toEqual({
			kind: "render-locked",
		});
	});

	test("an unlocked user at /deploy with a marker renders (no redirect, L10.3)", () => {
		expect(
			dashboardGateDecision(
				resolved("provisioned"),
				at("/deploy", { checkout_session_id: "cs_test_1" }),
			),
		).toEqual({ kind: "render" });
	});
});

describe("deactivatedGateDecision", () => {
	test("only a resolved terminated state renders the fenced screen (T-03-38)", () => {
		expect(deactivatedGateDecision(resolved("terminated"))).toEqual({ kind: "render" });
	});

	test("resolved non-terminated states are routed away", () => {
		expect(deactivatedGateDecision(resolved("provisioned"))).toEqual({ kind: "redirect", to: "/" });
	});

	test("a resolved billing_required leaves /deactivated for the locked shell at / (X3)", () => {
		expect(deactivatedGateDecision(resolved("billing_required"))).toEqual({
			kind: "redirect",
			to: "/",
		});
	});

	test("no unresolved outcome ever tells a healthy user their account is closed", () => {
		expect(deactivatedGateDecision(unreachable)).toEqual({ kind: "blocked", status: null });
		for (const status of REFUSAL_STATUSES) {
			expect(deactivatedGateDecision(refused(status))).toEqual({ kind: "blocked", status });
		}
	});
});

/*
 * 06.3 D-01: invite redemption is gone end to end. The module is the
 * access-status client and the gate decisions over it, nothing else: no
 * request to an invite endpoint, and no redeem client, error class or gate
 * decision left behind for a caller to reach for.
 */
describe("access-gate module surface (D-01)", () => {
	test("exports no redeem or invite symbol", () => {
		const inviteExports = Object.keys(accessGate).filter((name) => /redeem|invite/i.test(name));
		expect(inviteExports).toEqual([]);
	});

	test("sends no request to an invite endpoint", () => {
		const source = readFileSync(join(import.meta.dir, "access-gate.ts"), "utf8");
		const inviteLines = source.split("\n").filter((line) => line.includes("/v1/invites"));
		expect(inviteLines).toEqual([]);
	});
});
