"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { Button } from "@/components/ui/button";
import { ConfirmAction } from "@/components/ui/confirm-action";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import type { StatusTone } from "@/components/ui/status-badge";
import { StatusBadge } from "@/components/ui/status-badge";
import {
	type AdminTenantLifecycleResponse,
	type AdminTenantPurgeInfo,
	type AdminUserListItem,
	cancelTenantPurge,
	deleteTenant,
	getTenantLifecycle,
	isAdminKeyError,
	type TenantPurgeStatus,
} from "@/lib/admin-client";
import { fillCopyTemplate, formatShortDate, truncateForDisplay } from "@/lib/format";
import { TenantPicker, tenantDisplayEmail } from "@/pages/dashboard/admin/export-section";

/**
 * UI-SPEC tenant-lifecycle tones.
 *
 * `failed` is DESTRUCTIVE, not `warning`: a failed purge is a sweep that ran —
 * it issued deployment deletes, cancelled the subscription and removed S3
 * prefixes — and then died. It denotes data already lost, which is the one
 * thing the warning token does not mean (W-01 / WR-03).
 */
export const PURGE_STATUS_TONE: Record<TenantPurgeStatus, StatusTone> = {
	pending: "warning",
	failed: "destructive",
	purging: "warning",
	complete: "neutral",
};

/**
 * Copy this plan adds or changes. Every string below has a row in the
 * `## Copywriting Contract` table of `03-UI-SPEC.md`, and `deletion-section.test.tsx`
 * compares the two through `ui-spec-contract.ts` — an interpolating string is
 * declared and compared as a TEMPLATE, never as whatever a render produced.
 */
export const PURGE_FAILED_COPY = "Purge ran and failed — data may already be deleted";
export const PURGE_ATTEMPT_COPY = "Attempt {n}";
export const PURGE_ATTEMPT_WITH_ERROR_COPY = "Attempt {n} — {error}";
export const PURGE_RUNNING_COPY = "Purge running — started {date}";
export const PURGE_RUNNING_UNCLAIMED_COPY = "Purge running";
export const TENANT_DELETE_PARTIAL_COPY =
	"This tenant is locked out and their purge is scheduled, but the identity-provider record could not be removed. Confirm again to retry that step.";

/** Display budget for `TenantPurge.error`, a backend exception string up to
 * 1000 characters that is now rendered in the operator console (T-03-76). */
export const PURGE_ERROR_DISPLAY_MAX = 160;

/** "Attempt 3 — connection refused", or null when nothing has run yet. */
export function purgeAttemptSummary(purge: AdminTenantPurgeInfo): string | null {
	if (purge.attempt_count === 0 && purge.error === null) return null;
	const n = String(purge.attempt_count);
	if (purge.error === null) return fillCopyTemplate(PURGE_ATTEMPT_COPY, { n });
	return fillCopyTemplate(PURGE_ATTEMPT_WITH_ERROR_COPY, {
		n,
		error: truncateForDisplay(purge.error, PURGE_ERROR_DISPLAY_MAX),
	});
}

/** The same line with the error left WHOLE, for the rendered span's `title`.
 *
 * Walking UAT 61 showed the error being truncated TWICE: `purgeAttemptSummary`
 * cuts it to PURGE_ERROR_DISPLAY_MAX, and the span then clips what survives at
 * `max-w-96` with `truncate`. Against the real 372-character WR-04 message an
 * operator saw as far as "deploy_api_url is unset…" — the sentence naming the
 * fix (set DEPLOY_API_URL, or TENANT_PURGE_SKIP_HOSTED_TEARDOWN=true) was
 * unreachable from the console whose whole job is to say why the sweep failed.
 *
 * The visible budget is deliberately unchanged: the badge must stay one line.
 * This only makes the rest recoverable on hover and to a screen reader. Returns
 * null where `purgeAttemptSummary` does, and — when the error already fits —
 * returns a string EQUAL to it, so the caller can drop a redundant title.
 */
