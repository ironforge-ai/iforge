/**
 * The runtime half of TOOL-01: the executor MINTS a credential, it never forwards one.
 *
 * Every dependency is injected and nothing here touches the network or the filesystem — the
 * injected-dependency idiom `observation-producer.test.ts` uses, for the same reason: a test that
 * needs a live companion is a test that runs in one place.
 *
 * The sentinel is the point. A manifest whose `auth.secret` is a string that cannot occur
 * incidentally goes in; the outbound header value and every captured log line are asserted free
 * of it. The Python half asserts the same property over the control-plane surfaces in
 * `backend/tests/test_companion_facade_credentials.py`.
 */

import { afterEach, describe, expect, it } from "bun:test";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import * as companionExecutor from "./companion-executor";
import {
	COMPANION_ASSERTION_MAX_TTL_SECONDS,
	COMPANION_BACKOFF_BASE_MS,
	COMPANION_MAX_RATE_LIMIT_ATTEMPTS,
	type CompanionAuthMaterial,
	type CompanionDispatchCall,
	CompanionExecutor,
	companionFixedQuery,
	decodeDispatchCall,
	parseRetryAfterSeconds,
	signCompanionAssertion,
} from "./companion-executor";
import { FILE_BROWSER_SOURCE_NAME } from "./manifest-contract";
import { getRuntimePaths, type RuntimePaths } from "./paths";

/** 46 characters of `[A-Za-z0-9_-]`, inside the manifest schema's 43..128 bound. */
const SENTINEL_SECRET = "SENTINEL_COMPANION_SECRET_DO_NOT_LEAK_00000000";
const ACCESS_REVISION = "a".repeat(64);
const AUDIENCE = "clawdi-files:dep-test";

const AUTH: CompanionAuthMaterial = {
	method: "jwt",
	algorithm: "HS256",
	header: "X-JWT-Assertion",
	userIdentifier: "sub",
	groupsClaim: "groups",
	secret: SENTINEL_SECRET,
	audience: AUDIENCE,
	subject: "deployment:dep-test:owner",
	requiredGroup: `${AUDIENCE}:${ACCESS_REVISION}`,
	accessRevision: ACCESS_REVISION,
};

const NOW_MS = 1_800_000_000_000;
const NOW_SECONDS = Math.floor(NOW_MS / 1000);

function dispatchCall(overrides: Partial<CompanionDispatchCall> = {}): CompanionDispatchCall {
	return {
		companion_key: "filebrowser",
		verb: "list_dir",
		method: "GET",
		path_template: "/api/resources",
		arguments: { path: "/" },
		jti: "0123456789abcdef",
		aud: "clawdi-runtime-companion-dispatch",
		exp: NOW_SECONDS + 30,
		...overrides,
	};
}

function decodeJwtPayload(token: string): Record<string, unknown> {
	const segment = token.split(".")[1] as string;
	return JSON.parse(Buffer.from(segment, "base64url").toString("utf-8"));
}

/** Capture every structured log line the executor emits, without letting it reach stderr. */
function captureLogs(): { lines: string[]; restore: () => void } {
	const lines: string[] = [];
	const original = process.stderr.write.bind(process.stderr);
	// biome-ignore lint/suspicious/noExplicitAny: the stream's write overloads are not worth modelling here
	(process.stderr as any).write = (chunk: any, ...rest: any[]) => {
		lines.push(String(chunk));
		void rest;
		return true;
	};
	return {
		lines,
		restore: () => {
			// biome-ignore lint/suspicious/noExplicitAny: see above
			(process.stderr as any).write = original;
		},
	};
}

const restorers: Array<() => void> = [];

afterEach(() => {
	while (restorers.length > 0) restorers.pop()?.();
});

describe("signCompanionAssertion", () => {
	it("mints an assertion the companion's JWT method will accept", () => {
		const token = signCompanionAssertion(AUTH, NOW_SECONDS);
		const payload = decodeJwtPayload(token);

		expect(payload.sub).toBe(AUTH.subject);
		expect(payload.groups).toEqual([AUTH.requiredGroup]);
		expect(payload.aud).toBe(AUTH.audience);
	});

	it("bounds the assertion's life to seconds, not hours", () => {
		const payload = decodeJwtPayload(signCompanionAssertion(AUTH, NOW_SECONDS));

		const exp = payload.exp as number;
		expect(exp).toBeGreaterThan(NOW_SECONDS);
		expect(exp - NOW_SECONDS).toBeLessThanOrEqual(COMPANION_ASSERTION_MAX_TTL_SECONDS);
		expect(COMPANION_ASSERTION_MAX_TTL_SECONDS).toBeLessThanOrEqual(120);
	});

	it("never places the secret itself in the token it signs", () => {
		// The whole difference between minting and forwarding, as one assertion.
		expect(signCompanionAssertion(AUTH, NOW_SECONDS)).not.toContain(SENTINEL_SECRET);
	});

	it("produces a different signature for a different secret", () => {
		// Without this, an implementation that ignored the key entirely would pass everything
		// above — the signature would still be a well-formed third segment.
		const other = signCompanionAssertion({ ...AUTH, secret: "b".repeat(43) }, NOW_SECONDS);
		expect(other).not.toBe(signCompanionAssertion(AUTH, NOW_SECONDS));
	});
});

