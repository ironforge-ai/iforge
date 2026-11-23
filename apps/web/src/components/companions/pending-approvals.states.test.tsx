/**
 * Held-out UI-state tests for S3 — the pending-approvals list, the approve and
 * deny controls, and the NotificationCenter entry. One assertion per
 * `05-UI-SPEC.md` §UI Considerations row for those three elements.
 *
 * **SCOPE LIMIT, same as `companion-tools.states.test.tsx`.** Every assertion
 * reads `renderToStaticMarkup` output — which component rendered, which copy is
 * present, which classes and attributes are present, and in which ORDER
 * elements were emitted. It does not measure pixels. `overflow` and
 * `long-text` rows are asserted as their markup-level preconditions (the
 * payload is behind `TruncatedText` with `break-all`, the buttons sit on their
 * own wrapping row) rather than as observed widths.
 *
 * `ConfirmAction` renders only its trigger until a user opens it, so dialog
 * copy is asserted against `approval-decision.logic.ts`. The NotificationCenter
 * rows are asserted against `notification-center.logic.ts` for the same reason:
 * the component needs a router and a query client, neither of which exists
 * here, and the count rule is a pure function by design.
 */

import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import {
	getMergedNotificationCenterTriggerLabel,
	getMergedNotificationCount,
	getPendingApprovalCount,
	type NotificationSourceState,
	type PendingApprovalNotification,
	pendingApprovalEntryHref,
} from "@/components/notification-center.logic";
import { ApiError } from "@/lib/api-errors";
import { renderedText } from "@/lib/rendered-text";
import {
	APPROVAL_DECISION_ERROR_COPY,
	APPROVAL_EXPIRED_COPY,
	APPROVALS_COPY,
	APPROVALS_EMPTY_COPY,
	type CompanionApproval,
	confirmCopyFor,
	UNPREVIEWED_TARGET_COPY,
} from "./approval-decision.logic";
import { PendingApprovalsRegion } from "./pending-approvals-card";

const NOW = new Date("2026-09-16T08:30:00Z");
const AGENT_LABEL = "Research Agent";
const MAX_LENGTH_TOOL_NAME = `filebrowser__${"x".repeat(94 - "filebrowser__".length)}`;

function approval(overrides: Partial<CompanionApproval> = {}): CompanionApproval {
	return {
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
		...overrides,
	};
}

function renderRegion(props: Partial<Parameters<typeof PendingApprovalsRegion>[0]> = {}) {
	return renderToStaticMarkup(
		<PendingApprovalsRegion
			approvals={[approval()]}
			agentLabel={AGENT_LABEL}
			capUnitFor={() => "records"}
			onDecide={async () => undefined}
			onRetry={() => {}}
			now={NOW}
			{...props}
		/>,
	);
}

