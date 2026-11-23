/**
 * Held-out UI-state tests for S2 — the install dialog. One assertion per
 * `06-UI-SPEC.md` §UI Considerations row for S2, plus the three APP-08 claims
 * the plan names: confirm is disabled until the price renders, the install
 * request carries the RENDERED quote's id, and a rejection renders inline
 * with no toast.
 *
 * **SCOPE LIMIT.** There is no DOM library here, so nothing is clicked in a
 * DOM. The tier cards' `onClick` handlers are reached by walking the body's
 * element tree (`choiceCards`), which proves what a click calls. The
 * dialog's popup is portaled and does not render in `renderToStaticMarkup`, so
 * the body is asserted through `InstallCompanionDialogBody` (the exact element
 * the dialog mounts inside its popup), and the submit path through
 * `runInstall` (the exact function the dialog's confirm calls). What static
 * markup cannot show — the click itself — is the only thing not exercised.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { type ComponentProps, isValidElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { EntityChoiceCard } from "@/components/entity-card";
import { ApiError, ApiNetworkError } from "@/lib/api-errors";
import { renderedText } from "@/lib/rendered-text";
import {
	type CompanionCatalogItem,
	type CompanionInstallation,
	type CompanionInstallRequest,
	type CompanionQuote,
	canConfirmInstall,
	defaultTier,
	deriveQuoteState,
	INSTALL_DIALOG_COPY,
	type IsolationTier,
	installCredentialNote,
	installPriceBody,
	type QuoteState,
	runInstall,
	TRANSPORT_FAILURE_COPY,
	tierOptions,
} from "./companions-overview.logic";
import {
	InstallCompanionDialogBody,
	type InstallCompanionDialogBodyProps,
} from "./install-companion-dialog";

const AGENT_ID = "6c3b1a2e-0000-4000-8000-000000000001";
const NOW = Date.parse("2026-09-23T12:00:00Z");
const DIGEST = `sha256:${"b".repeat(64)}`;

const CATALOG_ITEM: CompanionCatalogItem = {
	companion_key: "postiz",
	display_name: "Postiz",
	version: "v2.24.0",
	images: {
		app: { image: "ghcr.io/gitroomhq/postiz-app", tag: "v2.24.0", sha256: DIGEST },
		postgres: { image: "postgres", tag: "17", sha256: DIGEST },
		redis: { image: "redis", tag: "7", sha256: DIGEST },
		cloudflared: { image: "cloudflare/cloudflared", tag: "2026.9.0", sha256: DIGEST },
	},
	tiers: [
		{ tier: "standard", offered: true },
		{ tier: "dedicated", offered: false },
		{ tier: "shared", offered: false },
	],
	from_monthly_price_usd_micros: 12_000_000,
};

function quote(overrides: Partial<CompanionQuote> = {}): CompanionQuote {
	return {
		quote_id: "q_7f3a9c",
		companion_key: "postiz",
		isolation_tier: "standard",
		currency: "usd",
		monthly_price_usd_micros: 12_345_000,
		storage_price_usd_micros_per_gib_month: 50_000,
		expires_at: "2026-09-23T12:15:00Z",
		...overrides,
	};
}

function installation(overrides: Partial<CompanionInstallation> = {}): CompanionInstallation {
	return {
		id: "0b7e2d1c-0000-4000-8000-000000000002",
		agent_id: AGENT_ID,
		companion_key: "postiz",
		isolation_tier: "standard",
		operation: "install",
		status: "queued",
		step: null,
		error: null,
		version: "v2.24.0",
		pinned_digest: DIGEST,
		hostname: "postiz-acme.companions.iforge.dev",
		owner_email: "owner@example.com",
		credential: "stored_in_vault",
		reachability: { state: "no_hostname_yet", checked_at: null },
		created_at: "2026-09-23T12:00:00Z",
		updated_at: "2026-09-23T12:00:00Z",
		completed_at: null,
		update: {
			state: "up_to_date",
			offered_version: null,
			offered_digest: null,
			deferred_digest: null,
			last_backup_at: null,
			error_code: null,
		},
		...overrides,
	};
}

const READY: QuoteState = { status: "ready", quote: quote() };
const LOADING: QuoteState = { status: "loading" };

function renderBody(props: Partial<InstallCompanionDialogBodyProps> = {}) {
	return renderToStaticMarkup(
		<InstallCompanionDialogBody
			companionName="Postiz"
			options={tierOptions(CATALOG_ITEM)}
			tier={defaultTier(CATALOG_ITEM)}
			quote={READY}
			{...props}
		/>,
	);
}

/** The confirm `<button>` tag, located by its slot. */
function confirmButtonTag(markup: string): string {
	const tag = /<button[^>]*data-slot="install-confirm"[^>]*>/.exec(markup)?.[0];
	if (!tag) throw new Error("the confirm button did not render");
	return tag;
}

