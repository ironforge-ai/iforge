import { describe, expect, test } from "bun:test";
import { BillingApiError, isRetryableError, normalizeBillingError } from "@/hosted/billing/errors";

/**
 * Fork-owned pin for 06.2-UI-SPEC H2 (D-02): a compute outage reads as a compute
 * outage, and says billing still works.
 *
 * The hosted service answers an inventory read with 503 `compute_unavailable`
 * while the provisioner is unreachable. Upstream maps every 5xx to the generic
 * billing sentence, which blames the wrong plane. The fork adds one branch for
 * that code ahead of the server-error fallback and leaves the fallback itself
 * unchanged. This file lives beside `errors.ts`, not in the upstream-tracked
 * `errors.test.ts`, so an upstream merge cannot drop it (03.1 D-02).
 */

const COMPUTE_UNAVAILABLE_SENTENCE =
	"Agent compute isn’t available right now. Your Wallet and subscriptions still work. Try again later.";
const SERVER_ERROR_SENTENCE =
	"The billing request couldn’t be completed right now. Try again in a moment.";

function computeUnavailable(): BillingApiError {
	return new BillingApiError(503, "", {
		detail: {
			code: "compute_unavailable",
			message: "Agent compute is not available right now.",
		},
	});
}

describe("06.2 H2: compute_unavailable has its own sentence", () => {
	test("a 503 compute_unavailable normalizes to the H2 sentence", () => {
		expect(normalizeBillingError(computeUnavailable())).toBe(COMPUTE_UNAVAILABLE_SENTENCE);
	});

	test("a bare 503 keeps the existing server-error sentence", () => {
		expect(normalizeBillingError(new BillingApiError(503, "Service Unavailable"))).toBe(
			SERVER_ERROR_SENTENCE,
		);
	});

	test("a compute_unavailable 503 stays retryable", () => {
		expect(isRetryableError(computeUnavailable())).toBe(true);
	});
});
