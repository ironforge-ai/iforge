"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Check, ChevronRight, Copy, ExternalLink, MoreHorizontal } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { useSetBreadcrumbTitle } from "@/components/breadcrumb-title";
import { EmptyState } from "@/components/empty-state";
import { PageHeader, PageHeaderSkeleton } from "@/components/page-header";
import { CENTERED_PAGE_WIDTH_CLASS } from "@/components/page-width";
import { TimeTooltip } from "@/components/time-tooltip";
import { TruncatedText } from "@/components/truncated-text";
import {
	Breadcrumb,
	BreadcrumbItem,
	BreadcrumbLink,
	BreadcrumbList,
	BreadcrumbPage,
	BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { Button } from "@/components/ui/button";
import { ConfirmAction } from "@/components/ui/confirm-action";
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { StatusBadge } from "@/components/ui/status-badge";
import { useCopyToClipboard } from "@/hooks/use-copy-to-clipboard";
import { useApi, useOpenApi } from "@/lib/api";
import { useIsomorphicLayoutEffect } from "@/lib/use-isomorphic-layout-effect";
import { cn, relativeTime } from "@/lib/utils";
import {
	ACCESS_COPY,
	type CompanionLogLine,
	companionName,
	companionRequests,
	completeSummary,
	DATA_COPY,
	DETAIL_COPY,
	DETAIL_POLL_INTERVAL_MS,
	digestPrefix,
	installationRefetchInterval,
	installFailure,
	isJobActive,
	isScrolledToBottom,
	isServing,
	jobChip,
	jobSteps,
	LOG_EMPTY_COPY,
	logCollapsed,
	mediaNote,
	mergeLogLines,
	notAnsweringCopy,
	notInstalledCopy,
	openCompanionLabel,
	readLogAfter,
	resolveInstallation,
	runLoginLink,
	runUpdateDecision,
	STREAM_MARKERS,
	setPasswordLabel,
	showNotAnswering,
	signInAsLabel,
	sourcePointer,
	tierRowValue,
	tlsNote,
	UPDATES_COPY,
	updateCard,
	updateChip,
	updateConfirmDescription,
	updateConfirmTitle,
	updateRejectedCopy,
} from "./companion-detail.logic";
import { CompanionIdentityTile } from "./companions-overview";
import {
	blockingFailure,
	COMPANION_INSTALLATIONS_QUERY_KEY,
	type CompanionInstallation,
	ISOLATION_TIER_LABELS,
	OVERVIEW_ERROR_NORMALIZER,
	reachabilityChip,
} from "./companions-overview.logic";
import { CompanionSectionTabs } from "./tool-activity-table";
import { UninstallCompanionDialog } from "./uninstall-companion-dialog";

const SURFACE_CLASS = cn(CENTERED_PAGE_WIDTH_CLASS.page, "space-y-6 px-4 lg:px-6");

/** The install log well — DESIGN.md "never break the data" for log lines. */
const LOG_WELL_CLASS =
	"max-h-96 overflow-y-auto rounded-lg bg-muted/30 p-4 font-mono text-xs leading-relaxed whitespace-pre-wrap break-all";

/**
 * S3 — one companion on one agent.
 *
 * Owns the reads (catalog for the name, the agent's installations for the
 * companion, the install log) and the polling; everything it renders is drawn
 * by `CompanionDetailView`, which takes props only so every S3 state renders in
 * a test without a network.
 */
export function CompanionDetail({
	agentId,
	companionKey,
}: {
	agentId: string;
	companionKey: string;
}) {
	const $api = useOpenApi();

	const catalog = $api.useQuery("get", "/v1/companions/catalog", {});
	const installations = $api.useQuery(
		"get",
		"/v1/companions/installations",
		{ params: { query: { agent_id: agentId } } },
		{
			// Polls only while the job is queued or running; a terminal state stops it.
			refetchInterval: (query) =>
				installationRefetchInterval(resolveInstallation(query.state.data?.items, companionKey)),
		},
	);

	const name = companionName(catalog.data?.items, companionKey);
	useSetBreadcrumbTitle(catalog.data ? name : null);

	const installation = installations.data
		? resolveInstallation(installations.data.items, companionKey)
		: undefined;
	const failure = blockingFailure([installations, catalog]);
	const log = useInstallLog(installation?.id, isJobActive(installation));
	const actions = useCompanionActions(installation ?? null, name);

	// S5 opens from the row menu, whose item unmounts as the menu closes, so the
	// page owns the dialog. Keyed per open: every open starts with nothing typed.
	const [uninstallOpen, setUninstallOpen] = useState(false);
	const [uninstallSession, setUninstallSession] = useState(0);

	return (
		<>
			<CompanionDetailView
				agentId={agentId}
				companionKey={companionKey}
				name={name}
				tabs={<CompanionSectionTabs agentId={agentId} />}
				installation={installation}
				loading={
					(installations.isLoading && !installations.data) || (catalog.isLoading && !catalog.data)
				}
				error={failure?.error}
				onRetry={failure?.retry}
				logLines={log.lines}
				logError={log.error}
				onRetryLog={log.retry}
				onUninstall={() => {
					setUninstallSession((session) => session + 1);
					setUninstallOpen(true);
				}}
				onSetPassword={actions.setPassword}
				loginLinkPending={actions.loginLinkPending}
				loginLinkError={actions.loginLinkError}
				onAcceptUpdate={actions.acceptUpdate}
				onDeferUpdate={actions.deferUpdate}
				deferPending={actions.deferPending}
				deferError={actions.deferError}
			/>
			{installation && uninstallSession > 0 ? (
				<UninstallCompanionDialog
					key={`${installation.id}:${uninstallSession}`}
					installationId={installation.id}
					name={name}
					open={uninstallOpen}
					onOpenChange={setUninstallOpen}
				/>
			) : null}
		</>
	);
}

/**
 * S3's writes other than S5: the set-password link (CK-D) and the update
 * decision. Every refusal lands inline at its control; only successes toast.
 */
function useCompanionActions(installation: CompanionInstallation | null, name: string) {
	const api = useApi();
	const queryClient = useQueryClient();
	const [loginLinkPending, setLoginLinkPending] = useState(false);
	const [loginLinkError, setLoginLinkError] = useState<string | null>(null);
	const [deferPending, setDeferPending] = useState(false);
	const [deferError, setDeferError] = useState<string | null>(null);
	const loginLock = useRef(false);
	const deferLock = useRef(false);

	async function refetch() {
		await queryClient.invalidateQueries({ queryKey: COMPANION_INSTALLATIONS_QUERY_KEY });
	}

	async function setPassword() {
		if (!installation || loginLock.current) return;
		loginLock.current = true;
		// Opened inside the click so a popup blocker allows it; pointed at the
		// minted link once it arrives, and closed again if none does.
		const popup = window.open("", "_blank");
		if (popup) popup.opener = null;
		setLoginLinkPending(true);
		setLoginLinkError(null);
		const outcome = await runLoginLink({
			name,
			requestLink: companionRequests(api, installation.id).loginLink,
			openUrl: (url) => {
				if (popup && !popup.closed) popup.location.replace(url);
				else window.open(url, "_blank", "noopener,noreferrer");
			},
		});
		if (!outcome.ok) {
			popup?.close();
			setLoginLinkError(outcome.message);
		}
		setLoginLinkPending(false);
		loginLock.current = false;
	}

	/** `ConfirmAction`'s onConfirm: a refusal throws its sentence so it renders in the open dialog. */
	async function acceptUpdate(digest: string) {
		if (!installation) return;
		const outcome = await runUpdateDecision({
			decision: "accept",
			digest,
			requestDecision: companionRequests(api, installation.id).decideUpdate,
			rejectedMessage: updateRejectedCopy(name),
			onDone: async () => {
				toast.success(UPDATES_COPY.startedToast);
				await refetch();
			},
		});
		if (!outcome.ok) throw new Error(outcome.message);
	}

	async function deferUpdate(digest: string) {
		if (!installation || deferLock.current) return;
		deferLock.current = true;
		setDeferPending(true);
		setDeferError(null);
		const outcome = await runUpdateDecision({
			decision: "defer",
			digest,
			requestDecision: companionRequests(api, installation.id).decideUpdate,
			rejectedMessage: UPDATES_COPY.deferRejected,
			onDone: async () => {
				toast.success(UPDATES_COPY.deferredToast);
				await refetch();
			},
		});
		if (!outcome.ok) setDeferError(outcome.message);
		setDeferPending(false);
		deferLock.current = false;
	}

	return {
		setPassword: () => {
			void setPassword();
		},
		loginLinkPending,
		loginLinkError,
		acceptUpdate,
		deferUpdate: (digest: string) => {
			void deferUpdate(digest);
		},
		deferPending,
		deferError,
	};
}

/**
 * Follow one installation's log by `after_seq`: read everything on mount, then
 * every two seconds while the job is active, and once more when it stops so
 * the final lines land. Lines are appended by `seq`, never re-fetched whole.
 */
function useInstallLog(installationId: string | undefined, active: boolean) {
	const api = useApi();
	const [log, setLog] = useState<{
		id: string | null;
		lines: CompanionLogLine[];
		error: unknown;
	}>({ id: null, lines: [], error: null });
	const cursor = useRef<{ id: string | null; afterSeq: number }>({ id: null, afterSeq: 0 });
	const [attempt, setAttempt] = useState(0);

	useEffect(() => {
		if (!installationId) return;
		if (cursor.current.id !== installationId) {
			cursor.current = { id: installationId, afterSeq: 0 };
		}
		let cancelled = false;
		let timer: ReturnType<typeof setTimeout> | undefined;
		const fetchPage = companionRequests(api, installationId).logPage;

		async function tick() {
			try {
				const read = await readLogAfter({ afterSeq: cursor.current.afterSeq, fetchPage });
				if (cancelled) return;
				cursor.current.afterSeq = read.nextAfterSeq;
				setLog((current) => ({
					id: installationId ?? null,
					lines: mergeLogLines(current.id === installationId ? current.lines : [], read.lines),
					error: null,
				}));
			} catch (error) {
				if (cancelled) return;
				setLog((current) => ({
					id: installationId ?? null,
					lines: current.id === installationId ? current.lines : [],
					error,
				}));
				// A failed read stops following until the tenant retries.
				return;
			}
			if (!cancelled && active) {
				timer = setTimeout(() => {
					void tick();
				}, DETAIL_POLL_INTERVAL_MS);
			}
		}

		void tick();
		return () => {
			cancelled = true;
			if (timer) clearTimeout(timer);
		};
		// `attempt` re-runs the follower after a retry.
	}, [api, installationId, active, attempt]);

	const current = log.id === installationId;
	return {
		lines: current ? log.lines : [],
		error: current ? log.error : null,
		retry: () => setAttempt((value) => value + 1),
	};
}

export interface CompanionDetailViewProps {
	agentId: string;
	companionKey: string;
	/** The catalog's display name — the page title and every interpolated name. */
	name: string;
	/** The section tab strip, with no tab active. Router-bound, so the container supplies it. */
	tabs?: ReactNode;
	/** Undefined while loading; null when this agent has no live installation of the companion. */
	installation: CompanionInstallation | null | undefined;
	loading?: boolean;
	error?: unknown;
	onRetry?: () => void;
	logLines?: readonly CompanionLogLine[];
	logError?: unknown;
	onRetryLog?: () => void;
	/** Opens S5 from the header row menu. */
	onUninstall?: () => void;
	/** CK-D: mint a set-password link and open it. */
	onSetPassword?: () => void;
	loginLinkPending?: boolean;
	loginLinkError?: string | null;
	/** "Back up and update" — rejects with the sentence to show in the still-open dialog. */
	onAcceptUpdate?: (digest: string) => Promise<unknown>;
	/** "Not now" — defers this one digest. */
	onDeferUpdate?: (digest: string) => void;
	deferPending?: boolean;
	deferError?: string | null;
}

export function CompanionDetailView({
	agentId,
	companionKey,
	name,
	tabs,
	installation,
	loading = false,
	error,
	onRetry,
	logLines = [],
	logError,
	onRetryLog,
	onUninstall,
	onSetPassword,
	loginLinkPending = false,
	loginLinkError = null,
	onAcceptUpdate,
	onDeferUpdate,
	deferPending = false,
	deferError = null,
}: CompanionDetailViewProps) {
	const breadcrumb = <CompanionBreadcrumb agentId={agentId} name={name} />;

	if (error !== undefined && error !== null) {
		return (
			<div className={SURFACE_CLASS} data-testid="companion-detail">
				{breadcrumb}
				<ApiErrorPanel error={error} onRetry={onRetry} normalizer={OVERVIEW_ERROR_NORMALIZER} />
			</div>
		);
	}

	if (loading || installation === undefined) {
		return <CompanionDetailSkeleton breadcrumb={breadcrumb} />;
	}

	if (installation === null) {
		const copy = notInstalledCopy(name);
		return (
			<div className={SURFACE_CLASS} data-testid="companion-detail">
				{breadcrumb}
				<EmptyState
					variant="page"
					title={copy.title}
					description={copy.body}
					action={
						<Button
							variant="outline"
							nativeButton={false}
							render={<Link to="/agents/$id/companions/installed" params={{ id: agentId }} />}
						>
							{copy.action}
						</Button>
					}
				/>
			</div>
		);
	}

	const state = jobChip(installation);
	const reachability = reachabilityChip(installation.reachability.state);
	const update = updateChip(installation.update.state);
	const serving = isServing(installation);

	return (
		<div
			className={SURFACE_CLASS}
			data-testid="companion-detail"
			data-companion={companionKey}
			data-state={installation.status}
			data-operation={installation.operation}
		>
			<div className="space-y-4">
				{breadcrumb}
				<PageHeader
					title={name}
					icon={<CompanionIdentityTile seed={companionKey} />}
					status={
						<div className="flex flex-wrap items-center gap-2" data-slot="companion-status-chips">
							<StatusBadge
								status={state.tone}
								withDot
								title={installation.status}
								data-state={installation.status}
							>
								{state.label}
							</StatusBadge>
							<StatusBadge
								status={reachability.tone}
								title={installation.reachability.state}
								data-reachability={installation.reachability.state}
							>
								{reachability.label}
							</StatusBadge>
							{update ? (
								<StatusBadge
									status={update.tone}
									title={installation.update.state}
									data-update={installation.update.state}
								>
									{update.label}
								</StatusBadge>
							) : null}
						</div>
					}
					actions={
						<>
							{serving ? (
								<Button
									variant="ghost"
									nativeButton={false}
									render={
										<a
											href={`https://${installation.hostname}`}
											target="_blank"
											rel="noopener noreferrer"
										/>
									}
									data-slot="open-companion"
								>
									<ExternalLink aria-hidden />
									{openCompanionLabel(name)}
								</Button>
							) : null}
							<DropdownMenu>
								<DropdownMenuTrigger
									render={
										<Button
											type="button"
											variant="ghost"
											size="icon-sm"
											aria-label={DETAIL_COPY.actionsMenuLabel}
											data-slot="companion-actions-trigger"
										/>
									}
								>
									<MoreHorizontal aria-hidden />
								</DropdownMenuTrigger>
								<DropdownMenuContent align="end">
									<DropdownMenuItem
										variant="destructive"
										className="text-destructive"
										onClick={() => onUninstall?.()}
									>
										{DETAIL_COPY.uninstallMenuItem}
									</DropdownMenuItem>
								</DropdownMenuContent>
							</DropdownMenu>
						</>
					}
				/>
				{tabs}
			</div>

			<InstallSection
				installation={installation}
				logLines={logLines}
				logError={logError}
				onRetryLog={onRetryLog}
			/>
			<Separator />
			<AccessSection
				installation={installation}
				name={name}
				onSetPassword={onSetPassword}
				loginLinkPending={loginLinkPending}
				loginLinkError={loginLinkError}
			/>
			<Separator />
			<UpdatesSection
				installation={installation}
				name={name}
				onAcceptUpdate={onAcceptUpdate}
				onDeferUpdate={onDeferUpdate}
				deferPending={deferPending}
				deferError={deferError}
			/>
			<Separator />
			<DataSection installation={installation} name={name} companionKey={companionKey} />
		</div>
	);
}

/** One label/value row of a section's definition list. */
function FactRow({ label, slot, children }: { label: string; slot: string; children: ReactNode }) {
	return (
		<div className="grid gap-1 sm:grid-cols-[10rem_minmax(0,1fr)] sm:gap-4" data-slot={slot}>
			<dt className="text-xs text-muted-foreground sm:pt-0.5">{label}</dt>
			<dd className="min-w-0 space-y-1 text-sm">{children}</dd>
		</div>
	);
}

/** Copy-to-clipboard icon button (the billing `CopyButton`'s affordance, outside `hosted/`). */
function CopyValueButton({ value, label }: { value: string; label: string }) {
	const { copied, copy } = useCopyToClipboard();
	return (
		<Button
			type="button"
			size="icon-sm"
			variant="ghost"
			onClick={() => {
				void copy(value);
			}}
			aria-label={label}
			data-copied={copied}
		>
			{copied ? <Check className="text-success" aria-hidden /> : <Copy aria-hidden />}
		</Button>
	);
}

/**
 * Access — APP-02's hostname and TLS note, APP-03's credential reference with
 * CK-D's sign-in action, and the isolation tier as a read-only fact. No
 * credential value and no reveal control exist anywhere on this page.
 */
function AccessSection({
	installation,
	name,
	onSetPassword,
	loginLinkPending,
	loginLinkError,
}: {
	installation: CompanionInstallation;
	name: string;
	onSetPassword?: () => void;
	loginLinkPending: boolean;
	loginLinkError: string | null;
}) {
	const serving = isServing(installation);
	const tierLabel = ISOLATION_TIER_LABELS[installation.isolation_tier];
	return (
		<section className="space-y-4" data-slot="companion-access" aria-labelledby="companion-access">
			<h2 id="companion-access" className="text-sm font-semibold">
				{DETAIL_COPY.sectionAccess}
			</h2>
			<dl className="space-y-4">
				<FactRow label={ACCESS_COPY.hostnameLabel} slot="access-hostname">
					{serving ? (
						<div className="flex min-w-0 items-center gap-2">
							<TruncatedText className="min-w-0 font-mono text-xs" data-slot="companion-hostname">
								{installation.hostname}
							</TruncatedText>
							<CopyValueButton
								value={installation.hostname}
								label={ACCESS_COPY.copyHostnameLabel}
							/>
						</div>
					) : (
						<p className="text-muted-foreground">
							{reachabilityChip(installation.reachability.state).label}
						</p>
					)}
					<p className="text-xs text-muted-foreground">{tlsNote(name)}</p>
					{showNotAnswering(installation) ? (
						<p className="text-sm" data-slot="access-not-answering">
							{notAnsweringCopy(name)}
						</p>
					) : null}
				</FactRow>
				<FactRow label={ACCESS_COPY.credentialLabel} slot="access-credential">
					<p data-slot="credential-reference">{ACCESS_COPY.credentialValue}</p>
					<div className="flex min-w-0 flex-wrap items-center gap-2" data-slot="access-sign-in">
						<TruncatedText className="min-w-0 text-muted-foreground">
							{signInAsLabel(installation.owner_email)}
						</TruncatedText>
						<Button
							type="button"
							variant="ghost"
							size="sm"
							onClick={onSetPassword}
							disabled={!serving || loginLinkPending}
							aria-busy={loginLinkPending}
							data-slot="set-password"
						>
							{loginLinkPending ? <Spinner /> : null}
							{setPasswordLabel(name)}
						</Button>
					</div>
					{loginLinkError ? (
						<p
							role="alert"
							className="text-sm text-destructive-muted-foreground"
							data-slot="set-password-error"
						>
							{loginLinkError}
						</p>
					) : null}
				</FactRow>
				<FactRow label={ACCESS_COPY.tierLabel} slot="access-tier">
					<p title={installation.isolation_tier}>{tierRowValue(tierLabel)}</p>
				</FactRow>
			</dl>
		</section>
	);
}

/**
 * Updates — pinned by default (D-28). A newer release is an offer with two
 * answers; "Update now" is the only accent button on the page and goes
 * through `ConfirmAction` so the backup it takes is named before it happens.
 */
function UpdatesSection({
	installation,
	name,
	onAcceptUpdate,
	onDeferUpdate,
	deferPending,
	deferError,
}: {
	installation: CompanionInstallation;
	name: string;
	onAcceptUpdate?: (digest: string) => Promise<unknown>;
	onDeferUpdate?: (digest: string) => void;
	deferPending: boolean;
	deferError: string | null;
}) {
	const card = updateCard(installation, name);
	const offered =
		card.kind === "available"
			? card.offeredDigest
			: card.kind === "aborted_backup_failed"
				? card.offeredDigest
				: null;
	return (
		<section
			className="space-y-4"
			data-slot="companion-updates"
			data-update={card.kind}
			aria-labelledby="companion-updates"
		>
			<h2 id="companion-updates" className="text-sm font-semibold">
				{DETAIL_COPY.sectionUpdates}
			</h2>
			{card.kind === "in_progress" ? (
				<p className="text-sm" data-slot="update-note" aria-live="polite">
					{card.note}
				</p>
			) : card.kind === "failed" ? (
				<p
					role="alert"
					className="text-sm text-destructive-muted-foreground"
					data-slot="update-body"
				>
					{card.body}
				</p>
			) : (
				<p className="text-sm" data-slot="update-body">
					{card.body}
				</p>
			)}
			{card.kind === "available" && card.offeredVersion ? (
				<p className="font-mono text-xs text-muted-foreground" data-slot="update-offered-version">
					{card.offeredVersion}
				</p>
			) : null}
			{offered ? (
				<div className="space-y-2">
					<div className="flex flex-wrap items-center gap-2" data-slot="update-actions">
						<ConfirmAction
							title={updateConfirmTitle(name)}
							description={updateConfirmDescription(name)}
							confirmLabel={UPDATES_COPY.confirmLabel}
							onConfirm={() => onAcceptUpdate?.(offered)}
						>
							<Button type="button" data-slot="update-now">
								{UPDATES_COPY.updateNow}
							</Button>
						</ConfirmAction>
						<Button
							type="button"
							variant="ghost"
							onClick={() => onDeferUpdate?.(offered)}
							disabled={deferPending}
							aria-busy={deferPending}
							data-slot="update-not-now"
						>
							{deferPending ? <Spinner /> : null}
							{UPDATES_COPY.notNow}
						</Button>
					</div>
					{deferError ? (
						<p
							role="alert"
							className="text-sm text-destructive-muted-foreground"
							data-slot="update-defer-error"
						>
							{deferError}
						</p>
					) : null}
				</div>
			) : null}
		</section>
	);
}

/**
 * Data — the last backup, where media lives, the way out (Settings → Data),
 * and what exactly is running. Uninstall is NOT here: a destructive action at
 * the bottom of a page about data is a mis-click waiting to happen.
 */
function DataSection({
	installation,
	name,
	companionKey,
}: {
	installation: CompanionInstallation;
	name: string;
	companionKey: string;
}) {
	const lastBackup = installation.update.last_backup_at;
	const source = sourcePointer(companionKey, name, installation.version);
	return (
		<section className="space-y-4" data-slot="companion-data" aria-labelledby="companion-data">
			<h2 id="companion-data" className="text-sm font-semibold">
				{DETAIL_COPY.sectionData}
			</h2>
			<dl className="space-y-4">
				<FactRow label={DATA_COPY.lastBackupLabel} slot="data-last-backup">
					{lastBackup ? (
						<TimeTooltip value={lastBackup}>
							<span className="tabular-nums">{relativeTime(lastBackup)}</span>
						</TimeTooltip>
					) : (
						<p className="text-muted-foreground">{DATA_COPY.noBackupYet}</p>
					)}
					<p className="text-xs text-muted-foreground">{DATA_COPY.backupHelper}</p>
				</FactRow>
				<FactRow label={DATA_COPY.versionLabel} slot="data-version">
					<div className="flex min-w-0 flex-wrap items-center gap-2">
						<span className="font-mono text-xs" data-slot="version-tag">
							{installation.version}
						</span>
						<span
							className="font-mono text-xs text-muted-foreground"
							title={installation.pinned_digest}
							data-slot="version-digest"
						>
							{digestPrefix(installation.pinned_digest)}
						</span>
						<CopyValueButton value={installation.pinned_digest} label={DATA_COPY.copyDigestLabel} />
					</div>
					{source ? (
						<a
							href={source.href}
							target="_blank"
							rel="noopener noreferrer"
							className="inline-flex items-center gap-1 text-xs text-muted-foreground underline underline-offset-4 hover:text-foreground"
							data-slot="version-source"
						>
							{source.label}
							<ExternalLink className="size-3" aria-hidden />
						</a>
					) : null}
				</FactRow>
			</dl>
			<p className="text-sm text-muted-foreground" data-slot="data-media-note">
				{mediaNote(name)}
			</p>
			<Link
				to="."
				search={(current: Record<string, unknown>) => ({ ...current, settings: "data" })}
				className="inline-flex text-sm underline underline-offset-4 hover:text-foreground"
				data-slot="data-export-link"
			>
				{DATA_COPY.exportLink}
			</Link>
		</section>
	);
}

function CompanionBreadcrumb({ agentId, name }: { agentId: string; name: string }) {
	return (
		<Breadcrumb data-slot="companion-breadcrumb">
			<BreadcrumbList>
				<BreadcrumbItem>
					<BreadcrumbLink
						render={<Link to="/agents/$id/companions/installed" params={{ id: agentId }} />}
					>
						{DETAIL_COPY.breadcrumbRoot}
					</BreadcrumbLink>
				</BreadcrumbItem>
				<BreadcrumbSeparator />
				<BreadcrumbItem>
					<BreadcrumbPage>{name}</BreadcrumbPage>
				</BreadcrumbItem>
			</BreadcrumbList>
		</Breadcrumb>
	);
}

/** Loading: the header skeleton plus section-shaped blocks — never a page spinner. */
function CompanionDetailSkeleton({ breadcrumb }: { breadcrumb: ReactNode }) {
	return (
		<div className={SURFACE_CLASS} data-testid="companion-detail" aria-busy="true">
			<div className="space-y-4">
				{breadcrumb}
				<PageHeaderSkeleton icon actions description={false} />
			</div>
			<div className="space-y-6" data-slot="companion-detail-loading">
				{[
					{ key: "install", body: "h-40" },
					{ key: "access", body: "h-24" },
					{ key: "updates", body: "h-20" },
					{ key: "data", body: "h-20" },
				].map((section, index) => (
					<div key={section.key} className="space-y-6">
						{index > 0 ? <Separator /> : null}
						<section className="space-y-3" data-slot="section-skeleton">
							<Skeleton className="h-4 w-24" />
							<Skeleton className={cn("w-full rounded-lg", section.body)} />
						</section>
					</div>
				))}
			</div>
		</div>
	);
}

function InstallSection({
	installation,
	logLines,
	logError,
	onRetryLog,
}: {
	installation: CompanionInstallation;
	logLines: readonly CompanionLogLine[];
	logError?: unknown;
	onRetryLog?: () => void;
}) {
	const collapsed = logCollapsed(installation);
	const active = isJobActive(installation);
	const failure = installFailure(installation);
	const log =
		logError !== undefined && logError !== null ? (
			<ApiErrorPanel error={logError} onRetry={onRetryLog} normalizer={OVERVIEW_ERROR_NORMALIZER} />
		) : (
			<InstallLogWell lines={logLines} />
		);

	return (
		<section
			className="space-y-4"
			data-slot="companion-install"
			aria-labelledby="companion-install"
		>
			<h2 id="companion-install" className="text-sm font-semibold">
				{DETAIL_COPY.sectionInstall}
			</h2>
			<JobStepReadout installation={installation} />
			{collapsed ? (
				<InstallLogDisclosure summary={completeSummary(installation)}>{log}</InstallLogDisclosure>
			) : (
				<div className="space-y-2" data-slot="install-log">
					<div className="flex flex-wrap items-baseline justify-between gap-2">
						<h3 className="text-sm font-semibold">{DETAIL_COPY.installLogHeading}</h3>
						{active ? (
							<p className="text-xs text-muted-foreground" data-slot="install-log-live-note">
								{DETAIL_COPY.installLogLiveNote}
							</p>
						) : null}
					</div>
					{log}
					{failure ? (
						<p
							role="alert"
							className="text-sm text-destructive-muted-foreground"
							data-slot="install-failure"
						>
							{failure}
						</p>
					) : null}
				</div>
			)}
		</section>
	);
}

/** The four job states in a row, the current one marked by its chip. */
function JobStepReadout({ installation }: { installation: CompanionInstallation }) {
	const steps = jobSteps(installation);
	return (
		<ol
			className="flex flex-wrap items-center gap-2 text-xs"
			data-slot="install-steps"
			aria-label={DETAIL_COPY.stepsLabel}
		>
			{steps.map((step, index) => (
				<li
					key={step.status}
					className="flex items-center gap-2"
					data-state={step.status}
					aria-current={step.current ? "step" : undefined}
				>
					{index > 0 ? <ChevronRight className="size-3 text-muted-foreground" aria-hidden /> : null}
					{step.current ? (
						<StatusBadge status={step.tone} withDot title={step.status}>
							{step.label}
						</StatusBadge>
					) : (
						<span className="text-muted-foreground" title={step.status}>
							{step.label}
						</span>
					)}
				</li>
			))}
		</ol>
	);
}

/**
 * A finished job's log, behind one disclosure. Collapsed by default so a
 * complete install does not own the top of the page.
 */
function InstallLogDisclosure({
	summary,
	children,
}: {
	summary: string | null;
	children: ReactNode;
}) {
	const [open, setOpen] = useState(false);
	return (
		<div className="space-y-2" data-slot="install-log-disclosure">
			<div className="flex flex-wrap items-center gap-2">
				{summary ? (
					<p className="text-sm text-muted-foreground" data-slot="install-summary">
						{summary}
					</p>
				) : null}
				<Button
					type="button"
					variant="ghost"
					size="sm"
					aria-expanded={open}
					onClick={() => setOpen((value) => !value)}
				>
					{open ? DETAIL_COPY.hideInstallLog : DETAIL_COPY.showInstallLog}
				</Button>
			</div>
			{open ? children : null}
		</div>
	);
}

/**
 * The install log. Untrusted text: each line is a React text node — never
 * markdown, never HTML, never auto-linkified. The well follows the newest line
 * only while the reader has not scrolled up.
 */
function InstallLogWell({ lines }: { lines: readonly CompanionLogLine[] }) {
	const ref = useRef<HTMLDivElement>(null);
	const following = useRef(true);

	useIsomorphicLayoutEffect(() => {
		const node = ref.current;
		if (node && following.current) node.scrollTop = node.scrollHeight;
	}, [lines]);

	return (
		<div
			ref={ref}
			role="log"
			aria-label={DETAIL_COPY.installLogHeading}
			// A scroll region must be reachable from the keyboard.
			// biome-ignore lint/a11y/noNoninteractiveTabindex: scrollable log region
			tabIndex={0}
			className={LOG_WELL_CLASS}
			data-slot="install-log-well"
			data-lines={lines.length}
			onScroll={(event) => {
				following.current = isScrolledToBottom(event.currentTarget);
			}}
		>
			{lines.length === 0 ? (
				<EmptyState
					variant="inset"
					title={LOG_EMPTY_COPY.title}
					description={LOG_EMPTY_COPY.body}
					className="border-none bg-transparent px-0 py-6 font-sans break-normal whitespace-normal md:p-0"
				/>
			) : (
				lines.map((line) => (
					<div
						key={line.seq}
						data-slot="install-log-line"
						data-stream={line.stream}
						data-seq={line.seq}
					>
						<span className="mr-2 text-2xs tracking-wider text-muted-foreground select-none">
							{STREAM_MARKERS[line.stream]}
						</span>
						{line.line}
					</div>
				))
			)}
		</div>
	);
}
