import { afterEach, describe, expect, test } from "bun:test";
import type { RuntimeUiCredentials } from "@clawdi/shared/api";
import type { ClawdiDesktopShellBridge } from "@clawdi/shared/desktop";
import {
	type FilesGrantDependencies,
	type FilesGrantRequest,
	type OpenFilesWindowDependencies,
	openFilesInNewWindow,
	primeFilesForPresentation,
} from "@/hosted/agents/use-files-grant-bootstrap";

/**
 * UI-SPEC G2 (SC-4, PROV-14): both Files surfaces obtain a freshly issued
 * runtime-UI grant via getRuntimeUiCredentials(deploymentId, resourceVersion)
 * and prime the Files origin with it. The Clerk session token never reaches the
 * Files origin, and a failed attempt's grant is never presented again.
 *
 * apps/web has no DOM renderer, so these cases drive the module seams the hooks
 * call: `primeFilesForPresentation` (each `useFilesGrantBootstrap` mount) and
 * `openFilesInNewWindow` (each `useOpenFilesInNewWindow` click). Requests to the
 * Files origin are observed through a recording `fetch`.
 */

const CLERK_TOKEN = "clerk-session-token-must-not-leave-the-control-plane";
const RUNTIME_ENDPOINT = "https://agent-1.runtime.example/";
const FILES_URL = "https://files-agent-1.runtime.example/";
const POPUP_BLOCKED = "Allow pop-ups for iforge, then try again.";
const PRIME_FAILED = "Files access couldn't be authenticated. Refresh the page and try again.";

type RecordedRequest = { method: string; url: string; init: RequestInit | undefined };

const originalFetch = globalThis.fetch;
afterEach(() => {
	globalThis.fetch = originalFetch;
});

function grant(token: string, resourceVersion: string): RuntimeUiCredentials {
	return {
		runtime: "openclaw",
		auth_mode: "openclaw_token",
		url: RUNTIME_ENDPOINT,
		deployment_resource_version: resourceVersion,
		token,
		handoff_url: `${RUNTIME_ENDPOINT}#token=${encodeURIComponent(token)}`,
	};
}

function authorization(request: RecordedRequest): string | null {
	return new Headers(request.init?.headers).get("authorization");
}

type Harness = {
	log: string[];
	requests: RecordedRequest[];
	issues: Array<{ id: string; resourceVersion: string }>;
	getTokenCalls: () => number;
	deps: FilesGrantDependencies & { getToken: () => Promise<string> };
};

/** `statuses` answers successive Files requests; the last value repeats. */
function harness(statuses: number[] = [200]): Harness {
	const log: string[] = [];
	const requests: RecordedRequest[] = [];
	const issues: Array<{ id: string; resourceVersion: string }> = [];
	let getTokenCalls = 0;
	globalThis.fetch = Object.assign(
		async (input: string | URL | Request, init?: RequestInit) => {
			const url = input instanceof Request ? input.url : String(input);
			const method = init?.method ?? "GET";
			requests.push({ method, url, init });
			log.push(`${method} ${url}`);
			const status = statuses[Math.min(requests.length - 1, statuses.length - 1)] ?? 200;
			return new Response(null, { status });
		},
		{ preconnect: originalFetch.preconnect },
	);
	return {
		log,
		requests,
		issues,
		getTokenCalls: () => getTokenCalls,
		deps: {
			client: {
				getRuntimeUiCredentials: async (id: string, resourceVersion: string) => {
					issues.push({ id, resourceVersion });
					log.push(`issue ${id} ${resourceVersion}`);
					return grant(`grant-${issues.length}`, resourceVersion);
				},
			},
			// Present so the cases can prove it is never consulted.
			getToken: async () => {
				getTokenCalls += 1;
				return CLERK_TOKEN;
			},
		},
	};
}

const REQUEST: FilesGrantRequest = {
	url: FILES_URL,
	deploymentId: "hdep_1",
	resourceVersion: "rv-1",
};

type FakePopup = {
	opener: unknown;
	closed: boolean;
	close(): void;
	location: { replace(url: string | URL): void };
};

function windowHarness(h: Harness, options: { blocked?: boolean; desktop?: boolean } = {}) {
	const toasts: Array<{ message: string; id?: string; description?: string }> = [];
	const tracked: string[] = [];
	let popup: FakePopup | null = null;
	const desktopBridge = options.desktop
		? ({
				openFilesWindow: async (url: string) => {
					h.log.push(`desktop ${url}`);
					return true;
				},
			} as unknown as ClawdiDesktopShellBridge)
		: null;
	const deps: OpenFilesWindowDependencies = {
		...h.deps,
		desktopBridge,
		openWindow: (url) => {
			h.log.push(`reserve ${String(url)}`);
			if (options.blocked) return null;
			popup = {
				opener: "dashboard",
				closed: false,
				close() {
					h.log.push("popup close");
					this.closed = true;
				},
				location: {
					replace: (target) => {
						h.log.push(`navigate ${String(target)}`);
					},
				},
			};
			return popup;
		},
		trackWindow: (deploymentId) => {
			tracked.push(deploymentId);
		},
		toastError: (message, opts) => {
			toasts.push({ message, id: opts?.id, description: opts?.description });
			return "toast";
		},
	};
	return { deps, toasts, tracked, popup: () => popup };
}

