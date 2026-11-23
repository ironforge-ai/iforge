/**
 * The help-brand contract.
 *
 * `packages/cli/src` sits OUTSIDE the pathspec of `scripts/check_rebrand_residual.py` by D-01,
 * which is correct for IDENTIFIERS and wrong for the STRINGS the CLI prints. This file is the
 * compensating control for exactly that gap: it executes the real binary and asserts on its
 * stdout, so the printed surface is guarded while every symbol in the tree stays byte-identical
 * to upstream. Nothing here reads source text looking for brand tokens — reaching the strings
 * through the running program is what keeps the identifiers out of reach by construction.
 *
 * ONE test here still reads source rather than stdout -- the daemon-install remediation check --
 * and it says why at its own site. The other source scanner, the deferred-invocation inventory
 * that assertions 5 and 6 consumed, was retired by plan 03.3-11 in favour of a check that owns
 * the whole class; see the note where its constant was declared.
 */
import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const cliRoot = resolve(import.meta.dir, "..");
const repoRoot = resolve(cliRoot, "..", "..");
const entrypoint = join(cliRoot, "src", "index.ts");
const srcRoot = join(cliRoot, "src");

const contractSource = readFileSync(resolve(repoRoot, "config/rebrand-env-contract.txt"), "utf8");
const allowlistSource = readFileSync(resolve(repoRoot, "config/rebrand-allowlist.txt"), "utf8");

/** Every spelling named by the frozen contract, public and internal, so the spawn can unset both. */
const CONTRACT_SPELLINGS: string[] = contractSource
	.split("\n")
	.map((line) => line.trim())
	.filter((line) => line.length > 0 && !line.startsWith("#"))
	.flatMap((line) => line.split("="))
	.map((token) => token.trim())
	.filter((token) => token.length > 0);

/** The left-hand sides only — the names a user is allowed to set, and the only ones help may print. */
const CONTRACT_PUBLIC_NAMES: string[] = contractSource
	.split("\n")
	.map((line) => line.trim())
	.filter((line) => line.length > 0 && !line.startsWith("#"))
	.map((line) => line.split("=")[0]?.trim() ?? "")
	.filter((token) => token.length > 0);

interface HelpCarveOut {
	/** The exact substring that survives in help output. */
	literal: string;
	/** A carve-out id written verbatim in `config/rebrand-allowlist.txt`. Asserted, not asserted-by-eye. */
	carveOutId: string;
	/** Why the token stays. */
	why: string;
}

/**
 * The ENUMERATED list of old-brand tokens that are allowed to survive in help output.
 *
 * This is a named allowlist, never a count: assertion 4 strips these and then demands the
 * remainder is free of the brand token entirely, and separately demands every entry below
 * actually matches something — a carve-out covering nothing is a fence nobody decided to keep,
 * which is the rule `config/rebrand-allowlist.txt` already applies to itself.
 */
