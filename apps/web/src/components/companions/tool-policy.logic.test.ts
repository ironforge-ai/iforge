import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { uiSpecCopy, uiSpecRowCells } from "@/lib/ui-spec-contract";
import {
	allowToolsSwitchLabel,
	allowWithoutAskingCopy,
	blockToolsCopy,
	type CompanionSummary,
	type CompanionToolCatalog,
	type CompanionToolSettings,
	companionsForAgent,
	enableDestroyToolsCopy,
	GROUP_ALLOW_ERROR_COPY,
	GROUP_ALLOWED_TOAST,
	GROUP_BLOCK_ERROR_COPY,
	GROUP_BLOCKED_CHIP,
	GROUP_BLOCKED_HELPER,
	GROUP_BLOCKED_TOAST,
	groupBlockErrorCopy,
	groupRendersHeader,
	groupToolsByCompanion,
	initialPolicyState,
	isToolClassResolved,
	nextPolicyState,
	POLICY_CHANGE_ERROR_COPY,
	policyIsOverridden,
	policyOptions,
	policySelectDisabled,
	policySelectValue,
	requiresConfirmation,
	resolvedPolicyOption,
	resolvedReasonSentence,
	showsDestroyGroup,
	toolClassTone,
} from "./tool-policy.logic";

const AGENT_ID = "6c3b1a2e-0000-4000-8000-000000000001";
const OTHER_AGENT_ID = "6c3b1a2e-0000-4000-8000-000000000002";

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

describe("policy options", () => {
	test("there are exactly three, and Allow is the weakened state", () => {
		expect(policyOptions.map((option) => option.value)).toEqual(["allow", "ask", "deny"]);
		expect(policyOptions.map((option) => option.label)).toEqual(["Allow", "Ask", "Deny"]);

		const byValue = Object.fromEntries(policyOptions.map((option) => [option.value, option]));
		// The deliberate inversion: a tool that runs without asking is weaker,
		// so its chip is `warning`, never `success`.
		expect(byValue.allow?.tone).toBe("warning");
		expect(byValue.deny?.tone).toBe("neutral");
		expect(byValue.ask?.tone).toBe("neutral");
	});

	test("class chips map to the spec's tones and an unknown class stays neutral", () => {
		expect(toolClassTone("read")).toBe("neutral");
		expect(toolClassTone("create")).toBe("info");
		expect(toolClassTone("mutate")).toBe("warning");
		expect(toolClassTone("destroy")).toBe("destructive");
		expect(toolClassTone("teleport")).toBe("neutral");
		expect(isToolClassResolved("teleport")).toBe(false);
	});
});

describe("confirmation gating", () => {
	test("Allow on a mutate or destroy tool requires confirmation", () => {
		expect(requiresConfirmation("mutate", "allow")).toBe(true);
		expect(requiresConfirmation("destroy", "allow")).toBe(true);
	});

	test("Allow on a read tool does not, and neither does Ask or Deny on anything", () => {
		expect(requiresConfirmation("read", "allow")).toBe(false);
		expect(requiresConfirmation("create", "allow")).toBe(false);
		expect(requiresConfirmation("destroy", "ask")).toBe(false);
		expect(requiresConfirmation("destroy", "deny")).toBe(false);
		expect(requiresConfirmation("mutate", "deny")).toBe(false);
	});

	test("the confirmation copy names the tool and the agent verbatim", () => {
		const copy = allowWithoutAskingCopy("filebrowser__move_path", "Research Agent");
		expect(copy.title).toBe("Allow without asking");
		expect(copy.confirmLabel).toBe("Allow without asking");
		expect(copy.destructive).toBe(false);
		expect(copy.description).toContain("filebrowser__move_path");
		expect(copy.description).toContain("Research Agent");
		expect(copy.description).toContain("Blast-radius caps still apply.");
	});

	test("enabling destroy tools is a destructive confirmation naming the companion", () => {
		const copy = enableDestroyToolsCopy("filebrowser", "Research Agent");
		expect(copy.title).toBe("Enable destroy tools");
		expect(copy.confirmLabel).toBe("Enable destroy tools");
		expect(copy.destructive).toBe(true);
		expect(copy.description).toContain("permanently remove data in filebrowser");
		expect(copy.description).toContain("Research Agent");
		expect(copy.description).toContain("You can turn this off at any time.");
	});
});

