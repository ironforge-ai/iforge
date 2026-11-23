import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
	NATIVE_RELEASE_REPOSITORY,
	nativeReleaseBaseUrl,
} from "../src/lib/native-release-manifest";

// ---------------------------------------------------------------------------
// The single authority for "is it published yet".
//
// config/distribution-state.txt is the one declared answer. Every assertion below
// branches on it rather than on a constant in this file, so the documents and the
// declaration cannot drift: changing one without the other turns this suite red.
// ---------------------------------------------------------------------------

const REPO_ROOT = resolve(import.meta.dir, "../../..");
const readRepoFile = (relativePath: string): string =>
	readFileSync(resolve(REPO_ROOT, relativePath), "utf8");

type DistributionState = "available" | "unavailable";

/**
 * Parse `config/distribution-state.txt`. Grammar, as declared in that file's own header:
 * one `key=value` pair per line; blank lines and lines whose first character is `#` are
 * skipped. A missing or malformed declaration is a hard failure, never a default -- a
 * default would let this suite certify documents against a state nobody declared.
 */
function readDistributionState(): DistributionState {
	const raw = readRepoFile("config/distribution-state.txt");
	const pairs = new Map<string, string>();
	for (const line of raw.split("\n")) {
		const trimmed = line.trim();
		if (trimmed.length === 0 || trimmed.startsWith("#")) continue;
		const separator = trimmed.indexOf("=");
		if (separator === -1) continue;
		pairs.set(trimmed.slice(0, separator).trim(), trimmed.slice(separator + 1).trim());
	}
	const declared = pairs.get("public_install_routes");
	if (declared !== "available" && declared !== "unavailable") {
		throw new Error(
			`config/distribution-state.txt must declare public_install_routes as "available" or "unavailable"; found ${JSON.stringify(declared)}`,
		);
	}
	return declared;
}

const distributionState = readDistributionState();

// ---------------------------------------------------------------------------
// URL prefixes, derived -- never retyped.
//
// The repository path is read from NATIVE_RELEASE_REPOSITORY and the origin is parsed
// back out of nativeReleaseBaseUrl(), so this file inherits the single-authority property
// plan 05 established instead of creating a second authority. `raw.githubusercontent.com`
// is a GitHub platform hostname, not a release-location decision; the part that encodes
// *which project* a user is sent to is the repository path, and that is derived.
// ---------------------------------------------------------------------------

const RELEASE_ORIGIN = new URL(nativeReleaseBaseUrl("0.0.0")).origin;
const REPOSITORY_URL_PREFIX = `${RELEASE_ORIGIN}/${NATIVE_RELEASE_REPOSITORY}`;
const RAW_CONTENT_HOST = "https://raw.githubusercontent.com";
const RAW_CONTENT_URL_PREFIX = `${RAW_CONTENT_HOST}/${NATIVE_RELEASE_REPOSITORY}`;

const cliPackage = JSON.parse(readRepoFile("packages/cli/package.json")) as {
	name: string;
	files?: string[];
};
const SCOPED_PACKAGE_NAME = cliPackage.name;

/**
 * The exact sentence both READMEs must carry while `public_install_routes=unavailable`.
 * Declared in ONE place so the repository README and the shipped CLI README cannot drift
 * into telling a reader two different things about the same fact.
 */
const NOT_YET_PUBLISHED_NOTE =
	"iforge is not published to a public registry yet, so there is no one-line install today: build the CLI from source with the steps below.";

// ---------------------------------------------------------------------------
// Extraction.
//
// Two patterns for routes that fetch over the network -- the shapes a reader copy-pastes
// into a shell and that therefore depend on a public route resolving. Plus one pattern for
// the build-from-source route, which is what a document must offer instead when no public
// route exists. Non-vacuity counts all three: a document must always tell a reader HOW to
// install, and the declared state governs only WHICH route it may name.
// ---------------------------------------------------------------------------

