"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { EmptyState } from "@/components/empty-state";
import { SettingsPanelHeader } from "@/components/settings/settings-panel-header";
import { SettingsSection } from "@/components/settings-section";
import { TimeTooltip } from "@/components/time-tooltip";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { StatusBadge } from "@/components/ui/status-badge";
import { useApi, useOpenApi } from "@/lib/api";
import { shouldBlockQueryError } from "@/lib/query-state";
import { relativeTime } from "@/lib/utils";
import {
	DATA_PANEL_COPY,
	EXPORT_ROW_BADGE,
	type ExportRowState,
	exportFailedCopy,
	exportRowState,
	exportsRefetchInterval,
	runDownloadExport,
	runStartExport,
	sortExportsNewestFirst,
	TENANT_EXPORTS_QUERY_KEY,
	type TenantExportListItem,
	tenantExportRequests,
} from "./data-panel.logic";

export type DataPanelViewProps = {
	items: TenantExportListItem[] | undefined;
	isLoading: boolean;
	/** A BLOCKING list error — one with no cached list left to render. */
	listError: unknown;
	onRetry: () => void;
	onStart: () => void;
	startPending: boolean;
	startError: string | null;
	onDownload: (jobId: string) => void;
	downloadingJobId: string | null;
	downloadError: { jobId: string; message: string } | null;
};

/**
 * Settings → Data (hosted-only): start an export of the caller's own account
 * data, follow it from Preparing to Ready, and download it from a link the
 * backend mints per click (plan 06-19). The destination S5's "Export data
 * first" offer names.
 */
export function DataPanel() {
	const $api = useOpenApi();
	const api = useApi();
	const queryClient = useQueryClient();
	const requests = useMemo(() => tenantExportRequests(api), [api]);
	const exports = $api.useQuery(
		"get",
		"/v1/me/exports",
		{},
		{
			refetchInterval: (query) => exportsRefetchInterval(query.state.data?.items),
			refetchIntervalInBackground: false,
		},
	);

	const [startPending, setStartPending] = useState(false);
	const [startError, setStartError] = useState<string | null>(null);
	// Synchronous lock: `disabled` lands only on the next render, which leaves
	// a double click room to queue two jobs.
	const startLock = useRef(false);
	const [downloadingJobId, setDownloadingJobId] = useState<string | null>(null);
	const [downloadError, setDownloadError] = useState<DataPanelViewProps["downloadError"]>(null);

	async function handleStart() {
		if (startLock.current) return;
		startLock.current = true;
		setStartPending(true);
		setStartError(null);
		try {
			const outcome = await runStartExport({
				requestStart: requests.start,
				onStarted: async () => {
					toast.success(DATA_PANEL_COPY.startedToast);
					await queryClient.invalidateQueries({ queryKey: TENANT_EXPORTS_QUERY_KEY });
				},
			});
			setStartError(outcome.ok ? null : outcome.message);
		} finally {
			startLock.current = false;
			setStartPending(false);
		}
	}

	async function handleDownload(jobId: string) {
		if (downloadingJobId !== null) return;
		setDownloadingJobId(jobId);
		setDownloadError(null);
		try {
			// The presigned link is cross-origin and served as an attachment, so
			// following it downloads the archive without leaving the dashboard —
			// the admin console's pattern. The URL is never held in state.
			const outcome = await runDownloadExport({
				jobId,
				requestDownload: requests.downloadLink,
				openUrl: (url) => window.location.assign(url),
			});
			if (!outcome.ok) setDownloadError({ jobId, message: outcome.message });
		} finally {
			setDownloadingJobId(null);
		}
	}

	return (
		<DataPanelView
			items={exports.data?.items}
			isLoading={exports.isLoading}
			listError={shouldBlockQueryError(exports.error, exports.data) ? exports.error : null}
			onRetry={() => void exports.refetch()}
			onStart={() => void handleStart()}
			startPending={startPending}
			startError={startError}
			onDownload={(jobId) => void handleDownload(jobId)}
			downloadingJobId={downloadingJobId}
			downloadError={downloadError}
		/>
	);
}

