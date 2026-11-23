import { afterEach, describe, expect, test } from "bun:test";
import { join } from "node:path";
import { stateRoot } from "../lib/state-root";
import { DEFAULT_RUNTIME_USER_CLI_STATE_ROOT, getRuntimePaths } from "./paths";

const originalEnv = { ...process.env };

afterEach(() => {
	process.env = { ...originalEnv };
});

describe("runtime paths", () => {
	test("uses the systemd and FHS platform roots for Hosted runtime data", () => {
		delete process.env.CLAWDI_SERVICE_STATE_DIR;
		delete process.env.CLAWDI_RUN_DIR;
		delete process.env.CLAWDI_RUNTIME_HOME;
		delete process.env.CLAWDI_HOME;
		delete process.env.HOME;

		const paths = getRuntimePaths({ mode: "hosted" });

		expect(paths.configurationRoot).toBe("/etc/iforge");
		expect(paths.fileBrowserConfigRoot).toBe("/run/iforge/files");
		expect(paths.fileBrowserConfig).toBe("/run/iforge/files/filebrowser.yaml");
		expect(paths.serviceStateRoot).toBe("/var/lib/iforge");
		expect(paths.clawdiHome).toBe("/var/lib/iforge-user");
		expect(paths.cacheRoot).toBe("/var/cache/iforge");
		expect(paths.manifestLastGood).toBe(
			"/var/lib/iforge/committed-runtime/manifest.last-good.json",
		);
		expect(paths.managedSecretCacheFile).toBe(
			"/var/lib/iforge/committed-runtime/runtime-secrets.last-good.json",
		);
		const local = getRuntimePaths({ mode: "local" });
		expect(local.manifestLastGood).toBe(join(local.cacheRoot, "manifest.last-good.json"));
		expect(local.managedSecretCacheFile).toBe(
			join(local.cacheRoot, "runtime-secrets.last-good.json"),
		);
		expect(paths.runRoot).toBe("/run/iforge");
		expect(paths.bootStatus).toBe("/var/lib/iforge/status/boot-status.json");
		expect(paths.cliManagedBin).toBe("/var/lib/iforge/maintained/clawdi/bin/clawdi");
		expect(paths.cliNpmPrefix).toBe("/var/lib/iforge/maintained/clawdi/npm");
		expect(paths.cliNpmCache).toBe("/var/cache/iforge/npm");
		expect(paths.fileBrowserInstallRoot).toBe("/var/lib/iforge/maintained/filebrowser");
		expect(paths.managedResourceRoot).toBe("/var/lib/iforge/managed-resources");
		expect(paths.fileBrowserStateRoot).toBe("/var/lib/iforge-files");
		expect(paths.egressServiceBinary).toBe("/run/iforge/egress/systemd/mitmdump");
		expect(paths.fileBrowserServiceBinary).toBe("/run/iforge-files/filebrowser");
		expect(paths.userNpmPrefix).toBe("/home/iforge/.local");
	});

	test("derives isolated config and cache roots from a service-state fixture", () => {
		process.env.CLAWDI_SERVICE_STATE_DIR = "/tmp/runtime-fixture/var/lib/clawdi";
		process.env.CLAWDI_RUN_DIR = "/tmp/runtime-fixture/run/clawdi";

		const paths = getRuntimePaths({ mode: "hosted" });

		expect(paths.configurationRoot).toBe("/tmp/runtime-fixture/etc/clawdi");
		expect(paths.fileBrowserConfigRoot).toBe("/tmp/runtime-fixture/run/clawdi/files");
		expect(paths.fileBrowserConfig).toBe("/tmp/runtime-fixture/run/clawdi/files/filebrowser.yaml");
		expect(paths.cacheRoot).toBe("/tmp/runtime-fixture/var/cache/clawdi");
		expect(paths.clawdiHome).toBe("/tmp/runtime-fixture/var/lib/clawdi-user");
		expect(paths.fileBrowserStateRoot).toBe("/var/lib/iforge-files");
	});

	test("keeps hosted CLI user state outside the default runtime home", () => {
		delete process.env.HOME;
		delete process.env.CLAWDI_RUNTIME_HOME;
		delete process.env.CLAWDI_HOME;

		const paths = getRuntimePaths({ mode: "hosted" });

		expect(paths.userHome).toBe("/home/iforge");
		expect(paths.clawdiHome).toBe("/var/lib/iforge-user");
		expect(paths.systemdUserRoot).toBe("/home/iforge/.config/systemd/user");
	});

	test("keeps hosted CLI user state outside an explicit runtime home", () => {
		process.env.HOME = "/root";
		process.env.CLAWDI_RUNTIME_HOME = "/srv/clawdi";
		delete process.env.CLAWDI_HOME;

		const paths = getRuntimePaths({ mode: "hosted" });

		expect(paths.userHome).toBe("/srv/clawdi");
		expect(paths.clawdiHome).toBe("/var/lib/iforge-user");
	});

	test("preserves the explicit Clawdi state override in hosted mode", () => {
		process.env.HOME = "/root";
		process.env.CLAWDI_RUNTIME_HOME = "/srv/clawdi";
		process.env.CLAWDI_HOME = "/var/lib/example-clawdi";

		const paths = getRuntimePaths({ mode: "hosted" });

		expect(paths.userHome).toBe("/srv/clawdi");
		expect(paths.clawdiHome).toBe("/var/lib/example-clawdi");
	});

	test("keeps local CLI paths based on HOME", () => {
		process.env.HOME = "/tmp/local-user";
		process.env.CLAWDI_RUNTIME_HOME = "/srv/ignored-hosted-home";
		delete process.env.CLAWDI_HOME;

		const paths = getRuntimePaths({ mode: "local" });

		expect(paths.userHome).toBe("/tmp/local-user");
		// DERIVED, not restated (P33-11). The local branch resolves through the state-root
		// authority; a literal here would be a second place to forget on the next move, and it
		// would pass against a stale constant rather than against what the CLI actually does.
		expect(paths.clawdiHome).toBe(stateRoot());
		expect(paths.clawdiHome.startsWith("/tmp/local-user/")).toBe(true);
	});

	test("resolves hosted user state independently of the local state-root authority", () => {
		// P33-21 asserted rather than remembered. The collapse in plan 03.3-02 pulled four local
		// resolvers onto one authority and deliberately left this branch out; without this test
		// the only record of that would be a comment, and the next person to see two resolvers
		// where the plan promised one would "finish" the collapse and relocate hosted state on
		// every provisioned server.
		process.env.HOME = "/tmp/local-user";
		process.env.CLAWDI_RUNTIME_HOME = "/srv/clawdi";
		delete process.env.CLAWDI_HOME;

		const hosted = getRuntimePaths({ mode: "hosted" });

		expect(hosted.clawdiHome).not.toBe(stateRoot());
		// Upstream #924 (sync step 1) moved hosted CLI state out of the tenant home, so it no longer
		// follows CLAWDI_RUNTIME_HOME at all.
		expect(hosted.clawdiHome).toBe(DEFAULT_RUNTIME_USER_CLI_STATE_ROOT);
		expect(hosted.clawdiHome.startsWith("/srv/clawdi/")).toBe(false);
	});
});
