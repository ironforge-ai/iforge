import type { WalletTransaction } from "@/hosted/billing/contracts";
import { formatUsdExact } from "@/hosted/billing/format";
import { computeTierLabel } from "@/hosted/billing/subscription/subscription-utils";
import { formatShortDate } from "@/lib/format";

const KIND_LABELS: Record<string, string> = {
	topup: "Top-up",
	auto_reload: "Auto-reload",
	x402: "USDC top-up",
	grant_signup: "Grant",
	admin_adjust: "Adjustment",
	refund: "Refund",
	compute_charge: "Compute charge",
	compute_credit: "Compute credit",
	companion_charge: "Companion charge",
	companion_credit: "Companion credit",
};

const PAYMENT_SOURCE_LABELS: Record<string, string> = {
	wallet: "Wallet",
	card: "Card",
	usdc: "USDC",
	unknown: "Unknown",
	multiple: "Multiple methods",
	us_bank_account: "US bank account",
	sepa_debit: "SEPA Direct Debit",
	bacs_debit: "Bacs Direct Debit",
	acss_debit: "ACSS Direct Debit",
	au_becs_debit: "BECS Direct Debit",
	customer_balance: "Bank transfer",
	paypal: "PayPal",
	cashapp: "Cash App",
	ideal: "iDEAL",
	alipay: "Alipay",
	wechat_pay: "WeChat Pay",
};

export function transactionPaymentSourceLabel(source: WalletTransaction["funding"]): string {
	if (source === null) return "—";
	return (
		PAYMENT_SOURCE_LABELS[source] ??
		source.replaceAll("_", " ").replace(/\b[a-z]/g, (letter) => letter.toUpperCase())
	);
}

export function transactionKindLabel(kind: string): string {
	return KIND_LABELS[kind] ?? "Other transaction";
}

export function transactionSignedAmount(transaction: WalletTransaction): string {
	const amount = formatUsdExact(transaction.amount.trim().replace(/^[+-]/, ""));
	return amount === "—" ? amount : `${transaction.direction === "credit" ? "+" : "−"}${amount}`;
}

export function transactionComputeDetails(transaction: WalletTransaction): string[] {
	if (!transaction.kind.startsWith("compute_")) return [];
	const context = transaction.context;
	if (!context) return ["Compute", "—"];
	const tier =
		context.plan === "compute_basic" || context.plan === "compute_performance"
			? computeTierLabel(context.plan)
			: null;
	const period =
		context.period_start || context.period_end
			? `${formatShortDate(context.period_start)} – ${formatShortDate(context.period_end)}`
			: "—";
	// No deployment on the charge means none was recorded (e.g. a subscription
	// not yet assigned to an agent), not that one was deleted: name only the plan.
	if (!context.deployment_id) return [tier ? `${tier} compute` : "Compute", period];
	const agent = context.agent_name || "Deleted agent";
	return [`${tier ?? "Compute"} · ${agent}`, period];
}
