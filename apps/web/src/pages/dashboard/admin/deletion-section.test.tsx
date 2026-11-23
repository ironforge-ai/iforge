import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import type {
	AdminTenantLifecycleResponse,
	AdminTenantPurgeInfo,
	AdminUserListItem,
} from "@/lib/admin-client";
import { ApiError } from "@/lib/api-errors";
import { fillCopyTemplate, formatShortDate, truncateForDisplay } from "@/lib/format";
import { renderedText } from "@/lib/rendered-text";
import { uiSpecCopy } from "@/lib/ui-spec-contract";
import {
	DELETE_CONFIRM_BODY_EMAIL,
	DELETE_CONFIRM_BODY_USER_ID,
	DELETE_CONFIRM_LABEL_EMAIL,
	DELETE_CONFIRM_LABEL_USER_ID,
	DeleteTenantConfirmBody,
	deleteTenantWithPartialReport,
	isDeleteConfirmMatch,
	PURGE_ATTEMPT_COPY,
	PURGE_ATTEMPT_WITH_ERROR_COPY,
	PURGE_ERROR_DISPLAY_MAX,
	PURGE_FAILED_COPY,
	PURGE_RUNNING_COPY,
	PURGE_RUNNING_UNCLAIMED_COPY,
	PURGE_STATUS_TONE,
	purgeAttemptSummary,
	purgeAttemptSummaryUntruncated,
	splitConfirmTemplate,
	TENANT_DELETE_PARTIAL_COPY,
	TenantLifecyclePanel,
	tenantConfirmTarget,
} from "@/pages/dashboard/admin/deletion-section";
import {
	TENANT_SEARCH_EMPTY_COPY,
	TENANT_SEARCH_LABEL,
	TENANT_SEARCH_PLACEHOLDER,
	TenantPickerView,
} from "@/pages/dashboard/admin/export-section";

/** renderToStaticMarkup escapes quotes/ampersands — decode the handful of
 * entities the contract copy can produce so assertions read as prose. */
function decode(markup: string): string {
	return markup.replaceAll("&#x27;", "'").replaceAll("&quot;", '"').replaceAll("&amp;", "&");
}

/** JSX wraps prose across lines; compare the source with runs collapsed. */
function collapse(source: string): string {
	return source.replace(/\s+/g, " ");
}

/** Await a promise that must reject, and hand back the rejection. */
async function rejection(promise: Promise<unknown>): Promise<Error> {
	try {
		await promise;
	} catch (error) {
		return error as Error;
	}
	throw new Error("expected the call to reject, but it resolved");
}

const PAGE_SRC = readFileSync(new URL("./page.tsx", import.meta.url), "utf8");

/** The 06.3 UI spec, which owns the S4 body row since X2 (06.3, D-01). Resolved
 * from `import.meta.url` like `UI_SPEC_PATH`, never from `process.cwd()`. */
const UI_SPEC_06_3_PATH = fileURLToPath(
	new URL(
		"../../../../../../.planning/phases/06.3-align-access-and-billing-gating-with-upstream-clerk-managed/06.3-UI-SPEC.md",
		import.meta.url,
	),
);
const DELETION_SECTION_SRC = readFileSync(
	new URL("./deletion-section.tsx", import.meta.url),
	"utf8",
);

const noop = () => {};

/**
 * A tenant row for the delete surface.
 *
 * `clerk_id` is present on every row and NULLABLE in value: the users table
 * constrains it to NULL for a partner tenant, which is one of the two
 * populations G-03-62 concerns (03-20 deviation 1).
 */
function tenantRow(overrides: Partial<AdminUserListItem> = {}): AdminUserListItem {
	return {
		id: "00000000-0000-0000-0000-0000000000d1",
		email: "tenant@example.com",
		clerk_id: "user_2fDeletionFixture",
		name: null,
		created_at: "2026-09-01T09:00:00Z",
		billing_exempt: false,
		...overrides,
	};
}

/** The value the dialog NAMES — read back out of its mono span, never
 * re-typed by the test, so the gate and the instruction cannot drift apart
 * while both assertions stay green. */
function namedConfirmValue(markup: string): string {
	const match = /<span class="font-mono">([^<]*)<\/span>/.exec(markup);
	if (match === null) throw new Error(`the dialog names no value at all: ${markup}`);
	return match[1] as string;
}

