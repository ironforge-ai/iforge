"use client";

import { Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { CALL_DETAIL_COPY, type ProvenanceRow, shortIdentifier } from "./call-detail.logic";
import { NOT_RECORDED } from "./tool-activity.logic";

/**
 * The chain, read top-down as one path.
 *
 * Rows are separated by hairlines and rendered in a fixed order, deliberately
 * NOT as a property grid: "this session, on this agent, prompted by that
 * content, with these arguments" is a sentence, and the order is the sentence.
 *
 * An unrecorded link renders the muted "Not recorded" — never a blank row and
 * never a fabricated identifier. A link still in flight renders a skeleton,
 * because "we have not asked yet" and "it was not recorded" are different
 * answers to the question a reviewer is actually asking.
 */
export function ProvenanceChain({
	rows,
	onCopy,
	onOpenCall,
}: {
	rows: readonly ProvenanceRow[];
	onCopy?: (value: string) => void;
	onOpenCall?: (callId: string) => void;
}) {
	return (
		<section data-slot="provenance-chain" className="space-y-1">
			<h3 className="font-semibold text-sm">{CALL_DETAIL_COPY.provenanceHeading}</h3>
			<dl className="divide-y divide-border">
				{rows.map((row) => (
					<div
						key={row.kind}
						data-slot="provenance-row"
						data-row={row.kind}
						data-state={row.state}
						className="flex flex-col gap-1 py-2 sm:flex-row sm:items-baseline sm:gap-4"
					>
						<dt className="w-32 shrink-0 text-muted-foreground text-xs">{row.label}</dt>
						<dd className="min-w-0 flex-1 space-y-1">
							<ProvenanceValue row={row} onCopy={onCopy} onOpenCall={onOpenCall} />
							{row.note ? (
								<p data-slot="provenance-note" className="text-muted-foreground text-xs">
									{row.note}
								</p>
							) : null}
						</dd>
					</div>
				))}
			</dl>
		</section>
	);
}

function ProvenanceValue({
	row,
	onCopy,
	onOpenCall,
}: {
	row: ProvenanceRow;
	onCopy?: (value: string) => void;
	onOpenCall?: (callId: string) => void;
}) {
	if (row.state === "pending") {
		return <Skeleton data-slot="provenance-pending" className="h-4 w-48 max-w-full" />;
	}
	if (row.state === "not-recorded" || row.value === null) {
		return (
			<span data-slot="provenance-value" className="text-muted-foreground text-xs">
				{NOT_RECORDED}
			</span>
		);
	}

	// The arguments row is a payload rather than an identifier: it wraps inside a
	// muted well with `break-all`, so a four-kibibyte single token cannot escape
	// the Sheet instead of being truncated to a 12-character prefix.
	if (row.kind === "arguments") {
		return (
			<pre
				data-slot="provenance-value"
				className="max-h-64 overflow-auto rounded-md bg-muted/30 p-4 font-mono text-xs break-all whitespace-pre-wrap"
			>
				{row.value}
			</pre>
		);
	}

	const display = row.mono ? shortIdentifier(row.value) : row.value;
	return (
		<span className="flex min-w-0 items-center gap-1">
			{row.linkCallId && onOpenCall ? (
				<button
					type="button"
					data-slot="provenance-value"
					title={row.value}
					className={cn("min-w-0 truncate text-xs underline-offset-2 hover:underline", "font-mono")}
					onClick={() => onOpenCall(row.linkCallId as string)}
				>
					{display}
				</button>
			) : (
				<span
					data-slot="provenance-value"
					title={row.value}
					className={cn("min-w-0 truncate text-xs", row.mono && "font-mono")}
				>
					{display}
				</span>
			)}
			{row.mono && onCopy ? (
				<Button
					type="button"
					variant="ghost"
					size="icon-sm"
					aria-label={`Copy ${row.label.toLowerCase()}`}
					onClick={() => onCopy(row.value as string)}
				>
					<Copy className="size-3" />
				</Button>
			) : null}
		</span>
	);
}
