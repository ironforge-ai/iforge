/**
 * Held-out UI-state tests for S1 — the tool catalog, the destroy-tools group
 * and the per-tool policy Select. One assertion per `05-UI-SPEC.md`
 * §UI Considerations row for those three elements.
 *
 * **SCOPE LIMIT, stated so a later reader does not mistake this for a layout
 * measurement.** There is no DOM library and no viewport harness in this
 * workspace, so every assertion here reads `renderToStaticMarkup` output:
 * which component rendered, which copy is present, which classes and `title`
 * attributes are present, and in which ORDER elements were emitted. It does
 * not measure pixels, and it cannot observe a rendered width. The `overflow`
 * and `long-text` rows are therefore asserted as their markup-level
 * PRECONDITIONS — the truncation affordance is present, the full value is in a
 * `title`, the trailing controls are still emitted after the long value, and
 * the container carries the wrapping/min-width classes that make the layout
 * claim possible — never as a measured result.
 *
 * Two further shapes are invisible to static markup and are asserted through
 * the logic module instead, which is the whole reason the copy lives there:
 *
 * - `ConfirmAction` renders only its TRIGGER until a user opens it; the dialog
 *   body is portaled. Dialog copy is asserted against `tool-policy.logic.ts`.
 * - `Select` renders only its trigger and a hidden input; the option list is
 *   portaled. Option labels are asserted against `policyOptions`.
 */

import { describe, expect, test } from "bun:test";
import type { paths } from "@clawdi/shared/api";
import createClient from "openapi-fetch";
import { renderToStaticMarkup } from "react-dom/server";
import { ApiError } from "@/lib/api-errors";
import { renderedText } from "@/lib/rendered-text";
import { CompanionToolRow, CompanionToolRowSkeleton } from "./companion-tool-row";
import { CompanionToolsRegion } from "./companion-tools-surface";
import { DestroyToolsGroup } from "./destroy-tools-group";
import {
	allowWithoutAskingCopy,
	blockToolsCopy,
	type CompanionSummary,
	type CompanionToolGroup,
	type CompanionToolSettings,
	DESTROY_ENABLEMENT_ERROR_COPY,
	DESTROY_GROUP_EMPTY_COPY,
	DESTROY_GROUP_TITLE,
	enableDestroyToolsCopy,
	groupBlockRequest,
	initialPolicyState,
	NO_COMPANIONS_EMPTY_COPY,
	nextPolicyState,
	POLICY_CHANGE_ERROR_COPY,
	policyOptions,
	runGroupBlock,
} from "./tool-policy.logic";

const AGENT_ID = "6c3b1a2e-0000-4000-8000-000000000001";
const AGENT_LABEL = "Research Agent";

/** The registry bounds a composite tool name at 94 characters. */
const MAX_LENGTH_TOOL_NAME = `filebrowser__${"x".repeat(94 - "filebrowser__".length)}`;

function tool(overrides: Partial<CompanionToolSettings> = {}): CompanionToolSettings {
	return {
		tool: "filebrowser__list_dir",
		companion_key: "filebrowser",
		verb: "list_dir",
		tool_class: "read",
		cap_unit: "calls",
		dry_run_available: false,
		stored_mode: null,
		resolved_mode: "allow",
		resolved_reason: "class_default",
		destroy_enabled: false,
		exposed_to_agent: true,
		...overrides,
	};
}

function companion(overrides: Partial<CompanionSummary> = {}): CompanionSummary {
	return {
		companion_key: "filebrowser",
		environment_ids: [AGENT_ID],
		has_destroy_verbs: true,
		destroy_enabled: false,
		visible_verb_count: 5,
		blocked: false,
		...overrides,
	};
}

