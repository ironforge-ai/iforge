"use client";

import { toast } from "sonner";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * Copy affordance strings. Both have a `## Copywriting Contract` row in
 * `03-UI-SPEC.md` and a parity assertion, so the picker in plan 03-21
 * inherits the contract rather than re-typing the literals.
 */
export const COPY_IDENTIFIER_TITLE = "Copy user id";
export const IDENTIFIER_COPIED_TOAST = "User id copied";

/**
 * The clerk id's own pair.
 *
 * The tenant picker's selection chip renders THREE identifiers side by side —
 * an email, a user id and a clerk id. Reusing "Copy user id" over a clerk id
 * would ship an instruction that names a value it does not act on, which is
 * the same defect plan 03-21 repairs in the delete dialog one file over. The
 * label is what tells two opaque strings apart.
 *
 * It reaches assistive tech as `aria-label`, NOT as `title`: a button with
 * text content derives its accessible name from that subtree, so `title`
 * alone left both affordances announcing their raw uuid (WR-02).
 */
export const COPY_CLERK_IDENTIFIER_TITLE = "Copy clerk id";
export const CLERK_IDENTIFIER_COPIED_TOAST = "Clerk id copied";

/**
 * Shown when the clipboard write does not happen. The operator's next move is
 * to select the visible text by hand, so the copy says that rather than
 * reporting an error code they cannot act on.
 */
export const IDENTIFIER_COPY_FAILED_TOAST = "Could not copy — select the value to copy it manually";

/**
 * Write one identifier to the clipboard, reporting whether it landed.
 *
 * Split out of the click handler so both failure paths are reachable from a
 * test: the web suite renders static markup and has no DOM, so a handler that
 * closes over `navigator` cannot be exercised at all (WR-01). Two things go
 * wrong in the field and neither used to surface — a rejected write (denied
 * permission) and an absent `navigator.clipboard` (any insecure context).
 */
export async function writeIdentifierToClipboard(
	value: string,
	clipboard: Pick<Clipboard, "writeText"> | undefined = globalThis.navigator?.clipboard,
): Promise<boolean> {
	if (!clipboard?.writeText) return false;
	try {
		await clipboard.writeText(value);
		return true;
	} catch {
		return false;
	}
}

/**
 * One tenant identifier, rendered so an operator can READ it and take it
 * somewhere — mono, truncated in place, full value in the tooltip, copied to
 * the clipboard on click.
 *
 * It lives under `pages/dashboard/admin/` rather than `components/ui/`
 * because it is admin-section vocabulary, not a house primitive (AGENTS.md
 * directory ownership). Three surfaces consume it: the invite table's
 * Redeemed-by cell, and — in plan 03-21 — the tenant picker's selection chip
 * and result rows.
 *
 * The value is rendered as a TEXT NODE, deliberately. Until G-03-62 the only
 * places a tenant id appeared in the console were attribute values, which is
 * a value an operator cannot read, select or copy.
 */
export function CopyIdentifier({
	value,
	className,
	title = COPY_IDENTIFIER_TITLE,
	toast: toastCopy = IDENTIFIER_COPIED_TOAST,
}: {
	value: string;
	className?: string;
	/** Names the identifier this affordance copies. Defaults to the user id,
	 * which is what every caller but the picker's clerk-id chip renders. */
	title?: string;
	toast?: string;
}) {
	return (
		<Tooltip>
			<TooltipTrigger
				render={
					<button
						type="button"
						title={title}
						aria-label={title}
						className={cn(
							"relative z-10 block max-w-48 cursor-pointer truncate font-mono text-xs",
							className,
						)}
						onClick={() => {
							void writeIdentifierToClipboard(value).then((copied) => {
								if (copied) toast.success(toastCopy);
								else toast.error(IDENTIFIER_COPY_FAILED_TOAST);
							});
						}}
					/>
				}
			>
				{value}
			</TooltipTrigger>
			<TooltipContent>{value}</TooltipContent>
		</Tooltip>
	);
}
