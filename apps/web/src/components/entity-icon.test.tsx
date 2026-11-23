import { describe, expect, test } from "bun:test";
import { DESTINATIONS } from "@clawdi/shared";
import { renderToStaticMarkup } from "react-dom/server";
import { FRAMEWORK_BRAND_ICON_IDS } from "@/components/entity-brand-icon-ids";
import { frameworkBrandIcon } from "@/components/entity-brand-icons";
import { EntityIcon } from "@/components/entity-icon";

const SIZES = ["sm", "md", "lg"] as const;

// Per-framework provenance. `ironhermes` is the first-party mark: it is the only
// framework sourced from this repo. Its larger 85% scale comes from its registry
// entry, like every vendor mark's.
const FRAMEWORK_ICON_SOURCES: Readonly<Record<string, string>> = {
	openclaw: "lobehub",
	hermes: "lobehub",
	"claude-code": "lobehub",
	codex: "lobehub",
	pi: "lobehub",
	opencode: "lobehub",
	ironhermes: "clawdi",
};

describe("EntityIcon brand marks", () => {
	test("uses the shared larger mark without clipping framework or provider tiles", () => {
		for (const kindAndId of [
			...FRAMEWORK_BRAND_ICON_IDS.map((id) => ({ kind: "framework" as const, id })),
			{ kind: "provider" as const, id: "openai" },
		]) {
			for (const size of SIZES) {
				const markup = renderToStaticMarkup(<EntityIcon {...kindAndId} size={size} />);
				const expectedIconSource =
					kindAndId.kind === "provider" ? "lobehub" : FRAMEWORK_ICON_SOURCES[kindAndId.id];
				expect(markup).toContain(`data-icon-source="${expectedIconSource}"`);
				const expectedIconSize =
					kindAndId.kind === "provider"
						? "84%"
						: `${(frameworkBrandIcon(kindAndId.id)?.iconScale ?? 0.84) * 100}%`;
				expect(markup).toContain(`width="${expectedIconSize}"`);
				expect(markup).toContain(`height="${expectedIconSize}"`);
			}
		}
	});

	test("keeps the first-party IronHermes mark off the Hermes plate at every entity size", () => {
		for (const size of SIZES) {
			const markup = renderToStaticMarkup(
				<EntityIcon kind="framework" id="ironhermes" size={size} />,
			);
			expect(markup).toContain('data-icon-source="clawdi"');
			expect(markup).toContain("<svg");
			expect(markup).not.toContain("bg-white");
			expect(markup).not.toContain("text-black");
			expect(markup).not.toContain("<img");
		}
	});

	test("keeps unknown frameworks on the existing monogram fallback", () => {
		const markup = renderToStaticMarkup(
			<EntityIcon kind="framework" id="custom-agent" label="Custom agent" size="sm" />,
		);
		expect(markup).toContain(">C</div>");
		expect(markup).not.toContain("<svg");
		expect(markup).not.toContain("<img");
	});
});

/*
 * Channels render a monogram and fetch nothing (P33-22, plan 03.3-07).
 *
 * These assertions are about WHAT RENDERS for a KNOWN channel, deliberately —
 * asserting that a constant changed would have passed just as happily against
 * an `ICON_BASE` pointing at a host with no DNS record, which is the exact
 * mistake the plan's precondition existed to prevent.
 *
 * The behaviour being pinned: the old channel branch emitted an `<img>` with no
 * `onError`, so a KNOWN channel whose file 404s had NO fallback — the monogram
 * below is reached on an unresolved id, never on a failed load. That hazard is
 * gone only because nothing is fetched at all now, and "nothing is fetched" is
 * what the last test here actually measures.
 */
const CHANNEL_IDS = ["telegram", "discord", "whatsapp", "slack"] as const;

/** The label's initial, which is what `NeutralMonogram` renders. */
const CHANNEL_MONOGRAM: Readonly<Record<string, string>> = {
	telegram: "T",
	discord: "D",
	whatsapp: "W",
	slack: "S",
};

describe("EntityIcon channels", () => {
	test("renders the neutral monogram tile for every supported channel", () => {
		for (const id of CHANNEL_IDS) {
			for (const size of SIZES) {
				const markup = renderToStaticMarkup(
					<EntityIcon kind="channel" id={id} label={id} size={size} />,
				);
				// The monogram is decorative: a labelled tile, not an image.
				expect(markup).toContain('aria-hidden="true"');
				expect(markup).toContain("bg-muted");
				expect(markup).toContain(`>${CHANNEL_MONOGRAM[id]}</span>`);
				expect(markup).not.toContain("<img");
				expect(markup).not.toContain("<svg");
			}
		}
	});

	test("a known channel issues no request — no img, and no eager preload", () => {
		for (const id of CHANNEL_IDS) {
			const markup = renderToStaticMarkup(
				<EntityIcon kind="channel" id={id} label={id} size="lg" />,
			);
			// React emits `<link rel="preload" as="image">` for an <img> src, so a
			// surviving remote source leaks at head-processing time, before paint.
			expect(markup).not.toContain('rel="preload"');
			expect(markup).not.toContain("<img");
		}
	});

	test("no channel source resolves to a host outside the authority's domain family", () => {
		const permitted = [DESTINATIONS.apexDomain, ...DESTINATIONS.externalHosts];
		for (const id of CHANNEL_IDS) {
			const markup = renderToStaticMarkup(
				<EntityIcon kind="channel" id={id} label={id} size="lg" />,
			);
			// Strongest available form: a channel render emits NO absolute URL at
			// all, so there is no host to compare — vacuously inside any family,
			// and necessarily never upstream's asset CDN again. This subsumes an
			// assertion naming that host, which is why none appears here: a
			// shipped file must not spell it (03.3 residual guard).
			const urls = markup.match(/https?:\/\/[^"'\s]+/g) ?? [];
			expect(urls).toEqual([]);
			// Guards the claim above against a future edit that reintroduces a
			// source: any URL that appears must sit in the authority's family.
			for (const url of urls) {
				expect(permitted.some((host) => new URL(url).hostname.endsWith(host))).toBe(true);
			}
		}
	});
});