const POPULATED_TOOLS: readonly CompanionToolSettings[] = [
	tool(),
	tool({ tool: "filebrowser__read_file", verb: "read_file", cap_unit: "bytes" }),
	tool({
		tool: "filebrowser__create_dir",
		verb: "create_dir",
		tool_class: "create",
		resolved_mode: "ask",
		dry_run_available: true,
	}),
	tool({
		tool: "filebrowser__write_file",
		verb: "write_file",
		tool_class: "create",
		cap_unit: "bytes",
		resolved_mode: "ask",
		dry_run_available: true,
	}),
	tool({
		tool: "filebrowser__move_path",
		verb: "move_path",
		tool_class: "mutate",
		cap_unit: "records",
		resolved_mode: "ask",
		dry_run_available: true,
	}),
];

const DESTROY_TOOL = tool({
	tool: "filebrowser__delete_path",
	verb: "delete_path",
	tool_class: "destroy",
	cap_unit: "records",
	resolved_mode: "deny",
	resolved_reason: "refused_destroy_disabled",
	dry_run_available: true,
	exposed_to_agent: false,
});

function group(overrides: Partial<CompanionToolGroup> = {}): CompanionToolGroup {
	return {
		companion: companion(),
		tools: POPULATED_TOOLS,
		destroyTools: [DESTROY_TOOL],
		...overrides,
	};
}

const REGION_HANDLERS = {
	agentLabel: AGENT_LABEL,
	policyState: () => initialPolicyState("ask"),
	onModeChange: async () => undefined,
	onSetDestroyEnabled: async () => undefined,
	onSetGroupBlocked: async () => undefined,
	onRetry: () => {},
};

function renderRegion(props: Partial<Parameters<typeof CompanionToolsRegion>[0]> = {}) {
	return renderToStaticMarkup(
		<CompanionToolsRegion
			loading={false}
			error={null}
			groups={[group()]}
			{...REGION_HANDLERS}
			{...props}
		/>,
	);
}

describe("S1 tool catalog", () => {
	test("empty: zero deployed companions renders the page EmptyState with the deploy action", () => {
		const markup = renderRegion({ groups: [] });
		const text = renderedText(markup);
		expect(text).toContain(NO_COMPANIONS_EMPTY_COPY.title);
		expect(text).toContain("classified, capped, and policy-gated");
		expect(text).toContain(NO_COMPANIONS_EMPTY_COPY.action);
		expect(markup).toContain('href="/deploy"');
	});

	test("loading: Skeleton rows at the real row geometry, never a bare page Spinner", () => {
		const markup = renderRegion({ loading: true });
		expect(markup).toContain('data-slot="companion-tool-row-skeleton"');
		expect(markup).not.toContain('data-slot="spinner"');
		// The skeleton row carries the same four slots as a real row: chip,
		// mono name, meta, and the trailing policy control.
		const skeleton = renderToStaticMarkup(<CompanionToolRowSkeleton />);
		expect(skeleton.match(/data-slot="skeleton"/g)?.length).toBe(5);
	});

	test("error: query failure renders ApiErrorPanel with a retry, and no custom layout", () => {
		const markup = renderRegion({ error: new ApiError(500, "boom") });
		expect(markup).toContain('data-slot="alert"');
		expect(renderedText(markup)).toContain("Retry");
		expect(markup).not.toContain('data-slot="companion-tool-row"');
	});

	test("populated: verbs grouped under SectionLabel, whole list rendered without paging", () => {
		const markup = renderRegion();
		for (const row of POPULATED_TOOLS) {
			expect(markup).toContain(`data-tool="${row.tool}"`);
		}
		expect(renderedText(markup)).toContain("filebrowser");
		expect(renderedText(markup)).not.toContain("Next page");
	});

	test("partial: a deployed-but-unreconciled companion renders skeleton rows, never zero verbs", () => {
		const markup = renderRegion({
			groups: [
				group({ tools: [], destroyTools: [], companion: companion({ visible_verb_count: 5 }) }),
			],
		});
		expect(markup.match(/data-slot="companion-tool-row-skeleton"/g)?.length).toBe(5);
		// The group header is still there — the companion is not hidden.
		expect(renderedText(markup)).toContain("filebrowser");
	});

	test("overflow: a maximum-length mono name keeps the meta and the Select on the row", () => {
		const markup = renderToStaticMarkup(
			<CompanionToolRow
				row={tool({ tool: MAX_LENGTH_TOOL_NAME })}
				agentLabel={AGENT_LABEL}
				state={initialPolicyState("allow")}
				onModeChange={async () => undefined}
			/>,
		);
		const namePosition = markup.indexOf(MAX_LENGTH_TOOL_NAME);
		expect(namePosition).toBeGreaterThan(-1);
		// The trailing controls are still EMITTED after the long name, and the
		// name's own container is min-width-0 + truncate so it yields rather
		// than pushing them out. Order and classes are what static markup can
		// prove; the rendered width is not.
		expect(markup.indexOf("Debits calls")).toBeGreaterThan(namePosition);
		expect(markup.indexOf('data-slot="select-trigger"')).toBeGreaterThan(namePosition);
		expect(markup).toContain("min-w-0 max-w-full font-mono text-xs");
	});

	test("zero-one-many: the count slot is a bare integer with no pluralized noun", () => {
		const one = renderedText(
			renderRegion({ groups: [group({ companion: companion({ visible_verb_count: 1 }) })] }),
		);
		const many = renderedText(
			renderRegion({ groups: [group({ companion: companion({ visible_verb_count: 6 }) })] }),
		);
		expect(one).toContain("filebrowser 1");
		expect(many).toContain("filebrowser 6");
		expect(one).not.toContain("1 tool");
		expect(many).not.toContain("6 tools");
	});

	test("long-text: a long tool name truncates with its full value in a title attribute", () => {
		const markup = renderToStaticMarkup(
			<CompanionToolRow
				row={tool({ tool: MAX_LENGTH_TOOL_NAME, cap_unit: "records" })}
				agentLabel={AGENT_LABEL}
				state={initialPolicyState("allow")}
				onModeChange={async () => undefined}
			/>,
		);
		expect(markup).toContain(`title="${MAX_LENGTH_TOOL_NAME}"`);
		expect(markup).toContain("truncate");
	});
});

