import { afterEach, describe, expect, test } from "bun:test";
import type { RuntimeUiCredentials } from "@clawdi/shared/api";
import { markOpenClawNativeHandoffLoaded } from "@/hosted/agents/runtime-ui-credentials";
import {
	loadRuntimeUiAccess,
	type RuntimeUiAccessDependencies,
	type RuntimeUiAccessRequest,
} from "@/hosted/agents/use-runtime-ui-credentials";

/**
 * UI-SPEC G1 (SC-4, PROV-14): the OpenClaw console issues a runtime-UI grant
 * before it sends anything to the runtime origin, and primes the browser
 * session with that grant. The Clerk session token authorizes the whole control
 * plane, so it must never reach the runtime origin on any path.
 *
 * apps/web has no DOM renderer, so these cases drive the hook's load chain
 * (`loadRuntimeUiAccess`) directly. `useRuntimeUiCredentials` calls it for every
 * attempt: first load, `load()`, `reconnect()` (`fresh: true`) and the automatic
 * re-run after a resource version change. Requests to the runtime origin are
 * observed through a recording `fetch`.
 */

const CLERK_TOKEN = "clerk-session-token-must-not-leave-the-control-plane";
const ENDPOINT = "https://agent-1.runtime.example/";
const BROWSER_SESSION_URL = new URL(".well-known/openclaw/browser-session", ENDPOINT).href;
const API_ORIGIN = "https://api.iforge.test";
const HERMES_OIDC_URL = `${API_ORIGIN}/v2/deployments/hdep_1/hermes-oidc/session`;

type RecordedRequest = { method: string; url: string; headers: Record<string, string> };

const originalFetch = globalThis.fetch;
afterEach(() => {
	globalThis.fetch = originalFetch;
});

function recordingTransport(log: string[], requests: RecordedRequest[]) {
	globalThis.fetch = Object.assign(
		async (input: string | URL | Request, init?: RequestInit) => {
			const url = input instanceof Request ? input.url : String(input);
			const method = init?.method ?? "GET";
			const headers = Object.fromEntries(new Headers(init?.headers).entries());
			requests.push({ method, url, headers });
			log.push(`${method} ${url}`);
			return new Response(null, { status: 204 });
		},
		{ preconnect: originalFetch.preconnect },
	);
}

function grant(token: string, resourceVersion: string): RuntimeUiCredentials {
	return {
		runtime: "openclaw",
		auth_mode: "openclaw_token",
		url: ENDPOINT,
		deployment_resource_version: resourceVersion,
		token,
		handoff_url: `${ENDPOINT}#token=${encodeURIComponent(token)}`,
	};
}

function memoryStorage(): Storage {
	const items = new Map<string, string>();
	return {
		get length() {
			return items.size;
		},
		clear: () => items.clear(),
		key: (index: number) => [...items.keys()][index] ?? null,
		getItem: (key: string) => items.get(key) ?? null,
		setItem: (key: string, value: string) => void items.set(key, value),
		removeItem: (key: string) => void items.delete(key),
	};
}

type Harness = {
	log: string[];
	requests: RecordedRequest[];
	issues: Array<{ id: string; resourceVersion: string }>;
	getTokenCalls: () => number;
	deps: RuntimeUiAccessDependencies;
};

function harness(
	issue: (resourceVersion: string, n: number) => RuntimeUiCredentials = (resourceVersion, n) =>
		grant(`grant-${n}`, resourceVersion),
	storage: Storage | null = memoryStorage(),
): Harness {
	const log: string[] = [];
	const requests: RecordedRequest[] = [];
	const issues: Array<{ id: string; resourceVersion: string }> = [];
	let getTokenCalls = 0;
	recordingTransport(log, requests);
	return {
		log,
		requests,
		issues,
		getTokenCalls: () => getTokenCalls,
		deps: {
			client: {
				getRuntimeUiCredentials: async (id: string, resourceVersion: string) => {
					issues.push({ id, resourceVersion });
					log.push(`issue ${resourceVersion}`);
					return issue(resourceVersion, issues.length);
				},
			},
			getToken: async () => {
				getTokenCalls += 1;
				return CLERK_TOKEN;
			},
			storage,
		},
	};
}

function openClawRequest(overrides: Partial<RuntimeUiAccessRequest> = {}): RuntimeUiAccessRequest {
	return {
		id: "hdep_1",
		resourceVersion: "rv-1",
		generation: 3,
		runtime: "openclaw",
		endpoint: ENDPOINT,
		storageScope: JSON.stringify(["user_1", "hdep_1"]),
		openClawBrowserSessionUrl: BROWSER_SESSION_URL,
		hermesOidcBrowserSessionUrl: null,
		trustedApiOrigin: API_ORIGIN,
		fresh: false,
		signal: new AbortController().signal,
		current: () => true,
		...overrides,
	};
}

function runtimeOriginRequests(requests: RecordedRequest[]): RecordedRequest[] {
	return requests.filter((request) => new URL(request.url).origin === new URL(ENDPOINT).origin);
}

