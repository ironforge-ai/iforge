import { describe, expect, test } from "bun:test";
import type { WalletTransaction } from "@/hosted/billing/contracts";
import { transactionComputeDetails, transactionKindLabel } from "./transactions-section.logic";

/**
 * Fork-owned pin for 06.2-UI-SPEC H4 (D-11, D-18): wallet-funded companion
 * movements read as companion charges and credits in Transactions.
 *
 * When the compute subscription is wallet-funded, the hosted service debits each
 * companion period as `companion_charge` and credits a stop's proration as
 * `companion_credit`, both with `context: null`. Upstream's `KIND_LABELS` knows
 * neither kind, so both would render "Other transaction". This file lives beside
 * the logic, not in the upstream-tracked `transactions-section.logic.test.ts`, so
 * an upstream merge cannot drop it (03.1 D-02).
 */

function companionTransaction(overrides: Partial<WalletTransaction> = {}): WalletTransaction {
	return {
		id: "wallet:companion",
		kind: "companion_charge",
		occurred_at: "2026-10-01T00:00:00Z",
		amount: "9.00",
		currency: "usd",
		direction: "debit",
		status: "applied",
		funding: "wallet",
		context: null,
		...overrides,
	};
}

describe("06.2 H4: companion transaction kinds have readable labels", () => {
	test("companion_charge reads as Companion charge", () => {
		expect(transactionKindLabel("companion_charge")).toBe("Companion charge");
	});

	test("companion_credit reads as Companion credit", () => {
		expect(transactionKindLabel("companion_credit")).toBe("Companion credit");
	});

	test("an unknown kind still reads as Other transaction", () => {
		expect(transactionKindLabel("internal_migration_v3")).toBe("Other transaction");
	});

	test("a companion debit with no context yields no compute detail lines", () => {
		expect(transactionComputeDetails(companionTransaction())).toEqual([]);
		expect(
			transactionComputeDetails(
				companionTransaction({ kind: "companion_credit", direction: "credit" }),
			),
		).toEqual([]);
	});
});