describe("S1 destroy tools group", () => {
	function renderDestroy(overrides: Partial<Parameters<typeof DestroyToolsGroup>[0]> = {}) {
		return renderToStaticMarkup(
			<DestroyToolsGroup
				companion={companion()}
				tools={[DESTROY_TOOL]}
				agentLabel={AGENT_LABEL}
				policyState={() => initialPolicyState("ask")}
				onModeChange={async () => undefined}
				onSetEnabled={async () => undefined}
				{...overrides}
			/>,
		);
	}

	test("empty: while the Switch is off the body is the inset EmptyState", () => {
		const text = renderedText(renderDestroy());
		expect(text).toContain(DESTROY_GROUP_EMPTY_COPY.title);
		expect(text).toContain("cannot see them and cannot call them by name");
		// Nothing about the destroy verb is rendered as a row while it is off.
		expect(renderDestroy()).not.toContain(`data-tool="${DESTROY_TOOL.tool}"`);
	});

	test("loading: the Switch is the dialog trigger, so it cannot move before the promise settles", () => {
		const markup = renderDestroy();
		// Off position: the Switch IS the AlertDialog trigger and carries no
		// change handler, so the confirmation holds the position rather than
		// the control flipping and being rolled back.
		expect(markup).toContain('role="switch"');
		expect(markup).toContain('aria-haspopup="dialog"');
		expect(markup).toContain('aria-checked="false"');
	});

	test("error: an enablement rejection renders the spec sentence and the Switch stays off", () => {
		const markup = renderDestroy({ enablementError: DESTROY_ENABLEMENT_ERROR_COPY });
		expect(renderedText(markup)).toContain(DESTROY_ENABLEMENT_ERROR_COPY);
		expect(markup).toContain('data-destroy-enabled="false"');
		expect(markup).toContain('aria-checked="false"');
		// `ConfirmAction` puts this same sentence inline in the still-open
		// dialog, because `normalizeApiError` returns a plain Error's message.
		expect(DESTROY_ENABLEMENT_ERROR_COPY).not.toContain("!");
	});

	test("populated: destroy verbs sit in their own titled group with the Switch in the header", () => {
		const markup = renderDestroy({ companion: companion({ destroy_enabled: true }) });
		const text = renderedText(markup);
		expect(text).toContain(DESTROY_GROUP_TITLE);
		expect(markup).toContain(`data-tool="${DESTROY_TOOL.tool}"`);
		expect(markup).toContain('data-destroy-enabled="true"');
		expect(markup.indexOf(DESTROY_GROUP_TITLE.toUpperCase())).toBe(-1);
	});

	test("partial: enablement is all-or-nothing — no mixed enabled and disabled group exists", () => {
		const both = [
			DESTROY_TOOL,
			tool({ tool: "filebrowser__purge", verb: "purge", tool_class: "destroy" }),
		];
		const on = renderDestroy({ companion: companion({ destroy_enabled: true }), tools: both });
		const off = renderDestroy({ tools: both });
		for (const row of both) {
			expect(on).toContain(`data-tool="${row.tool}"`);
			expect(off).not.toContain(`data-tool="${row.tool}"`);
		}
	});

	test("overflow: the header carries both the title and the Switch, neither fixed-width", () => {
		const markup = renderDestroy();
		const titlePosition = markup.indexOf(DESTROY_GROUP_TITLE);
		expect(titlePosition).toBeGreaterThan(-1);
		expect(markup.indexOf('role="switch"')).toBeGreaterThan(titlePosition);
		expect(markup).toContain("flex items-center justify-between gap-2");
	});

	test("zero-one-many: a companion with no destroy verb renders no group at all", () => {
		const markup = renderDestroy({ companion: companion({ has_destroy_verbs: false }), tools: [] });
		expect(markup).toBe("");
	});

	test("long-text: a long companion name survives whole in the confirmation description", () => {
		const longName = "a".repeat(31);
		const copy = enableDestroyToolsCopy(longName, AGENT_LABEL);
		expect(copy.description).toContain(longName);
		// `ConfirmAction`'s confirm button ships `min-w-0 whitespace-normal`,
		// which is what lets the description wrap instead of clipping it. The
		// dialog body is portaled, so the copy is the half this test can read.
		expect(copy.confirmLabel).toBe("Enable destroy tools");
	});
});

