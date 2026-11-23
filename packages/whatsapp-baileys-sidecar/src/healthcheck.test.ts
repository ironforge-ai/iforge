import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { checkSidecarHealth } from "./healthcheck.js";

const TOKEN = "test-sidecar-token";
const SESSION_ID = "11111111-1111-4111-8111-111111111111";
const servers: Server[] = [];
const temporaryDirectories: string[] = [];

afterEach(async () => {
	await Promise.all(
		servers
			.splice(0)
			.map((server) => new Promise<void>((resolve) => server.close(() => resolve()))),
	);
	for (const directory of temporaryDirectories.splice(0)) {
		rmSync(directory, { force: true, recursive: true });
	}
});

describe("sidecar healthcheck", () => {
	it("authenticates and verifies the service over one Unix socket", async () => {
		const socketPath = await startUnixHealthServer();

		await expect(
			checkSidecarHealth({
				IFORGE_WA_SIDECAR_SOCKET_PATH: socketPath,
				CHANNEL_WHATSAPP_BAILEYS_SIDECAR_TOKEN: TOKEN,
			}),
		).resolves.toBeUndefined();
	});

	// Regression for the fourth-entrypoint defect: `dist/healthcheck.js` is its own process and
	// inherits nothing from the server's `loadConfigFromEnv`. `config/deploy.yml` populates the
	// container with IFORGE_* only, so a healthcheck that does not apply the boundary resolves no
	// socket path and falls back to TCP 127.0.0.1:8787 where the socket-bound server never listens.
	// These assert the *aliasing behaviour*, not the current internal spelling: they would still
	// fail if the boundary were removed and the read were merely renamed.
	it("resolves the endpoint from the public IFORGE_* spelling deploy.yml actually sets", async () => {
		const socketPath = await startUnixHealthServer();

		await expect(
			checkSidecarHealth({
				IFORGE_WA_SIDECAR_SOCKET_PATH: socketPath,
				CHANNEL_WHATSAPP_BAILEYS_SIDECAR_TOKEN: TOKEN,
			}),
		).resolves.toBeUndefined();
	});

	it("applies the boundary in place, KEEPING the public name beside the internal register", async () => {
		const socketPath = await startUnixHealthServer();
		const env: NodeJS.ProcessEnv = {
			IFORGE_WA_SIDECAR_SOCKET_PATH: socketPath,
			CHANNEL_WHATSAPP_BAILEYS_SIDECAR_TOKEN: TOKEN,
		};

		await expect(checkSidecarHealth(env)).resolves.toBeUndefined();

		// The alias COPIED the value rather than the read moving to the public name.
		expect(env.CLAWDI_WA_SIDECAR_SOCKET_PATH).toBe(socketPath);
		// INVERTED deliberately by plan 03.1-16, mirroring the CLI inversion in plan 03.1-09.
		// The public key used to be consumed. Keeping it is what lets
		// `refuseUserSuppliedInternalNames` run unconditionally at this entrypoint: a child that
		// inherits this environment re-derives its own register from the public spelling instead
		// of inheriting an internal name it could not tell apart from an operator-supplied one.
		expect(env.IFORGE_WA_SIDECAR_SOCKET_PATH).toBe(socketPath);
	});

	it("lets the public socket path win over an internal one already in the environment", async () => {
		const socketPath = await startUnixHealthServer();

		// A stale internal value must not shadow the spelling the deploy contract sets. If the
		// boundary regressed to "do not clobber an internal value", this would connect nowhere.
		await expect(
			checkSidecarHealth({
				IFORGE_WA_SIDECAR_SOCKET_PATH: socketPath,
				CLAWDI_WA_SIDECAR_SOCKET_PATH: "/run/stale/sidecar.sock",
				CHANNEL_WHATSAPP_BAILEYS_SIDECAR_TOKEN: TOKEN,
			}),
		).resolves.toBeUndefined();
	});

	it("rejects a public TCP spelling combined with a public socket path", async () => {
		const socketPath = await startUnixHealthServer();

		// The mixed-endpoint guard must see aliased values too, otherwise a deploy that sets both
		// public spellings silently picks one instead of failing loudly.
		await expect(
			checkSidecarHealth({
				IFORGE_WA_SIDECAR_SOCKET_PATH: socketPath,
				CLAWDI_WA_SIDECAR_HOST: "127.0.0.1",
				CHANNEL_WHATSAPP_BAILEYS_SIDECAR_TOKEN: TOKEN,
			}),
		).rejects.toThrow("cannot be combined");
	});

	it("preserves authenticated exact-loopback TCP health", async () => {
		const port = await startTcpHealthServer();

		await expect(
			checkSidecarHealth({
				CLAWDI_WA_SIDECAR_HOST: "127.0.0.1",
				CLAWDI_WA_SIDECAR_PORT: String(port),
				CHANNEL_WHATSAPP_BAILEYS_SIDECAR_TOKEN: TOKEN,
			}),
		).resolves.toBeUndefined();
	});

	it("requires every requested durable session to recover", async () => {
		const recoveredPort = await startTcpHealthServer(true, true);
		const env = {
			CLAWDI_WA_SIDECAR_HOST: "127.0.0.1",
			CLAWDI_WA_SIDECAR_PORT: String(recoveredPort),
			CHANNEL_WHATSAPP_BAILEYS_SIDECAR_TOKEN: TOKEN,
		};

		await expect(checkSidecarHealth(env, [SESSION_ID])).resolves.toBeUndefined();

		const disconnectedPort = await startTcpHealthServer(true, false);
		await expect(
			checkSidecarHealth({ ...env, CLAWDI_WA_SIDECAR_PORT: String(disconnectedPort) }, [
				SESSION_ID,
			]),
		).rejects.toThrow("session did not recover");
		await expect(checkSidecarHealth(env, ["not-a-session"])).rejects.toThrow(
			"invalid sidecar session id",
		);
	});

	it("rejects mixed UDS/TCP endpoints and non-loopback TCP", async () => {
		const socketPath = await startUnixHealthServer();
		const base = {
			IFORGE_WA_SIDECAR_SOCKET_PATH: socketPath,
			CHANNEL_WHATSAPP_BAILEYS_SIDECAR_TOKEN: TOKEN,
		};

		await expect(
			checkSidecarHealth({ ...base, CLAWDI_WA_SIDECAR_HOST: "127.0.0.1" }),
		).rejects.toThrow("cannot be combined");
		await expect(checkSidecarHealth({ ...base, CLAWDI_WA_SIDECAR_PORT: "8787" })).rejects.toThrow(
			"cannot be combined",
		);
		await expect(
			checkSidecarHealth({
				CLAWDI_WA_SIDECAR_HOST: "0.0.0.0",
				CHANNEL_WHATSAPP_BAILEYS_SIDECAR_TOKEN: TOKEN,
			}),
		).rejects.toThrow("exact loopback host");
	});

	it("rejects malformed TCP ports and missing credentials", async () => {
		for (const port of ["0", "65536", "8787x", "1.5", "-1"]) {
			await expect(
				checkSidecarHealth({
					CLAWDI_WA_SIDECAR_PORT: port,
					CHANNEL_WHATSAPP_BAILEYS_SIDECAR_TOKEN: TOKEN,
				}),
			).rejects.toThrow("valid port");
		}
		await expect(checkSidecarHealth({})).rejects.toThrow("missing sidecar token");
	});

	it("fails on a wrong bearer or returned service identity", async () => {
		const wrongBearerPort = await startTcpHealthServer();
		await expect(
			checkSidecarHealth({
				CLAWDI_WA_SIDECAR_PORT: String(wrongBearerPort),
				CHANNEL_WHATSAPP_BAILEYS_SIDECAR_TOKEN: "wrong-token",
			}),
		).rejects.toThrow("request failed");

		const wrongIdentityPort = await startTcpHealthServer(false);
		await expect(
			checkSidecarHealth({
				CLAWDI_WA_SIDECAR_PORT: String(wrongIdentityPort),
				CHANNEL_WHATSAPP_BAILEYS_SIDECAR_TOKEN: TOKEN,
			}),
		).rejects.toThrow("identity mismatch");
	});
});

