import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
	CallToolRequestSchema,
	ListToolsRequestSchema,
	type ServerResult,
} from "@modelcontextprotocol/sdk/types.js";
import { getClawdiAccessToken } from "../lib/clerk-oauth";
import { getConfig, isLoggedIn } from "../lib/config";
import { FetchTimeoutError, timedFetch } from "../lib/timed-fetch";

interface JsonRpcResponse {
	result?: unknown;
	error?: unknown;
}

export type ClawdiMcpCaller = (
	method: string,
	params?: Record<string, unknown>,
) => Promise<unknown>;

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireMcpResult(method: string, result: unknown): Record<string, unknown> {
	if (isRecord(result)) return result;
	throw new Error(`Clawdi MCP ${method} returned an invalid result`);
}

function requireMcpLogin(): void {
	if (!isLoggedIn()) {
		throw new Error("Not logged in. Run `iforge auth login` first.");
	}
}

function ensureMcpLogin(): void {
	try {
		requireMcpLogin();
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		process.stderr.write(`${message}\n`);
		process.exit(1);
	}
}

const MCP_ENDPOINT_PATH = "/v1/mcp/clawdi";
const MCP_DISCOVERY_TIMEOUT_MS = 30_000;
// Backend MCP execution has a 360s deadline (300s read plus setup overhead).
const MCP_TOOL_CALL_TIMEOUT_MS = 390_000;

/**
 * The MCP Streamable HTTP session header, in the spec's spelling. `Headers.get` is
 * case-insensitive, so this matches the lower-cased name `read_mcp_session_id` reads on the
 * backend — they are the same header, written twice in the two conventions of their languages.
 */
const MCP_SESSION_ID_HEADER = "Mcp-Session-Id";

/**
 * A MATCHED PAIR with `MCP_SESSION_ID_PATTERN` in `backend/app/routes/mcp_bridge.py`. An id the
 * server would reject is an id this client must not hold: holding one anyway would mean sending
 * a header the server discards on every request for the life of the process, which is worse than
 * sending none, because it looks like attribution is working. If the server's bound moves, both
 * move.
 */
const MCP_SESSION_ID_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;

/** Mirrors `MCP_PROTOCOL_VERSION` in `backend/app/routes/mcp_bridge.py`. */
const MCP_CLIENT_PROTOCOL_VERSION = "2025-06-18";

/**
 * Deliberately shorter than `MCP_FORWARD_TIMEOUT_MS`, so a dead or slow server adds a BOUNDED
 * delay to the first forwarded call rather than doubling its budget. It is additionally clamped
 * to the caller's own timeout below: a caller that asked for a 10 ms deadline must not wait ten
 * seconds on a handshake it never asked for.
 */
const MCP_SESSION_HANDSHAKE_TIMEOUT_MS = 10_000;

// Per proxy PROCESS. `mcpSessionHandshake` exists because the SDK `Server` dispatches overlapping
// requests over one stdio transport, so two concurrently forwarded calls could otherwise each run
// an `initialize` and mint two ids for one process — which is exactly the cross-attribution this
// work exists to close, arriving from the client side. `mcpSessionHandshakeAttempted` remembers a
// FAILED handshake so it is not re-run in front of every later tool call.
let mcpSessionId: string | null = null;
let mcpSessionHandshake: Promise<string | null> | null = null;
let mcpSessionHandshakeAttempted = false;

async function postClawdiMcp(
	apiUrl: string,
	accessToken: string,
	method: string,
	params: Record<string, unknown> | undefined,
	timeoutMs: number,
	sessionId: string | null,
): Promise<Response> {
	const headers: Record<string, string> = {
		Authorization: `Bearer ${accessToken}`,
		"Content-Type": "application/json",
	};
	if (sessionId !== null) headers[MCP_SESSION_ID_HEADER] = sessionId;
	return timedFetch(
		`${apiUrl}${MCP_ENDPOINT_PATH}`,
		{
			method: "POST",
			headers,
			body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params: params ?? {} }),
		},
		timeoutMs,
	);
}

/**
 * This proxy process's Streamable HTTP session id, or `null` when it has none.
 *
 * **This function MUST NOT throw, for any reason.** A non-ok response, an absent header, a
 * malformed value, a rejected fetch and a `FetchTimeoutError` all resolve to `null`. A proxy that
 * refused to forward a tool call because a session header could not be obtained would be strictly
 * worse than the NULL `session_id` the header replaces: the tenant would lose the call, not just
 * its attribution. Absent identity degrades to exactly the behaviour this module had before
 * session handling existed, and nothing more.
 *
 * No `notifications/initialized` follows the handshake, and that omission is decided rather than
 * forgotten. `MCP_ENDPOINT_PATH` is a matched client/server wire path — this client only ever
 * calls this repository's own backend — and that backend's `initialize` branch is stateless while
 * its `tools/call` branch is not gated on a completed lifecycle, so the notification would be a
 * round trip with no receiver behaviour behind it.
 */
