/**
 * The one place a printed cardinality is turned into words.
 *
 * UI-SPEC rule N8: every `{count}` interpolation in this phase's terminal copy goes through this
 * function, and it is singular at exactly 1. The rule is shared rather than per-row for a reason
 * this repository can already point at — five separate S1 rows print `{count} files`, and five
 * inline `${n} file${n === 1 ? "" : "s"}` expressions are five chances to ship `1 files`. A row
 * that spells its own plural is a row that can disagree with the other four.
 *
 * Zero takes the PLURAL, which is the half of the rule an intuitive `count > 1 ? plural :
 * singular` guard gets backwards.
 *
 * Deliberately independent of `pluralize()` in `commands/vault.ts`: that helper returns the noun
 * alone and is wired into a different surface. Folding the two together is a refactor of vault's
 * printed output, not a formatting cleanup, and it is out of scope here.
 */
export function formatCount(count: number, singular: string, plural?: string): string {
	const noun = count === 1 ? singular : (plural ?? `${singular}s`);
	return `${count} ${noun}`;
}
