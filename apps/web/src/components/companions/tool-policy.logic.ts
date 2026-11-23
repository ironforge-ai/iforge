import type { components, paths } from "@clawdi/shared/api";
import type { Client } from "openapi-fetch";
import type { StatusTone } from "@/components/ui/status-badge";
import { unwrap } from "@/lib/api";

/**
 * Per-tool approval policy, as transitions rather than as component state.
 *
 * Everything here is pure so the rules that decide whether a tool can run
 * without asking are testable without rendering one. The component owns the
 * mutation; this module owns what the mutation is allowed to mean.
 *
 * **Stored mode and resolved mode are different facts.** `GET /v1/companions/tools`
 * carries `stored_mode` (what the tenant saved, null when no row exists),
 * `resolved_mode` (what `evaluate_policy` would answer right now) and
 * `resolved_reason` (why). `evaluate_policy` checks destroy-enablement first and
 * unconditionally, so a stored `allow` on a destroy verb whose switch is off
 * resolves to `deny`. Rendering only the stored value would display a mode the
 * next call will not honour; rendering only the resolved value would make the
 * destroy switch look like it silently rewrote the tenant's choice. The Select
 * carries the tenant's SETTING and the chip beside it carries what is IN FORCE.
 */

type Schemas = components["schemas"];

export type CompanionToolSettings = Schemas["CompanionToolSettingsResponse"];
export type CompanionSummary = Schemas["CompanionSummaryResponse"];
export type CompanionToolCatalog = Schemas["CompanionToolCatalogResponse"];
export type PolicyMode = Schemas["CompanionToolPolicyRequest"]["mode"];

/** The four classes `config/companion-registry.txt` can assign to a verb. */
export const TOOL_CLASSES = ["read", "create", "mutate", "destroy"] as const;
export type ToolClass = (typeof TOOL_CLASSES)[number];

/** The three cap units a verb can debit. */
export type CapUnit = "calls" | "records" | "bytes";

export interface PolicyOption {
	value: PolicyMode;
	/** Option label. Three fixed words — bounded by design, not by truncation. */
	label: string;
	/** The chip label for the mode currently in force on a row. */
	chipLabel: string;
	tone: StatusTone;
}

/**
 * Three values, in the order the UI spec lists them.
 *
 * Note the deliberate tone inversion: `allow` is `warning`, not `success`. A
 * tool that runs without asking is the weakened state and the chip should say
 * so. `deny` is `neutral` because a blocked tool is a settled, safe condition.
 */
export const policyOptions: readonly PolicyOption[] = [
	{ value: "allow", label: "Allow", chipLabel: "No approval", tone: "warning" },
	{ value: "ask", label: "Ask", chipLabel: "Ask first", tone: "neutral" },
	{ value: "deny", label: "Deny", chipLabel: "Blocked", tone: "neutral" },
];

const POLICY_MODES: readonly PolicyMode[] = policyOptions.map((option) => option.value);

/** Tool-class chip tones, from the UI spec's status token mapping. */
const TOOL_CLASS_TONES: Record<ToolClass, StatusTone> = {
	read: "neutral",
	create: "info",
	mutate: "warning",
	destroy: "destructive",
};

/**
 * A failed companion action, carrying the sentence the UI spec assigns to it.
 *
 * `normalizeApiError` returns `error.message` for any plain `Error`, which is
 * what puts the spec's own copy inline at the action — in `ConfirmAction`'s
 * still-open dialog for a confirmed act, or beneath the control otherwise.
 * The underlying transport failure is kept as `cause` rather than discarded.
 */
export class CompanionActionError extends Error {
	constructor(copy: string, options?: { cause?: unknown }) {
		super(copy);
		this.name = "CompanionActionError";
		if (options?.cause !== undefined) this.cause = options.cause;
	}
}

export const POLICY_CHANGE_ERROR_COPY =
	"The policy change was not saved. Nothing changed — try again.";

export const POLICY_SAVED_TOAST = "Policy saved";

export const DESTROY_ENABLEMENT_ERROR_COPY =
	"Destroy tools were not enabled. They stay hidden from the agent.";

export const DESTROY_ENABLED_TOAST = "Destroy tools enabled";
export const DESTROY_DISABLED_TOAST = "Destroy tools disabled";

export const DESTROY_GROUP_TITLE = "Destroy tools";

