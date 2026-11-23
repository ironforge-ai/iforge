import { describe, expect, test } from "bun:test";
import { DESTINATIONS } from "@clawdi/shared";
import {
	type AccountNotification,
	filterAccountNotifications,
	getAcceptedProjectInvitationToastCopy,
	getMergedNotificationCenterTriggerLabel,
	getMergedNotificationCount,
	getNotificationCenterEmptyCopy,
	getNotificationCenterTriggerLabel,
	getPendingApprovalCount,
	getPendingApprovalTriggerLabel,
	getPendingNotificationCount,
	NOTIFICATION_CENTER_MEMBERSHIP_QUERY_KEYS,
	type NotificationSourceState,
	PENDING_APPROVALS_QUERY_KEY,
	type PendingApprovalNotification,
	type ProjectInvitationNotification,
	pendingApprovalEntryHref,
	resolveNotificationUrl,
	settledNotificationItems,
} from "./notification-center.logic";

const walletNotification = {
	id: "wallet-low-balance",
	title: "Your wallet balance is down to $1.25",
	description: "Top up before paid requests begin to fail.",
	category: "Wallet",
	createdAt: new Date("2026-05-15T08:00:00Z"),
	read: false,
	actionLabel: "Top up",
	actionUrl: "https://cloud.clawdi.ai/?settings=billing-wallet#billing",
	severity: "warning",
} satisfies AccountNotification;

const invitation = {
	id: "inv_1",
	project_id: "proj_1",
	project_name: "Shared Workspace",
	project_kind: "workspace",
	owner_display: "Ada Lovelace",
	owner_handle: "ada",
	invitee_email: "viewer@example.com",
	invited_by_user_id: "user_1",
	invited_by_display: "Ada Lovelace",
	created_at: "2026-05-15T08:00:00Z",
} satisfies ProjectInvitationNotification;

describe("notification center logic", () => {
	test("counts pending notifications and names the compact trigger", () => {
		expect(getPendingNotificationCount(undefined)).toBe(0);
		expect(getPendingNotificationCount([])).toBe(0);
		expect(getPendingNotificationCount([invitation])).toBe(1);
		expect(getPendingNotificationCount([invitation], 1)).toBe(2);
		expect(getPendingNotificationCount([invitation, { ...invitation, id: "inv_2" }])).toBe(2);

		expect(getNotificationCenterTriggerLabel(0)).toBe("Notifications");
		expect(getNotificationCenterTriggerLabel(1)).toBe("Notifications, 1 new item");
		expect(getNotificationCenterTriggerLabel(2)).toBe("Notifications, 2 new items");
	});

	test("filters history into all and unread views", () => {
		const readNotification = { ...walletNotification, id: "read", read: true };
		expect(filterAccountNotifications([walletNotification, readNotification], "all")).toHaveLength(
			2,
		);
		expect(filterAccountNotifications([walletNotification, readNotification], "unread")).toEqual([
			walletNotification,
		]);

		const allEmpty = getNotificationCenterEmptyCopy("all");
		expect(allEmpty.title).toBe("No notifications yet");
		const unreadEmpty = getNotificationCenterEmptyCopy("unread");
		expect(unreadEmpty.title).toBe("You're all caught up");
	});

	test("accepts same-origin and HTTPS notification actions only", () => {
		const relative = resolveNotificationUrl("/deploy", DESTINATIONS.dashboardOrigin);
		expect(relative?.kind).toBe("same-origin");
		expect(relative?.url.href).toBe(`${DESTINATIONS.dashboardOrigin}/deploy`);
		expect(
			resolveNotificationUrl(`${DESTINATIONS.wwwOrigin}/dashboard`, DESTINATIONS.dashboardOrigin)
				?.kind,
		).toBe("external");
		expect(resolveNotificationUrl("https://example.com", DESTINATIONS.dashboardOrigin)).toBeNull();
		expect(resolveNotificationUrl("http://example.com", DESTINATIONS.dashboardOrigin)).toBeNull();
		expect(resolveNotificationUrl("javascript:alert(1)", DESTINATIONS.dashboardOrigin)).toBeNull();
	});

	test("includes the accepted Project name with a fallback", () => {
		const accepted = getAcceptedProjectInvitationToastCopy("Shared Workspace");
		expect(accepted.title).toBe("Joined Shared Workspace");
		expect(getAcceptedProjectInvitationToastCopy().title).toBe("Project joined");
	});

	test("keeps the invitation trigger label unchanged as a second source joins", () => {
		// Plan 05-08 added pending companion approvals beside invitations. The
		// invitation-only sentence is untouched; the merged label composes.
		expect(getNotificationCenterTriggerLabel(1)).toBe("Notifications, 1 new item");
		expect(getMergedNotificationCenterTriggerLabel({ attention: 1, approvals: 0 })).toBe(
			getNotificationCenterTriggerLabel(1),
		);
		expect(getMergedNotificationCenterTriggerLabel({ attention: 2, approvals: 0 })).toBe(
			getNotificationCenterTriggerLabel(2),
		);
	});

	test("declares canonical OpenAPI membership cache keys", () => {
		expect(NOTIFICATION_CENTER_MEMBERSHIP_QUERY_KEYS).toContainEqual(["get", "/v1/projects"]);
		expect(NOTIFICATION_CENTER_MEMBERSHIP_QUERY_KEYS).toContainEqual(["get", "/v1/agents"]);
		expect(NOTIFICATION_CENTER_MEMBERSHIP_QUERY_KEYS).not.toContainEqual(["projects"]);
		expect(NOTIFICATION_CENTER_MEMBERSHIP_QUERY_KEYS).not.toContainEqual(["agents"]);
	});
});

