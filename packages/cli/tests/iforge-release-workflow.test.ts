import { describe, expect, test } from "bun:test";
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { basename, resolve } from "node:path";
import { parse } from "yaml";

import {
	NATIVE_BINARY_NAME,
	NATIVE_RELEASE_REPOSITORY,
	nativeReleaseBaseUrl,
} from "../src/lib/native-release-manifest";

// `.github/workflows/iforge-release.yml` creates the calendar-versioned app/backend/web
// GitHub Release. Its two sibling release workflows each have a contract test; this is the
// third, and it was the one never written. That gap matters more here than it would in a
// repository with CI: this repository's origin is self-hosted and GitHub Actions never
// execute against it (lefthook.yml:14-22), so a contract test is not a supplement to an
// execution signal, it is the only signal there is.
//
// Which is why the mutation case below is this file's real deliverable. A suite that only
// ever sees a document that was already correct proves nothing about itself. Every check
// here is therefore a function over a workflow subject, so the same functions that accept
// the committed file are shown to reject documents carrying the regressions they exist to
// catch.

const repositoryRoot = resolve(import.meta.dir, "../../..");
const workflowPath = resolve(repositoryRoot, ".github/workflows/iforge-release.yml");
const workflowSource = readFileSync(workflowPath, "utf8");
const releaseRunbook = readFileSync(resolve(repositoryRoot, "docs/runbooks/release.md"), "utf8");

// `native-release-manifest.ts` is the single TypeScript authority for this fork's release
// protocol, and the calendar release line must not become a second one. So nothing below
// retypes a repository path or a tag prefix: the CLI release tag prefix is read back out of
// the URL builder exactly as `cli-publish-workflow.test.ts` does, and the calendar line's
// prefix is derived from it. That derivation is itself a claim about the two lines — the CLI
// line extends the app line with `cli-v`, which is precisely why excluding the CLI prefix is
// what keeps a CLI tag out of an app release's notes range.
const CLI_LINE_INFIX = "cli-v";
const releaseBase = nativeReleaseBaseUrl("0.0.0");
const cliTagPrefix = releaseBase.slice(releaseBase.lastIndexOf("/") + 1, -"0.0.0".length);
const appTagPrefix = cliTagPrefix.slice(0, -CLI_LINE_INFIX.length);
const productName = appTagPrefix.slice(0, -1);

// The one name this file cannot derive. The pre-rebrand product name is, by definition,
// absent from every authority module the rebrand left behind; it exists here to be searched
// for and to build the mutants out of.
const LEGACY_PRODUCT_NAME = "clawdi";

// WR-08 said this workflow can no longer see the repository's existing release tags. Half of
// that was right and half was not: the review's premise — that every historical release tag
// here carries the pre-rebrand prefix — is false, because this fork carries no release tags
// at all. So the stated harm cannot occur, but only by coincidence. Fetching an upstream
// remote's tags, or creating one pre-rebrand tag, re-arms it in full.
//
// This constant turns that coincidence into a declared precondition. It is asserted against
// `git tag --list` below and against the runbook that states it to the operator, so the
// prose and the enforcement cannot drift apart. It is deliberately a single constant and not
// two spellings; it is deliberately NOT exported, because Biome's `noExportsInTest` forbids
// an export here and nothing outside this file consumes it — the runbook is the only other
// place the sentence appears, and it appears there as prose this file reads back.
const RELEASE_TAG_PRECONDITION = "This fork carries no release tag from before the rebrand.";

interface ReleaseWorkflowStep {
	env?: Record<string, string>;
	name?: string;
	run?: string;
	uses?: string;
	with?: Record<string, unknown>;
}

interface ReleaseWorkflowJob {
	"runs-on"?: string;
	steps?: ReleaseWorkflowStep[];
}

interface ReleaseWorkflowInput {
	description?: string;
	required?: boolean;
}

interface ReleaseWorkflowDocument {
	name?: string;
	on?: { workflow_dispatch?: { inputs?: Record<string, ReleaseWorkflowInput> } };
	permissions?: Record<string, string>;
	concurrency?: Record<string, unknown>;
	jobs?: Record<string, ReleaseWorkflowJob>;
}