async function resolveClawdiMcpSessionId(
	apiUrl: string,
	accessToken: string,
	timeoutMs: number,
): Promise<string | null> {
	if (mcpSessionId !== null) return mcpSessionId;
	if (mcpSessionHandshakeAttempted) return null;
	if (mcpSessionHandshake !== null) return mcpSessionHandshake;

	const budget = Math.min(MCP_SESSION_HANDSHAKE_TIMEOUT_MS, timeoutMs);
	mcpSessionHandshake = (async () => {
		try {
			const response = await postClawdiMcp(
				apiUrl,
				accessToken,
				"initialize",
				{
					protocolVersion: MCP_CLIENT_PROTOCOL_VERSION,
					capabilities: {},
					// Mirrors the registration `createTransparentMcpServer` passes to `Server`.
					clientInfo: { name: "iforge", version: "0.0.1" },
				},
				budget,
				null,
			);
			if (!response.ok) return null;
			const assigned = response.headers.get(MCP_SESSION_ID_HEADER);
			if (assigned === null || !MCP_SESSION_ID_PATTERN.test(assigned)) return null;
			return assigned;
		} catch {
			return null;
		} finally {
			mcpSessionHandshakeAttempted = true;
		}
	})();

	try {
		mcpSessionId = await mcpSessionHandshake;
		return mcpSessionId;
	} finally {
		mcpSessionHandshake = null;
	}
}

/**
 * Forget this process's session, so the next call runs a fresh handshake. Exported solely so a
 * test can simulate a second proxy PROCESS: the identity property under test — one process, one
 * id; two processes, two ids — is process-scoped, and without this seam it is only assertable by
 * arguing about module lifetimes rather than by running something.
 */
export function resetClawdiMcpSessionForTesting(): void {
	mcpSessionId = null;
	mcpSessionHandshake = null;
	mcpSessionHandshakeAttempted = false;
}

export async function callClawdiMcp(
	method: string,
	params?: Record<string, unknown>,
	timeoutMs = method === "tools/call" ? MCP_TOOL_CALL_TIMEOUT_MS : MCP_DISCOVERY_TIMEOUT_MS,
): Promise<unknown> {
	const config = getConfig();
	const accessToken = await getClawdiAccessToken(config.apiUrl);
	const sessionId = await resolveClawdiMcpSessionId(config.apiUrl, accessToken, timeoutMs);
	// Upstream #1432: a timed-out tool call may still have run, so it is never reported
	// as a plain transport failure an agent would blindly retry.
	const rethrowToolCallTimeout = (error: unknown): never => {
		if (method === "tools/call" && error instanceof FetchTimeoutError) {
			throw new Error(
				"Tool call timed out; execution may still have completed. Check the outcome before retrying; do not automatically retry side-effecting calls.",
			);
		}
		throw error;
	};
	let response = await postClawdiMcp(
		config.apiUrl,
		accessToken,
		method,
		params,
		timeoutMs,
		sessionId,
	).catch(rethrowToolCallTimeout);
	// The spec's staleness rule: the server assigns at `initialize`, the client echoes thereafter,
	// and a `404` on a request that CARRIED a session id means that session is gone. Re-initialize
	// once and retry that one request once. Bounded at a single attempt, so a server answering 404
	// to everything cannot spin the proxy; a 404 on a request that carried no session says
	// something else entirely and is left alone.
	if (response.status === 404 && sessionId !== null) {
		resetClawdiMcpSessionForTesting();
		const renewed = await resolveClawdiMcpSessionId(config.apiUrl, accessToken, timeoutMs);
		response = await postClawdiMcp(
			config.apiUrl,
			accessToken,
			method,
			params,
			timeoutMs,
			renewed,
		).catch(rethrowToolCallTimeout);
	}
	if (!response.ok) {
		throw new Error(`Clawdi MCP request failed (HTTP ${response.status})`);
	}
	const body = (await response.json()) as JsonRpcResponse;
	if (body.error) {
		throw new Error(typeof body.error === "string" ? body.error : JSON.stringify(body.error));
	}
	return body.result;
}

/**
 * Build a protocol-transparent tools proxy. The stdio adapter deliberately
 * does not reconstruct tool definitions or results through a local schema:
 * /v1/mcp/clawdi remains the runtime authority for native and dynamic tools.
 * The adapter now also carries the transport's session IDENTITY, which is not
 * tool knowledge, so that authority is unchanged by it.
 */
export function createTransparentMcpHandlers(callMcp: ClawdiMcpCaller) {
	return {
		listTools: async (params: Record<string, unknown> = {}) =>
			requireMcpResult("tools/list", await callMcp("tools/list", params)),
		callTool: async (params: Record<string, unknown>) =>
			requireMcpResult("tools/call", await callMcp("tools/call", params)),
	};
}

export function createTransparentMcpServer(callMcp: ClawdiMcpCaller): Server {
	const server = new Server(
		{
			name: "iforge",
			version: "0.0.1",
		},
		{ capabilities: { tools: {} } },
	);
	const handlers = createTransparentMcpHandlers(callMcp);

	server.setRequestHandler(ListToolsRequestSchema, async (request) => {
		const params = isRecord(request.params) ? request.params : {};
		return (await handlers.listTools(params)) as ServerResult;
	});
	server.setRequestHandler(CallToolRequestSchema, async (request) => {
		try {
			return (await handlers.callTool(request.params)) as ServerResult;
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			return { content: [{ type: "text", text: `Error: ${message}` }], isError: true };
		}
	});

	return server;
}

export async function createClawdiMcpServer(): Promise<Server> {
	requireMcpLogin();
	return createTransparentMcpServer(callClawdiMcp);
}

export async function startMcpServer() {
	ensureMcpLogin();
	const server = await createClawdiMcpServer();
	const transport = new StdioServerTransport();
	await server.connect(transport);
}