/**
 * `curl <url> | sh` and `wget <url> | sh` — the fetch-and-execute route in every spelling.
 *
 * WIDENED by CR-02. It required the literal `-fsSL` and the literal `sh` immediately after
 * the pipe, so three one-line variations on the exact defect this test exists to prevent
 * passed it: `curl -sSL … | sh`, `curl -fsSL … | bash` and `wget -qO- … | sh`, each pointing
 * at upstream's `https://clawdi.ai/install.sh`. Under `public_install_routes=unavailable` the
 * network assertion is a NEGATIVE (`toEqual([])`), so a pattern that fails to match is the
 * passing state — a narrow regex here is not a smaller net, it is a silent one.
 *
 * Flags are now unconstrained, the fetcher may be curl or wget, and the interpreter may be
 * any of sh/bash/zsh/dash with or without inline env assignments. `[^\n|]*` on both sides of
 * the URL keeps the match on one line, so a multi-line `curl … \` continuation that ends in
 * `| jq` (README.md's admin-key example) is not a match.
 */
const NETWORK_SCRIPT_PATTERN =
	/(?:curl|wget)\s+[^\n|]*?(https?:\/\/\S+)[^\n|]*\|\s*(?:[A-Z_][A-Z0-9_]*=\S+\s+)*(?:ba|z|da)?sh\b/g;
/**
 * `npm i -g <spec>`, `npm install --global <spec>`, `bun add -g <spec>`,
 * `yarn global add <spec>`, `pnpm add -g <spec>`.
 *
 * WIDENED by CR-02 alongside the pattern above: it matched the short `-g` only, so
 * `npm i --global clawdi` — the documented long spelling of the same command — was invisible.
 *
 * `yarn global add` is its own alternative rather than sharing the flag group: yarn spells the
 * global-ness in the verb and takes no `-g`, so folding it in behind a required `(?:-g|--global)`
 * would have added a spelling the pattern still could not see. Caught by the control case below,
 * which is the point of having one.
 */
const GLOBAL_PACKAGE_PATTERN =
	/(?:(?:npm\s+(?:i|install)|bun\s+add|pnpm\s+add)\s+(?:-g|--global)|yarn\s+global\s+add)\s+(\S+)/g;
/**
 * `npx <spec>`, `bunx <spec>`, `pnpm dlx <spec>` — the run-without-installing routes.
 *
 * Added at phase end after verification found `npx @iforge/cli --help` alive in
 * README.md while this suite reported 14 pass. These fetch from the registry just
 * as `npm i -g` does, so under `public_install_routes=unavailable` they are network
 * instructions like any other; the original three patterns simply could not see
 * them. A gate that passes over a document carrying a dead route is the
 * narrowed-until-provable failure this phase exists to close, one layer down.
 */
const EPHEMERAL_RUNNER_PATTERN = /(?:npx|bunx|pnpm\s+dlx)\s+(?!-)(\S+)/g;
/** The build-from-source route: clone the repository, or run the CLI entrypoint from it. */
const SOURCE_BUILD_PATTERN = /git\s+clone\s+(\S+)|bun\s+run\s+packages\/cli\/src\/index\.ts/g;

type InstructionKind = "network-script" | "network-package" | "source-build";

interface Instruction {
	kind: InstructionKind;
	/** The captured URL or package spec, when the pattern captures one. */
	target: string | null;
	/** Byte offset in the document, used by the ordering assertion. */
	index: number;
	text: string;
}

function extractInstructions(document: string): Instruction[] {
	const found: Instruction[] = [];
	const scan = (pattern: RegExp, kind: InstructionKind): void => {
		for (const match of document.matchAll(new RegExp(pattern.source, pattern.flags))) {
			found.push({
				kind,
				target: match[1] ?? null,
				index: match.index ?? 0,
				text: match[0],
			});
		}
	};
	scan(NETWORK_SCRIPT_PATTERN, "network-script");
	scan(GLOBAL_PACKAGE_PATTERN, "network-package");
	scan(EPHEMERAL_RUNNER_PATTERN, "network-package");
	scan(SOURCE_BUILD_PATTERN, "source-build");
	return found.sort((left, right) => left.index - right.index);
}

const isNetworkRoute = (instruction: Instruction): boolean =>
	instruction.kind === "network-script" || instruction.kind === "network-package";

