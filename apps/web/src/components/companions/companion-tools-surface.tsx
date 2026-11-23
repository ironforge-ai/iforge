"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Blocks } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { agentDisplayName } from "@/components/dashboard/agent-label";
import { EmptyState } from "@/components/empty-state";
import { PageHeader, PageHeaderSkeleton } from "@/components/page-header";
import { CENTERED_PAGE_WIDTH_CLASS } from "@/components/page-width";
import { SectionLabel } from "@/components/section-label";
import { buttonVariants } from "@/components/ui/button";
import { ConfirmAction } from "@/components/ui/confirm-action";
import { StatusBadge } from "@/components/ui/status-badge";
import { Switch } from "@/components/ui/switch";
import { unwrap, useApi, useOpenApi } from "@/lib/api";
import { shouldBlockQueryError } from "@/lib/query-state";
import { cn } from "@/lib/utils";
import {
	APPROVAL_DECISION_ERROR_COPY,
	APPROVALS_COPY,
	type ApprovalDecision,
	type CompanionApproval,
} from "./approval-decision.logic";
import { BlastRadiusCapsCard } from "./blast-radius-caps-card";
import { matchApprovalToCall } from "./call-detail.logic";
import { CallDetailSheet } from "./call-detail-sheet";
import {
	CAPS_COPY,
	type CapVectorRequest,
	type ConsumptionItem,
	capVectorFor,
} from "./caps-form.logic";
import { CompanionToolRow, CompanionToolRowSkeleton } from "./companion-tool-row";
import { DestroyToolsGroup } from "./destroy-tools-group";
import { PendingApprovalsRegion } from "./pending-approvals-card";
import { CompanionSectionTabs } from "./tool-activity-table";
import {
	allowToolsSwitchLabel,
	blockToolsCopy,
	COMPANION_TOOLS_PAGE_COPY,
	CompanionActionError,
	type CompanionSummary,
	type CompanionToolCatalog,
	type CompanionToolGroup,
	DESTROY_DISABLED_TOAST,
	DESTROY_ENABLED_TOAST,
	DESTROY_ENABLEMENT_ERROR_COPY,
	GROUP_BLOCKED_CHIP,
	GROUP_BLOCKED_HELPER,
	groupBlockRequest,
	groupIsReconciling,
	groupRendersHeader,
	groupToolsByCompanion,
	initialPolicyState,
	NO_COMPANIONS_EMPTY_COPY,
	nextPolicyState,
	POLICY_CHANGE_ERROR_COPY,
	POLICY_SAVED_TOAST,
	type PolicyMode,
	type PolicyRowState,
	policySelectValue,
	runGroupBlock,
} from "./tool-policy.logic";

/** openapi-react-query cache keys for the companion settings reads. */
export const COMPANION_TOOLS_QUERY_KEY = ["get", "/v1/companions/tools"] as const;

export const COMPANION_CAPS_QUERY_KEY = ["get", "/v1/companions/caps"] as const;
export const COMPANION_CONSUMPTION_QUERY_KEY = ["get", "/v1/companions/consumption"] as const;
export const COMPANION_APPROVALS_QUERY_KEY = ["get", "/v1/companions/approvals"] as const;

/** Where "Deploy a companion" goes — the existing deploy route. */
export const DEPLOY_COMPANION_HREF = "/deploy";

const SURFACE_CLASS = cn(CENTERED_PAGE_WIDTH_CLASS.page, "space-y-8 px-4 lg:px-6");

/**
 * The Companion Tools page: three regions in approvals, tools, caps order.
 *
 * Approvals come first because they are the only region with a deadline. The
 * page reads every value from the companions settings routes through the
 * generated client — never a hand-written fetch — so a mode this surface
 * displays is always one the façade would actually resolve.
 */