export const DESTROY_GROUP_EMPTY_COPY = {
	title: "No destroy tools enabled",
	description:
		"Destroy tools stay hidden from the agent until you enable them. While they are off, the agent cannot see them and cannot call them by name.",
} as const;

export const NO_COMPANIONS_EMPTY_COPY = {
	title: "No companions deployed",
	description:
		"Deploy a companion to this agent and its tools appear here — classified, capped, and policy-gated before the agent can call one.",
	action: "Deploy a companion",
} as const;

export const COMPANION_TOOLS_PAGE_COPY = {
	title: "Companion Tools",
	description: "Every tool your agent can reach, and the policy that decides whether it runs.",
	toolsHeading: "Tools",
} as const;

/** True when the registry gave this verb one of the four known classes. */
export function isToolClassResolved(toolClass: string): toolClass is ToolClass {
	return (TOOL_CLASSES as readonly string[]).includes(toolClass);
}

export function toolClassTone(toolClass: string): StatusTone {
	return isToolClassResolved(toolClass) ? TOOL_CLASS_TONES[toolClass] : "neutral";
}

function isPolicyMode(value: string | null | undefined): value is PolicyMode {
	return typeof value === "string" && (POLICY_MODES as readonly string[]).includes(value);
}

/**
 * The value the Select renders — always one of the three, never unset.
 *
 * A row with no stored policy falls back to `resolved_mode`, which for a row
 * with no policy IS the class default the façade would apply. That is why "the
 * shipped default is ask-on-write so no row renders as unset" holds without the
 * UI inventing a default of its own: the server already answered.
 *
 * A stored value outside the vocabulary (the corrupt-row case `stored_mode` is
 * typed `string | null` to keep visible) also falls back to the resolved mode,
 * which `evaluate_policy` reports as `ask`.
 */
export function policySelectValue(row: CompanionToolSettings): PolicyMode {
	return isPolicyMode(row.stored_mode) ? row.stored_mode : row.resolved_mode;
}

/** The chip for what is actually in force, which is never the stored value. */
export function resolvedPolicyOption(row: CompanionToolSettings): PolicyOption {
	const option = policyOptions.find((candidate) => candidate.value === row.resolved_mode);
	// `resolved_mode` is a closed enum on the wire; the fallback exists so a
	// server that widened it renders as the safest mode rather than crashing.
	return option ?? (policyOptions[1] as PolicyOption);
}

/** True when what is in force differs from what the tenant set. */
export function policyIsOverridden(row: CompanionToolSettings): boolean {
	return row.resolved_mode !== policySelectValue(row);
}

/**
 * The sentence that explains a resolution the tenant did not choose, or null
 * when the resolution needs no explanation.
 */
export function resolvedReasonSentence(reason: string): string | null {
	if (reason === "refused_destroy_disabled") {
		return "Destroy tools are off for this companion, so this tool is refused whatever mode you set.";
	}
	if (reason === "unclassified_defaults_to_ask") {
		return "The registry has not classified this tool, so it asks before running and its policy cannot be changed.";
	}
	return null;
}

/**
 * Setting a `mutate` or `destroy` tool to Allow routes through a confirmation.
 * Allowing a `read` tool does not — read is already the class default.
 */
export function requiresConfirmation(toolClass: string, nextMode: PolicyMode): boolean {
	if (nextMode !== "allow") return false;
	return toolClass === "mutate" || toolClass === "destroy";
}

/**
 * An unclassified verb renders its Select DISABLED rather than defaulting to
 * Allow. A verb the registry could not classify must never become callable
 * without approval because a control defaulted open.
 */
export function policySelectDisabled(row: CompanionToolSettings, pending: boolean): boolean {
	return pending || !isToolClassResolved(row.tool_class);
}

export interface PolicyRowState {
	/** What the Select renders. Never the requested value while in flight. */
	mode: PolicyMode;
	/** What a rejection reverts to. */
	priorMode: PolicyMode;
	/** What was asked for, while a change is in flight. */
	requestedMode: PolicyMode | null;
	pending: boolean;
	error: string | null;
}

export type PolicyRowEvent =
	| { type: "request"; mode: PolicyMode }
	| { type: "settled"; mode: PolicyMode }
	| { type: "failed" };