describe("useFilesGrantBootstrap priming (UI-SPEC G2)", () => {
	test("issues a grant for the deployment before the Files request", async () => {
		const h = harness();
		await primeFilesForPresentation(REQUEST, h.deps);
		expect(h.issues).toEqual([{ id: "hdep_1", resourceVersion: "rv-1" }]);
		expect(h.log).toEqual(["issue hdep_1 rv-1", `GET ${FILES_URL}`]);
	});

	test("primes Files with the issued grant in the standing request shape", async () => {
		const h = harness();
		await primeFilesForPresentation(REQUEST, h.deps);
		const [request] = h.requests;
		expect(request?.method).toBe("GET");
		expect(request && authorization(request)).toBe("Bearer grant-1");
		expect(request?.init?.credentials).toBe("include");
		expect(request?.init?.cache).toBe("no-store");
	});

	test("never sends the Clerk getToken() result to the Files origin", async () => {
		const h = harness();
		await primeFilesForPresentation(REQUEST, h.deps);
		expect(h.requests.length).toBe(1);
		expect(h.requests.map(authorization)).not.toContain(`Bearer ${CLERK_TOKEN}`);
		expect(h.getTokenCalls()).toBe(0);
	});

	test("a refresh after a failed prime issues a fresh grant", async () => {
		const h = harness([401, 200]);
		await expect(primeFilesForPresentation(REQUEST, h.deps)).rejects.toThrow();
		await primeFilesForPresentation(REQUEST, h.deps);
		expect(h.issues).toHaveLength(2);
		expect(h.requests.map(authorization)).toEqual(["Bearer grant-1", "Bearer grant-2"]);
	});
});

describe("useOpenFilesInNewWindow priming (UI-SPEC G2)", () => {
	test("reserves the popup, then issues, then primes, then navigates", async () => {
		const h = harness();
		const w = windowHarness(h);
		await openFilesInNewWindow(REQUEST, w.deps);
		expect(h.log).toEqual([
			"reserve about:blank",
			"issue hdep_1 rv-1",
			`GET ${FILES_URL}`,
			`navigate ${FILES_URL}`,
		]);
		expect(w.tracked).toEqual(["hdep_1"]);
		expect(w.popup()?.opener).toBeNull();
		expect(w.toasts).toEqual([]);
	});

	test("primes with the issued grant and never the Clerk token", async () => {
		const h = harness();
		await openFilesInNewWindow(REQUEST, windowHarness(h).deps);
		expect(h.requests.map(authorization)).toEqual(["Bearer grant-1"]);
		expect(h.getTokenCalls()).toBe(0);
	});

	test("a failed prime closes the popup and toasts the standing copy without navigating", async () => {
		const h = harness([401]);
		const w = windowHarness(h);
		await openFilesInNewWindow(REQUEST, w.deps);
		expect(h.log).toEqual([
			"reserve about:blank",
			"issue hdep_1 rv-1",
			`GET ${FILES_URL}`,
			"popup close",
		]);
		expect(w.toasts).toEqual([
			{ message: "Couldn't open Files", id: "files-window-launch", description: PRIME_FAILED },
		]);
	});

	test("a retry after a failed attempt issues a fresh grant", async () => {
		const h = harness([401, 200]);
		await openFilesInNewWindow(REQUEST, windowHarness(h).deps);
		await openFilesInNewWindow(REQUEST, windowHarness(h).deps);
		expect(h.issues).toHaveLength(2);
		expect(h.requests.map(authorization)).toEqual(["Bearer grant-1", "Bearer grant-2"]);
	});

	test("a blocked popup toasts the standing copy and issues nothing", async () => {
		const h = harness();
		const w = windowHarness(h, { blocked: true });
		await openFilesInNewWindow(REQUEST, w.deps);
		expect(h.issues).toEqual([]);
		expect(h.requests).toEqual([]);
		expect(w.toasts).toEqual([
			{ message: "Couldn't open Files", id: "files-window-launch", description: POPUP_BLOCKED },
		]);
	});

	test("desktop opens the child only after a grant-primed Files request", async () => {
		const h = harness();
		await openFilesInNewWindow(REQUEST, windowHarness(h, { desktop: true }).deps);
		expect(h.log).toEqual(["issue hdep_1 rv-1", `GET ${FILES_URL}`, `desktop ${FILES_URL}`]);
		expect(h.requests.map(authorization)).toEqual(["Bearer grant-1"]);
	});
});