const HELP_CARVE_OUTS: HelpCarveOut[] = [
	// The vault secret-reference scheme and its prose noun stood here as C-4 carve-outs until
	// upstream sync step 4 (06.1-11). The fork's backend writes every reference and only the
	// fork's CLI and backend read them, so under the iforge-only rule the scheme is `iforge://`
	// and both entries stopped covering anything; removed in the same change.
	// `.env.clawdi` stood here until plan 03.3-11. The scripted sweep rewrote all six printed
	// occurrences of that example filename (REPLACE:example-filename), so the entry stopped
	// covering anything -- and the assertion below calls a carve-out that covers nothing "a fence
	// nobody decided to keep". Removed in the SAME change that made it dead, which is the only
	// order that leaves this file green at every commit. The `X-1` id it cited stays in
	// `config/rebrand-allowlist.txt`: that file is plan 03.3-14's, and a fence covering zero hits
	// is harmless to the residual scanner. `~/.clawdi` stood here too, fencing `iforge migrate --help`;
	// the command was removed with every other Clawdi-name alias (user directive, 2026-09-30).
	// `/run/clawdi/` stood here, citing the hosted run root, until the platform-identity move
	// (P33-21 reversed 2026-09-11, commit b04e4160). The only help site it covered was the
	// `iforge daemon run --auth-token-file` example, which prints the path derived from
	// `DEFAULT_RUN_ROOT` -- now `/run/iforge` -- so the entry stopped covering anything. Removed in
	// the same change that updated the expectations it fenced; the `A-P1`
	// and `X-1` ids it cited stay in `config/rebrand-allowlist.txt` (the `.env.clawdi` precedent).
	{
		literal: "clawdi_pair",
		carveOutId: "D-07 / X-5",
		why: "An MCP command id written into third-party agent config files on user machines.",
	},
	{
		literal: "clawdi_unpair",
		carveOutId: "D-07 / X-5",
		why: "An MCP command id written into third-party agent config files on user machines.",
	},
	// `clawdi skill` stood here until plan 03.3-21, citing the bundled skill id: "`install.sh`
	// validates the exact skill member paths inside the release tarball, so the id is a published
	// identity". That reason expired when 03.3-10 moved both skill directories AND both halves of
	// the tarball member contract -- `install.sh` and `native-activation.ts` now require
	// `skills/iforge/SKILL.md` and `skills/hosted-versions/1/iforge/SKILL.md`, so the published
	// identity the entry named is `iforge`. It went on covering one site anyway: the `--keep-skill`
	// help text, which named the moved skill by its old id and was wrong twice over. 03.3-21 swept
	// that line, and the entry came down in the SAME change -- the `.env.clawdi` precedent above,
	// and the only order that leaves this file green at every commit. The `D-07 / X-5` id it cited
	// stays in `config/rebrand-allowlist.txt`, which `clawdi_pair` and `clawdi_unpair` still cite.
	{
		literal: "clawdi.ai",
		carveOutId: "D-07 / X-6",
		why: "The hosted share domain baked into the production build. A domain move is a DNS and certificate migration.",
	},
];

// `DEFERRED_INVOCATION_BUDGET` stood here from plan 03.1-10 until plan 03.3-11, an equality
// ratchet over the count of lines under `packages/cli/src` still carrying a bare invocation of
// the old command name: 296 at its introduction, then 289, 277, 275, 273, 272, and finally 23
// once the scripted sweep ran. It was always an INTERIM measure -- an inventory of what a phase
// had not fixed, standing in for a real check while the class had no owner.
//
// RETIRED BY REPLACEMENT, never by deletion. `scripts/rewrite_brand_strings.py --check` now
// refuses any occurrence still pending a rewrite (`assert_sweep_is_complete`), which is
// strictly stronger in both directions this constant could not reach: it covers all five
// replacement classes rather than the one mechanical shape counted here, its ceiling is zero
// rather than 272, and it names the class of the offending line instead of a number that moved.
// It is enforced in both places, exactly as this constant was: host-side in `scripts/test.sh`
// where the repository enumeration can run, and inside the container by
// `test_the_real_tree_carries_no_pending_rewrite` in `backend/tests/test_rebrand_guards.py`.
//
// The 23 lines that still carry the shape are all NAMED KEEPS in that script's rule set, which
// is the answer to the question this constant's own doc comment kept asking: the shape is
// mechanical, the deferral was never the same thing as the count, and the deferral is now zero.

// ── the shapes ───────────────────────────────────────────────────────────────
/** An internal register variable: the private spelling the boundary copies into, never documented. */
const INTERNAL_REGISTER_SHAPE = /CLAWDI_[A-Z0-9_]+/;
/**
 * A bare invocation: the old command name as a whole word, then whitespace, then a lowercase
 * subcommand word. The negative lookbehind is what keeps this off a URI scheme (`<name>://`),
 * the dotted manifest filename, the dot-prefixed state root and the underscore-suffixed MCP ids.
 */
const BARE_INVOCATION_SHAPE = /(?<![A-Za-z0-9_.\-/])clawdi[ \t]+[a-z][a-z0-9-]*/;
/** The old capitalised product name as it was printed next to the hosted service. */
const OLD_PRODUCT_CLOUD_SHAPE = /Clawdi\s+Cloud/;
/** Any surviving brand token at all, after the enumerated carve-outs are removed. */
const ANY_BRAND_TOKEN_SHAPE = /clawdi/i;