function isDisabled(tag: string): boolean {
	return / disabled=""/.test(tag);
}

/** The Isolation fieldset's markup, from its opening tag to its close. */
function tierFieldset(markup: string): string {
	const start = markup.lastIndexOf("<fieldset", markup.indexOf('data-slot="install-tier"'));
	const end = markup.indexOf("</fieldset>", start);
	if (start < 0 || end < 0) throw new Error("the Isolation fieldset did not render");
	return markup.slice(start, end);
}

/** Each tier option's markup up to its choice card's end, keyed by wire value. */
function tierBlocks(markup: string): Record<string, string> {
	const blocks: Record<string, string> = {};
	const parts = markup.split('data-slot="install-tier-option" data-tier="').slice(1);
	for (const part of parts) {
		const tier = part.slice(0, part.indexOf('"'));
		blocks[tier] = part.slice(0, part.indexOf("</button>"));
	}
	return blocks;
}

/** Every `aria-pressed` element inside the Isolation fieldset. */
function pressableTags(markup: string): string[] {
	return tierFieldset(markup).match(/<[a-z]+[^>]*aria-pressed="(?:true|false)"[^>]*>/g) ?? [];
}

/** The tiers whose choice card renders pressed. */
function pressedTiers(markup: string): string[] {
	return Object.entries(tierBlocks(markup))
		.filter(([, block]) => /<button[^>]*aria-pressed="true"/.test(block))
		.map(([tier]) => tier);
}

/**
 * The `EntityChoiceCard` elements the body builds, in render order. The body
 * uses no hooks, so calling it returns its element tree; walking that tree
 * reaches each card's `onClick` without a DOM library.
 */
function choiceCards(props: InstallCompanionDialogBodyProps) {
	const cards: ComponentProps<typeof EntityChoiceCard>[] = [];
	const walk = (node: ReactNode): void => {
		if (Array.isArray(node)) {
			for (const child of node) walk(child);
			return;
		}
		if (!isValidElement(node)) return;
		const elementProps = node.props as { children?: ReactNode };
		if (node.type === EntityChoiceCard) {
			cards.push(elementProps as ComponentProps<typeof EntityChoiceCard>);
		}
		walk(elementProps.children);
	};
	walk(InstallCompanionDialogBody(props));
	return cards;
}

function bodyProps(
	overrides: Partial<InstallCompanionDialogBodyProps> = {},
): InstallCompanionDialogBodyProps {
	return {
		companionName: "Postiz",
		options: tierOptions(CATALOG_ITEM),
		tier: defaultTier(CATALOG_ITEM),
		quote: READY,
		...overrides,
	};
}

/** The `data-quote-id` the price well rendered, i.e. the quote the tenant is looking at. */
function renderedQuoteId(markup: string): string | null {
	return /data-slot="install-price"[^>]*data-quote-id="([^"]+)"/.exec(markup)?.[1] ?? null;
}