describe("S1 per-tool policy Select", () => {
	function renderRow(row: CompanionToolSettings, state = initialPolicyState("ask")) {
		return renderToStaticMarkup(
			<CompanionToolRow
				row={row}
				agentLabel={AGENT_LABEL}
				state={state}
				onModeChange={async () => undefined}
			/>,
		);
	}

	test("empty: every row carries a Select with a resolved value — there is no placeholder", () => {
		const markup = renderRow(
			tool({ stored_mode: null, resolved_mode: "ask", tool_class: "create" }),
		);
		expect(markup).toContain('data-slot="select-trigger"');
		// `data-placeholder` as an ATTRIBUTE is base-ui's unset marker; the
		// same token appears in the trigger's class list as a Tailwind variant,
		// which is why this asserts the attribute form rather than the token.
		expect(markup).not.toContain('data-placeholder=""');
		expect(markup).toContain('value="ask"');
	});

	test("loading: the Select is disabled with a Spinner while a change is in flight", () => {
		const markup = renderRow(tool({ tool_class: "mutate", resolved_mode: "ask" }), {
			...initialPolicyState("ask"),
			pending: true,
			requestedMode: "allow",
		});
		expect(markup).toContain('data-slot="spinner"');
		expect(markup).toContain("disabled");
	});

	test("error: a rejected change renders the spec sentence at the action", () => {
		const failed = nextPolicyState(
			nextPolicyState(initialPolicyState("ask"), { type: "request", mode: "allow" }),
			{ type: "failed" },
		);
		const row = tool({ tool_class: "mutate", stored_mode: "ask", resolved_mode: "ask" });
		const markup = renderRow(row, failed);
		expect(renderedText(markup)).toContain(POLICY_CHANGE_ERROR_COPY);
		// The Select never moved, so "reverts to its prior value" is the
		// absence of a change rather than an undo.
		expect(markup).toContain('value="ask"');
	});

	test("populated: three values, Allow marked warning and Deny neutral, mutate Allow confirms", () => {
		expect(policyOptions.map((option) => option.label)).toEqual(["Allow", "Ask", "Deny"]);
		expect(policyOptions.find((option) => option.value === "allow")?.tone).toBe("warning");
		expect(policyOptions.find((option) => option.value === "deny")?.tone).toBe("neutral");
		const markup = renderRow(
			tool({ tool_class: "mutate", stored_mode: "allow", resolved_mode: "allow" }),
		);
		// The chip for what is in force renders with the weakened-state tone.
		expect(markup).toContain('data-status="warning"');
		expect(renderedText(markup)).toContain("No approval");
		expect(allowWithoutAskingCopy("filebrowser__move_path", AGENT_LABEL).title).toBe(
			"Allow without asking",
		);
	});

	test("partial: an unclassified verb renders its Select disabled rather than defaulting to Allow", () => {
		const markup = renderRow(
			tool({
				tool_class: "teleport",
				resolved_mode: "ask",
				resolved_reason: "unclassified_defaults_to_ask",
			}),
		);
		expect(markup).toContain("disabled");
		expect(markup).toContain('value="ask"');
		expect(renderedText(markup)).toContain("has not classified this tool");
	});

	test("zero-one-many: policy is set one tool at a time — there is no bulk control", () => {
		const markup = renderRegion();
		expect(markup).not.toContain('type="checkbox" name="select-row"');
		expect(renderedText(markup)).not.toContain("Select all");
		const triggers = markup.match(/data-slot="select-trigger"/g) ?? [];
		expect(triggers.length).toBe(POPULATED_TOOLS.length);
	});

	test("long-text: option labels are the fixed three words, bounded by design", () => {
		for (const option of policyOptions) {
			expect(option.label.length).toBeLessThanOrEqual(5);
		}
		expect(policyOptions).toHaveLength(3);
	});

	test("overflow: the Select and the row menu stay after a maximum-length mono name", () => {
		const markup = renderRow(tool({ tool: MAX_LENGTH_TOOL_NAME }));
		const namePosition = markup.indexOf(MAX_LENGTH_TOOL_NAME);
		expect(markup.indexOf('data-slot="select-trigger"')).toBeGreaterThan(namePosition);
		expect(markup.indexOf(`Actions for ${MAX_LENGTH_TOOL_NAME}`)).toBeGreaterThan(namePosition);
	});
});

