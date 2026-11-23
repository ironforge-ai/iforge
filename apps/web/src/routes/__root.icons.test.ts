import { describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

// This suite asserts on FILES, not on rendered markup. The browser chrome is a
// set of static assets referenced by a route file and a manifest, and the way
// that set breaks is a link pointing at a path that is not there, or an asset
// that was renamed rather than redrawn. Neither is visible to a render test.

const WEB_ROOT = resolve(import.meta.dir, "../..");
const REPO_ROOT = resolve(WEB_ROOT, "../..");
const PUBLIC_DIR = join(WEB_ROOT, "public");
const ROUTE_FILE = join(WEB_ROOT, "src/routes/__root.tsx");
const MANIFEST_FILE = join(PUBLIC_DIR, "site.webmanifest");
const THEME_FILE = join(REPO_ROOT, "packages/shared/src/style/theme.css");

/** The six shipped asset paths. P33-18 retired the other two. */
const SHIPPED_ASSETS = [
	"favicon.ico",
	"iforge.svg",
	"apple-touch-icon.png",
	"android-chrome-192x192.png",
	"android-chrome-512x512.png",
	"iforge-logo-transparent.png",
] as const;

/** Retired by P33-18: the favicon container already carries 16 and 32. */
const RETIRED_ASSETS = ["favicon-16x16.png", "favicon-32x32.png"] as const;

/**
 * sha256 of every asset as it stood before the redraw, measured 2026-09-09.
 * A surviving digest means that asset was renamed rather than redrawn, which
 * is the exact defect this phase exists to close (WINDOWS #36). Note that
 * android-chrome-512x512 and iforge-logo-transparent shared one digest: eight
 * paths were only seven distinct files.
 */
const PRE_PHASE_DIGESTS: Readonly<Record<string, string>> = {
	"favicon.ico": "6cab4860abcdf51f384629397e9495c32aa6eafb7e26515e4c92664fdcf5b1e3",
	"iforge.svg": "2ae2c8dd4670c0556acb909af467b856a4830aa073a9271a1f95a108b8e89d58",
	"apple-touch-icon.png": "6b9da0271bfa03611fb712bbf8bc15b7805afe53b5c477b2ee0b268084ac998a",
	"android-chrome-192x192.png": "272f22c9802581716ebd1d71d149f22454dabf42567a24eeddc34ceca25ca12f",
	"android-chrome-512x512.png": "9c106126fb2a09c368222c70b96443236996f6ad81d52dc496527f8ae5b6aa32",
	"iforge-logo-transparent.png": "9c106126fb2a09c368222c70b96443236996f6ad81d52dc496527f8ae5b6aa32",
	"favicon-16x16.png": "227034e867e18bfc3271a2c8f5d9f3f6a458a7920fd16049d717d82076a3875c",
	"favicon-32x32.png": "108b079268db777769ef70da2bfefa326898762c912db30d81653e2bb5f13aa8",
};

function digestOf(assetName: string): string {
	return createHash("sha256")
		.update(readFileSync(join(PUBLIC_DIR, assetName)))
		.digest("hex");
}

/**
 * Pull the `links:` array out of the route source and return every `href`.
 * The head is a literal in a TanStack route definition, so reading the source
 * is the honest way to ask what it points at without booting a router.
 */
function headLinkHrefs(): string[] {
	const source = readFileSync(ROUTE_FILE, "utf8");
	const start = source.indexOf("links: [");
	expect(start).toBeGreaterThan(-1);
	let depth = 0;
	let end = -1;
	for (let i = source.indexOf("[", start); i < source.length; i++) {
		if (source[i] === "[") depth++;
		else if (source[i] === "]") {
			depth--;
			if (depth === 0) {
				end = i;
				break;
			}
		}
	}
	expect(end).toBeGreaterThan(start);
	const block = source.slice(start, end);
	return [...block.matchAll(/href:\s*"([^"]+)"/g)].map((match) => match[1]);
}

type ManifestIcon = { src: string; sizes: string; type: string; purpose?: string };
type Manifest = { icons: ManifestIcon[]; theme_color: string; background_color: string };

function readManifest(): Manifest {
	return JSON.parse(readFileSync(MANIFEST_FILE, "utf8")) as Manifest;
}

/**
 * Read a light-mode token from the committed stylesheet. Only the `:root`
 * block is searched, so a `.dark` override of the same name cannot be picked
 * up by accident.
 */
function lightModeToken(name: string): { l: number; c: number; h: number } {
	const css = readFileSync(THEME_FILE, "utf8");
	const rootStart = css.indexOf(":root {");
	expect(rootStart).toBeGreaterThan(-1);
	const rootEnd = css.indexOf("\n}", rootStart);
	expect(rootEnd).toBeGreaterThan(rootStart);
	const rootBlock = css.slice(rootStart, rootEnd);
	const match = rootBlock.match(
		new RegExp(`--${name}:\\s*oklch\\(([\\d.]+)\\s+([\\d.]+)\\s+([\\d.]+)\\)`),
	);
	if (!match) throw new Error(`--${name} not found as an oklch() value in the :root block`);
	return { l: Number(match[1]), c: Number(match[2]), h: Number(match[3]) };
}

/** OKLCH to sRGB hex, so the manifest's literals can be checked rather than trusted. */
function oklchToHex({ l: L, c: C, h: H }: { l: number; c: number; h: number }): string {
	const rad = (H * Math.PI) / 180;
	const a = C * Math.cos(rad);
	const b = C * Math.sin(rad);
	const long = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
	const medium = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
	const short = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
	const linear = [
		4.0767416621 * long - 3.3077115913 * medium + 0.2309699292 * short,
		-1.2684380046 * long + 2.6097574011 * medium - 0.3413193965 * short,
		-0.0041960863 * long - 0.7034186147 * medium + 1.707614701 * short,
	];
	const channels = linear.map((channel) => {
		const encoded = channel <= 0.0031308 ? 12.92 * channel : 1.055 * channel ** (1 / 2.4) - 0.055;
		return Math.round(Math.min(1, Math.max(0, encoded)) * 255);
	});
	return `#${channels.map((value) => value.toString(16).padStart(2, "0")).join("")}`;
}

describe("browser chrome asset set", () => {
	test("every shipped asset path exists, and the two retired paths do not", () => {
		for (const asset of SHIPPED_ASSETS) {
			expect(existsSync(join(PUBLIC_DIR, asset))).toBe(true);
		}
		for (const asset of RETIRED_ASSETS) {
			expect(existsSync(join(PUBLIC_DIR, asset))).toBe(false);
		}
	});

	test("no shipped asset still carries the bytes it replaced", () => {
		const retiredDigests = new Set(Object.values(PRE_PHASE_DIGESTS));
		for (const asset of SHIPPED_ASSETS) {
			expect(retiredDigests.has(digestOf(asset))).toBe(false);
		}
	});

	test("no two shipped assets share a digest", () => {
		const digests = SHIPPED_ASSETS.map(digestOf);
		expect(new Set(digests).size).toBe(SHIPPED_ASSETS.length);
	});
});

describe("head and manifest agree with the shipped set", () => {
	test("every head link resolves to a file under public/", () => {
		const hrefs = headLinkHrefs();
		expect(hrefs.length).toBeGreaterThan(0);
		for (const href of hrefs) {
			expect(href.startsWith("/")).toBe(true);
			expect(existsSync(join(PUBLIC_DIR, href.slice(1)))).toBe(true);
		}
	});

	test("the vector ships as an icon link with its media type, before the container, with no sizes", () => {
		const source = readFileSync(ROUTE_FILE, "utf8");
		expect(source).toContain('{ rel: "icon", href: "/iforge.svg", type: "image/svg+xml" }');
		expect(source).toContain('{ rel: "icon", href: "/favicon.ico" }');
		expect(source.indexOf('href: "/iforge.svg"')).toBeLessThan(
			source.indexOf('href: "/favicon.ico"'),
		);
		// Neither icon entry carries a sizes attribute, and none was inherited
		// from the raster links P33-18 removed.
		expect(headLinkHrefs()).not.toContain("/favicon-16x16.png");
		expect(headLinkHrefs()).not.toContain("/favicon-32x32.png");
		const linksBlock = source.slice(source.indexOf("links: ["), source.indexOf("}),"));
		expect(linksBlock).not.toContain("sizes:");
	});

	test("every manifest icon resolves to a file under public/, listed smallest first", () => {
		const { icons } = readManifest();
		expect(icons.length).toBe(2);
		for (const icon of icons) {
			expect(icon.src.startsWith("/")).toBe(true);
			expect(existsSync(join(PUBLIC_DIR, icon.src.slice(1)))).toBe(true);
		}
		const widths = icons.map((icon) => Number(icon.sizes.split("x")[0]));
		expect(widths).toEqual([...widths].sort((a, b) => a - b));
	});

	test("the manifest declares no maskable purpose", () => {
		// Declaring an unpadded raster maskable is the pre-existing defect this
		// redraw must not re-introduce (P33-19). Adding a padded maskable asset
		// would make a seventh path and needs MIGR-04's count re-amended first.
		expect(readFileSync(MANIFEST_FILE, "utf8")).not.toContain("maskable");
		for (const icon of readManifest().icons) {
			expect(icon.purpose).toBeUndefined();
		}
	});

	test("the manifest colours are the sRGB of the committed light-mode tokens", () => {
		// A manifest cannot read a stylesheet token, so these two literals are a
		// duplication by necessity. This is what makes it a CHECKED duplication.
		const manifest = readManifest();
		expect(manifest.theme_color).toBe(oklchToHex(lightModeToken("primary")));
		expect(manifest.background_color).toBe(oklchToHex(lightModeToken("background")));
	});
});
