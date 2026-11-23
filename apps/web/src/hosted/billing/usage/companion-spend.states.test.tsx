/**
 * S6 — Companion spend and ceiling (06-UI-SPEC §S6) over plans 06-21 and
 * 06-22's `/v1/companions/spend` and `/v1/companions/spend-ceiling` routes.
 *
 * **SCOPE LIMIT.** There is no DOM library here, so nothing is clicked. Every
 * state is rendered through `CompanionSpendSectionView` (the exact element
 * `CompanionSpendSection` mounts inside the Usage panel), and the save through
 * `runSaveCeiling` (the exact function "Save ceiling" calls), driven by a REAL
 * openapi-fetch client over a fake `fetch`, so the method, path and body the
 * section sends are the ones asserted. The click itself is the only thing not
 * exercised.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { paths } from "@clawdi/shared/api";
import createClient from "openapi-fetch";
import { renderToStaticMarkup } from "react-dom/server";
import type { CompanionCatalogItem } from "@/components/companions/companions-overview.logic";
import { renderedText } from "@/lib/rendered-text";
import {
	COMPANION_SPEND_COPY,
	type CompanionSpend,
	type CompanionSpendCeiling,
	type CompanionSpendLine,
	companionSpendRequests,
	runSaveCeiling,
} from "./companion-spend.logic";
import {
	CompanionSpendSectionView,
	type CompanionSpendSectionViewProps,
} from "./companion-spend-section";

const INSTALLATION_ID = "0f0e0d0c-0000-4000-8000-000000000001";
const AGENT_ID = "1a1b1c1d-0000-4000-8000-000000000002";

function image(name: string) {
	return { image: `ghcr.io/example/${name}`, tag: "1.0.0", sha256: `sha256:${"a".repeat(64)}` };
}

function catalogItem(overrides: Partial<CompanionCatalogItem> = {}): CompanionCatalogItem {
	return {
		companion_key: "postiz",
		display_name: "Postiz",
		version: "1.0.0",
		images: {
			app: image("postiz"),
			postgres: image("postgres"),
			redis: image("redis"),
			cloudflared: image("cloudflared"),
		},
		tiers: [
			{ tier: "standard", offered: true },
			{ tier: "dedicated", offered: false },
			{ tier: "shared", offered: false },
		],
		from_monthly_price_usd_micros: 12_000_000,
		...overrides,
	};
}

function spendLine(overrides: Partial<CompanionSpendLine> = {}): CompanionSpendLine {
	return {
		installation_id: INSTALLATION_ID,
		agent_id: AGENT_ID,
		companion_key: "postiz",
		isolation_tier: "standard",
		total_usd_micros: 3_450_000,
		compute_usd_micros: 3_000_000,
		storage_usd_micros: 450_000,
		ai_usage_usd_micros: null,
		...overrides,
	};
}

function spend(companions: CompanionSpendLine[] = [spendLine()]): CompanionSpend {
	return {
		month: "2026-09",
		currency: "usd",
		companions,
		nested_llm_usd_micros: null,
		total_usd_micros: companions.length ? 3_450_000 : null,
	};
}

function ceiling(overrides: Partial<CompanionSpendCeiling> = {}): CompanionSpendCeiling {
	return {
		ceiling_usd_micros: null,
		source: "none",
		spent_usd_micros: 0,
		reached: false,
		month: "2026-09",
		...overrides,
	};
}

function viewProps(
	overrides: Partial<CompanionSpendSectionViewProps> = {},
): CompanionSpendSectionViewProps {
	return {
		ceiling: ceiling(),
		ceilingLoading: false,
		ceilingError: null,
		onRetryCeiling: () => undefined,
		ceilingInput: "",
		onCeilingInputChange: () => undefined,
		onSaveCeiling: () => undefined,
		savePending: false,
		saveError: null,
		spend: spend(),
		spendLoading: false,
		spendError: null,
		onRetrySpend: () => undefined,
		catalog: [catalogItem()],
		expanded: new Set<string>(),
		onToggleRow: () => undefined,
		...overrides,
	};
}

function render(overrides: Partial<CompanionSpendSectionViewProps> = {}): string {
	return renderToStaticMarkup(<CompanionSpendSectionView {...viewProps(overrides)} />);
}

/** The opening tag and content of the `<button>` whose text contains `label`, or "". */
function buttonMarkup(markup: string, label: string): string {
	const at = markup.indexOf(`>${label}<`);
	if (at === -1) return "";
	const start = markup.lastIndexOf("<button", at);
	if (start === -1) return "";
	return markup.slice(start, markup.indexOf("</button>", at) + "</button>".length);
}

