import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { DESTINATIONS } from "./destinations";

const APEX = "iforge.dev";

const REPO_ROOT = join(import.meta.dir, "..", "..", "..");

/**
 * Upstream's apex, spelled out deliberately as red-case data for the assertions below.
 *
 * Every file this suite reads is a place NOTHING in CI ever executes — a bundler define
 * flag, a native build script, a package manifest script, a shell default, a workflow
 * env value and a Kamal config. A presence-only assertion passes a file that still names
 * BOTH hosts, so each contract below checks both directions: the authority's value is
 * present, and no upstream host survives anywhere in the file.
 *
 * `packages/shared/src` sits outside `scripts/check_rebrand_residual.py`'s SCAN_PATHSPEC,
 * so this literal is not a residual hit and does not depend on any allowlist entry.
 */
const UPSTREAM_APEX = "clawdi.ai";

function readRepoFile(relativePath: string): string {
	return readFileSync(join(REPO_ROOT, relativePath), "utf8");
}

function expectNoUpstreamHost(relativePath: string): void {
	const text = readRepoFile(relativePath);
	const offenders = text
		.split("\n")
		.map((line, index) => ({ line, number: index + 1 }))
		.filter((entry) => entry.line.includes(UPSTREAM_APEX))
		.map((entry) => `${relativePath}:${entry.number}: ${entry.line.trim()}`);
	expect(offenders).toEqual([]);
}

/** Every member that is spelled as a URL, fork-owned or third-party. */
const URL_MEMBERS = [
	"webOrigin",
	"wwwOrigin",
	"dashboardOrigin",
	"cloudApiOrigin",
	"deployApiOrigin",
	"assetsOrigin",
	"iconBase",
	"docsUrl",
	"githubUrl",
] as const;

/** The subset that must live under the apex domain this fork controls. */
const FORK_OWNED_URL_MEMBERS = [
	"webOrigin",
	"wwwOrigin",
	"dashboardOrigin",
	"cloudApiOrigin",
	"deployApiOrigin",
	"assetsOrigin",
	"iconBase",
] as const;

function hostOf(member: (typeof URL_MEMBERS)[number]): string {
	return new URL(DESTINATIONS[member]).hostname;
}

function isApexOrSubdomain(hostname: string, apex: string): boolean {
	return hostname === apex || hostname.endsWith(`.${apex}`);
}