function renderConfirmBody(tenant: AdminUserListItem, graceDays = 30): string {
	return decode(
		renderToStaticMarkup(
			<DeleteTenantConfirmBody
				confirmValue={tenantConfirmTarget(tenant).confirmValue}
				confirmsWithEmail={tenantConfirmTarget(tenant).confirmsWithEmail}
				graceDays={graceDays}
				typed=""
				onTypedChange={noop}
			/>,
		),
	);
}

/** A schedule date and a claim date that never format to the same string. */
const PURGE_AFTER = "2026-03-01T00:00:00Z";
const CLAIMED_AT = "2026-09-04T11:00:00Z";

function purgeInfo(overrides: Partial<AdminTenantPurgeInfo> = {}): AdminTenantPurgeInfo {
	return {
		status: "pending",
		purge_after: PURGE_AFTER,
		completed_at: null,
		error: null,
		attempt_count: 0,
		claimed_at: null,
		...overrides,
	};
}

function lifecycle(
	overrides: Partial<AdminTenantLifecycleResponse> = {},
): AdminTenantLifecycleResponse {
	return { fenced: true, purge: null, grace_days: 30, ...overrides };
}

function renderPanel(value: AdminTenantLifecycleResponse): string {
	return decode(
		renderToStaticMarkup(
			<TenantLifecyclePanel
				lifecycle={value}
				confirmValue="tenant@example.com"
				confirmsWithEmail
				typed=""
				onTypedChange={noop}
				onDelete={async () => {}}
				onCancelPurge={async () => {}}
			/>,
		),
	);
}

describe("a failed purge is never labelled as scheduled (W-01 / WR-03)", () => {
	test("the failed badge says a sweep ran and did not finish, in the destructive tone", () => {
		const markup = renderPanel(
			lifecycle({
				purge: purgeInfo({
					status: "failed",
					error: "boto3: connection refused",
					attempt_count: 3,
				}),
			}),
		);
		expect(markup).toContain(PURGE_FAILED_COPY);
		// Both halves of the old badge were false on this branch.
		expect(markup).not.toContain("Purge scheduled");
		expect(markup).not.toContain(formatShortDate(PURGE_AFTER));
		// Data already lost is destructive, not a warning.
		expect(markup).toContain('data-status="destructive"');
	});

	test("the recorded error and the attempt count render beside the badge", () => {
		const markup = renderPanel(
			lifecycle({
				purge: purgeInfo({
					status: "failed",
					error: "boto3: connection refused",
					attempt_count: 3,
				}),
			}),
		);
		expect(markup).toContain("Attempt 3 — boto3: connection refused");
	});

	test("the cancel-purge affordance and its status-split dialog stay on the failed branch", () => {
		const markup = renderPanel(lifecycle({ purge: purgeInfo({ status: "failed" }) }));
		expect(markup).toContain("Cancel purge");
	});

	test("PURGE_STATUS_TONE gives failed the destructive tone and leaves the rest alone", () => {
		expect(PURGE_STATUS_TONE).toEqual({
			pending: "warning",
			failed: "destructive",
			purging: "warning",
			complete: "neutral",
		});
	});

	test("a never-claimed purge has no attempt summary at all", () => {
		expect(purgeAttemptSummary(purgeInfo({ attempt_count: 0, error: null }))).toBeNull();
		expect(purgeAttemptSummary(purgeInfo({ attempt_count: 2, error: null }))).toBe("Attempt 2");
	});

	test("a 1000-character backend exception is truncated for display, not dumped (T-03-76)", () => {
		const long = "x".repeat(1000);
		const summary = purgeAttemptSummary(purgeInfo({ attempt_count: 1, error: long })) as string;
		expect(summary.length).toBeLessThan(PURGE_ERROR_DISPLAY_MAX + 32);
		expect(summary).not.toContain(long);
		expect(truncateForDisplay("short", PURGE_ERROR_DISPLAY_MAX)).toBe("short");
		expect(truncateForDisplay(long, PURGE_ERROR_DISPLAY_MAX).endsWith("…")).toBe(true);
	});

	test("the truncated error stays RECOVERABLE — the full text is the span's title (UAT 61)", () => {
		// The real WR-04 message that exposed this: 372 characters whose
		// actionable half sits well past the 160-char display budget, and past
		// the max-w-96 CSS clip on top of it.
		const long = `tenant purge cannot complete: deploy_api_url is unset, so D-16's subscription cancel (POST /v2/subscription/cancel) and deployment teardown (DELETE /v2/deployments/{id}) can be neither performed nor verified. Set DEPLOY_API_URL, or set TENANT_PURGE_SKIP_HOSTED_TEARDOWN=true to opt this deployment out explicitly (the skip is then recorded in the tenant.purged audit row).`;
		const purge = purgeInfo({ status: "failed", attempt_count: 5, error: long });

		const visible = purgeAttemptSummary(purge) as string;
		const full = purgeAttemptSummaryUntruncated(purge) as string;
		expect(visible).not.toContain("TENANT_PURGE_SKIP_HOSTED_TEARDOWN");
		expect(full).toContain("TENANT_PURGE_SKIP_HOSTED_TEARDOWN");
		expect(full).toContain("Set DEPLOY_API_URL");
		expect(full).toBe(`Attempt 5 — ${long}`);

		const markup = renderPanel(lifecycle({ purge }));
		expect(markup).toContain("TENANT_PURGE_SKIP_HOSTED_TEARDOWN");
	});

	test("a title is omitted when it would only repeat the visible line", () => {
		const short = purgeInfo({ status: "failed", attempt_count: 3, error: "connection refused" });
		expect(purgeAttemptSummaryUntruncated(short)).toBe(purgeAttemptSummary(short));
		expect(purgeAttemptSummaryUntruncated(purgeInfo({ attempt_count: 0, error: null }))).toBeNull();
		expect(purgeAttemptSummaryUntruncated(purgeInfo({ attempt_count: 2, error: null }))).toBe(
			"Attempt 2",
		);
	});
});