export function CompanionToolsSurface({ environmentId }: { environmentId: string }) {
	const $api = useOpenApi();
	const api = useApi();
	const queryClient = useQueryClient();

	const agent = $api.useQuery("get", "/v1/agents/{agent_id}", {
		params: { path: { agent_id: environmentId } },
	});
	const catalog = $api.useQuery("get", "/v1/companions/tools", {});
	const caps = $api.useQuery("get", "/v1/companions/caps", {});
	const consumption = $api.useQuery("get", "/v1/companions/consumption", {});
	const approvals = $api.useQuery("get", "/v1/companions/approvals", {});

	const agentLabel = agent.data ? agentDisplayName(agent.data) : "this agent";
	const catalogError = shouldBlockQueryError(catalog.error, catalog.data) ? catalog.error : null;
	const capsError = shouldBlockQueryError(caps.error, caps.data) ? caps.error : null;
	const approvalsError = shouldBlockQueryError(approvals.error, approvals.data)
		? approvals.error
		: null;

	// The approval whose provenance chain the reviewer asked to see, and the
	// lookup that turns it into a recorded call. There is no approval->call route
	// and the listing row carries no `approval_id`, so the candidate is CHOSEN by
	// timestamp proximity among this tenant's pending-approval rows for the same
	// tool and then CONFIRMED against the detail's own `approval_id`. An
	// unconfirmed candidate is never rendered: showing somebody else's chain to a
	// reviewer who is one click from authorizing a write would be worse than
	// showing none.
	const [reviewing, setReviewing] = useState<CompanionApproval | null>(null);
	const pendingCalls = $api.useQuery(
		"get",
		"/v1/companions/activity",
		{ params: { query: { decision: "pending_approval" as const, limit: 200, offset: 0 } } },
		{ enabled: reviewing !== null },
	);
	const reviewCallId = reviewing
		? matchApprovalToCall(pendingCalls.data?.items ?? [], reviewing)
		: null;

	const [policyStates, setPolicyStates] = useState<Record<string, PolicyRowState>>({});
	const [enablementErrors, setEnablementErrors] = useState<Record<string, string | null>>({});
	const [groupBlockErrors, setGroupBlockErrors] = useState<Record<string, string | null>>({});
	const [groupBlockPending, setGroupBlockPending] = useState<Record<string, boolean>>({});
	// Synchronous per-companion lock: `groupBlockPending` only disables the
	// Switch on the next render, so a fast double toggle would otherwise send
	// two requests before React repaints.
	const groupBlockInFlight = useRef(new Set<string>());

	function policyState(tool: string, fallback: PolicyMode): PolicyRowState {
		return policyStates[tool] ?? initialPolicyState(fallback);
	}

	function dispatchPolicy(
		tool: string,
		fallback: PolicyMode,
		event: Parameters<typeof nextPolicyState>[1],
	) {
		setPolicyStates((previous) => ({
			...previous,
			[tool]: nextPolicyState(previous[tool] ?? initialPolicyState(fallback), event),
		}));
	}

	async function setToolPolicy(tool: string, current: PolicyMode, mode: PolicyMode) {
		dispatchPolicy(tool, current, { type: "request", mode });
		try {
			await unwrap(
				await api.PUT("/v1/companions/tools/{tool}/policy", {
					params: { path: { tool } },
					body: { mode },
				}),
			);
			dispatchPolicy(tool, current, { type: "settled", mode });
			toast.success(POLICY_SAVED_TOAST);
			await queryClient.invalidateQueries({ queryKey: COMPANION_TOOLS_QUERY_KEY });
		} catch (cause) {
			dispatchPolicy(tool, current, { type: "failed" });
			// The dialog stays open and renders this sentence inline; the Select
			// never moved, so there is nothing to revert.
			throw new CompanionActionError(POLICY_CHANGE_ERROR_COPY, { cause });
		}
	}

	async function setDestroyEnabled(companionKey: string, enabled: boolean) {
		try {
			await unwrap(
				await api.PUT("/v1/companions/destroy-enablement/{companion_key}", {
					params: { path: { companion_key: companionKey } },
					body: { enabled },
				}),
			);
			setEnablementErrors((previous) => ({ ...previous, [companionKey]: null }));
			toast.success(enabled ? DESTROY_ENABLED_TOAST : DESTROY_DISABLED_TOAST);
			await queryClient.invalidateQueries({ queryKey: COMPANION_TOOLS_QUERY_KEY });
		} catch (cause) {
			setEnablementErrors((previous) => ({
				...previous,
				[companionKey]: DESTROY_ENABLEMENT_ERROR_COPY,
			}));
			throw new CompanionActionError(DESTROY_ENABLEMENT_ERROR_COPY, { cause });
		}
	}

	/**
	 * S4: block or allow a companion's whole tool group.
	 *
	 * The Switch renders from the server's `blocked` alone, so it cannot move
	 * before the PUT is accepted AND the catalog has been re-read. A refusal
	 * records the direction's sentence beneath the Switch and rethrows it, so a
	 * confirmed block also shows it in `ConfirmAction`'s still-open dialog.
	 */
	async function setGroupBlocked(companionKey: string, blocked: boolean) {
		if (groupBlockInFlight.current.has(companionKey)) return;
		groupBlockInFlight.current.add(companionKey);
		setGroupBlockPending((previous) => ({ ...previous, [companionKey]: true }));
		setGroupBlockErrors((previous) => ({ ...previous, [companionKey]: null }));
		try {
			const outcome = await runGroupBlock({
				blocked,
				request: groupBlockRequest(api, companionKey),
				onAccepted: async (message) => {
					toast.success(message);
					await queryClient.invalidateQueries({ queryKey: COMPANION_TOOLS_QUERY_KEY });
				},
			});
			if (!outcome.ok) {
				setGroupBlockErrors((previous) => ({ ...previous, [companionKey]: outcome.message }));
				throw new CompanionActionError(outcome.message);
			}
		} finally {
			groupBlockInFlight.current.delete(companionKey);
			setGroupBlockPending((previous) => ({ ...previous, [companionKey]: false }));
		}
	}

	async function saveCaps(companionKey: string, payload: CapVectorRequest) {
		await unwrap(
			await api.PUT("/v1/companions/caps/{companion_key}", {
				params: { path: { companion_key: companionKey } },
				body: payload,
			}),
		);
		toast.success(CAPS_COPY.savedToast);
		await Promise.all([
			queryClient.invalidateQueries({ queryKey: COMPANION_CAPS_QUERY_KEY }),
			queryClient.invalidateQueries({ queryKey: COMPANION_CONSUMPTION_QUERY_KEY }),
		]);
	}

	async function decide(approval: CompanionApproval, decision: ApprovalDecision) {
		try {
			await unwrap(
				decision === "approve"
					? await api.POST("/v1/companions/approvals/{approval_id}/approve", {
							params: { path: { approval_id: approval.id } },
						})
					: await api.POST("/v1/companions/approvals/{approval_id}/deny", {
							params: { path: { approval_id: approval.id } },
						}),
			);
			toast.success(
				decision === "approve" ? APPROVALS_COPY.approvedToast : APPROVALS_COPY.deniedToast,
			);
		} catch (cause) {
			// A conflict or an expiry is the server's answer, not a client
			// state to fix: refetch so the card re-renders from what IS.
			await queryClient.invalidateQueries({ queryKey: COMPANION_APPROVALS_QUERY_KEY });
			throw new CompanionActionError(APPROVAL_DECISION_ERROR_COPY, { cause });
		}
		await Promise.all([
			queryClient.invalidateQueries({ queryKey: COMPANION_APPROVALS_QUERY_KEY }),
			queryClient.invalidateQueries({ queryKey: COMPANION_CONSUMPTION_QUERY_KEY }),
		]);
	}

	function copyToolName(tool: string) {
		void navigator.clipboard?.writeText(tool).then(
			() => toast.success("Tool name copied"),
			() => toast.error("The tool name could not be copied."),
		);
	}

	const groups = groupToolsByCompanion(catalog.data, environmentId);
	/** The verb's cap unit, joined from the catalog the page already holds. */
	function capUnitFor(tool: string): string | null {
		return catalog.data?.tools.find((row) => row.tool === tool)?.cap_unit ?? null;
	}
	const agentCompanionKeys = new Set(groups.map((group) => group.companion.companion_key));
	// The approvals route is tenant-scoped by design; this page is agent-scoped,
	// so the agent's own companions are the filter rather than a query the
	// server does not accept.
	const agentApprovals = (approvals.data?.items ?? []).filter((approval) =>
		agentCompanionKeys.has(approval.companion_key),
	);

	return (
		<div className={SURFACE_CLASS} data-testid="companion-tools-surface">
			{catalog.isLoading ? (
				<PageHeaderSkeleton />
			) : (
				<PageHeader
					title={COMPANION_TOOLS_PAGE_COPY.title}
					description={COMPANION_TOOLS_PAGE_COPY.description}
					status={<CompanionStatusRow groups={groups} />}
				/>
			)}
			{/* The Tools / Activity pair. Without it the activity log is
			    unreachable by clicking: the sidebar's Companions entry resolves to
			    this index route only. */}
			<CompanionSectionTabs agentId={environmentId} active="tools" />
			{/* Region 1 — pending approvals, first because they have a deadline.
			    `onReviewDetails` opens the SAME drawer the activity table opens,
			    so a reviewer sees the whole chain and the untrusted bytes before
			    authorizing. Plan 05-08 left this prop unpassed on purpose while
			    the Sheet did not exist; 05-09 ships it and passes it. */}
			<PendingApprovalsRegion
				approvals={agentApprovals}
				agentLabel={agentLabel}
				capUnitFor={capUnitFor}
				loading={approvals.isLoading}
				error={approvalsError}
				onRetry={() => void approvals.refetch()}
				onDecide={decide}
				onReviewDetails={setReviewing}
			/>
			<CallDetailSheet
				callId={reviewCallId}
				row={pendingCalls.data?.items.find((item) => item.id === reviewCallId) ?? null}
				open={reviewing !== null}
				onOpenChange={(next) => {
					if (!next) setReviewing(null);
				}}
				agentId={environmentId}
				agentLabel={agent.data ? agentDisplayName(agent.data) : undefined}
				expectedApprovalId={reviewing?.id}
				unlinked={reviewing !== null && pendingCalls.isSuccess && reviewCallId === null}
			/>
			{/* Region 2 — the tool catalog. */}
			<CompanionToolsRegion
				loading={catalog.isLoading}
				error={catalogError}
				onRetry={() => void catalog.refetch()}
				groups={groups}
				agentLabel={agentLabel}
				policyState={policyState}
				onModeChange={setToolPolicy}
				onSetDestroyEnabled={setDestroyEnabled}
				enablementErrors={enablementErrors}
				onSetGroupBlocked={setGroupBlocked}
				groupBlockErrors={groupBlockErrors}
				groupBlockPending={groupBlockPending}
				onCopyToolName={copyToolName}
			/>
			{/* Region 3 — blast-radius caps, one card per deployed companion. */}
			{groups.map((group) => (
				<BlastRadiusCapsCard
					key={group.companion.companion_key}
					companionKey={group.companion.companion_key}
					vector={capVectorFor(caps.data, group.companion.companion_key)}
					consumption={windowConsumptionFor(
						consumption.data?.items ?? [],
						group.companion.companion_key,
						environmentId,
					)}
					windowStartedAt={consumption.data?.window_started_at}
					loading={caps.isLoading}
					error={capsError}
					onRetry={() => void caps.refetch()}
					onSave={(payload) => saveCaps(group.companion.companion_key, payload)}
				/>
			))}
		</div>
	);
}