describe("DESTINATIONS", () => {
	test("exposes the whole destination family as named members", () => {
		expect(DESTINATIONS.apexDomain).toBe(APEX);
		expect(DESTINATIONS.webOrigin).toBe("https://iforge.dev");
		expect(DESTINATIONS.wwwOrigin).toBe("https://www.iforge.dev");
		expect(DESTINATIONS.dashboardOrigin).toBe("https://cloud.iforge.dev");
		expect(DESTINATIONS.cloudApiOrigin).toBe("https://cloud-api.iforge.dev");
		expect(DESTINATIONS.deployApiOrigin).toBe("https://api.iforge.dev");
		expect(DESTINATIONS.assetsOrigin).toBe("https://assets.iforge.dev");
		expect(DESTINATIONS.iconBase).toBe("https://assets.iforge.dev/icons");
		expect(DESTINATIONS.supportEmail).toBe("support@iforge.dev");
		expect(DESTINATIONS.docsUrl).toBe("https://deepwiki.com/ironforge-ai/iforge");
		expect(DESTINATIONS.githubUrl).toBe("https://github.com/ironforge-ai/iforge");
		expect(DESTINATIONS.githubOrg).toBe("ironforge-ai");
		expect(DESTINATIONS.npmScope).toBe("@iforge");

		// This fork owns no Telegram destination and is not creating one (P33-23, settled by
		// the operator in plan 03.3-07). The Help-menu item was removed rather than repointed:
		// a link to a community that does not exist is worse than no link.
		//
		// The key must be PRESENT and explicitly null — an omitted key is indistinguishable
		// from an oversight, and this is a decision. Setting it again is one half of restoring
		// the menu item; `apps/web/src/lib/web-destinations-contract.test.ts` pins the other.
		expect("telegramUrl" in DESTINATIONS).toBe(true);
		expect(DESTINATIONS.telegramUrl).toBeNull();

		expect(Object.isFrozen(DESTINATIONS)).toBe(true);
	});

	test("every URL member is absolute HTTPS with no credentials, query or fragment", () => {
		for (const member of URL_MEMBERS) {
			const raw = DESTINATIONS[member];
			expect(typeof raw).toBe("string");
			const url = new URL(raw);
			expect(`${member}:${url.protocol}`).toBe(`${member}:https:`);
			expect(`${member}:${url.username}`).toBe(`${member}:`);
			expect(`${member}:${url.password}`).toBe(`${member}:`);
			expect(`${member}:${url.search}`).toBe(`${member}:`);
			expect(`${member}:${url.hash}`).toBe(`${member}:`);
			expect(raw).not.toContain("@");
			expect(raw).not.toContain("?");
			expect(raw).not.toContain("#");
			// Origin-shaped members carry no trailing slash: the canonical origin form is
			// what `isProductionCloudApiOrigin`'s exact `===` compares against.
			expect(`${member}:${raw.endsWith("/")}`).toBe(`${member}:false`);
		}
	});

	test("every fork-owned host is the apex domain or a subdomain of it", () => {
		for (const member of FORK_OWNED_URL_MEMBERS) {
			const hostname = hostOf(member);
			expect(`${member}:${isApexOrSubdomain(hostname, DESTINATIONS.apexDomain)}`).toBe(
				`${member}:true`,
			);
		}
	});

	test("a host that merely ends with the apex domain does not satisfy origin equality", () => {
		const impostors = [
			"https://evil-iforge.dev",
			"https://notiforge.dev",
			"https://xcloud-api.iforge.dev.attacker.example",
		];

		for (const impostor of impostors) {
			expect(impostor === DESTINATIONS.cloudApiOrigin).toBe(false);
			expect(impostor === DESTINATIONS.deployApiOrigin).toBe(false);
			expect(impostor === DESTINATIONS.webOrigin).toBe(false);
		}

		// The trap this test exists for: a naive suffix test accepts the first two.
		// Exact equality is not a suffix test, and this asserts the difference is real.
		expect(new URL(impostors[0] as string).hostname.endsWith(APEX)).toBe(true);
		expect(new URL(impostors[1] as string).hostname.endsWith(APEX)).toBe(true);
		expect(isApexOrSubdomain(new URL(impostors[0] as string).hostname, APEX)).toBe(false);
		expect(isApexOrSubdomain(new URL(impostors[1] as string).hostname, APEX)).toBe(false);
	});

	test("an origin carrying a path, query or fragment does not satisfy origin equality", () => {
		const decorated = [
			`${DESTINATIONS.cloudApiOrigin}/`,
			`${DESTINATIONS.cloudApiOrigin}/v1`,
			`${DESTINATIONS.cloudApiOrigin}?token=1`,
			`${DESTINATIONS.cloudApiOrigin}#frag`,
		];
		for (const candidate of decorated) {
			expect(candidate === DESTINATIONS.cloudApiOrigin).toBe(false);
		}
	});

	test("destination matching is case-sensitive ASCII", () => {
		expect(DESTINATIONS.cloudApiOrigin.toUpperCase() === DESTINATIONS.cloudApiOrigin).toBe(false);
		// Widened to `string` deliberately: the members carry literal types, and TS2367
		// would reject a literal-to-literal comparison the runtime still needs asserted.
		const uppercaseSpelling: string = "https://CLOUD-API.IFORGE.DEV";
		expect(uppercaseSpelling === DESTINATIONS.cloudApiOrigin).toBe(false);
		expect(DESTINATIONS.supportEmail.toUpperCase() === DESTINATIONS.supportEmail).toBe(false);
		// The product name is lowercase everywhere except the SCREAMING_SNAKE environment
		// register, which this module does not hold.
		for (const member of URL_MEMBERS) {
			expect(`${member}:${DESTINATIONS[member]}`).toBe(
				`${member}:${DESTINATIONS[member].toLowerCase()}`,
			);
		}
	});

	test("no member names a host outside the declared domain family", () => {
		const declaredExternal = [...DESTINATIONS.externalHosts];
		expect(declaredExternal.length).toBeGreaterThan(0);

		const unusedDeclarations = new Set(declaredExternal);
		for (const member of URL_MEMBERS) {
			const hostname = hostOf(member);
			const covered =
				isApexOrSubdomain(hostname, DESTINATIONS.apexDomain) || declaredExternal.includes(hostname);
			expect(`${member}:${hostname}:${covered}`).toBe(`${member}:${hostname}:true`);
			unusedDeclarations.delete(hostname);
		}

		// An external host declared but never used would let the list drift into fiction.
		expect([...unusedDeclarations]).toEqual([]);

		// The support address is an email, not a URL, and it lives under the apex too.
		const emailHost = DESTINATIONS.supportEmail.split("@")[1] as string;
		expect(isApexOrSubdomain(emailHost, DESTINATIONS.apexDomain)).toBe(true);
	});
});

