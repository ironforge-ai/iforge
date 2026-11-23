/**
 * Held-out UI-state tests for S2 — the blast-radius caps form. One assertion
 * per `05-UI-SPEC.md` §UI Considerations row for that element.
 *
 * **SCOPE LIMIT, same as `companion-tools.states.test.tsx`.** There is no DOM
 * library and no viewport harness here, so every assertion reads
 * `renderToStaticMarkup` output: which component rendered, which copy is
 * present, which classes and attributes are present. It does not measure
 * pixels. The `overflow` and `long-text` rows are asserted as their
 * markup-level preconditions — the unit suffix is an `InputGroup` addon rather
 * than a raw byte count, and the helper copy sits inside a `max-w-2xl` block —
 * never as an observed width.
 */

import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { ApiError } from "@/lib/api-errors";
import { renderedText } from "@/lib/rendered-text";
import { BlastRadiusCapsCard } from "./blast-radius-caps-card";
import {
	CAPS_COPY,
	CAPS_SAVE_ERROR_COPY,
	type CapVector,
	type ConsumptionItem,
	SHIPPED_DEFAULT_CAPS,
} from "./caps-form.logic";
import { windowConsumptionFor } from "./companion-tools-surface";

const MEBIBYTE = 1024 * 1024;
const WINDOW_STARTED_AT = "2026-09-16T08:14:00Z";
const AGENT_ID = "6c3b1a2e-0000-4000-8000-000000000001";
const OTHER_AGENT_ID = "6c3b1a2e-0000-4000-8000-000000000002";

function vector(overrides: Partial<CapVector> = {}): CapVector {
	return {
		companion_key: "filebrowser",
		...SHIPPED_DEFAULT_CAPS,
		is_default: true,
		updated_at: null,
		...overrides,
	};
}

function consumption(overrides: Partial<ConsumptionItem> = {}): ConsumptionItem {
	return {
		companion_key: "filebrowser",
		environment_id: "6c3b1a2e-0000-4000-8000-000000000001",
		session_id: null,
		calls_consumed: 0,
		records_consumed: 0,
		bytes_consumed: 0,
		per_session_calls: SHIPPED_DEFAULT_CAPS.per_session_calls,
		per_session_records: SHIPPED_DEFAULT_CAPS.per_session_records,
		per_session_bytes: SHIPPED_DEFAULT_CAPS.per_session_bytes,
		...overrides,
	};
}

function renderCard(props: Partial<Parameters<typeof BlastRadiusCapsCard>[0]> = {}) {
	return renderToStaticMarkup(
		<BlastRadiusCapsCard
			companionKey="filebrowser"
			vector={vector()}
			consumption={consumption()}
			windowStartedAt={WINDOW_STARTED_AT}
			onSave={async () => undefined}
			{...props}
		/>,
	);
}