// A subject is a parsed document and the text it was parsed from, together. Some of what this
// workflow does lives in YAML structure and some of it lives in a shell block that YAML sees
// as one opaque string, so an assertion that could only read one of the two would be blind to
// half the file.
interface WorkflowSubject {
	document: ReleaseWorkflowDocument;
	source: string;
}

const readSubject = (source: string): WorkflowSubject => ({
	document: parse(source) as ReleaseWorkflowDocument,
	source,
});

const workflow = readSubject(workflowSource);

function releaseShell(subject: WorkflowSubject): string {
	const shell = subject.document.jobs?.release?.steps?.find(
		(step) => typeof step.run === "string",
	)?.run;
	if (!shell) throw new Error("the release job has no shell step to assert on");
	return shell;
}

function assertManualOnlyTrigger(subject: WorkflowSubject): void {
	expect(Object.keys(subject.document.on ?? {})).toEqual(["workflow_dispatch"]);
	const inputs = subject.document.on?.workflow_dispatch?.inputs ?? {};
	expect(Object.keys(inputs)).toEqual(["version", "target"]);
	expect(Object.values(inputs).map((input) => input.required ?? false)).toEqual([false, false]);
	expect(Object.values(inputs).filter((input) => !input.description)).toEqual([]);
}

function assertLeastPrivilege(subject: WorkflowSubject): void {
	expect(subject.document.permissions).toEqual({ contents: "write" });
	// One grant, declared once. A job-level block would be a second place to widen it.
	expect(Object.values(subject.document.jobs ?? {}).filter((job) => "permissions" in job)).toEqual(
		[],
	);
}

function assertForkIdentity(subject: WorkflowSubject): void {
	const current = new RegExp(productName, "i");
	const legacy = new RegExp(LEGACY_PRODUCT_NAME, "i");
	// Every name a human sees: in the Actions list, in the concurrency queue, and on the
	// published release itself. Matched case-insensitively so a capitalised survivor is caught.
	const identity = {
		displayName: subject.document.name ?? "",
		concurrencyGroup: String(subject.document.concurrency?.group ?? ""),
		releaseTitle: releaseShell(subject).match(/--title "([^"]*)"/)?.[1] ?? "",
	};
	expect(
		Object.entries(identity)
			.filter(([, value]) => value === "")
			.map(([field]) => field),
	).toEqual([]);
	expect(
		Object.entries(identity)
			.filter(([, value]) => !current.test(value))
			.map(([field]) => field),
	).toEqual([]);
	expect(
		Object.entries(identity)
			.filter(([, value]) => legacy.test(value))
			.map(([field]) => field),
	).toEqual([]);
}

function assertWritePaths(subject: WorkflowSubject): void {
	const shell = releaseShell(subject);
	const created = [...shell.matchAll(/^\s*TAG="([^"]*)"/gm)].map((match) => match[1] ?? "");
	expect(created).toHaveLength(1);
	expect(created.filter((tag) => !tag.startsWith(appTagPrefix))).toEqual([]);
	expect(shell).toContain('release create "$TAG"');
	// An operator may paste a whole tag into the version input; both of this fork's prefixes
	// are stripped back off, so the two spellings converge on one tag.
	expect(shell).toContain(`\${INPUT_VERSION#${appTagPrefix}v}`);
	expect(shell).toContain(`\${VERSION#${appTagPrefix}}`);
	expect(shell).not.toContain(LEGACY_PRODUCT_NAME);
}

// Extracted from the workflow's own bash test rather than retyped, so the table in the test
// below exercises the rule the workflow actually enforces instead of a copy of it. Anchored
// on "$VERSION" because the same shell block contains two further `=~` tests for the same-day
// sequence suffix, and an unanchored match would silently pick up the first of those.
function versionPattern(subject: WorkflowSubject): RegExp {
	const pattern = releaseShell(subject).match(/"\$VERSION" =~ (\S+) \]\]/)?.[1];
	if (!pattern) throw new Error("the release workflow does not validate its version");
	return new RegExp(pattern);
}

function assertReadPaths(subject: WorkflowSubject): void {
	const shell = releaseShell(subject);
	const listed = [...shell.matchAll(/git tag --list ['"]([^'"]*)['"]/g)].map(
		(match) => match[1] ?? "",
	);
	expect(listed).toHaveLength(4);
	expect(listed.filter((pattern) => !pattern.startsWith(appTagPrefix))).toEqual([]);
	// WR-08 / T-03.1G-19. Without this filter the newest CLI release tag becomes the app
	// release's --notes-start-tag and the published notes cover a commit range nobody chose.
	expect(shell).toContain(`grep -v '^${cliTagPrefix}'`);
	expect(shell).toContain(`grep -v "^\${TAG}$"`);
	expect(shell).toContain("--notes-start-tag");
}

