"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { useEffect, useState } from "react";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { EmptyState } from "@/components/empty-state";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { SearchInput } from "@/components/ui/search-input";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import type { StatusTone } from "@/components/ui/status-badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import {
	type AdminTenantExportListItem,
	type AdminUserListItem,
	downloadTenantExport,
	isAdminKeyError,
	listTenantExports,
	lookupUsers,
	type TenantExportStatus,
	triggerTenantExport,
} from "@/lib/admin-client";
import { normalizeApiError } from "@/lib/api-errors";
import { fillCopyTemplate, truncateForDisplay } from "@/lib/format";
import {
	CLERK_IDENTIFIER_COPIED_TOAST,
	COPY_CLERK_IDENTIFIER_TITLE,
	CopyIdentifier,
} from "@/pages/dashboard/admin/copy-identifier";

/** UI-SPEC status-token mapping for export jobs. */
export const EXPORT_STATUS_TONE: Record<TenantExportStatus, StatusTone> = {
	queued: "info",
	running: "info",
	complete: "success",
	failed: "destructive",
};

/**
 * The operator-facing word for each status. The chip used to render
 * `{job.status}` — the raw API enum, lowercase — which the UI-SPEC casing rule
 * sanctions for nothing (UI-REVIEW 5). Typed as an exhaustive
 * `Record<TenantExportStatus, string>` so a status added later fails the
 * typecheck instead of silently rendering an enum again.
 */
export const EXPORT_STATUS_LABEL: Record<TenantExportStatus, string> = {
	queued: "Queued",
	running: "Running",
	complete: "Complete",
	failed: "Failed",
};

/** Mirrors EXPORT_PRESIGNED_TTL_SECONDS in backend/app/services/tenant_export.py.
 * Since 03-18 the link is presigned when the operator asks for it, not on read,
 * and the endpoint echoes this TTL back on every issuance. */
export const EXPORT_LINK_TTL_SECONDS = 3600;

/** Fixed-format TTL string for the contract copy ("expires in {ttl}"). */
export const EXPORT_LINK_TTL_LABEL = "60 minutes";

/**
 * Tenant picker copy. Every string has a `## Copywriting Contract` row in
 * `03-UI-SPEC.md` and a parity assertion; email alone was the whole of W-05,
 * because backend incident logs emit `user_id` and an admin-provisioned or
 * partner tenant may carry no email at all.
 */
export const TENANT_SEARCH_LABEL = "Search tenants by email or user id";
export const TENANT_SEARCH_PLACEHOLDER = "Search tenants by email or user id…";
export const TENANT_SEARCH_EMPTY_COPY = "No tenants match this email or user id.";

/**
 * Failed-export copy, and the display budget for the recorded error.
 *
 * `record_tenant_export_failure` writes `status="failed"` WITH a
 * `next_attempt_at`, and `claim_tenant_export` re-claims a failed job once the
 * backoff elapses — so a failed job is ALREADY durable retry work. The old
 * "The job can be retried without losing anything." plus a "Retry export"
 * button described something that does not exist: clicking queued a SECOND
 * job racing the first, each writing its own artifact key (WR-10). Starting a
 * new export is a separate, labelled act, and the label names the
 * vault-secrets setting the new job will carry.
 */
export const EXPORT_FAILED_COPY = "Export failed. It retries automatically.";
export const EXPORT_FAILED_WITH_ERROR_COPY = "Export failed. It retries automatically — {error}";
export const EXPORT_AGAIN_WITH_VAULT_LABEL = "Start a new export with vault secrets";
export const EXPORT_AGAIN_WITHOUT_VAULT_LABEL = "Start a new export without vault secrets";
export const EXPORT_ERROR_DISPLAY_MAX = 160;

/** The failed-branch sentence for a job, with its recorded error when there is one. */
export function exportFailedCopy(error: string | null): string {
	if (error === null) return EXPORT_FAILED_COPY;
	return fillCopyTemplate(EXPORT_FAILED_WITH_ERROR_COPY, {
		error: truncateForDisplay(error, EXPORT_ERROR_DISPLAY_MAX),
	});
}