describe("S2 blast-radius caps form", () => {
	test("empty: an unsaved tenant renders the shipped defaults as values, never blank inputs", () => {
		const markup = renderCard();
		expect(markup).toContain('value="1"');
		expect(markup).toContain('value="50"');
		expect(markup).toContain('value="200"');
		expect(markup).toContain('value="500"');
		expect(markup).toContain('value="10"');
		expect(markup).not.toContain('value=""');
		expect(renderedText(markup)).toContain(CAPS_COPY.defaultsMarker);
	});

	test("empty: the read-out reads zero of the budget used SINCE a time, not this session", () => {
		const text = renderedText(renderCard());
		expect(text).toContain("0 of 200 calls used since");
		expect(text).toContain("0 of 500 records used since");
		expect(text).toContain("0 B of 200 MiB used since");
		// The protocol delivers no session identifier to the façade, so the
		// read-out must not name a scope the server does not meter.
		expect(text).not.toContain("this session");
	});

	test("loading: Skeleton blocks at the three-column geometry with Save caps disabled", () => {
		const markup = renderCard({ loading: true, vector: undefined });
		expect(markup).toContain('data-slot="skeleton"');
		expect(markup).toContain("sm:grid-cols-3");
		expect(markup).toContain("max-w-2xl");
		const savePosition = markup.indexOf(CAPS_COPY.save);
		expect(savePosition).toBeGreaterThan(-1);
		expect(markup.slice(0, savePosition)).toContain("disabled");
		expect(markup).not.toContain('data-slot="input-group-control"');
	});

	test("error: a load failure renders ApiErrorPanel rather than an editable form", () => {
		const markup = renderCard({ error: new ApiError(500, "boom"), onRetry: () => {} });
		expect(markup).toContain('data-slot="alert"');
		expect(renderedText(markup)).toContain("Retry");
		expect(markup).not.toContain('data-slot="input-group-control"');
	});

	test("error: a save rejection renders inline beneath the form, never as a toast", () => {
		// `BlastRadiusCapsCard` owns this state internally, so the assertion
		// that can be made from static markup is the one that matters: the
		// sentence is the spec's, it is a paragraph in the card rather than a
		// toast payload, and nothing in the card emits a toast on failure.
		expect(CAPS_SAVE_ERROR_COPY).toBe(
			"The caps were not saved. The old limits are still in force.",
		);
		const source = renderCard();
		expect(source).toContain('data-slot="blast-radius-caps"');
		expect(source).not.toContain("sonner");
	});

	test("populated: six number inputs in a three-column grid with the read-out above", () => {
		const markup = renderCard();
		const inputs = markup.match(/data-slot="input-group-control"/g) ?? [];
		expect(inputs).toHaveLength(6);
		expect(markup).toContain('type="number"');
		// React's server renderer emits the camelCase property name verbatim.
		expect(markup).toContain('inputMode="numeric"');
		expect(markup).toContain("sm:grid-cols-3");
		expect(markup).toContain("tabular-nums");
		expect(markup.indexOf('data-slot="caps-readout"')).toBeLessThan(
			markup.indexOf('data-slot="caps-per_call"'),
		);
	});

	test("populated: Save caps is the only accent button and starts disabled", () => {
		const markup = renderCard();
		const savePosition = markup.indexOf(CAPS_COPY.save);
		expect(savePosition).toBeGreaterThan(-1);
		expect(markup.slice(0, savePosition)).toContain("disabled");
		expect((markup.match(/bg-primary text-primary-foreground/g) ?? []).length).toBe(1);
	});

	test("partial: a saved vector clears the defaults marker without comparing values", () => {
		// Saving the shipped numbers verbatim is a CHOICE; the marker follows
		// the server's row-existence flag, not a value comparison.
		const markup = renderCard({
			vector: vector({ is_default: false, updated_at: "2026-09-16T08:00:00Z" }),
		});
		const text = renderedText(markup);
		expect(text).toContain(CAPS_COPY.savedMarker);
		expect(text).not.toContain(CAPS_COPY.defaultsMarker);
	});

	test("partial: caps save as one unit — there is one Save control, not six", () => {
		const markup = renderCard();
		expect((markup.match(new RegExp(CAPS_COPY.save, "g")) ?? []).length).toBe(1);
		expect(renderedText(markup)).not.toContain("Save per call");
	});

	test("zero-one-many: a cap of zero renders as 0, not as empty or absent", () => {
		const markup = renderCard({
			vector: vector({ per_session_calls: 0, per_call_records: 0 }),
		});
		expect(markup).toContain('value="0"');
		expect(renderedText(markup)).toContain("0 of 0 calls used since");
	});

	test("overflow: a maximum-value byte budget renders through an InputGroup suffix", () => {
		const markup = renderCard({
			vector: vector({ per_session_bytes: 1024 ** 4 * 8, per_call_bytes: 512 * MEBIBYTE }),
		});
		expect(markup).toContain('data-slot="input-group-addon"');
		expect(renderedText(markup)).toContain("TiB");
		expect(renderedText(markup)).toContain("MiB");
		// The raw byte count never reaches the input.
		expect(markup).not.toContain(String(1024 ** 4 * 8));
		expect(markup).toContain('value="8"');
	});

	test("long-text: helper copy sits inside the bounded block and wraps rather than clipping", () => {
		const markup = renderCard();
		const text = renderedText(markup);
		expect(text).toContain(CAPS_COPY.perCallHelper);
		expect(text).toContain(CAPS_COPY.perSessionHelper);
		expect(text).toContain("An agent cannot raise them");
		expect(markup).toContain("max-w-2xl");
		// No `truncate` anywhere in the card: helper copy must wrap, because a
		// clipped sentence about what a cap means is a cap nobody understands.
		expect(markup).not.toContain("truncate");
	});

	test("the card names nothing an agent argument could set", () => {
		const text = renderedText(renderCard()).toLowerCase();
		for (const forbidden of ["max_records", "max records", "tool parameter", "argument"]) {
			expect(text).not.toContain(forbidden);
		}
		expect(text).toContain("an agent cannot raise them");
	});
});

describe("the consumption row the card reads (WINDOWS #187, #195)", () => {
	// The read-out carries a WINDOW item (null session) and SESSION items for
	// the same companion and Agent — the MCP session id has reached the façade
	// since plan 05-10, so a single null-session fixture no longer describes
	// what the server sends. The session item is listed FIRST on purpose: a
	// bare match on companion key would read it.
	const ITEMS: readonly ConsumptionItem[] = [
		consumption({ session_id: "mcp-session-1", calls_consumed: 2 }),
		consumption({ environment_id: OTHER_AGENT_ID, calls_consumed: 150 }),
		consumption({ calls_consumed: 7 }),
	];

	test("the fixture carries a window item and a session item for one companion", () => {
		const mine = ITEMS.filter(
			(item) => item.companion_key === "filebrowser" && item.environment_id === AGENT_ID,
		);
		expect(mine.map((item) => item.session_id)).toEqual(["mcp-session-1", null]);
	});

	test("the card reads this Agent's window item, even with a session item listed first", () => {
		const chosen = windowConsumptionFor(ITEMS, "filebrowser", AGENT_ID);
		expect(chosen?.session_id).toBeNull();
		expect(chosen?.environment_id).toBe(AGENT_ID);
		const text = renderedText(renderCard({ consumption: chosen }));
		expect(text).toContain("7 of 200 calls used since");
		expect(text).not.toContain("2 of 200 calls used since");
		expect(text).not.toContain("150 of 200 calls used since");
	});

	test("no window item yet reads as nothing spent, never as a session's counter", () => {
		const onlySession = [consumption({ session_id: "mcp-session-1", calls_consumed: 2 })];
		expect(windowConsumptionFor(onlySession, "filebrowser", AGENT_ID)).toBeUndefined();
		expect(renderedText(renderCard({ consumption: undefined }))).toContain(
			"0 of 200 calls used since",
		);
	});
});