describe("S2 install dialog — empty", () => {
	test("empty: standard is preselected on open, so the dialog never shows with no tier chosen", () => {
		expect(defaultTier(CATALOG_ITEM)).toBe("standard");
		const markup = renderBody();
		const tags = pressableTags(markup);
		expect(tags).toHaveLength(3);
		for (const tag of tags) expect(tag).toMatch(/^<button type="button"/);
		expect(tags.filter((tag) => tag.includes('aria-pressed="true"'))).toHaveLength(1);
		expect(pressedTiers(markup)).toEqual(["standard"]);
	});

	test("empty: clicking the selected card re-selects it, so one option always stays pressed", () => {
		const chosen: IsolationTier[] = [];
		const cards = choiceCards(bodyProps({ onTierChange: (tier) => chosen.push(tier) }));
		expect(cards).toHaveLength(3);
		// Every option gets a handler, disabled ones included, so each renders a button.
		for (const card of cards) expect(typeof card.onClick).toBe("function");
		const selected = cards.find((card) => card.selected);
		expect(selected?.title).toBe(INSTALL_DIALOG_COPY.tierOptions.standard.title);
		selected?.onClick?.();
		expect(chosen).toEqual(["standard"]);
		const markup = renderBody({ tier: chosen[0] });
		expect(pressedTiers(markup)).toEqual(["standard"]);
	});
});

describe("S2 install dialog — tiers", () => {
	test("dedicated and shared render natively disabled with the 'Not available yet' chip, in contract order", () => {
		const markup = renderBody();
		const blocks = tierBlocks(markup);
		const disabledButton = /<button[^>]* disabled=""[^>]*>/;
		expect(blocks.standard).not.toMatch(disabledButton);
		expect(blocks.dedicated).toMatch(disabledButton);
		expect(blocks.shared).toMatch(disabledButton);
		expect(blocks.standard).not.toContain('data-slot="status-badge"');
		expect(blocks.dedicated).toMatch(
			/data-slot="status-badge" data-status="neutral"[^>]*>Not available yet<\/span>/,
		);
		expect(blocks.shared).toMatch(
			/data-slot="status-badge" data-status="neutral"[^>]*>Not available yet<\/span>/,
		);
		expect(blocks.standard).not.toContain(INSTALL_DIALOG_COPY.unavailableNote);
		expect(blocks.dedicated).toContain(INSTALL_DIALOG_COPY.unavailableNote);
		expect(blocks.shared).toContain(INSTALL_DIALOG_COPY.unavailableNote);
		const text = renderedText(markup);
		expect(text.split(INSTALL_DIALOG_COPY.unavailableNote)).toHaveLength(3);
		const order = ["standard", "dedicated", "shared"].map((tier) =>
			markup.indexOf(`data-tier="${tier}"`),
		);
		expect(order).toEqual([...order].sort((a, b) => a - b));
		for (const tier of ["standard", "dedicated", "shared"] as const) {
			expect(text).toContain(INSTALL_DIALOG_COPY.tierOptions[tier].title);
			expect(text).toContain(INSTALL_DIALOG_COPY.tierOptions[tier].description);
		}
	});

	// 06.1 UI-REVIEW: a card-wide opacity-60 left the chip at about 2.6:1. The dimming
	// now sits on the icon and copy, so the chip keeps its full-strength tokens.
	test("a disabled tier dims its icon and copy but not its 'Not available yet' chip", () => {
		const blocks = tierBlocks(renderBody());
		for (const tier of ["dedicated", "shared"] as const) {
			const block = blocks[tier] ?? "";
			const button = block.match(/<button[^>]*>/)?.[0] ?? "";
			expect(button).toContain('disabled=""');
			expect(button).toContain("pointer-events-none");
			expect(button).not.toContain("opacity-60");
			const badgeAt = block.indexOf('data-slot="entity-choice-badge"');
			expect(badgeAt).toBeGreaterThan(-1);
			// Every element still open where the chip starts: none may carry the dimming.
			const open: string[] = [];
			for (const tag of block.slice(0, badgeAt).matchAll(/<(\/?)([a-z]+)([^>]*)>/g)) {
				if (tag[1]) open.pop();
				else if (!tag[3].endsWith("/")) open.push(tag[3]);
			}
			expect(open.length).toBeGreaterThan(0);
			for (const attrs of open) expect(attrs).not.toContain("opacity-60");
			expect(block).toMatch(/<span[^>]*class="[^"]*opacity-60[^"]*"[^>]*>[^<]*<\/span>/);
			expect(
				block.match(/<span class="min-w-0 flex-1 truncate text-sm font-medium opacity-60"/),
			).toHaveLength(1);
		}
		expect(blocks.standard).not.toContain("opacity-60");
	});

	// 06.1 UI-REVIEW: no tooltip shows the lowercase wire value; the card's own
	// title tooltip carries the human tier name.
	test("the tier's wire value travels in data-tier only, never as rendered text or a tooltip", () => {
		const markup = renderBody();
		for (const tier of ["standard", "dedicated", "shared"] as const) {
			expect(markup).toContain(`data-slot="install-tier-option" data-tier="${tier}">`);
			expect(markup).not.toContain(`title="${tier}"`);
			expect(markup).toContain(`title="${INSTALL_DIALOG_COPY.tierOptions[tier].title}"`);
			// No text node is the bare wire value; titles render sentence case.
			expect(markup).not.toContain(`>${tier}<`);
		}
		expect(renderedText(markup)).not.toMatch(/\b(standard|dedicated)\b/);
	});

	test("each option is an EntityChoiceCard with a muted IconChip and no className override", () => {
		const cards = choiceCards(bodyProps());
		expect(cards.map((card) => card.title)).toEqual(["Standard", "Dedicated", "Shared"]);
		for (const card of cards) {
			expect(card.className).toBeUndefined();
			expect(card.variant).toBeUndefined();
		}
		const fieldset = tierFieldset(renderBody());
		expect(fieldset).toContain('<div class="grid gap-2">');
		expect(
			fieldset.match(/class="[^"]*size-8 rounded-md[^"]*bg-muted text-muted-foreground"/g),
		).toHaveLength(3);
	});
});