describe("the other lifecycle branches", () => {
	test("pending still renders the scheduled badge against purge_after, in warning", () => {
		const markup = renderPanel(lifecycle({ purge: purgeInfo({ status: "pending" }) }));
		expect(markup).toContain(`Purge scheduled — ${formatShortDate(PURGE_AFTER)}`);
		expect(markup).toContain('data-status="warning"');
	});

	test("purging is labelled with the claim time, not the schedule date", () => {
		const markup = renderPanel(
			lifecycle({ purge: purgeInfo({ status: "purging", claimed_at: CLAIMED_AT }) }),
		);
		expect(markup).toContain(
			fillCopyTemplate(PURGE_RUNNING_COPY, { date: formatShortDate(CLAIMED_AT) }),
		);
		expect(markup).not.toContain(formatShortDate(PURGE_AFTER));
	});

	test("purging degrades gracefully when the claim time is null", () => {
		const markup = renderPanel(
			lifecycle({ purge: purgeInfo({ status: "purging", claimed_at: null }) }),
		);
		expect(markup).toContain(PURGE_RUNNING_UNCLAIMED_COPY);
		expect(markup).not.toContain("started");
	});

	test("complete and unfenced branches are unchanged", () => {
		const done = renderPanel(
			lifecycle({
				purge: purgeInfo({ status: "complete", completed_at: "2026-04-02T00:00:00Z" }),
			}),
		);
		expect(done).toContain(`Purged — ${formatShortDate("2026-04-02T00:00:00Z")}`);
		const fenced = renderPanel(lifecycle({ fenced: true, purge: null }));
		expect(fenced).toContain("Fenced — no purge scheduled");
		expect(fenced).toContain("Delete tenant");
	});
});

describe("the dialog states the server's grace window, not a client constant (CR-02 / WR-13)", () => {
	test("rendering with two windows produces two sentences", () => {
		const thirty = renderConfirmBody(tenantRow(), 30);
		const seven = renderConfirmBody(tenantRow(), 7);
		expect(thirty).toContain("grace window (30 days)");
		expect(seven).toContain("grace window (7 days)");
		expect(seven).not.toContain("30 days");
	});

	test("the panel threads the lifecycle response's window into the dialog", () => {
		// `ConfirmAction` renders its description only once the dialog is open,
		// which a static render never reaches — so the wiring is asserted at the
		// seam: the panel hands the dialog the SERVER's number, and the test
		// above proves the dialog renders whatever number it is handed.
		const src = readFileSync(new URL("./deletion-section.tsx", import.meta.url), "utf8");
		expect(collapse(src)).toContain("graceDays={lifecycle.grace_days}");
	});

	test("the mirrored client constant is gone from the module", () => {
		const src = readFileSync(new URL("./deletion-section.tsx", import.meta.url), "utf8");
		expect(src).not.toContain("TENANT_PURGE_GRACE_DAYS");
	});
});

