import type { components, paths } from "@clawdi/shared/api";
import type { Client } from "openapi-fetch";
import type { StatusTone } from "@/components/ui/status-badge";
import { unwrap } from "@/lib/api";
import { ApiError, parseApiDetail } from "@/lib/api-errors";
import { relativeTime } from "@/lib/utils";
import {
	type ChipModel,
	type CompanionCatalogItem,
	type CompanionInstallation,
	type InstallationOperation,
	type InstallationStatus,
	installationStateChip,
} from "./companions-overview.logic";

/**
 * S3 — the Companion detail page — as data rather than as markup.
 *
 * Every customer-facing S3 string lives here, verbatim from `06-UI-SPEC.md`
 * §Copywriting Contract (plus the recorded CK-D deltas), so
 * `companion-detail.logic.test.ts` can hold each one against its contract row.
 * Every rule that decides what the page shows — which chip, which step is
 * current, whether the page is still polling, where the log cursor stands — is
 * a pure function here, testable without rendering the page.
 */

type Schemas = components["schemas"];

export type CompanionLogLine = Schemas["CompanionInstallLogItemResponse"];
export type CompanionLogPage = Schemas["CompanionInstallLogResponse"];
export type LogStream = CompanionLogLine["stream"];

/** How often a queued or running job is re-read: the installation and its log alike. */
export const DETAIL_POLL_INTERVAL_MS = 2_000;

/** One log request's page size; the route allows up to 500. */
export const LOG_PAGE_LIMIT = 500;

export const DETAIL_COPY = {
	/** The breadcrumb's root, back to S1. */
	breadcrumbRoot: "Companions",
	sectionInstall: "Install",
	sectionAccess: "Access",
	sectionUpdates: "Updates",
	sectionData: "Data",
	installLogHeading: "Install log",
	installLogLiveNote: "Streaming — this updates as the install runs.",
	showInstallLog: "Show install log",
	hideInstallLog: "Hide install log",
	/** The header row menu's trigger. Accessible name only — the trigger is an icon. */
	actionsMenuLabel: "Companion actions",
	uninstallMenuItem: "Uninstall",
	/** The steps read-out's accessible name. */
	stepsLabel: "Job progress",
} as const;

/** §Empty states — S3, the install log has no lines yet (`EmptyState variant="inset"`). */
export const LOG_EMPTY_COPY = {
	title: "Waiting for the first log line",
	body: "The install has been queued. Output appears here as soon as it starts.",
} as const;

/** §Error states — "Install job failed (terminal, rendered on S3)". */
export const INSTALL_FAILED_COPY =
	"The install failed. Nothing is running and you have not been charged. The log above shows where it stopped.";

/**
 * The page when this agent has no live installation of the companion — a
 * stale link, or one opened after an uninstall finished. Not in the contract:
 * authored under the house rules (sentence case, no exclamation, says what to
 * do next).
 */
export function notInstalledCopy(name: string) {
	return {
		title: `${name} is not installed on this agent`,
		body: "Install it from Companions to manage it here.",
		action: "Go to Companions",
	} as const;
}

/** S3 open CTA — "Open Postiz". Opens the hostname in a new tab. */
export function openCompanionLabel(name: string): string {
	return `Open ${name}`;
}

/** The stream marker prefixed to each log line (ALL-CAPS meta label). */
export const STREAM_MARKERS: Readonly<Record<LogStream, string>> = {
	stdout: "STDOUT",
	stderr: "STDERR",
};

// ---------------------------------------------------------------------------
// Resolving the page's installation
// ---------------------------------------------------------------------------

/** An uninstall that finished is not a companion on this agent. */
function isOnAgent(installation: CompanionInstallation): boolean {
	return !(installation.operation === "uninstall" && installation.status === "complete");
}

/**
 * The agent's live installation of `companionKey`, or null when there is none.
 * The newest wins should the list ever carry more than one.
 */