export function purgeAttemptSummaryUntruncated(purge: AdminTenantPurgeInfo): string | null {
	if (purge.attempt_count === 0 && purge.error === null) return null;
	const n = String(purge.attempt_count);
	if (purge.error === null) return fillCopyTemplate(PURGE_ATTEMPT_COPY, { n });
	return fillCopyTemplate(PURGE_ATTEMPT_WITH_ERROR_COPY, { n, error: purge.error });
}

/**
 * The typed-confirmation copy, in two variants that differ only in the NOUN.
 *
 * `confirmValue` is `email ?? id`, but both the sentence and the input's
 * accessible name used to say "email" unconditionally — so an operator
 * deleting a tenant with no email was told to type a value the gate would
 * reject, in the last text anyone reads before an irreversible action
 * (G-03-62, missing item 7). Declared as TEMPLATES with a `{value}` slot,
 * because the value renders `font-mono` as data inside the sentence, and
 * compared against their `## Copywriting Contract` rows as templates.
 */
export const DELETE_CONFIRM_BODY_EMAIL =
	"This locks {value} out immediately and permanently deletes all their data after the grace window ({days} days). Their subscription is canceled and hosted deployments are torn down. Type the tenant's email to confirm.";
export const DELETE_CONFIRM_BODY_USER_ID =
	"This locks {value} out immediately and permanently deletes all their data after the grace window ({days} days). Their subscription is canceled and hosted deployments are torn down. Type the tenant's user id to confirm.";
export const DELETE_CONFIRM_LABEL_EMAIL = "Type the tenant's email to confirm";
export const DELETE_CONFIRM_LABEL_USER_ID = "Type the tenant's user id to confirm";

/**
 * The typed-confirmation gate: the operator must type the confirmation value
 * in full — the match is exact on the full untruncated value (no trimming, no
 * case folding), and an empty target can never be satisfied.
 *
 * The second parameter is `confirmValue`, not `email`. It holds `email ?? id`
 * and always did; the old name is what let the copy drift away from the check.
 */
export function isDeleteConfirmMatch(typed: string, confirmValue: string): boolean {
	return confirmValue.length > 0 && typed === confirmValue;
}

/**
 * What the typed-confirmation gate demands, and what the copy must therefore
 * name — decided ONCE, so the dialog cannot ask for a value the gate does not
 * check.
 *
 * `User.email` is a nullable `String(320)` with no CHECK constraint, and
 * `user_provisioning.py` passes the claim through unnormalised, so `""` is
 * representable. Three predicates used to disagree about it: `email ?? id`
 * passes `""` through, `email != null` calls `""` an email, and
 * `isDeleteConfirmMatch` rejects an empty target — which together made such a
 * tenant permanently undeletable from the console under a dialog asking for an
 * email and naming nothing (WR-06). An empty email is not an email.
 */
export function tenantConfirmTarget(tenant: AdminUserListItem | null): {
	confirmValue: string;
	confirmsWithEmail: boolean;
} {
	if (!tenant) return { confirmValue: "", confirmsWithEmail: false };
	// The SAME predicate the picker labels its rows with — one definition of
	// "has an email" repo-wide, so the dialog and the row it was chosen from
	// can never disagree (W-13).
	const email = tenantDisplayEmail(tenant);
	return { confirmValue: email ?? tenant.id, confirmsWithEmail: email !== null };
}

/**
 * Split a confirmation template around its single `{value}` marker.
 *
 * The value renders as data (`font-mono`) inside the sentence, so it is an
 * element rather than a substitution — but the surrounding halves are still
 * copy, and BOTH need filling. Only the trailing half used to be filled, and a
 * two-element destructure dropped everything past a second marker in silence
 * (WR-05). A template that does not name its value exactly once is a copy bug,
 * so it throws rather than rendering something plausible.
 */
export function splitConfirmTemplate(template: string): [string, string] {
	const parts = template.split("{value}");
	if (parts.length !== 2) {
		throw new Error(
			`A confirmation template must contain exactly one {value}; found ${parts.length - 1} in: ${template}`,
		);
	}
	return [parts[0] as string, parts[1] as string];
}