/** The FIRST element carrying `data-slot="name"`, sliced to its matching close tag. */
function slot(markup: string, name: string): string {
	const attribute = markup.indexOf(`data-slot="${name}"`);
	if (attribute === -1) return "";
	const start = markup.lastIndexOf("<", attribute);
	const tag = /^<([a-z0-9]+)/.exec(markup.slice(start))?.[1];
	if (!tag) return "";
	const pattern = new RegExp(`<${tag}[\\s>]|</${tag}>`, "g");
	pattern.lastIndex = start;
	let depth = 0;
	for (let match = pattern.exec(markup); match; match = pattern.exec(markup)) {
		depth += match[0].startsWith("</") ? -1 : 1;
		if (depth === 0) return markup.slice(start, match.index + match[0].length);
	}
	return markup.slice(start);
}

/** Every element carrying `data-slot="name"`, each sliced to its close tag. */
function slots(markup: string, name: string): string[] {
	const found: string[] = [];
	let rest = markup;
	for (let element = slot(rest, name); element !== ""; element = slot(rest, name)) {
		found.push(element);
		rest = rest.slice(rest.indexOf(element) + element.length);
	}
	return found;
}

type Captured = { method: string; url: string; body: unknown };

/**
 * A REAL generated client over a fake `fetch`, so the request the switch sends
 * is the one the route receives — path, verb and body — not a hand-built URL.
 * `hold` keeps the response back until released, which is how the in-flight
 * window is observed.
 */
