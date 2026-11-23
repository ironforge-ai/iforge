// iforge (06.3 UAT): a Checkout Elements session created without customer_email never reaches
// canConfirm until the session's own email is set, so Subscribe stayed disabled after a
// complete card. The dialog fills it from the signed-in account, or asks for it.
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
	CHECKOUT_EMAIL_INVALID_MESSAGE,
	CHECKOUT_EMAIL_UNREACHABLE_MESSAGE,
	checkoutEmailToApply,
	checkoutEmailToSubmit,
	showCheckoutEmailField,
	updateCheckoutEmail,
} from "@/hosted/billing/components/stripe-checkout.logic";
import { CheckoutEmailField } from "@/hosted/billing/components/stripe-checkout-dialog";

function renderEmailField(props: {
	value: string;
	sessionEmail: string | null;
	error: string | null;
}): string {
	return renderToStaticMarkup(
		createElement(CheckoutEmailField, { ...props, onChange: () => {}, onBlur: () => {} }),
	)
		.replaceAll("&#x27;", "'")
		.replaceAll("&quot;", '"')
		.replaceAll("&amp;", "&");
}

/** The email input's own tag. */
function emailInputTag(markup: string): string {
	const match = markup.match(/<input[^>]*id="checkout-email"[^>]*>/);
	if (!match) throw new Error(`no checkout-email input in: ${markup}`);
	return match[0];
}

describe("checkout session email", () => {
	test("applies the account email when the session has none", () => {
		expect(checkoutEmailToApply(null, "owner@example.com")).toBe("owner@example.com");
	});

	test("trims the account email before applying it", () => {
		expect(checkoutEmailToApply(null, "  owner@example.com  ")).toBe("owner@example.com");
	});

	test("never overwrites an email the session already carries", () => {
		expect(checkoutEmailToApply("billing@example.com", "owner@example.com")).toBeNull();
	});

	test("applies nothing when the account has no usable email", () => {
		expect(checkoutEmailToApply(null, null)).toBeNull();
		expect(checkoutEmailToApply(null, undefined)).toBeNull();
		expect(checkoutEmailToApply(null, "   ")).toBeNull();
	});

	test("asks for an email while the session has none and none is being applied", () => {
		expect(showCheckoutEmailField({ sessionEmail: null, applying: false, ownsEmail: false })).toBe(
			true,
		);
		expect(showCheckoutEmailField({ sessionEmail: null, applying: false, ownsEmail: true })).toBe(
			true,
		);
		expect(showCheckoutEmailField({ sessionEmail: null, applying: true, ownsEmail: true })).toBe(
			false,
		);
	});

	// WR-04: Stripe accepting a typo'd address must not unmount the field.
	test("keeps the field after Stripe accepts an email this client set", () => {
		expect(
			showCheckoutEmailField({
				sessionEmail: "owner@gmial.com",
				applying: false,
				ownsEmail: true,
			}),
		).toBe(true);
	});

	test("never shows the field for a session created with its own email", () => {
		expect(
			showCheckoutEmailField({
				sessionEmail: "billing@example.com",
				applying: false,
				ownsEmail: false,
			}),
		).toBe(false);
	});
});

describe("checkout email submit (WR-04)", () => {
	test("submits a changed, trimmed draft", () => {
		expect(checkoutEmailToSubmit("  owner@gmail.com ", "owner@gmial.com")).toBe("owner@gmail.com");
		expect(checkoutEmailToSubmit("owner@example.com", null)).toBe("owner@example.com");
	});

	test("submits nothing for an empty draft or the address the session already has", () => {
		expect(checkoutEmailToSubmit("", null)).toBeNull();
		expect(checkoutEmailToSubmit("   ", "owner@example.com")).toBeNull();
		expect(checkoutEmailToSubmit(" owner@example.com ", "owner@example.com")).toBeNull();
	});
});

describe("checkout email update outcome (WR-05)", () => {
	test("is null when Stripe accepts the address", async () => {
		const sent: string[] = [];
		const message = await updateCheckoutEmail(async (email) => {
			sent.push(email);
			return { type: "success" };
		}, "owner@example.com");
		expect(message).toBeNull();
		expect(sent).toEqual(["owner@example.com"]);
	});

	test("is Stripe's own message when Stripe refuses the address", async () => {
		const message = await updateCheckoutEmail(
			async () => ({ type: "error", error: { message: "Your email address is invalid." } }),
			"owner@",
		);
		expect(message).toBe("Your email address is invalid.");
	});

	test("falls back to a generic message when Stripe's refusal has none", async () => {
		const message = await updateCheckoutEmail(
			async () => ({ type: "error", error: { message: "" } }),
			"owner@",
		);
		expect(message).toBe(CHECKOUT_EMAIL_INVALID_MESSAGE);
	});

	test("is the connection message, never a rejection, when the call fails", async () => {
		const message = await updateCheckoutEmail(async () => {
			throw new TypeError("Failed to fetch");
		}, "owner@example.com");
		expect(message).toBe(CHECKOUT_EMAIL_UNREACHABLE_MESSAGE);
	});

	test("a failed auto-apply renders its message beside the prefilled account email", async () => {
		const message = await updateCheckoutEmail(async () => {
			throw new TypeError("Failed to fetch");
		}, "owner@example.com");
		const markup = renderEmailField({
			value: "owner@example.com",
			sessionEmail: null,
			error: message,
		});
		expect(emailInputTag(markup)).toContain('value="owner@example.com"');
		expect(emailInputTag(markup)).toContain('aria-invalid="true"');
		expect(markup).toContain(`role="alert">${CHECKOUT_EMAIL_UNREACHABLE_MESSAGE}</p>`);
		expect(markup).not.toContain("This checkout still uses");
	});
});

