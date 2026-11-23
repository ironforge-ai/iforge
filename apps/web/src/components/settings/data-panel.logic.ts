import type { components, paths } from "@clawdi/shared/api";
import type { Client } from "openapi-fetch";
import type { StatusTone } from "@/components/ui/status-badge";
import { unwrap } from "@/lib/api";

/**
 * Settings → Data — the tenant's own data export (APP-07, D-31) — as data
 * rather than as markup.
 *
 * `06-UI-SPEC.md` names "Settings → Data" as the destination of S5's export
 * offer but contracts no panel, so every string below is AUTHORED under the
 * house rules (sentence case, errors inline and never toasted, one success
 * toast from the S5 toast list) and recorded in the 06-33 SUMMARY for the UI
 * review. The row rules mirror the admin console's export helpers
 * (`pages/dashboard/admin/export-section.tsx`) against the TENANT list item,
 * which carries no vault flag and no admin identifiers, rather than importing
 * admin types.
 *
 * The presigned download link is bearer-equivalent for its whole life
 * (T-06-33-01). Nothing here keeps it: `runDownloadExport` hands it to
 * `openUrl` and returns only whether that happened.
 */

type Schemas = components["schemas"];

export type TenantExportListItem = Schemas["TenantExportListItem"];
export type TenantExportStatus = TenantExportListItem["status"];
export type TenantExportCreated = Schemas["TenantExportCreatedResponse"];
export type TenantExportDownload = Schemas["TenantExportDownloadResponse"];

/** openapi-react-query cache key for the caller's export list. */
export const TENANT_EXPORTS_QUERY_KEY = ["get", "/v1/me/exports"] as const;

/** Poll cadence while any export is still being prepared. */
export const EXPORT_POLL_INTERVAL_MS = 5_000;

/** Authored-not-contracted copy (see the module comment). */
export const DATA_PANEL_COPY = {
	title: "Data",
	description: "Download a copy of your account data.",
	sectionTitle: "Exports",
	intro:
		"A copy of your account data, including each companion's database and uploaded media. Download links expire; start a new export any time.",
	startButton: "Start export",
	startedToast: "Export started",
	startRejected: "The export was not started. Try again.",
	emptyTitle: "No exports yet",
	emptyBody: "Exports you start appear here.",
	listErrorTitle: "Couldn't load exports",
	startedAt: "Started",
	preparingMessage: "This can take a few minutes.",
	download: "Download",
	downloadRejected: "The download link was not created. Try again.",
	expiredMessage: "Start a new export to download this data again.",
	failed: "Export failed. Start a new export to try again.",
} as const;

/**
 * Tenant sentences for the named, terminal companion failures plan 06-19
 * records as `"{code}: {detail}"`. Keyed by that code; the detail (internal
 * installation ids, byte counts) is never shown.
 */
export const COMPANION_EXPORT_FAILURE_COPY: Readonly<Record<string, string>> = {
	companion_export_component_timeout:
		"Export failed: a companion did not send its data in time. Start a new export to try again.",
	companion_export_component_failed:
		"Export failed: a companion could not send its data. Start a new export to try again.",
	companion_media_exceeds_export_bound:
		"Export failed: a companion's uploaded media is larger than an export can carry.",
};

/**
 * The failed-row sentence — the admin `exportFailedCopy` shape (one sentence,
 * sharpened by the recorded error when it names something) without its
 * payload: the admin console appends the raw exception string, which is
 * operator detail the phase 03 threat model kept behind the admin key
 * (T-03-76). A tenant gets a named companion failure explained, and every
 * other failure as the generic sentence. The generic sentence promises no
 * automatic retry, because a companion failure is terminal (plan 06-19) and
 * the tenant list does not say which kind a job hit.
 */
export function exportFailedCopy(error: string | null): string {
	if (error) {
		const code = error.split(":", 1)[0]?.trim() ?? "";
		if (Object.hasOwn(COMPANION_EXPORT_FAILURE_COPY, code)) {
			return COMPANION_EXPORT_FAILURE_COPY[code];
		}
	}
	return DATA_PANEL_COPY.failed;
}

function createdAtMs(job: TenantExportListItem): number {
	const ms = Date.parse(job.created_at);
	return Number.isNaN(ms) ? Number.NEGATIVE_INFINITY : ms;
}

