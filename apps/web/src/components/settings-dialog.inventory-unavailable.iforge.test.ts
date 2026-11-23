import { beforeAll, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";

/**
 * Fork-owned pin for the 06.1 UI-REVIEW warning: Settings → Wallet showed
 * `RouteLoadingSkeleton` forever when the hosted inventory failed, for a user
 * whose billing visibility waits on that inventory. The panel now settles on an
 * error with Retry, as the agent rail does (e398995ce). It lives beside the
 * dialog, not in an upstream-tracked test, so an upstream merge cannot drop it.
 */

type Helper = typeof import("@/components/settings-dialog").billingInventoryUnavailable;

let helper: Helper | null = null;

beforeAll(async () => {
	process.env.VITE_IFORGE_API_URL = "http://localhost:8000";
	process.env.VITE_CLERK_PUBLISHABLE_KEY = "pk_test_dummy";
	helper = (await import("@/components/settings-dialog")).billingInventoryUnavailable;
});

function unavailable(args: Parameters<Helper>[0]): boolean {
	if (!helper) throw new Error("billingInventoryUnavailable was not loaded");
	return helper(args);
}

const FAILED = {
	billingAccessPending: true,
	mounted: true,
	accessLoading: false,
	inventoryUnavailable: true,
};

describe("billing settings settle when the hosted inventory is unavailable", () => {
	test("pending only on a failed inventory settles as unavailable", () => {
		expect(unavailable(FAILED)).toBe(true);
	});

	test("a first load, an unmounted dialog or loading access keeps the skeleton", () => {
		expect(unavailable({ ...FAILED, inventoryUnavailable: false })).toBe(false);
		expect(unavailable({ ...FAILED, mounted: false })).toBe(false);
		expect(unavailable({ ...FAILED, accessLoading: true })).toBe(false);
	});

	test("a section that is not pending is never replaced", () => {
		expect(unavailable({ ...FAILED, billingAccessPending: false })).toBe(false);
	});
});

describe("settings dialog wiring", () => {
	const dialog = readFileSync(new URL("./settings-dialog.tsx", import.meta.url), "utf8");
	const sidebar = readFileSync(new URL("./app-sidebar.tsx", import.meta.url), "utf8");

	test("the unavailable panel renders before the skeleton and offers the inventory retry", () => {
		const failed = dialog.indexOf("{billingInventoryFailed ? (");
		const skeleton = dialog.indexOf(") : billingAccessPending ? (");
		expect(failed).toBeGreaterThan(-1);
		expect(skeleton).toBeGreaterThan(failed);
		const branch = dialog.slice(failed, skeleton);
		expect(branch).toContain("<ApiErrorPanel");
		expect(branch).toContain("onRetry={onRetryCloudInventory ?? undefined}");
		expect(branch).toContain('title="iforge Cloud inventory unavailable"');
	});

	test("the sidebar hands the dialog the rail's unavailable state and retry", () => {
		expect(sidebar).toContain(
			"cloudInventoryUnavailable={unifiedAgentListEnabled && hostedInventoryUnavailable}",
		);
		expect(sidebar).toContain("onRetryCloudInventory={retryHostedInventory}");
	});
});
