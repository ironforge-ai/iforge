import { beforeEach, describe, expect, it } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import {
	callClawdiMcp,
	createClawdiMcpServer,
	createTransparentMcpHandlers,
	createTransparentMcpServer,
	resetClawdiMcpSessionForTesting,
} from "./server";

describe("MCP stdio proxy", () => {
	it("throws instead of exiting when there is no CLI auth", async () => {
		const previousClawdiHome = process.env.CLAWDI_HOME;
		const previousAuthToken = process.env.CLAWDI_AUTH_TOKEN;
		const clawdiHome = mkdtempSync(join(tmpdir(), "clawdi-mcp-auth-"));
		process.env.CLAWDI_HOME = clawdiHome;
		delete process.env.CLAWDI_AUTH_TOKEN;
		try {
			await expect(createClawdiMcpServer()).rejects.toThrow("Not logged in");
		} finally {
			if (previousClawdiHome === undefined) delete process.env.CLAWDI_HOME;
			else process.env.CLAWDI_HOME = previousClawdiHome;
			if (previousAuthToken === undefined) delete process.env.CLAWDI_AUTH_TOKEN;
			else process.env.CLAWDI_AUTH_TOKEN = previousAuthToken;
			rmSync(clawdiHome, { recursive: true, force: true });
		}
	});

	it.each(["tools/list", "tools/call"])("aborts stalled %s without retrying", async (method) => {
		const previousAuthToken = process.env.CLAWDI_AUTH_TOKEN;
		const previousAuthTokenOrigin = process.env.CLAWDI_AUTH_TOKEN_ORIGIN;
		const previousApiUrl = process.env.CLAWDI_API_URL;
		const originalFetch = globalThis.fetch;
		process.env.CLAWDI_AUTH_TOKEN = "test-mcp-token";
		process.env.CLAWDI_AUTH_TOKEN_ORIGIN = "http://localhost:8000";
		process.env.CLAWDI_API_URL = "http://localhost:8000";
		let aborted = false;
		let attempts = 0;
		// The fork's proxy runs one bounded `initialize` handshake per process before its first
		// forwarded call (the Mcp-Session-Id header). That handshake is not a retry of the stalled
		// request, so only forwarded requests are counted.
		resetClawdiMcpSessionForTesting();
		globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
			const forwarded = JSON.parse(String(init?.body ?? "{}")) as { method?: string };
			if (forwarded.method !== "initialize") attempts += 1;
			const body = new ReadableStream<Uint8Array>({
				start(controller) {
					init?.signal?.addEventListener(
						"abort",
						() => {
							aborted = true;
							controller.error(new DOMException("Aborted", "AbortError"));
						},
						{ once: true },
					);
				},
			});
			return new Response(body, { status: 200 });
		}) as typeof fetch;
		try {
			await expect(callClawdiMcp(method, {}, 10)).rejects.toThrow(
				method === "tools/call"
					? "execution may still have completed"
					: "request timed out after 10ms",
			);
			expect(aborted).toBe(true);
			expect(attempts).toBe(1);
		} finally {
			globalThis.fetch = originalFetch;
			if (previousAuthToken === undefined) delete process.env.CLAWDI_AUTH_TOKEN;
			else process.env.CLAWDI_AUTH_TOKEN = previousAuthToken;
			if (previousAuthTokenOrigin === undefined) delete process.env.CLAWDI_AUTH_TOKEN_ORIGIN;
			else process.env.CLAWDI_AUTH_TOKEN_ORIGIN = previousAuthTokenOrigin;
			if (previousApiUrl === undefined) delete process.env.CLAWDI_API_URL;
			else process.env.CLAWDI_API_URL = previousApiUrl;
		}
	});

	it("preserves complete runtime tool definitions without a hardcoded catalog", async () => {
		const upstream = {
			tools: [
				{
					name: "COMPOSIO_SEARCH_TOOLS",
					description: "Search current tools",
					inputSchema: {
						type: "object",
						properties: {
							queries: {
								type: "array",
								items: {
									type: "object",
									properties: { known_fields: { type: ["string", "null"] } },
								},
							},
						},
					},
					outputSchema: {
						type: "object",
						properties: { redirect_url: { type: ["string", "null"] } },
					},
					_meta: { composio: { future_contract: true } },
					futureDefinitionField: { preserved: true },
				},
			],
			nextCursor: "next-page",
			_meta: { router: { session: "opaque" } },
		};
		const calls: Array<{ method: string; params?: Record<string, unknown> }> = [];
		const handlers = createTransparentMcpHandlers(async (method, params) => {
			calls.push({ method, params });
			return upstream;
		});
		const params = { cursor: "page-1", _meta: { trace: "opaque" } };

		const result = await handlers.listTools(params);

		expect(result).toBe(upstream);
		expect(calls).toEqual([{ method: "tools/list", params }]);
	});

	it("preserves nested arguments and complete structured tool results", async () => {
		const upstream = {
			content: [
				{
					type: "text",
					text: '{"status":"initiated"}',
					_meta: { composio: { tool: "COMPOSIO_MANAGE_CONNECTIONS" } },
				},
			],
			structuredContent: {
				status: "initiated",
				redirect_url: "https://connect.test/link",
			},
			redirect_url: "https://connect.test/link",
			_meta: { future: { preserved: true } },
			futureResultField: { preserved: true },
		};
		const calls: Array<{ method: string; params?: Record<string, unknown> }> = [];
		const handlers = createTransparentMcpHandlers(async (method, params) => {
			calls.push({ method, params });
			return upstream;
		});
		const params = {
			name: "COMPOSIO_MULTI_EXECUTE_TOOL",
			arguments: {
				tasks: [
					{
						slug: "GITHUB_CREATE_ISSUE",
						arguments: { title: "Bug", labels: ["runtime", "mcp"] },
					},
				],
			},
			_meta: { progressToken: "progress-1" },
		};

		const result = await handlers.callTool(params);

		expect(result).toBe(upstream);
		expect(calls).toEqual([{ method: "tools/call", params }]);
	});

	it("keeps runtime schemas and result extensions across the stdio server protocol", async () => {
		const toolDefinition = {
			name: "COMPOSIO_MANAGE_CONNECTIONS",
			inputSchema: { type: "object" as const, properties: { toolkit: { type: "string" } } },
			outputSchema: {
				type: "object" as const,
				properties: { redirect_url: { type: ["string", "null"] } },
			},
			_meta: { composio: { version: "future" } },
			futureDefinitionField: { preserved: true },
		};
		const toolResult = {
			content: [{ type: "text", text: "connection initiated" }],
			structuredContent: { redirect_url: "https://connect.test/link" },
			redirect_url: "https://connect.test/link",
			_meta: { composio: { version: "future" } },
			futureResultField: { preserved: true },
		};
		const server = createTransparentMcpServer(async (method) =>
			method === "tools/list" ? { tools: [toolDefinition] } : toolResult,
		);
		const client = new Client({ name: "clawdi-proxy-test", version: "1.0.0" });
		const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
		await server.connect(serverTransport);
		await client.connect(clientTransport);
		try {
			const listResult = await client.listTools();
			const callResult = await client.callTool({
				name: toolDefinition.name,
				arguments: { toolkit: "github" },
			});

			expect(listResult.tools).toHaveLength(1);
			expect(listResult.tools[0]?.outputSchema).toEqual(toolDefinition.outputSchema);
			expect(listResult.tools[0]?._meta).toEqual(toolDefinition._meta);
			expect(callResult.structuredContent).toEqual(toolResult.structuredContent);
			expect(callResult.redirect_url).toBe(toolResult.redirect_url);
			expect(callResult._meta).toEqual(toolResult._meta);
			expect(callResult.futureResultField).toEqual(toolResult.futureResultField);
		} finally {
			await client.close();
			await server.close();
		}
	});

	it("fails closed when the aggregate returns a non-object result", async () => {
		const handlers = createTransparentMcpHandlers(async () => ["not", "an", "mcp", "result"]);

		await expect(handlers.listTools()).rejects.toThrow(
			"Clawdi MCP tools/list returned an invalid result",
		);
	});
});