describe("useRuntimeUiCredentials OpenClaw priming (UI-SPEC G1)", () => {
	test("issues the grant before any request to the runtime origin", async () => {
		const h = harness();
		await loadRuntimeUiAccess(openClawRequest(), h.deps);
		expect(h.issues).toEqual([{ id: "hdep_1", resourceVersion: "rv-1" }]);
		expect(h.log).toEqual([
			"issue rv-1",
			`POST ${BROWSER_SESSION_URL}`,
			`HEAD ${BROWSER_SESSION_URL}`,
		]);
	});

	test("primes the browser session with the issued grant and the resource version", async () => {
		const h = harness();
		const outcome = await loadRuntimeUiAccess(openClawRequest(), h.deps);
		const [bootstrap] = runtimeOriginRequests(h.requests);
		expect(bootstrap?.method).toBe("POST");
		expect(bootstrap?.headers.authorization).toBe("Bearer grant-1");
		expect(bootstrap?.headers["if-match"]).toBe('"rv-1"');
		expect(outcome).toEqual({ kind: "credentials", credentials: grant("grant-1", "rv-1") });
	});

	test("never sends the Clerk getToken() result to the runtime origin", async () => {
		const h = harness();
		await loadRuntimeUiAccess(openClawRequest(), h.deps);
		await loadRuntimeUiAccess(openClawRequest({ fresh: true }), h.deps);
		const runtimeLog = JSON.stringify(runtimeOriginRequests(h.requests));
		expect(runtimeOriginRequests(h.requests).length).toBeGreaterThan(0);
		expect(runtimeLog).not.toContain(CLERK_TOKEN);
		expect(h.getTokenCalls()).toBe(0);
	});

	test("reconnect issues a second grant and re-primes with it, never the first", async () => {
		const h = harness();
		await loadRuntimeUiAccess(openClawRequest(), h.deps);
		await loadRuntimeUiAccess(openClawRequest({ fresh: true }), h.deps);
		expect(h.issues).toHaveLength(2);
		const bootstraps = runtimeOriginRequests(h.requests).filter((r) => r.method === "POST");
		expect(bootstraps.map((r) => r.headers.authorization)).toEqual([
			"Bearer grant-1",
			"Bearer grant-2",
		]);
	});

	test("a resource version change re-issues against the new version", async () => {
		const h = harness();
		await loadRuntimeUiAccess(openClawRequest(), h.deps);
		await loadRuntimeUiAccess(openClawRequest({ resourceVersion: "rv-2" }), h.deps);
		expect(h.issues.map((issue) => issue.resourceVersion)).toEqual(["rv-1", "rv-2"]);
		const bootstraps = runtimeOriginRequests(h.requests).filter((r) => r.method === "POST");
		expect(bootstraps.map((r) => [r.headers.authorization, r.headers["if-match"]])).toEqual([
			["Bearer grant-1", '"rv-1"'],
			["Bearer grant-2", '"rv-2"'],
		]);
	});

	test("a loaded native handoff still primes with a fresh grant and skips the credentials", async () => {
		const storage = memoryStorage();
		const scope = openClawRequest().storageScope;
		const native = {
			...grant("grant-native", "rv-1"),
			handoff_url: `${ENDPOINT}#bootstrapToken=one-time&bootstrapProfile=owner`,
		};
		expect(markOpenClawNativeHandoffLoaded(storage, scope, ENDPOINT, native, 3)).toBe(true);
		const h = harness(undefined, storage);
		const outcome = await loadRuntimeUiAccess(openClawRequest(), h.deps);
		expect(outcome).toEqual({ kind: "native_handoff_loaded" });
		expect(h.log[0]).toBe("issue rv-1");
		const [bootstrap] = runtimeOriginRequests(h.requests);
		expect(bootstrap?.headers.authorization).toBe("Bearer grant-1");
		expect(h.getTokenCalls()).toBe(0);
	});

	test("an invalid issue response throws before any runtime-origin request", async () => {
		const h = harness((_resourceVersion, n) => grant(`grant-${n}`, "rv-stale"));
		await expect(loadRuntimeUiAccess(openClawRequest(), h.deps)).rejects.toThrow(
			"Invalid runtime access response.",
		);
		expect(runtimeOriginRequests(h.requests)).toEqual([]);
	});
});

describe("useRuntimeUiCredentials Hermes OIDC priming (out of scope, pinned)", () => {
	test("keeps sending the Clerk token and never issues a runtime-UI grant", async () => {
		const h = harness();
		const outcome = await loadRuntimeUiAccess(
			openClawRequest({
				runtime: "hermes",
				openClawBrowserSessionUrl: null,
				hermesOidcBrowserSessionUrl: HERMES_OIDC_URL,
			}),
			h.deps,
		);
		expect(outcome).toEqual({ kind: "hermes_oidc_primed" });
		expect(h.issues).toEqual([]);
		expect(h.requests).toEqual([
			{
				method: "POST",
				url: HERMES_OIDC_URL,
				headers: { authorization: `Bearer ${CLERK_TOKEN}`, "if-match": '"rv-1"' },
			},
		]);
	});
});
