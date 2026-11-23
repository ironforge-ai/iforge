import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { ApiNetworkError } from "@/lib/api-errors";
import {
	AUTH_STATE_RETRY_DELAYS_MS,
	createAuthStateFetch,
	protectedMatchAdmits,
} from "@/lib/protected-admission";

/**
 * Fork-owned tests for `protected-admission.ts`. They pin G-06.3-R2-2
 * (06.3 D-03): one dropped getAuthState request during a same-tab sign-up must
 * be retried instead of surfacing as `_protected`'s RootError, while
 * `_protected` stays fail-closed (no identity without the server's answer).
 *
 * The source pins read files with comments stripped, so a comment cannot
 * satisfy a pin, and every slice is asserted non-empty and unique. The file
 * lives beside the module, not in an upstream-tracked test, so an upstream
 * merge cannot drop it (03.1 D-02).
 */

type Call = { input: RequestInfo | URL; init: RequestInit | undefined };

function recorder(answers: Array<Response | Error>) {
	const calls: Call[] = [];
	const sleeps: number[] = [];
	const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
		calls.push({ input, init });
		const answer = answers.shift();
		if (answer === undefined) throw new Error("recorder ran out of answers");
		if (answer instanceof Error) throw answer;
		return answer;
	};
	const sleep = async (ms: number) => {
		sleeps.push(ms);
	};
	return { calls, sleeps, fetch: createAuthStateFetch({ fetchImpl, sleep }) };
}

async function settle<T>(
	promise: Promise<T>,
): Promise<{ ok: true; value: T } | { ok: false; error: unknown }> {
	try {
		return { ok: true, value: await promise };
	} catch (error) {
		return { ok: false, error };
	}
}

const URL_ = "/_serverFn/getAuthState";

describe("createAuthStateFetch: bounded retry of a dropped auth-state request (G-06.3-R2-2)", () => {
	test("AUTH_STATE_RETRY_DELAYS_MS is [250, 750]", () => {
		expect([...AUTH_STATE_RETRY_DELAYS_MS]).toEqual([250, 750]);
	});

	test("a success on the first attempt is returned with one call and no sleep", async () => {
		const ok = new Response("{}");
		const r = recorder([ok]);
		expect(await r.fetch(URL_, { method: "GET" })).toBe(ok);
		expect(r.calls).toHaveLength(1);
		expect(r.sleeps).toEqual([]);
	});

	test("one rejection then a Response resolves with the second Response after 250 ms", async () => {
		const ok = new Response("{}");
		const r = recorder([new TypeError("Failed to fetch"), ok]);
		const result = await settle(r.fetch(URL_, { method: "GET" }));
		expect(result.ok).toBe(true);
		expect(result.ok && result.value).toBe(ok);
		expect(r.calls).toHaveLength(2);
		expect(r.sleeps).toEqual([250]);
	});

	test("two rejections then a Response takes three calls and sleeps [250, 750]", async () => {
		const ok = new Response("{}");
		const r = recorder([new TypeError("Failed to fetch"), new TypeError("Failed to fetch"), ok]);
		const result = await settle(r.fetch(URL_, { method: "GET" }));
		expect(result.ok).toBe(true);
		expect(result.ok && result.value).toBe(ok);
		expect(r.calls).toHaveLength(3);
		expect(r.sleeps).toEqual([250, 750]);
	});

	test("three rejections reject with ApiNetworkError offline caused by the last rejection", async () => {
		const last = new TypeError("Failed to fetch (third)");
		const r = recorder([new TypeError("first"), new TypeError("second"), last]);
		const result = await settle(r.fetch(URL_, { method: "GET" }));
		expect(result.ok).toBe(false);
		const error = result.ok ? null : result.error;
		expect(error).toBeInstanceOf(ApiNetworkError);
		expect((error as ApiNetworkError).kind).toBe("offline");
		expect((error as ApiNetworkError).message).toBe("API request failed");
		expect((error as ApiNetworkError).cause).toBe(last);
		expect(r.calls).toHaveLength(3);
	});

	test("an HTTP error answer is returned unchanged and never retried", async () => {
		for (const status of [500, 503]) {
			const answer = new Response("down", { status });
			const r = recorder([answer]);
			expect(await r.fetch(URL_, { method: "GET" })).toBe(answer);
			expect(r.calls).toHaveLength(1);
			expect(r.sleeps).toEqual([]);
		}
	});

	// IN-05: a superseded navigation is an abort, never classified as offline.
	test("a rejection with an aborted signal rethrows that abort after one call and never sleeps", async () => {
		const controller = new AbortController();
		controller.abort();
		const abort = new DOMException("aborted", "AbortError");
		const r = recorder([abort, new Response("{}")]);
		const result = await settle(r.fetch(URL_, { method: "GET", signal: controller.signal }));
		expect(result.ok).toBe(false);
		const error = result.ok ? null : result.error;
		expect(error).toBe(abort);
		expect(error).not.toBeInstanceOf(ApiNetworkError);
		expect(r.calls).toHaveLength(1);
		expect(r.sleeps).toEqual([]);
	});

	test("an abort during the retry wait ends the wait and rethrows the abort without another call", async () => {
		const controller = new AbortController();
		const calls: number[] = [];
		const fetch = createAuthStateFetch({
			delaysMs: [60_000],
			fetchImpl: async () => {
				calls.push(calls.length);
				setTimeout(() => controller.abort(), 0);
				throw new TypeError("Failed to fetch");
			},
		});
		const started = Date.now();
		const result = await settle(fetch(URL_, { method: "GET", signal: controller.signal }));
		expect(Date.now() - started).toBeLessThan(5_000);
		expect(result.ok).toBe(false);
		const error = result.ok ? null : result.error;
		expect(error).toBe(controller.signal.reason);
		expect((error as DOMException).name).toBe("AbortError");
		expect(error).not.toBeInstanceOf(ApiNetworkError);
		expect(calls).toHaveLength(1);
	});

	test("the retry wait is handed the request's signal", async () => {
		const controller = new AbortController();
		const signals: Array<AbortSignal | null | undefined> = [];
		const fetch = createAuthStateFetch({
			fetchImpl: async (_input, _init) => {
				if (signals.length === 0) throw new TypeError("Failed to fetch");
				return new Response("{}");
			},
			sleep: async (_ms, signal) => {
				signals.push(signal);
			},
		});
		await fetch(URL_, { method: "GET", signal: controller.signal });
		expect(signals).toEqual([controller.signal]);
	});

	test("every retry re-sends the same input and the same init object", async () => {
		const init: RequestInit = { method: "GET", headers: { accept: "application/json" } };
		const r = recorder([new TypeError("a"), new TypeError("b"), new Response("{}")]);
		await settle(r.fetch(URL_, init));
		expect(r.calls).toHaveLength(3);
		for (const call of r.calls) {
			expect(call.input).toBe(URL_);
			expect(call.init).toBe(init);
		}
	});
});