describe("CompanionExecutor environment binding (#1189 source, T-06.1-24)", () => {
	// The claim names the environment a dispatch is bound to, so it must come from exactly
	// where upstream's own runtime readers take it after #1189: the watch unit's process
	// environment, canonical or refused. Never a guess and never a replica file.
	const original = process.env.CLAWDI_ENVIRONMENT_ID;
	afterEach(() => {
		if (original === undefined) delete process.env.CLAWDI_ENVIRONMENT_ID;
		else process.env.CLAWDI_ENVIRONMENT_ID = original;
	});

	function unboundExecutor(claimed: string[]): CompanionExecutor {
		return new CompanionExecutor({
			abort: new AbortController().signal,
			paths: getRuntimePaths(),
			claim: async (environmentId) => {
				claimed.push(environmentId);
				return null;
			},
			report: async () => {},
			execute: async () => ({ status: 200, body: "{}" }),
			readCompanionAuth: () => AUTH,
			now: () => NOW_MS,
		});
	}

	it("claims for the environment the watch unit's process environment names", async () => {
		process.env.CLAWDI_ENVIRONMENT_ID = "env-from-unit";
		const claimed: string[] = [];
		await unboundExecutor(claimed).runOnce();
		expect(claimed).toEqual(["env-from-unit"]);
	});

	it("idles without claiming when no environment is named", async () => {
		delete process.env.CLAWDI_ENVIRONMENT_ID;
		const claimed: string[] = [];
		expect(await unboundExecutor(claimed).runOnce()).toEqual({ outcome: "idle" });
		expect(claimed).toEqual([]);
	});

	it("refuses a non-canonical environment id rather than claiming for it", async () => {
		for (const value of ["", " env-from-unit", "env-from-unit\n"]) {
			process.env.CLAWDI_ENVIRONMENT_ID = value;
			const claimed: string[] = [];
			await expect(unboundExecutor(claimed).runOnce()).rejects.toThrow(
				"CLAWDI_ENVIRONMENT_ID must be a non-empty canonical identity",
			);
			expect(claimed).toEqual([]);
		}
	});
});

