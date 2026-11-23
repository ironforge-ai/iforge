import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
	IFORGE_DESKTOP_ENV_ALIASES,
	publicDesktopEnvText,
	withDesktopBrandEnv,
} from "./iforge-brand-env";
import { readDesktopReleaseConfiguration } from "./release-contract";

// iforge (06.1-14, D-23): fork-owned. The desktop's six public release names are IFORGE_* only;
// this boundary copies them into the CLAWDI_* register upstream's release reader reads, and a
// bare CLAWDI_* name is refused exactly as packages/cli/src/lib/brand-env.ts refuses one.

const REPO_ROOT = join(import.meta.dir, "..", "..", "..");
const CONTRACT_PATH = join(REPO_ROOT, "config", "rebrand-env-contract.txt");
const DESKTOP_PUBLIC_PREFIXES = ["IFORGE_DESKTOP_", "IFORGE_WINDOWS_"];

const SIX_PAIRS: Record<string, string> = {
	IFORGE_DESKTOP_ARCH: "CLAWDI_DESKTOP_ARCH",
	IFORGE_DESKTOP_UPDATE_CHANNEL: "CLAWDI_DESKTOP_UPDATE_CHANNEL",
	IFORGE_DESKTOP_UPDATE_FEED_URL: "CLAWDI_DESKTOP_UPDATE_FEED_URL",
	IFORGE_DESKTOP_VERSION: "CLAWDI_DESKTOP_VERSION",
	IFORGE_WINDOWS_PUBLISHER: "CLAWDI_WINDOWS_PUBLISHER",
	IFORGE_WINDOWS_TASK_TEST: "CLAWDI_WINDOWS_TASK_TEST",
};

function desktopPairsInContract(): Record<string, string> {
	const pairs: Record<string, string> = {};
	for (const raw of readFileSync(CONTRACT_PATH, "utf8").split("\n")) {
		const line = raw.trim();
		if (!line || line.startsWith("#")) continue;
		const [publicName, internalName] = line.split("=");
		if (!publicName || !internalName) continue;
		if (!DESKTOP_PUBLIC_PREFIXES.some((prefix) => publicName.startsWith(prefix))) continue;
		pairs[publicName] = internalName;
	}
	return pairs;
}

describe("desktop IFORGE_* boundary", () => {
	test("names exactly the six public desktop release names, sorted", () => {
		expect(IFORGE_DESKTOP_ENV_ALIASES).toEqual(SIX_PAIRS);
		const names = Object.keys(IFORGE_DESKTOP_ENV_ALIASES);
		expect(names).toEqual([...names].sort());
	});

	test("equals the IFORGE_DESKTOP_* and IFORGE_WINDOWS_* pairs in config/rebrand-env-contract.txt", () => {
		expect(IFORGE_DESKTOP_ENV_ALIASES).toEqual(desktopPairsInContract());
	});

	for (const [publicName, internalName] of Object.entries(SIX_PAIRS)) {
		test(`copies ${publicName} into ${internalName}`, () => {
			expect(withDesktopBrandEnv({ [publicName]: "value-1" })[internalName]).toBe("value-1");
		});

		test(`refuses a bare ${internalName} with no ${publicName}`, () => {
			const env = withDesktopBrandEnv({ [internalName]: "user-set" });
			expect(internalName in env).toBe(false);
		});

		test(`the public ${publicName} wins over a user-set ${internalName}`, () => {
			const env = withDesktopBrandEnv({ [publicName]: "public", [internalName]: "internal" });
			expect(env[internalName]).toBe("public");
		});
	}

	test("an empty public value is unset and does not rescue a user-set internal name", () => {
		const env = withDesktopBrandEnv({ IFORGE_DESKTOP_ARCH: "", CLAWDI_DESKTOP_ARCH: "x64" });
		expect("CLAWDI_DESKTOP_ARCH" in env).toBe(false);
	});

	test("keeps the public names and every non-member untouched", () => {
		const env = withDesktopBrandEnv({
			IFORGE_DESKTOP_VERSION: "1.2.3",
			CLAWDI_DESKTOP_CLI: "/tmp/cli",
			CSC_NAME: "Developer ID Application: iforge",
		});
		expect(env.IFORGE_DESKTOP_VERSION).toBe("1.2.3");
		expect(env.CLAWDI_DESKTOP_CLI).toBe("/tmp/cli");
		expect(env.CSC_NAME).toBe("Developer ID Application: iforge");
	});

	test("does not mutate its input", () => {
		const input = { IFORGE_DESKTOP_ARCH: "x64", CLAWDI_WINDOWS_PUBLISHER: "CN=x" };
		const snapshot = { ...input };
		const output = withDesktopBrandEnv(input);
		expect(input).toEqual(snapshot);
		expect(output).not.toBe(input);
	});

	test("feeds upstream's release reader from the IFORGE_* spelling only", () => {
		const configuration = readDesktopReleaseConfiguration(
			withDesktopBrandEnv({
				IFORGE_DESKTOP_ARCH: "x64",
				IFORGE_DESKTOP_VERSION: "1.2.3-beta.1",
				IFORGE_DESKTOP_UPDATE_CHANNEL: "beta",
				IFORGE_DESKTOP_UPDATE_FEED_URL: "https://downloads.example.test/iforge/desktop/beta/",
			}),
			"linux",
		);
		expect(configuration).toMatchObject({
			arch: "x64",
			version: "1.2.3-beta.1",
			channel: "beta",
			updateFeedUrl: "https://downloads.example.test/iforge/desktop/beta/",
		});
		expect(() =>
			readDesktopReleaseConfiguration(
				withDesktopBrandEnv({
					CLAWDI_DESKTOP_VERSION: "1.2.3",
					CLAWDI_DESKTOP_UPDATE_FEED_URL: "https://downloads.example.test/iforge/desktop/stable/",
				}),
				"linux",
			),
		).toThrow();
	});

	test("rewrites the register names in a release error to their public spelling", () => {
		expect(
			publicDesktopEnvText(
				"Windows signing requires WIN_CSC_LINK, WIN_CSC_KEY_PASSWORD and CLAWDI_WINDOWS_PUBLISHER together.",
			),
		).toBe(
			"Windows signing requires WIN_CSC_LINK, WIN_CSC_KEY_PASSWORD and IFORGE_WINDOWS_PUBLISHER together.",
		);
		expect(publicDesktopEnvText("CLAWDI_DESKTOP_UPDATE_FEED_URL must be explicit.")).toBe(
			"IFORGE_DESKTOP_UPDATE_FEED_URL must be explicit.",
		);
		expect(publicDesktopEnvText("CLAWDI_DESKTOP_UPDATE_CHANNEL_X")).toBe(
			"CLAWDI_DESKTOP_UPDATE_CHANNEL_X",
		);
	});
});
