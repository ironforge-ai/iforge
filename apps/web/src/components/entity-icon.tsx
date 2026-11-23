import { AgentFrameworkIcon } from "@/components/agent-framework-icon";
import { BrandIconTile } from "@/components/brand-icon-tile";
import { providerBrandIcon } from "@/components/entity-brand-icons";
import { cn } from "@/lib/utils";

/**
 * One icon for every entity — channels, AI providers, and agent frameworks —
 * so they share identical geometry (rounded tile + subtle shadow) across
 * cards, pickers, lists, and the sidebar.
 *
 * EntityIcon is for real brand/app/framework imagery. Use `IconChip` for
 * abstract Lucide glyphs, resource-color tiles, or object emoji marks.
 *
 * Sources, in resolution order:
 *   - channel   → neutral monogram tile (see below)
 *   - framework → official LobeHub React icon
 *   - provider  → official LobeHub React icon in the same neutral tile
 *                 used by monogram fallbacks; ids without an official mark →
 *                 monogram
 *   - anything unresolved → neutral monogram tile
 *
 * **Channels render a monogram, not a brand mark (P33-22, 03.3-07).** They used
 * to load a full-colour app-icon PNG per channel from UPSTREAM's asset CDN — a
 * live runtime dependency, executed on every dashboard render and eagerly at
 * that: React emits a `<link rel="preload" as="image">` for each one, so the
 * request left the browser during head processing. That leaked an IP and a
 * referrer to a host this fork does not control (T-03.3-28).
 *
 * Neither replacement was available. `assets.iforge.dev` has no DNS record, and
 * a repoint to a host that does not serve the files would render four BROKEN
 * IMAGES rather than falling back — the monogram branch below is keyed on an
 * unknown id, and there is no `onError` path, so a 404 on a KNOWN id had no
 * fallback at all. Self-hosting was not available either: the four files were
 * inspected and are mixed provenance — two are the vendors' genuine app icons,
 * two are stylized third-party re-renderings — and redistribution could not be
 * confirmed for any of them.
 *
 * So the channel branch was removed outright and its ids fall through to the
 * neutral monogram. Nothing is fetched, so nothing can leak and nothing can 404.
 * Reversible: restore a source map here once licensed artwork exists.
 */

const SIZE = {
	sm: { px: 24, box: "size-6 rounded-md", mono: "text-3xs" },
	md: { px: 40, box: "size-10 rounded-lg", mono: "text-sm" },
	lg: { px: 48, box: "size-12 rounded-xl", mono: "text-base" },
} as const;

export type EntityIconSize = keyof typeof SIZE;
export type EntityKind = "channel" | "provider" | "framework";

const SHADOW = "shadow-[0_2px_6px_rgba(0,0,0,0.1)] dark:shadow-none";
const PROVIDER_TILE = "border border-border/60 bg-muted/40 shadow-none";

function NeutralMonogram({
	label,
	size,
	className,
}: {
	label: string;
	size: EntityIconSize;
	className?: string;
}) {
	const s = SIZE[size];
	const mono = label.trim().charAt(0).toUpperCase() || "?";
	return (
		<span
			aria-hidden
			className={cn(
				s.box,
				"flex shrink-0 items-center justify-center bg-muted font-semibold text-muted-foreground",
				s.mono,
				className,
			)}
		>
			{mono}
		</span>
	);
}

export function EntityIcon({
	kind,
	id,
	label,
	size = "md",
	className,
}: {
	kind: EntityKind;
	/** Provider type / channel provider / framework agent_type. */
	id: string;
	/** Human label — used for alt text and the fallback monogram. */
	label?: string;
	size?: EntityIconSize;
	className?: string;
}) {
	const s = SIZE[size];
	const key = id?.toLowerCase?.() ?? "";
	const providerBrand = kind === "provider" ? providerBrandIcon(key) : undefined;
	const alt = label ?? providerBrand?.label ?? id ?? "";

	if (kind === "framework") {
		return (
			<AgentFrameworkIcon
				agent={id}
				label={alt}
				alt={alt}
				pixelSize={s.px}
				boxClassName={cn(s.box, SHADOW)}
				fallback="monogram"
				className={cn(s.mono, className)}
			/>
		);
	}

	// Provider brand logo from the official LobeHub React package.
	if (kind === "provider") {
		if (providerBrand) {
			return (
				<BrandIconTile
					icon={providerBrand.icon}
					iconClassName={providerBrand.iconClassName}
					iconScale={providerBrand.iconScale}
					label={alt}
					boxClassName={s.box}
					className={cn(providerBrand.tileClassName, className)}
				/>
			);
		}
		return <NeutralMonogram label={alt} size={size} className={cn(PROVIDER_TILE, className)} />;
	}

	// Every channel lands here, plus anything else left unresolved. For channels
	// this is the rendering contract rather than a fallback — see the note above.
	return <NeutralMonogram label={alt} size={size} className={className} />;
}