function isDisabledButton(markup: string, label: string): boolean {
	const button = buttonMarkup(markup, label);
	if (!button) throw new Error(`no <button> labelled "${label}" in the rendered section`);
	const openTag = button.slice(0, button.indexOf(">") + 1);
	return /\sdisabled=""/.test(openTag);
}

type Captured = { method: string; url: string; body: string };

/** A real generated client whose transport records each request and answers from `respond`. */
function fakeApi(respond: (request: Captured) => { status: number; body: unknown }) {
	const captured: Captured[] = [];
	const client = createClient<paths>({
		baseUrl: "http://api.test",
		fetch: async (input: Request) => {
			const request = { method: input.method, url: input.url, body: await input.text() };
			captured.push(request);
			const { status, body } = respond(request);
			return new Response(JSON.stringify(body), {
				status,
				headers: { "content-type": "application/json" },
			});
		},
	});
	return { client, captured };
}

describe("S6 tracer", () => {
	test("tracer: a tenant sets a monthly ceiling and reads companion spend in Settings → Usage", async () => {
		// 1. No ceiling yet, one Postiz standard installation this month.
		const before = render({ ceiling: ceiling(), ceilingInput: "" });
		const beforeText = renderedText(before);
		// Hosted-only: the section root carries the OSS-boundary marker.
		expect(before).toContain('data-hosted="true"');
		expect(beforeText).toContain("Companion spend");
		expect(beforeText).toContain("Monthly spend ceiling");
		expect(beforeText).toContain("Postiz — Standard");
		expect(beforeText).toContain("$3.45");
		expect(beforeText).not.toContain("used this month");
		expect(isDisabledButton(before, "Save ceiling")).toBe(true);

		// 2. The tenant types 25: the value differs from the stored one, so Save enables.
		const typed = render({ ceiling: ceiling(), ceilingInput: "25" });
		expect(isDisabledButton(typed, "Save ceiling")).toBe(false);

		// 3. Save: one PUT with integer micros, then the toast and the refetch.
		const saved = ceiling({
			ceiling_usd_micros: 25_000_000,
			source: "tenant",
			spent_usd_micros: 1_200_000,
		});
		const { client, captured } = fakeApi(({ method }) =>
			method === "PUT" ? { status: 200, body: saved } : { status: 404, body: { detail: "no" } },
		);
		const toasts: string[] = [];
		let refetched = 0;
		const outcome = await runSaveCeiling({
			input: "25",
			requestSave: companionSpendRequests(client).saveCeiling,
			onSaved: async () => {
				toasts.push(COMPANION_SPEND_COPY.savedToast);
				refetched += 1;
			},
		});
		expect(outcome).toEqual({ ok: true });
		expect(captured.map(({ method, url }) => ({ method, url }))).toEqual([
			{ method: "PUT", url: "http://api.test/v1/companions/spend-ceiling" },
		]);
		expect(JSON.parse(captured[0]?.body ?? "null")).toEqual({ ceiling_usd_micros: 25_000_000 });
		expect(toasts).toEqual(["Ceiling saved"]);
		expect(refetched).toBe(1);

		// 4. After the refetch: the read-out, and Save is disabled again (nothing changed).
		const after = render({ ceiling: saved, ceilingInput: "25" });
		expect(renderedText(after)).toContain("$1.20 of $25.00 used this month");
		expect(isDisabledButton(after, "Save ceiling")).toBe(true);

		// 5. The Usage panel mounts the section.
		const usagePage = readFileSync(
			fileURLToPath(new URL("./usage-page.tsx", import.meta.url)),
			"utf8",
		);
		expect(usagePage).toContain("<CompanionSpendSection");
	});
});

/**
 * The whole element (opening tag to its BALANCED closing tag) that carries
 * `attribute`, e.g. `data-slot="ceiling-control"`. Nested elements of the same
 * tag are counted, so a `<div>` slot with `<div>` children is returned whole.
 */