describe("CompanionExecutor", () => {
	it("claims, executes and reports without touching the network or the filesystem", async () => {
		const reported: Array<Record<string, unknown>> = [];
		const executed: CompanionDispatchCall[] = [];
		const executor = new CompanionExecutor({
			abort: new AbortController().signal,
			environmentId: "env-1",
			paths: getRuntimePaths(),
			claim: async () => ({ call: dispatchCall(), claimId: "claim-1" }),
			report: async (jti, claimId, outcome) => {
				reported.push({ jti, claimId, ...outcome });
			},
			execute: async (call) => {
				executed.push(call);
				return { status: 200, body: '{"items":[]}' };
			},
			readCompanionAuth: () => AUTH,
			now: () => NOW_MS,
		});
		const logs = captureLogs();
		restorers.push(logs.restore);

		const outcome = await executor.runOnce();

		expect(outcome).toEqual({ outcome: "executed", jti: "0123456789abcdef", status: 200 });
		expect(executed).toHaveLength(1);
		expect(executed[0]?.verb).toBe("list_dir");
		expect(reported).toEqual([
			{ jti: "0123456789abcdef", claimId: "claim-1", status: 200, body: '{"items":[]}' },
		]);
		for (const line of logs.lines) {
			expect(line).not.toContain(SENTINEL_SECRET);
		}
		expect(logs.lines.length).toBeGreaterThan(0);
	});

	it("sends the companion's required `source`, and an agent cannot override it", async () => {
		// `/api/resources` requires `source` beside `path` and answers 500
		// "source not provided" otherwise, so every such verb failed in production
		// while the stub — which answers whatever it is sent — stayed green.
		//
		// The override half is the security property rather than a nicety: `source`
		// names WHICH store the call addresses, so an agent that could set it would
		// be choosing its own target. Fixed parameters are applied after the
		// agent's arguments for exactly that reason.
		const urls: string[] = [];
		const originalFetch = globalThis.fetch;
		globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
			urls.push(new Request(input as RequestInfo, init).url);
			return new Response('{"items":[]}', { status: 200 });
		}) as typeof fetch;
		restorers.push(() => {
			globalThis.fetch = originalFetch;
		});
		const executor = new CompanionExecutor({
			abort: new AbortController().signal,
			environmentId: "env-1",
			paths: getRuntimePaths(),
			claim: async () => ({
				call: dispatchCall({
					// The agent tries to redirect the call at another store.
					arguments: { path: "/", source: "somewhere-else" },
				}),
				claimId: "claim-1",
			}),
			report: async () => {},
			readCompanionAuth: () => AUTH,
			now: () => NOW_MS,
		});
		const logs = captureLogs();
		restorers.push(logs.restore);

		await executor.runOnce();

		expect(urls).toHaveLength(1);
		const query = new URL(urls[0] as string).searchParams;
		expect(query.get("path")).toBe("/");
		expect(query.get("source")).toBe(FILE_BROWSER_SOURCE_NAME);
		expect(query.get("source")).not.toBe("somewhere-else");
	});

	it("sends a minted assertion to the registry's address, and nothing from the envelope", async () => {
		const requests: Array<{ url: string; headers: Record<string, string>; method: string }> = [];
		const originalFetch = globalThis.fetch;
		// A recorder, not a network call: the executor's own request-building is what is under
		// test, and it is the only thing that ever sets the credential header.
		globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
			const request = new Request(input as RequestInfo, init);
			requests.push({
				url: request.url,
				method: request.method,
				headers: Object.fromEntries(request.headers.entries()),
			});
			return new Response('{"items":[]}', { status: 200 });
		}) as typeof fetch;
		restorers.push(() => {
			globalThis.fetch = originalFetch;
		});
		const executor = new CompanionExecutor({
			abort: new AbortController().signal,
			environmentId: "env-1",
			paths: getRuntimePaths(),
			claim: async () => ({ call: dispatchCall(), claimId: "claim-1" }),
			report: async () => {},
			readCompanionAuth: () => AUTH,
			now: () => NOW_MS,
		});
		const logs = captureLogs();
		restorers.push(logs.restore);

		const outcome = await executor.runOnce();

		expect(outcome.outcome).toBe("executed");
		expect(requests).toHaveLength(1);
		const sent = requests[0];
		// Host and port from the registry row, path from the envelope, arguments as query.
		expect(sent?.url).toBe("http://127.0.0.1:9120/api/resources?path=%2F&source=Files");
		expect(sent?.method).toBe("GET");
		const assertion = sent?.headers["x-jwt-assertion"];
		expect(assertion).toBeTruthy();
		expect(assertion).not.toContain(SENTINEL_SECRET);
		const payload = decodeJwtPayload(assertion as string);
		expect(payload.sub).toBe(AUTH.subject);
		expect(payload.groups).toEqual([AUTH.requiredGroup]);
		expect((payload.exp as number) - NOW_SECONDS).toBeLessThanOrEqual(120);
		for (const line of logs.lines) {
			expect(line).not.toContain(SENTINEL_SECRET);
		}
	});

	it("reports a failure back rather than leaving the façade to time out", async () => {
		const reported: Array<Record<string, unknown>> = [];
		const executor = new CompanionExecutor({
			abort: new AbortController().signal,
			environmentId: "env-1",
			paths: getRuntimePaths(),
			claim: async () => ({ call: dispatchCall(), claimId: "claim-1" }),
			report: async (jti, claimId, outcome) => {
				reported.push({ jti, claimId, ...outcome });
			},
			execute: async () => {
				throw new Error("companion refused the connection");
			},
			readCompanionAuth: () => AUTH,
			now: () => NOW_MS,
		});
		const logs = captureLogs();
		restorers.push(logs.restore);

		const outcome = await executor.runOnce();

		expect(outcome.outcome).toBe("failed");
		expect(reported).toEqual([
			{
				jti: "0123456789abcdef",
				claimId: "claim-1",
				error: "companion refused the connection",
			},
		]);
	});

	it("is idle when the control plane has nothing to hand out", async () => {
		const executor = new CompanionExecutor({
			abort: new AbortController().signal,
			environmentId: "env-1",
			paths: getRuntimePaths(),
			claim: async () => null,
			report: async () => {},
			execute: async () => ({ status: 200, body: "" }),
			now: () => NOW_MS,
		});

		expect(await executor.runOnce()).toEqual({ outcome: "idle" });
	});
});

describe("decodeDispatchCall", () => {
	it("rejects an envelope carrying a field the contract does not declare", () => {
		// `.strict()` is load-bearing: this object crosses into a tenant network, so an
		// unexpected field is a wire-contract mismatch to fail on, not a value to ignore.
		const token = `x.${Buffer.from(
			JSON.stringify({ ...dispatchCall(), secret: SENTINEL_SECRET }),
		).toString("base64url")}.y`;

		expect(() => decodeDispatchCall(token)).toThrow();
	});

	it("rejects a path template that is not companion-relative", () => {
		const token = `x.${Buffer.from(
			JSON.stringify(dispatchCall({ path_template: "//evil.test/api" })),
		).toString("base64url")}.y`;

		expect(() => decodeDispatchCall(token)).toThrow();
	});

	it("rejects a method outside the registry's vocabulary", () => {
		const token = `x.${Buffer.from(JSON.stringify({ ...dispatchCall(), method: "TRACE" })).toString(
			"base64url",
		)}.y`;

		expect(() => decodeDispatchCall(token)).toThrow();
	});
});

