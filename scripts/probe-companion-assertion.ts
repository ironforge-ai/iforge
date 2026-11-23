/**
 * A2/A3 probe — settles plan 05-01 Task 4's <human-check> against a RUNNING filebrowser.
 *
 * Mints an `X-JWT-Assertion` through the SAME function the runtime executor uses
 * (`signCompanionAssertion`, so the probe cannot drift from the thing it is proving) and lists the
 * root of the declared source: `GET /api/resources?path=/&source=<FILE_BROWSER_SOURCE_NAME>`.
 * Both query parameters are mandatory — omitting either answers 500 before the permission check
 * runs, which is a status none of the verdicts below can read. Prints the HTTP status, which is
 * the whole answer:
 *
 *   200  -> A2 and A3 hold. `userDefaults.account.permissions.api:false` and
 *           `http.trustedHeaders:[]` are the shipped values AND the running binary still honours
 *           a JWT assertion. Plan 05-02 lands unchanged.
 *   401  -> the assertion was rejected. A3 is wrong: the binary is not reading the header.
 *   403  -> the assertion was accepted but the account is denied API access. A2 is wrong.
 *
 * 401 or 403 means `packages/cli/src/runtime/file-browser-companion.ts` needs a companion-config
 * change BEFORE 05-02 lands.
 *
 * Run on the provisioned host, beside the running companion:
 *
 *   IFORGE_COMPANION_UAT_BASE_URL=http://127.0.0.1:9120 \
 *     IFORGE_COMPANION_MANIFEST_PATH=/path/to/manifest.json \
 *     bun run scripts/probe-companion-assertion.ts
 *
 * Reads auth material from the host manifest. Point at a different manifest with
 * IFORGE_COMPANION_MANIFEST_PATH if it is not at the default runtime location.
 */

import { readFileSync } from "node:fs";

import { signCompanionAssertion } from "../packages/cli/src/runtime/companion-executor";
import { FILE_BROWSER_SOURCE_NAME } from "../packages/cli/src/runtime/file-browser-companion";
import { fileBrowserAuthSchema } from "../packages/cli/src/runtime/manifest-contract";

const COMPANION_KIND = "filebrowser";

function fail(message: string): never {
	console.error(`probe: ${message}`);
	process.exit(2);
}

const baseUrl = process.env.IFORGE_COMPANION_UAT_BASE_URL?.trim();
if (!baseUrl) {
	fail(
		"IFORGE_COMPANION_UAT_BASE_URL is unset. Set it to the operator's filebrowser instance, " +
			"normally http://127.0.0.1:9120 on the provisioned host.",
	);
}

const manifestPath = process.env.IFORGE_COMPANION_MANIFEST_PATH?.trim();
if (!manifestPath) {
	fail(
		"IFORGE_COMPANION_MANIFEST_PATH is unset. Point it at the host manifest JSON that carries " +
			`companions.${COMPANION_KIND}.auth.`,
	);
}

let manifest: unknown;
try {
	manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
} catch (error) {
	fail(`cannot read manifest at ${manifestPath}: ${(error as Error).message}`);
}

const rawAuth = (manifest as { companions?: Record<string, { auth?: unknown }> })?.companions?.[
	COMPANION_KIND
]?.auth;
if (!rawAuth) {
	fail(`manifest at ${manifestPath} has no companions.${COMPANION_KIND}.auth block`);
}

// Validate through the real schema: a manifest this probe would accept but the executor would
// reject proves nothing about the executor's path.
const parsed = fileBrowserAuthSchema.safeParse(rawAuth);
if (!parsed.success) {
	fail(
		`companions.${COMPANION_KIND}.auth does not satisfy fileBrowserAuthSchema:\n${parsed.error}`,
	);
}
const auth = parsed.data;

const assertion = signCompanionAssertion(auth, Math.floor(Date.now() / 1000));

// `path` AND `source` are both mandatory on this release: omitting either answers
// 500 "path not provided" / "source not provided" BEFORE the account permission
// check runs. A bare `/api/resources` therefore cannot settle A2 or A3 — it
// returns a status this probe's own vocabulary has no meaning for, which is what
// it did until a live instance was finally pointed at it. The root of the
// declared source is the smallest request that reaches the check.
const target = new URL("/api/resources", baseUrl);
target.searchParams.set("path", "/");
target.searchParams.set("source", FILE_BROWSER_SOURCE_NAME);
const response = await fetch(target, {
	method: "GET",
	headers: { [auth.header]: assertion },
	signal: AbortSignal.timeout(15_000),
});

const verdict =
	response.status === 200
		? "A2 and A3 HOLD — 05-02 lands unchanged."
		: response.status === 401
			? "A3 IS WRONG — the running binary is not reading the assertion header. " +
				"file-browser-companion.ts needs a companion-config change before 05-02."
			: response.status === 403
				? "A2 IS WRONG — the assertion was accepted but the account is denied API access. " +
					"file-browser-companion.ts needs a companion-config change before 05-02."
				: "UNEXPECTED — neither the pass nor either documented failure. Record the status verbatim.";

console.log(`base url : ${baseUrl}`);
console.log(`subject  : ${auth.subject}`);
console.log(`audience : ${auth.audience}`);
console.log(`group    : ${auth.requiredGroup}`);
console.log(`status   : ${response.status} ${response.statusText}`);
console.log(`verdict  : ${verdict}`);

// Non-zero on a documented failure so a CI-style caller notices without reading prose.
process.exit(response.status === 200 ? 0 : 1);
