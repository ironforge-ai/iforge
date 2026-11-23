import { describe, expect, test } from "bun:test";
import {
	ApiError,
	ApiNetworkError,
	apiErrorCode,
	formatApiError,
	isAccountSuspendedError,
	isApiAuthError,
	isApiNetworkError,
	isApiNotFoundError,
	isApiServerError,
	normalizeApiError,
	parseApiDetail,
} from "./api-errors";

describe("API error details", () => {
	test("parses nested FastAPI details", () => {
		const detail = parseApiDetail(
			JSON.stringify({
				detail: {
					code: "vault_conflicts_blocked",
					message: "Source project has 1 vault key that already exists.",
					conflicts: [{ vault_slug: "prod", section: "api", item_name: "TOKEN" }],
				},
			}),
		);

		expect(detail).toEqual({
			code: "vault_conflicts_blocked",
			message: "Source project has 1 vault key that already exists.",
			conflicts: [{ vault_slug: "prod", section: "api", item_name: "TOKEN" }],
		});
	});

	test("formats structured API errors without exposing raw JSON", () => {
		const message = formatApiError(
			JSON.stringify({
				detail: {
					error: "already_member",
					message: "Already a member.",
				},
			}),
		);

		expect(message).toBe("Already a member.");
	});

	test("keeps plain text API errors readable", () => {
		expect(formatApiError("project not found")).toBe("project not found");
	});

	test("extracts stable codes from Problem Details and structured FastAPI details", () => {
		expect(apiErrorCode({ code: "account_suspended", detail: "Account is suspended" })).toBe(
			"account_suspended",
		);
		expect(apiErrorCode({ detail: { code: "already_member" } })).toBe("already_member");
	});
});

describe("cloud-api error classification", () => {
	test("401 is an auth error, not server", () => {
		const e = new ApiError(401, "token expired");
		expect(isApiAuthError(e)).toBe(true);
		expect(isApiNotFoundError(e)).toBe(false);
		expect(isApiServerError(e)).toBe(false);
		expect(isApiNetworkError(e)).toBe(false);
	});

	test("suspension is distinguishable from an ordinary 401", () => {
		expect(
			isAccountSuspendedError(new ApiError(401, "Account is suspended", "account_suspended")),
		).toBe(true);
		expect(isAccountSuspendedError(new ApiError(401, "token expired"))).toBe(false);
	});

	test("404 is a not-found error, not transport", () => {
		const e = new ApiError(404, "agent not found");
		expect(isApiNotFoundError(e)).toBe(true);
		expect(isApiAuthError(e)).toBe(false);
		expect(isApiServerError(e)).toBe(false);
		expect(isApiNetworkError(e)).toBe(false);
	});

	test("5xx and 429 are server errors", () => {
		for (const status of [500, 502, 503, 429]) {
			expect(isApiServerError(new ApiError(status, "boom"))).toBe(true);
		}
	});

	test("network errors classify as transport failures", () => {
		for (const kind of ["timeout", "offline"] as const) {
			const e = new ApiNetworkError(kind);
			expect(isApiNetworkError(e)).toBe(true);
			expect(isApiAuthError(e)).toBe(false);
			expect(isApiNotFoundError(e)).toBe(false);
			expect(isApiServerError(e)).toBe(false);
		}
	});

	test("non-api errors classify as nothing", () => {
		const e = new Error("random");
		expect(isApiAuthError(e)).toBe(false);
		expect(isApiNotFoundError(e)).toBe(false);
		expect(isApiServerError(e)).toBe(false);
		expect(isApiNetworkError(e)).toBe(false);
	});
});