/**
 * Rate-limit backoff (TOOL-09's runtime half).
 *
 * Hitting a companion's limit must SLOW the agent's task rather than fail it, and the slowdown has
 * to be visible in the runtime log — a backed-off call that looked identical to a hung one would
 * make "slows the task" indistinguishable from "stopped responding", which is the prohibition this
 * plan carries.
 *
 * Every delay and every random draw is injected. Real sleeping would make the exponential cases
 * take half a minute, and real randomness would make the jitter assertions flaky in exactly the
 * way that teaches a reader to ignore them.
 */

interface BackoffHarness {
	executor: CompanionExecutor;
	delays: number[];
	reported: Array<Record<string, unknown>>;
	attempts: () => number;
}

function backoffHarness(
	responses: Array<{ status: number; body: string; retryAfter?: string | null }>,
	draws: number[] = [],
): BackoffHarness {
	const delays: number[] = [];
	const reported: Array<Record<string, unknown>> = [];
	let attempt = 0;
	let draw = 0;
	const executor = new CompanionExecutor({
		abort: new AbortController().signal,
		environmentId: "env-1",
		paths: getRuntimePaths(),
		claim: async () => ({ call: dispatchCall(), claimId: "claim-1" }),
		report: async (jti, claimId, outcome) => {
			reported.push({ jti, claimId, ...outcome });
		},
		execute: async () => {
			const response = responses[Math.min(attempt, responses.length - 1)];
			attempt += 1;
			return response as { status: number; body: string };
		},
		readCompanionAuth: () => AUTH,
		now: () => NOW_MS,
		delay: async (ms: number) => {
			delays.push(ms);
		},
		random: () => {
			const value = draws[Math.min(draw, draws.length - 1)] ?? 0;
			draw += 1;
			return value;
		},
	});
	return { executor, delays, reported, attempts: () => attempt };
}

describe("parseRetryAfterSeconds", () => {
	it("reads delta-seconds", () => {
		expect(parseRetryAfterSeconds("7")).toBe(7);
		expect(parseRetryAfterSeconds("0")).toBe(0);
	});

	it("treats an unparseable value as ABSENT rather than as zero", () => {
		// Zero would mean "retry immediately", which is the opposite of what a 429 asked for.
		for (const value of ["soon", "", "-1", "Wed, 21 Oct 2015 07:28:00 GMT", null, undefined]) {
			expect(parseRetryAfterSeconds(value)).toBeNull();
		}
	});
});

describe("CompanionExecutor rate-limit backoff", () => {
	it("honours a Retry-After header exactly, with no jitter added on top", async () => {
		// The server has given an authoritative answer. Second-guessing it with extra backoff is
		// how a client turns one rate limit into two.
		const harness = backoffHarness(
			[
				{ status: 429, body: "", retryAfter: "7" },
				{ status: 200, body: '{"items":[]}' },
			],
			[1],
		);
		const logs = captureLogs();
		restorers.push(logs.restore);

		const outcome = await harness.executor.runOnce();

		expect(outcome).toEqual({ outcome: "executed", jti: "0123456789abcdef", status: 200 });
		expect(harness.delays).toEqual([7000]);
		expect(harness.attempts()).toBe(2);
	});

	it("takes the jittered path when Retry-After cannot be parsed", async () => {
		// The draw is pinned at the ceiling, so an implementation that read "soon" as zero and
		// retried immediately would show a delay of 0 here and fail.
		const harness = backoffHarness(
			[
				{ status: 429, body: "", retryAfter: "soon" },
				{ status: 200, body: "{}" },
			],
			[1],
		);

		const outcome = await harness.executor.runOnce();

		expect(outcome.outcome).toBe("executed");
		expect(harness.delays).toEqual([COMPANION_BACKOFF_BASE_MS]);
	});

	it("draws each delay from zero to the current ceiling, and doubles the ceiling", async () => {
		// Full jitter is the load-bearing part: without randomness, concurrent retries synchronize
		// into bursts that generate more 429s than they avoid.
		const draws = [0, 0.5, 1, 0.25];
		const harness = backoffHarness([{ status: 429, body: "", retryAfter: null }], draws);

		const outcome = await harness.executor.runOnce();

		expect(outcome.outcome).toBe("backed_off");
		const ceilings = draws.map((_, index) => COMPANION_BACKOFF_BASE_MS * 2 ** index);
		expect(harness.delays).toEqual([0, 1000, 4000, 2000]);
		harness.delays.forEach((delay, index) => {
			expect(delay).toBeGreaterThanOrEqual(0);
			expect(delay).toBeLessThanOrEqual(ceilings[index] as number);
		});
	});

	it("bounds the attempts and reports a failure carrying the observed retry-after", async () => {
		const harness = backoffHarness([{ status: 429, body: "", retryAfter: "3" }], [0]);

		const outcome = await harness.executor.runOnce();

		expect(harness.attempts()).toBe(COMPANION_MAX_RATE_LIMIT_ATTEMPTS);
		expect(harness.delays).toHaveLength(COMPANION_MAX_RATE_LIMIT_ATTEMPTS - 1);
		expect(outcome).toEqual({
			outcome: "backed_off",
			jti: "0123456789abcdef",
			attempts: COMPANION_MAX_RATE_LIMIT_ATTEMPTS,
			retryAfterSeconds: 3,
		});
		// Reported back rather than dropped: the façade is holding a request open on this call, and
		// the control plane's own bucket learns the companion's real limit from this field.
		expect(harness.reported).toHaveLength(1);
		expect(harness.reported[0]?.retry_after_seconds).toBe(3);
		expect(harness.reported[0]?.error).toBeTruthy();
	});

	it("logs each backoff with its attempt number and delay", async () => {
		// A slowed call must be distinguishable from a hung one in the runtime log.
		const harness = backoffHarness([{ status: 429, body: "", retryAfter: "2" }], [0]);
		const logs = captureLogs();
		restorers.push(logs.restore);

		await harness.executor.runOnce();
		logs.restore();

		const backedOff = logs.lines.filter((line) =>
			line.includes("daemon.companion_dispatch_backed_off"),
		);
		expect(backedOff).toHaveLength(COMPANION_MAX_RATE_LIMIT_ATTEMPTS - 1);
		for (const [index, line] of backedOff.entries()) {
			expect(line).toContain(`"attempt":${index + 1}`);
			expect(line).toContain('"delay_ms":2000');
		}
	});

	it("reports the observed retry-after alongside a call that eventually succeeded", async () => {
		const harness = backoffHarness(
			[
				{ status: 429, body: "", retryAfter: "4" },
				{ status: 200, body: "{}" },
			],
			[0],
		);

		await harness.executor.runOnce();

		expect(harness.reported[0]?.retry_after_seconds).toBe(4);
		expect(harness.reported[0]?.status).toBe(200);
	});

	it("does not back off, delay or log for a non-429 response", async () => {
		// The retry path must be reachable ONLY from a 429. A 500 that silently retried four times
		// would quadruple the load on an already-failing companion.
		const harness = backoffHarness([{ status: 500, body: "boom" }], [0]);

		const outcome = await harness.executor.runOnce();

		expect(outcome).toEqual({ outcome: "executed", jti: "0123456789abcdef", status: 500 });
		expect(harness.delays).toEqual([]);
		expect(harness.reported[0]?.retry_after_seconds).toBeUndefined();
	});
});

