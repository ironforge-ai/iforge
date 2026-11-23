import { StatusBadge } from "@/components/ui/status-badge";
import { CALL_DETAIL_COPY } from "./call-detail.logic";

/**
 * The one component that renders companion bytes, with exactly one job: display
 * them without interpreting them.
 *
 * **Why this is stricter than "sanitize the HTML".** An allowlist sanitizer's
 * job is to keep markup that is safe to RENDER. These bytes were already read by
 * a model as potential instructions, and the person looking at them here is
 * about to authorize a write. `<b>Ignore previous instructions</b>` passes every
 * sanitizer in existence; markup at this point in the system is aimed at the
 * HUMAN, not at the browser. So the content is rendered as TEXT — never through
 * `Markdown`, never through a markdown library, never through an
 * innerHTML-shaped prop, and never auto-linkified.
 *
 * **The marking is shown, not stripped.** The spotlighting envelope — the
 * delimiters plus the interleaved per-response mark — is what the agent saw, and
 * a reviewer comparing notes with an agent needs the same bytes rather than a
 * cleaned-up version of them. The opening delimiter renders above the well and
 * the closing one below it, so nothing is dropped and the boundary the façade
 * drew stays visible.
 *
 * **Truncation is disclosed.** The backend already carries the notice in the
 * opening delimiter, because silent truncation is a lie about what the agent
 * received. This surfaces it rather than dropping it.
 */

const OPEN_PREFIX = "<<<UNTRUSTED_COMPANION_CONTENT";
const CLOSE_PREFIX = "<<<END_UNTRUSTED_COMPANION_CONTENT";

export interface UntrustedEnvelope {
	/** The opening delimiter, verbatim. Empty when the content was not framed. */
	header: string;
	/** The closing delimiter, verbatim. Empty when the content was not framed. */
	footer: string;
	/** The marked body, verbatim. The WHOLE input when it was not framed. */
	body: string;
	/** `id` from the delimiter — the content identifier a reviewer can cite. */
	contentId: string | null;
	/** `source` from the delimiter: `{companion}:{verb}`. */
	source: string | null;
	/** The per-response datamark. Rendered as part of the header, never stripped. */
	mark: string | null;
	/** Set when the façade cut the body at the per-call cap. */
	truncation: { limit: number; originalLength: number | null } | null;
	/** False when the input did not carry the envelope at all. */
	framed: boolean;
}

/**
 * Split one envelope into its header, body and footer WITHOUT losing a byte.
 *
 * An input that does not match the shape is not discarded and is not repaired:
 * the whole string becomes the body and `framed` is false, so the reviewer sees
 * everything that arrived and the component can say the framing was missing
 * rather than silently presenting unframed companion bytes as framed ones.
 */
export function parseUntrustedEnvelope(content: string): UntrustedEnvelope {
	const unframed: UntrustedEnvelope = {
		header: "",
		footer: "",
		body: content,
		contentId: null,
		source: null,
		mark: null,
		truncation: null,
		framed: false,
	};
	if (!content.startsWith(OPEN_PREFIX)) return unframed;

	const headerEnd = content.indexOf(">>>");
	if (headerEnd === -1) return unframed;
	const header = content.slice(0, headerEnd + 3);

	const footerStart = content.lastIndexOf(`\n${CLOSE_PREFIX}`);
	const footer = footerStart === -1 ? "" : content.slice(footerStart + 1);
	const body = content.slice(headerEnd + 3, footerStart === -1 ? content.length : footerStart);

	return {
		header,
		footer,
		// The leading newline the façade writes between the delimiter and the body
		// is envelope structure, not content.
		body: body.startsWith("\n") ? body.slice(1) : body,
		contentId: attribute(header, "id"),
		source: attribute(header, "source"),
		mark: attribute(header, "mark"),
		truncation:
			attribute(header, "truncated") === "true"
				? {
						limit: Number.parseInt(attribute(header, "limit") ?? "0", 10) || 0,
						originalLength: toInt(attribute(header, "original_length")),
					}
				: null,
		framed: true,
	};
}