/**
 * The dialog names the value it will actually check (G-03-62, missing item 7).
 *
 * `confirmValue` is `email ?? id`, but BOTH the sentence and the input's
 * accessible name said "email" unconditionally — so an operator deleting a
 * tenant with no email was instructed to type a value the gate would reject,
 * in the last text anyone reads before an irreversible action.
 */
describe("the delete confirmation asks for the value the gate demands", () => {
	test("a tenant WITH an email is asked for the email, and the email satisfies the gate", () => {
		const tenant = tenantRow();
		const markup = renderConfirmBody(tenant);
		const named = namedConfirmValue(markup);
		expect(named).toBe(tenant.email as string);
		expect(markup).toContain(DELETE_CONFIRM_LABEL_EMAIL);
		expect(markup).toContain("Type the tenant's email to confirm");
		expect(markup).not.toContain("user id to confirm");
		// The value the dialog NAMES is the value the gate accepts.
		expect(isDeleteConfirmMatch(named, tenant.email ?? tenant.id)).toBe(true);
	});

	test("a tenant with NO email is asked for the user id, and the user id satisfies the gate", () => {
		const tenant = tenantRow({ email: null, clerk_id: null });
		const markup = renderConfirmBody(tenant);
		const named = namedConfirmValue(markup);
		expect(named).toBe(tenant.id);
		expect(markup).toContain(DELETE_CONFIRM_LABEL_USER_ID);
		expect(markup).toContain("Type the tenant's user id to confirm");
		// The defect in one line: the dialog used to demand an email this
		// tenant does not have, while the gate held a uuid.
		expect(markup).not.toContain("Type the tenant's email to confirm");
		expect(isDeleteConfirmMatch(named, tenant.email ?? tenant.id)).toBe(true);
	});

	test("STRUCTURAL: the instruction and the gate are computed from ONE value, for either tenant", () => {
		// Nothing here is hardcoded twice. The confirm value is derived from the
		// row exactly as `DeletionSection` derives it, the NAMED value is read
		// back out of the rendered dialog, and the gate is asked about that.
		for (const tenant of [tenantRow(), tenantRow({ email: null, clerk_id: null })]) {
			const confirmValue = tenant.email ?? tenant.id;
			const named = namedConfirmValue(renderConfirmBody(tenant));
			expect(named).toBe(confirmValue);
			expect(isDeleteConfirmMatch(named, confirmValue)).toBe(true);
			// The other identifier the row carries never satisfies the gate.
			expect(isDeleteConfirmMatch(tenant.id, confirmValue)).toBe(tenant.email === null);
		}
	});

	test("the discriminant comes from the tenant row, never from sniffing the string for an @", () => {
		expect(DELETION_SECTION_SRC).toContain("confirmsWithEmail");
		expect(DELETION_SECTION_SRC).not.toMatch(/confirmValue\.includes\(\s*"@"\s*\)/);
		// One site computes both — now literally one call returning both, so
		// they cannot drift apart the way `email ?? id` and `email != null` did
		// over an empty-string email (WR-06).
		expect(collapse(DELETION_SECTION_SRC)).toContain(
			"const { confirmValue, confirmsWithEmail } = tenantConfirmTarget(tenant);",
		);
	});

	test("the matcher keeps every property it has — the rename weakens nothing (T-03-87)", () => {
		expect(isDeleteConfirmMatch("tenant@example.com", "tenant@example.com")).toBe(true);
		expect(isDeleteConfirmMatch("tenant@example", "tenant@example.com")).toBe(false);
		expect(isDeleteConfirmMatch("tenant@example.com ", "tenant@example.com")).toBe(false);
		expect(isDeleteConfirmMatch("Tenant@Example.com", "tenant@example.com")).toBe(false);
		expect(isDeleteConfirmMatch("", "tenant@example.com")).toBe(false);
		// An empty target is never a free pass.
		expect(isDeleteConfirmMatch("", "")).toBe(false);
		// And the uuid branch has the same properties, not looser ones.
		const id = "00000000-0000-0000-0000-0000000000d1";
		expect(isDeleteConfirmMatch(id, id)).toBe(true);
		expect(isDeleteConfirmMatch(id.toUpperCase(), id)).toBe(false);
		expect(isDeleteConfirmMatch(id.slice(0, 8), id)).toBe(false);
	});

	test("the parameter no longer encodes an assumption the value does not honour", () => {
		// `email` as a name is the defect in miniature: it is why the copy said
		// one thing while the gate checked another.
		expect(DELETION_SECTION_SRC).not.toMatch(/isDeleteConfirmMatch\([^)]*\bemail: string/);
		expect(DELETION_SECTION_SRC).toContain("confirmValue: string");
	});
});