/**
 * Contract assertions over the places a SHIPPED artifact bakes an origin in.
 *
 * None of these files can import a TypeScript module, and no CI job executes any of
 * them, so a text assertion here is the only signal that can fail when one of them
 * drifts away from the authority.
 */
describe("baked-in build defaults agree with the authority", () => {
	test("the CLI bundler define flags name both fork-owned origins", () => {
		const relativePath = "packages/cli/package.json";
		const manifest = JSON.parse(readRepoFile(relativePath)) as {
			scripts: Record<string, string>;
		};
		const build = manifest.scripts.build as string;

		// Two distinct origins compiled into the same build line (P33-13, P33-24).
		expect(build).toContain(`process.env.CLAWDI_DEFAULT_API_URL="${DESTINATIONS.cloudApiOrigin}"`);
		expect(build).toContain(
			`process.env.CLAWDI_DEFAULT_DEPLOY_API_URL="${DESTINATIONS.deployApiOrigin}"`,
		);
		expectNoUpstreamHost(relativePath);
	});

	test("the native build script names both fork-owned origins", () => {
		// This is the native-binary path — the worst place in the repository to read one
		// member and not the other, which is the asymmetry that let the deploy default go
		// ungated in the first place. Each fallback is asserted against its OWN member.
		const relativePath = "packages/cli/scripts/build-native.mjs";
		const source = readRepoFile(relativePath);

		expect(source).toContain(
			`const defaultApiUrl = process.env.CLAWDI_DEFAULT_API_URL || "${DESTINATIONS.cloudApiOrigin}";`,
		);
		expect(source).toContain(
			`const defaultDeployApiUrl = process.env.CLAWDI_DEFAULT_DEPLOY_API_URL || "${DESTINATIONS.deployApiOrigin}";`,
		);
		expectNoUpstreamHost(relativePath);
	});

	test("the web app's live deploy-API generator names the authority's deploy origin", () => {
		const relativePath = "apps/web/package.json";
		const manifest = JSON.parse(readRepoFile(relativePath)) as {
			scripts: Record<string, string>;
		};

		expect(manifest.scripts["generate-deploy-api:live"]).toContain(
			`DEPLOY_OPENAPI_SOURCE=${DESTINATIONS.deployApiOrigin}/openapi.json`,
		);
		expectNoUpstreamHost(relativePath);
	});

	test("the deploy-contract check script's spec_source default names the authority", () => {
		// `bash -n` proves only that this script parses; it can never say which host it names.
		const relativePath = "scripts/check-deploy-generated-api.sh";
		const source = readRepoFile(relativePath);

		expect(source).toContain(
			`spec_source="\${DEPLOY_OPENAPI_SOURCE:-${DESTINATIONS.deployApiOrigin}/openapi.json}"`,
		);
		expectNoUpstreamHost(relativePath);
	});

	test("both CI workflow deploy-OpenAPI sources name the authority", () => {
		// GitHub Actions never execute against this origin, so this assertion is the only
		// signal that exists for anything under the workflows directory.
		const relativePath = ".github/workflows/client-ci.yml";
		const source = readRepoFile(relativePath);
		const expected = `DEPLOY_OPENAPI_SOURCE: ${DESTINATIONS.deployApiOrigin}/openapi.json`;

		expect(source.split(expected).length - 1).toBe(2);
		expectNoUpstreamHost(relativePath);
	});
});

/**
 * The desktop app (06.1-14, D-08/D-23). Its main process exposes a privileged preload bridge
 * to whatever origin DASHBOARD_ORIGIN names and hands the bundled CLI its production API
 * origins, so each must come from the authority and no upstream host may survive beside it
 * (T-06.1-22).
 */