describe("normalizeApiError", () => {
	test("timeout → try-again guidance", () => {
		expect(normalizeApiError(new ApiNetworkError("timeout"))).toMatch(/taking longer than usual/i);
	});

	test("offline → connection guidance", () => {
		expect(normalizeApiError(new ApiNetworkError("offline"))).toMatch(
			/couldn't reach the service/i,
		);
	});

	test("401 → session expired prompt", () => {
		expect(normalizeApiError(new ApiError(401, "jwt expired"))).toMatch(/session has expired/i);
	});

	test("suspension 401 uses deactivation copy without exposing detail", () => {
		const message = normalizeApiError(
			new ApiError(401, "internal suspension detail", "account_suspended"),
		);
		expect(message).toMatch(/account has been deactivated/i);
		expect(message).not.toContain("internal suspension detail");
	});

	test("5xx → transient message, not the raw detail", () => {
		const msg = normalizeApiError(new ApiError(503, "upstream connect error"));
		expect(msg).toMatch(/having trouble/i);
		expect(msg).not.toMatch(/upstream connect error/);
	});

	test("snake_case codes stay internal while user-facing sentences pass through", () => {
		expect(normalizeApiError(new ApiError(400, "provider_not_found"))).toBe(
			"The request could not be completed. Review the details and try again.",
		);
		expect(normalizeApiError(new ApiError(409, "That name is already in use."))).toBe(
			"That name is already in use.",
		);
	});

	test("nested FastAPI detail is unwrapped to its message", () => {
		const detail = JSON.stringify({
			detail: { error: "already_member", message: "Already a member." },
		});
		expect(normalizeApiError(new ApiError(403, detail))).toBe("Already a member.");
	});

	test("unknown shapes get a safe fallback", () => {
		expect(normalizeApiError(null)).toMatch(/something went wrong/i);
		expect(normalizeApiError(new Error("internal request detail"))).not.toContain(
			"internal request detail",
		);
	});
});

/*
 * W-04 / WR-05 / UI-REVIEW blocker 1 — no backend payload shape may reach a
 * user as copy.
 *
 * Phase 03 is the phase that made FastAPI's `detail` an OBJECT rather than a
 * string (`{"code":"billing_required"}`, `{"code":"invite_rate_limited"}`,
 * `{"code":"invite_invalid"}`, `{"code":"billing_unavailable"}`).
 * `parseApiDetail` unwraps `body.detail` to that object, `formatApiError`
 * probed only for a `message` key, found none, and fell through to
 * `return detail` — the original raw JSON string. It then survived
 * `normalizeApiError`'s snake_case humanizer (`/^[a-z0-9_]+$/` cannot match
 * JSON), reached `ApiErrorPanel` verbatim, and `aiProviderErrorNormalizer`
 * appended " iforge AI still works." — so a billing-locked user saw
 * `{"detail":{"code":"billing_required"}} iforge AI still works.` on the exact
 * screen SC-2 sends them to in order to pay.
 *
 * The table below drives REAL bodies this phase emits. The brace/quote/"detail"
 * guard runs over EVERY row, which is what makes a future payload shape unable
 * to regress this.
 */

/** The single "we do not know what happened" sentence in the app — pinned here
 * so `formatApiError`'s terminal fallback and `normalizeApiError`'s unknown-
 * error arm cannot drift into two different house sentences. */
const GENERIC_COPY = "Something went wrong. Please try again.";

/** Upstream's copy for a bare snake_case code (sync step 3): an internal code is never
 * product copy, so `normalizeApiError` answers with one fixed sentence instead of
 * title-casing the token. Stricter than this suite's rule, never looser. */
const UNREADABLE_CODE_COPY =
	"The request could not be completed. Review the details and try again.";