/** The re-export label for a job, naming the setting the NEW export will use. */
export function exportAgainLabel(includeVaultValues: boolean): string {
	return includeVaultValues ? EXPORT_AGAIN_WITH_VAULT_LABEL : EXPORT_AGAIN_WITHOUT_VAULT_LABEL;
}

/** Poll while any job is still moving; stop once everything is terminal. */
export function shouldPollExports(items: AdminTenantExportListItem[]): boolean {
	return items.some((job) => job.status === "queued" || job.status === "running");
}

export type ExportJobRowState = "in-progress" | "complete" | "failed" | "link-expired";

/**
 * Which branch a job row renders.
 *
 * 03-18 moved presigning off the list, so `download_url` is now null for EVERY
 * job and the old `download_url === null` test sent every complete job to the
 * expired branch. `downloadable` is the backend's own predicate — true exactly
 * when the download endpoint answers 200 rather than 409 — so the control can
 * never promise a link the server will refuse.
 *
 * The link-freshness clock this function used to carry is gone with the same
 * change: a link now exists only for the instant between the click and the
 * navigation, so there is no client-held URL that can go stale.
 */
export function exportJobRowState(job: AdminTenantExportListItem): ExportJobRowState {
	if (job.status === "queued" || job.status === "running") return "in-progress";
	if (job.status === "failed") return "failed";
	return job.downloadable ? "complete" : "link-expired";
}

function formatTimestamp(value: string): string {
	const d = new Date(value);
	if (Number.isNaN(d.getTime())) return value;
	return d.toLocaleString(undefined, {
		year: "numeric",
		month: "short",
		day: "numeric",
		hour: "2-digit",
		minute: "2-digit",
	});
}

export interface TenantPickerViewProps {
	selected: AdminUserListItem | null;
	onSelect: (tenant: AdminUserListItem | null) => void;
	searchLabel: string;
	term: string;
	onTermChange: (next: string) => void;
	results: AdminUserListItem[];
	isLoading: boolean;
	error: unknown;
	onRetry: () => void;
}

/** Presentational tenant picker — state in, markup out (component-testable). */
/**
 * The tenant's email, or null when it does not have one.
 *
 * `User.email` is a nullable `String(320)` with no CHECK constraint and
 * `user_provisioning.py` passes the claim through unnormalised, so `""` is
 * representable — and an empty email is not an email. One predicate, so a
 * label and the affordance beside it can never disagree about the same row
 * (W-13, the third of the three predicates W-04 named).
 */
export function tenantDisplayEmail(user: AdminUserListItem): string | null {
	return user.email ? user.email : null;
}