describe("the desktop app reaches only the authority's hosts", () => {
	const DESKTOP_SOURCES = [
		"apps/desktop/src/main.ts",
		"apps/desktop/src/native-cli.ts",
		"apps/desktop/src/shell-preload.ts",
		"apps/desktop/scripts/smoke-packaged.mjs",
		"apps/desktop/scripts/fake-native-smoke.sh",
		"apps/desktop/README.md",
		"apps/desktop/package.json",
	];

	test("no upstream host survives in the desktop sources that named one", () => {
		for (const relativePath of DESKTOP_SOURCES) expectNoUpstreamHost(relativePath);
	});

	test("the dashboard origin and both API origins are read from DESTINATIONS", () => {
		expect(readRepoFile("apps/desktop/src/main.ts")).toContain(
			"const DASHBOARD_ORIGIN = DESTINATIONS.dashboardOrigin;",
		);
		expect(readRepoFile("apps/desktop/src/shell-preload.ts")).toContain(
			"location.origin === DESTINATIONS.dashboardOrigin",
		);
		const nativeCli = readRepoFile("apps/desktop/src/native-cli.ts");
		expect(nativeCli).toContain("const PRODUCTION_CLOUD_API_URL = DESTINATIONS.cloudApiOrigin;");
		expect(nativeCli).toContain("const PRODUCTION_DEPLOY_API_URL = DESTINATIONS.deployApiOrigin;");
	});

	test("the packaged identity names the authority's homepage and support contact", () => {
		const manifest = JSON.parse(readRepoFile("apps/desktop/package.json")) as {
			homepage: string;
			build: { linux: { maintainer: string } };
		};
		expect(manifest.homepage).toBe(DESTINATIONS.webOrigin);
		expect(manifest.build.linux.maintainer).toContain(`<${DESTINATIONS.supportEmail}>`);
	});
});

/**
 * The Kamal deploy config. No CI job renders or executes it, and 03.1-06 recorded that
 * `kamal` itself is unavailable on this host, so this contract test is the ONLY signal
 * that exists for these four destination groups.
 */
describe("config/deploy.yml agrees with the authority", () => {
	const DEPLOY_CONFIG = "config/deploy.yml";

	test("the CORS origin family matches the authority exactly, in order", () => {
		const source = readRepoFile(DEPLOY_CONFIG);
		const match = source.match(/^\s*CORS_ORIGINS: '(.*)'$/m);
		expect(match).not.toBeNull();

		const raw = (match as RegExpMatchArray)[1] as string;
		// Still a JSON array of strings: the backend parses this scalar, and nothing in CI
		// would catch a malformed value here.
		const parsed = JSON.parse(raw) as unknown;
		expect(Array.isArray(parsed)).toBe(true);
		for (const member of parsed as unknown[]) {
			expect(typeof member).toBe("string");
		}

		// Order is asserted, not just membership: a partial move would leave an upstream
		// origin allowed to call this fork's API (T-03.3-25).
		expect(parsed).toEqual([...DESTINATIONS.corsOrigins]);
	});

	test("the proxy host, public API URL and web origin match the authority", () => {
		const source = readRepoFile(DEPLOY_CONFIG);

		// The proxy host is a bare hostname, not an origin.
		expect(source).toContain(`    - ${new URL(DESTINATIONS.cloudApiOrigin).hostname}\n`);
		expect(source).toContain(`PUBLIC_API_URL: ${DESTINATIONS.cloudApiOrigin}`);
		// WEB_ORIGIN is the DASHBOARD, not the apex homepage: the CLI device-flow
		// verification_uri is built from it.
		expect(source).toContain(`WEB_ORIGIN: ${DESTINATIONS.dashboardOrigin}`);
	});

	test("no upstream host survives anywhere in the deploy config", () => {
		// Both directions: presence alone would pass a file that still names both.
		expectNoUpstreamHost(DEPLOY_CONFIG);
	});

	test("the documented example environment files name fork-owned production values", () => {
		expectNoUpstreamHost("apps/web/.env.example");
		expectNoUpstreamHost("backend/.env.example");
		expect(readRepoFile("apps/web/.env.example")).toContain(DESTINATIONS.cloudApiOrigin);
		expect(readRepoFile("apps/web/.env.example")).toContain(DESTINATIONS.deployApiOrigin);
		expect(readRepoFile("backend/.env.example")).toContain(DESTINATIONS.dashboardOrigin);
	});
});