function fakeApi(status: number, hold?: Promise<void>) {
	const captured: Captured[] = [];
	const client = createClient<paths>({
		baseUrl: "http://api.test",
		fetch: async (input: Request) => {
			const text = await input.text();
			captured.push({
				method: input.method,
				url: input.url,
				body: text === "" ? null : JSON.parse(text),
			});
			if (hold) await hold;
			const body =
				status < 400
					? companion({ companion_key: "postiz", has_destroy_verbs: false, blocked: true })
					: { detail: "companion group block failed" };
			return new Response(JSON.stringify(body), {
				status,
				headers: { "content-type": "application/json" },
			});
		},
	});
	return { client, captured };
}

const POSTIZ_TOOLS: readonly CompanionToolSettings[] = [
	tool({ tool: "postiz__list_posts", companion_key: "postiz", verb: "list_posts" }),
	tool({
		tool: "postiz__schedule_post",
		companion_key: "postiz",
		verb: "schedule_post",
		tool_class: "create",
		resolved_mode: "ask",
	}),
];

function postizGroup(
	overrides: Partial<CompanionSummary> = {},
	tools: readonly CompanionToolSettings[] = POSTIZ_TOOLS,
): CompanionToolGroup {
	return {
		companion: companion({
			companion_key: "postiz",
			has_destroy_verbs: false,
			// The server reads a blocked group as zero visible verbs (06-24).
			visible_verb_count: overrides.blocked ? 0 : tools.length,
			...overrides,
		}),
		tools,
		destroyTools: [],
	};
}

const BLOCKED_HELPER =
	"Your agent cannot see or call these. Calls it tries anyway are refused and recorded.";
const BLOCK_REJECTED = "The tools were not disabled. Your agent can still call them.";
const ALLOW_REJECTED = "The tools were not enabled. They stay blocked.";

