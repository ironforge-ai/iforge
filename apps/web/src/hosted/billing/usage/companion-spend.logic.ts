import type { components, paths } from "@clawdi/shared/api";
import type { Client } from "openapi-fetch";
import {
	type CompanionCatalogItem,
	ISOLATION_TIER_LABELS,
	type IsolationTier,
} from "@/components/companions/companions-overview.logic";
import { formatUsdExact } from "@/hosted/billing/format";
import { unwrap } from "@/lib/api";

/**
 * S6 — Companion spend and ceiling (06-UI-SPEC §S6) — as data rather than as
 * markup.
 *
 * Every customer-facing string lives in `COMPANION_SPEND_COPY`, verbatim from
 * `06-UI-SPEC.md` §Copywriting Contract, so `companion-spend.logic.test.ts`
 * can hold each one against its contract row.
 *
 * MONEY. The wire carries integer micro-dollars (plans 06-21 and 06-22). They
 * become a display string by string arithmetic on their digits, and the
 * ceiling input becomes micros the same way, so no amount ever passes through
 * a binary fraction: 12.50 is 12500000 micros exactly, never 12499999.
 */

type Schemas = components["schemas"];

export type CompanionSpend = Schemas["CompanionSpendResponse"];
export type CompanionSpendLine = Schemas["CompanionSpendLineResponse"];
export type CompanionSpendCeiling = Schemas["CompanionSpendCeilingResponse"];

/** openapi-react-query cache keys for S6's two reads. */
export const COMPANION_SPEND_QUERY_KEY = ["get", "/v1/companions/spend"] as const;
export const COMPANION_SPEND_CEILING_QUERY_KEY = ["get", "/v1/companions/spend-ceiling"] as const;

export const COMPANION_SPEND_COPY = {
	sectionTitle: "Companion spend",
	/** `{companion}` is the catalog display name, `{tier}` the sentence-case tier. */
	rowLabel: "{companion} — {tier}",
	/** Tenant-facing dimension labels. The internal name for the third never reaches the UI. */
	compute: "Compute",
	storage: "Storage",
	aiUsage: "AI usage",
	ceilingLabel: "Monthly spend ceiling",
	ceilingHelper:
		"When this month's companion spend reaches the ceiling, your agent stops making new companion calls and no new companions are provisioned. Companions you already have keep running and your data stays reachable.",
	/** `{spent}` and `{ceiling}` are replaced by `formatUsdExact` amounts. */
	readOut: "{spent} of {ceiling} used this month",
	saveCta: "Save ceiling",
	savedToast: "Ceiling saved",
	reachedBanner:
		"Your companion spend ceiling is reached. New companion calls are being refused until the ceiling is raised or the month rolls over. Nothing has been shut down.",
	emptyTitle: "No companion spend yet",
	emptyBody: "Companion charges appear here once you install one.",
	spendLoadFailed:
		"Companion spend could not be loaded. Your ceiling is still enforced — this is a display problem, not a billing one.",
	saveRejected: "The ceiling was not saved. The old ceiling is still in force.",
	/**
	 * The tooltip on an unmeasured dimension. The UI Considerations `partial`
	 * row quotes "not yet attributed"; it ships sentence-cased because it
	 * stands alone as the tooltip's whole text.
	 */
	notAttributed: "Not yet attributed",
	/** Screen-reader label for the row skeletons (authored, not contracted). */
	loadingSpend: "Loading companion spend",
} as const;

/** The three cost dimensions, in the contract's order. */
export const SPEND_DIMENSIONS = [
	{ field: "compute_usd_micros", label: COMPANION_SPEND_COPY.compute },
	{ field: "storage_usd_micros", label: COMPANION_SPEND_COPY.storage },
	{ field: "ai_usage_usd_micros", label: COMPANION_SPEND_COPY.aiUsage },
] as const satisfies readonly { field: keyof CompanionSpendLine; label: string }[];

const MICRO_DIGITS = 6;

/**
 * Integer micros → the exact decimal dollar string, trailing zeros dropped:
 * 1234567 → "1.234567", 5000000 → "5", 0 → "0". Works on the digits of the
 * integer, never on a quotient. Anything that is not a safe integer yields
 * "", which `formatUsdExact` renders as an em dash rather than a number the
 * wire never sent.
 */
export function microsToDollarString(micros: number): string {
	if (!Number.isSafeInteger(micros)) return "";
	const sign = micros < 0 ? "-" : "";
	const digits = String(Math.abs(micros)).padStart(MICRO_DIGITS + 1, "0");
	const whole = digits.slice(0, -MICRO_DIGITS).replace(/^0+(?=\d)/, "");
	const fraction = digits.slice(-MICRO_DIGITS).replace(/0+$/, "");
	return `${sign}${whole}${fraction ? `.${fraction}` : ""}`;
}

/** Integer micros → "$12.50" through the one sanctioned money formatter. */
export function formatMicros(micros: number): string {
	return formatUsdExact(microsToDollarString(micros));
}