/**
 * T-03-88's mitigation is INHERITED, so it gets a gate.
 *
 * `confirmValue` is a GATE value and cannot hold two identifiers at once, so
 * the delete panel does not render the id itself — it renders the SHARED
 * `TenantPicker`, whose chip plan 03-21 task 1 taught to show the email and
 * the id together. A refactor to a local chip would silently reopen the
 * deletion half of `missing` item 6 and break no test at all. These two
 * assertions are that test.
 */
describe("the delete panel reads its tenant id off the shared picker", () => {
	test("RENDERED: the shared chip puts BOTH the email and the tenant id in front of the operator", () => {
		const tenant = tenantRow();
		const text = renderedText(
			renderToStaticMarkup(
				<TenantPickerView
					selected={tenant}
					onSelect={noop}
					searchLabel={TENANT_SEARCH_LABEL}
					term=""
					onTermChange={noop}
					results={[]}
					isLoading={false}
					error={null}
					onRetry={noop}
				/>,
			),
		);
		expect(text).toContain(tenant.email as string);
		expect(text).toContain(tenant.id);
	});

	test("STRUCTURAL: DeletionSection still renders the imported TenantPicker with the selected tenant", () => {
		expect(collapse(DELETION_SECTION_SRC)).toContain("<TenantPicker selected={tenant}");
		// TenantPicker must come from export-section — localizing a copy here
		// would silently reopen the deletion half of G-03-62 missing item 6.
		// Tolerant of SIBLING named imports from the same module (the shared
		// tenantDisplayEmail predicate rides this import, W-13), because what
		// this guards is the source of the picker, not the import's width.
		expect(DELETION_SECTION_SRC).toMatch(
			/import \{[^}]*\bTenantPicker\b[^}]*\} from "@\/pages\/dashboard\/admin\/export-section"/,
		);
	});
});

describe("a fenced-then-failed delete reports as partially applied (W-06)", () => {
	const boom = new Error("provider exploded");

	test("a successful delete is passed straight through", async () => {
		const result = await deleteTenantWithPartialReport(
			async () => "ok",
			async () => lifecycle(),
		);
		expect(result).toBe("ok");
	});

	test("a rejection with a purge row on the refetch reports partial application", async () => {
		const error = await rejection(
			deleteTenantWithPartialReport(
				async () => {
					throw boom;
				},
				async () => lifecycle({ purge: purgeInfo({ status: "pending" }) }),
			),
		);
		expect(error.message).toBe(TENANT_DELETE_PARTIAL_COPY);
		expect(TENANT_DELETE_PARTIAL_COPY).toContain("purge is scheduled");
		expect(TENANT_DELETE_PARTIAL_COPY).toContain("could not be removed");
	});

	test("a rejection with no purge row keeps the original error", async () => {
		const error = await rejection(
			deleteTenantWithPartialReport(
				async () => {
					throw boom;
				},
				async () => lifecycle({ fenced: false, purge: null }),
			),
		);
		expect(error).toBe(boom);
	});

	test("an unreadable lifecycle keeps the original error rather than guessing", async () => {
		const error = await rejection(
			deleteTenantWithPartialReport(
				async () => {
					throw boom;
				},
				async () => {
					throw new Error("lifecycle unreachable");
				},
			),
		);
		expect(error).toBe(boom);
	});

	test("a rejected admin key routes to the key path first, untouched", async () => {
		const keyError = new ApiError(401, "Unauthorized");
		let refetched = false;
		const error = await rejection(
			deleteTenantWithPartialReport(
				async () => {
					throw keyError;
				},
				async () => {
					refetched = true;
					return lifecycle({ purge: purgeInfo() });
				},
			),
		);
		expect(error).toBe(keyError);
		expect(refetched).toBe(false);
	});
});