// ---------------------------------------------------------------------------
// 05-13 (WR-06b): parameter placement is the registry's DECLARED per-verb property, not a branch
// on `call.method`.
//
// Before this plan `create_dir`, `write_file` and `move_path` sent a JSON body with no query
// string at all, and nothing in this repository had ever dispatched one of them — the shared test
// stub implemented only `do_GET` and `do_DELETE`. These cases are the first assertion of what
// those three verbs put on the wire.
//
// What they prove is the SHAPE the executor sends. They prove nothing about what a live
// filebrowser accepts: research assumptions A2 and A3 stay open and
// `scripts/probe-companion-assertion.ts` is what settles them when a provisioned host exists
// (WINDOWS #133).
// ---------------------------------------------------------------------------

describe("CompanionExecutor argument placement", () => {
	const restorers: Array<() => void> = [];

	afterEach(() => {
		while (restorers.length) restorers.pop()?.();
	});

	/** Record every outgoing request without letting one reach the network. */
	function recordFetch(): Array<{
		url: string;
		method: string;
		body: string | null;
		headers: Record<string, string>;
	}> {
		const requests: Array<{
			url: string;
			method: string;
			body: string | null;
			headers: Record<string, string>;
		}> = [];
		const originalFetch = globalThis.fetch;
		globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
			const request = new Request(input as RequestInfo, init);
			requests.push({
				url: request.url,
				method: request.method,
				body: request.body === null ? null : await request.text(),
				headers: Object.fromEntries(request.headers.entries()),
			});
			return new Response("{}", { status: 200 });
		}) as typeof fetch;
		restorers.push(() => {
			globalThis.fetch = originalFetch;
		});
		return requests;
	}

	function executorFor(call: CompanionDispatchCall): CompanionExecutor {
		return new CompanionExecutor({
			abort: new AbortController().signal,
			environmentId: "env-1",
			paths: getRuntimePaths(),
			claim: async () => ({ call, claimId: "claim-1" }),
			report: async () => {},
			readCompanionAuth: () => AUTH,
			now: () => NOW_MS,
		});
	}

	it("puts a create_dir's arguments in the query string and sends no body", async () => {
		const requests = recordFetch();
		const logs = captureLogs();
		restorers.push(logs.restore);

		const outcome = await executorFor(
			dispatchCall({ verb: "create_dir", method: "POST", arguments: { path: "/reports" } }),
		).runOnce();

		expect(outcome.outcome).toBe("executed");
		expect(requests).toHaveLength(1);
		expect(requests[0]?.url).toBe(
			"http://127.0.0.1:9120/api/resources?path=%2Freports&source=Files",
		);
		expect(requests[0]?.method).toBe("POST");
		expect(requests[0]?.body).toBeNull();
		expect(requests[0]?.headers["content-type"]).toBeUndefined();
	});

	it("puts a write_file's arguments in the query string and sends no body", async () => {
		const requests = recordFetch();
		const logs = captureLogs();
		restorers.push(logs.restore);

		const outcome = await executorFor(
			dispatchCall({ verb: "write_file", method: "POST", arguments: { path: "/notes.md" } }),
		).runOnce();

		expect(outcome.outcome).toBe("executed");
		expect(requests[0]?.url).toBe(
			"http://127.0.0.1:9120/api/resources?path=%2Fnotes.md&source=Files",
		);
		expect(requests[0]?.body).toBeNull();
	});

	it("carries BOTH path and destination in a move_path's query string", async () => {
		const requests = recordFetch();
		const logs = captureLogs();
		restorers.push(logs.restore);

		const outcome = await executorFor(
			dispatchCall({
				verb: "move_path",
				method: "PATCH",
				arguments: { path: "/notes.md", destination: "/archive/notes.md" },
			}),
		).runOnce();

		expect(outcome.outcome).toBe("executed");
		const url = new URL(requests[0]?.url as string);
		expect(url.searchParams.get("path")).toBe("/notes.md");
		expect(url.searchParams.get("destination")).toBe("/archive/notes.md");
		expect(requests[0]?.method).toBe("PATCH");
		expect(requests[0]?.body).toBeNull();
	});

	it("leaves list_dir and delete_path exactly as they are today", async () => {
		const requests = recordFetch();
		const logs = captureLogs();
		restorers.push(logs.restore);

		await executorFor(dispatchCall()).runOnce();
		await executorFor(
			dispatchCall({ verb: "delete_path", method: "DELETE", arguments: { path: "/gone" } }),
		).runOnce();

		expect(requests[0]?.url).toBe("http://127.0.0.1:9120/api/resources?path=%2F&source=Files");
		expect(requests[0]?.method).toBe("GET");
		expect(requests[0]?.body).toBeNull();
		expect(requests[1]?.url).toBe("http://127.0.0.1:9120/api/resources?path=%2Fgone&source=Files");
		expect(requests[1]?.method).toBe("DELETE");
		expect(requests[1]?.body).toBeNull();
	});

	it("refuses an envelope whose verb owns no registry row, before any request is issued", async () => {
		// T-05-13-03. An envelope the host's registry copy does not recognise must not be executed
		// with a guessed placement. The stubbed fetch NEVER being called is the assertion — a
		// refusal that happened after the request would satisfy an outcome check and miss this.
		const requests = recordFetch();
		const logs = captureLogs();
		restorers.push(logs.restore);

		const outcome = await executorFor(
			dispatchCall({ verb: "shred_everything", method: "DELETE" }),
		).runOnce();

		expect(outcome.outcome).toBe("failed");
		expect(requests).toEqual([]);
	});

	it("still refuses an envelope naming a companion kind with no registry row", async () => {
		const requests = recordFetch();
		const logs = captureLogs();
		restorers.push(logs.restore);

		// Not `postiz`: since 06-03 that kind OWNS a registry row, so naming it here would exercise a
		// different refusal and leave this case asserting nothing about a row-less kind.
		const outcome = await executorFor(dispatchCall({ companion_key: "nosuchkind" })).runOnce();

		expect(outcome.outcome).toBe("failed");
		expect(requests).toEqual([]);
	});
});

