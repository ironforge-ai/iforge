import { describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { parse } from "yaml";

import {
	NATIVE_BINARY_NAME,
	NATIVE_RELEASE_MANIFEST_NAME,
	NATIVE_RELEASE_REPOSITORY,
	nativeAssetName,
	nativeReleaseBaseUrl,
} from "../src/lib/native-release-manifest";

interface WorkflowJob {
	needs?: unknown;
	permissions?: Record<string, string>;
	steps?: Array<Record<string, unknown>>;
}

interface WorkflowDocument {
	jobs: Record<string, WorkflowJob>;
}

const workflow = readFileSync(
	resolve(import.meta.dir, "../../../.github/workflows/cli-publish.yml"),
	"utf8",
);
const workflowDocument = parse(workflow) as WorkflowDocument;
const releaseRunbookDoc = readFileSync(
	resolve(import.meta.dir, "../../../docs/runbooks/release.md"),
	"utf8",
);
const cliDevelopmentDoc = readFileSync(
	resolve(import.meta.dir, "../../../docs/cli-development.md"),
	"utf8",
);
const publishManifestCheckerPath = resolve(
	import.meta.dir,
	"../scripts/check-publish-manifest.mjs",
);
const publishManifestChecker = readFileSync(publishManifestCheckerPath, "utf8");
const manifestContract = readFileSync(
	resolve(import.meta.dir, "../src/runtime/manifest-contract.ts"),
	"utf8",
);
const installScript = readFileSync(resolve(import.meta.dir, "../../../install.sh"), "utf8");
const cliPackage = JSON.parse(
	readFileSync(resolve(import.meta.dir, "../package.json"), "utf8"),
) as {
	name: string;
	version: string;
	publishConfig?: { access?: string; tag?: unknown };
};

describe("CLI pack inventory", () => {
	const packed = {
		id: `${cliPackage.name}@${cliPackage.version}`,
		name: cliPackage.name,
		version: cliPackage.version,
		filename: `${cliPackage.name}-${cliPackage.version}.tgz`,
		files: [
			{ path: "package.json" },
			{ path: "egress-addon/clawdi_egress_addon.py" },
			{ path: "skills/future-skill/SKILL.md" },
		],
	};
	const check = (inventory: unknown) =>
		spawnSync("node", [publishManifestCheckerPath, "--pack-json"], {
			input: JSON.stringify(inventory),
			encoding: "utf8",
		});

	test("accepts the exact source and additional intentional skill files", () => {
		for (const inventory of [[packed], { [cliPackage.name]: packed }]) {
			const result = check(inventory);
			expect(result.status).toBe(0);
			expect(result.stdout.trim()).toBe(packed.filename);
		}
	});

	test("rejects Python cache directories and bytecode anywhere in the package", () => {
		for (const path of [
			"egress-addon/__pycache__/cache.json",
			"egress-addon/addon.pyc",
			"skills/future-skill/cache.pyo",
		]) {
			const result = check([{ ...packed, files: [...packed.files, { path }] }]);
			expect(result.status).toBe(1);
			expect(result.stderr).toContain(path);
			expect(result.stdout).toBe("");
		}
	});

	test("fails closed for invalid inventory, identity or missing addon source", () => {
		for (const inventory of [
			null,
			[],
			[packed, packed],
			{ [cliPackage.name]: packed, other: packed },
			{ other: packed },
			{ [cliPackage.name]: [packed] },
			...[[packed], { [cliPackage.name]: packed }].flatMap((shape) =>
				[
					{ ...packed, id: "other@1.0.0" },
					{ ...packed, name: "other" },
					{ ...packed, version: "unreviewed" },
					{ ...packed, filename: `../${packed.filename}` },
					...["../escape", "/absolute", "a/../b", "a//b", "./file", "a\\b", "C:drive", "a\n"].map(
						(path) => ({ ...packed, files: [...packed.files, { path }] }),
					),
				].map((entry) => (Array.isArray(shape) ? [entry] : { [cliPackage.name]: entry })),
			),
			[{ ...packed, files: [{ path: 1 }] }],
			[{ ...packed, files: [{ path: "package.json" }] }],
		]) {
			const result = check(inventory);
			expect(result.status).toBe(1);
			expect(result.stdout).toBe("");
		}
	});
});

describe("CLI publish workflow contract", () => {
	test("keeps current-run release decisions inside the protected publish topology", () => {
		const build = workflowDocument.jobs["build-immutable-artifact"];
		const publish = workflowDocument.jobs["publish-immutable-artifact-with-oidc"];

		expect(Object.keys(workflowDocument.jobs)).toEqual([
			"build-immutable-artifact",
			"publish-immutable-artifact-with-oidc",
		]);
		expect(build.permissions).toEqual({ contents: "read" });
		expect(publish.needs).toBe("build-immutable-artifact");
		expect(publish.permissions).toEqual({ contents: "write", "id-token": "write" });
		expect(build.steps?.find((step) => step.id === "check")?.["working-directory"]).toBe(
			"packages/cli",
		);
		expect(build.steps?.map((step) => step.id).filter(Boolean)).toEqual([
			"check",
			"build_native_release",
			"pack_release",
		]);
		for (const stepId of ["build_native_release", "pack_release"]) {
			expect(build.steps?.find((step) => step.id === stepId)?.["working-directory"]).toBe(
				"packages/cli",
			);
		}
		expect(publish.steps?.map((step) => step.id).filter(Boolean)).toEqual([
			"verify_release",
			"publish",
			"release",
		]);
	});

	test("keeps the protected OIDC publish fully repository-local", () => {
		const build = workflow.indexOf("  build-immutable-artifact:");
		const publish = workflow.indexOf("  publish-immutable-artifact-with-oidc:");
		const buildJob = workflow.slice(build, publish);
		const publishJob = workflow.slice(publish);

		expect(build).toBeGreaterThan(-1);
		expect(publish).toBeGreaterThan(build);
		expect(buildJob).toContain(
			`runs-on: \${{ vars.CI_RUNNER || 'blacksmith-16vcpu-ubuntu-2404' }}`,
		);
		expect(publishJob).toContain("runs-on: ubuntu-latest");
		expect(publishJob).not.toContain("vars.CI_RUNNER");
		expect(workflow).toContain("needs: build-immutable-artifact");
		expect(workflow).toContain("environment: npm");
		expect(workflow).toContain("id-token: write");
		expect(publishJob).toContain('node-version: "24"');
		expect(publishJob).toContain("npm install --global npm@11.5.1");
		expect(publishJob).toContain('test "$(npm --version)" = "11.5.1"');
		expect(workflow.match(/test "\$\(git rev-parse HEAD\)" = "\$GITHUB_SHA"/g)).toHaveLength(2);
		expect(workflow).not.toContain("git checkout --detach");
		expect(workflow).not.toContain("source_commit");
		expect(workflow).not.toContain("Clawdi-AI/clawdi-hosted");
		expect(workflow).not.toContain("uses: Clawdi-AI/");
		expect(workflow).not.toContain("repository_dispatch");
		expect(workflow).not.toContain("workflow_run");
		expect(workflow).not.toContain("repository: Clawdi-AI/");
	});

	test("builds and publishes the same verified tarball exactly once", () => {
		const publishCommands = workflow.match(/npm publish /g) ?? [];

		expect(publishCommands).toHaveLength(1);
		expect(workflow).not.toContain("publishConfig");
		expect(workflow).toContain(
			`NPM_TAG: \${{ needs['build-immutable-artifact'].outputs.npm_tag }}`,
		);
		expect(workflow).toContain(
			'npm publish "./release/$CLI_TARBALL_FILENAME" --access public --provenance --ignore-scripts --tag "$NPM_TAG"',
		);
		expect(cliPackage.version).not.toContain("-");
		expect(cliPackage.publishConfig).toEqual({ access: "public" });
		expect(publishManifestChecker).toContain(
			'Object.hasOwn(packageJson.publishConfig ?? {}, "tag")',
		);
		expect(publishManifestChecker).toContain(
			"published CLI package must not declare publishConfig.tag",
		);
		expect(workflow).toContain("node scripts/check-publish-manifest.mjs");
		expect(workflow).toContain("CLI_ARTIFACT_NAME: iforge-cli-release");
		expect(workflow).toContain(
			`CLI_TARBALL_FILENAME: \${{ needs['build-immutable-artifact'].outputs.cli_tarball_filename }}`,
		);
		expect(workflow).toContain('tarball="$CLI_TARBALL_FILENAME"');
		expect(workflow).toContain(`name: \${{ env.CLI_ARTIFACT_NAME }}`);
		expect(workflow).toContain("run: bun run typecheck");
		expect(workflow).toContain("- name: Test (ephemeral internal suite)");
		expect(workflow).toContain("run: bun test --isolate --max-concurrency=1 packages/cli");
		expect(workflow).toContain("- name: Native lifecycle (ephemeral internal suite)");
		expect(workflow.indexOf("- name: Test")).toBeLessThan(
			workflow.indexOf("- name: Build package and native release matrix"),
		);
		expect(workflow).toContain('npm install "$tarball_path" --ignore-scripts --no-audit --no-fund');
		expect(workflow).toContain('sha256sum --check "$tarball.sha256"');
		expect(workflow.match(/npm pack /g) ?? []).toHaveLength(1);
		expect(workflow.indexOf("npm pack ")).toBeLessThan(workflow.indexOf("npm publish "));
		expect(workflow).not.toMatch(/npm dist-tag (?:add|rm)/);
		expect(workflow).not.toContain("npm stage publish");
		expect(workflow).not.toContain("NPM_TOKEN");
	});

	test("publishes only an absent exact version and verifies artifact integrity", () => {
		const publishJob = workflow.slice(workflow.indexOf("  publish-immutable-artifact-with-oidc:"));
		const absenceCheck = publishJob.indexOf("if ! npm_release_visible; then");
		const publishCommand = publishJob.indexOf("npm publish ");
		const visibilityWait = publishJob.indexOf("for attempt in $(seq 1 12); do", publishCommand);
		const versionRead = publishJob.indexOf(
			'registry_version=$(npm view "@iforge/cli@$VERSION" version)',
		);
		const integrityRead = publishJob.indexOf(
			'published_integrity=$(npm view "@iforge/cli@$VERSION" dist.integrity)',
		);

		expect(workflow).toContain('- "packages/cli/package.json"');
		expect(absenceCheck).toBeGreaterThan(-1);
		expect(absenceCheck).toBeLessThan(publishCommand);
		expect(visibilityWait).toBeGreaterThan(publishCommand);
		expect(visibilityWait).toBeLessThan(versionRead);
		expect(versionRead).toBeLessThan(integrityRead);
		expect(publishJob).toContain('test "$registry_version" = "$VERSION"');
		expect(publishJob).toContain('if [ "$published_integrity" != "$local_integrity" ]; then');
		expect(publishJob).toContain(
			'echo "@iforge/cli@$VERSION registry integrity does not match this workflow artifact" >&2',
		);
		expect(workflow).toContain('if [ "$attempt" -lt 12 ]; then sleep 5; fi');
		expect(workflow).toContain(
			'echo "@iforge/cli@$VERSION was not visible in the npm registry after 60 seconds" >&2',
		);
	});

	test("creates or completes only the current commit GitHub release", () => {
		expect(workflow.indexOf("npm publish ")).toBeLessThan(
			workflow.indexOf('release create "$tag"'),
		);
		expect(workflow).toContain('if [ "$tag_commit" != "$GITHUB_SHA" ]; then');
		expect(workflow).toContain("release.targetCommitish !== process.env.EXPECTED_COMMIT");
		expect(workflow).toContain('console.log(release.isDraft ? "resume-draft" : "none")');
		expect(workflow).toContain('gh release upload "$tag"');
		expect(workflow).toContain("--clobber");
		expect(workflow).toContain('--target "$GITHUB_SHA"');
		expect(workflow).toContain("--draft");
		expect(workflow).toContain('gh release edit "$tag" --draft=false');
		expect(workflow.match(/gh release upload /g)).toHaveLength(1);
		expect(workflow.match(/gh release edit "\$tag" --draft=false/g)).toHaveLength(2);
		expect(workflow.match(/release create "\$tag"/g)).toHaveLength(1);
		expect(workflow).toContain('case "$NPM_TAG" in');
		expect(workflow).toContain("args+=(--prerelease)");
		expect(workflow).toContain("latest) ;;");
		expect(workflow).toContain('echo "unsupported npm release tag: $NPM_TAG" >&2');
		expect(workflow).not.toContain("pull_request:");
	});

	test("keeps Hosted production semantics exact-version only", () => {
		for (const surface of [workflow, cliDevelopmentDoc, releaseRunbookDoc, manifestContract]) {
			expect(surface).not.toMatch(/clawdi@agent-v2(?!-)/);
		}
		expect(workflow).toContain("npm install -g @iforge/cli@$VERSION");
		expect(cliDevelopmentDoc).toMatch(
			/explicitly supplies the exact\s+`clawdi@<semver>` package spec/,
		);
		expect(cliDevelopmentDoc).toContain("fails closed when the exact input is missing");
		expect(releaseRunbookDoc).toMatch(/supplies the exact\s+`clawdi@<semver>`\s+package\s+spec/);
		expect(releaseRunbookDoc).toMatch(/fails when the exact spec is\s+missing/);
		for (const surface of [cliDevelopmentDoc, releaseRunbookDoc]) {
			expect(surface).toMatch(/never\s+resolves\s+an\s+npm\s+dist-tag/);
			expect(surface).not.toContain("resolves that candidate");
		}
		expect(manifestContract).toContain("must be clawdi@<exact-semver>");
		expect(workflow).not.toContain("npm view clawdi@agent-v2");
	});

	// `native-release-manifest.ts` is the single TypeScript authority for the release
	// protocol, but neither this workflow nor `install.sh` can import a TypeScript module,
	// so both are unlinked duplicates of it. Every literal below is derived from that
	// authority rather than retyped, so a rename on either side fails here instead of
	// silently shipping a workflow that uploads assets no installer can find. These
	// workflows never execute against this repository's origin, so this is the only signal.
	test("spells the release protocol exactly as native-release-manifest.ts defines it", () => {
		const assetPrefix = nativeAssetName("linux-x64").replace(/linux-x64\.tar\.gz$/, "");
		const releaseBase = nativeReleaseBaseUrl("0.0.0");
		const releaseTagPrefix = releaseBase.slice(releaseBase.lastIndexOf("/") + 1, -"0.0.0".length);

		expect(assetPrefix).toBe("iforge-cli-");
		expect(releaseTagPrefix).toBe("iforge-cli-v");

		// The upload/download artifact name shares the asset prefix: one naming scheme.
		expect(workflow).toContain(`CLI_ARTIFACT_NAME: ${assetPrefix}release`);
		expect(workflow).toContain(`release_dir="$RUNNER_TEMP/${assetPrefix}release"`);
		expect(workflow).toContain(`path: \${{ runner.temp }}/${assetPrefix}release/`);

		// The npm tarball, the native archives and the manifest, as the build job names them.
		expect(workflow).toContain(
			`cli_tarball_filename: \`${assetPrefix}\${process.env.VERSION}.tgz\``,
		);
		expect(workflow).toContain(`cp dist-release/${assetPrefix}*.tar.gz "$release_dir/"`);
		expect(workflow).toContain(`cp dist-release/${NATIVE_RELEASE_MANIFEST_NAME} "$release_dir/"`);
		expect(workflow).toContain(`"$install_dir/node_modules/.bin/${NATIVE_BINARY_NAME}" --version`);

		// The same names again in the publish job, which only ever sees the artifact.
		expect(workflow).toContain(`test -s ${NATIVE_RELEASE_MANIFEST_NAME}`);
		expect(workflow).toContain(`test -s "${assetPrefix}$target.tar.gz"`);
		// `(?<!dist-)` keeps the build job's `cp dist-release/...` out of the count: the two
		// expected matches are the resume-draft upload and the create-draft argument list.
		const uploadedAssets = new RegExp(`(?<!dist-)release/${assetPrefix}\\*\\.tar\\.gz`, "g");
		const uploadedManifest = new RegExp(
			`(?<!dist-)release/${NATIVE_RELEASE_MANIFEST_NAME.replace(/\./g, "\\.")}`,
			"g",
		);
		expect(workflow.match(uploadedAssets)).toHaveLength(2);
		expect(workflow.match(uploadedManifest)).toHaveLength(2);

		// The GitHub release tag, and the tag listing that finds the previous one.
		expect(workflow).toContain(`tag="${releaseTagPrefix}$VERSION"`);
		expect(workflow).toContain(`git tag --list '${releaseTagPrefix}*'`);

		// The install one-liner in the release notes, and install.sh itself, must both
		// address this fork's repository and build the same download base as the CLI reader.
		expect(workflow).toContain(
			`https://raw.githubusercontent.com/${NATIVE_RELEASE_REPOSITORY}/main/install.sh`,
		);
		expect(installScript).toContain(
			`release_base="https://github.com/${NATIVE_RELEASE_REPOSITORY}/releases/download/${releaseTagPrefix}$version"`,
		);

		// Nothing may still carry the pre-rebrand release protocol.
		for (const surface of [workflow, installScript]) {
			expect(surface).not.toContain("clawdi-cli-");
		}
	});
});