describe("S2 install dialog — loading", () => {
	test("loading: the price well shows a Skeleton at its final height and confirm is disabled", () => {
		const markup = renderBody({ quote: LOADING });
		expect(markup).toContain('data-slot="install-price-skeleton"');
		expect(markup).toContain("h-11");
		expect(markup).toContain('aria-busy="true"');
		expect(isDisabled(confirmButtonTag(markup))).toBe(true);
		expect(renderedQuoteId(markup)).toBeNull();
		expect(renderedText(markup)).not.toContain("per month");
	});

	test("the rendered price reserves the same height the skeleton held", () => {
		expect(renderBody()).toMatch(/class="min-h-11[^"]*"[^>]*data-slot="install-price-body"/);
	});

	test("loading: after confirm the dialog holds with a Spinner and every control locked", () => {
		const markup = renderBody({ pending: true });
		const tag = confirmButtonTag(markup);
		expect(isDisabled(tag)).toBe(true);
		expect(tag).toContain('aria-busy="true"');
		const confirmIndex = markup.indexOf('data-slot="install-confirm"');
		expect(markup.slice(confirmIndex)).toContain('data-slot="spinner"');
		// The tier fieldset is disabled too: nothing changes under an in-flight install.
		expect(markup).toMatch(
			/<fieldset[^>]*disabled=""[^>]*data-slot="install-tier"|<fieldset[^>]*data-slot="install-tier"[^>]*disabled=""/,
		);
		expect(canConfirmInstall(READY, true)).toBe(false);
	});
});