describe("CheckoutEmailField (WR-04)", () => {
	test("shows the accepted session email in an editable input", () => {
		const input = emailInputTag(
			renderEmailField({ value: "owner@gmial.com", sessionEmail: "owner@gmial.com", error: null }),
		);
		expect(input).toContain('value="owner@gmial.com"');
		expect(input).toContain('type="email"');
		// Attribute positions only: the class list carries Tailwind `disabled:` variants.
		expect(input).not.toMatch(/\s(readonly|readOnly|disabled)(=|\s|\/?>)/);
		expect(input).not.toContain('aria-invalid="');
	});

	test("shows the error, and the address the checkout still uses, when Stripe refuses a change", () => {
		const markup = renderEmailField({
			value: "owner@",
			sessionEmail: "owner@gmial.com",
			error: "Enter a valid email address.",
		});
		expect(emailInputTag(markup)).toContain('aria-invalid="true"');
		expect(markup).toMatch(/<p[^>]*role="alert"[^>]*>Enter a valid email address\.<\/p>/);
		expect(markup).toContain("This checkout still uses owner@gmial.com.");
	});
});

describe("checkout dialog wiring", () => {
	const source = readFileSync(join(import.meta.dir, "stripe-checkout-dialog.tsx"), "utf8");

	/** The account-email auto-apply effect, from its comment to its dependency list. */
	function autoApplyEffect(): string {
		const start = source.indexOf("fill the session email from the signed-in account");
		const end = source.indexOf("}, [checkout, accountEmail]);", start);
		expect(start).toBeGreaterThanOrEqual(0);
		expect(end).toBeGreaterThan(start);
		return source.slice(start, end);
	}

	test("sets the session email through Stripe's updateEmail", () => {
		expect(source).toContain("checkoutEmailToApply(");
		expect(source).toContain(".updateEmail(");
	});

	test("reads the signed-in account's primary email", () => {
		expect(source).toContain("useCurrentUser()");
		expect(source).toContain("primaryEmailAddress");
	});

	test("renders an email field when the session still has no email", () => {
		expect(source).toContain("showCheckoutEmailField(");
		expect(source).toContain('type="email"');
	});

	// WR-04: ownership is sticky, and both update paths show the address Stripe took.
	test("marks the email as owned when the session arrives without one", () => {
		expect(source).toMatch(/if \(checkout && !sessionEmail\) setOwnsEmail\(true\)/);
		expect(source).not.toMatch(/setOwnsEmail\(false\)/);
		expect(source).toMatch(/showCheckoutEmailField\(\{[^}]*ownsEmail,?\s*\}\)/);
	});

	test("syncs the draft to the email Stripe accepted", () => {
		expect(autoApplyEffect()).toContain("setEmailDraft(email)");
		expect(source).toMatch(
			/if \(message\) setEmailError\(message\);\s*else setEmailDraft\(email\);/,
		);
	});

	// WR-05: the auto-apply surfaces Stripe's refusal or a failed call as visible text.
	test("the account-email auto-apply sets the error from the update outcome", () => {
		const effect = autoApplyEffect();
		expect(effect).toContain("updateCheckoutEmail(");
		expect(effect).toMatch(/\.then\(\(message\) => \{[^}]*setEmailError\(message\)/);
		expect(effect).not.toContain(".catch(");
	});

	// IN-02: a payer who starts typing before the account loads keeps their draft.
	test("typing in the field spends the account-email auto-apply", () => {
		const start = source.indexOf("function changeCheckoutEmail(");
		expect(start).toBeGreaterThanOrEqual(0);
		const handler = source.slice(start, source.indexOf("\n\t}\n", start));
		expect(handler).toContain("accountEmailTriedRef.current = true;");
		expect(handler).toContain("setEmailDraft(value);");
		expect(source).toContain("onChange={changeCheckoutEmail}");
		expect(source).not.toContain("onChange={setEmailDraft}");
		expect(autoApplyEffect()).toMatch(
			/if \(!checkout \|\| accountEmailTriedRef\.current\) return;/,
		);
	});

	test("no updateEmail call bypasses the outcome mapping", () => {
		const calls = source.match(/\.updateEmail\(/g) ?? [];
		const mapped =
			source.match(/updateCheckoutEmail\(\(value\) => \w+\.updateEmail\(value\)/g) ?? [];
		expect(calls.length).toBe(2);
		expect(mapped.length).toBe(calls.length);
	});
});