describe("the value the Select renders", () => {
	test("a row with no stored policy renders the class default, never unset", () => {
		const row = tool({
			stored_mode: null,
			resolved_mode: "allow",
			resolved_reason: "class_default",
		});
		expect(policySelectValue(row)).toBe("allow");

		const writeRow = tool({
			tool: "filebrowser__write_file",
			verb: "write_file",
			tool_class: "create",
			stored_mode: null,
			resolved_mode: "ask",
			resolved_reason: "class_default",
		});
		// Ask-on-write is the shipped default, and it arrives from the server's
		// own resolution rather than from a default invented here.
		expect(policySelectValue(writeRow)).toBe("ask");
	});

	test("a stored mode outside the vocabulary falls back to what the server resolved", () => {
		// `stored_mode` is typed `string | null` on the wire precisely so a
		// corrupt row stays visible instead of taking the catalog down.
		const row = tool({
			stored_mode: "yolo",
			resolved_mode: "ask",
			resolved_reason: "unclassified_defaults_to_ask",
		});
		expect(policySelectValue(row)).toBe("ask");
	});

	test("stored and resolved are rendered as different facts", () => {
		const row = tool({
			tool: "filebrowser__delete_path",
			verb: "delete_path",
			tool_class: "destroy",
			stored_mode: "allow",
			resolved_mode: "deny",
			resolved_reason: "refused_destroy_disabled",
			destroy_enabled: false,
		});
		// The Select shows what the tenant SET.
		expect(policySelectValue(row)).toBe("allow");
		// The chip shows what is IN FORCE.
		expect(resolvedPolicyOption(row).value).toBe("deny");
		expect(resolvedPolicyOption(row).chipLabel).toBe("Blocked");
		expect(policyIsOverridden(row)).toBe(true);
		expect(resolvedReasonSentence(row.resolved_reason)).toContain("Destroy tools are off");
	});

	test("a resolution the tenant chose needs no explanation", () => {
		expect(resolvedReasonSentence("class_default")).toBeNull();
		expect(resolvedReasonSentence("explicit_policy_row")).toBeNull();
		expect(resolvedReasonSentence("unclassified_defaults_to_ask")).toContain(
			"has not classified this tool",
		);
	});
});

describe("the disabled Select", () => {
	test("an unresolved class disables it rather than defaulting to Allow", () => {
		const row = tool({
			tool_class: "teleport",
			resolved_mode: "ask",
			resolved_reason: "unclassified_defaults_to_ask",
		});
		expect(policySelectDisabled(row, false)).toBe(true);
	});

	test("a classified tool is enabled until a change is in flight", () => {
		const row = tool({ tool_class: "mutate", resolved_mode: "ask" });
		expect(policySelectDisabled(row, false)).toBe(false);
		expect(policySelectDisabled(row, true)).toBe(true);
	});
});

describe("the policy transition machine", () => {
	test("a request keeps the prior value on screen — there is no optimistic flip", () => {
		const state = nextPolicyState(initialPolicyState("ask"), { type: "request", mode: "allow" });
		expect(state.mode).toBe("ask");
		expect(state.requestedMode).toBe("allow");
		expect(state.pending).toBe(true);
		expect(state.error).toBeNull();
	});

	test("a settled request moves to the accepted value", () => {
		const requested = nextPolicyState(initialPolicyState("ask"), {
			type: "request",
			mode: "allow",
		});
		const settled = nextPolicyState(requested, { type: "settled", mode: "allow" });
		expect(settled.mode).toBe("allow");
		expect(settled.priorMode).toBe("allow");
		expect(settled.pending).toBe(false);
		expect(settled.error).toBeNull();
	});

	test("an error reverts to the prior value and says so at the action", () => {
		const requested = nextPolicyState(initialPolicyState("ask"), {
			type: "request",
			mode: "allow",
		});
		const failed = nextPolicyState(requested, { type: "failed" });
		expect(failed.mode).toBe("ask");
		expect(failed.priorMode).toBe("ask");
		expect(failed.pending).toBe(false);
		expect(failed.error).toBe(POLICY_CHANGE_ERROR_COPY);
		expect(failed.error).not.toContain("!");
	});

	test("a second request inside one frame is dropped", () => {
		const first = nextPolicyState(initialPolicyState("ask"), { type: "request", mode: "allow" });
		const second = nextPolicyState(first, { type: "request", mode: "deny" });
		expect(second).toBe(first);
		expect(second.requestedMode).toBe("allow");
	});

	test("a settled change clears a previous error", () => {
		const failed = nextPolicyState(
			nextPolicyState(initialPolicyState("ask"), { type: "request", mode: "allow" }),
			{ type: "failed" },
		);
		const retried = nextPolicyState(failed, { type: "request", mode: "deny" });
		expect(retried.error).toBeNull();
		expect(nextPolicyState(retried, { type: "settled", mode: "deny" }).mode).toBe("deny");
	});
});