export function TenantPickerView({
	selected,
	onSelect,
	searchLabel,
	term,
	onTermChange,
	results,
	isLoading,
	error,
	onRetry,
}: TenantPickerViewProps) {
	const query = term.trim();

	if (selected) {
		return (
			<div className="flex flex-wrap items-center gap-2">
				<span className="text-sm text-muted-foreground">Tenant</span>
				{/* Email and identifier TOGETHER, never one instead of the other.
				 * The retired email-or-else-id fallback made the display
				 * condition and the discovery condition mutually exclusive: an id
				 * appeared only for a tenant an operator could reach only by
				 * already holding its id, so the set of ids obtainable through
				 * the console was empty (G-03-62). With no email the identifier
				 * IS the label — one identifier is rendered once, never twice. */}
				{selected.email ? (
					<span className="max-w-72 truncate font-mono text-xs">{selected.email}</span>
				) : null}
				<CopyIdentifier
					value={selected.id}
					className={selected.email ? "max-w-72 text-muted-foreground" : "max-w-72"}
				/>
				{/* The third identifier `admin_lookup_users` accepts. Nullable by
				 * table constraint for a partner tenant (03-20), so the chip
				 * renders it only when there is one. */}
				{selected.clerk_id ? (
					<CopyIdentifier
						value={selected.clerk_id}
						className="max-w-72 text-muted-foreground"
						title={COPY_CLERK_IDENTIFIER_TITLE}
						toast={CLERK_IDENTIFIER_COPIED_TOAST}
					/>
				) : null}
				<Button
					variant="ghost"
					size="sm"
					onClick={() => {
						onSelect(null);
						onTermChange("");
					}}
				>
					Change
				</Button>
			</div>
		);
	}

	return (
		<div className="max-w-sm space-y-2">
			<SearchInput
				value={term}
				onChange={onTermChange}
				placeholder={TENANT_SEARCH_PLACEHOLDER}
				ariaLabel={searchLabel}
			/>
			{query.length === 0 ? null : error ? (
				<ApiErrorPanel error={error} onRetry={onRetry} title="Couldn't look up tenants" />
			) : isLoading ? (
				<div className="space-y-1.5">
					<Skeleton className="h-8 w-full" />
					<Skeleton className="h-8 w-full" />
				</div>
			) : results.length === 0 ? (
				<p className="text-sm text-muted-foreground">{TENANT_SEARCH_EMPTY_COPY}</p>
			) : (
				<ul className="divide-y divide-border rounded-lg border border-border bg-card">
					{results.map((user) => (
						// The select control and the copy affordance are SIBLINGS,
						// not nested: `CopyIdentifier` renders a <button>, and a
						// button inside a button is invalid HTML that React will
						// not hydrate. The row keeps one hover surface.
						<li key={user.id} className="flex items-center gap-2 px-3 py-2 hover:bg-muted/40">
							<button
								type="button"
								className="flex min-w-0 flex-1 cursor-pointer items-center gap-2 text-left"
								onClick={() => onSelect(user)}
							>
								<span className="min-w-0 flex-1 truncate font-mono text-xs">
									{tenantDisplayEmail(user) ?? user.id}
								</span>
								{user.name ? (
									<span className="max-w-40 truncate text-xs text-muted-foreground">
										{user.name}
									</span>
								) : null}
							</button>
							{/* The identifier ALWAYS renders: as the label above when
							 * the tenant has no email, and here as a copyable
							 * secondary when it does. Never both — an operator
							 * reading the same uuid twice learns nothing extra, and
							 * the row has one width budget.
							 *
							 * Both branches read the SAME predicate. They used to
							 * disagree — `email ?? id` passes "" through while
							 * truthiness rejects it — so an empty-string email
							 * rendered neither, and this comment was false for
							 * exactly that population (W-13). */}
							{tenantDisplayEmail(user) !== null ? (
								<CopyIdentifier value={user.id} className="text-muted-foreground" />
							) : null}
						</li>
					))}
				</ul>
			)}
		</div>
	);
}

/**
 * Tenant picker over `GET /v1/admin/users` — shared by the export and
 * deletion sections. Identifiers render as data (`font-mono`).
 *
 * The search term is deliberately NOT called `email`: since 03-16 the endpoint
 * matches a user id and a clerk id as well, which is what makes an email-less
 * tenant reachable at all (W-05).
 */
export function TenantPicker({
	selected,
	onSelect,
	onNeedsKey,
	searchLabel = TENANT_SEARCH_LABEL,
}: {
	selected: AdminUserListItem | null;
	onSelect: (tenant: AdminUserListItem | null) => void;
	onNeedsKey: () => void;
	searchLabel?: string;
}) {
	const [term, setTerm] = useState("");
	const query = term.trim();
	const users = useQuery({
		queryKey: ["admin", "users", query],
		queryFn: () => lookupUsers(query),
		enabled: query.length > 0,
		retry: (failureCount, error) => !isAdminKeyError(error) && failureCount < 2,
	});
	const usersError = users.error;
	useEffect(() => {
		if (isAdminKeyError(usersError)) onNeedsKey();
	}, [usersError, onNeedsKey]);

	return (
		<TenantPickerView
			selected={selected}
			onSelect={onSelect}
			searchLabel={searchLabel}
			term={term}
			onTermChange={setTerm}
			results={users.data?.items ?? []}
			isLoading={users.isLoading}
			error={isAdminKeyError(usersError) ? null : usersError}
			onRetry={() => {
				void users.refetch();
			}}
		/>
	);
}