describe("S4 tool group disable (APP-05, D-29, D-30)", () => {
	test("tracer: a tenant blocks postiz's tool group — confirm, PUT, no flip in flight, then the blocked group", async () => {
		// 1. Allowed: the per-companion header carries a trailing Switch, on,
		//    with the contract's screen-reader label. On is the position the
		//    weakening move leaves, so the Switch IS the dialog trigger.
		const before = renderRegion({ groups: [postizGroup()] });
		const control = slot(before, "group-block-control");
		expect(control).toContain('role="switch"');
		expect(control).toContain('aria-checked="true"');
		expect(control).toContain('aria-label="Allow postiz tools"');
		expect(control).toContain('aria-haspopup="dialog"');
		expect(renderedText(before)).not.toContain("Tools blocked");

		// 2. The dialog it opens (portaled, so read from its copy builder).
		const confirm = blockToolsCopy("postiz");
		expect(confirm.title).toBe("Block postiz tools");
		expect(confirm.confirmLabel).toBe("Block tools");
		expect(confirm.destructive).toBe(true);

		// 3. "Block tools" sends the PUT, and while it is in flight the Switch
		//    holds its prior position — no optimistic flip on a safety control.
		let release: () => void = () => {};
		const hold = new Promise<void>((resolve) => {
			release = resolve;
		});
		const { client, captured } = fakeApi(200, hold);
		const toasts: string[] = [];
		const settled = runGroupBlock({
			blocked: true,
			request: groupBlockRequest(client, "postiz"),
			onAccepted: (message) => toasts.push(message),
		});
		for (let tick = 0; tick < 50 && captured.length === 0; tick += 1) {
			await Promise.resolve();
		}
		expect(captured).toEqual([
			{
				method: "PUT",
				url: "http://api.test/v1/companions/group-block/postiz",
				body: { blocked: true },
			},
		]);
		const inFlight = renderRegion({
			groups: [postizGroup()],
			groupBlockPending: { postiz: true },
		});
		expect(slot(inFlight, "group-block-control")).toContain('aria-checked="true"');
		expect(renderedText(inFlight)).not.toContain("Tools blocked");
		expect(toasts).toEqual([]);

		release();
		expect(await settled).toEqual({ ok: true });
		expect(toasts).toEqual(["Tools blocked"]);

		// 4. The server's answer: the group is blocked. Chip, helper, and the
		//    rows still there — dimmed and non-interactive.
		const after = renderRegion({ groups: [postizGroup({ blocked: true })] });
		const header = slot(after, "group-block-header");
		expect(slot(header, "status-badge")).toContain('data-status="destructive"');
		expect(renderedText(slot(header, "status-badge"))).toBe("Tools blocked");
		expect(slot(after, "group-block-control")).toContain('aria-checked="false"');
		expect(renderedText(slot(after, "group-blocked-helper"))).toBe(BLOCKED_HELPER);
		const rows = slots(after, "companion-tool-row");
		expect(rows).toHaveLength(POSTIZ_TOOLS.length);
		for (const row of rows) {
			expect(row).toContain('data-blocked="true"');
			expect(row).toContain("text-muted-foreground");
		}
	});

	test("populated: turning a blocked group back on sends the PUT directly, with no dialog", async () => {
		const markup = renderRegion({ groups: [postizGroup({ blocked: true })] });
		const control = slot(markup, "group-block-control");
		expect(control).toContain('aria-checked="false"');
		// Allowing is the safe direction: the Switch is NOT a dialog trigger.
		expect(control).not.toContain("aria-haspopup");

		const { client, captured } = fakeApi(200);
		const toasts: string[] = [];
		const outcome = await runGroupBlock({
			blocked: false,
			request: groupBlockRequest(client, "postiz"),
			onAccepted: (message) => toasts.push(message),
		});
		expect(outcome).toEqual({ ok: true });
		expect(captured).toEqual([
			{
				method: "PUT",
				url: "http://api.test/v1/companions/group-block/postiz",
				body: { blocked: false },
			},
		]);
		expect(toasts).toEqual(["Tools allowed"]);
	});

	test("error: a rejected block leaves the Switch on and says so inline — never a toast", async () => {
		const { client } = fakeApi(500);
		const toasts: string[] = [];
		const outcome = await runGroupBlock({
			blocked: true,
			request: groupBlockRequest(client, "postiz"),
			onAccepted: (message) => toasts.push(message),
		});
		expect(outcome).toEqual({ ok: false, message: BLOCK_REJECTED });
		expect(toasts).toEqual([]);

		const markup = renderRegion({
			groups: [postizGroup()],
			groupBlockErrors: { postiz: BLOCK_REJECTED },
		});
		expect(slot(markup, "group-block-control")).toContain('aria-checked="true"');
		expect(renderedText(slot(markup, "group-block-error"))).toBe(BLOCK_REJECTED);
		expect(markup).not.toContain('data-slot="group-blocked-helper"');
	});

	test("error: a rejected allow leaves the Switch off with the second sentence", async () => {
		const { client } = fakeApi(500);
		const toasts: string[] = [];
		const outcome = await runGroupBlock({
			blocked: false,
			request: groupBlockRequest(client, "postiz"),
			onAccepted: (message) => toasts.push(message),
		});
		expect(outcome).toEqual({ ok: false, message: ALLOW_REJECTED });
		expect(toasts).toEqual([]);

		const markup = renderRegion({
			groups: [postizGroup({ blocked: true })],
			groupBlockErrors: { postiz: ALLOW_REJECTED },
		});
		expect(slot(markup, "group-block-control")).toContain('aria-checked="false"');
		expect(renderedText(slot(markup, "group-block-error"))).toBe(ALLOW_REJECTED);
	});

	test("loading: an allow in flight keeps the Switch off and locked — no intermediate position", () => {
		const markup = renderRegion({
			groups: [postizGroup({ blocked: true })],
			groupBlockPending: { postiz: true },
		});
		const control = slot(markup, "group-block-control");
		expect(control).toContain('aria-checked="false"');
		expect(control).toContain("data-disabled");
		expect(renderedText(slot(markup, "group-block-header"))).toContain("Tools blocked");
	});

	test("partial: a blocked group's rows stay present and readable but non-interactive", () => {
		const markup = renderRegion({ groups: [postizGroup({ blocked: true })] });
		for (const row of POSTIZ_TOOLS) {
			expect(markup).toContain(`data-tool="${row.tool}"`);
			expect(renderedText(markup)).toContain(row.tool);
		}
		const rows = slots(markup, "companion-tool-row");
		expect(rows).toHaveLength(POSTIZ_TOOLS.length);
		for (const row of rows) {
			// The policy Select and the row menu are both disabled.
			expect(slot(row, "select-trigger")).toContain("data-disabled");
			const menu = /<button[^>]*aria-label="Actions for [^"]+"[^>]*>/.exec(row)?.[0] ?? "";
			expect(menu).toMatch(/\sdisabled=""/);
		}
		// An allowed group's rows are untouched by the block.
		const allowed = renderRegion({ groups: [postizGroup()] });
		expect(allowed).not.toContain('data-blocked="true"');
		expect(allowed).not.toContain('data-slot="group-blocked-helper"');
	});

	test("empty: a companion with zero registered tools renders no group header and no switch", () => {
		const markup = renderRegion({
			groups: [postizGroup({ visible_verb_count: 0 }, []), group()],
		});
		expect(markup).not.toContain("postiz");
		expect(slots(markup, "group-block-control")).toHaveLength(1);
		expect(slot(markup, "group-block-control")).toContain('aria-label="Allow filebrowser tools"');
	});

	test("populated: Phase 5's rows are unchanged — the Switch is the only header addition, trailing the name", () => {
		const markup = renderRegion({ groups: [postizGroup()] });
		const header = slot(markup, "group-block-header");
		expect(header.indexOf("postiz")).toBeGreaterThan(-1);
		expect(header.indexOf('role="switch"')).toBeGreaterThan(header.indexOf("postiz"));
		for (const row of POSTIZ_TOOLS) {
			expect(markup).toContain(`data-tool="${row.tool}"`);
		}
		expect(markup).not.toContain('data-blocked="true"');
	});

	test("zero-one-many and long-text: chip, helper and label are fixed strings with only the name interpolated", () => {
		const longName = "a".repeat(31);
		for (const tools of [POSTIZ_TOOLS.slice(0, 1), POSTIZ_TOOLS]) {
			const markup = renderRegion({ groups: [postizGroup({ blocked: true }, tools)] });
			expect(renderedText(slot(markup, "group-blocked-helper"))).toBe(BLOCKED_HELPER);
			expect(renderedText(slot(slot(markup, "group-block-header"), "status-badge"))).toBe(
				"Tools blocked",
			);
		}
		const long = renderRegion({
			groups: [
				postizGroup({ companion_key: longName, blocked: true }, [
					tool({ tool: `${longName}__list`, companion_key: longName, verb: "list" }),
				]),
			],
		});
		expect(slot(long, "group-block-control")).toContain(`aria-label="Allow ${longName} tools"`);
		expect(renderedText(slot(long, "group-blocked-helper"))).toBe(BLOCKED_HELPER);
		expect(blockToolsCopy(longName).title).toBe(`Block ${longName} tools`);
	});
});
