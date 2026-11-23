import { DESTINATIONS } from "@clawdi/shared";
import type { components } from "@/lib/api-schemas";

type Schemas = components["schemas"];

export type ProjectInvitationNotification = Schemas["InvitationResponse"];
export type AcceptInvitationResponse = Schemas["InvitationAcceptResponse"];

export type AccountNotification = {
	id: string;
	title: string;
	description: string;
	category: string;
	createdAt: Date;
	read: boolean;
	actionLabel?: string;
	actionUrl?: string;
	severity: "info" | "warning" | "destructive";
};

export type NotificationCenterView = "all" | "unread";

// The external origins an account notification's action may open: this product's own
// dashboard and marketing site, from the destinations authority (03.3-07), never a host
// literal here.
const NOTIFICATION_ACTION_ORIGINS = new Set<string>([
	DESTINATIONS.dashboardOrigin,
	DESTINATIONS.wwwOrigin,
]);

// Project invitations are the first notification source. Keep the shell named
// generically so future notification types (agent health, billing, access
// changes) can join without replacing the header affordance.
export const NOTIFICATION_CENTER_QUERY_KEY = ["get", "/v1/me/invitations"] as const;
export const NOTIFICATION_CENTER_MEMBERSHIP_QUERY_KEYS = [
	NOTIFICATION_CENTER_QUERY_KEY,
	["skills"],
	["get", "/v1/projects"],
	["get", "/v1/agents"],
] as const;

export function getPendingNotificationCount(
	notifications: readonly ProjectInvitationNotification[] | null | undefined,
	accountUnreadCount = 0,
): number {
	return (notifications?.length ?? 0) + accountUnreadCount;
}

export function filterAccountNotifications(
	notifications: readonly AccountNotification[],
	view: NotificationCenterView,
): readonly AccountNotification[] {
	return view === "unread"
		? notifications.filter((notification) => !notification.read)
		: notifications;
}

export function resolveNotificationUrl(
	value: string,
	origin: string,
): { kind: "same-origin" | "external"; url: URL } | null {
	try {
		const base = new URL(origin);
		const url = new URL(value, base);
		if (url.origin === base.origin) return { kind: "same-origin", url };
		return NOTIFICATION_ACTION_ORIGINS.has(url.origin) ? { kind: "external", url } : null;
	} catch {
		return null;
	}
}

export function getNotificationCenterTriggerLabel(count: number): string {
	if (count === 1) return "Notifications, 1 new item";
	if (count > 1) return `Notifications, ${count} new items`;
	return "Notifications";
}

export function getNotificationCenterEmptyCopy(view: NotificationCenterView): {
	title: string;
	description: string;
} {
	return view === "unread"
		? {
				title: "You're all caught up",
				description: "New account updates and project invitations will appear here.",
			}
		: {
				title: "No notifications yet",
				description: "Account updates and project invitations will appear here.",
			};
}

export function getNotificationCenterDescription(): string {
	return "Account activity and project invitations.";
}

// ---------------------------------------------------------------------------
// Pending companion approvals — the second notification source (plan 05-08).
// ---------------------------------------------------------------------------

export type PendingApprovalNotification = Schemas["CompanionApprovalResponse"];

export const PENDING_APPROVALS_QUERY_KEY = ["get", "/v1/companions/approvals"] as const;

/**
 * One source's state, as the bell is allowed to see it.
 *
 * The count derives from SETTLED sources only. A source still loading
 * contributes nothing, so the bell never renders a placeholder count that
 * later corrects itself downward; a source that FAILED also contributes
 * nothing, because on a safety surface a silent zero is better than a number
 * that might be wrong. Both cases are the same answer for different reasons,
 * and both are stated here rather than inferred at the render site.
 */
export type NotificationSourceState<T> =
	| { status: "loading" }
	| { status: "error" }
	| { status: "settled"; items: readonly T[] };

export function settledNotificationItems<T>(
	source: NotificationSourceState<T> | undefined,
): readonly T[] {
	return source?.status === "settled" ? source.items : [];
}

export function getPendingApprovalCount(
	source: NotificationSourceState<PendingApprovalNotification> | undefined,
): number {
	return settledNotificationItems(source).length;
}

/** Follows the existing singular and plural trigger-label convention. */
export function getPendingApprovalTriggerLabel(count: number): string {
	if (count === 1) return "Notifications, 1 Call Waiting";
	if (count > 1) return `Notifications, ${count} Calls Waiting`;
	return "Notifications";
}

/**
 * The merged trigger label across every source.
 *
 * `attention` is the count the bell already shows without approvals: pending
 * invitations plus unread account updates (`getPendingNotificationCount`).
 * Approvals lead because they are the only source with a deadline. With one
 * source contributing, the label is exactly that source's own sentence.
 */
export function getMergedNotificationCenterTriggerLabel(counts: {
	attention: number;
	approvals: number;
}): string {
	if (counts.approvals === 0) return getNotificationCenterTriggerLabel(counts.attention);
	const parts: string[] = [];
	if (counts.approvals === 1) parts.push("1 Call Waiting");
	else parts.push(`${counts.approvals} Calls Waiting`);
	if (counts.attention === 1) parts.push("1 new item");
	else if (counts.attention > 1) parts.push(`${counts.attention} new items`);
	return `Notifications, ${parts.join(", ")}`;
}

/** The count dot on the bell — settled sources only, never a partial merge. */
export function getMergedNotificationCount(counts: {
	attention: number;
	approvals: number;
}): number {
	return counts.attention + counts.approvals;
}

/** A companion summary, reduced to the join this module needs. */
export interface ApprovalCompanionPlacement {
	companion_key: string;
	environment_ids: readonly string[];
}

/**
 * Where a pending-approval entry links to.
 *
 * The approvals response carries no agent identifier — it is tenant-scoped —
 * so the agent comes from the companion summary that names where the companion
 * is deployed. When no placement is known the entry renders WITHOUT a link
 * rather than with a dead one.
 */
export function pendingApprovalEntryHref(
	approval: Pick<PendingApprovalNotification, "companion_key">,
	placements: readonly ApprovalCompanionPlacement[],
): string | null {
	const placement = placements.find(
		(candidate) => candidate.companion_key === approval.companion_key,
	);
	const environmentId = placement?.environment_ids[0];
	if (!environmentId) return null;
	return `/agents/${encodeURIComponent(environmentId)}/companions`;
}

export function getProjectInvitationAccessCopy(): string {
	return "View shared Projects and link them to your Agents. Only the owner can edit.";
}

export function getAcceptedProjectInvitationToastCopy(projectName?: string): {
	title: string;
	description: string;
} {
	return {
		title: projectName ? `Joined ${projectName}` : "Project joined",
		description: "Open the Project to view its resources or link it to an Agent.",
	};
}