describe("S2 install dialog — priced before provisioning (APP-08)", () => {
	test("confirm is enabled only once the price is on screen", () => {
		const markup = renderBody();
		expect(isDisabled(confirmButtonTag(markup))).toBe(false);
		const text = renderedText(markup);
		expect(text).toContain(installPriceBody("$12.35"));
		expect(markup).toMatch(/<span class="[^"]*tabular-nums[^"]*">\$12\.35<\/span>/);
	});

	test("a leftover quote for another tier, or an expired one, is not a price you may confirm", () => {
		const other = deriveQuoteState({
			data: quote({ isolation_tier: "dedicated" }),
			error: null,
			companionKey: "postiz",
			tier: "standard",
			now: NOW,
		});
		expect(other.status).toBe("loading");
		const expired = deriveQuoteState({
			data: quote({ expires_at: "2026-09-23T11:59:59Z" }),
			error: null,
			companionKey: "postiz",
			tier: "standard",
			now: NOW,
		});
		expect(expired.status).toBe("loading");
		expect(isDisabled(confirmButtonTag(renderBody({ quote: expired })))).toBe(true);
		const current = deriveQuoteState({
			data: quote(),
			error: null,
			companionKey: "postiz",
			tier: "standard",
			now: NOW,
		});
		expect(current.status).toBe("ready");
	});

	test("the install request carries the quote id the price well rendered", async () => {
		const shown = quote({ quote_id: "q_rendered_42", monthly_price_usd_micros: 9_990_000 });
		const state: QuoteState = { status: "ready", quote: shown };
		const markup = renderBody({ quote: state });
		const displayed = renderedQuoteId(markup);
		expect(displayed).toBe("q_rendered_42");
		expect(renderedText(markup)).toContain("$9.99 per month");

		const sent: CompanionInstallRequest[] = [];
		const outcome = await runInstall({
			agentId: AGENT_ID,
			state,
			requestInstall: async (body) => {
				sent.push(body);
				return installation();
			},
			onAccepted: () => undefined,
		});
		expect(outcome.ok).toBe(true);
		expect(sent).toHaveLength(1);
		expect(sent[0]?.quote_id).toBe(displayed as string);
		expect(sent[0]).toEqual({
			agent_id: AGENT_ID,
			companion_key: "postiz",
			isolation_tier: "standard",
			quote_id: "q_rendered_42",
		});
	});

	test("nothing is requested while no price is on screen", async () => {
		let requested = 0;
		const outcome = await runInstall({
			agentId: AGENT_ID,
			state: LOADING,
			requestInstall: async () => {
				requested += 1;
				return installation();
			},
			onAccepted: () => undefined,
		});
		expect(requested).toBe(0);
		expect(outcome.ok).toBe(false);
	});
});