export function initialPolicyState(mode: PolicyMode): PolicyRowState {
	return { mode, priorMode: mode, requestedMode: null, pending: false, error: null };
}

/**
 * The policy transition machine.
 *
 * There is NO optimistic flip. While a change is in flight the Select keeps
 * rendering the prior value, so a rejection has nothing to undo and the control
 * never shows a mode the server has not accepted. A second request inside one
 * frame is dropped by the pending guard.
 */
export function nextPolicyState(state: PolicyRowState, event: PolicyRowEvent): PolicyRowState {
	if (event.type === "request") {
		if (state.pending) return state;
		return {
			mode: state.mode,
			priorMode: state.mode,
			requestedMode: event.mode,
			pending: true,
			error: null,
		};
	}
	if (event.type === "settled") {
		return {
			mode: event.mode,
			priorMode: event.mode,
			requestedMode: null,
			pending: false,
			error: null,
		};
	}
	return {
		mode: state.priorMode,
		priorMode: state.priorMode,
		requestedMode: null,
		pending: false,
		error: POLICY_CHANGE_ERROR_COPY,
	};
}

export interface ConfirmCopy {
	title: string;
	description: string;
	confirmLabel: string;
	destructive: boolean;
}

/** The "Allow without asking" confirmation, from the UI spec's table. */
export function allowWithoutAskingCopy(tool: string, agentLabel: string): ConfirmCopy {
	return {
		title: "Allow without asking",
		description: `${tool} will run whenever ${agentLabel} calls it, with no approval step. Blast-radius caps still apply.`,
		confirmLabel: "Allow without asking",
		destructive: false,
	};
}

/** The "Enable destroy tools" confirmation, from the UI spec's table. */
export function enableDestroyToolsCopy(companionKey: string, agentLabel: string): ConfirmCopy {
	return {
		title: "Enable destroy tools",
		description: `Destroy tools permanently remove data in ${companionKey}. Once enabled, ${agentLabel} can see them and call them, subject to the approval policy you set. You can turn this off at any time.`,
		confirmLabel: "Enable destroy tools",
		destructive: true,
	};
}

/**
 * S4 — a companion's whole tool group, blocked or allowed (APP-05, D-29, D-30).
 *
 * The block is a control-plane guarantee enforced server-side (plan 06-24): a
 * blocked group leaves the agent's `tools/list` and a call to it by name is
 * refused and recorded. The copy says exactly that and no more. The weakening
 * direction is BLOCKING — it takes tools away from a running agent — so that is
 * the direction that confirms; allowing again does not.
 */
export const GROUP_BLOCKED_CHIP = "Tools blocked";
export const GROUP_BLOCKED_HELPER =
	"Your agent cannot see or call these. Calls it tries anyway are refused and recorded.";
export const GROUP_BLOCK_ERROR_COPY =
	"The tools were not disabled. Your agent can still call them.";
export const GROUP_ALLOW_ERROR_COPY = "The tools were not enabled. They stay blocked.";
export const GROUP_BLOCKED_TOAST = "Tools blocked";
export const GROUP_ALLOWED_TOAST = "Tools allowed";

/** The "Block {companion} tools" confirmation, from the UI spec's table. */
export function blockToolsCopy(companionKey: string): ConfirmCopy {
	return {
		title: `Block ${companionKey} tools`,
		description: `Your agent will stop being able to see or call any of ${companionKey}'s tools. Calls it tries anyway are refused and recorded. ${companionKey} keeps running and your data stays reachable. You can allow them again at any time.`,
		confirmLabel: "Block tools",
		destructive: true,
	};
}

/** The group Switch's screen-reader label. Checked means the group is allowed. */
export function allowToolsSwitchLabel(companionKey: string): string {
	return `Allow ${companionKey} tools`;
}

/** The sentence a refused request renders at the Switch, by the direction asked. */
export function groupBlockErrorCopy(blocked: boolean): string {
	return blocked ? GROUP_BLOCK_ERROR_COPY : GROUP_ALLOW_ERROR_COPY;
}

export type GroupBlockOutcome = { ok: true } | { ok: false; message: string };

/** `PUT /v1/companions/group-block/{companion_key}` through the generated client. */
export function groupBlockRequest(
	api: Client<paths>,
	companionKey: string,
): (blocked: boolean) => Promise<unknown> {
	return async (blocked) =>
		unwrap(
			await api.PUT("/v1/companions/group-block/{companion_key}", {
				params: { path: { companion_key: companionKey } },
				body: { blocked },
			}),
		);
}