/**
 * Per-kind request authentication (06-07).
 *
 * filebrowser authenticates each call with a freshly minted JWT assertion. Postiz's public API
 * takes a static API key in a bare `Authorization` header with no `Bearer` prefix
 * (`public.auth.middleware.ts` at v2.24.0). The key is read from the root-only cached secret
 * values the manifest's `credentials.apiKeySecretRef` names, and it must never reach a log line, a
 * reported body or an error message.
 */
describe("CompanionExecutor per-kind authentication", () => {
	const restorers: Array<() => void> = [];

	afterEach(() => {
		while (restorers.length) restorers.pop()?.();
	});

	/** 44 characters, none of which the stub response or any log line legitimately carries. */
	const POSTIZ_KEY = "POSTIZ_SENTINEL_KEY_DO_NOT_LEAK_abcdef123456";
	const POSTIZ_FIXTURE = JSON.parse(
		readFileSync(
			resolve(import.meta.dir, "../../../../test-fixtures/companions/postiz-manifest-entry.json"),
			"utf-8",
		),
	) as { credentials: { apiKeySecretRef: string } };
	const API_KEY_REF = POSTIZ_FIXTURE.credentials.apiKeySecretRef;

	/** A host whose last-good manifest declares both kinds and whose secret cache is `cached`. */
	function hostPaths(cached: Record<string, string> | null): RuntimePaths {
		const root = mkdtempSync(join(tmpdir(), "iforge-companion-auth-"));
		restorers.push(() => rmSync(root, { recursive: true, force: true }));
		const paths: RuntimePaths = {
			...getRuntimePaths(),
			manifestLastGood: join(root, "manifest.last-good.json"),
			managedSecretCacheFile: join(root, "runtime-secrets.last-good.json"),
		};
		writeFileSync(
			paths.manifestLastGood,
			JSON.stringify({
				manifest: { companions: { filebrowser: { auth: AUTH }, postiz: POSTIZ_FIXTURE } },
			}),
		);
		if (cached !== null) writeFileSync(paths.managedSecretCacheFile, JSON.stringify(cached));
		return paths;
	}

	function recordFetch(
		response: () => Response = () => new Response('[{"id":"int-1"}]', { status: 200 }),
	): Array<{ url: string; method: string; body: string | null; headers: Record<string, string> }> {
		const requests: Array<{
			url: string;
			method: string;
			body: string | null;
			headers: Record<string, string>;
		}> = [];
		const originalFetch = globalThis.fetch;
		globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
			const request = new Request(input as RequestInfo, init);
			requests.push({
				url: request.url,
				method: request.method,
				body: request.body === null ? null : await request.text(),
				headers: Object.fromEntries(request.headers.entries()),
			});
			return response();
		}) as typeof fetch;
		restorers.push(() => {
			globalThis.fetch = originalFetch;
		});
		return requests;
	}

	function postizCall(overrides: Partial<CompanionDispatchCall> = {}): CompanionDispatchCall {
		return dispatchCall({
			companion_key: "postiz",
			verb: "list_integrations",
			method: "GET",
			path_template: "/api/public/v1/integrations",
			arguments: {},
			...overrides,
		});
	}

	function hostExecutor(
		call: CompanionDispatchCall,
		paths: RuntimePaths,
		reported: Array<Record<string, unknown>> = [],
	): CompanionExecutor {
		return new CompanionExecutor({
			abort: new AbortController().signal,
			environmentId: "env-1",
			paths,
			claim: async () => ({ call, claimId: "claim-1" }),
			report: async (jti, claimId, outcome) => {
				reported.push({ jti, claimId, ...outcome });
			},
			now: () => NOW_MS,
		});
	}

	/** Every 8-character window of the key: a leak of any fragment of it is still a leak. */
	function keyFragments(key: string): string[] {
		const fragments: string[] = [];
		for (let start = 0; start + 8 <= key.length; start += 1) {
			fragments.push(key.slice(start, start + 8));
		}
		return fragments;
	}

	it("reads the postiz key from the cached secret values the manifest's apiKeySecretRef names", () => {
		const paths = hostPaths({ [API_KEY_REF]: POSTIZ_KEY });

		expect(companionExecutor.readCompanionRequestAuth(paths, "postiz")).toEqual({
			kind: "static-header",
			header: "Authorization",
			value: POSTIZ_KEY,
		});
	});

	it("reads filebrowser's JWT material exactly as before", () => {
		const paths = hostPaths({ [API_KEY_REF]: POSTIZ_KEY });

		expect(companionExecutor.readCompanionRequestAuth(paths, "filebrowser")).toEqual({
			kind: "jwt-assertion",
			material: AUTH,
		});
		expect(companionExecutor.readCompanionRequestAuth(paths, "nosuchkind")).toBeNull();
	});

	it("sends a postiz call the cached key as a bare Authorization header", async () => {
		const requests = recordFetch();
		const logs = captureLogs();
		restorers.push(logs.restore);

		const outcome = await hostExecutor(
			postizCall(),
			hostPaths({ [API_KEY_REF]: POSTIZ_KEY }),
		).runOnce();

		expect(outcome).toEqual({ outcome: "executed", jti: "0123456789abcdef", status: 200 });
		expect(requests).toHaveLength(1);
		const sent = requests[0];
		// Host and port from the postiz registry row; no fixed query, because postiz has none.
		expect(sent?.url).toBe("http://127.0.0.1:9130/api/public/v1/integrations");
		expect(sent?.method).toBe("GET");
		expect(sent?.headers.authorization).toBe(POSTIZ_KEY);
		expect(sent?.headers.authorization?.startsWith("Bearer ")).toBe(false);
		expect(sent?.headers["x-jwt-assertion"]).toBeUndefined();
	});

	it("sends create_post's nested body as JSON with the same header", async () => {
		const requests = recordFetch(() => new Response("[]", { status: 201 }));
		const logs = captureLogs();
		restorers.push(logs.restore);
		const body = {
			type: "schedule",
			date: "2026-10-01T12:00:00Z",
			shortLink: false,
			tags: [{ value: "launch", label: "Launch" }],
			posts: [{ integration: { id: "int-1" }, value: [{ content: "We ship today", image: [] }] }],
		};

		const outcome = await hostExecutor(
			postizCall({
				verb: "create_post",
				method: "POST",
				path_template: "/api/public/v1/posts",
				arguments: body,
			}),
			hostPaths({ [API_KEY_REF]: POSTIZ_KEY }),
		).runOnce();

		expect(outcome).toEqual({ outcome: "executed", jti: "0123456789abcdef", status: 201 });
		expect(requests[0]?.url).toBe("http://127.0.0.1:9130/api/public/v1/posts");
		expect(requests[0]?.headers["content-type"]).toBe("application/json");
		expect(requests[0]?.headers.authorization).toBe(POSTIZ_KEY);
		expect(JSON.parse(requests[0]?.body as string)).toEqual(body);
	});

	it("decodes an envelope whose body arguments nest, and still refuses an undeclared field", () => {
		const nested = postizCall({
			verb: "create_post",
			method: "POST",
			path_template: "/api/public/v1/posts",
			arguments: { posts: [{ integration: { id: "int-1" }, value: [{ content: "x" }] }] },
		});
		const token = `x.${Buffer.from(JSON.stringify(nested)).toString("base64url")}.y`;

		expect(decodeDispatchCall(token).arguments).toEqual(nested.arguments);
		const extra = `x.${Buffer.from(JSON.stringify({ ...nested, key: POSTIZ_KEY })).toString(
			"base64url",
		)}.y`;
		expect(() => decodeDispatchCall(extra)).toThrow();
	});

	it("refuses a nested value in a query-placed verb rather than sending '[object Object]'", async () => {
		const requests = recordFetch();
		const logs = captureLogs();
		restorers.push(logs.restore);

		const outcome = await hostExecutor(
			postizCall({
				verb: "list_posts",
				path_template: "/api/public/v1/posts",
				arguments: {
					startDate: { $gt: "" },
				} as unknown as CompanionDispatchCall["arguments"],
			}),
			hostPaths({ [API_KEY_REF]: POSTIZ_KEY }),
		).runOnce();

		expect(outcome.outcome).toBe("failed");
		expect(requests).toEqual([]);
	});

	it("still signs a filebrowser call with a JWT assertion when the seam returns the union", async () => {
		const requests = recordFetch();
		const logs = captureLogs();
		restorers.push(logs.restore);
		const executor = new CompanionExecutor({
			abort: new AbortController().signal,
			environmentId: "env-1",
			paths: getRuntimePaths(),
			claim: async () => ({ call: dispatchCall(), claimId: "claim-1" }),
			report: async () => {},
			readCompanionAuth: () => ({ kind: "jwt-assertion", material: AUTH }),
			now: () => NOW_MS,
		});

		await executor.runOnce();

		expect(requests[0]?.url).toBe("http://127.0.0.1:9120/api/resources?path=%2F&source=Files");
		expect(requests[0]?.headers["x-jwt-assertion"]).toBeTruthy();
		expect(requests[0]?.headers.authorization).toBeUndefined();
	});

	it("fails a postiz call with no cached key before any request, naming the ref and not a value", async () => {
		const requests = recordFetch();
		const logs = captureLogs();
		restorers.push(logs.restore);
		const reported: Array<Record<string, unknown>> = [];

		const outcome = await hostExecutor(
			postizCall(),
			hostPaths({ "secret://some/other/ref": POSTIZ_KEY }),
			reported,
		).runOnce();

		expect(outcome.outcome).toBe("failed");
		expect(requests).toEqual([]);
		expect(reported).toHaveLength(1);
		const error = String(reported[0]?.error);
		expect(error).toContain(API_KEY_REF);
		for (const fragment of keyFragments(POSTIZ_KEY)) {
			expect(error).not.toContain(fragment);
		}
	});

	it("never places any fragment of the key in a log line or a reported body", async () => {
		// The companion's answer echoes nothing of the key; everything the executor itself writes is
		// what is under test, across a success, a backed-off 429 and a transport failure.
		const reported: Array<Record<string, unknown>> = [];
		const logs = captureLogs();
		restorers.push(logs.restore);
		const paths = hostPaths({ [API_KEY_REF]: POSTIZ_KEY });

		recordFetch(() => new Response('[{"id":"int-1"}]', { status: 200 }));
		await hostExecutor(postizCall(), paths, reported).runOnce();
		restorers.pop()?.();
		recordFetch(() => new Response("{}", { status: 429, headers: { "retry-after": "0" } }));
		await hostExecutor(postizCall(), paths, reported).runOnce();
		restorers.pop()?.();
		const originalFetch = globalThis.fetch;
		globalThis.fetch = (async () => {
			throw new Error("connection refused");
		}) as unknown as typeof fetch;
		restorers.push(() => {
			globalThis.fetch = originalFetch;
		});
		await hostExecutor(postizCall(), paths, reported).runOnce();

		expect(reported).toHaveLength(3);
		expect(logs.lines.length).toBeGreaterThan(0);
		const written = [...logs.lines, JSON.stringify(reported)].join("\n");
		for (const fragment of keyFragments(POSTIZ_KEY)) {
			expect(written).not.toContain(fragment);
		}
	});

	it("applies no fixed query to postiz", () => {
		expect(companionFixedQuery("postiz")).toEqual({});
	});
});
