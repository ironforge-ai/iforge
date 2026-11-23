"use client";

import { Link } from "@tanstack/react-router";
import { ListChecks, PlusCircleIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { agentDisplayName } from "@/components/dashboard/agent-label";
import { EmptyState } from "@/components/empty-state";
import { ListToolbar } from "@/components/list-toolbar";
import { PageHeader, PageHeaderSkeleton } from "@/components/page-header";
import { CENTERED_PAGE_WIDTH_CLASS } from "@/components/page-width";
import { TimeTooltip } from "@/components/time-tooltip";
import { TruncatedText } from "@/components/truncated-text";
import { Button } from "@/components/ui/button";
import { DataTable, type DataTableColumnDef } from "@/components/ui/data-table";
import { DataTableFacetedFilter } from "@/components/ui/data-table-faceted-filter";
import { SearchInput } from "@/components/ui/search-input";
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { useOpenApi } from "@/lib/api";
import { shouldBlockQueryError } from "@/lib/query-state";
import { cn, formatAbsoluteTooltip, relativeTime } from "@/lib/utils";
import { CallDetailSheet } from "./call-detail-sheet";
import { COMPANION_SECTION_TABS, type CompanionSectionTab } from "./companions-overview.logic";
import {
	ACTIVITY_EMPTY_COPY,
	ACTIVITY_FILTER_LABELS,
	ACTIVITY_FILTERED_EMPTY_COPY,
	ACTIVITY_PAGE_COPY,
	ACTIVITY_PAGE_SIZE_OPTIONS,
	type ActivityFilters,
	activeFacetCount,
	activityColumns,
	activityQueryParams,
	CALL_DECISIONS,
	type CompanionToolCall,
	capDebitLabel,
	clampPageSize,
	DEFAULT_ACTIVITY_PAGE_SIZE,
	decisionLabel,
	decisionTone,
	EMPTY_ACTIVITY_FILTERS,
	hasFullPage,
	sessionCellValue,
	sessionFacetOptions,
	visibleActivityRows,
} from "./tool-activity.logic";
import { TOOL_CLASSES, toolClassTone } from "./tool-policy.logic";

/** openapi-react-query cache key for the activity listing. */
export const COMPANION_ACTIVITY_QUERY_KEY = ["get", "/v1/companions/activity"] as const;

const SURFACE_CLASS = cn(CENTERED_PAGE_WIDTH_CLASS.page, "space-y-6 px-4 lg:px-6");

/**
 * The Companions / Tools / Activity strip, rendered on every companion route.
 *
 * It lives here rather than in any one page because no page can reach the
 * others without it: the sidebar entry resolves to the Tools index only, so a
 * surface with no link into it is a surface nobody can open. The order is
 * fixed by `06-UI-SPEC.md` §Navigation and read from `COMPANION_SECTION_TABS`
 * — installing precedes configuring, so Companions comes first. `active` is
 * omitted on a companion detail page, which renders the strip with no tab on.
 */
export function CompanionSectionTabs({
	agentId,
	active,
}: {
	agentId: string;
	active?: CompanionSectionTab;
}) {
	const base =
		"rounded-md px-2 py-1 text-sm transition-colors hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring";
	const on = "bg-muted font-medium text-foreground";
	const off = "text-muted-foreground";
	return (
		<nav data-slot="companion-section-tabs" aria-label="Companions" className="flex gap-1">
			{COMPANION_SECTION_TABS.map((tab) => {
				const className = cn(base, active === tab.key ? on : off);
				const current = active === tab.key ? ("page" as const) : undefined;
				switch (tab.key) {
					case "companions":
						return (
							<Link
								key={tab.key}
								to="/agents/$id/companions/installed"
								params={{ id: agentId }}
								className={className}
								aria-current={current}
							>
								{tab.label}
							</Link>
						);
					case "tools":
						return (
							<Link
								key={tab.key}
								to="/agents/$id/companions"
								params={{ id: agentId }}
								className={className}
								aria-current={current}
							>
								{ACTIVITY_PAGE_COPY.toolsTab}
							</Link>
						);
					default:
						return (
							<Link
								key={tab.key}
								to="/agents/$id/companions/activity"
								params={{ id: agentId }}
								className={className}
								aria-current={current}
							>
								{ACTIVITY_PAGE_COPY.activityTab}
							</Link>
						);
				}
			})}
		</nav>
	);
}

/**
 * The Tool Activity page: every ATTEMPT this agent made through the façade.
 *
 * The query carries four facets, a bound and an offset, and nothing else —
 * `activityQueryParams` is the single place that shape is decided and its test
 * asserts the key set, because a filter that could carry a tenant identifier is
 * the one way an audit surface becomes a cross-tenant read.
 */
export function ToolActivitySurface({ environmentId }: { environmentId: string }) {
	const $api = useOpenApi();
	const [filters, setFilters] = useState<ActivityFilters>(EMPTY_ACTIVITY_FILTERS);
	const [page, setPage] = useState(1);
	const [pageSize, setPageSize] = useState(DEFAULT_ACTIVITY_PAGE_SIZE);
	const [openCallId, setOpenCallId] = useState<string | null>(null);

	const query = $api.useQuery("get", "/v1/companions/activity", {
		params: { query: activityQueryParams(filters, page, pageSize) },
	});
	// The Companion facet's option source. It is a real query and it can fail,
	// which is what makes the spec's "a failed facet-options query renders that
	// trigger disabled with its label intact" a state this surface can reach.
	const catalog = $api.useQuery("get", "/v1/companions/tools", {});
	// `environment_id` is a raw identifier and plan 05-07 exposed no name for it.
	// This page already knows which agent it is, so it supplies the name for its
	// OWN id; the chain falls back to the identifier for anything else rather
	// than inventing a label.
	const agent = $api.useQuery("get", "/v1/agents/{agent_id}", {
		params: { path: { agent_id: environmentId } },
	});

	const items = query.data?.items ?? [];
	const error = shouldBlockQueryError(query.error, query.data) ? query.error : null;

	return (
		<div className={SURFACE_CLASS} data-testid="tool-activity-surface">
			{query.isLoading && !query.data ? (
				<PageHeaderSkeleton />
			) : (
				<PageHeader title={ACTIVITY_PAGE_COPY.title} description={ACTIVITY_PAGE_COPY.description} />
			)}
			<CompanionSectionTabs agentId={environmentId} active="activity" />
			<ToolActivityRegion
				agentId={environmentId}
				items={items}
				loading={query.isLoading}
				error={error}
				onRetry={() => void query.refetch()}
				filters={filters}
				onFiltersChange={(next) => {
					setFilters(next);
					setPage(1);
				}}
				companionOptions={(catalog.data?.companions ?? []).map((companion) => ({
					label: companion.companion_key,
					value: companion.companion_key,
				}))}
				companionOptionsError={Boolean(catalog.error)}
				page={page}
				pageSize={pageSize}
				onPageChange={setPage}
				onPageSizeChange={(size) => {
					setPageSize(clampPageSize(size));
					setPage(1);
				}}
				onOpenCall={setOpenCallId}
			/>
			<CallDetailSheet
				callId={openCallId}
				row={items.find((item) => item.id === openCallId) ?? null}
				open={openCallId !== null}
				onOpenChange={(next) => {
					if (!next) setOpenCallId(null);
				}}
				agentId={environmentId}
				agentLabel={agent.data ? agentDisplayName(agent.data) : undefined}
				onOpenCall={setOpenCallId}
			/>
		</div>
	);
}

/**
 * The activity region as a pure function of its props.
 *
 * Split from the connected surface for the reason plan 05-08 established: every
 * UI state — empty, filtered-to-zero, loading, error, populated, one row — is
 * then reachable from a test with no router and no query client.
 */
export function ToolActivityRegion({
	agentId,
	items,
	loading = false,
	error,
	onRetry,
	filters,
	onFiltersChange,
	companionOptions,
	companionOptionsError = false,
	page,
	pageSize,
	onPageChange,
	onPageSizeChange,
	onOpenCall,
}: {
	agentId: string;
	items: readonly CompanionToolCall[];
	loading?: boolean;
	error?: unknown;
	onRetry?: () => void;
	filters: ActivityFilters;
	onFiltersChange: (next: ActivityFilters) => void;
	companionOptions: readonly { label: string; value: string }[];
	companionOptionsError?: boolean;
	page: number;
	pageSize: number;
	onPageChange: (page: number) => void;
	onPageSizeChange: (size: number) => void;
	/**
	 * Opens the provenance drawer for one row. Optional on purpose: while it is
	 * absent the rows carry no click affordance at all, rather than a handler
	 * wired to nothing. Same rule plan 05-08 applied to "Review details".
	 */
	onOpenCall?: (callId: string) => void;
}) {
	const rows = visibleActivityRows(items, agentId, filters);
	const facets = activeFacetCount(filters);
	const columns = useMemo(
		() => buildActivityColumns((sessionId) => onFiltersChange({ ...filters, sessionId })),
		[filters, onFiltersChange],
	);

	// The toolbar is mounted FIRST and unconditionally — before the data, above
	// every other branch — so applying a filter never reflows the control row and
	// an error state does not take the filters away with it.
	const toolbar = (
		<ListToolbar
			search={
				<SearchInput
					value={filters.search}
					onChange={(search) => onFiltersChange({ ...filters, search })}
					placeholder={ACTIVITY_PAGE_COPY.searchPlaceholder}
					ariaLabel="Search tool activity"
				/>
			}
			filters={
				<>
					<ActivityFacet
						label={ACTIVITY_FILTER_LABELS.toolClass}
						options={TOOL_CLASSES.map((value) => ({ label: value, value }))}
						selected={filters.toolClass}
						onSelect={(toolClass) => onFiltersChange({ ...filters, toolClass })}
					/>
					<ActivityFacet
						label={ACTIVITY_FILTER_LABELS.decision}
						options={CALL_DECISIONS.map((value) => ({
							label: decisionLabel(value),
							value,
						}))}
						selected={filters.decision}
						onSelect={(decision) =>
							onFiltersChange({
								...filters,
								decision: (decision as ActivityFilters["decision"]) ?? null,
							})
						}
					/>
					<ActivityFacet
						label={ACTIVITY_FILTER_LABELS.companionKey}
						options={companionOptions}
						selected={filters.companionKey}
						onSelect={(companionKey) => onFiltersChange({ ...filters, companionKey })}
						failed={companionOptionsError}
					/>
					<ActivityFacet
						label={ACTIVITY_FILTER_LABELS.sessionId}
						options={sessionFacetOptions(items)}
						selected={filters.sessionId}
						onSelect={(sessionId) => onFiltersChange({ ...filters, sessionId })}
					/>
				</>
			}
			actions={
				facets > 0 ? (
					<Button
						type="button"
						variant="ghost"
						size="sm"
						className="h-8 px-2"
						onClick={() => onFiltersChange(EMPTY_ACTIVITY_FILTERS)}
					>
						{ACTIVITY_PAGE_COPY.clearFilters}
					</Button>
				) : null
			}
		/>
	);

	return (
		<section data-slot="tool-activity-region" className="space-y-4">
			{toolbar}
			{error ? (
				<ApiErrorPanel error={error} onRetry={onRetry} title="Couldn't load tool activity" />
			) : loading ? (
				<DataTable
					columns={columns}
					data={[]}
					isLoading
					tableContainerClassName="overflow-x-auto"
				/>
			) : rows.length === 0 ? (
				facets > 0 ? (
					<EmptyState
						variant="inset"
						title={ACTIVITY_FILTERED_EMPTY_COPY.title}
						description={ACTIVITY_FILTERED_EMPTY_COPY.description}
						action={
							<Button
								type="button"
								variant="outline"
								size="sm"
								onClick={() => onFiltersChange(EMPTY_ACTIVITY_FILTERS)}
							>
								{ACTIVITY_FILTERED_EMPTY_COPY.action}
							</Button>
						}
					/>
				) : (
					<EmptyState
						variant="page"
						icon={ListChecks}
						title={ACTIVITY_EMPTY_COPY.title}
						description={ACTIVITY_EMPTY_COPY.description}
					/>
				)
			) : (
				<>
					{/* The container scrolls; no column is ever dropped. Each column
					    carries an explicit width, so the seven together exceed a narrow
					    viewport and the table's own `overflow-x-auto` takes over — which
					    is the recorded choice for the spec's unresolved narrow-width row.
					    Dropping a column would silently remove evidence. */}
					<DataTable
						columns={columns}
						data={rows as CompanionToolCall[]}
						onRowClick={onOpenCall ? (row) => onOpenCall(row.id) : undefined}
						tableContainerClassName="overflow-x-auto"
					/>
					<ActivityPagination
						page={page}
						pageSize={pageSize}
						rowsOnPage={rows.length}
						hasNext={hasFullPage(items, pageSize)}
						onPageChange={onPageChange}
						onPageSizeChange={onPageSizeChange}
					/>
				</>
			)}
		</section>
	);
}

/**
 * One faceted filter trigger, or the disabled stand-in when its options failed.
 *
 * A failed or unloaded option source renders the trigger DISABLED with its
 * label intact rather than removing it, so the control row keeps its geometry
 * and a reader can see that the facet exists and why it is unusable — instead
 * of a filter silently vanishing from an audit surface.
 */
export function ActivityFacet({
	label,
	options,
	selected,
	onSelect,
	failed = false,
}: {
	label: string;
	options: readonly { label: string; value: string }[];
	selected: string | null;
	onSelect: (value: string | null) => void;
	failed?: boolean;
}) {
	if (failed || options.length === 0) {
		return (
			<Button
				type="button"
				variant="outline"
				size="sm"
				disabled
				className="h-8 border-dashed"
				data-slot="activity-facet-unavailable"
				data-facet={label}
			>
				<PlusCircleIcon className="size-4" />
				{label}
			</Button>
		);
	}
	return (
		<span
			data-slot="activity-facet"
			data-facet={label}
			className="inline-flex max-w-[12rem] min-w-0 [&_[data-slot=badge]]:max-w-[8rem] [&_[data-slot=badge]]:truncate"
		>
			<DataTableFacetedFilter
				title={label}
				options={options as { label: string; value: string }[]}
				selected={selected ? [selected] : []}
				onChange={(next) => onSelect(next[0] ?? null)}
			/>
		</span>
	);
}

/**
 * Prev / next paging with no fabricated total.
 *
 * `DataTablePagination` renders `{first}–{last} of {total}` and `{page} /
 * {pageCount}`, and the listing response carries no total to put in either
 * (plan 05-07 note 4: `{items: [...]}`, matching the audit precedent). Passing
 * a synthetic total would print a count the server never sent onto the one
 * surface whose whole value is that it does not invent facts, so this renders
 * what IS known — the rows on this page, and whether the page came back full.
 */
export function ActivityPagination({
	page,
	pageSize,
	rowsOnPage,
	hasNext,
	onPageChange,
	onPageSizeChange,
}: {
	page: number;
	pageSize: number;
	rowsOnPage: number;
	hasNext: boolean;
	onPageChange: (page: number) => void;
	onPageSizeChange: (size: number) => void;
}) {
	const size = clampPageSize(pageSize);
	const first = rowsOnPage === 0 ? 0 : (page - 1) * size + 1;
	const last = (page - 1) * size + rowsOnPage;
	return (
		<div
			data-slot="activity-pagination"
			className="flex flex-col-reverse items-center justify-between gap-3 px-1 sm:flex-row"
		>
			<div className="text-muted-foreground text-sm tabular-nums">
				{rowsOnPage === 0 ? "0 calls" : `Showing ${first}–${last}`}
			</div>
			<div className="flex w-full flex-col items-center gap-3 sm:w-auto sm:flex-row sm:gap-6">
				<div className="flex items-center gap-2">
					<span className="text-muted-foreground text-sm">Rows</span>
					<Select value={String(size)} onValueChange={(v) => onPageSizeChange(Number(v))}>
						<SelectTrigger size="sm" className="w-[80px]">
							<SelectValue />
						</SelectTrigger>
						<SelectContent>
							{ACTIVITY_PAGE_SIZE_OPTIONS.map((n) => (
								<SelectItem key={n} value={String(n)}>
									{n}
								</SelectItem>
							))}
						</SelectContent>
					</Select>
				</div>
				<div className="flex items-center gap-1">
					<Button
						type="button"
						variant="outline"
						size="sm"
						disabled={page <= 1}
						onClick={() => onPageChange(page - 1)}
					>
						Previous
					</Button>
					<span className="min-w-16 px-2 text-center text-sm tabular-nums whitespace-nowrap">
						Page {page}
					</span>
					<Button
						type="button"
						variant="outline"
						size="sm"
						disabled={!hasNext}
						onClick={() => onPageChange(page + 1)}
					>
						Next
					</Button>
				</div>
			</div>
		</div>
	);
}

/**
 * The seven columns, built from the descriptors in `tool-activity.logic.ts`.
 *
 * `hideable: false` on every descriptor is the recorded answer to the spec's
 * unresolved narrow-width question: the container scrolls horizontally and no
 * column is ever dropped, because a dropped column silently removes evidence
 * from an audit surface.
 */
export function buildActivityColumns(
	onFilterBySession: (sessionId: string) => void,
): DataTableColumnDef<CompanionToolCall>[] {
	const cells: Record<string, DataTableColumnDef<CompanionToolCall>["cell"]> = {
		created_at: ({ row }) => (
			<TimeTooltip value={row.original.created_at}>
				<span
					data-slot="activity-time"
					className="whitespace-nowrap text-muted-foreground text-sm"
					title={formatAbsoluteTooltip(row.original.created_at)}
				>
					{relativeTime(row.original.created_at)}
				</span>
			</TimeTooltip>
		),
		// Mono at the small type size so the `kind__verb` double underscore stays
		// visually intact; a proportional font makes `__` read as `_`. A name the
		// façade could not resolve renders exactly as recorded — the recorded
		// value is the evidence, so nothing here infers a prefix or blanks a cell.
		tool: ({ row }) => (
			<TruncatedText
				data-slot="activity-tool"
				className="block max-w-full font-mono text-xs"
				title={row.original.tool}
			>
				{row.original.tool}
			</TruncatedText>
		),
		tool_class: ({ row }) => (
			<StatusBadge status={toolClassTone(row.original.tool_class)} className="font-mono">
				{row.original.tool_class}
			</StatusBadge>
		),
		// A refusal is a NORMAL row with a destructive-tone chip. `ApiErrorPanel`
		// is for a failed query; a refused call is the log working.
		decision: ({ row }) => (
			<StatusBadge
				status={decisionTone(row.original.decision)}
				data-decision={row.original.decision}
			>
				{decisionLabel(row.original.decision)}
			</StatusBadge>
		),
		dry_run: ({ row }) =>
			row.original.dry_run ? <StatusBadge status="info">Dry run</StatusBadge> : null,
		// Not a link: `session_id` is an opaque MCP protocol string with no row in
		// `sessions` (plan 05-07 chose a plain column over a foreign key for
		// exactly that reason), so a hyperlink would be a dead one. Clicking
		// narrows the log to that session, which is the real action.
		session_id: ({ row }) =>
			row.original.session_id ? (
				<button
					type="button"
					data-slot="activity-session"
					className="relative z-10 max-w-full truncate font-mono text-xs underline-offset-2 hover:underline"
					title={row.original.session_id}
					onClick={(event) => {
						event.stopPropagation();
						if (row.original.session_id) onFilterBySession(row.original.session_id);
					}}
				>
					{row.original.session_id}
				</button>
			) : (
				<span data-slot="activity-session" className="text-muted-foreground text-xs">
					{sessionCellValue(row.original.session_id)}
				</span>
			),
		cap_debit: ({ row }) => (
			<span data-slot="activity-cap-debit" className="text-sm tabular-nums">
				{capDebitLabel(row.original)}
			</span>
		),
	};
	return activityColumns.map((column) => ({
		id: column.id,
		header: column.header,
		size: column.size,
		enableHiding: column.hideable,
		cell: cells[column.id],
	}));
}