function stripComments(code: string): string {
	return code
		.replace(/\/\*[\s\S]*?\*\//g, "")
		.split("\n")
		.map((line) => {
			const comment = line.indexOf("//");
			return comment === -1 ? line : line.slice(0, comment);
		})
		.join("\n");
}

function source(relative: string): string {
	return stripComments(readFileSync(new URL(relative, import.meta.url), "utf8"));
}

function occurrences(code: string, needle: string): number {
	return code.split(needle).length - 1;
}

describe("_protected.tsx: beforeLoad hands authStateFetch to getAuthState", () => {
	const route = source("../routes/_protected.tsx");

	function beforeLoadSlice(): string {
		expect(occurrences(route, "beforeLoad: async")).toBe(1);
		const start = route.indexOf("beforeLoad: async");
		const end = route.indexOf("errorComponent:", start);
		expect(end).toBeGreaterThan(start);
		const slice = route.slice(start, end);
		expect(slice.length).toBeGreaterThan("beforeLoad: async".length);
		return slice;
	}

	test("the beforeLoad passes fetch: authStateFetch once and has no inline .catch(", () => {
		const slice = beforeLoadSlice();
		expect(occurrences(slice, "fetch: authStateFetch")).toBe(1);
		expect(slice).not.toContain(".catch(");
		expect(slice).toContain("requireRouteIdentity(authState, location.href)");
	});

	test("authStateFetch comes from the admission module and RootError still owns the error", () => {
		expect(route).toMatch(
			/import \{[^}]*\bauthStateFetch\b[^}]*\} from "@\/lib\/protected-admission";/,
		);
		expect(occurrences(route, "errorComponent: RootError,")).toBe(1);
	});
});

const ADMITTED = JSON.stringify(["user_b", "sess_b"]);
const OTHER = JSON.stringify(["user_a", "sess_a"]);

