/**
 * The canonical brand strings, as one frozen record.
 *
 * This is the substitution source of truth for `scripts/rewrite_brand_strings.py`, which owns
 * the class of user-visible strings printed from upstream-aligned CLI source (MIGR-03). A
 * value here is a string the sweep may WRITE INTO the tree, so the record's contents are the
 * definition of what that sweep is allowed to produce.
 *
 * The entries are written out VERBATIM rather than read from `config/brand-strings.txt` at
 * runtime, for the same reason `brand-env.ts` gives for the environment contract: this module
 * is bundled into a native binary that has no repository next to it, so there is no file to
 * read. `brand.test.ts` asserts the two agree in both directions, and that test is the only
 * thing stopping them drifting.
 *
 * BRAND STRINGS ONLY. No URL, no email address, no domain -- the destinations authority in
 * packages/shared owns those, and the separation is deliberate rather than tidy. The rewrite
 * script consumes this record as its substitution source; a destination in here would become a
 * destination the sweep writes into source nobody asked it to repoint. Do not merge the two.
 *
 * CASING. The product name is lowercase everywhere, including sentence-initially (N1 / D-04).
 * A compound keeps the qualifier's case and lowercases only the product name (N2). The one
 * uppercase spelling below is the public environment-register prefix, which is SCREAMING_SNAKE
 * by the convention every shell shares (N5 / D-05a) -- printed to the user under that public
 * name while the code keeps reading the internal register, which is the asymmetry D-05a IS.
 */
export const BRAND: Readonly<Record<string, string>> = Object.freeze({
	compoundAI: "iforge AI",
	compoundAPI: "iforge API",
	compoundCLI: "iforge CLI",
	compoundChannels: "iforge Channels",
	compoundCloud: "iforge Cloud",
	compoundMCP: "iforge MCP",
	compoundOAuth: "iforge OAuth",
	compoundProject: "iforge Project",
	compoundVault: "iforge Vault",
	compoundWallet: "iforge Wallet",
	compoundWhatsApp: "iforge WhatsApp",
	environmentPrefix: "IFORGE_",
	invocation: "iforge",
	productName: "iforge",
});