/**
 * The consumption item the caps card reads for one companion on this Agent:
 * the WINDOW ledger's row, selected by its null `session_id` (WINDOWS #187).
 *
 * The read-out carries a window row AND live session rows per companion and
 * Agent, so a bare match on the companion key read whichever row came first —
 * a session counter of 2 while the ceiling that refuses the next call stood at
 * 7. The discriminator is on every item; select by it, never by position. The
 * card's read-out is per Agent because the window ledger is keyed by Agent.
 * No window row yet is `undefined`, which the card renders as nothing spent.
 */
export function windowConsumptionFor(
	items: readonly ConsumptionItem[],
	companionKey: string,
	environmentId: string,
): ConsumptionItem | undefined {
	return items.find(
		(item) =>
			item.companion_key === companionKey &&
			item.environment_id === environmentId &&
			item.session_id === null,
	);
}

/** One `StatusBadge` per deployed companion, in the header's meta row. */
export function CompanionStatusRow({ groups }: { groups: readonly CompanionToolGroup[] }) {
	if (groups.length === 0) return null;
	return (
		<div className="flex flex-wrap items-center gap-2">
			{groups.map((group) => (
				<StatusBadge
					key={group.companion.companion_key}
					status={group.companion.destroy_enabled ? "warning" : "neutral"}
				>
					<span className="font-mono">{group.companion.companion_key}</span>
				</StatusBadge>
			))}
		</div>
	);
}