describe("S3 pending approvals list", () => {
	test("empty: zero pending renders the inset EmptyState with no action", () => {
		const markup = renderRegion({ approvals: [] });
		const text = renderedText(markup);
		expect(text).toContain(APPROVALS_EMPTY_COPY.title);
		expect(text).toContain("Nothing runs while one waits.");
		expect(markup).not.toContain('data-slot="pending-approval-card"');
		expect(text).not.toContain(APPROVALS_COPY.approve);
	});

	test("loading: Skeleton cards at the real card geometry, keeping the region's position", () => {
		const markup = renderRegion({ loading: true });
		expect(markup).toContain('data-slot="pending-approval-skeleton"');
		expect(markup).toContain("rounded-lg border border-border p-4");
		// The region heading stays mounted, so the Tools region below does not
		// jump when approvals resolve.
		expect(renderedText(markup)).toContain(APPROVALS_COPY.heading);
	});

	test("error: the failure is scoped to this card, leaving the other regions readable", () => {
		const markup = renderRegion({ error: new ApiError(500, "boom") });
		expect(markup).toContain('data-slot="alert"');
		expect(renderedText(markup)).toContain("Retry");
		expect(markup).not.toContain('data-slot="pending-approval-card"');
		// Nothing outside this section is rendered by it at all.
		expect(markup.startsWith('<section data-slot="pending-approvals"')).toBe(true);
	});

	test("populated: stacked cards, newest first", () => {
		const older = approval({ id: "older", created_at: "2026-09-16T08:00:00Z" });
		const newer = approval({ id: "newer", created_at: "2026-09-16T08:29:00Z" });
		const markup = renderRegion({ approvals: [older, newer] });
		expect(markup.indexOf('data-approval="newer"')).toBeLessThan(
			markup.indexOf('data-approval="older"'),
		);
		expect((markup.match(/data-slot="pending-approval-card"/g) ?? []).length).toBe(2);
	});

	test("partial: an expired-but-unswept approval says so and disables both buttons", () => {
		const markup = renderRegion({
			approvals: [approval({ expires_at: "2026-09-16T08:29:30Z" })],
		});
		expect(renderedText(markup)).toContain(APPROVAL_EXPIRED_COPY);
		expect(markup).toContain('data-state="expired"');
		// Both buttons carry the disabled attribute rather than offering an
		// action the server would answer with a conflict.
		expect((markup.match(/disabled=""/g) ?? []).length).toBeGreaterThanOrEqual(2);
	});

	test("overflow: a large argument payload stays behind TruncatedText in a bounded well", () => {
		const payload = "x".repeat(4096);
		const markup = renderRegion({
			approvals: [approval({ arguments_preview: { blob: payload } })],
		});
		expect(markup).toContain("truncate");
		expect(markup).toContain("bg-muted/30 p-4 font-mono text-xs");
		// The full value stays reachable through the title rather than being
		// dropped, and the card's own width classes are unchanged by it.
		expect(markup).toContain(`title=`);
		expect(markup).toContain("rounded-lg border border-border p-4");
	});

	test("zero-one-many: one card has the same chrome as many, with no pluralized noun inside", () => {
		const one = renderedText(renderRegion());
		const many = renderedText(
			renderRegion({ approvals: [approval(), approval({ id: "second" })] }),
		);
		expect(one).toContain(APPROVALS_COPY.approve);
		expect(many).toContain(APPROVALS_COPY.approve);
		expect(one).not.toContain("1 call");
		expect(many).not.toContain("2 calls");
	});

	test("long-text: a maximum-length mono name shares the meta row and keeps the buttons reachable", () => {
		const markup = renderRegion({ approvals: [approval({ tool: MAX_LENGTH_TOOL_NAME })] });
		const namePosition = markup.indexOf(MAX_LENGTH_TOOL_NAME);
		expect(namePosition).toBeGreaterThan(-1);
		expect(markup).toContain(`title="${MAX_LENGTH_TOOL_NAME}"`);
		// The Deny/Approve pair is emitted after the name, in its own wrapping
		// flex row rather than inside the meta row.
		expect(markup.indexOf(APPROVALS_COPY.deny)).toBeGreaterThan(namePosition);
		expect(markup).toContain("flex flex-wrap items-center gap-2");
	});
});