/**
 * The Streamable HTTP session leg (plan 05-10, D-01 relaxed).
 *
 * `server.ts` was frozen by D-01 and stayed protocol-transparent, which meant no client this
 * repository ships ever echoed a session id: `callClawdiMcp` sent only `Authorization` and
 * `Content-Type` and read no response header, and the SDK `Server` terminates `initialize`
 * locally. So `companion_tool_calls.session_id` was NULL on the canonical agent path
 * (`setup.ts` registers `iforge mcp` as THE agent integration) and TOOL-07's per-session clause
 * was unreachable in production. The user relaxed D-01 during gap closure for session-id
 * handling and nothing else.
 *
 * **The degradation cases carry as much weight as the echo case.** A proxy that refused to
 * forward a tool call because it could not obtain a session header would trade an attribution
 * gap for an outage of the canonical agent integration -- the tenant would lose the call, not
 * just its attribution. Tests 4, 6, 7 and 8 below are what keep that from being possible.
 */
describe("MCP stdio proxy session identity", () => {
	const HANDSHAKE_RESULT = JSON.stringify({
		jsonrpc: "2.0",
		id: 1,
		result: { protocolVersion: "2025-06-18" },
	});
	const FORWARD_RESULT = JSON.stringify({ jsonrpc: "2.0", id: 1, result: { tools: [] } });
	const MINTED = "sessionAAAAAAAAAAAAAAAA";
	const SECOND_MINTED = "sessionBBBBBBBBBBBBBBBB";

	interface SeenRequest {
		rpcMethod: string;
		sessionHeader: string | null;
	}

	type StubHandler = (request: SeenRequest, index: number) => Response | Promise<Response>;

	function handshakeResponse(sessionId?: string): Response {
		const headers: Record<string, string> = { "Content-Type": "application/json" };
		if (sessionId !== undefined) headers["Mcp-Session-Id"] = sessionId;
		return new Response(HANDSHAKE_RESULT, { status: 200, headers });
	}

	function forwardResponse(status = 200): Response {
		return new Response(FORWARD_RESULT, {
			status,
			headers: { "Content-Type": "application/json" },
		});
	}

	let seen: SeenRequest[] = [];
	let restore: () => void = () => {};

	function stubUpstream(handler: StubHandler): void {
		const originalFetch = globalThis.fetch;
		const previousToken = process.env.CLAWDI_AUTH_TOKEN;
		const previousOrigin = process.env.CLAWDI_AUTH_TOKEN_ORIGIN;
		const previousApiUrl = process.env.CLAWDI_API_URL;
		process.env.CLAWDI_AUTH_TOKEN = "test-mcp-token";
		process.env.CLAWDI_AUTH_TOKEN_ORIGIN = "http://localhost:8000";
		process.env.CLAWDI_API_URL = "http://localhost:8000";
		globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
			const headers = new Headers(init?.headers);
			const body = JSON.parse(String(init?.body ?? "{}")) as { method?: string };
			const request: SeenRequest = {
				rpcMethod: body.method ?? "",
				sessionHeader: headers.get("Mcp-Session-Id"),
			};
			seen.push(request);
			return handler(request, seen.length - 1);
		}) as typeof fetch;
		restore = () => {
			globalThis.fetch = originalFetch;
			if (previousToken === undefined) delete process.env.CLAWDI_AUTH_TOKEN;
			else process.env.CLAWDI_AUTH_TOKEN = previousToken;
			if (previousOrigin === undefined) delete process.env.CLAWDI_AUTH_TOKEN_ORIGIN;
			else process.env.CLAWDI_AUTH_TOKEN_ORIGIN = previousOrigin;
			if (previousApiUrl === undefined) delete process.env.CLAWDI_API_URL;
			else process.env.CLAWDI_API_URL = previousApiUrl;
		};
	}

	beforeEach(() => {
		restore();
		restore = () => {};
		seen = [];
		// Every case starts as a freshly launched proxy PROCESS. The identity property under test
		// is process-scoped -- one process, one id; two processes, two ids -- so without this the
		// first case's handshake would satisfy every later one and the suite would assert nothing.
		resetClawdiMcpSessionForTesting();
	});

	it("runs one initialize and echoes the minted id on the forwarded request", async () => {
		// THE ECHO ASSERTION. If session handling regresses, this is the case that says so.
		stubUpstream((request) =>
			request.rpcMethod === "initialize" ? handshakeResponse(MINTED) : forwardResponse(),
		);
		try {
			await callClawdiMcp("tools/list", {});

			expect(seen).toHaveLength(2);
			expect(seen[0]?.rpcMethod).toBe("initialize");
			expect(seen[0]?.sessionHeader).toBeNull();
			expect(seen[1]?.rpcMethod).toBe("tools/list");
			expect(seen[1]?.sessionHeader).toBe(MINTED);
		} finally {
			restore();
		}
	});

	it("runs exactly one initialize per process and reuses the id across calls", async () => {
		// One process holding two ids is precisely the cross-attribution this plan exists to
		// close, read from the client side.
		stubUpstream((request) =>
			request.rpcMethod === "initialize" ? handshakeResponse(MINTED) : forwardResponse(),
		);
		try {
			await callClawdiMcp("tools/list", {});
			await callClawdiMcp("tools/call", { name: "x", arguments: {} });
			await callClawdiMcp("tools/list", {});

			expect(seen.filter((request) => request.rpcMethod === "initialize")).toHaveLength(1);
			const forwarded = seen.filter((request) => request.rpcMethod !== "initialize");
			expect(forwarded).toHaveLength(3);
			expect(forwarded.every((request) => request.sessionHeader === MINTED)).toBe(true);
		} finally {
			restore();
		}
	});

	it("gives a second proxy process its own handshake and its own id", async () => {
		let handshakes = 0;
		stubUpstream((request) => {
			if (request.rpcMethod !== "initialize") return forwardResponse();
			handshakes += 1;
			return handshakeResponse(handshakes === 1 ? MINTED : SECOND_MINTED);
		});
		try {
			await callClawdiMcp("tools/list", {});
			resetClawdiMcpSessionForTesting();
			await callClawdiMcp("tools/list", {});

			expect(handshakes).toBe(2);
			const forwarded = seen.filter((request) => request.rpcMethod !== "initialize");
			expect(forwarded[0]?.sessionHeader).toBe(MINTED);
			expect(forwarded[1]?.sessionHeader).toBe(SECOND_MINTED);
		} finally {
			restore();
		}
	});

	it("forwards normally when the server assigns no session at all", async () => {
		// A server that assigns nothing is conformant: the spec says MAY. This must behave
		// exactly as the proxy did before session handling existed.
		stubUpstream((request) =>
			request.rpcMethod === "initialize" ? handshakeResponse() : forwardResponse(),
		);
		try {
			const result = await callClawdiMcp("tools/list", {});

			expect(result).toEqual({ tools: [] });
			const forwarded = seen.filter((request) => request.rpcMethod !== "initialize");
			expect(forwarded).toHaveLength(1);
			expect(forwarded[0]?.sessionHeader).toBeNull();
		} finally {
			restore();
		}
	});

	it.each([
		["too short", "abcd"],
		["too long", "z".repeat(200)],
		["outside the alphabet", "not a session id"],
	])("treats a %s session value as absent and never sends it back", async (_label, value) => {
		// The client bound and the server's `MCP_SESSION_ID_PATTERN` are a MATCHED PAIR: an id the
		// server would reject is an id this client must not hold. Holding one anyway would mean
		// sending a header the server discards on every request for the life of the process.
		stubUpstream((request) =>
			request.rpcMethod === "initialize" ? handshakeResponse(value) : forwardResponse(),
		);
		try {
			const result = await callClawdiMcp("tools/list", {});

			expect(result).toEqual({ tools: [] });
			const forwarded = seen.filter((request) => request.rpcMethod !== "initialize");
			expect(forwarded).toHaveLength(1);
			expect(forwarded[0]?.sessionHeader).toBeNull();
		} finally {
			restore();
		}
	});

	it("still forwards when the handshake rejects with a network error", async () => {
		stubUpstream((request) => {
			if (request.rpcMethod === "initialize") throw new Error("ECONNREFUSED");
			return forwardResponse();
		});
		try {
			const result = await callClawdiMcp("tools/list", {});

			expect(result).toEqual({ tools: [] });
			const forwarded = seen.filter((request) => request.rpcMethod !== "initialize");
			expect(forwarded[0]?.sessionHeader).toBeNull();
		} finally {
			restore();
		}
	});

	it("still forwards when the handshake answers HTTP 500", async () => {
		stubUpstream((request) =>
			request.rpcMethod === "initialize"
				? new Response("nope", { status: 500 })
				: forwardResponse(),
		);
		try {
			const result = await callClawdiMcp("tools/list", {});

			expect(result).toEqual({ tools: [] });
			const forwarded = seen.filter((request) => request.rpcMethod !== "initialize");
			expect(forwarded[0]?.sessionHeader).toBeNull();
		} finally {
			restore();
		}
	});

	it("does not re-run a failed handshake in front of every later call", async () => {
		// A per-call retry would put an extra round trip and an extra failure in front of every
		// tool call for the life of the process, which is a performance bug wearing a correctness
		// bug's clothes.
		stubUpstream((request) => {
			if (request.rpcMethod === "initialize") throw new Error("ECONNREFUSED");
			return forwardResponse();
		});
		try {
			await callClawdiMcp("tools/list", {});
			await callClawdiMcp("tools/list", {});
			await callClawdiMcp("tools/list", {});
			await callClawdiMcp("tools/list", {});

			expect(seen.filter((request) => request.rpcMethod === "initialize")).toHaveLength(1);
			expect(seen.filter((request) => request.rpcMethod !== "initialize")).toHaveLength(4);
		} finally {
			restore();
		}
	});

	it("re-initializes once and retries when a session-bearing request is answered 404", async () => {
		// The spec's staleness rule: the server assigns at `initialize`, the client echoes
		// thereafter, and a 404 on a session id means that session is gone -- re-initialize.
		let handshakes = 0;
		let forwards = 0;
		stubUpstream((request) => {
			if (request.rpcMethod === "initialize") {
				handshakes += 1;
				return handshakeResponse(handshakes === 1 ? MINTED : SECOND_MINTED);
			}
			forwards += 1;
			return forwards === 1 ? forwardResponse(404) : forwardResponse();
		});
		try {
			const result = await callClawdiMcp("tools/list", {});

			expect(result).toEqual({ tools: [] });
			expect(handshakes).toBe(2);
			const forwarded = seen.filter((request) => request.rpcMethod !== "initialize");
			expect(forwarded).toHaveLength(2);
			expect(forwarded[0]?.sessionHeader).toBe(MINTED);
			expect(forwarded[1]?.sessionHeader).toBe(SECOND_MINTED);
		} finally {
			restore();
		}
	});

	it("surfaces the HTTP error rather than looping when every request answers 404", async () => {
		// Bounded at one retry. A server answering 404 to everything must not be able to spin the
		// proxy, and the caller's error surface does not change.
		let handshakes = 0;
		stubUpstream((request) => {
			if (request.rpcMethod === "initialize") {
				handshakes += 1;
				return handshakeResponse(handshakes === 1 ? MINTED : SECOND_MINTED);
			}
			return forwardResponse(404);
		});
		try {
			await expect(callClawdiMcp("tools/list", {})).rejects.toThrow(
				"Clawdi MCP request failed (HTTP 404)",
			);
			expect(handshakes).toBeLessThanOrEqual(2);
			expect(seen.filter((request) => request.rpcMethod !== "initialize")).toHaveLength(2);
		} finally {
			restore();
		}
	});

	it("does not re-initialize when a request that carried no session is answered 404", async () => {
		// The staleness rule is about a session the server no longer knows. With no session sent,
		// a 404 says something else entirely, and re-handshaking on it would turn an ordinary
		// routing error into an extra round trip on every one of them.
		let handshakes = 0;
		stubUpstream((request) => {
			if (request.rpcMethod === "initialize") {
				handshakes += 1;
				return handshakeResponse();
			}
			return forwardResponse(404);
		});
		try {
			await expect(callClawdiMcp("tools/list", {})).rejects.toThrow(
				"Clawdi MCP request failed (HTTP 404)",
			);
			expect(handshakes).toBe(1);
			expect(seen.filter((request) => request.rpcMethod !== "initialize")).toHaveLength(1);
		} finally {
			restore();
		}
	});
});