// ── help discovery: execute the real binary, read its stdout ─────────────────
interface HelpPage {
	commandPath: string;
	helpText: string;
}

/**
 * A spawn environment that shows the CLI's OWN defaults rather than the developer's shell:
 * every contract spelling (public and internal) is removed, and only the two public
 * update-suppression names are set.
 */
function helpEnv(): Record<string, string | undefined> {
	const env: Record<string, string | undefined> = { ...process.env };
	for (const name of CONTRACT_SPELLINGS) delete env[name];
	env.IFORGE_NO_AUTO_UPDATE = "1";
	env.IFORGE_NO_UPDATE_CHECK = "1";
	return env;
}

function runHelp(segments: string[]): string {
	const result = spawnSync("bun", ["run", entrypoint, ...segments, "--help"], {
		cwd: cliRoot,
		encoding: "utf8",
		env: helpEnv(),
	});
	return `${result.stdout ?? ""}${result.stderr ?? ""}`;
}

/** Parse the `Commands:` section of a help page into subcommand names. */
function parseSubcommands(helpText: string): string[] {
	const lines = helpText.split("\n");
	const start = lines.findIndex((line) => /^Commands:/.test(line));
	if (start === -1) return [];
	const names: string[] = [];
	for (let i = start + 1; i < lines.length; i++) {
		const line = lines[i] ?? "";
		if (line.trim() !== "" && /^\S/.test(line)) break;
		if (line.trim() === "") continue;
		const match = line.match(/^ {2}([a-z][a-z0-9-]*)/);
		if (!match) continue;
		if (match[1] === "help") continue;
		names.push(match[1]);
	}
	return names;
}

function collectHelpPages(): HelpPage[] {
	const pages: HelpPage[] = [];
	const rootHelp = runHelp([]);
	pages.push({ commandPath: "iforge", helpText: rootHelp });
	for (const top of parseSubcommands(rootHelp)) {
		const topHelp = runHelp([top]);
		pages.push({ commandPath: `iforge ${top}`, helpText: topHelp });
		for (const sub of parseSubcommands(topHelp)) {
			pages.push({ commandPath: `iforge ${top} ${sub}`, helpText: runHelp([top, sub]) });
		}
	}
	return pages;
}

const helpPages = collectHelpPages();

/** Remove every enumerated carve-out. Longest literal first, so a short entry cannot eat a long one. */
function stripCarveOuts(text: string): string {
	let remainder = text;
	const ordered = [...HELP_CARVE_OUTS].sort((a, b) => b.literal.length - a.literal.length);
	for (const carveOut of ordered) {
		remainder = remainder.split(carveOut.literal).join(" ");
	}
	return remainder;
}

/** Every offending line, with the command path that printed it, so a failure names its own fix. */
function offenders(shape: RegExp, transform: (text: string) => string = (text) => text): string[] {
	const found: string[] = [];
	for (const page of helpPages) {
		for (const line of transform(page.helpText).split("\n")) {
			if (shape.test(line)) found.push(`${page.commandPath}: ${line.trim()}`);
		}
	}
	return found;
}

// The deferred-invocation inventory helpers (`listNonTestSources`, `measureDeferredInvocations`)
// and the committed-baseline reader (`insideCheckout`, `committedBudget`) went with the constant
// they existed to serve. `BARE_INVOCATION_SHAPE` below stays: assertion 2 forbids that shape in
// help output, and `scripts/rewrite_brand_strings.py` compiles its own copy of the regex OUT OF
// this file rather than restating it, so this declaration is still one half of that comparison.

