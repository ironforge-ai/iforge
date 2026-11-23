import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { FREE_TIER_OFFERED } from "@/hosted/billing/deploy/deploy-defaults";

/**
 * Fork-owned pins for the withdrawn free Basic slot (fork 53535ff6).
 *
 * These lived in the upstream-tracked `deploy-wizard.test.ts` until the step-2 sync
 * (06.1-06) moved them here, so an upstream rewrite of that file can no longer
 * drop them in a merge resolution and they add no conflict surface to it (03.1 D-02,
 * T-06.1-17). Upstream #1142 replaced the deployment-inventory free-slot derivation
 * with the server-side `GET /v2/subscriptions/included-basic` query; the withdrawal
 * is asserted on that query.
 */

const wizardSource = readFileSync(new URL("./deploy-wizard.tsx", import.meta.url), "utf8");
const deployDefaultsSource = readFileSync(new URL("./deploy-defaults.ts", import.meta.url), "utf8");
const billingHooksSource = readFileSync(new URL("../hooks.ts", import.meta.url), "utf8");

describe("withdrawn free Basic slot (FREE_TIER_OFFERED)", () => {
	test("withdraws the free Basic slot so every deploy goes through checkout", () => {
		// Basic remains selectable as an ordinary paid tier; only the free
		// included-subscription source is withdrawn. Reporting the included slot
		// as unavailable is what keeps the source picker on reusable or new
		// subscriptions.
		expect(FREE_TIER_OFFERED).toBe(false);
		expect(deployDefaultsSource).toContain("export const FREE_TIER_OFFERED = false;");
		expect(wizardSource).toContain("const includedBasicAvailable = !FREE_TIER_OFFERED");
		expect(wizardSource).toContain("showIncluded={includedBasicAvailable === true}");
	});

	test("gates every surface that exists only to explain the availability check", () => {
		// The surfaces that exist only to explain the availability check must not
		// reach a user who can no longer be offered the free slot. Each is gated,
		// and the error panel matters most: a billing-locked user's availability
		// read 403s, and an ungated block would stop them buying the paid tier.
		for (const gated of [
			"useIncludedBasicAvailability({ enabled: FREE_TIER_OFFERED })",
			"FREE_TIER_OFFERED && shouldBlockQueryError(includedBasic.error, includedBasic.data)",
		]) {
			expect(wizardSource).toContain(gated);
		}
		// The ungated upstream call would run the query and could block on its error.
		expect(wizardSource).not.toContain("useIncludedBasicAvailability()");
	});

	test("keeps the whole included-slot apparatus so flipping the flag restores it", () => {
		// The query hook still takes the switch, and the resolver still derives
		// availability from the server's slot count once the offer is back on.
		expect(billingHooksSource).toContain(
			"export function useIncludedBasicAvailability({ enabled = true }: { enabled?: boolean } = {})",
		);
		expect(wizardSource).toContain("includedBasic.data.available_slots > 0");
		expect(wizardSource).toContain('title="Couldn\'t check included Basic availability"');
	});
});