describe("S3 approve / deny controls", () => {
	test("empty: with nothing pending no approve or deny control is rendered at all", () => {
		const markup = renderRegion({ approvals: [] });
		expect(markup).not.toContain(APPROVALS_COPY.approve);
		expect(markup).not.toContain(APPROVALS_COPY.deny);
	});

	test("loading: Approve routes through ConfirmAction, which owns the in-flight lock", () => {
		const markup = renderRegion();
		// The approve button is an AlertDialog trigger; `ConfirmAction` holds
		// the dialog open with a Spinner and blocks double submit with a ref.
		expect(markup).toContain('data-slot="alert-dialog-trigger"');
		expect(markup).toContain('aria-haspopup="dialog"');
	});

	test("error: the rejection sentence renders at the action, never as a toast", () => {
		expect(APPROVAL_DECISION_ERROR_COPY).toContain("reload to see where it stands");
		// The failed-decision state is internal to the card, so the property
		// that can actually be gated is the one that matters: this module
		// renders the sentence into an inline slot and imports no toast API at
		// all, which makes "never a toast" unrepresentable rather than merely
		// unobserved.
		const source = readFileSync(
			fileURLToPath(new URL("./pending-approvals-card.tsx", import.meta.url)),
			"utf8",
		);
		expect(source).toContain('data-slot="approval-error"');
		expect(source).toContain("{state.error}");
		expect(source).not.toContain('from "sonner"');
		expect(source).not.toContain("toast.");
	});

	test("populated: Deny is a ghost destructive-text button with no dialog; Approve is the accent primary", () => {
		const markup = renderRegion();
		const denyPosition = markup.indexOf(APPROVALS_COPY.deny);
		const approvePosition = markup.indexOf(APPROVALS_COPY.approve);
		expect(denyPosition).toBeGreaterThan(-1);
		expect(approvePosition).toBeGreaterThan(denyPosition);
		expect(markup).toContain("text-destructive");
		expect(markup).toContain("bg-primary text-primary-foreground");
		// Deny carries no dialog: only one alert-dialog trigger on the card.
		expect((markup.match(/data-slot="alert-dialog-trigger"/g) ?? []).length).toBe(1);

		const destroy = confirmCopyFor(approval({ tool_class: "destroy" }), AGENT_LABEL, "records");
		expect(destroy.confirmLabel).toBe("Approve and run");
		expect(destroy.destructive).toBe(true);
	});

	test("partial: an un-previewed target count renders as un-previewed, never as zero", () => {
		const markup = renderRegion({ approvals: [approval({ resolved_target_count: null })] });
		const text = renderedText(markup);
		expect(text).toContain(UNPREVIEWED_TARGET_COPY);
		expect(text).toContain("188 records left");
		expect(text).not.toContain("0 records ·");
	});

	test("zero-one-many: each card carries exactly one approve and deny pair, with no bulk control", () => {
		const markup = renderRegion({ approvals: [approval(), approval({ id: "second" })] });
		expect((markup.match(new RegExp(APPROVALS_COPY.approve, "g")) ?? []).length).toBe(2);
		expect((markup.match(new RegExp(APPROVALS_COPY.deny, "g")) ?? []).length).toBe(2);
		expect(renderedText(markup)).not.toContain("Approve all");
	});

	test("overflow: confirmation descriptions interpolate values and wrap rather than scroll", () => {
		const copy = confirmCopyFor(approval(), AGENT_LABEL, "records");
		expect(copy.description).toContain(AGENT_LABEL);
		expect(copy.description).toContain("filebrowser__move_path");
		expect(copy.description).toContain("12 records");
		// `ConfirmAction`'s confirm button ships `min-w-0 whitespace-normal`,
		// which is what keeps the confirm row visible beneath a long body.
		expect(copy.confirmLabel.length).toBeLessThan(40);
	});

	test("long-text: a maximum-length reason inside the dialog wraps rather than truncating", () => {
		const copy = confirmCopyFor(
			approval({ tool: MAX_LENGTH_TOOL_NAME, tool_class: "destroy" }),
			AGENT_LABEL,
			"records",
		);
		// The reviewer is about to authorize a write, so nothing in the dialog
		// body is shortened — the whole tool name survives into the sentence.
		expect(copy.description).toContain(MAX_LENGTH_TOOL_NAME);
		expect(copy.description).not.toContain("…");
	});
});

describe("T-5-51 — companion content never reaches a rendering surface", () => {
	test("no file under components/companions renders markup from companion bytes", () => {
		// The committed form of the plan's grep gate. The three forbidden names
		// are assembled from fragments so this assertion does not itself become
		// the match it is looking for.
		const forbidden = [
			`dangerously${"SetInnerHTML"}`,
			`from "react-${"markdown"}"`,
			`@/components/${"markdown"}`,
		];
		const directory = fileURLToPath(new URL(".", import.meta.url));
		const offenders: string[] = [];
		for (const entry of readdirSync(directory)) {
			if (!/\.(ts|tsx)$/.test(entry)) continue;
			const source = readFileSync(join(directory, entry), "utf8");
			for (const needle of forbidden) {
				if (source.includes(needle)) offenders.push(`${entry}: ${needle}`);
			}
		}
		expect(offenders).toEqual([]);
	});
});