/**
 * Hold one group-block position against the server.
 *
 * There is no optimistic flip: the caller renders the Switch from the server's
 * `blocked` alone, so nothing here moves it. A refusal resolves to the S4
 * sentence for the direction asked and raises NO toast — errors render inline
 * at the action. Only an accepted request reaches `onAccepted`, with the
 * success toast for that direction.
 */
export async function runGroupBlock({
	blocked,
	request,
	onAccepted,
}: {
	blocked: boolean;
	request: (blocked: boolean) => Promise<unknown>;
	onAccepted: (toast: string) => unknown;
}): Promise<GroupBlockOutcome> {
	try {
		await request(blocked);
	} catch {
		return { ok: false, message: groupBlockErrorCopy(blocked) };
	}
	await onAccepted(blocked ? GROUP_BLOCKED_TOAST : GROUP_ALLOWED_TOAST);
	return { ok: true };
}

export interface CompanionToolGroup {
	companion: CompanionSummary;
	/** read, create and mutate verbs, in registry order. */
	tools: readonly CompanionToolSettings[];
	/** destroy verbs, which render in their own group behind the Switch. */
	destroyTools: readonly CompanionToolSettings[];
}

/**
 * Only the companions deployed on THIS agent.
 *
 * The settings API is tenant-scoped — it has no agent parameter, by design —
 * so the agent scope this route already carries is applied here rather than
 * asked of the server. A companion summary names every environment it is
 * deployed to; membership is that list, never a guess.
 */
export function companionsForAgent(
	catalog: CompanionToolCatalog | undefined,
	environmentId: string,
): readonly CompanionSummary[] {
	return (catalog?.companions ?? []).filter((companion) =>
		companion.environment_ids.includes(environmentId),
	);
}

/** Group the flat verb rows under the companions deployed on this agent. */
export function groupToolsByCompanion(
	catalog: CompanionToolCatalog | undefined,
	environmentId: string,
): readonly CompanionToolGroup[] {
	const companions = companionsForAgent(catalog, environmentId);
	const rows = catalog?.tools ?? [];
	return companions.map((companion) => {
		const owned = rows.filter((row) => row.companion_key === companion.companion_key);
		return {
			companion,
			tools: owned.filter((row) => row.tool_class !== "destroy"),
			destroyTools: owned.filter((row) => row.tool_class === "destroy"),
		};
	});
}

/**
 * A companion the catalog knows about but whose verbs have not arrived.
 *
 * `visible_verb_count` comes from the companion summary and the rows come from
 * the flat verb list; a deployed-but-unreconciled companion has the first and
 * not yet the second. Such a group renders SKELETON rows, because rendering it
 * as a zero-verb companion would state something false — that this companion
 * exposes nothing — about a companion that simply has not been read yet.
 */
export function groupIsReconciling(group: CompanionToolGroup): boolean {
	return (
		group.tools.length === 0 &&
		group.destroyTools.length === 0 &&
		group.companion.visible_verb_count > 0
	);
}

/**
 * Whether a companion's group renders at all (S4 `empty`).
 *
 * A companion with zero registered tools — no rows, and no verbs the catalog
 * still owes — renders no header and no group Switch: a header above an empty
 * region, carrying a control that governs nothing, is not rendered. A
 * reconciling group still renders, because its verbs are coming.
 */
export function groupRendersHeader(group: CompanionToolGroup): boolean {
	return group.tools.length > 0 || group.destroyTools.length > 0 || groupIsReconciling(group);
}

/**
 * Whether the destroy group renders at all.
 *
 * `has_destroy_verbs` and `destroy_enabled` are separate fields because they
 * are separate situations. A companion that declares no destroy verb renders NO
 * group — a group with a Switch that governs nothing is worse than no group.
 */
export function showsDestroyGroup(companion: CompanionSummary): boolean {
	return companion.has_destroy_verbs;
}

/** The verb count the SectionLabel renders — a bare integer, never pluralized. */
export function visibleVerbCount(companion: CompanionSummary): number {
	return companion.visible_verb_count;
}

export function capUnitMeta(capUnit: string): string {
	return `Debits ${capUnit}`;
}