async function startUnixHealthServer(): Promise<string> {
	const root = mkdtempSync(join(tmpdir(), "clawdi-healthcheck-"));
	temporaryDirectories.push(root);
	const socketDirectory = join(root, "run");
	mkdirSync(socketDirectory);
	const socketPath = join(socketDirectory, "sidecar.sock");
	const server = createHealthServer();
	await new Promise<void>((resolve, reject) => {
		server.once("error", reject);
		server.listen(socketPath, resolve);
	});
	return socketPath;
}

async function startTcpHealthServer(
	validIdentity = true,
	sessionConnected?: boolean,
): Promise<number> {
	const server = createHealthServer(validIdentity, sessionConnected);
	await new Promise<void>((resolve, reject) => {
		server.once("error", reject);
		server.listen(0, "127.0.0.1", resolve);
	});
	return (server.address() as AddressInfo).port;
}

function createHealthServer(validIdentity = true, sessionConnected?: boolean): Server {
	const server = createServer((request, response) => {
		response.setHeader("content-type", "application/json");
		if (request.headers.authorization !== `Bearer ${TOKEN}`) {
			response.statusCode = 401;
			response.end(JSON.stringify({ error: "unauthorized" }));
			return;
		}
		if (request.url === `/v1/sessions/${SESSION_ID}/health` && sessionConnected !== undefined) {
			response.end(
				JSON.stringify({
					sessionId: SESSION_ID,
					status: sessionConnected ? "connected" : "disconnected",
					connected: sessionConnected,
					registered: true,
				}),
			);
			return;
		}
		if (request.url !== "/v1/health") {
			response.statusCode = 404;
			response.end(JSON.stringify({ error: "not_found" }));
			return;
		}
		response.end(
			JSON.stringify(
				validIdentity
					? { schemaVersion: "clawdi.whatsapp.sidecar-health.v1", ready: true }
					: { schemaVersion: "unexpected", ready: true },
			),
		);
	});
	servers.push(server);
	return server;
}