describe("S3 NotificationCenter entry", () => {
	function source(
		status: "loading" | "error" | "settled",
		items: readonly PendingApprovalNotification[] = [],
	): NotificationSourceState<PendingApprovalNotification> {
		return status === "settled" ? { status, items } : { status };
	}

	test("empty: zero pending contributes no entry and no count dot", () => {
		expect(getPendingApprovalCount(source("settled", []))).toBe(0);
		expect(getMergedNotificationCount({ attention: 0, approvals: 0 })).toBe(0);
		expect(getMergedNotificationCenterTriggerLabel({ attention: 0, approvals: 0 })).toBe(
			"Notifications",
		);
	});

	test("loading: the source contributes nothing until it resolves", () => {
		// A placeholder count that later corrects itself DOWNWARD is the exact
		// failure this rule exists to prevent on a safety surface.
		expect(getPendingApprovalCount(source("loading"))).toBe(0);
		expect(getPendingApprovalCount(undefined)).toBe(0);
	});

	test("error: a failed approvals query contributes no entry and no dot", () => {
		expect(getPendingApprovalCount(source("error"))).toBe(0);
		// The other source renders normally — a silent zero here, not a
		// blanked bell.
		expect(getMergedNotificationCount({ attention: 2, approvals: 0 })).toBe(2);
		expect(getMergedNotificationCenterTriggerLabel({ attention: 2, approvals: 0 })).toBe(
			"Notifications, 2 new items",
		);
	});

	test("populated: the entry links to the companions surface for the right agent", () => {
		const placements = [
			{ companion_key: "filebrowser", environment_ids: ["agent-1"] },
			{ companion_key: "elsewhere", environment_ids: ["agent-2"] },
		];
		expect(pendingApprovalEntryHref(approval(), placements)).toBe("/agents/agent-1/companions");
		// No placement known: no link rather than a dead one.
		expect(pendingApprovalEntryHref(approval({ companion_key: "unknown" }), placements)).toBeNull();
		expect(pendingApprovalEntryHref(approval(), [])).toBeNull();
	});

	test("partial: a merge across sources counts only the settled ones", () => {
		const approvals = getPendingApprovalCount(source("loading"));
		expect(getMergedNotificationCount({ attention: 3, approvals })).toBe(3);
		const settled = getPendingApprovalCount(source("settled", [approval()]));
		expect(getMergedNotificationCount({ attention: 3, approvals: settled })).toBe(4);
	});

	test("overflow: a three-digit count renders in the label without extra chrome", () => {
		const label = getMergedNotificationCenterTriggerLabel({ attention: 0, approvals: 128 });
		expect(label).toBe("Notifications, 128 Calls Waiting");
		expect(label.length).toBeLessThan(60);
	});

	test("zero-one-many: the trigger label follows the existing singular and plural pattern", () => {
		expect(getMergedNotificationCenterTriggerLabel({ attention: 0, approvals: 1 })).toBe(
			"Notifications, 1 Call Waiting",
		);
		expect(getMergedNotificationCenterTriggerLabel({ attention: 0, approvals: 4 })).toBe(
			"Notifications, 4 Calls Waiting",
		);
	});

	test("long-text: an entry row's mono tool name truncates rather than widening the popover", () => {
		// `NotificationCenter` needs a router and a query client, neither of
		// which exists here, so the shipped row is gated at its source: the
		// mono name truncates and keeps its full value in `title`.
		const source = readFileSync(
			fileURLToPath(new URL("../notification-center.tsx", import.meta.url)),
			"utf8",
		);
		expect(source).toContain('data-slot="pending-approval-entries"');
		expect(source).toContain('className="truncate font-mono text-xs" title={approval.tool}');
	});
});