const PAYLOAD_CASES: ReadonlyArray<{
	name: string;
	detail: string;
	status: number;
	formatted: string;
	normalized: string;
}> = [
	{
		name: "403 billing_required (the paid-path defect)",
		detail: JSON.stringify({ detail: { code: "billing_required" } }),
		status: 403,
		formatted: "billing_required",
		normalized: UNREADABLE_CODE_COPY,
	},
	{
		name: "429 invite_rate_limited",
		detail: JSON.stringify({ detail: { code: "invite_rate_limited" } }),
		status: 403,
		formatted: "invite_rate_limited",
		normalized: UNREADABLE_CODE_COPY,
	},
	{
		name: "409 already_provisioned (03-14's CR-01 contract)",
		detail: JSON.stringify({ detail: { code: "already_provisioned" } }),
		status: 409,
		formatted: "already_provisioned",
		normalized: UNREADABLE_CODE_COPY,
	},
	{
		name: "a message key still wins over the code probe",
		detail: JSON.stringify({ detail: { error: "already_member", message: "Already a member." } }),
		status: 403,
		formatted: "Already a member.",
		normalized: "Already a member.",
	},
	{
		name: "a string detail passes through unchanged",
		detail: JSON.stringify({ detail: "plain sentence" }),
		status: 400,
		formatted: "plain sentence",
		normalized: "plain sentence",
	},
	{
		name: "a body that is not JSON at all passes through unchanged",
		detail: "project not found",
		status: 404,
		formatted: "project not found",
		normalized: "project not found",
	},
	{
		name: "an object with neither message nor a string code",
		detail: JSON.stringify({ detail: { conflicts: [{ vault_slug: "prod" }] } }),
		status: 409,
		formatted: GENERIC_COPY,
		normalized: GENERIC_COPY,
	},
	{
		name: "a non-string code is not copy either",
		detail: JSON.stringify({ detail: { code: 42 } }),
		status: 400,
		formatted: GENERIC_COPY,
		normalized: GENERIC_COPY,
	},
	{
		name: "an empty detail object",
		detail: JSON.stringify({ detail: {} }),
		status: 400,
		formatted: GENERIC_COPY,
		normalized: GENERIC_COPY,
	},
	{
		name: "an empty body",
		detail: JSON.stringify({}),
		status: 400,
		formatted: GENERIC_COPY,
		normalized: GENERIC_COPY,
	},
];

describe("no API payload shape becomes customer copy (W-04 / WR-05)", () => {
	for (const row of PAYLOAD_CASES) {
		test(`${row.name} — formatApiError`, () => {
			expect(formatApiError(row.detail)).toBe(row.formatted);
		});

		test(`${row.name} — normalizeApiError`, () => {
			expect(normalizeApiError(new ApiError(row.status, row.detail))).toBe(row.normalized);
		});

		test(`${row.name} — leaks no payload punctuation`, () => {
			for (const output of [
				formatApiError(row.detail),
				normalizeApiError(new ApiError(row.status, row.detail)),
			]) {
				expect(output).not.toContain("{");
				expect(output).not.toContain("}");
				expect(output).not.toContain('"');
				// The payload KEY, not the English word: upstream's own sentence says "Review
				// the details", and a raw body would carry the key in quotes, guarded above.
				expect(output).not.toMatch(/\bdetail\b/);
			}
		});
	}

	test("the two entry points share ONE unknown-error sentence", () => {
		// Inventing a second house sentence for "we do not know what happened"
		// is what this assertion exists to prevent.
		expect(formatApiError(JSON.stringify({ detail: {} }))).toBe(GENERIC_COPY);
		expect(normalizeApiError(null)).toBe(GENERIC_COPY);
	});

	test("the generic sentence obeys the house rules (DESIGN.md, CI-greppable)", () => {
		expect(GENERIC_COPY).not.toContain("!");
		expect(GENERIC_COPY.toLowerCase()).not.toContain("oops");
	});

	test("the status branches still run AHEAD of the formatter", () => {
		const payload = JSON.stringify({ detail: { code: "billing_required" } });
		expect(normalizeApiError(new ApiError(401, payload))).toMatch(/session has expired/i);
		expect(normalizeApiError(new ApiError(500, payload))).toMatch(/having trouble/i);
		expect(normalizeApiError(new ApiError(429, payload))).toMatch(/having trouble/i);
	});
});