export function ExportJobRow({
	job,
	rowState,
	onExportAgain,
	onDownload,
	downloadPending = false,
	downloadError = null,
}: {
	job: AdminTenantExportListItem;
	rowState: ExportJobRowState;
	/** Takes the vault flag from its caller — the row passes the JOB's, never
	 * the section's checkbox, which is the whole of WR-10. */
	onExportAgain: (includeVaultValues: boolean) => void;
	onDownload: () => void;
	downloadPending?: boolean;
	downloadError?: unknown;
}) {
	return (
		<li className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
			<StatusBadge status={EXPORT_STATUS_TONE[job.status]}>
				{EXPORT_STATUS_LABEL[job.status]}
			</StatusBadge>
			<span className="font-mono text-xs text-muted-foreground">
				{formatTimestamp(job.created_at)}
			</span>
			{job.include_vault_values ? (
				<Tooltip>
					<TooltipTrigger render={<span className="text-xs text-warning-muted-foreground" />}>
						Includes vault secrets
					</TooltipTrigger>
					<TooltipContent>This export carries decrypted vault values (opt-in).</TooltipContent>
				</Tooltip>
			) : null}
			<span className="min-w-0 flex-1" />
			{/* The four peer status messages below share one type size — a row
			 * must not resize its own text as a job transitions (Pillar 4). */}
			{rowState === "in-progress" ? (
				<span data-slot="export-job-message" className="text-xs text-muted-foreground">
					Export in progress — this can take a few minutes for large tenants.
				</span>
			) : rowState === "complete" ? (
				<span className="flex flex-wrap items-center gap-2">
					<span data-slot="export-job-message" className="text-xs text-muted-foreground">
						Export ready. This download link expires in {EXPORT_LINK_TTL_LABEL}.
					</span>
					{/* The list carries no URL since 03-18: the control asks the
					 * backend for a link at click time and follows it. */}
					<Button size="sm" variant="outline" onClick={onDownload} disabled={downloadPending}>
						{downloadPending ? <Spinner /> : <Download className="size-3.5" />}
						Download export
					</Button>
					{downloadError ? (
						<span className="text-xs text-destructive-muted-foreground">
							{normalizeApiError(downloadError)}
						</span>
					) : null}
				</span>
			) : rowState === "failed" ? (
				<span className="flex flex-wrap items-center gap-2">
					<span data-slot="export-job-message" className="text-xs text-muted-foreground">
						{exportFailedCopy(job.error)}
					</span>
					<Button
						size="sm"
						variant="outline"
						onClick={() => onExportAgain(job.include_vault_values)}
					>
						{exportAgainLabel(job.include_vault_values)}
					</Button>
				</span>
			) : (
				<span className="flex flex-wrap items-center gap-2">
					<span data-slot="export-job-message" className="text-xs text-muted-foreground">
						Link expired
					</span>
					<Button
						size="sm"
						variant="outline"
						onClick={() => onExportAgain(job.include_vault_values)}
					>
						Export again
					</Button>
				</span>
			)}
		</li>
	);
}

