import { afterEach, describe, expect, mock, test } from "bun:test";
import {
	QueryClient,
	QueryClientProvider,
	QueryObserver,
	type QueryObserverOptions,
} from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import * as billingClientModule from "@/hosted/billing/billing-client";
import type { WalletBinding } from "@/hosted/billing/contracts";
import { billingKeys } from "@/hosted/billing/query-keys";
import type { WalletCacheSnapshot } from "@/hosted/billing/wallet/wallet-cache";
import { renderedText } from "@/lib/rendered-text";

/**
 * Fork-owned pin for 06.2-UI-SPEC D1 (D-19, SC-6): browser-wallet USDC funding is
 * deferred, and the upstream card already says so.
 *
 * The hosted service answers the wallet read with the deferred quadruple
 * (`x402_enabled: false`, `x402_payment_authority: null`,
 * `x402_payment_status: "idle"`, `x402_payment_attempt: null`). UI-SPEC D1 keeps
 * `x402-card.tsx` upstream-verbatim, so this file characterizes the shipped card
 * rather than changing it: it renders the "Coming soon" card with its three
 * strings, and the wallet-binding query it registers is disabled, so a recording
 * billing client sees no `getWalletBinding` call (`GET /v2/wallet-binding`). A
 * second case with a payment in flight proves the guard is real: that wallet
 * renders the live card and the same client records the binding read. This
 * passes at RED by design; it pins behavior D1 keeps.
 *
 * The card reads its client from `useBillingClient()`, which has no provider to
 * inject through: it builds a client from the Clerk session. The test therefore
 * swaps that one export for a recording stub with `mock.module`, and the stub
 * hands every other caller the real hook, so the swap is inert outside these
 * tests even though Bun keeps module mocks for the rest of the process.
 *
 * The file lives beside the card, not in an upstream-tracked test, so an
 * upstream merge cannot drop it (03.1 D-02).
 */

const COMING_SOON = "Coming soon";
const TITLE = "USDC from a browser wallet";
const DESCRIPTION = "Browser-wallet USDC funding is not available for this account yet.";

function deferredWallet(overrides: Partial<WalletCacheSnapshot> = {}): WalletCacheSnapshot {
	return {
		balance_usd: "25.00",
		x402_enabled: false,
		x402_payment_authority: null,
		x402_payment_status: "idle",
		x402_payment_attempt: null,
		auto_reload_enabled: false,
		auto_reload_has_payment_method: false,
		auto_reload_card: null,
		auto_reload_currency: "usd",
		auto_reload_required_consent_version: "wallet_auto_reload_off_session_v2",
		auto_reload_amount_policy: "wallet_reload_configured_plus_negative_balance_v1",
		auto_reload_consent_version: null,
		auto_reload_consented_at: null,
		auto_reload_threshold_usd: "5.00",
		auto_reload_amount_cents: 2_500,
		auto_reload_monthly_cap_cents: 10_000,
		auto_reload_monthly_spent_cents: 0,
		auto_reload_period_end: "2026-11-01T00:00:00Z",
		auto_reload_status: "off",
		auto_reload_action: null,
		...overrides,
	};
}

type BillingClient = billingClientModule.BillingClient;

const realBillingClient = { ...billingClientModule };
let recordingClient: BillingClient | null = null;

mock.module("@/hosted/billing/billing-client", () => ({
	...realBillingClient,
	useBillingClient: (): BillingClient => recordingClient ?? realBillingClient.useBillingClient(),
}));

const { X402Card } = await import("./x402-card");

const cleanups: Array<() => void> = [];

afterEach(() => {
	for (const cleanup of cleanups.splice(0)) cleanup();
	recordingClient = null;
});

/** A billing client that records each wallet-binding read and answers "not bound". */
function installRecordingClient(): { walletBindingReads: () => number } {
	let reads = 0;
	const unbound: WalletBinding = { bound: false, address: null };
	recordingClient = {
		getWalletBinding: async () => {
			reads += 1;
			return unbound;
		},
	} as unknown as BillingClient;
	return { walletBindingReads: () => reads };
}

/**
 * Render the shipped card, then subscribe an observer to the wallet-binding query
 * it registered, as a mounted browser would, and report whether that query reads.
 */
function renderCard(wallet: WalletCacheSnapshot) {
	const client = installRecordingClient();
	const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
	cleanups.push(() => queryClient.clear());
	const markup = renderToStaticMarkup(
		<QueryClientProvider client={queryClient}>
			<X402Card wallet={wallet} />
		</QueryClientProvider>,
	);
	const bindingQuery = queryClient.getQueryCache().find({ queryKey: billingKeys.walletBinding });
	if (!bindingQuery) throw new Error("X402Card did not register the wallet-binding query");
	// The card registered this query through useQuery, so its stored options are observer options.
	const bindingOptions = bindingQuery.options as QueryObserverOptions;
	const observer = new QueryObserver(queryClient, { ...bindingOptions, retry: false });
	cleanups.push(observer.subscribe(() => undefined));
	return {
		text: renderedText(markup),
		bindingEnabled: bindingOptions.enabled,
		bindingFetchStatus: bindingQuery.state.fetchStatus,
		walletBindingReads: client.walletBindingReads,
	};
}

async function settle(): Promise<void> {
	await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("06.2 D1: the deferred x402 card stays upstream-verbatim", () => {
	test("the deferred quadruple renders the Coming soon card with its three strings", () => {
		const { text } = renderCard(deferredWallet());
		expect(text).toContain(COMING_SOON);
		expect(text).toContain(TITLE);
		expect(text).toContain(DESCRIPTION);
	});

	test("the deferred card never reads /v2/wallet-binding", async () => {
		const card = renderCard(deferredWallet());
		await settle();
		expect(card.bindingEnabled).toBe(false);
		expect(card.bindingFetchStatus).toBe("idle");
		expect(card.walletBindingReads()).toBe(0);
	});

	test("a payment in flight is not the deferred card, and its binding query reads", async () => {
		const card = renderCard(deferredWallet({ x402_payment_status: "processing" }));
		await settle();
		expect(card.text).not.toContain(COMING_SOON);
		expect(card.text).not.toContain(DESCRIPTION);
		expect(card.bindingEnabled).toBe(true);
		expect(card.walletBindingReads()).toBe(1);
	});
});
