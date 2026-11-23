import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import { renderedText } from "@/lib/rendered-text";
import {
	COPY_CLERK_IDENTIFIER_TITLE,
	COPY_IDENTIFIER_TITLE,
	CopyIdentifier,
	writeIdentifierToClipboard,
} from "./copy-identifier";

const VALUE = "8f14e45f-ea0d-4b3a-9c11-2b7d5e6a1c94";

describe("writeIdentifierToClipboard", () => {
	test("reports success when the write resolves", async () => {
		const written: string[] = [];
		const ok = await writeIdentifierToClipboard(VALUE, {
			writeText: async (v: string) => {
				written.push(v);
			},
		});
		expect(ok).toBe(true);
		expect(written).toEqual([VALUE]);
	});

	test("reports failure when the write rejects, rather than throwing", async () => {
		// WR-01: the original called .then() with no .catch(), so a denied
		// clipboard permission produced an unhandled rejection and the operator
		// saw nothing at all — not even a failure.
		const ok = await writeIdentifierToClipboard(VALUE, {
			writeText: async () => {
				throw new Error("NotAllowedError: write permission denied");
			},
		});
		expect(ok).toBe(false);
	});

	test("reports failure in an insecure context, where navigator.clipboard is undefined", async () => {
		// WR-01: over plain http `navigator.clipboard` is undefined, so the
		// original threw a TypeError synchronously inside the click handler.
		const ok = await writeIdentifierToClipboard(VALUE, undefined);
		expect(ok).toBe(false);
	});
});

describe("CopyIdentifier accessible name", () => {
	test("the accessible name is the label, not the uuid", () => {
		// WR-02: a <button> with text content derives its accessible name from
		// that subtree, so `title` never reached assistive tech and the chip's
		// two copy buttons announced as two indistinguishable uuids.
		const markup = renderToStaticMarkup(<CopyIdentifier value={VALUE} />);
		expect(markup).toContain(`aria-label="${COPY_IDENTIFIER_TITLE}"`);
	});

	test("the clerk-id affordance announces its own identifier", () => {
		const markup = renderToStaticMarkup(
			<CopyIdentifier value={VALUE} title={COPY_CLERK_IDENTIFIER_TITLE} />,
		);
		expect(markup).toContain(`aria-label="${COPY_CLERK_IDENTIFIER_TITLE}"`);
		expect(markup).not.toContain(`aria-label="${COPY_IDENTIFIER_TITLE}"`);
	});

	test("the value is still rendered as readable text, not only as an attribute", () => {
		const markup = renderToStaticMarkup(<CopyIdentifier value={VALUE} />);
		expect(renderedText(markup)).toContain(VALUE);
	});
});