/** Newest first by created time; ties (and unparseable times, last) keep the server's order. */
export function sortExportsNewestFirst(
	items: readonly TenantExportListItem[],
): TenantExportListItem[] {
	return [...items].sort((left, right) => {
		const a = createdAtMs(left);
		const b = createdAtMs(right);
		return a === b ? 0 : b > a ? 1 : -1;
	});
}

export type ExportRowState = "in-progress" | "ready" | "failed" | "link-expired";

/** The one chip each row wears — labels authored, tones from `StatusBadge`'s semantic set. */
export const EXPORT_ROW_BADGE: Record<ExportRowState, { label: string; tone: StatusTone }> = {
	"in-progress": { label: "Preparing", tone: "info" },
	ready: { label: "Ready", tone: "success" },
	failed: { label: "Failed", tone: "destructive" },
	"link-expired": { label: "Link expired", tone: "neutral" },
};

function isInProgress(status: TenantExportStatus): boolean {
	return status === "queued" || status === "running";
}

/**
 * Which branch a row renders — the admin `exportJobRowState` rule.
 *
 * `downloadable` is the backend's own predicate (true exactly when the
 * download route answers 200 rather than 409), so a Ready row can never
 * promise a link the server will refuse, and a complete job whose archive is
 * gone reads "Link expired" with no Download.
 */
export function exportRowState(job: TenantExportListItem): ExportRowState {
	if (isInProgress(job.status)) return "in-progress";
	if (job.status === "failed") return "failed";
	return job.downloadable ? "ready" : "link-expired";
}

/** Poll while any job is still moving; stop once everything is terminal. */
export function shouldPollExports(items: readonly TenantExportListItem[]): boolean {
	return items.some((job) => isInProgress(job.status));
}

/** The list query's `refetchInterval`: 5 s while a job is in progress, off otherwise. */
export function exportsRefetchInterval(
	items: readonly TenantExportListItem[] | undefined,
): number | false {
	return items && shouldPollExports(items) ? EXPORT_POLL_INTERVAL_MS : false;
}

export type StartOutcome = { ok: true } | { ok: false; message: string };
export type DownloadOutcome = { ok: true } | { ok: false; message: string };

/**
 * What the Start export button runs. A rejected request resolves to the
 * inline sentence and nothing else, so a failure can never reach a toast;
 * `onStarted` (the toast and the list refresh) runs only for an accepted job.
 */
export async function runStartExport({
	requestStart,
	onStarted,
}: {
	requestStart: () => Promise<TenantExportCreated>;
	onStarted: (created: TenantExportCreated) => unknown;
}): Promise<StartOutcome> {
	let created: TenantExportCreated;
	try {
		created = await requestStart();
	} catch {
		return { ok: false, message: DATA_PANEL_COPY.startRejected };
	}
	await onStarted(created);
	return { ok: true };
}

/**
 * What a Ready row's Download runs: ask the backend for one freshly minted
 * link to THIS job (one link, one audit row — plan 06-19) and open it. The
 * link is passed straight to `openUrl` and never returned, stored or logged.
 */
export async function runDownloadExport({
	jobId,
	requestDownload,
	openUrl,
}: {
	jobId: string;
	requestDownload: (jobId: string) => Promise<TenantExportDownload>;
	openUrl: (url: string) => unknown;
}): Promise<DownloadOutcome> {
	let issued: TenantExportDownload;
	try {
		issued = await requestDownload(jobId);
	} catch {
		return { ok: false, message: DATA_PANEL_COPY.downloadRejected };
	}
	openUrl(issued.url);
	return { ok: true };
}

/**
 * The panel's two writes over the generated client. Both routes are scoped to
 * the caller server-side (T-06-33-02); the panel sends no tenant identifier.
 */
export function tenantExportRequests(api: Client<paths>) {
	return {
		start: async (): Promise<TenantExportCreated> => unwrap(await api.POST("/v1/me/exports")),
		downloadLink: async (jobId: string): Promise<TenantExportDownload> =>
			unwrap(
				await api.GET("/v1/me/exports/{job_id}/download", {
					params: { path: { job_id: jobId } },
				}),
			),
	};
}