function layoutMatch(status: string, context?: unknown) {
	return { routeId: "/_protected", status, context };
}

describe("protectedMatchAdmits: the loaded /_protected match already admitted this identity", () => {
	test("a null identity is never admitted", () => {
		expect(protectedMatchAdmits([layoutMatch("success", { authIdentity: null })], null)).toBe(
			false,
		);
	});

	test("a successful /_protected match carrying the identity admits it", () => {
		const matches = [
			{ routeId: "__root__", status: "success", context: {} },
			layoutMatch("success", { authIdentity: ADMITTED }),
		];
		expect(protectedMatchAdmits(matches, ADMITTED)).toBe(true);
	});

	test("the same match does not admit a different identity", () => {
		expect(protectedMatchAdmits([layoutMatch("success", { authIdentity: OTHER })], ADMITTED)).toBe(
			false,
		);
	});

	test("a pending or errored match admits nothing", () => {
		for (const status of ["pending", "error"]) {
			expect(
				protectedMatchAdmits([layoutMatch(status, { authIdentity: ADMITTED })], ADMITTED),
			).toBe(false);
		}
	});

	test("a match without context or with a non-string authIdentity admits nothing", () => {
		expect(protectedMatchAdmits([layoutMatch("success")], ADMITTED)).toBe(false);
		expect(protectedMatchAdmits([layoutMatch("success", { authIdentity: 42 })], ADMITTED)).toBe(
			false,
		);
		expect(protectedMatchAdmits([layoutMatch("success", null)], ADMITTED)).toBe(false);
	});

	test("no /_protected match admits nothing", () => {
		expect(protectedMatchAdmits([], ADMITTED)).toBe(false);
		expect(
			protectedMatchAdmits([{ routeId: "__root__", status: "success", context: {} }], ADMITTED),
		).toBe(false);
	});

	test("only the layout's own match counts, not a child carrying the identity", () => {
		const child = {
			routeId: "/_protected/_dashboard",
			status: "success",
			context: { authIdentity: ADMITTED },
		};
		expect(protectedMatchAdmits([child], ADMITTED)).toBe(false);
	});
});

describe("auth-router-bridge.tsx: skips the invalidate for an already-admitted identity", () => {
	const bridge = source("../components/auth-router-bridge.tsx");

	function layoutEffectBody(): string {
		expect(occurrences(bridge, "useLayoutEffect(() => {")).toBe(1);
		const start = bridge.indexOf("useLayoutEffect(() => {");
		const end = bridge.indexOf("}, [", start);
		expect(end).toBeGreaterThan(start);
		const body = bridge.slice(start, end);
		expect(body.length).toBeGreaterThan("useLayoutEffect(() => {".length);
		return body;
	}

	function effectDeps(): string {
		const start = bridge.indexOf("useLayoutEffect(() => {");
		const open = bridge.indexOf("}, [", start);
		const close = bridge.indexOf("]);", open);
		expect(open).toBeGreaterThan(start);
		expect(close).toBeGreaterThan(open);
		return bridge.slice(open + "}, [".length, close);
	}

	test("the admission check runs once, between clearCache and invalidate", () => {
		const body = layoutEffectBody();
		const check = "protectedMatchAdmits(router.state.matches, identity)";
		expect(occurrences(body, check)).toBe(1);
		const clear = body.indexOf("router.clearCache(");
		const at = body.indexOf(check);
		// Upstream formats the call as `void router` + newline + `.invalidate(`.
		const invalidate = body.search(/router\s*\.invalidate\(/);
		expect(clear).toBeGreaterThan(-1);
		expect(at).toBeGreaterThan(clear);
		expect(invalidate).toBeGreaterThan(at);
	});

	test("the admission check is an if whose body is a bare return", () => {
		expect(layoutEffectBody()).toMatch(
			/if \(protectedMatchAdmits\(router\.state\.matches, identity\)\)\s*(?:return;|\{\s*return;\s*\})/,
		);
	});

	test("identity is an effect dependency and the helper comes from the admission module", () => {
		expect(
			effectDeps()
				.split(",")
				.map((dep) => dep.trim()),
		).toContain("identity");
		expect(bridge).toMatch(
			/import \{[^}]*\bprotectedMatchAdmits\b[^}]*\} from "@\/lib\/protected-admission";/,
		);
	});
});