// ---------------------------------------------------------------------------
// The enumerated surface.
//
// `minimumInstructions` is what makes a vacuous pass impossible (T-03.1G-14). A doc-contract
// test that matches nothing passes and reads as evidence -- that is exactly how BRAND-05 came
// to be checked while its first verb was untested.
//
// The floors are now KIND-SPECIFIC, and the reason is CR-02. They were set against a tree that
// carried three network instructions in README.md and two in packages/cli/README.md, and the
// comment here claimed that a regex which silently stopped matching would fail this suite
// rather than satisfy it. That stopped being true the moment plan 12 removed the network
// routes: the totals are now met ENTIRELY by `source-build` hits, so every network pattern
// could match nothing forever and the floor would still be satisfied. Measured at this HEAD:
//
//   README.md              net:0 pkg:0 eph:0 src:3   -> total 3
//   packages/cli/README.md net:0 pkg:0 eph:0 src:2   -> total 2
//
// So a total floor cannot be the non-vacuity control for the network extractor while the
// declared state is `unavailable` and the network assertion is a negative. `minimumSourceBuild`
// keeps the floor honest about what it actually counts, and the extractor control below --
// which runs the real patterns over a fixed inline fixture -- is what proves the negative
// assertion is still CAPABLE of firing. That control is deliberately independent of the
// documents: it cannot be satisfied by a `source-build` hit, and it cannot be quietly
// weakened by an edit to a README.
// ---------------------------------------------------------------------------

interface InstallDocSource {
	path: string;
	/** Floor over instructions of every kind: a document must always say HOW to install. */
	minimumInstructions: number;
	/** Floor over `source-build` alone, the route the declared state currently supports. */
	minimumSourceBuild: number;
	/** True when the file travels inside the published tarball via package.json `files`. */
	isShipped: boolean;
}

const INSTALL_DOC_SOURCES: InstallDocSource[] = [
	{ path: "README.md", minimumInstructions: 3, minimumSourceBuild: 3, isShipped: false },
	{
		path: "packages/cli/README.md",
		minimumInstructions: 2,
		minimumSourceBuild: 2,
		isShipped: true,
	},
];

// ---------------------------------------------------------------------------
// The extractor control (CR-02).
//
// One line per shape, each asserted to yield exactly one instruction of the named kind. This
// is the POSITIVE half of a negative assertion: `${path} presents no network install route`
// passes when the extractor matches nothing, so without a control that the extractor can
// still match, that test certifies the extractor rather than the document.
//
// The first four rows are the exact one-liners measured to pass BOTH this suite and
// scripts/check_rebrand_residual.py before CR-02 was fixed, with upstream's real installer URL
// left in place: they are the regression, written down.
// ---------------------------------------------------------------------------

interface ExtractorControlCase {
	line: string;
	kind: InstructionKind;
}

const EXTRACTOR_CONTROL_CASES: ExtractorControlCase[] = [
	{ line: "curl -sSL https://clawdi.ai/install.sh | sh", kind: "network-script" },
	{ line: "curl -fsSL https://clawdi.ai/install.sh | bash", kind: "network-script" },
	{ line: "wget -qO- https://clawdi.ai/install.sh | sh", kind: "network-script" },
	{ line: "npm i --global clawdi", kind: "network-package" },
	{ line: "curl -fsSL https://example.test/install.sh | sh", kind: "network-script" },
	{
		line: "curl -fsSL https://example.test/install.sh | IFORGE_HOME=/opt sh",
		kind: "network-script",
	},
	{ line: "wget -qO- https://example.test/install.sh | zsh", kind: "network-script" },
	{ line: "npm i -g @scope/pkg", kind: "network-package" },
	{ line: "npm install -g @scope/pkg", kind: "network-package" },
	{ line: "bun add -g @scope/pkg", kind: "network-package" },
	{ line: "yarn global add @scope/pkg", kind: "network-package" },
	{ line: "pnpm add --global @scope/pkg", kind: "network-package" },
	{ line: "npx @scope/pkg --help", kind: "network-package" },
	{ line: "bunx @scope/pkg --help", kind: "network-package" },
	{ line: "git clone https://example.test/fork.git", kind: "source-build" },
	{ line: "bun run packages/cli/src/index.ts --help", kind: "source-build" },
];