const CEILING_INPUT = /^(\d+)(?:\.(\d{1,6}))?$/;

/**
 * What the tenant typed → integer micros, or null when it is not a ceiling.
 *
 * Accepts plain dollars with at most six fractional digits (one micro):
 * "25" → 25000000, "12.50" → 12500000, "0" → 0 (refuse everything). Rejects
 * negatives, signs, exponents, grouping separators, a bare fraction, finer
 * than micro precision, the empty field and anything past a safe integer.
 * The digits are joined as a string and parsed as an integer once.
 */
export function parseCeilingInput(raw: string): number | null {
	const match = CEILING_INPUT.exec(raw.trim());
	if (!match) return null;
	const [, whole = "", fraction = ""] = match;
	const digits = `${whole}${fraction.padEnd(MICRO_DIGITS, "0")}`.replace(/^0+(?=\d)/, "");
	if (digits.length > String(Number.MAX_SAFE_INTEGER).length) return null;
	const micros = Number.parseInt(digits, 10);
	return Number.isSafeInteger(micros) ? micros : null;
}

/** The stored ceiling as the input's text: exact dollars, "0" for zero, "" for no ceiling. */
export function ceilingInputValue(ceiling: number | null): string {
	return ceiling === null ? "" : microsToDollarString(ceiling);
}

/**
 * "Save ceiling" is enabled only when the input parses AND differs from the
 * stored ceiling (`undefined` while the ceiling route has not answered, so
 * nothing can be compared and nothing can be saved).
 */
export function canSaveCeiling(input: string, stored: number | null | undefined): boolean {
	if (stored === undefined) return false;
	const parsed = parseCeilingInput(input);
	return parsed !== null && parsed !== stored;
}

export type DimensionCell = { text: string; attributed: boolean; tooltip: string | null };

/**
 * One cost figure. Null means nothing was recorded for it, which is not the
 * same fact as a recorded zero: it renders as an em dash with the
 * not-yet-attributed tooltip, never as "$0.00" (T-06-28-03).
 */
export function dimensionCell(micros: number | null): DimensionCell {
	if (micros === null) {
		return { text: "—", attributed: false, tooltip: COMPANION_SPEND_COPY.notAttributed };
	}
	return { text: formatMicros(micros), attributed: true, tooltip: null };
}

/** "{spent} of {ceiling} used this month", both amounts formatted; a 0 ceiling reads $0.00. */
export function ceilingReadOut(spent: number, ceiling: number): string {
	return COMPANION_SPEND_COPY.readOut
		.replace("{spent}", () => formatMicros(spent))
		.replace("{ceiling}", () => formatMicros(ceiling));
}

/** "{companion} — {tier}", with the sentence-case tier the install dialog shows. */
export function rowLabel(companion: string, tier: IsolationTier): string {
	// Function replacers, so a catalog name is inserted literally (no `$&` expansion).
	return COMPANION_SPEND_COPY.rowLabel
		.replace("{tier}", () => ISOLATION_TIER_LABELS[tier])
		.replace("{companion}", () => companion);
}

/** The catalog's display name for a companion key, or the key itself when the catalog has none. */
export function companionDisplayName(
	companionKey: string,
	catalog: readonly Pick<CompanionCatalogItem, "companion_key" | "display_name">[] | undefined,
): string {
	return catalog?.find((item) => item.companion_key === companionKey)?.display_name ?? companionKey;
}

export type SaveOutcome = { ok: true } | { ok: false; message: string };

/**
 * What "Save ceiling" runs. Input that does not parse sends nothing. A
 * rejected request resolves to the inline sentence and nothing else, so a
 * failure can never reach a toast; `onSaved` (the toast and the refetch) runs
 * only for an accepted ceiling.
 */
export async function runSaveCeiling({
	input,
	requestSave,
	onSaved,
}: {
	input: string;
	requestSave: (micros: number) => Promise<CompanionSpendCeiling>;
	onSaved: (saved: CompanionSpendCeiling) => unknown;
}): Promise<SaveOutcome> {
	const micros = parseCeilingInput(input);
	if (micros === null) return { ok: false, message: COMPANION_SPEND_COPY.saveRejected };
	let saved: CompanionSpendCeiling;
	try {
		saved = await requestSave(micros);
	} catch {
		return { ok: false, message: COMPANION_SPEND_COPY.saveRejected };
	}
	await onSaved(saved);
	return { ok: true };
}

/**
 * S6's one write over the generated client. The body is exactly the ceiling;
 * the tenant is always the caller server-side (plan 06-22), so the section
 * names none.
 */
export function companionSpendRequests(api: Client<paths>) {
	return {
		saveCeiling: async (micros: number): Promise<CompanionSpendCeiling> =>
			unwrap(
				await api.PUT("/v1/companions/spend-ceiling", {
					body: { ceiling_usd_micros: micros },
				}),
			),
	};
}