/**
 * The tools region, as a pure function of its props.
 *
 * Kept separate from the connected surface so every UI state — empty, loading,
 * error, populated — is reachable from a test without a query client.
 */
export function CompanionToolsRegion({
	loading,
	error,
	onRetry,
	groups,
	agentLabel,
	policyState,
	onModeChange,
	onSetDestroyEnabled,
	enablementErrors,
	onSetGroupBlocked,
	groupBlockErrors,
	groupBlockPending,
	onCopyToolName,
}: {
	loading: boolean;
	error: unknown;
	onRetry: () => void;
	groups: readonly CompanionToolGroup[];
	agentLabel: string;
	policyState: (tool: string, fallback: PolicyMode) => PolicyRowState;
	onModeChange: (tool: string, current: PolicyMode, mode: PolicyMode) => Promise<unknown>;
	onSetDestroyEnabled: (companionKey: string, enabled: boolean) => Promise<unknown>;
	enablementErrors?: Record<string, string | null>;
	/** Resolves when the server has accepted the position; rejects otherwise. */
	onSetGroupBlocked: (companionKey: string, blocked: boolean) => Promise<unknown>;
	groupBlockErrors?: Record<string, string | null>;
	groupBlockPending?: Record<string, boolean>;
	onCopyToolName?: (tool: string) => void;
}) {
	if (error) {
		return (
			<section data-slot="companion-tools-region" className="space-y-3">
				<SectionLabel>{COMPANION_TOOLS_PAGE_COPY.toolsHeading}</SectionLabel>
				<ApiErrorPanel error={error} onRetry={onRetry} />
			</section>
		);
	}

	if (loading) {
		return (
			<section data-slot="companion-tools-region" className="space-y-3">
				<SectionLabel>{COMPANION_TOOLS_PAGE_COPY.toolsHeading}</SectionLabel>
				<div className="rounded-lg border border-border">
					{Array.from({ length: 5 }).map((_, index) => (
						<CompanionToolRowSkeleton key={index} />
					))}
				</div>
			</section>
		);
	}

	if (groups.length === 0) {
		return (
			<section data-slot="companion-tools-region">
				<EmptyState
					variant="page"
					icon={Blocks}
					title={NO_COMPANIONS_EMPTY_COPY.title}
					description={NO_COMPANIONS_EMPTY_COPY.description}
					action={
						// A plain anchor, styled as a button, following
						// `components/app-not-found.tsx`. A `Link` would need a
						// RouterProvider, which would put this state — the one a
						// tenant with nothing deployed actually sees — beyond the
						// reach of the held-out state test that proves its copy.
						<a href={DEPLOY_COMPANION_HREF} className={cn(buttonVariants())}>
							{NO_COMPANIONS_EMPTY_COPY.action}
						</a>
					}
				/>
			</section>
		);
	}

	return (
		<section data-slot="companion-tools-region" className="space-y-6">
			{groups.filter(groupRendersHeader).map((group) => {
				const companionKey = group.companion.companion_key;
				const blocked = group.companion.blocked;
				const blockError = groupBlockErrors?.[companionKey] ?? null;
				return (
					<div
						key={companionKey}
						data-slot="companion-tool-group"
						data-group-blocked={blocked ? "true" : "false"}
						className="space-y-3"
					>
						<div data-slot="group-block-header">
							<SectionLabel
								leading={<Blocks className="size-4" aria-hidden />}
								count={group.companion.visible_verb_count}
								trailing={
									<>
										{blocked ? (
											<StatusBadge status="destructive">{GROUP_BLOCKED_CHIP}</StatusBadge>
										) : null}
										<GroupBlockSwitch
											companion={group.companion}
											pending={groupBlockPending?.[companionKey] ?? false}
											onSetBlocked={(next) => onSetGroupBlocked(companionKey, next)}
										/>
									</>
								}
							>
								{companionKey}
							</SectionLabel>
						</div>
						{blockError ? (
							<p
								data-slot="group-block-error"
								className="text-destructive-muted-foreground text-xs"
							>
								{blockError}
							</p>
						) : null}
						{blocked ? (
							<p data-slot="group-blocked-helper" className="text-muted-foreground text-xs">
								{GROUP_BLOCKED_HELPER}
							</p>
						) : null}
						<div className="rounded-lg border border-border">
							{groupIsReconciling(group)
								? Array.from({ length: group.companion.visible_verb_count }).map((_, index) => (
										<CompanionToolRowSkeleton key={index} />
									))
								: group.tools.map((row) => (
										<CompanionToolRow
											key={row.tool}
											row={row}
											agentLabel={agentLabel}
											state={policyState(row.tool, policySelectValue(row))}
											onModeChange={(mode) => onModeChange(row.tool, policySelectValue(row), mode)}
											onCopyToolName={onCopyToolName}
											blocked={blocked}
										/>
									))}
						</div>
						<DestroyToolsGroup
							companion={group.companion}
							tools={group.destroyTools}
							agentLabel={agentLabel}
							policyState={(tool) => {
								const row = group.destroyTools.find((candidate) => candidate.tool === tool);
								return policyState(tool, row ? policySelectValue(row) : "ask");
							}}
							onModeChange={(tool, mode) => {
								const row = group.destroyTools.find((candidate) => candidate.tool === tool);
								return onModeChange(tool, row ? policySelectValue(row) : "ask", mode);
							}}
							onSetEnabled={(enabled) => onSetDestroyEnabled(companionKey, enabled)}
							enablementError={enablementErrors?.[companionKey] ?? null}
							onCopyToolName={onCopyToolName}
							blocked={blocked}
						/>
					</div>
				);
			})}
		</section>
	);
}

