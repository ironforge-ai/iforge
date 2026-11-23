import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import type { AdminUserListItem } from "@/lib/admin-client";
import {
	BillingExemptionPanel,
	EXEMPTION_OFF_COPY,
	EXEMPTION_ON_COPY,
	EXEMPTION_PARTNER_COPY,
	EXEMPTION_REASON_MAX,
	EXEMPTION_SWITCH_LABEL,
	isValidExemptionReason,
} from "@/pages/dashboard/admin/billing-exemption-section";

function tenantRow(overrides: Partial<AdminUserListItem> = {}): AdminUserListItem {
	return {
		id: "00000000-0000-0000-0000-0000000000b1",
		email: "tenant@example.com",
		clerk_id: "user_2fExemptionFixture",
		name: null,
		created_at: "2026-09-01T09:00:00Z",
		billing_exempt: false,
		...overrides,
	};
}

function renderPanel(tenant: AdminUserListItem): string {
	return renderToStaticMarkup(
		<BillingExemptionPanel tenant={tenant} onSetExempt={() => Promise.resolve()} />,
	)
		.replaceAll("&#x27;", "'")
		.replaceAll("&quot;", '"')
		.replaceAll("&amp;", "&");
}

/** The switch's own tag, found by its accessible name. */
function switchTag(markup: string): string {
	const match = markup.match(new RegExp(`<[^>]*aria-label="${EXEMPTION_SWITCH_LABEL}"[^>]*>`));
	if (!match) throw new Error(`no switch labelled "${EXEMPTION_SWITCH_LABEL}" in: ${markup}`);
	return match[0];
}

describe("BillingExemptionPanel", () => {
	test("a gated Clerk tenant shows an off switch and the subscription copy", () => {
		const markup = renderPanel(tenantRow());
		expect(switchTag(markup)).toContain('aria-checked="false"');
		expect(markup).toContain(EXEMPTION_OFF_COPY);
		expect(markup).not.toContain(EXEMPTION_ON_COPY);
	});

	test("an exempt Clerk tenant shows an on switch and the exemption copy", () => {
		const markup = renderPanel(tenantRow({ billing_exempt: true }));
		expect(switchTag(markup)).toContain('aria-checked="true"');
		expect(markup).toContain(EXEMPTION_ON_COPY);
	});

	test("a partner tenant is shown exempt with the switch disabled", () => {
		const markup = renderPanel(tenantRow({ clerk_id: null, email: null, billing_exempt: true }));
		const tag = switchTag(markup);
		expect(tag).toContain('aria-checked="true"');
		expect(tag).toMatch(/aria-disabled="true"|data-disabled/);
		expect(markup).toContain(EXEMPTION_PARTNER_COPY);
	});
});

describe("isValidExemptionReason mirrors the backend's reason check", () => {
	test.each([
		["design partner", true],
		["", false],
		[" padded", false],
		["padded ", false],
		["line\nbreak", false],
		["x".repeat(EXEMPTION_REASON_MAX), true],
		["x".repeat(EXEMPTION_REASON_MAX + 1), false],
	])("%p → %p", (reason, valid) => {
		expect(isValidExemptionReason(reason)).toBe(valid);
	});
});

type ReasonCase =
	| { id: string; valid: boolean; reason: string }
	| { id: string; valid: boolean; repeat: { text: string; times: number } };

/** WR-05 (06.3-21): the shared parity set. The backend schema is the
 * authority; backend/tests/test_admin_billing_exemption.py asserts the same
 * billing_exemption_reason_cases.json against AdminBillingExemptionUpdate. */
function loadReasonCases(): Array<{ id: string; reason: string; valid: boolean }> {
	const raw = readFileSync(
		new URL(
			"../../../../../../backend/tests/fixtures/billing_exemption_reason_cases.json",
			import.meta.url,
		),
		"utf8",
	);
	const cases = (JSON.parse(raw) as { cases: ReasonCase[] }).cases;
	if (!Array.isArray(cases) || cases.length === 0) {
		throw new Error("billing_exemption_reason_cases.json has no cases");
	}
	return cases.map((entry) => ({
		id: entry.id,
		valid: entry.valid,
		reason: "repeat" in entry ? entry.repeat.text.repeat(entry.repeat.times) : entry.reason,
	}));
}

describe("isValidExemptionReason agrees with the backend on the shared fixture (WR-05)", () => {
	const cases = loadReasonCases();

	test("the fixture loads and is non-empty", () => {
		expect(cases.length).toBeGreaterThan(0);
	});

	test.each(cases.map((entry) => [entry.id, entry.reason, entry.valid] as const))(
		"%s",
		(_id, reason, valid) => {
			expect(isValidExemptionReason(reason)).toBe(valid);
		},
	);
});