export function ExportSection({ onNeedsKey }: { onNeedsKey: () => void }) {
	const queryClient = useQueryClient();
	const [tenant, setTenant] = useState<AdminUserListItem | null>(null);
	const [includeVaultValues, setIncludeVaultValues] = useState(false);

	const exports = useQuery({
		queryKey: ["admin", "tenant-exports", tenant?.id],
		queryFn: () => listTenantExports(tenant?.id ?? ""),
		enabled: tenant !== null,
		retry: (failureCount, error) => !isAdminKeyError(error) && failureCount < 2,
		refetchInterval: (query) => (shouldPollExports(query.state.data?.items ?? []) ? 4000 : false),
		refetchIntervalInBackground: false,
	});
	const exportsError = exports.error;
	useEffect(() => {
		if (isAdminKeyError(exportsError)) onNeedsKey();
	}, [exportsError, onNeedsKey]);

	const items = exports.data?.items ?? [];

	// The flag rides the VARIABLES, never a closure over component state: the
	// section button passes the checkbox (there the checkbox IS the operator's
	// intent), and a row passes its own job's flag (WR-10 / T-03-73).
	const trigger = useMutation({
		mutationFn: (variables: { includeVaultValues: boolean }) =>
			triggerTenantExport(tenant?.id ?? "", variables.includeVaultValues),
		onSuccess: () => {
			void queryClient.invalidateQueries({ queryKey: ["admin", "tenant-exports"] });
		},
		onError: (error) => {
			if (isAdminKeyError(error)) onNeedsKey();
		},
	});

	// Presign-on-demand (03-18): one click, one link, one audit row. The
	// presigned URL is cross-origin, so navigate to it rather than synthesising
	// an anchor — a `download` attribute never survived the origin hop anyway.
	const download = useMutation({
		mutationFn: (variables: { jobId: string }) =>
			downloadTenantExport(tenant?.id ?? "", variables.jobId),
		onSuccess: (issued) => {
			window.location.href = issued.download_url;
		},
		onError: (error) => {
			if (isAdminKeyError(error)) onNeedsKey();
		},
	});

	return (
		<div className="space-y-4">
			<TenantPicker selected={tenant} onSelect={setTenant} onNeedsKey={onNeedsKey} />

			{tenant ? (
				<>
					<div className="space-y-2">
						<div className="flex items-start gap-2">
							<Checkbox
								id="export-include-vault-values"
								checked={includeVaultValues}
								onCheckedChange={(checked) => setIncludeVaultValues(checked === true)}
							/>
							<div className="space-y-1">
								<Label htmlFor="export-include-vault-values">Include decrypted vault secrets</Label>
								<p className="text-xs text-warning-muted-foreground">
									Off by default. Only enable when the tenant has asked for full portability — the
									export becomes a credentials bundle.
								</p>
							</div>
						</div>
						<div className="flex flex-wrap items-center gap-3">
							<Button
								size="sm"
								onClick={() => trigger.mutate({ includeVaultValues })}
								disabled={trigger.isPending}
							>
								{trigger.isPending ? <Spinner /> : null}
								Export tenant data
							</Button>
							{/* Mutation errors surface inline at the action, never as toasts. */}
							{trigger.error && !isAdminKeyError(trigger.error) ? (
								<p className="text-sm text-destructive-muted-foreground">
									{normalizeApiError(trigger.error)}
								</p>
							) : null}
						</div>
					</div>

					{exportsError && !isAdminKeyError(exportsError) ? (
						<ApiErrorPanel
							error={exportsError}
							onRetry={() => {
								void exports.refetch();
							}}
							title="Couldn't load exports"
						/>
					) : exports.isLoading ? (
						<div className="space-y-1.5">
							<Skeleton className="h-10 w-full" />
							<Skeleton className="h-10 w-full" />
						</div>
					) : items.length === 0 ? (
						<EmptyState variant="inset" title="No exports yet." />
					) : (
						// Bounded history: long job lists scroll inside the section
						// instead of stretching the page (backstop consideration).
						<ul className="max-h-80 divide-y divide-border overflow-y-auto rounded-lg border border-border bg-card">
							{items.map((job) => {
								const isThisJob = download.variables?.jobId === job.job_id;
								return (
									<ExportJobRow
										key={job.job_id}
										job={job}
										rowState={exportJobRowState(job)}
										onExportAgain={(includeVaultValues) => trigger.mutate({ includeVaultValues })}
										onDownload={() => download.mutate({ jobId: job.job_id })}
										downloadPending={download.isPending && isThisJob}
										downloadError={
											isThisJob && download.error && !isAdminKeyError(download.error)
												? download.error
												: null
										}
									/>
								);
							})}
						</ul>
					)}
				</>
			) : null}
		</div>
	);
}