function element(markup: string, attribute: string, tag = "div"): string {
	const at = markup.indexOf(attribute);
	if (at === -1) return "";
	const start = markup.lastIndexOf(`<${tag}`, at);
	const tokens = new RegExp(`<${tag}[\\s>]|</${tag}>`, "g");
	tokens.lastIndex = start;
	let depth = 0;
	for (let match = tokens.exec(markup); match; match = tokens.exec(markup)) {
		depth += match[0].startsWith("</") ? -1 : 1;
		if (depth === 0) return markup.slice(start, match.index + match[0].length);
	}
	return "";
}

function slot(markup: string, name: string, tag = "div"): string {
	return element(markup, `data-slot="${name}"`, tag);
}

/** Every `<span data-slot="spend-amount|spend-unattributed" …>` opening tag. */
function amountTags(markup: string): string[] {
	return [...markup.matchAll(/<span[^>]*data-slot="spend-(?:amount|unattributed)"[^>]*>/g)].map(
		(match) => match[0],
	);
}

/** Each `<button …>` opening tag. */
function buttonTags(markup: string): string[] {
	return [...markup.matchAll(/<button[^>]*>/g)].map((match) => match[0]);
}

const TWO_MORE_LINES = [
	spendLine({
		installation_id: "0f0e0d0c-0000-4000-8000-000000000002",
		isolation_tier: "dedicated",
	}),
	spendLine({ installation_id: "0f0e0d0c-0000-4000-8000-000000000003", total_usd_micros: 0 }),
];