describe("calendar release workflow contract", () => {
	test("is manual-only and declares exactly its two documented inputs", () => {
		assertManualOnlyTrigger(workflow);
	});

	test("grants exactly one permission, at the level the release step needs", () => {
		assertLeastPrivilege(workflow);
		// The grant is spent through the run token, not a longer-lived credential.
		const step = workflow.document.jobs?.release?.steps?.find((candidate) => candidate.run);
		expect(step?.env?.GH_TOKEN).toBe(`\${{ github.token }}`);
		expect(workflowSource).not.toContain("secrets.");
	});

	test("carries this fork's name in its display name, concurrency group and release title", () => {
		assertForkIdentity(workflow);
		// A release is a durable public artifact; a queued one must wait, never be cancelled.
		expect(workflow.document.concurrency?.["cancel-in-progress"]).toBe(false);
	});

	test("creates only this fork's tags and enforces the two documented version shapes", () => {
		assertWritePaths(workflow);
		const pattern = versionPattern(workflow);
		const cases = [
			{ version: "2026-02-14", accepted: true },
			{ version: "2026-02-14-2", accepted: true },
			{ version: "2026-02-14-rc1", accepted: false },
			{ version: "0.13.47", accepted: false },
			{ version: "", accepted: false },
		];
		expect(cases.map(({ version }) => ({ version, accepted: pattern.test(version) }))).toEqual(
			cases,
		);
		// The shapes the regex accepts are the shapes the input description promises.
		const described = workflow.document.on?.workflow_dispatch?.inputs?.version?.description ?? "";
		expect(described).toContain("YYYY-MM-DD");
		expect(described).toContain("YYYY-MM-DD-N");
	});

	test("reads only this fork's tags and keeps the CLI release line out of the notes range", () => {
		assertReadPaths(workflow);
		const shell = releaseShell(workflow);
		// Tag enumeration is meaningless against a shallow clone.
		const checkout = workflow.document.jobs?.release?.steps?.find((step) =>
			step.uses?.startsWith("actions/checkout"),
		);
		expect(checkout?.with?.["fetch-depth"]).toBe(0);
		// The short-circuit settles whether this release exists before the notes range is chosen.
		expect(shell.indexOf('gh release view "$TAG"')).toBeLessThan(shell.indexOf("PREVIOUS=$("));
		expect(shell).toContain('args+=(--notes-start-tag "$PREVIOUS")');
		// And the notes say out loud which line the reader is not looking at.
		expect(shell).toContain(`${cliTagPrefix}...`);
	});

	test("asserts the release protocol through native-release-manifest.ts rather than restating it", () => {
		// One identity, spelled in three places. If any is renamed without the others this
		// fails here rather than at a release that publishes a tag no reader can resolve.
		expect(NATIVE_RELEASE_REPOSITORY.split("/")[1]).toBe(productName);
		expect(NATIVE_BINARY_NAME).toBe(productName);
		expect(cliTagPrefix.endsWith(CLI_LINE_INFIX)).toBe(true);
		expect(cliTagPrefix).toBe(`${appTagPrefix}${CLI_LINE_INFIX}`);
		// And each derived value is load-bearing against the workflow, so the imports above
		// cannot decay into decoration.
		expect(releaseShell(workflow)).toContain(`grep -v '^${cliTagPrefix}'`);
		expect(releaseShell(workflow)).toContain(`TAG="${appTagPrefix}\${VERSION}"`);
	});

	// The deliverable. Tests 1-6 and 8 run against a document that was already correct by
	// inspection, so on their own they cannot distinguish a real assertion from a vacuous one.
	// These mutants can: each carries exactly one regression this file exists to catch, and is
	// fed through the same functions the passing tests use. Weakening any of those functions
	// later breaks this test too.
	test("rejects a workflow whose identity, tag construction or notes filter regressed", () => {
		const mutate = (from: string, to: string): WorkflowSubject => {
			expect(workflowSource).toContain(from);
			const mutated = workflowSource.replaceAll(from, to);
			expect(mutated).not.toBe(workflowSource);
			return readSubject(mutated);
		};

		const rebranded = mutate(
			`group: ${appTagPrefix}release`,
			`group: ${LEGACY_PRODUCT_NAME}-release`,
		);
		const misTagged = mutate(
			`TAG="${appTagPrefix}\${VERSION}"`,
			`TAG="${LEGACY_PRODUCT_NAME}-\${VERSION}"`,
		);
		const unfiltered = mutate(
			`grep -v '^${cliTagPrefix}'`,
			`grep -v '^${LEGACY_PRODUCT_NAME}-${CLI_LINE_INFIX}'`,
		);

		expect(() => assertForkIdentity(rebranded)).toThrow();
		expect(() => assertWritePaths(misTagged)).toThrow();
		expect(() => assertReadPaths(unfiltered)).toThrow();

		// Each mutant is still a workflow the parser reads and the untouched checks accept, so
		// the rejection above is attributable to the regression rather than to a broken file.
		for (const mutant of [rebranded, misTagged, unfiltered]) {
			expect(() => assertManualOnlyTrigger(mutant)).not.toThrow();
			expect(() => assertLeastPrivilege(mutant)).not.toThrow();
		}
		expect(() => assertWritePaths(rebranded)).not.toThrow();
		expect(() => assertForkIdentity(misTagged)).not.toThrow();
		expect(() => assertForkIdentity(unfiltered)).not.toThrow();
	});

	test("is named by the release runbook, so the two cannot drift apart silently", () => {
		expect(releaseRunbook).toContain(`.github/workflows/${basename(workflowPath)}`);
		expect(releaseRunbook).toContain(workflow.document.name ?? "");
	});
});