/**
 * Run the delete, and tell the truth when it half-lands.
 *
 * `admin_delete_tenant` commits the fence and the purge and THEN deletes the
 * provider identity, so a provider failure is a 500 over a tenant that IS
 * fenced with a purge scheduled. Reporting that as "the request did not land"
 * is the worst possible direction for a confirmation error on a destructive
 * action, and `03-UAT.md` test 6 hit it live (W-06 / T-03-72). Re-running the
 * delete is safe and is what retries the identity deletion, so the copy sends
 * the operator back through the same dialog — which `ConfirmAction` has kept
 * open, with this message rendered inline.
 *
 * A 401/403 is returned untouched so the page's key path runs first, and an
 * unreadable lifecycle keeps the original error rather than guessing.
 */
export async function deleteTenantWithPartialReport(
	runDelete: () => Promise<unknown>,
	readLifecycle: () => Promise<AdminTenantLifecycleResponse>,
): Promise<unknown> {
	try {
		return await runDelete();
	} catch (error) {
		if (isAdminKeyError(error)) throw error;
		let after: AdminTenantLifecycleResponse | null = null;
		try {
			after = await readLifecycle();
		} catch {
			after = null;
		}
		if (after?.purge != null) throw new Error(TENANT_DELETE_PARTIAL_COPY);
		throw error;
	}
}

/** Contract copy for the "Delete this tenant" typed-confirmation dialog.
 *
 * The grace window is a PROP, sourced from the lifecycle response. It used to
 * be a module constant whose only enforcement was a comment, so a deployment
 * with a different window still read "30 days" in the last text an operator
 * sees before authorising an irreversible action (CR-02 / WR-13). */
export function DeleteTenantConfirmBody({
	confirmValue,
	confirmsWithEmail,
	graceDays,
	typed,
	onTypedChange,
}: {
	/** Exactly the value `isDeleteConfirmMatch` will check. */
	confirmValue: string;
	/** Whether that value is the tenant's email or its user id. Derived from
	 * the tenant ROW at the single site that computes `confirmValue` — never by
	 * sniffing the string for an "@", which would make the instruction a guess
	 * about the gate rather than a statement of it. */
	confirmsWithEmail: boolean;
	graceDays: number;
	typed: string;
	onTypedChange: (next: string) => void;
}) {
	const template = confirmsWithEmail ? DELETE_CONFIRM_BODY_EMAIL : DELETE_CONFIRM_BODY_USER_ID;
	// Split on `{value}` BEFORE filling: the value renders as data (font-mono)
	// inside the sentence, so it is an element, not a substitution. Both halves
	// are still copy and both get filled (WR-05).
	const [before, after] = splitConfirmTemplate(template);
	return (
		<>
			<p>
				{fillCopyTemplate(before, { days: String(graceDays) })}
				<span className="font-mono">{confirmValue}</span>
				{fillCopyTemplate(after, { days: String(graceDays) })}
			</p>
			<Input
				className="font-mono"
				value={typed}
				onChange={(e) => onTypedChange(e.target.value)}
				autoComplete="off"
				aria-label={confirmsWithEmail ? DELETE_CONFIRM_LABEL_EMAIL : DELETE_CONFIRM_LABEL_USER_ID}
			/>
		</>
	);
}

/**
 * Contract copy for the "Cancel this purge" dialog, split by purge status.
 *
 * A `failed` purge is not a purge that has not started — it is a sweep that
 * RAN and did not finish, so some of the tenant's data is already gone. The
 * single `pending`-shaped sentence promised the opposite ("their data will not
 * be deleted") on that branch, which is the one branch where it is false
 * (WR-02). Canceling a failed purge is still the right affordance: it deletes
 * the row, which is what stops the worker re-claiming it on every elapsed
 * backoff. The copy just has to say what it actually does.
 */