export function resolveInstallation(
	items: readonly CompanionInstallation[] | undefined,
	companionKey: string,
): CompanionInstallation | null {
	if (!items) return null;
	const matches = items.filter(
		(installation) => installation.companion_key === companionKey && isOnAgent(installation),
	);
	matches.sort((a, b) => Date.parse(b.created_at ?? "") - Date.parse(a.created_at ?? ""));
	return matches[0] ?? null;
}

/** The catalog's display name for the companion; the key only when the catalog lacks it. */
export function companionName(
	catalog: readonly Pick<CompanionCatalogItem, "companion_key" | "display_name">[] | undefined,
	companionKey: string,
): string {
	return catalog?.find((item) => item.companion_key === companionKey)?.display_name ?? companionKey;
}

// ---------------------------------------------------------------------------
// The job state machine
// ---------------------------------------------------------------------------

/** D-26's four job states, in the order the read-out lists them. */
export const JOB_STATES: readonly InstallationStatus[] = [
	"queued",
	"running",
	"complete",
	"failed",
];

/** Is the current job still moving? Only then does the page poll. */
export function isJobActive(
	installation: Pick<CompanionInstallation, "status"> | null | undefined,
): boolean {
	return installation?.status === "queued" || installation?.status === "running";
}

/**
 * The installation query's `refetchInterval`: every two seconds while the job
 * is queued or running, off at `complete` and `failed` — no indefinite poll on
 * a terminal state, and none when there is nothing to follow.
 */
export function installationRefetchInterval(
	installation: Pick<CompanionInstallation, "status"> | null | undefined,
): number | false {
	return isJobActive(installation) ? DETAIL_POLL_INTERVAL_MS : false;
}

export interface JobStepModel {
	status: InstallationStatus;
	label: string;
	tone: StatusTone;
	current: boolean;
}

/**
 * The horizontal read-out of the four job states, labelled for the current
 * operation (Queued / Installing / Running / Install failed for an install),
 * with exactly the current state marked. The enum never becomes visible text.
 */
export function jobSteps(
	installation: Pick<CompanionInstallation, "operation" | "status">,
): JobStepModel[] {
	return JOB_STATES.map((status) => {
		const chip = installationStateChip({ operation: installation.operation, status });
		return {
			status,
			label: chip.label,
			tone: chip.tone,
			current: status === installation.status,
		};
	});
}

/** The header's first chip: the job state, labelled for its operation. */
export function jobChip(
	installation: Pick<CompanionInstallation, "operation" | "status">,
): ChipModel {
	return installationStateChip(installation);
}

/**
 * A finished job does not own the top of the page: its log collapses behind
 * "Show install log" once the job is `complete`.
 */
export function logCollapsed(installation: Pick<CompanionInstallation, "status">): boolean {
	return installation.status === "complete";
}

const COMPLETE_SUMMARY_VERB: Readonly<Record<InstallationOperation, string>> = {
	install: "Installed",
	update: "Updated",
	export: "Installed",
	uninstall: "Removed",
};

/**
 * The collapsed Install section's one-line summary — "Installed {relative}",
 * the same phrase S1's meta row uses — or null when no time is known.
 */
export function completeSummary(
	installation: Pick<CompanionInstallation, "operation" | "completed_at" | "updated_at">,
): string | null {
	const at = installation.completed_at ?? installation.updated_at;
	if (!at) return null;
	return `${COMPLETE_SUMMARY_VERB[installation.operation]} ${relativeTime(at)}`;
}

/** The terminal failure sentence the Install section owns (update failures live in Updates). */
export function installFailure(
	installation: Pick<CompanionInstallation, "operation" | "status">,
): string | null {
	return installation.operation === "install" && installation.status === "failed"
		? INSTALL_FAILED_COPY
		: null;
}

// ---------------------------------------------------------------------------
// The install log
// ---------------------------------------------------------------------------

/**
 * Append a page of log lines to what is already on screen, by `seq`: a line
 * already held is not repeated, and the result stays in `seq` order however
 * the pages arrived. Each line's text is kept exactly as served.
 */