describe("calendar release tag-set precondition", () => {
	// The clean Docker runner (`scripts/test.sh`) rsyncs the checkout in WITHOUT
	// repository metadata, so `git tag --list` cannot run there at all — a
	// different failure from the one this test guards against. WINDOWS #38's
	// accepted remedy for the three prior assertions in this class is to skip
	// outside a checkout and enforce host-side, which `scripts/test.sh` now does
	// beside the three rebrand guards. Enumerating tags is the ONLY thing gated
	// here: if `.git` is present the assertion runs exactly as before.
	const insideCheckout = existsSync(resolve(repositoryRoot, ".git"));

	test.skipIf(!insideCheckout)("carries no release tag from before the rebrand", () => {
		// A non-empty result here is NOT a test bug. It is the precondition being violated:
		// pre-rebrand tags have been fetched from an upstream remote, or one was created here.
		// Do not relax this assertion to make it green. Pick a remedy from the runbook first —
		// either delete those tags from this repository, or widen the workflow's
		// `git tag --list` patterns so the enumeration sees them — and only then revisit what
		// this test should say.
		const legacyTags = execFileSync(
			"git",
			["tag", "--list", `${LEGACY_PRODUCT_NAME}-*`, `${LEGACY_PRODUCT_NAME}-v*`],
			{ cwd: repositoryRoot, encoding: "utf8" },
		)
			.split("\n")
			.filter((tag) => tag.length > 0);
		expect(legacyTags).toEqual([]);
	});

	test("never lets a CLI release tag become an app release's notes start tag", () => {
		// Deliberately overlapping the read-path case above. This one filter is what carries
		// WR-08's consequence, so it should be findable from either direction.
		const shell = releaseShell(workflow);
		expect(shell).toContain(`git tag --list '${appTagPrefix}*' --sort=-creatordate`);
		expect(shell).toContain(`grep -v '^${cliTagPrefix}'`);
		// The two release lines share a prefix and sort into one newest-first list, which is
		// exactly why the app line has to exclude the CLI line by name rather than by shape.
		expect(cliTagPrefix.startsWith(appTagPrefix)).toBe(true);
	});

	test("states the tag-set precondition to the operator in the release runbook", () => {
		expect(releaseRunbook).toContain(RELEASE_TAG_PRECONDITION);
		// Both remedies, because which one is right depends on why the tags are there.
		expect(releaseRunbook).toMatch(/delete\s+those\s+tags/);
		expect(releaseRunbook).toMatch(/widen\s+the\s+workflow's\s+`git tag --list`\s+patterns/);
	});
});