/** Props-only render of every Data panel state (component-testable without a network). */
export function DataPanelView({
	items,
	isLoading,
	listError,
	onRetry,
	onStart,
	startPending,
	startError,
	onDownload,
	downloadingJobId,
	downloadError,
}: DataPanelViewProps) {
	return (
		<div className="flex flex-col gap-8 px-5 sm:px-6 lg:px-8">
			<SettingsPanelHeader
				title={DATA_PANEL_COPY.title}
				description={DATA_PANEL_COPY.description}
			/>

			<SettingsSection title={DATA_PANEL_COPY.sectionTitle} description={DATA_PANEL_COPY.intro}>
				<div className="flex flex-col gap-4">
					<div className="flex flex-col items-start gap-2">
						<Button type="button" size="sm" onClick={onStart} disabled={startPending}>
							{startPending ? <Spinner /> : null}
							{DATA_PANEL_COPY.startButton}
						</Button>
						{/* Errors render inline at the action, never as a toast. */}
						{startError ? (
							<p role="alert" className="text-sm text-destructive-muted-foreground">
								{startError}
							</p>
						) : null}
					</div>

					{listError ? (
						<ApiErrorPanel
							error={listError}
							onRetry={onRetry}
							title={DATA_PANEL_COPY.listErrorTitle}
						/>
					) : isLoading ? (
						<div className="flex flex-col gap-1.5" role="status">
							<span className="sr-only">Loading exports</span>
							<Skeleton className="h-10 w-full" />
							<Skeleton className="h-10 w-full" />
						</div>
					) : !items || items.length === 0 ? (
						<EmptyState
							variant="inset"
							title={DATA_PANEL_COPY.emptyTitle}
							description={DATA_PANEL_COPY.emptyBody}
						/>
					) : (
						// One scroll region only: the settings dialog's panel already
						// scrolls, so the list grows inside the section rather than
						// nesting a second scroller.
						<ul
							data-slot="export-list"
							className="divide-y divide-border rounded-lg border border-border bg-card"
						>
							{sortExportsNewestFirst(items).map((job) => (
								<ExportRow
									key={job.job_id}
									job={job}
									rowState={exportRowState(job)}
									onDownload={() => onDownload(job.job_id)}
									downloadPending={downloadingJobId === job.job_id}
									downloadDisabled={downloadingJobId !== null}
									downloadError={downloadError?.jobId === job.job_id ? downloadError.message : null}
								/>
							))}
						</ul>
					)}
				</div>
			</SettingsSection>
		</div>
	);
}

function ExportRow({
	job,
	rowState,
	onDownload,
	downloadPending,
	downloadDisabled,
	downloadError,
}: {
	job: TenantExportListItem;
	rowState: ExportRowState;
	onDownload: () => void;
	downloadPending: boolean;
	downloadDisabled: boolean;
	downloadError: string | null;
}) {
	const badge = EXPORT_ROW_BADGE[rowState];
	return (
		<li
			data-job-id={job.job_id}
			data-row-state={rowState}
			className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2"
		>
			<StatusBadge status={badge.tone}>{badge.label}</StatusBadge>
			<TimeTooltip value={job.created_at}>
				<span className="text-xs text-muted-foreground">
					{DATA_PANEL_COPY.startedAt} {relativeTime(job.created_at)}
				</span>
			</TimeTooltip>
			<span className="min-w-0 flex-1" />
			{rowState === "ready" ? (
				<span className="flex flex-wrap items-center gap-2">
					{downloadError ? (
						<span role="alert" className="text-xs text-destructive-muted-foreground">
							{downloadError}
						</span>
					) : null}
					<Button
						type="button"
						size="sm"
						variant="outline"
						onClick={onDownload}
						disabled={downloadDisabled}
					>
						{downloadPending ? <Spinner /> : <Download aria-hidden="true" />}
						{DATA_PANEL_COPY.download}
					</Button>
				</span>
			) : (
				<span
					data-slot="export-job-message"
					className="min-w-0 text-xs text-muted-foreground [overflow-wrap:anywhere]"
				>
					{rowState === "in-progress"
						? DATA_PANEL_COPY.preparingMessage
						: rowState === "failed"
							? exportFailedCopy(job.error)
							: DATA_PANEL_COPY.expiredMessage}
				</span>
			)}
		</li>
	);
}
