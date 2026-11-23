#!/usr/bin/env bun
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
	NATIVE_RELEASE_MANIFEST_NAME,
	parseNativeReleaseManifest,
} from "../src/lib/native-release-manifest.ts";
import { validateNativePublicationArchive } from "./native-publication.mjs";

// The repository root, resolved from this module's own location rather than from the caller's
// working directory: the gate below must find the same guard whichever directory the release was
// started from, and a cwd-relative path would silently not-run from anywhere but the repo root.
const repoRoot = resolve(fileURLToPath(new URL("../../..", import.meta.url)));

// The origin gate runs FIRST, ahead of the manifest parse.
//
// `packages/shared/src/destinations.ts` is compiled into every artifact this script verifies,
// and the cloud API origin it names is where a shipped binary sends a user's bearer token. A
// build whose baked-in origin does not resolve, or whose origin never completes a TLS handshake,
// sends that credential into a connection whose far end is not under this fork's control — and a
// failed handshake is not a refusal to send. So the question "do these hosts answer" is asked
// before the checksums, not after: there is no point verifying the integrity of an archive that
// must not ship.
//
// There is deliberately no way to turn this off. A bypass would be used exactly once, in the
// situation the gate exists to prevent. If a genuine offline-verification need appears later,
// that is a decision for whoever has it, made visibly in a commit.
//
// This is WINDOWS #88.
const gate = spawnSync("python3", ["scripts/check_production_origins.py", "--json"], {
	cwd: repoRoot,
	encoding: "utf8",
});
if (gate.error) {
	throw new Error(`could not run the production origin gate: ${gate.error.message}`);
}
let gateReport;
try {
	gateReport = JSON.parse(gate.stdout);
} catch {
	throw new Error(
		`the production origin gate produced no JSON report (exit ${gate.status}): ${
			gate.stderr?.trim() || gate.stdout?.trim() || "no output"
		}`,
	);
}
if (gate.status !== 0) {
	const unserved = gateReport.origins
		.filter((origin) => origin.verdict !== "SERVED")
		.map((origin) => `${origin.host} (${origin.verdict})`);
	const problems = [...unserved];
	if (gateReport.mail.verdict !== "MAIL") {
		problems.push(`${gateReport.mail.host} (${gateReport.mail.verdict})`);
	}
	throw new Error(
		`refusing to verify a native release: ${problems.join(", ")}. ${gateReport.summary}. ` +
			"See docs/runbooks/iforge-production-dns-tls.md (WINDOWS #88).",
	);
}

const releaseDir = resolve(process.argv[2] || "dist-release");
const expectedVersion = process.argv[3] || JSON.parse(readFileSync("package.json", "utf8")).version;
const manifest = parseNativeReleaseManifest(
	readFileSync(resolve(releaseDir, NATIVE_RELEASE_MANIFEST_NAME), "utf8"),
);
if (manifest.version !== expectedVersion) throw new Error("native release version mismatch");

for (const artifact of manifest.artifacts) {
	const path = resolve(releaseDir, artifact.asset);
	const archive = readFileSync(path);
	const actual = createHash("sha256").update(archive).digest("hex");
	if (actual !== artifact.sha256) throw new Error(`checksum mismatch for ${artifact.asset}`);
	await validateNativePublicationArchive(archive);
}

console.log(`verified native release ${manifest.version} (${manifest.artifacts.length} targets)`);