function attribute(header: string, name: string): string | null {
	const match = header.match(new RegExp(`${name}="([^"]*)"`));
	return match?.[1] ?? null;
}

function toInt(value: string | null): number | null {
	if (value === null) return null;
	const parsed = Number.parseInt(value, 10);
	return Number.isFinite(parsed) ? parsed : null;
}

/** A byte count as the largest binary unit that divides it exactly. */
export function formatByteLimit(bytes: number): string {
	const units = ["bytes", "KiB", "MiB", "GiB"] as const;
	let value = bytes;
	let unit = 0;
	while (unit < units.length - 1 && value >= 1024 && value % 1024 === 0) {
		value /= 1024;
		unit += 1;
	}
	return `${value} ${units[unit]}`;
}

/** The disclosure sentence for a cut body. Never omitted when the cap was hit. */
export function truncationNotice(truncation: {
	limit: number;
	originalLength: number | null;
}): string {
	const cap = `Truncated at the ${formatByteLimit(truncation.limit)} per-call cap.`;
	if (truncation.originalLength === null) return cap;
	return `${cap} The companion returned ${formatByteLimit(truncation.originalLength)}.`;
}

export const UNTRUSTED_BADGE_LABEL = "Untrusted content";
export const UNFRAMED_NOTICE =
	"This content did not arrive inside the untrusted envelope. It is shown as recorded.";

export function UntrustedContentWell({
	content,
	companionKey,
	verb,
}: {
	/** `companion_content` from the detail response — the envelope, verbatim. */
	content: string;
	companionKey: string;
	verb: string;
}) {
	const envelope = parseUntrustedEnvelope(content);
	// The panel is labelled with its PROVENANCE, not only its content: which
	// companion, which verb, and the content identifier a reviewer can cite.
	const provenance = `${companionKey}:${verb}${
		envelope.contentId ? ` · content ${envelope.contentId}` : ""
	}`;

	return (
		<section data-slot="untrusted-content" className="space-y-2">
			<div className="flex flex-wrap items-center gap-2">
				<h3 className="font-semibold text-sm">{CALL_DETAIL_COPY.companionContentHeading}</h3>
				<StatusBadge status="warning">{UNTRUSTED_BADGE_LABEL}</StatusBadge>
			</div>
			<p data-slot="untrusted-provenance" className="font-mono text-muted-foreground text-xs">
				{provenance}
			</p>
			{envelope.framed ? (
				<p
					data-slot="untrusted-envelope-header"
					className="text-2xs text-warning-muted-foreground tracking-wider break-all"
				>
					{envelope.header}
				</p>
			) : (
				<p data-slot="untrusted-unframed" className="text-muted-foreground text-xs">
					{UNFRAMED_NOTICE}
				</p>
			)}
			{/*
			 * Plain text, in a mono well. `whitespace-pre-wrap` keeps the newlines the
			 * companion sent and `break-all` keeps a four-kibibyte single token inside
			 * the Sheet. React escapes what it renders here, so markup in the payload
			 * arrives on screen as the literal characters the agent saw — which is the
			 * point: the reviewer is comparing notes with a model, not reading a page
			 * the companion got to lay out.
			 */}
			<div
				data-slot="untrusted-well"
				className="max-h-96 overflow-y-auto rounded-md bg-muted/30 p-4 font-mono text-xs break-all whitespace-pre-wrap"
			>
				{envelope.body}
			</div>
			{envelope.framed && envelope.footer ? (
				<p
					data-slot="untrusted-envelope-footer"
					className="text-2xs text-warning-muted-foreground tracking-wider break-all"
				>
					{envelope.footer}
				</p>
			) : null}
			{envelope.truncation ? (
				<p data-slot="untrusted-truncation" className="text-muted-foreground text-xs">
					{truncationNotice(envelope.truncation)}
				</p>
			) : null}
		</section>
	);
}