describe("CLI help-brand contract", () => {
	test("discovery collected a real help surface", () => {
		// A parse that silently found nothing must not be able to satisfy every hard zero below.
		expect(helpPages.length).toBeGreaterThanOrEqual(20);
		expect(helpPages[0]?.helpText).toContain("Usage: iforge");
	});

	test("assertion 1: no help text documents an internal register variable", () => {
		// Raw text on purpose: no carve-out contains an uppercase register name, so this hard
		// zero owes nothing to the allowlist and cannot be widened by adding to it.
		expect(offenders(INTERNAL_REGISTER_SHAPE)).toEqual([]);
	});

	test("assertion 2: no help text invokes the CLI by the old command name", () => {
		// Carve-outs are stripped FIRST, because an enumerated carve-out may share this shape by
		// construction. None does today: the prose noun `clawdi reference` (C-4) moved with the
		// scheme at 06.1-11, and the bundled skill id `clawdi skill` (D-07 / X-5) was retired by
		// plan 03.3-21 with the help string it held. Stripping is what keeps this a hard zero over
		// everything nobody decided to keep, rather than a hard zero with a silent exception.
		expect(offenders(BARE_INVOCATION_SHAPE, stripCarveOuts)).toEqual([]);
	});

	test("assertion 3: no help text names the old capitalised product", () => {
		expect(offenders(OLD_PRODUCT_CLOUD_SHAPE)).toEqual([]);
	});

	test("assertion 4: every surviving brand token is an enumerated carve-out", () => {
		expect(offenders(ANY_BRAND_TOKEN_SHAPE, stripCarveOuts)).toEqual([]);
	});

	test("assertion 4: every carve-out cites an id that exists in the allowlist", () => {
		const unknown = HELP_CARVE_OUTS.filter(
			(carveOut) => !allowlistSource.includes(carveOut.carveOutId),
		).map((carveOut) => `${carveOut.literal} -> ${carveOut.carveOutId}`);
		expect(unknown).toEqual([]);
	});

	test("assertion 4: every carve-out actually covers something", () => {
		const covering = new Set(
			HELP_CARVE_OUTS.filter((carveOut) =>
				helpPages.some((page) => page.helpText.includes(carveOut.literal)),
			).map((carveOut) => carveOut.literal),
		);
		const dead = HELP_CARVE_OUTS.map((carveOut) => carveOut.literal).filter(
			(literal) => !covering.has(literal),
		);
		expect(dead).toEqual([]);
	});

	test("assertion 4: every environment name printed in help is a public contract name", () => {
		const printed = new Set<string>();
		for (const page of helpPages) {
			for (const match of page.helpText.matchAll(/\bIFORGE_[A-Z0-9_]+/g)) {
				printed.add(match[0]);
			}
		}
		const offContract = [...printed].filter((name) => !CONTRACT_PUBLIC_NAMES.includes(name));
		expect(offContract).toEqual([]);
	});

	test("the daemon-install remediation names this fork's own published package", () => {
		// The one source assertion left in this file, and it earns that: reaching this string
		// through the running CLI would require a real failed daemon install. T-03.1G-05 — the
		// string is an INSTRUCTION a user copy-pastes into a shell, so a bare package name that
		// resolves on the public registry to someone else's package is a supply-chain defect,
		// not a branding one (the CR-04 / WINDOWS #51 class).
		const installer = readFileSync(join(srcRoot, "serve", "installer.ts"), "utf8");
		const manifestName = (
			JSON.parse(readFileSync(join(cliRoot, "package.json"), "utf8")) as { name: string }
		).name;
		const advertised = [...installer.matchAll(/npm (?:i|install) -g ([^\s)"'`]+)/g)].map(
			(match) => match[1],
		);
		expect(advertised.length).toBeGreaterThan(0);
		expect([...new Set(advertised)]).toEqual([manifestName]);
	});

	// Assertions 5 and 6 stood here until plan 03.3-11 and were the only source-scanning
	// members of this file. 5 compared `DEFERRED_INVOCATION_BUDGET` to a live scan with `===`;
	// 6 compared it to the value committed at HEAD, skipping outside a checkout and enforced
	// host-side instead (WR-01). Both went with the constant, in the one commit that also
	// removed the host-side half -- see the note where the constant was declared above, and
	// the successor comment in `scripts/test.sh`. Every assertion left in this file reaches
	// the strings through the RUNNING PROGRAM, which is the property the file was written to
	// have and briefly did not.
});
