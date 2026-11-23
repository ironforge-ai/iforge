import { afterEach, describe, expect, test } from "bun:test";
import { publicNameFor } from "../lib/brand-env";
import type { RuntimeApplyContext } from "./apply-identity";
import { assertHostedRuntimeContract } from "./hosted-runtime-contract";
import { getRuntimePaths } from "./paths";

const originalEnv = { ...process.env };
const applyContext: RuntimeApplyContext = {
	kind: "context-file",
	backend: "incus",
	identity: {
		generation: 1,
		manifestETag: '"test-manifest"',
		applyReceiptId: "test-apply-receipt",
		bootNonce: "test-boot-nonce",
	},
	manifestSource: {
		type: "http",
		url: "https://runtime.test/v1/runtime/manifest",
		auth: { type: "bearer", token: "test-token" },
	},
};

afterEach(() => {
	process.env = { ...originalEnv };
});

describe("hosted runtime convergence contract", () => {
	test("rejects a hosted convergence whose resolved HOME is not the tenant home", () => {
		process.env.CLAWDI_RUNTIME_MODE = "hosted";
		process.env.CLAWDI_RUNTIME_HOME = "/root";
		process.env.CLAWDI_RUNTIME_USER = "iforge";
		process.env.CLAWDI_RUNTIME_UID = "10001";
		process.env.CLAWDI_RUNTIME_GID = "10001";

		expect(() =>
			assertHostedRuntimeContract(getRuntimePaths(), applyContext, {
				platformRoots: "deferred",
			}),
		).toThrow("hosted runtime HOME must resolve to /home/iforge; resolved /root");
	});

	test("rejects a hosted CLI state root inside the tenant home", () => {
		process.env.CLAWDI_RUNTIME_MODE = "hosted";
		process.env.CLAWDI_RUNTIME_HOME = "/home/iforge";
		process.env.CLAWDI_RUNTIME_USER = "iforge";
		process.env.CLAWDI_HOME = "/home/iforge/.iforge";

		expect(() =>
			assertHostedRuntimeContract(getRuntimePaths(), applyContext, {
				platformRoots: "deferred",
				resolveUserIdentity: () => ({ uid: 10_001, gid: 10_001 }),
			}),
		).toThrow("hosted CLAWDI_HOME must be outside the tenant home");
	});

	test("rejects a hosted CLI state root outside the service-state directory family", () => {
		process.env.CLAWDI_RUNTIME_MODE = "hosted";
		process.env.CLAWDI_RUNTIME_HOME = "/home/iforge";
		process.env.CLAWDI_RUNTIME_USER = "iforge";
		process.env.CLAWDI_HOME = "/var/cache/iforge-user";

		expect(() =>
			assertHostedRuntimeContract(getRuntimePaths(), applyContext, {
				platformRoots: "deferred",
				resolveUserIdentity: () => ({ uid: 10_001, gid: 10_001 }),
			}),
		).toThrow("hosted CLAWDI_HOME must be an absolute sibling of /var/lib/iforge");
	});

	test("rejects a hosted convergence whose runtime user is wrong", () => {
		process.env.CLAWDI_RUNTIME_MODE = "hosted";
		process.env.CLAWDI_RUNTIME_HOME = "/home/iforge";
		process.env.CLAWDI_RUNTIME_USER = "root";

		expect(() =>
			assertHostedRuntimeContract(getRuntimePaths(), applyContext, {
				platformRoots: "deferred",
			}),
		).toThrow("hosted runtime user must be iforge; resolved root");
	});

	test("rejects a hosted runtime account that resolves to the wrong identity", () => {
		process.env.CLAWDI_RUNTIME_MODE = "hosted";
		process.env.CLAWDI_RUNTIME_HOME = "/home/iforge";
		process.env.CLAWDI_RUNTIME_USER = "iforge";

		expect(() =>
			assertHostedRuntimeContract(getRuntimePaths(), applyContext, {
				platformRoots: "deferred",
				resolveUserIdentity: () => ({ uid: 0, gid: 0 }),
			}),
		).toThrow("hosted runtime user iforge resolved to root identity 0:0");
	});

	test("names a mismatched runtime uid by its public spelling", () => {
		process.env.CLAWDI_RUNTIME_MODE = "hosted";
		process.env.CLAWDI_RUNTIME_HOME = "/home/iforge";
		process.env.CLAWDI_RUNTIME_USER = "iforge";
		process.env.CLAWDI_RUNTIME_UID = "10002";

		let message = "";
		try {
			assertHostedRuntimeContract(getRuntimePaths(), applyContext, {
				platformRoots: "deferred",
				resolveUserIdentity: () => ({ uid: 10_001, gid: 10_001 }),
			});
		} catch (error) {
			message = error instanceof Error ? error.message : String(error);
		}
		expect(message).toBe(
			`${publicNameFor("CLAWDI_RUNTIME_UID")} must match the resolved hosted runtime identity 10001; resolved 10002`,
		);
		expect(message).not.toContain("CLAWDI_");
	});

	// The register name is DERIVED from the alias map, never restated here. The thrown message
	// prints the PUBLIC spelling while the module still READS the internal one, so writing the
	// name out in this file would be a second place to forget when the contract moves. It also
	// fails loudly rather than asserting nothing: were the map to lose the entry,
	// `publicNameFor` returns undefined and this expectation becomes "undefined=hosted".
	test("rejects convergence when hosted mode was not selected explicitly", () => {
		process.env.CLAWDI_RUNTIME_MODE = "local";
		process.env.CLAWDI_RUNTIME_HOME = "/home/clawdi";
		process.env.CLAWDI_RUNTIME_USER = "clawdi";

		expect(() =>
			assertHostedRuntimeContract(getRuntimePaths(), applyContext, {
				platformRoots: "deferred",
			}),
		).toThrow(
			`hosted convergence requires ${publicNameFor("CLAWDI_RUNTIME_MODE")}=hosted explicitly`,
		);
	});
});