/** Shapes that must NOT be read as install instructions, so the widened patterns stay honest. */
const EXTRACTOR_NEGATIVE_CONTROL_CASES: string[] = [
	"curl -sS http://localhost:8000/health",
	"curl -sS -X POST http://localhost:8000/v1/admin/auth/keys \\\n  -H 'X-Admin-Key: k' \\\n  | jq -r .raw_key",
	"| Connectors/Memory | Agent connects to `mcp.clawdi.ai` directly | Local MCP server |",
	"bun install",
];

const documents = new Map(
	INSTALL_DOC_SOURCES.map((source) => [source.path, readRepoFile(source.path)]),
);
const instructions = new Map(
	INSTALL_DOC_SOURCES.map((source) => [
		source.path,
		extractInstructions(documents.get(source.path) ?? ""),
	]),
);

const documentOf = (path: string): string => documents.get(path) ?? "";
const instructionsOf = (path: string): Instruction[] => instructions.get(path) ?? [];

describe("install documentation contract", () => {
	// Test 1 -- non-vacuity. The check that stops this suite certifying an empty set.
	describe("non-vacuity", () => {
		for (const source of INSTALL_DOC_SOURCES) {
			test(`${source.path} yields at least ${source.minimumInstructions} install instructions`, () => {
				const found = instructionsOf(source.path);
				expect(found.length).toBeGreaterThanOrEqual(source.minimumInstructions);
			});

			test(`${source.path} yields at least ${source.minimumSourceBuild} source-build instructions`, () => {
				const sourceBuild = instructionsOf(source.path).filter(
					(instruction) => instruction.kind === "source-build",
				);
				expect(sourceBuild.length).toBeGreaterThanOrEqual(source.minimumSourceBuild);
			});
		}

		// Test 1b (CR-02) -- the extractor control. Every assertion under
		// `public_install_routes=unavailable` that says a document presents NO network route is
		// satisfied by an extractor that matches nothing. These cases are the proof that the
		// extractor still matches, and they cannot be met by a `source-build` hit or weakened
		// by an edit to a README.
		for (const control of EXTRACTOR_CONTROL_CASES) {
			test(`the extractor reads ${JSON.stringify(control.line)} as ${control.kind}`, () => {
				const found = extractInstructions(control.line);
				expect(
					found.map((instruction) => instruction.kind),
					`${control.line} was not extracted as a ${control.kind} instruction`,
				).toEqual([control.kind]);
			});
		}

		for (const line of EXTRACTOR_NEGATIVE_CONTROL_CASES) {
			test(`the extractor reads no install instruction in ${JSON.stringify(line)}`, () => {
				expect(
					extractInstructions(line).map((instruction) => instruction.text),
					`${line} was read as an install instruction`,
				).toEqual([]);
			});
		}
	});

	// Test 2 -- identity. A bare package name, or a URL under someone else's namespace, is
	// how a reader ends up installing a product this fork does not publish (T-03.1G-05, D-04).
	describe("identity", () => {
		for (const source of INSTALL_DOC_SOURCES) {
			test(`${source.path} names only this fork's package or repository`, () => {
				for (const instruction of instructionsOf(source.path)) {
					if (instruction.kind === "network-package") {
						expect(instruction.target).toBe(SCOPED_PACKAGE_NAME);
						continue;
					}
					const target = instruction.target;
					if (target === null) continue; // `bun run packages/cli/src/index.ts` names no URL.
					const underThisFork =
						target.startsWith(RAW_CONTENT_URL_PREFIX) || target.startsWith(REPOSITORY_URL_PREFIX);
					expect(
						underThisFork,
						`${source.path} points at ${target}, which is not under ${NATIVE_RELEASE_REPOSITORY}`,
					).toBe(true);
				}
			});
		}
	});

	// Tests 3 and 4 -- the documents may promise only what the declaration supports.
	describe(`declared state: public_install_routes=${distributionState}`, () => {
		if (distributionState === "unavailable") {
			for (const source of INSTALL_DOC_SOURCES) {
				test(`${source.path} presents no network install route`, () => {
					const network = instructionsOf(source.path).filter(isNetworkRoute);
					expect(
						network.map((instruction) => instruction.text),
						`${source.path} still presents a network install route while public_install_routes=unavailable`,
					).toEqual([]);
				});

				test(`${source.path} carries the not-yet-published note verbatim`, () => {
					expect(documentOf(source.path)).toContain(NOT_YET_PUBLISHED_NOTE);
				});

				test(`${source.path} offers the build-from-source route instead`, () => {
					const sourceBuild = instructionsOf(source.path).filter(
						(instruction) => instruction.kind === "source-build",
					);
					expect(sourceBuild.length).toBeGreaterThan(0);
				});
			}
		} else {
			for (const source of INSTALL_DOC_SOURCES) {
				test(`${source.path} still presents its network install routes`, () => {
					const network = instructionsOf(source.path).filter(isNetworkRoute);
					expect(network.length).toBeGreaterThan(0);
				});
			}
		}
	});

	// Test 5 -- ordering. A document whose FIRST instruction is the unsupported one is a defect
	// even when a later one works: the first is the one a reader runs.
	test("README.md leads with an instruction the declared state supports", () => {
		const first = instructionsOf("README.md")[0];
		expect(first).toBeDefined();
		if (!first) return;
		const supported = distributionState === "available" ? true : first.kind === "source-build";
		expect(
			supported,
			`README.md leads with a ${first.kind} instruction (${first.text}) while public_install_routes=${distributionState}`,
		).toBe(true);
	});

	// Test 6 -- the shipped surface. packages/cli/README.md travels inside the tarball, so its
	// instructions reach consumers who never open this repository (WINDOWS #51). Asserted by
	// reading the manifest so the coupling is stated rather than remembered.
	test("the shipped CLI README is inside the published tarball", () => {
		const shipped = INSTALL_DOC_SOURCES.filter((source) => source.isShipped);
		expect(shipped.map((source) => source.path)).toEqual(["packages/cli/README.md"]);
		expect(cliPackage.files ?? []).toContain("README.md");
	});

	// Test 7 -- opt-in live probe. Hermetic by default; a REPORTED skip, never a silent pass.
	const probeEnabled = process.env.INSTALL_DOC_PROBE === "1";
	test.skipIf(!probeEnabled)("every documented URL resolves (INSTALL_DOC_PROBE=1)", async () => {
		const urls = INSTALL_DOC_SOURCES.flatMap((source) =>
			instructionsOf(source.path)
				.filter((instruction) => instruction.kind === "network-script")
				.map((instruction) => instruction.target),
		).filter((url): url is string => url !== null);
		for (const url of urls) {
			const response = await fetch(url);
			expect(response.status, `${url} did not resolve`).toBe(200);
		}
	});

	// Test 8 -- BRAND-05's SECOND verb. `nativeInstallCommand` builds the CLI's own self-update
	// one-liner from nativeReleaseBaseUrl, which resolves under the same release repository the
	// documented install URLs resolve under. Holding both to the one authority is what stops
	// them ever disagreeing about where a user is sent.
	describe("self-update route", () => {
		const updateSource = readRepoFile("packages/cli/src/commands/update.ts");

		test("derives its URL from the release-manifest authority, not a literal", () => {
			expect(updateSource).toContain("nativeReleaseBaseUrl(version)");
			expect(updateSource).not.toContain(RAW_CONTENT_HOST);
			expect(updateSource).not.toContain(NATIVE_RELEASE_REPOSITORY);
			expect(nativeReleaseBaseUrl("1.2.3").startsWith(REPOSITORY_URL_PREFIX)).toBe(true);
		});

		test("is governed by the same declaration as the documented install routes", () => {
			const selfUpdateBase = nativeReleaseBaseUrl("1.2.3");
			if (distributionState === "unavailable") {
				// DECLARED-UNAVAILABLE: the route resolves under a release path the repository
				// declares does not exist, and no document may present it as a working instruction.
				for (const source of INSTALL_DOC_SOURCES) {
					expect(documentOf(source.path)).not.toContain(`${selfUpdateBase}/install.sh`);
				}
			} else {
				expect(selfUpdateBase.startsWith(REPOSITORY_URL_PREFIX)).toBe(true);
			}
		});
	});
});