describe("S2 install dialog — error", () => {
	test("a rejected install resolves to the exact contract sentence and triggers no toast", async () => {
		const toasts: string[] = [];
		const outcome = await runInstall({
			agentId: AGENT_ID,
			state: READY,
			requestInstall: async () => {
				throw new ApiError(409, "quote_expired");
			},
			onAccepted: () => {
				toasts.push(INSTALL_DIALOG_COPY.startedToast);
			},
		});
		expect(outcome).toEqual({
			ok: false,
			message:
				"The install was not started. Nothing was provisioned and you have not been charged.",
		});
		expect(toasts).toHaveLength(0);
	});

	test("an accepted install is the only path that reaches the success toast", async () => {
		const toasts: string[] = [];
		await runInstall({
			agentId: AGENT_ID,
			state: READY,
			requestInstall: async () => installation(),
			onAccepted: () => {
				toasts.push(INSTALL_DIALOG_COPY.startedToast);
			},
		});
		expect(toasts).toEqual(["Install started"]);
	});

	test("error: the rejection renders inline beneath the confirm button with the tier intact", () => {
		const markup = renderBody({ rejection: INSTALL_DIALOG_COPY.rejected });
		const confirmIndex = markup.indexOf('data-slot="install-confirm"');
		const rejectionIndex = markup.indexOf('data-slot="install-rejection"');
		expect(confirmIndex).toBeGreaterThan(-1);
		expect(rejectionIndex).toBeGreaterThan(confirmIndex);
		expect(markup).toContain('role="alert"');
		expect(renderedText(markup)).toContain(
			"The install was not started. Nothing was provisioned and you have not been charged.",
		);
		// The tier is still selected and the price still shown: nothing was reset.
		expect(pressedTiers(markup)).toEqual(["standard"]);
		expect(renderedQuoteId(markup)).toBe("q_7f3a9c");
	});

	test("the dialog module emits no error toast anywhere", () => {
		const source = readFileSync(
			fileURLToPath(new URL("./install-companion-dialog.tsx", import.meta.url)),
			"utf8",
		);
		expect(source).not.toContain("toast.error");
		expect(source).not.toContain("toastApiError");
		expect((source.match(/toast\.success\(/g) ?? []).length).toBe(1);
	});

	test("a quote that fails to load keeps confirm disabled and offers retry", () => {
		const markup = renderBody({
			quote: { status: "error", error: new ApiError(503, "companion_billing_unavailable") },
			onRetryQuote: () => undefined,
		});
		expect(isDisabled(confirmButtonTag(markup))).toBe(true);
		expect(markup).toContain('data-slot="alert"');
		expect(renderedText(markup)).toContain("Retry");
	});

	test("a quote request with no HTTP response names iforge", () => {
		const markup = renderBody({
			quote: { status: "error", error: new ApiNetworkError("offline") },
		});
		expect(renderedText(markup)).toContain(TRANSPORT_FAILURE_COPY);
	});
});

describe("S2 install dialog — partial and long text", () => {
	test("partial: one input, one selectable value — confirm is gated on the price", () => {
		const markup = renderBody();
		const enabledOptions = pressableTags(markup).filter((tag) => !/ disabled=""/.test(tag));
		expect(enabledOptions).toHaveLength(1);
		expect(markup).not.toContain("<input");
		expect(canConfirmInstall(LOADING, false)).toBe(false);
		expect(canConfirmInstall(READY, false)).toBe(true);
	});

	test("long-text: every string is an authored constant except the formatUsdExact price", () => {
		const text = renderedText(renderBody());
		const authored = [
			INSTALL_DIALOG_COPY.tierGroupLabel,
			...Object.values(INSTALL_DIALOG_COPY.tierOptions).flatMap((option) => [
				option.title,
				option.description,
			]),
			INSTALL_DIALOG_COPY.unavailableNote,
			INSTALL_DIALOG_COPY.unavailableNote,
			INSTALL_DIALOG_COPY.priceLabel,
			installPriceBody("$12.35"),
			installCredentialNote("Postiz"),
			INSTALL_DIALOG_COPY.cancel,
			INSTALL_DIALOG_COPY.confirm,
		];
		let rest = text;
		for (const string of authored) {
			expect(rest).toContain(string);
			rest = rest.replace(string, "");
		}
		expect(rest.trim()).toBe("");
	});
});

describe("S2 install dialog — populated order", () => {
	test("Isolation → You will be charged → credential note → confirm", () => {
		const markup = renderBody();
		const positions = [
			markup.indexOf('data-slot="install-tier"'),
			markup.indexOf('data-slot="install-price"'),
			markup.indexOf('data-slot="install-credential-note"'),
			markup.indexOf('data-slot="install-confirm"'),
		];
		expect(positions.every((position) => position > -1)).toBe(true);
		expect(positions).toEqual([...positions].sort((a, b) => a - b));
	});

	test("the price well is the bg-muted/30 rounded-lg p-4 inset", () => {
		expect(renderBody()).toMatch(
			/class="[^"]*rounded-lg bg-muted\/30 p-4[^"]*"[^>]*data-slot="install-price"/,
		);
	});

	test("no field or control renders a credential, and the note is fixed copy", () => {
		const markup = renderBody();
		expect(markup).not.toContain('type="password"');
		expect(renderedText(markup)).toContain(
			"Postiz's admin credential is generated for you and stored in your vault. It is never shown here and never sent to your browser.",
		);
	});
});