describe("grouping by companion", () => {
	const catalog: CompanionToolCatalog = {
		companions: [
			companion(),
			companion({
				companion_key: "elsewhere",
				environment_ids: [OTHER_AGENT_ID],
				has_destroy_verbs: false,
			}),
		],
		tools: [
			tool(),
			tool({ tool: "filebrowser__write_file", verb: "write_file", tool_class: "create" }),
			tool({ tool: "filebrowser__move_path", verb: "move_path", tool_class: "mutate" }),
			tool({ tool: "filebrowser__delete_path", verb: "delete_path", tool_class: "destroy" }),
			tool({ tool: "elsewhere__read_row", companion_key: "elsewhere", verb: "read_row" }),
		],
	};

	test("only companions deployed on this agent are rendered", () => {
		expect(companionsForAgent(catalog, AGENT_ID).map((c) => c.companion_key)).toEqual([
			"filebrowser",
		]);
		expect(companionsForAgent(catalog, OTHER_AGENT_ID).map((c) => c.companion_key)).toEqual([
			"elsewhere",
		]);
		expect(companionsForAgent(undefined, AGENT_ID)).toEqual([]);
	});

	test("destroy verbs are split out of the main group", () => {
		const [group] = groupToolsByCompanion(catalog, AGENT_ID);
		expect(group?.tools.map((row) => row.verb)).toEqual(["list_dir", "write_file", "move_path"]);
		expect(group?.destroyTools.map((row) => row.verb)).toEqual(["delete_path"]);
	});

	test("a companion with no destroy verb renders no destroy group", () => {
		expect(showsDestroyGroup(companion({ has_destroy_verbs: false }))).toBe(false);
		expect(showsDestroyGroup(companion({ has_destroy_verbs: true, destroy_enabled: false }))).toBe(
			true,
		);
	});

	test("a companion with zero registered tools renders no header; a reconciling one does", () => {
		const empty = companion({ visible_verb_count: 0, has_destroy_verbs: false });
		expect(groupRendersHeader({ companion: empty, tools: [], destroyTools: [] })).toBe(false);
		// Verbs the catalog still owes are coming: the header stays.
		const reconciling = companion({ visible_verb_count: 3 });
		expect(groupRendersHeader({ companion: reconciling, tools: [], destroyTools: [] })).toBe(true);
		// A blocked group reads zero visible verbs but keeps its rows, and its header.
		const blocked = companion({ visible_verb_count: 0, blocked: true });
		expect(groupRendersHeader({ companion: blocked, tools: [tool()], destroyTools: [] })).toBe(
			true,
		);
	});
});

/** The phase-06 UI contract, resolved from this module rather than the cwd. */
const SPEC_06 = fileURLToPath(
	new URL(
		"../../../../../.planning/phases/06-first-companion-end-to-end/06-UI-SPEC.md",
		import.meta.url,
	),
);
const copy06 = (element: string) => uiSpecCopy(element, SPEC_06);
const cells06 = (element: string) => uiSpecRowCells(element, SPEC_06);
/** Widen a literal-typed constant so it compares with a string read from the spec. */
const shipped = (value: string): string => value;
const NAME = "postiz";

describe("S4 copy parity with the 06 contract", () => {
	test("Disable a companion's tool group — title, description, confirm label, destructive", () => {
		const [, title, description, confirmLabel, destructive, typed] = cells06(
			"Disable a companion's tool group",
		);
		const fill = (cell: string | undefined) =>
			(cell ?? "").replace(/^"|"$/g, "").replaceAll("{companion}", NAME);
		const built = blockToolsCopy(NAME);
		expect(built.title).toBe(fill(title));
		expect(built.description).toBe(fill(description));
		expect(built.confirmLabel).toBe(fill(confirmLabel));
		expect(destructive).toBe("`true`");
		expect(built.destructive).toBe(true);
		// Blocking is not irreversible destruction: no typed confirmation.
		expect(typed).toBe("no");
	});

	test("the Switch's screen-reader label", () => {
		expect(allowToolsSwitchLabel(NAME)).toBe(
			copy06("S4 switch label (screen-reader)").replace("{companion}", NAME),
		);
	});

	test("both rejection sentences, by the direction asked", () => {
		expect(shipped(GROUP_BLOCK_ERROR_COPY)).toBe(copy06("Group disable rejected"));
		expect(shipped(GROUP_ALLOW_ERROR_COPY)).toBe(copy06("Group enable rejected"));
		expect(groupBlockErrorCopy(true)).toBe(GROUP_BLOCK_ERROR_COPY);
		expect(groupBlockErrorCopy(false)).toBe(GROUP_ALLOW_ERROR_COPY);
	});

	test("the Group disabled chip is destructive and reads Tools blocked", () => {
		const [, tone, label] = cells06("Group disabled").map((value) => value.replace(/`/g, ""));
		expect(tone).toBe("destructive");
		expect(shipped(GROUP_BLOCKED_CHIP)).toBe(label as string);
	});

	test("the helper line and both toasts are the contract's", () => {
		const spec = readFileSync(SPEC_06, "utf8");
		expect(spec).toContain(`"${GROUP_BLOCKED_HELPER}"`);
		const toasts = spec.slice(spec.indexOf("### Toasts"), spec.indexOf("## Screen Contracts"));
		expect(toasts).toContain(`"${GROUP_BLOCKED_TOAST}"`);
		expect(toasts).toContain(`"${GROUP_ALLOWED_TOAST}"`);
	});

	test("house rules: no exclamation marks in any S4 string", () => {
		for (const value of [
			GROUP_BLOCKED_CHIP,
			GROUP_BLOCKED_HELPER,
			GROUP_BLOCK_ERROR_COPY,
			GROUP_ALLOW_ERROR_COPY,
			GROUP_BLOCKED_TOAST,
			GROUP_ALLOWED_TOAST,
			blockToolsCopy(NAME).description,
		]) {
			expect(value).not.toContain("!");
		}
	});
});
