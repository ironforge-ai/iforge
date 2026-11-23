import { describe, expect, test } from "bun:test";
import { renderedText } from "@/lib/rendered-text";

describe("renderedText strips everything that is not a text node", () => {
	test("THE PROPERTY THAT MATTERS: a value present only as an attribute is not output", () => {
		// This is the shape of `<input value={term}>` — exactly how
		// export-section.test.tsx's "an email-less tenant renders its id" passed
		// with zero results rendered. An assertion that echoes its own input is
		// not evidence that the console shows anything.
		const id = "3f1c2b84-5d6e-4a70-9b21-0c8e7f4a1d33";
		expect(renderedText(`<input value="${id}" />`)).not.toContain(id);
		expect(renderedText(`<div data-id="${id}"><span>nothing here</span></div>`)).not.toContain(id);
	});

	test("text content survives", () => {
		const id = "3f1c2b84-5d6e-4a70-9b21-0c8e7f4a1d33";
		expect(renderedText(`<span class="font-mono">${id}</span>`)).toBe(id);
	});

	test("a title attribute repeating a value does not double it", () => {
		const id = "3f1c2b84-5d6e-4a70-9b21-0c8e7f4a1d33";
		// A naive "strip the angle brackets" helper would emit the id twice, so a
		// `toContain` for a DIFFERENT value could pass off the concatenation.
		expect(renderedText(`<span title="${id}">${id}</span>`)).toBe(id);
	});

	test("adjacent text nodes are joined by a single space, never concatenated", () => {
		// Without the separator, `<td>a</td><td>b</td>` yields "ab" — and "ab" is a
		// substring match for a value neither cell displays.
		expect(renderedText("<td>alpha</td><td>beta</td>")).toBe("alpha beta");
	});

	test("entity decoding round-trips for an apostrophe and an ampersand", () => {
		expect(renderedText("<p>We couldn&#x27;t reach it</p>")).toBe("We couldn't reach it");
		expect(renderedText("<p>Export &amp; delete</p>")).toBe("Export & delete");
		expect(renderedText("<p>&quot;quoted&quot;</p>")).toBe('"quoted"');
	});

	test("decoding runs AFTER tags are removed, so an escaped tag cannot become one", () => {
		// `&lt;script&gt;` is TEXT the operator sees. Decoding first would
		// manufacture a tag and swallow it.
		expect(renderedText("<p>&lt;script&gt;</p>")).toBe("<script>");
	});

	test("markup with no text nodes is the empty string", () => {
		expect(renderedText('<input value="x" />')).toBe("");
	});
});