export function CancelPurgeConfirmBody({ status }: { status: TenantPurgeStatus }) {
	if (status === "failed") {
		return (
			<p>
				A purge attempt already ran for this tenant and did not finish, so some of their data may
				already be deleted. Canceling stops further attempts and keeps the record of what ran. The
				tenant stays locked out. You can delete again later.
			</p>
		);
	}
	return (
		<p>
			The tenant stays locked out, but their data will not be deleted. You can delete again later.
		</p>
	);
}

export function DeletionSection({ onNeedsKey }: { onNeedsKey: () => void }) {
	const queryClient = useQueryClient();
	const [tenant, setTenant] = useState<AdminUserListItem | null>(null);
	const [typed, setTyped] = useState("");

	const lifecycle = useQuery({
		queryKey: ["admin", "tenant-lifecycle", tenant?.id],
		queryFn: () => getTenantLifecycle(tenant?.id ?? ""),
		enabled: tenant !== null,
		retry: (failureCount, error) => !isAdminKeyError(error) && failureCount < 2,
	});
	const lifecycleError = lifecycle.error;
	useEffect(() => {
		if (isAdminKeyError(lifecycleError)) onNeedsKey();
	}, [lifecycleError, onNeedsKey]);

	const refetchLifecycle = () => {
		void queryClient.invalidateQueries({ queryKey: ["admin", "tenant-lifecycle"] });
	};

	// ConfirmAction keeps its dialog open on rejection and renders the
	// normalized error inline at the action — mutation errors never toast.
	const remove = useMutation({
		mutationFn: () => deleteTenant(tenant?.id ?? ""),
		onSuccess: () => {
			toast.success("Tenant deletion scheduled");
			refetchLifecycle();
		},
		onError: (error) => {
			if (isAdminKeyError(error)) onNeedsKey();
		},
	});
	const cancel = useMutation({
		mutationFn: () => cancelTenantPurge(tenant?.id ?? ""),
		onSuccess: () => {
			toast.success("Purge canceled");
			refetchLifecycle();
		},
		onError: (error) => {
			if (isAdminKeyError(error)) onNeedsKey();
		},
	});

	// The email is the confirmation value; a tenant row without one falls back
	// to the id so the gate stays a full-value match, never a free pass. This
	// line is deliberately UNCHANGED by G-03-62: the gate needs exactly ONE
	// value and cannot hold two identifiers at once.
	//
	// The identifier an operator may need to READ arrives by inheritance, not
	// from here: `<TenantPicker selected={tenant}>` below is the SHARED picker
	// from `export-section.tsx`, whose chip renders the email and the tenant id
	// together. Localizing that chip here would silently reopen the deletion
	// half of G-03-62 missing item 6, so `deletion-section.test.tsx` asserts
	// both the chip's content and the fact that this stays the shared picker.
	// The discriminant rides the ROW, beside the value it describes, and both
	// come from ONE predicate so they cannot disagree (WR-06).
	const { confirmValue, confirmsWithEmail } = tenantConfirmTarget(tenant);

	return (
		<div className="space-y-4">
			<TenantPicker
				selected={tenant}
				onSelect={(next) => {
					setTenant(next);
					setTyped("");
				}}
				onNeedsKey={onNeedsKey}
			/>

			{tenant ? (
				lifecycleError && !isAdminKeyError(lifecycleError) ? (
					<ApiErrorPanel
						error={lifecycleError}
						onRetry={() => {
							void lifecycle.refetch();
						}}
						title="Couldn't load tenant status"
					/>
				) : lifecycle.isLoading ? (
					<Skeleton className="h-8 w-72" />
				) : lifecycle.data ? (
					<TenantLifecyclePanel
						lifecycle={lifecycle.data}
						confirmValue={confirmValue}
						confirmsWithEmail={confirmsWithEmail}
						typed={typed}
						onTypedChange={setTyped}
						onDelete={() =>
							deleteTenantWithPartialReport(
								() => remove.mutateAsync(),
								async () => {
									const fresh = await getTenantLifecycle(tenant.id);
									refetchLifecycle();
									return fresh;
								},
							)
						}
						onCancelPurge={() => cancel.mutateAsync()}
					/>
				) : null
			) : null}
		</div>
	);
}