export function mergeLogLines(
	existing: readonly CompanionLogLine[],
	incoming: readonly CompanionLogLine[],
): CompanionLogLine[] {
	if (incoming.length === 0) return existing as CompanionLogLine[];
	const bySeq = new Map(existing.map((line) => [line.seq, line]));
	for (const line of incoming) {
		if (!bySeq.has(line.seq)) bySeq.set(line.seq, line);
	}
	return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

/**
 * Read every log line after `afterSeq`, page by page, until a short page says
 * the log is drained. Returns the lines read and the cursor to resume from.
 */
export async function readLogAfter({
	afterSeq,
	fetchPage,
	limit = LOG_PAGE_LIMIT,
	maxPages = 20,
}: {
	afterSeq: number;
	fetchPage: (afterSeq: number, limit: number) => Promise<CompanionLogPage>;
	limit?: number;
	maxPages?: number;
}): Promise<{ lines: CompanionLogLine[]; nextAfterSeq: number }> {
	let cursor = afterSeq;
	let lines: CompanionLogLine[] = [];
	for (let page = 0; page < maxPages; page += 1) {
		const result = await fetchPage(cursor, limit);
		lines = mergeLogLines(lines, result.items);
		const advanced = result.next_after_seq > cursor;
		cursor = Math.max(cursor, result.next_after_seq);
		if (result.items.length < limit || !advanced) break;
	}
	return { lines, nextAfterSeq: cursor };
}

/** Within this many pixels of the bottom, the well still counts as "at the bottom". */
export const LOG_STICK_THRESHOLD_PX = 16;

/**
 * Has the reader scrolled up? The well follows the newest line ONLY while the
 * reader is at the bottom; once they scroll up to read, new lines stop
 * yanking them back down.
 */
export function isScrolledToBottom({
	scrollTop,
	scrollHeight,
	clientHeight,
}: {
	scrollTop: number;
	scrollHeight: number;
	clientHeight: number;
}): boolean {
	return scrollHeight - scrollTop - clientHeight <= LOG_STICK_THRESHOLD_PX;
}

// ---------------------------------------------------------------------------
// Access (APP-02, APP-03, CK-D)
// ---------------------------------------------------------------------------

export const ACCESS_COPY = {
	hostnameLabel: "Hostname",
	credentialLabel: "Admin credential",
	/**
	 * CK-D (06-09): platform-generated credentials live in the iforge-owned
	 * companion store, not as tenant vault items, so the row says where the
	 * credential is and links nowhere. There is no value and no reveal control.
	 */
	credentialValue: "Stored in your vault",
	tierLabel: "Isolation",
	copyHostnameLabel: "Copy hostname",
} as const;

/** S3 TLS note. */
export function tlsNote(name: string): string {
	return `Certificate is issued and renewed at the edge. ${name} never handles it.`;
}

/** S3 tier row — "{tier}. Chosen at install and fixed for this companion." */
export function tierRowValue(tierLabel: string): string {
	return `${tierLabel}. Chosen at install and fixed for this companion.`;
}

/** CK-D: the account the tenant's own password belongs to. */
export function signInAsLabel(email: string): string {
	return `Sign in as ${email}`;
}

/** CK-D: the ghost action that mints a set-password link for that account. */
export function setPasswordLabel(name: string): string {
	return `Set your ${name} password`;
}

/**
 * The login-link route refused or failed. Not in the contract (CK-D added the
 * action after approval); authored under the house rules: inline, says what
 * did not change.
 */
export function loginLinkFailedCopy(name: string): string {
	return `The link was not created. Your ${name} password is unchanged.`;
}

/** §Error states — "Hostname or certificate not ready after the install completed". */
export function notAnsweringCopy(name: string): string {
	return `${name} is running but its hostname is not answering yet. This usually settles within a few minutes.`;
}

/** Serving: the install completed and nothing is tearing the companion down. */
export function isServing(
	installation: Pick<CompanionInstallation, "operation" | "status" | "hostname">,
): boolean {
	const reached = installation.operation !== "install" || installation.status === "complete";
	return reached && installation.operation !== "uninstall" && Boolean(installation.hostname);
}

/**
 * S3 `partial`: running, but the hostname does not answer. The reachability
 * chip and the hostname row both stay on screen and this sentence says why
 * they disagree.
 */
export function showNotAnswering(
	installation: Pick<CompanionInstallation, "operation" | "status" | "hostname" | "reachability">,
): boolean {
	return isServing(installation) && installation.reachability.state === "not_answering";
}

/** A minted link is only ever opened when it is an absolute https URL. */
export function isHttpsUrl(value: string): boolean {
	try {
		return new URL(value).protocol === "https:";
	} catch {
		return false;
	}
}

// ---------------------------------------------------------------------------
// Updates (APP-06, OPS-02)
// ---------------------------------------------------------------------------

export type UpdateState = CompanionInstallation["update"]["state"];

export const UPDATES_COPY = {
	deferredBody:
		"You deferred this version. It stays available, and the next release will be offered the same way.",
	updateNow: "Update now",
	notNow: "Not now",
	inProgressBackup: "Taking a backup before updating.",
	confirmLabel: "Back up and update",
	startedToast: "Update started",
	deferredToast: "Update deferred",
	/** §Error states — "Update job failed after the backup succeeded". */
	failedAfterBackup:
		"The update failed. A backup was taken before it started — contact support with this companion's name before retrying.",
	/** Not in the contract: a refused "Not now". Authored — inline beneath the control. */
	deferRejected: "The version was not deferred. It is still on offer.",
} as const;

export function upToDateBody(name: string): string {
	return `${name} is pinned to the version you installed. Nothing updates on its own.`;
}

export function updateAvailableBody(name: string): string {
	return `A newer version of ${name} is available. Nothing changes until you choose.`;
}

export function updatingNote(name: string): string {
	return `Updating ${name}.`;
}

/** §Error states — "Update refused because the pre-update backup failed". Reads as protection. */
export function backupFailedCopy(name: string): string {
	return `The update was cancelled because the backup failed. ${name} is still running the version you had, and your data is untouched.`;
}

/** §Destructive confirmations — "Update now". */
export function updateConfirmTitle(name: string): string {
	return `Update ${name}`;
}

export function updateConfirmDescription(name: string): string {
	return `A backup is taken first. If the backup fails, the update is cancelled and nothing changes. ${name} is briefly unavailable while it restarts.`;
}

/** Not in the contract: an accept the API refused. Authored — inline in the still-open dialog. */
export function updateRejectedCopy(name: string): string {
	return `The update was not started. ${name} is still running the version you had.`;
}

const UPDATE_CHIPS: Partial<Record<UpdateState, ChipModel>> = {
	up_to_date: { tone: "neutral", label: "Up to date" },
	available: { tone: "info", label: "Update available" },
	deferred: { tone: "neutral", label: "Update deferred" },
	aborted_backup_failed: { tone: "destructive", label: "Update aborted — backup failed" },
};

/**
 * The header's third chip. In-progress and failed updates carry no update
 * chip: the job chip already reads "Updating" / "Update failed", and a second
 * chip saying the same would be noise.
 */
export function updateChip(state: UpdateState): ChipModel | null {
	return UPDATE_CHIPS[state] ?? null;
}

export type UpdateCardModel =
	| { kind: "up_to_date"; body: string }
	| { kind: "available"; body: string; offeredVersion: string | null; offeredDigest: string }
	| { kind: "deferred"; body: string }
	| { kind: "in_progress"; note: string }
	| { kind: "aborted_backup_failed"; body: string; offeredDigest: string | null }
	| { kind: "failed"; body: string };

/**
 * What the Updates card says. An installation that was never offered a
 * release reads as up to date — never as an empty state, which would imply
 * something is missing.
 */
export function updateCard(
	installation: Pick<CompanionInstallation, "operation" | "status" | "step" | "update">,
	name: string,
): UpdateCardModel {
	const { update } = installation;
	const updating = installation.operation === "update" && isJobActive(installation);
	if (updating || update.state === "in_progress") {
		// The runtime's first update step is the backup; a queued update has not passed it.
		const backingUp =
			installation.status === "queued" ||
			installation.step === null ||
			installation.step === "backup";
		return {
			kind: "in_progress",
			note: backingUp ? UPDATES_COPY.inProgressBackup : updatingNote(name),
		};
	}
	switch (update.state) {
		case "available":
			if (update.offered_digest) {
				return {
					kind: "available",
					body: updateAvailableBody(name),
					offeredVersion: update.offered_version,
					offeredDigest: update.offered_digest,
				};
			}
			return { kind: "up_to_date", body: upToDateBody(name) };
		case "deferred":
			return { kind: "deferred", body: UPDATES_COPY.deferredBody };
		case "aborted_backup_failed":
			return {
				kind: "aborted_backup_failed",
				body: backupFailedCopy(name),
				offeredDigest: update.offered_digest,
			};
		case "failed":
			return { kind: "failed", body: UPDATES_COPY.failedAfterBackup };
		default:
			return { kind: "up_to_date", body: upToDateBody(name) };
	}
}

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------

export const DATA_COPY = {
	lastBackupLabel: "Last backup",
	backupHelper: "Taken automatically before every update.",
	/** Not in the contract: the backup row before any backup exists. Authored. */
	noBackupYet: "No backup yet",
	exportLink: "Export data",
	versionLabel: "Version",
	copyDigestLabel: "Copy image digest",
} as const;

/**
 * The media-storage note. The contract names the note but carries no copy;
 * authored from 06-19's export, which carries every media object.
 */
export function mediaNote(name: string): string {
	return `Uploaded media is kept in ${name}'s own storage and is included when you export your data.`;
}

/** Upstream source for the companion's unmodified AGPL image, by catalog key (D-03 stance). */
export const COMPANION_SOURCE_REPOSITORIES: Readonly<Record<string, string>> = {
	postiz: "https://github.com/gitroomhq/postiz-app",
};

/** "Postiz v2.24.0 — source" and the upstream tag it points at, or null when unknown. */
export function sourcePointer(
	companionKey: string,
	name: string,
	version: string,
): { label: string; href: string } | null {
	const repository = COMPANION_SOURCE_REPOSITORIES[companionKey];
	if (!repository || !version) return null;
	return {
		label: `${name} ${version} — source`,
		href: `${repository}/tree/${encodeURIComponent(version)}`,
	};
}

/** Characters of the digest shown after the `sha256:` marker. */
export const DIGEST_PREFIX_LENGTH = 12;

/** `sha256:abcdef…` → `abcdef012345` (12 characters); the full value travels in `title`. */
export function digestPrefix(digest: string): string {
	const hex = digest.startsWith("sha256:") ? digest.slice("sha256:".length) : digest;
	return hex.slice(0, DIGEST_PREFIX_LENGTH);
}

// ---------------------------------------------------------------------------
// S5 — uninstall with the export offer (APP-07)
// ---------------------------------------------------------------------------

export const UNINSTALL_COPY = {
	/** The export sentence, bold in the description until the export is started. */
	exportFirst: "Export your data first — you cannot export it after this.",
	/**
	 * Replaces `exportFirst` once "Export data first" has started the export.
	 * From then on the confirm stays disabled for the rest of this open: the
	 * uninstall waits for the export it would otherwise destroy (CR-05).
	 */
	exportPreparing:
		"Your export is being prepared. You can uninstall once it is ready to download from Settings → Data.",
	confirmLabel: "Uninstall and delete data",
	cancel: "Cancel",
	exportAction: "Export data first",
	exportStartedToast: "Export started",
	uninstallStartedToast: "Uninstall started",
	/** §Error states — "Export could not be started". */
	exportRejected: "The export was not started. Nothing has been removed.",
} as const;

/** S5 dialog title — "Uninstall Postiz". */
export function uninstallTitle(name: string): string {
	return `Uninstall ${name}`;
}

/** The description's opening sentences, before the export sentence. */
export function uninstallConsequences(name: string): string {
	return `This permanently removes ${name}, its database and its uploaded media. Your backups are kept to their normal schedule but this companion's live data is gone. Billing for it stops today.`;
}

/** The description's closing instruction, around the name rendered in mono. */
export const UNINSTALL_TYPE_PROMPT = { before: "Type", after: "to confirm." } as const;

/** §Error states — "Uninstall could not be started". */
export function uninstallRejectedCopy(name: string): string {
	return `The uninstall was not started. ${name} is still running.`;
}

/**
 * §Error states — "Uninstall refused while an export is in progress": the
 * server's 409 `companion_export_in_progress` (CR-05). The export would
 * otherwise lose the data the uninstall destroys.
 */
export function uninstallExportInProgressCopy(name: string): string {
	return `An export that includes ${name}'s data is still being prepared. You can uninstall once it is ready to download from Settings → Data.`;
}

/** The typed confirmation's accessible name. */
export function uninstallInputLabel(name: string): string {
	return `Type ${name} to confirm`;
}

/**
 * The typed confirmation gate: the companion's name exactly, case-sensitive,
 * with only leading and trailing whitespace ignored — the rule the backend
 * applies (plan 06-20).
 */
export function uninstallConfirmMatches(typed: string, name: string): boolean {
	return typed.trim() === name;
}

// ---------------------------------------------------------------------------
// The writes
// ---------------------------------------------------------------------------

export type CompanionLoginLink = Schemas["CompanionLoginLinkResponse"];
export type UpdateDecision = Schemas["CompanionUpdateDecisionRequest"]["decision"];

function statusOf(error: unknown): number | null {
	if (typeof error === "object" && error !== null && "status" in error) {
		const status = (error as { status: unknown }).status;
		return typeof status === "number" ? status : null;
	}
	return null;
}

export type UninstallOutcome =
	| { kind: "started" }
	| { kind: "conflict" }
	| { kind: "rejected"; message: string };

/** The `code` of an `ApiError`'s object detail, or null (any other shape, or unparseable). */
function apiErrorCode(error: unknown): string | null {
	if (!(error instanceof ApiError)) return null;
	const detail = parseApiDetail(error.detail);
	if (typeof detail !== "object" || detail === null) return null;
	const code = (detail as { code?: unknown }).code;
	return typeof code === "string" ? code : null;
}

/**
 * Confirm S5. Nothing is sent unless the typed name matches. A 409
 * `companion_export_in_progress` means a data export still needs this
 * companion's data: that is a refusal rendered inline in the still-open dialog
 * (CR-05). Any other 409 means an uninstall (or another operation) is already
 * running: that is not a second successful uninstall, so there is no success
 * toast — the caller closes and refetches. Any other failure resolves to the
 * contract sentence, never a toast.
 */
export async function runUninstall({
	typed,
	name,
	requestUninstall,
	onStarted,
}: {
	typed: string;
	name: string;
	requestUninstall: (confirm: string) => Promise<unknown>;
	onStarted: () => unknown;
}): Promise<UninstallOutcome> {
	if (!uninstallConfirmMatches(typed, name)) {
		return { kind: "rejected", message: uninstallRejectedCopy(name) };
	}
	try {
		await requestUninstall(typed.trim());
	} catch (error) {
		if (statusOf(error) === 409) {
			if (apiErrorCode(error) === "companion_export_in_progress") {
				return { kind: "rejected", message: uninstallExportInProgressCopy(name) };
			}
			return { kind: "conflict" };
		}
		return { kind: "rejected", message: uninstallRejectedCopy(name) };
	}
	await onStarted();
	return { kind: "started" };
}

/**
 * What S5 does once the uninstall request settles. Accepted: close (the toast
 * and refetch already ran in `onStarted`). Already running (409): refetch so
 * the page shows the server's state, then close — no toast. Refused: stay open
 * with the sentence inline.
 */
export async function settleUninstall(
	outcome: UninstallOutcome,
	{
		close,
		refetch,
		showRejection,
	}: {
		close: () => unknown;
		refetch: () => unknown;
		showRejection: (message: string) => unknown;
	},
): Promise<void> {
	if (outcome.kind === "rejected") {
		await showRejection(outcome.message);
		return;
	}
	if (outcome.kind === "conflict") await refetch();
	await close();
}

export type ActionOutcome = { ok: true } | { ok: false; message: string };

/** "Export data first": start the tenant export (D-31); a failure is the inline sentence. */
export async function runExportFirst({
	requestExport,
	onStarted,
}: {
	requestExport: () => Promise<unknown>;
	onStarted: () => unknown;
}): Promise<ActionOutcome> {
	try {
		await requestExport();
	} catch {
		return { ok: false, message: UNINSTALL_COPY.exportRejected };
	}
	await onStarted();
	return { ok: true };
}

/** Accept or defer the ONE offered digest. A refusal resolves to the given sentence. */
export async function runUpdateDecision({
	decision,
	digest,
	requestDecision,
	rejectedMessage,
	onDone,
}: {
	decision: UpdateDecision;
	digest: string;
	requestDecision: (body: { decision: UpdateDecision; digest: string }) => Promise<unknown>;
	rejectedMessage: string;
	onDone: () => unknown;
}): Promise<ActionOutcome> {
	try {
		await requestDecision({ decision, digest });
	} catch {
		return { ok: false, message: rejectedMessage };
	}
	await onDone();
	return { ok: true };
}

/**
 * "Set your Postiz password": mint the link and open it. The URL is the
 * backend's, opened as served; the page composes nothing (T-06-27-04). A URL
 * that is not https is refused rather than opened.
 */
export async function runLoginLink({
	name,
	requestLink,
	openUrl,
}: {
	name: string;
	requestLink: () => Promise<CompanionLoginLink>;
	openUrl: (url: string) => unknown;
}): Promise<ActionOutcome> {
	let link: CompanionLoginLink;
	try {
		link = await requestLink();
	} catch {
		return { ok: false, message: loginLinkFailedCopy(name) };
	}
	if (!isHttpsUrl(link.url)) return { ok: false, message: loginLinkFailedCopy(name) };
	openUrl(link.url);
	return { ok: true };
}

/**
 * S3's and S5's writes over the generated client, for one installation. The
 * export route is scoped to the caller server-side; nothing here names a tenant.
 */
export function companionRequests(api: Client<paths>, installationId: string) {
	const path = { installation_id: installationId };
	return {
		uninstall: async (confirm: string) =>
			unwrap(
				await api.POST("/v1/companions/installations/{installation_id}/uninstall", {
					params: { path },
					body: { confirm },
				}),
			),
		startExport: async () => unwrap(await api.POST("/v1/me/exports")),
		decideUpdate: async (body: { decision: UpdateDecision; digest: string }) =>
			unwrap(
				await api.POST("/v1/companions/installations/{installation_id}/update", {
					params: { path },
					body,
				}),
			),
		loginLink: async (): Promise<CompanionLoginLink> =>
			unwrap(
				await api.POST("/v1/companions/installations/{installation_id}/login-link", {
					params: { path },
				}),
			),
		logPage: async (afterSeq: number, limit: number): Promise<CompanionLogPage> =>
			unwrap(
				await api.GET("/v1/companions/installations/{installation_id}/logs", {
					params: { path, query: { after_seq: afterSeq, limit } },
				}),
			),
	};
}
