import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import {
	BILLING_LOCK_HIDDEN_SETTINGS_SECTIONS,
	isSettingsSectionVisibleWhileLocked,
	lockedSettingsFallback,
} from "@/lib/billing-lock-settings";
import { SETTINGS_SECTION_IDS, type SettingsSectionId } from "@/lib/settings-routes";

/**
 * 06.3 UI-SPEC L4 (D-03): Settings while billing-locked.
 *
 * Locked users see General, Wallet and Compute; API Keys, AI Usage and Data call
 * gated routes and are hidden (not disabled). A deep link to a hidden section
 * lands on Compute when it is visible, General otherwise. Unlocked behavior is
 * unchanged: an invisible request falls back to General.
 */

const HIDDEN: readonly SettingsSectionId[] = ["api-keys", "billing-usage", "data"];
const VISIBLE: readonly SettingsSectionId[] = ["general", "billing-wallet", "billing-plan"];
const HOSTED_LOCKED_VISIBLE: readonly SettingsSectionId[] = [
	"general",
	"billing-wallet",
	"billing-plan",
];
const OSS_LOCKED_VISIBLE: readonly SettingsSectionId[] = ["general"];

describe("isSettingsSectionVisibleWhileLocked", () => {
	test.each([...SETTINGS_SECTION_IDS])("not locked: %s is visible", (id) => {
		expect(isSettingsSectionVisibleWhileLocked(id, false)).toBe(true);
	});

	test.each([...HIDDEN])("locked: %s is hidden", (id) => {
		expect(isSettingsSectionVisibleWhileLocked(id, true)).toBe(false);
	});

	test.each([...VISIBLE])("locked: %s stays visible", (id) => {
		expect(isSettingsSectionVisibleWhileLocked(id, true)).toBe(true);
	});

	test("the hidden set is exactly API Keys, AI Usage and Data", () => {
		expect([...BILLING_LOCK_HIDDEN_SETTINGS_SECTIONS].sort()).toEqual([...HIDDEN].sort());
	});
});

describe("lockedSettingsFallback", () => {
	test.each([...HIDDEN])("locked hosted: a deep link to %s opens Compute", (requested) => {
		expect(
			lockedSettingsFallback({ requested, visibleIds: HOSTED_LOCKED_VISIBLE, locked: true }),
		).toBe("billing-plan");
	});

	test.each([...HIDDEN])("locked OSS: a deep link to %s opens General", (requested) => {
		expect(
			lockedSettingsFallback({ requested, visibleIds: OSS_LOCKED_VISIBLE, locked: true }),
		).toBe("general");
	});

	test.each([...HOSTED_LOCKED_VISIBLE])("locked: a visible %s is kept", (requested) => {
		expect(
			lockedSettingsFallback({ requested, visibleIds: HOSTED_LOCKED_VISIBLE, locked: true }),
		).toBe(requested);
	});

	test("not locked: an invisible request falls back to General (today's behavior)", () => {
		expect(
			lockedSettingsFallback({
				requested: "billing-usage",
				visibleIds: ["general", "api-keys"],
				locked: false,
			}),
		).toBe("general");
	});

	test("not locked: a visible request is kept", () => {
		expect(
			lockedSettingsFallback({
				requested: "api-keys",
				visibleIds: ["general", "api-keys", "billing-plan"],
				locked: false,
			}),
		).toBe("api-keys");
	});
});

describe("OSS boundary", () => {
	test("the module imports nothing from the hosted tree", () => {
		const code = readFileSync(new URL("./billing-lock-settings.ts", import.meta.url), "utf8");
		expect(code.length).toBeGreaterThan(0);
		expect(code).not.toContain("@/hosted/");
	});
});
