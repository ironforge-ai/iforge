import type { SVGProps } from "react";

/**
 * Traced from `./ironhermes-source.png`, the winged emblem the user approved during
 * `/gsd-ui-phase 2`: a faceted low-poly pair of wings sweeping up and outward from a
 * central downward-pointing chevron.
 *
 * Two deliberate departures from the source, both required by the 16px floor of the
 * `AgentIconSize` ladder:
 *
 * 1. **Two feather facets per wing, not the source's six to seven.** Each wing is the
 *    solid swept mass of the source with a single notch cut into its trailing edge. Three
 *    facets were built and rejected: at 16px one viewBox unit is 0.57 device pixels, and
 *    the second notch closed into a ragged edge rather than reading as a gap. Two facets
 *    that read beat three that merge, so the notch here stays wide enough to survive.
 * 2. **Monochrome.** The source is a copper-to-orange gradient with specular facet
 *    highlights. The mark inherits `text-foreground` from its tile and carries no colour
 *    of its own — iforge permits exactly one saturated brand colour and it is the accent,
 *    which is reserved for selected state. An accent-coloured runtime mark would read as
 *    *selected* in every list it appears in.
 *
 * Geometry: horizontally full-bleed and vertically centred in a 24x24 box (x spans 0-24,
 * y spans 5.2-18.8 about the 12 midline). Proportions are measured off the source: wing
 * tips at the upper outer corners, a shallow leading edge dropping to the wing shoulders,
 * a slot between the shoulders descending to the chevron's peak, and the chevron pointing
 * down well past the wing roots.
 */
const IRONHERMES_MARK_PATH = [
	"M0 5.2", // left wing tip
	"L9.2 7.3", // left wing shoulder — inboard end of the leading edge
	"L12 11.7", // chevron peak, at the bottom of the slot between the shoulders
	"L14.8 7.3", // right wing shoulder
	"L24 5.2", // right wing tip
	"L19.4 9.6", // right notch, dividing the two feather facets
	"L20.9 13.4", // right lower feather tip
	"L14.9 13.6", // right wing root == chevron right vertex
	"L12 18.8", // chevron point
	"L9.1 13.6", // left wing root == chevron left vertex
	"L3.1 13.4", // left lower feather tip
	"L4.6 9.6", // left notch
	"Z",
].join(" ");

/**
 * First-party IronHermes brand mark, shaped to match `BrandIconComponent` so it drops into
 * `BrandIconTile` beside the vendor marks. Unlike those, it is authored in this repo, so it
 * reports `data-icon-source="clawdi"` — the attribute is written after the prop spread so a
 * caller's vendor default cannot overwrite it with a claim that is not true.
 */
export function IronHermesMark({
	size = "1em",
	...props
}: Omit<SVGProps<SVGSVGElement>, "size"> & { size?: number | string }) {
	return (
		<svg
			fill="currentColor"
			height={size}
			viewBox="0 0 24 24"
			width={size}
			xmlns="http://www.w3.org/2000/svg"
			{...props}
			data-icon-source="clawdi"
		>
			<title>IronHermes</title>
			<path d={IRONHERMES_MARK_PATH} fillRule="evenodd" />
		</svg>
	);
}