describe("code and contract agree — read from both sides, never from a diff", () => {
	test("the tenant picker's three strings equal their contract rows", () => {
		expect(uiSpecCopy("S3 tenant picker — accessible label")).toBe(TENANT_SEARCH_LABEL);
		expect(uiSpecCopy("S3 tenant picker — placeholder")).toBe(TENANT_SEARCH_PLACEHOLDER);
		expect(uiSpecCopy("S3 tenant picker — no matches")).toBe(TENANT_SEARCH_EMPTY_COPY);
	});

	test("the failed-purge badge and its attempt templates equal their contract rows", () => {
		expect(uiSpecCopy("S3 purge failed badge")).toBe(PURGE_FAILED_COPY);
		// Interpolated rows are compared as TEMPLATES, never as a rendered value.
		expect(uiSpecCopy("S3 purge failed detail — attempts only")).toBe(PURGE_ATTEMPT_COPY);
		expect(uiSpecCopy("S3 purge failed detail — attempts and error")).toBe(
			PURGE_ATTEMPT_WITH_ERROR_COPY,
		);
	});

	test("the purging badge's two labels equal their contract rows", () => {
		expect(uiSpecCopy("S3 purge running badge")).toBe(PURGE_RUNNING_COPY);
		expect(uiSpecCopy("S3 purge running badge — never claimed")).toBe(PURGE_RUNNING_UNCLAIMED_COPY);
	});

	test("the partially-applied delete message equals its contract row", () => {
		expect(uiSpecCopy("S3 delete partially applied")).toBe(TENANT_DELETE_PARTIAL_COPY);
	});

	test("both confirmation bodies and both accessible names equal their contract rows", () => {
		// Interpolated rows are compared as TEMPLATES, never as a rendered value.
		expect(uiSpecCopy("S3 delete confirmation body — email")).toBe(DELETE_CONFIRM_BODY_EMAIL);
		expect(uiSpecCopy("S3 delete confirmation body — user id")).toBe(DELETE_CONFIRM_BODY_USER_ID);
		expect(uiSpecCopy("S3 delete confirmation input — email")).toBe(DELETE_CONFIRM_LABEL_EMAIL);
		expect(uiSpecCopy("S3 delete confirmation input — user id")).toBe(DELETE_CONFIRM_LABEL_USER_ID);
	});

	test("the two variants differ ONLY in the noun they ask for", () => {
		expect(DELETE_CONFIRM_BODY_EMAIL.replace("email", "user id")).toBe(DELETE_CONFIRM_BODY_USER_ID);
		expect(DELETE_CONFIRM_LABEL_EMAIL.replace("email", "user id")).toBe(
			DELETE_CONFIRM_LABEL_USER_ID,
		);
		// House rules: active voice, no exclamation marks.
		for (const copy of [DELETE_CONFIRM_BODY_EMAIL, DELETE_CONFIRM_BODY_USER_ID]) {
			expect(copy).not.toContain("!");
			expect(copy).toContain("{value}");
			expect(copy).toContain("{days}");
		}
	});
});

describe("the admin key gate is a declared surface (UI-REVIEW 11)", () => {
	test("its four shipped strings equal their S4 contract rows", () => {
		const heading = uiSpecCopy("S4 admin key gate — heading");
		// X2 (06.3, D-01): the body no longer mentions invites, so its row moved to
		// the 06.3 spec. The other three rows stay on the 03 spec.
		const body = uiSpecCopy("X2 S4 admin key gate body", UI_SPEC_06_3_PATH);
		const submit = uiSpecCopy("S4 admin key gate — submit");
		const rejected = uiSpecCopy("S4 admin key gate — rejected key");
		expect(collapse(PAGE_SRC)).toContain(`>${heading}</h1>`);
		expect(collapse(PAGE_SRC)).toContain(body);
		expect(collapse(PAGE_SRC)).toContain(`> ${submit} </Button>`);
		expect(collapse(PAGE_SRC)).toContain(rejected);
	});

	test("the gate's spacing is on the declared scale, with only the named exception", () => {
		expect(PAGE_SRC).not.toContain("py-16");
		const halfSteps = new Set(
			[
				...PAGE_SRC.matchAll(
					/\b(?:p|px|py|pt|pb|pl|pr|m|mx|my|mt|mb|ml|mr|gap|gap-x|gap-y|space-x|space-y)-\d*\.5\b/g,
				),
			].map((match) => match[0]),
		);
		expect([...halfSteps]).toEqual(["space-y-1.5"]);
	});
});