describe("S6 UI Considerations", () => {
	test("empty: no companions installed renders the inset empty state, and the ceiling control still renders", () => {
		const markup = render({ spend: spend([]) });
		const text = renderedText(markup);
		expect(text).toContain("No companion spend yet");
		expect(text).toContain("Companion charges appear here once you install one.");
		expect(markup).not.toContain('data-slot="companion-spend-list"');
		// A ceiling is settable before the first companion exists.
		expect(text).toContain("Monthly spend ceiling");
		const typed = render({ spend: spend([]), ceilingInput: "10" });
		expect(isDisabledButton(typed, "Save ceiling")).toBe(false);
	});

	test("loading: row skeletons render while spend loads, and the ceiling control is already interactive and saves", async () => {
		const markup = render({
			spend: undefined,
			spendLoading: true,
			ceiling: ceiling({ ceiling_usd_micros: 25_000_000, source: "tenant" }),
			ceilingInput: "30",
		});
		const loading = slot(markup, "companion-spend-loading");
		expect(loading).toContain('role="status"');
		expect([...loading.matchAll(/data-slot="skeleton"/g)].length).toBeGreaterThanOrEqual(2);
		expect(renderedText(loading)).toBe(COMPANION_SPEND_COPY.loadingSpend);
		// The control reads the ceiling route only: enabled input, enabled Save.
		const input = markup.match(/<input[^>]*>/)?.[0] ?? "";
		expect(input).not.toMatch(/\sdisabled=""/);
		expect(input).toContain('value="30"');
		expect(isDisabledButton(markup, "Save ceiling")).toBe(false);
		// And saving touches only the ceiling route, never the spend route.
		const { client, captured } = fakeApi(() => ({
			status: 200,
			body: ceiling({ ceiling_usd_micros: 30_000_000, source: "tenant" }),
		}));
		const outcome = await runSaveCeiling({
			input: "30",
			requestSave: companionSpendRequests(client).saveCeiling,
			onSaved: () => undefined,
		});
		expect(outcome).toEqual({ ok: true });
		expect(captured.map(({ method, url }) => `${method} ${url}`)).toEqual([
			"PUT http://api.test/v1/companions/spend-ceiling",
		]);
	});

	test("loading: while the ceiling route has not answered, the input and Save are disabled", () => {
		const markup = render({ ceiling: undefined, ceilingLoading: true, ceilingInput: "" });
		expect(markup.match(/<input[^>]*>/)?.[0] ?? "").toContain('disabled=""');
		expect(isDisabledButton(markup, "Save ceiling")).toBe(true);
	});

	test("error: a failed spend load renders the contract sentence and never hides the ceiling", () => {
		const markup = render({
			spend: undefined,
			spendError: new Error("boom"),
			ceiling: ceiling({ ceiling_usd_micros: 25_000_000, spent_usd_micros: 1_200_000 }),
			ceilingInput: "25",
		});
		const text = renderedText(markup);
		expect(text).toContain(
			"Companion spend could not be loaded. Your ceiling is still enforced — this is a display problem, not a billing one.",
		);
		expect(text).toContain("Monthly spend ceiling");
		expect(text).toContain("$1.20 of $25.00 used this month");
		expect(buttonMarkup(markup, "Save ceiling")).not.toBe("");
		expect(markup).not.toContain('data-slot="companion-spend-list"');
	});

	test("error: a rejected save renders the inline sentence beneath the control", async () => {
		const { client } = fakeApi(() => ({
			status: 422,
			body: { detail: [{ msg: "Input should be greater than or equal to 0" }] },
		}));
		const toasts: string[] = [];
		const outcome = await runSaveCeiling({
			input: "25",
			requestSave: companionSpendRequests(client).saveCeiling,
			onSaved: () => {
				toasts.push(COMPANION_SPEND_COPY.savedToast);
			},
		});
		expect(outcome).toEqual({
			ok: false,
			message: "The ceiling was not saved. The old ceiling is still in force.",
		});
		expect(toasts).toEqual([]);

		const markup = render({ ceilingInput: "25", saveError: COMPANION_SPEND_COPY.saveRejected });
		const control = slot(markup, "ceiling-control");
		const errorAt = control.indexOf(COMPANION_SPEND_COPY.saveRejected);
		expect(errorAt).toBeGreaterThan(control.indexOf(">Save ceiling<"));
		expect(control.slice(control.lastIndexOf("<p", errorAt), errorAt)).toContain('role="alert"');
	});

	test("populated: Alert when reached, then the control, then the read-out, then the rows", () => {
		const markup = render({
			ceiling: ceiling({
				ceiling_usd_micros: 1_000_000,
				source: "tenant",
				spent_usd_micros: 3_450_000,
				reached: true,
			}),
			ceilingInput: "1",
		});
		const order = [
			markup.indexOf("data-ceiling-reached"),
			markup.indexOf('data-slot="ceiling-control"'),
			markup.indexOf('data-slot="ceiling-read-out"'),
			markup.indexOf('data-slot="companion-spend-list"'),
		];
		expect(order.every((index) => index !== -1)).toBe(true);
		expect([...order].sort((a, b) => a - b)).toEqual(order);
		expect(renderedText(markup)).toContain("$3.45 of $1.00 used this month");
	});

	test("populated: line items are list rows, not cards", () => {
		const markup = render({ spend: spend([spendLine(), ...TWO_MORE_LINES]) });
		const list = slot(markup, "companion-spend-list", "ul");
		expect([...list.matchAll(/<li[^>]*data-slot="companion-spend-row"/g)].length).toBe(3);
		expect(list).not.toContain('data-slot="card"');
		expect(list).not.toMatch(/rounded-lg[^"]*p-4|p-4[^"]*rounded-lg/);
		expect(renderedText(list)).toContain("Postiz — Dedicated");
	});

	test("populated: the disclosure reveals Compute, Storage and AI usage", () => {
		const collapsed = render();
		const toggle = buttonTags(collapsed).find((tag) => tag.includes("aria-expanded")) ?? "";
		expect(toggle).toContain('aria-expanded="false"');
		expect(collapsed).not.toContain('data-slot="companion-spend-dimensions"');

		const open = render({ expanded: new Set([INSTALLATION_ID]) });
		const openToggle = buttonTags(open).find((tag) => tag.includes("aria-expanded")) ?? "";
		expect(openToggle).toContain('aria-expanded="true"');
		const controls = openToggle.match(/aria-controls="([^"]+)"/)?.[1] ?? "missing";
		const dimensions = slot(open, "companion-spend-dimensions", "dl");
		expect(dimensions).toContain(`id="${controls}"`);
		const labels = [...dimensions.matchAll(/<dt[^>]*>([^<]*)<\/dt>/g)].map((match) => match[1]);
		expect(labels).toEqual(["Compute", "Storage", "AI usage"]);
		for (const tag of amountTags(dimensions)) expect(tag).toContain("tabular-nums");
	});

	test("populated: Save ceiling is the section's only accent button", () => {
		const markup = render({
			spend: spend([spendLine(), ...TWO_MORE_LINES]),
			ceilingInput: "25",
			expanded: new Set([INSTALLATION_ID]),
		});
		const accent = buttonTags(markup).filter((tag) => /\bbg-primary\b/.test(tag));
		expect(accent.length).toBe(1);
		expect(buttonMarkup(markup, "Save ceiling")).toContain("bg-primary");
	});

	test("partial: an unmeasured dimension is an em dash with the not-yet-attributed tooltip, never $0.00", () => {
		const markup = render({ expanded: new Set([INSTALLATION_ID]) });
		const dimensions = slot(markup, "companion-spend-dimensions", "dl");
		const aiUsage = dimensions.slice(dimensions.indexOf(">AI usage<") + 1);
		expect(renderedText(aiUsage).startsWith("AI usage — Not yet attributed")).toBe(true);
		expect(aiUsage).toContain('data-slot="spend-unattributed"');
		expect(renderedText(dimensions)).not.toContain("$0.00");
		// The row total stays the authoritative charge.
		expect(renderedText(slot(markup, "companion-spend-list", "ul"))).toContain("$3.45");
	});

	test("partial: a measured zero is a fact and renders as $0.00", () => {
		const markup = render({
			spend: spend([spendLine({ storage_usd_micros: 0 })]),
			expanded: new Set([INSTALLATION_ID]),
		});
		const dimensions = slot(markup, "companion-spend-dimensions", "dl");
		expect(renderedText(dimensions)).toContain("Storage $0.00");
	});

	test("overflow: the section introduces no second scroll region, and money stays right-aligned", () => {
		const markup = render({
			spend: spend([spendLine(), ...TWO_MORE_LINES]),
			expanded: new Set([INSTALLATION_ID]),
		});
		expect(markup).not.toMatch(/overflow-(?:y-|x-)?(?:auto|scroll)|max-h-/);
		const tags = amountTags(markup);
		expect(tags.length).toBe(6);
		for (const tag of tags) {
			expect(tag).toContain("tabular-nums");
			expect(tag).toContain("text-right");
		}
	});

	test("zero-one-many: a ceiling of 0 renders as 0, not blank", () => {
		const markup = render({
			ceiling: ceiling({ ceiling_usd_micros: 0, source: "tenant", spent_usd_micros: 0 }),
			ceilingInput: "0",
		});
		expect(markup.match(/<input[^>]*>/)?.[0] ?? "").toContain('value="0"');
		expect(renderedText(markup)).toContain("$0.00 of $0.00 used this month");
	});

	test("zero-one-many: the read-out and helper read identically for one and many companions", () => {
		const standing = ceiling({ ceiling_usd_micros: 25_000_000, spent_usd_micros: 1_200_000 });
		const one = render({ ceiling: standing, spend: spend([spendLine()]) });
		const many = render({ ceiling: standing, spend: spend([spendLine(), ...TWO_MORE_LINES]) });
		expect(slot(one, "ceiling-read-out", "p")).toBe(slot(many, "ceiling-read-out", "p"));
		expect(slot(one, "ceiling-control")).toBe(slot(many, "ceiling-control"));
	});

	test("long-text: money is tabular-nums and never wraps; labels truncate instead", () => {
		const markup = render({
			spend: spend([spendLine({ total_usd_micros: 123_456_789_000_000 })]),
			expanded: new Set([INSTALLATION_ID]),
		});
		expect(renderedText(markup)).toContain("$123,456,789.00");
		for (const tag of amountTags(markup)) {
			expect(tag).toContain("tabular-nums");
			expect(tag).toContain("whitespace-nowrap");
			expect(tag).not.toMatch(/break-all|break-words|whitespace-normal/);
		}
		expect(markup).toMatch(/<span class="truncate" title="standard">Postiz — Standard<\/span>/);
	});

	test("reached: the ceiling-reached Alert is warning-toned, never destructive", () => {
		const markup = render({
			ceiling: ceiling({ ceiling_usd_micros: 0, source: "tenant", reached: true }),
			ceilingInput: "0",
		});
		const alert = markup.match(/<div[^>]*data-slot="alert"[^>]*>/)?.[0] ?? "";
		expect(alert).toContain("data-ceiling-reached");
		expect(alert).toContain('data-variant="warning"');
		expect(alert).toContain("bg-warning-muted");
		expect(alert).not.toMatch(/destructive/);
		expect(renderedText(element(markup, "data-ceiling-reached"))).toBe(
			"Your companion spend ceiling is reached. New companion calls are being refused until the ceiling is raised or the month rolls over. Nothing has been shut down.",
		);
	});

	test("the internal term for nested LLM spend never reaches the rendered section", () => {
		const markup = render({ expanded: new Set([INSTALLATION_ID]) });
		expect(markup.toLowerCase()).not.toContain("nested");
		expect(markup).not.toContain("LLM");
	});
});