/**
 * S4's group Switch. Checked means the group is ALLOWED.
 *
 * **The Switch never flips optimistically** — the `DestroyToolsGroup`
 * precedent, with the direction inverted because here the weakening move is
 * turning it OFF. While allowed, the Switch IS the "Block {companion} tools"
 * `ConfirmAction` trigger and carries no `onCheckedChange`, so it cannot move
 * until the server has accepted the block and the catalog says so; a refusal
 * stays inline in the still-open dialog. While blocked, allowing again is the
 * safe direction and sends the PUT directly, locked while it is in flight.
 */
export function GroupBlockSwitch({
	companion,
	pending,
	onSetBlocked,
}: {
	companion: CompanionSummary;
	pending: boolean;
	/** Resolves when the server has accepted the position; rejects otherwise. */
	onSetBlocked: (blocked: boolean) => Promise<unknown>;
}) {
	const label = allowToolsSwitchLabel(companion.companion_key);
	if (companion.blocked) {
		return (
			<span data-slot="group-block-control" className="inline-flex">
				<Switch
					checked={false}
					aria-label={label}
					disabled={pending}
					onCheckedChange={() => {
						void onSetBlocked(false).catch(() => {});
					}}
				/>
			</span>
		);
	}
	const copy = blockToolsCopy(companion.companion_key);
	return (
		<span data-slot="group-block-control" className="inline-flex">
			<ConfirmAction
				title={copy.title}
				description={copy.description}
				confirmLabel={copy.confirmLabel}
				destructive={copy.destructive}
				onConfirm={() => onSetBlocked(true)}
			>
				<Switch checked aria-label={label} />
			</ConfirmAction>
		</span>
	);
}

export type { CompanionToolCatalog };
