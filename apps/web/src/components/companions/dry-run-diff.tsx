"use client";

import { Skeleton } from "@/components/ui/skeleton";
import { CALL_DETAIL_COPY, type DryRunSectionState, dryRunFailureCopy } from "./call-detail.logic";

/**
 * "What would change" — the preview, exactly as it was computed at the time.
 *
 * Four things this component refuses to do, each because a reviewer reads it
 * immediately before authorizing a write:
 *
 * 1. It never renders a failed probe as a clean diff. A preview that could not
 *    read its target has nothing to preview, and an uninformed preview that
 *    looks identical to an informed one is the failure D-11 exists to prevent.
 * 2. It never renders "nothing would change" for a preview that did not run.
 *    `dry_run_diff: null` is "nobody looked"; an empty before-and-after list is
 *    "the probe read the target and nothing would change". Different answers.
 * 3. It never renders a partial diff as complete. The computed entries are shown
 *    together with an explicit statement of what the façade could not determine,
 *    because the complete-looking diff is the one an approver trusts most.
 * 4. It never presents the preview as a guarantee. The diff carries the
 *    façade's own `guarantee` sentence and it is rendered rather than dropped.
 */
export function DryRunDiff({
	state,
	guarantee,
}: {
	state: DryRunSectionState;
	/** `dry_run_diff.guarantee` — the façade's own point-in-time caveat. */
	guarantee?: string | null;
}) {
	if (state.kind === "absent") return null;

	return (
		<section data-slot="dry-run-diff" data-state={state.kind} className="space-y-2">
			<h3 className="font-semibold text-sm">{CALL_DETAIL_COPY.dryRunHeading}</h3>
			{state.kind === "loading" ? (
				<div data-slot="dry-run-skeleton" aria-hidden="true" className="space-y-2">
					<Skeleton className="h-4 w-64 max-w-full" />
					<Skeleton className="h-4 w-48 max-w-full" />
				</div>
			) : state.kind === "failed" ? (
				// An error, never a diff. No before-and-after markup is emitted at all
				// on this branch, so there is nothing for a reader to mistake for a
				// clean preview.
				<p data-slot="dry-run-failure" className="text-destructive-muted-foreground text-sm">
					{dryRunFailureCopy(state.reason)}
				</p>
			) : state.kind === "clean" ? (
				<p data-slot="dry-run-clean" className="text-sm">
					{CALL_DETAIL_COPY.dryRunClean}
				</p>
			) : (
				<>
					{state.target ? (
						<p className="font-mono text-muted-foreground text-xs">{state.target}</p>
					) : null}
					{/* The entries scroll INSIDE the Sheet body rather than the page, so a
					    two-hundred-entry diff leaves the header and the Decision section
					    reachable. */}
					<ul
						data-slot="dry-run-entries"
						className="max-h-96 space-y-2 overflow-y-auto rounded-md bg-muted/30 p-4"
					>
						{state.entries.map((entry, index) => (
							<li
								// The pairs have no identity of their own; the index IS the
								// pairing rule (`before[i]` against `after[i]`).
								key={`${index}-${entry.before ?? ""}-${entry.after ?? ""}`}
								data-slot="dry-run-entry"
								className="space-y-1 font-mono text-xs break-all whitespace-pre-wrap"
							>
								<div data-slot="dry-run-before" className="text-muted-foreground">
									<span className="select-none">− </span>
									{entry.before ?? "(nothing)"}
								</div>
								<div data-slot="dry-run-after">
									<span className="select-none">+ </span>
									{entry.after ?? "(nothing)"}
								</div>
							</li>
						))}
					</ul>
					{state.kind === "partial" ? (
						<div data-slot="dry-run-undetermined" className="space-y-1">
							<p className="text-warning-muted-foreground text-xs">
								{CALL_DETAIL_COPY.dryRunUndetermined}
							</p>
							<ul className="list-inside list-disc text-muted-foreground text-xs">
								{state.undetermined.map((item) => (
									<li key={item}>{item}</li>
								))}
							</ul>
						</div>
					) : null}
				</>
			)}
			{guarantee && state.kind !== "loading" && state.kind !== "failed" ? (
				<p data-slot="dry-run-guarantee" className="text-muted-foreground text-xs">
					{guarantee}
				</p>
			) : null}
		</section>
	);
}