/** Presentational lifecycle panel — props in, markup out (component-testable). */
export function TenantLifecyclePanel({
	lifecycle,
	confirmValue,
	confirmsWithEmail,
	typed,
	onTypedChange,
	onDelete,
	onCancelPurge,
}: {
	lifecycle: AdminTenantLifecycleResponse;
	confirmValue: string;
	/** Whether `confirmValue` is the tenant's email or its user id — threaded
	 * from the row so the dialog's instruction names the gate's own value. */
	confirmsWithEmail: boolean;
	typed: string;
	onTypedChange: (next: string) => void;
	onDelete: () => Promise<unknown>;
	onCancelPurge: () => Promise<unknown>;
}) {
	const purge = lifecycle.purge;

	if (purge && (purge.status === "pending" || purge.status === "failed")) {
		// One badge served both statuses, and `purge_after` for a failed row is
		// by definition in the PAST — the row was only claimable once
		// `purge_after <= now`. Both halves of that badge were false (W-01).
		const failed = purge.status === "failed";
		const attempts = failed ? purgeAttemptSummary(purge) : null;
		// Only when it actually says more than the visible line does (see
		// purgeAttemptSummaryUntruncated) — a title identical to the text is
		// noise for a screen reader.
		const attemptsFull = failed ? purgeAttemptSummaryUntruncated(purge) : null;
		const attemptsTitle = attemptsFull && attemptsFull !== attempts ? attemptsFull : undefined;
		return (
			<div className="flex flex-wrap items-center gap-3">
				<StatusBadge status={PURGE_STATUS_TONE[purge.status]}>
					{failed ? PURGE_FAILED_COPY : `Purge scheduled — ${formatShortDate(purge.purge_after)}`}
				</StatusBadge>
				{attempts ? (
					<span className="max-w-96 truncate text-xs text-muted-foreground" title={attemptsTitle}>
						{attempts}
					</span>
				) : null}
				<ConfirmAction
					title="Cancel this purge"
					description={<CancelPurgeConfirmBody status={purge.status} />}
					confirmLabel={failed ? "Stop the purge" : "Keep data"}
					onConfirm={onCancelPurge}
				>
					<Button size="sm" variant="outline">
						Cancel purge
					</Button>
				</ConfirmAction>
			</div>
		);
	}

	if (purge && purge.status === "purging") {
		// Labelled with the claim time now that 03-16 puts it on the wire — the
		// schedule date says nothing about a sweep that is already running.
		return (
			<StatusBadge status={PURGE_STATUS_TONE.purging}>
				{purge.claimed_at
					? fillCopyTemplate(PURGE_RUNNING_COPY, { date: formatShortDate(purge.claimed_at) })
					: PURGE_RUNNING_UNCLAIMED_COPY}
			</StatusBadge>
		);
	}

	if (purge && purge.status === "complete") {
		return (
			<StatusBadge status={PURGE_STATUS_TONE.complete}>
				Purged — {formatShortDate(purge.completed_at ?? purge.purge_after)}
			</StatusBadge>
		);
	}

	return (
		<div className="flex flex-wrap items-center gap-3">
			{lifecycle.fenced ? (
				<StatusBadge status="warning">Fenced — no purge scheduled</StatusBadge>
			) : null}
			<ConfirmAction
				title="Delete this tenant"
				description={
					<DeleteTenantConfirmBody
						confirmValue={confirmValue}
						confirmsWithEmail={confirmsWithEmail}
						graceDays={lifecycle.grace_days}
						typed={typed}
						onTypedChange={onTypedChange}
					/>
				}
				confirmLabel="Delete tenant"
				destructive
				confirmDisabled={!isDeleteConfirmMatch(typed, confirmValue)}
				onOpenChange={(open) => {
					if (!open) onTypedChange("");
				}}
				onConfirm={onDelete}
			>
				<Button size="sm" variant="destructive">
					Delete tenant
				</Button>
			</ConfirmAction>
		</div>
	);
}
