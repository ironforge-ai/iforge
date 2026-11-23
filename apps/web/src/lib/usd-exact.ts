/**
 * The exact decimal-string USD formatter — the ONE money formatter the
 * dashboard uses for billing figures.
 *
 * It lives in `lib/` rather than `hosted/billing/` because OSS-reachable
 * surfaces need it too (the companion install price, 06-UI-SPEC S2), and the
 * OSS boundary (`hosted/oss-clean.test.ts`) forbids a static `@/hosted/*`
 * import outside `hosted/`. `hosted/billing/format.ts` re-exports it, so every
 * hosted caller keeps its import and there is still exactly one
 * implementation.
 */

const DECIMAL_USD = /^([+-]?)(\d+)(?:\.(\d+))?$/;

function incrementDecimalDigits(digits: string): string {
	const incremented = [...digits];
	for (let index = incremented.length - 1; index >= 0; index -= 1) {
		const digit = incremented[index];
		if (digit === "9") {
			incremented[index] = "0";
			continue;
		}
		incremented[index] = String.fromCharCode(digit.charCodeAt(0) + 1);
		return incremented.join("");
	}
	return `1${incremented.join("")}`;
}

/**
 * Decimal-string USD → an exactly rounded, grouped cents display without
 * converting the amount to a JavaScript number. Non-zero sub-cent values use
 * a visible floor so usage never appears as "$0.00".
 */
export function formatUsdExact(dollars: string): string {
	const match = DECIMAL_USD.exec(dollars.trim());
	if (!match) return "—";
	const [, sign, rawWhole, rawFraction] = match;
	const whole = rawWhole.replace(/^0+(?=\d)/, "");
	const fraction = rawFraction ?? "";
	const isZero = whole === "0" && !/[1-9]/.test(fraction);
	const normalizedSign = isZero || sign !== "-" ? "" : "-";
	const firstTwoFractionDigits = fraction.slice(0, 2).padEnd(2, "0");
	if (!isZero && whole === "0" && firstTwoFractionDigits === "00") {
		return `${normalizedSign}<$0.01`;
	}

	const unroundedCents = `${whole}${firstTwoFractionDigits}`;
	const roundedCents =
		(fraction[2] ?? "0") >= "5" ? incrementDecimalDigits(unroundedCents) : unroundedCents;
	const roundedWhole = roundedCents.slice(0, -2);
	const displayFraction = roundedCents.slice(-2);
	const groupedWhole = roundedWhole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
	return `${normalizedSign}$${groupedWhole}.${displayFraction}`;
}