const approval = {
	id: "b1d0c4f6-0000-4000-8000-000000000001",
	tool: "filebrowser__move_path",
	tool_class: "mutate",
	companion_key: "filebrowser",
	verb: "move_path",
	arguments_preview: { from: "/a", to: "/b" },
	resolved_target_count: 12,
	remaining_budget: 188,
	status: "issued",
	expires_at: "2026-09-16T08:35:00Z",
	decided_at: null,
	created_at: "2026-09-16T08:29:00Z",
} satisfies PendingApprovalNotification;

function approvalSource(
	status: "loading" | "error" | "settled",
	items: readonly PendingApprovalNotification[] = [],
): NotificationSourceState<PendingApprovalNotification> {
	return status === "settled" ? { status, items } : { status };
}

describe("pending companion approvals as a notification source", () => {
	test("the count derives from SETTLED sources only", () => {
		// A source still loading must contribute nothing, so the bell never
		// renders a placeholder count that later corrects itself downward.
		expect(getPendingApprovalCount(approvalSource("loading"))).toBe(0);
		expect(getPendingApprovalCount(undefined)).toBe(0);
		expect(settledNotificationItems(approvalSource("loading"))).toEqual([]);

		expect(getPendingApprovalCount(approvalSource("settled", []))).toBe(0);
		expect(getPendingApprovalCount(approvalSource("settled", [approval]))).toBe(1);
		expect(
			getPendingApprovalCount(approvalSource("settled", [approval, { ...approval, id: "second" }])),
		).toBe(2);
	});

	test("a failed approvals query contributes no entry and no dot", () => {
		// On a safety surface a silent zero is preferred to a false count.
		expect(getPendingApprovalCount(approvalSource("error"))).toBe(0);
		expect(settledNotificationItems(approvalSource("error"))).toEqual([]);
		// The other source still renders normally.
		expect(getMergedNotificationCount({ attention: 3, approvals: 0 })).toBe(3);
	});

	test("the trigger label follows the existing singular and plural pattern", () => {
		expect(getPendingApprovalTriggerLabel(0)).toBe("Notifications");
		expect(getPendingApprovalTriggerLabel(1)).toBe("Notifications, 1 Call Waiting");
		expect(getPendingApprovalTriggerLabel(5)).toBe("Notifications, 5 Calls Waiting");
		expect(getMergedNotificationCenterTriggerLabel({ attention: 0, approvals: 1 })).toBe(
			getPendingApprovalTriggerLabel(1),
		);
	});

	test("approvals lead the merged label, because they are the source with a deadline", () => {
		expect(getMergedNotificationCenterTriggerLabel({ attention: 2, approvals: 1 })).toBe(
			"Notifications, 1 Call Waiting, 2 new items",
		);
		expect(getMergedNotificationCount({ attention: 2, approvals: 1 })).toBe(3);
	});

	test("an entry links through the companion's deployment, or not at all", () => {
		const placements = [
			{ companion_key: "filebrowser", environment_ids: ["agent-1"] },
			{ companion_key: "elsewhere", environment_ids: ["agent-2"] },
		];
		expect(pendingApprovalEntryHref(approval, placements)).toBe("/agents/agent-1/companions");
		// The approvals response carries no agent id; with no placement known
		// the entry renders WITHOUT a link rather than with a dead one.
		expect(pendingApprovalEntryHref(approval, [])).toBeNull();
		expect(pendingApprovalEntryHref({ companion_key: "unknown" }, placements)).toBeNull();
		expect(
			pendingApprovalEntryHref(approval, [{ companion_key: "filebrowser", environment_ids: [] }]),
		).toBeNull();
	});

	test("the approvals cache key is the canonical OpenAPI pair", () => {
		expect(PENDING_APPROVALS_QUERY_KEY).toEqual(["get", "/v1/companions/approvals"]);
	});
});