describe("one authoritative predicate decides what the delete gate demands", () => {
	test("an empty-string email is not an email — the tenant stays deletable by id", () => {
		// WR-06: three predicates disagreed about "". `confirmValue` used
		// `email ?? id` (`??` passes "" through), `confirmsWithEmail` used
		// `email != null` (true for ""). The result: confirmValue was "",
		// isDeleteConfirmMatch requires length > 0, so the confirm button was
		// permanently disabled — while the dialog asked for an email and named
		// no value at all. `User.email` is a nullable String(320) with no CHECK
		// and user_provisioning.py passes the claim through unnormalised, so ""
		// is representable.
		const blank = tenantConfirmTarget(tenantRow({ email: "" }));
		expect(blank.confirmsWithEmail).toBe(false);
		expect(blank.confirmValue).toBe(tenantRow().id);
		expect(isDeleteConfirmMatch(blank.confirmValue, blank.confirmValue)).toBe(true);
	});

	test("a real email still gates on the email", () => {
		const withEmail = tenantConfirmTarget(tenantRow({ email: "someone@example.com" }));
		expect(withEmail.confirmsWithEmail).toBe(true);
		expect(withEmail.confirmValue).toBe("someone@example.com");
	});

	test("a null email gates on the id", () => {
		const noEmail = tenantConfirmTarget(tenantRow({ email: null }));
		expect(noEmail.confirmsWithEmail).toBe(false);
		expect(noEmail.confirmValue).toBe(tenantRow().id);
	});

	test("no tenant selected gates on nothing, and nothing satisfies it", () => {
		const none = tenantConfirmTarget(null);
		expect(none.confirmValue).toBe("");
		expect(isDeleteConfirmMatch("", none.confirmValue)).toBe(false);
	});
});

describe("the confirmation template is filled on both sides of its value", () => {
	test("a placeholder before {value} is filled, not rendered literally", () => {
		// WR-05: only the half AFTER {value} was passed through
		// fillCopyTemplate, so a placeholder ahead of it would have shipped as
		// literal braces. Both shipped templates happen to put {value} first,
		// which is exactly why nothing caught it.
		const [before, after] = splitConfirmTemplate("Within {days} days {value} is erased.");
		expect(fillCopyTemplate(before, { days: "30" })).toBe("Within 30 days ");
		expect(after).toBe(" is erased.");
	});

	test("a template with two {value} markers is rejected, not silently truncated", () => {
		// String.split + a 2-element destructure dropped everything after the
		// second marker without a word.
		expect(() => splitConfirmTemplate("{value} and also {value}.")).toThrow(/exactly one/);
	});

	test("a template with no {value} is rejected", () => {
		expect(() => splitConfirmTemplate("This names nothing.")).toThrow(/exactly one/);
	});

	test("both shipped templates satisfy the guard", () => {
		expect(() => splitConfirmTemplate(DELETE_CONFIRM_BODY_EMAIL)).not.toThrow();
		expect(() => splitConfirmTemplate(DELETE_CONFIRM_BODY_USER_ID)).not.toThrow();
	});
});

describe("the component fills both halves, not just the helper (W-14)", () => {
	test("DeleteTenantConfirmBody passes the leading half through fillCopyTemplate", () => {
		// W-14: WR-05 changed splitConfirmTemplate AND the component, but only
		// the helper was gated — removing fillCopyTemplate from the before-half
		// left 42 pass / 0 fail. It is inert only because both shipped templates
		// happen to put {value} first, so no rendered assertion can reach it;
		// the moment a contract row moves the placeholder, the WR-05 condition
		// re-opens silently. Source-level by necessity (I-05 class), and named
		// as such rather than dressed up as a behavioural check.
		const collapsed = collapse(DELETION_SECTION_SRC);
		expect(collapsed).toContain("{fillCopyTemplate(before, { days: String(graceDays) })}");
		expect(collapsed).toContain("{fillCopyTemplate(after, { days: String(graceDays) })}");
	});

	test("neither half is interpolated raw", () => {
		const collapsed = collapse(DELETION_SECTION_SRC);
		expect(collapsed).not.toMatch(/\{before\}/);
		expect(collapsed).not.toMatch(/\{after\}/);
	});
});
