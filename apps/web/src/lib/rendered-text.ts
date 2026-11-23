/**
 * TEST-ONLY assertion instrument: the text an operator can actually READ.
 *
 * Imported only from `*.test.*` files, mirroring the discipline
 * `ui-spec-contract.ts` already states.
 *
 * **Why it exists.** `renderToStaticMarkup` output contains attribute values,
 * so `expect(markup).toContain(id)` passes when the id appears only inside
 * `<input value={id}>` — i.e. when the assertion is reading back the search
 * term the test itself typed. `export-section.test.tsx`'s "an email-less
 * tenant renders its id" passes that way today with zero result rows
 * rendered. A claim that the console DISPLAYS a value must be checked against
 * text nodes only; attributes live inside tags, so they are gone by
 * construction here.
 *
 * Two deliberate properties:
 *
 * - Tags are removed BEFORE entities are decoded, so `&lt;script&gt;` stays
 *   the literal text an operator sees instead of being manufactured into a
 *   tag and swallowed.
 * - Text nodes are joined by a single space, never concatenated. Without the
 *   separator `<td>a</td><td>b</td>` yields `"ab"`, and `"ab"` is a substring
 *   match for a value neither cell displays.
 *
 * A helper that quietly returned `""` would make every assertion built on it
 * vacuous, so its own test file pins each of these properties.
 */

/** The entities `renderToStaticMarkup` produces for text content. */
const ENTITIES: Record<string, string> = {
	"&#x27;": "'",
	"&quot;": '"',
	"&amp;": "&",
	"&lt;": "<",
	"&gt;": ">",
};

/** A sentinel no HTML text node can contain, marking where a tag stood. */
const TAG_MARKER = "\u0000";

/** The visible text of a static-markup string: tags and attributes removed. */
export function renderedText(markup: string): string {
	return markup
		.replace(/<[^>]*>/g, TAG_MARKER)
		.replace(/&(?:#x27|quot|amp|lt|gt);/g, (entity) => ENTITIES[entity] as string)
		.split(TAG_MARKER)
		.map((segment) => segment.replace(/\s+/g, " ").trim())
		.filter((segment) => segment !== "")
		.join(" ");
}
