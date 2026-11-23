import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { isValidElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
	type AdminTenantExportListItem,
	type AdminUserListItem,
	downloadTenantExport,
	lookupUsers,
} from "@/lib/admin-client";
import { renderedText } from "@/lib/rendered-text";
import { uiSpecCopy } from "@/lib/ui-spec-contract";
import {
	CLERK_IDENTIFIER_COPIED_TOAST,
	COPY_CLERK_IDENTIFIER_TITLE,
	COPY_IDENTIFIER_TITLE,
	IDENTIFIER_COPIED_TOAST,
} from "@/pages/dashboard/admin/copy-identifier";
import {
	EXPORT_AGAIN_WITH_VAULT_LABEL,
	EXPORT_AGAIN_WITHOUT_VAULT_LABEL,
	EXPORT_FAILED_COPY,
	EXPORT_FAILED_WITH_ERROR_COPY,
	EXPORT_LINK_TTL_LABEL,
	EXPORT_STATUS_LABEL,
	ExportJobRow,
	type ExportJobRowState,
	exportJobRowState,
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

const noop = () => {};

const EXPORT_SECTION_SRC = readFileSync(new URL("./export-section.tsx", import.meta.url), "utf8");

/** How many times `needle` occurs in `haystack` — "rendered once, not twice"
 * is a claim about a COUNT, and `toContain` cannot express it. */
function occurrences(haystack: string, needle: string): number {
	return haystack.split(needle).length - 1;
}

/** A uuid no fixture carries, for the "a typed value is not evidence" guard. */
const UNMATCHED_UUID = "3f1b9c7e-0000-4000-8000-2d5a6b7c8d9e";

function exportJob(overrides: Partial<AdminTenantExportListItem> = {}): AdminTenantExportListItem {
	return {
		job_id: "00000000-0000-0000-0000-0000000000e1",
		status: "complete",
		created_at: "2026-09-05T12:00:00Z",
		completed_at: "2026-09-05T12:04:00Z",
		include_vault_values: false,
		download_url: null,
		downloadable: true,
		error: null,
		...overrides,
	};
}

function tenant(overrides: Partial<AdminUserListItem> = {}): AdminUserListItem {
	return {
		id: "00000000-0000-0000-0000-0000000000a1",
		email: "tenant@example.com",
		// Deliberately unlike `id`, so no test can confuse the two identifiers.
		clerk_id: "user_2fTenantFixture",
		name: null,
		created_at: "2026-09-01T09:00:00Z",
		billing_exempt: false,
		...overrides,
	};
}

function renderPicker(
	overrides: Partial<React.ComponentProps<typeof TenantPickerView>> = {},
): string {
	return renderToStaticMarkup(
		<TenantPickerView
			selected={null}
			onSelect={noop}
			searchLabel={TENANT_SEARCH_LABEL}
			term=""
			onTermChange={noop}
			results={[]}
			isLoading={false}
			error={null}
			onRetry={noop}
			{...overrides}
		/>,
	);
}

function renderRow(
	job: AdminTenantExportListItem,
	rowState: ExportJobRowState,
	overrides: Partial<React.ComponentProps<typeof ExportJobRow>> = {},
): string {
	return renderToStaticMarkup(
		<ExportJobRow
			job={job}
			rowState={rowState}
			onExportAgain={noop}
			onDownload={noop}
			{...overrides}
		/>,
	);
}

/** Flatten an element tree's text, so a control can be found by its label. */
function textOf(node: unknown): string {
	if (typeof node === "string" || typeof node === "number") return String(node);
	if (Array.isArray(node)) return node.map(textOf).join("");
	if (isValidElement(node)) return textOf((node.props as { children?: unknown }).children);
	return "";
}

/**
 * Every clickable in a rendered tree, with the handler the component actually
 * built. Static markup cannot be clicked, and the defect this proves lives in
 * the ARGUMENT a handler passes — so the handler itself has to be invoked.
 */
function clickables(
	node: unknown,
	out: { label: string; onClick: (...args: never[]) => void }[] = [],
): { label: string; onClick: (...args: never[]) => void }[] {
	if (Array.isArray(node)) {
		for (const child of node) clickables(child, out);
		return out;
	}
	if (!isValidElement(node)) return out;
	const props = node.props as { onClick?: (...args: never[]) => void; children?: ReactNode };
	if (typeof props.onClick === "function") {
		out.push({ label: textOf(props.children), onClick: props.onClick });
	}
	clickables(props.children, out);
	return out;
}

/** The text of every status chip in a rendered markup string. */
function statusChips(markup: string): string[] {
	return [...markup.matchAll(/<span[^>]*data-slot="status-badge"[^>]*>([^<]*)<\/span>/g)].map(
		(match) => match[1] as string,
	);
}

/** The class list on the row's status-message element, whatever branch rendered it. */
function messageClass(markup: string): string {
	const el = /<span[^>]*data-slot="export-job-message"[^>]*>/.exec(markup)?.[0];
	if (el === undefined) throw new Error(`no export-job-message element in: ${markup}`);
	const cls = /class="([^"]*)"/.exec(el)?.[1];
	if (cls === undefined) throw new Error(`export-job-message carries no class: ${el}`);
	return cls;
}

/** Stub `fetch` for one call and hand back the URL the client asked for. */
async function captureFetch<T>(
	body: unknown,
	run: () => Promise<T>,
): Promise<{ url: string; result: T }> {
	const urls: string[] = [];
	const original = globalThis.fetch;
	globalThis.fetch = (async (input: RequestInfo | URL) => {
		urls.push(String(input));
		return new Response(JSON.stringify(body), {
			status: 200,
			headers: { "content-type": "application/json" },
		});
	}) as typeof fetch;
	try {
		const result = await run();
		return { url: urls[0] as string, result };
	} finally {
		globalThis.fetch = original;
	}
}

describe("tenant picker addresses a tenant by the identifier an operator holds (W-05)", () => {
	test("the search field names email AND user id in its placeholder and its accessible name", () => {
		const markup = decode(renderPicker());
		expect(TENANT_SEARCH_LABEL).toContain("user id");
		expect(TENANT_SEARCH_PLACEHOLDER).toContain("user id");
		expect(markup).toContain(`placeholder="${TENANT_SEARCH_PLACEHOLDER}"`);
		expect(markup).toContain(`aria-label="${TENANT_SEARCH_LABEL}"`);
		// The old email-only affordance is gone from both.
		expect(markup).not.toContain('placeholder="Search tenants by email…"');
	});

	test("the zero-result sentence names both identifiers, not email alone", () => {
		const markup = decode(renderPicker({ term: "no-such-tenant" }));
		expect(markup).toContain(TENANT_SEARCH_EMPTY_COPY);
		expect(TENANT_SEARCH_EMPTY_COPY).toContain("user id");
		expect(markup).not.toContain("No tenants match this email.");
	});

	test("lookupUsers sends the identifier-agnostic q parameter (03-16 contract)", async () => {
		const { url } = await captureFetch({ items: [] }, () => lookupUsers("00000000-dead-beef"));
		expect(url).toContain("q=00000000-dead-beef");
		expect(url).not.toContain("email=");
	});
});

/**
 * The picker's half of G-03-62.
 *
 * Every "the operator can see this" assertion below runs against
 * `renderedText`, never raw markup. The test this block replaces — "an
 * email-less tenant renders its id" — passed the tenant's OWN id as the search
 * term and asserted `markup.toContain(id)`, which the `<input value={term}>`
 * attribute satisfies with `results: []`. It was green while the capability it
 * named did not exist, which is precisely the failure this gap is about.
 */
describe("the picker shows the identifier AND the email, never one instead of the other", () => {
	test("REPAIRED: an email-less tenant's id is read out of the ROW, with a term that cannot echo it", () => {
		const emailLess = tenant({ email: null, name: "Partner Co" });
		// The term shares no substring with the id, so the search box cannot
		// supply the value — and `renderedText` has no attributes at all.
		const text = renderedText(renderPicker({ term: "Partner", results: [emailLess] }));
		expect(text).toContain(emailLess.id);
	});

	test("THE GUARD TWIN: a uuid typed into an empty result set is not a rendered identifier", () => {
		const markup = renderPicker({ term: UNMATCHED_UUID, results: [] });
		// What the repaired assertion says: nothing was displayed.
		expect(renderedText(markup)).not.toContain(UNMATCHED_UUID);
		expect(renderedText(markup)).toContain(TENANT_SEARCH_EMPTY_COPY);
		// What the OLD assertion said about the very same render — the false
		// green, kept here so the lesson cannot be deleted by accident.
		expect(markup).toContain(UNMATCHED_UUID);
	});

	test("a tenant WITH an email renders both identifiers in the result row", () => {
		const withEmail = tenant();
		const text = renderedText(renderPicker({ term: "tenant@", results: [withEmail] }));
		expect(text).toContain(withEmail.email as string);
		expect(text).toContain(withEmail.id);
	});

	test("a tenant WITH an email renders both identifiers in the selected chip, and the clerk id", () => {
		const withEmail = tenant();
		const text = renderedText(renderPicker({ selected: withEmail }));
		expect(text).toContain(withEmail.email as string);
		expect(text).toContain(withEmail.id);
		// The third identifier `admin_lookup_users` accepts, put on the wire by
		// plan 03-20 — obtainable only if something renders it.
		expect(text).toContain(withEmail.clerk_id as string);
	});

	test("one identifier is rendered ONCE: an email-less chip does not repeat its id", () => {
		const emailLess = tenant({ email: null, clerk_id: null });
		const text = renderedText(renderPicker({ selected: emailLess }));
		expect(occurrences(text, emailLess.id)).toBe(1);
	});

	test("a partner tenant's null clerk id renders nothing, never the word null", () => {
		// `clerk_id` is `string | null` and the users table CONSTRAINS it to NULL
		// for `principal_kind = 'partner_tenant'` — one of the two populations
		// this gap exists to reach (03-20 deviation 1).
		const partner = tenant({ email: null, clerk_id: null });
		const text = renderedText(renderPicker({ selected: partner }));
		expect(text).toContain(partner.id);
		expect(text).not.toContain("null");
	});

	test("every rendered identifier carries its own copy affordance, named for what it copies", () => {
		// Count the ACCESSIBLE NAME, not the raw label string. The label appears
		// twice per affordance — once as `title` (the pointer tooltip) and once
		// as `aria-label` (what assistive tech announces, WR-02) — so counting
		// the bare string counts attributes rather than affordances.
		const named = (markup: string, label: string) => occurrences(markup, `aria-label="${label}"`);

		const markup = renderPicker({ selected: tenant() });
		expect(named(markup, COPY_IDENTIFIER_TITLE)).toBe(1);
		expect(named(markup, COPY_CLERK_IDENTIFIER_TITLE)).toBe(1);
		// An email-less chip carries one identifier and one affordance.
		const emailLess = renderPicker({ selected: tenant({ email: null, clerk_id: null }) });
		expect(named(emailLess, COPY_IDENTIFIER_TITLE)).toBe(1);
		expect(named(emailLess, COPY_CLERK_IDENTIFIER_TITLE)).toBe(0);
	});

	test("truncation survives the second identifier — a uuid beside a long email cannot break the row", () => {
		expect(EXPORT_SECTION_SRC).toContain("min-w-0 flex-1 truncate");
		expect(EXPORT_SECTION_SRC).toContain("max-w-72 truncate");
		// The fallback that made display and discovery mutually exclusive.
		expect(EXPORT_SECTION_SRC).not.toContain("selected.email ?? selected.id");
	});

	test("the copy affordance is never nested inside the row's own select button", () => {
		// `CopyIdentifier` renders a <button>; a <button> inside a <button> is
		// invalid HTML and React refuses to hydrate it. The result row's select
		// control and the identifier chip are therefore SIBLINGS.
		expect(EXPORT_SECTION_SRC).not.toMatch(
			/<button[^>]*onClick=\{\(\) => onSelect\(user\)\}[\s\S]*?<CopyIdentifier[\s\S]*?<\/button>/,
		);
	});
});

describe("the Download control asks for a link at click time (W-03 web half, 03-18)", () => {
	test("downloadTenantExport calls the 03-18 endpoint and returns its url and ttl", async () => {
		const { url, result } = await captureFetch(
			{ job_id: "job-1", download_url: "https://example.test/artifact", expires_in_seconds: 3600 },
			() => downloadTenantExport("user-1", "job-1"),
		);
		expect(url).toContain("/v1/admin/tenants/user-1/exports/job-1/download");
		expect(result.download_url).toBe("https://example.test/artifact");
		expect(result.expires_in_seconds).toBe(3600);
	});

	test("the complete branch renders a button, never an href read off the list item", () => {
		const markup = decode(renderRow(exportJob(), "complete"));
		expect(markup).toContain("Download export");
		expect(markup).toContain(`expires in ${EXPORT_LINK_TTL_LABEL}`);
		// The list no longer carries a URL — nothing in this branch may link out.
		expect(markup).not.toContain("<a ");
		expect(markup).not.toContain("href=");
		expect(markup).not.toContain("download_url");
		// The source must not read the retired field at all.
		expect(EXPORT_SECTION_SRC).not.toContain("job.download_url");
	});

	test("the vault-secrets tooltip still rides a job carrying the flag", () => {
		const markup = decode(renderRow(exportJob({ include_vault_values: true }), "complete"));
		expect(markup).toContain("Includes vault secrets");
	});

	test("row state comes from `downloadable`, not from a download_url that is always null", () => {
		expect(exportJobRowState(exportJob({ status: "queued", downloadable: false }))).toBe(
			"in-progress",
		);
		expect(exportJobRowState(exportJob({ status: "running", downloadable: false }))).toBe(
			"in-progress",
		);
		expect(exportJobRowState(exportJob({ status: "failed", downloadable: false }))).toBe("failed");
		expect(exportJobRowState(exportJob({ status: "complete", downloadable: true }))).toBe(
			"complete",
		);
		// A complete job the backend will refuse (409) must not offer an enabled control.
		expect(exportJobRowState(exportJob({ status: "complete", downloadable: false }))).toBe(
			"link-expired",
		);
	});

	test("a complete-but-not-downloadable job renders the expired branch, not the control", () => {
		const markup = decode(renderRow(exportJob({ downloadable: false }), "link-expired"));
		expect(markup).toContain("Link expired");
		expect(markup).not.toContain("Download export");
	});
});

describe("one row carries one type rhythm (UI-REVIEW Pillar 4 / Pillar 5)", () => {
	test("all four peer status messages render at the same size", () => {
		const states: ExportJobRowState[] = ["in-progress", "complete", "failed", "link-expired"];
		const classes = states.map((state) =>
			messageClass(renderRow(exportJob({ error: "boom" }), state)),
		);
		// Compared as rendered classes, not read off the source.
		expect(new Set(classes).size).toBe(1);
		expect(classes[0]).toContain("text-xs");
		expect(classes[0]).not.toContain("text-sm");
	});

	test("no spacing value in export-section.tsx computes to 2px or 10px", () => {
		expect(EXPORT_SECTION_SRC).not.toContain("py-2.5");
		expect(EXPORT_SECTION_SRC).not.toContain("space-y-0.5");
		// The only half-step spacing utility left is the shadcn label/input
		// step, which Task 2 declares as a named exception in the UI-SPEC.
		const halfSteps = new Set(
			[
				...EXPORT_SECTION_SRC.matchAll(
					/\b(?:p|px|py|pt|pb|pl|pr|m|mx|my|mt|mb|ml|mr|gap|gap-x|gap-y|space-x|space-y)-\d*\.5\b/g,
				),
			].map((match) => match[0]),
		);
		expect([...halfSteps]).toEqual(["space-y-1.5"]);
	});
});

describe("re-running an export carries THAT job's vault flag, in both directions (WR-10)", () => {
	function reExportFlag(job: AdminTenantExportListItem, rowState: ExportJobRowState): boolean[] {
		const calls: boolean[] = [];
		const tree = ExportJobRow({
			job,
			rowState,
			onExportAgain: (includeVaultValues: boolean) => calls.push(includeVaultValues),
			onDownload: noop,
		});
		const control = clickables(tree).find((c) => /export/i.test(c.label));
		if (control === undefined) throw new Error("no re-export control in the rendered row");
		(control.onClick as () => void)();
		return calls;
	}

	test("a failed VAULT export re-runs with the vault flag, whatever the checkbox holds", () => {
		expect(
			reExportFlag(
				exportJob({ status: "failed", downloadable: false, include_vault_values: true }),
				"failed",
			),
		).toEqual([true]);
	});

	test("a failed NON-vault export never re-runs as a credentials bundle", () => {
		expect(
			reExportFlag(
				exportJob({ status: "failed", downloadable: false, include_vault_values: false }),
				"failed",
			),
		).toEqual([false]);
	});

	test("the link-expired branch carries the job's flag by the same mechanism", () => {
		expect(
			reExportFlag(exportJob({ downloadable: false, include_vault_values: true }), "link-expired"),
		).toEqual([true]);
		expect(
			reExportFlag(exportJob({ downloadable: false, include_vault_values: false }), "link-expired"),
		).toEqual([false]);
	});

	test("the trigger mutation takes its flag from its caller, not from component state", () => {
		expect(EXPORT_SECTION_SRC).toContain("variables.includeVaultValues");
		// The defect shape: the mutationFn closing over the checkbox.
		expect(EXPORT_SECTION_SRC).not.toMatch(/triggerTenantExport\([^)]*,\s*includeVaultValues\s*\)/);
		// The section's own button is unchanged: there the checkbox IS the intent.
		expect(EXPORT_SECTION_SRC).toContain("trigger.mutate({ includeVaultValues })");
	});
});

describe("the failed-export copy states what is actually true (WR-10)", () => {
	test("it says the job retries automatically, and offers a labelled NEW export", () => {
		const markup = decode(
			renderRow(
				exportJob({ status: "failed", downloadable: false, error: "s3: access denied" }),
				"failed",
			),
		);
		expect(markup).toContain("retries automatically");
		expect(markup).toContain("s3: access denied");
		// `record_tenant_export_failure` sets next_attempt_at and
		// `claim_tenant_export` re-claims it, so this promise was false.
		expect(markup).not.toContain("The job can be retried without losing anything.");
		expect(markup).not.toContain("Retry export");
		expect(markup).toContain(EXPORT_AGAIN_WITHOUT_VAULT_LABEL);
	});

	test("the action label names the vault-secrets setting the new export will carry", () => {
		const vault = decode(
			renderRow(
				exportJob({ status: "failed", downloadable: false, include_vault_values: true }),
				"failed",
			),
		);
		expect(vault).toContain(EXPORT_AGAIN_WITH_VAULT_LABEL);
		expect(vault).not.toContain(EXPORT_AGAIN_WITHOUT_VAULT_LABEL);
	});

	test("a failed job with no recorded error renders the bare sentence", () => {
		const markup = decode(
			renderRow(exportJob({ status: "failed", downloadable: false, error: null }), "failed"),
		);
		expect(markup).toContain(EXPORT_FAILED_COPY);
	});
});

describe("no export chip renders an API enum (UI-REVIEW 5)", () => {
	test("every status renders its Title-Cased label, and the record is exhaustive", () => {
		for (const [status, label] of Object.entries(EXPORT_STATUS_LABEL)) {
			const job = exportJob({ status: status as AdminTenantExportListItem["status"] });
			const chips = statusChips(renderRow(job, exportJobRowState(job)));
			expect(chips).toEqual([label]);
			expect(chips).not.toContain(status);
		}
		expect(Object.keys(EXPORT_STATUS_LABEL).sort()).toEqual([
			"complete",
			"failed",
			"queued",
			"running",
		]);
	});
});

describe("code and contract agree — read from both sides, never from a diff", () => {
	test("the failed-export copy and its two action labels equal their contract rows", () => {
		expect(uiSpecCopy("S3 export failed")).toBe(EXPORT_FAILED_COPY);
		// Interpolated rows are compared as TEMPLATES, never as a rendered value.
		expect(uiSpecCopy("S3 export failed — with recorded error")).toBe(
			EXPORT_FAILED_WITH_ERROR_COPY,
		);
		expect(uiSpecCopy("S3 export failed action — vault secrets on")).toBe(
			EXPORT_AGAIN_WITH_VAULT_LABEL,
		);
		expect(uiSpecCopy("S3 export failed action — vault secrets off")).toBe(
			EXPORT_AGAIN_WITHOUT_VAULT_LABEL,
		);
	});

	test("every export status chip label equals its contract row", () => {
		for (const [status, label] of Object.entries(EXPORT_STATUS_LABEL)) {
			expect(uiSpecCopy(`S3 export status chip — ${status}`)).toBe(label);
		}
	});

	test("both copy affordances name the identifier they actually copy", () => {
		expect(uiSpecCopy("S3 invite table — copy identifier affordance")).toBe(COPY_IDENTIFIER_TITLE);
		expect(uiSpecCopy("S3 invite table — identifier copied toast")).toBe(IDENTIFIER_COPIED_TOAST);
		// A clerk id copied under a "Copy user id" label is this plan's own
		// defect — an instruction naming a value it does not act on.
		expect(uiSpecCopy("S3 tenant picker — copy clerk id affordance")).toBe(
			COPY_CLERK_IDENTIFIER_TITLE,
		);
		expect(uiSpecCopy("S3 tenant picker — clerk id copied toast")).toBe(
			CLERK_IDENTIFIER_COPIED_TOAST,
		);
	});

	test("the picker's own contract row requires the identifier to be displayed, not just searched", () => {
		const spec = readFileSync(
			new URL(
				"../../../../../../.planning/phases/03-invite-gated-access-tenant-data-rights/03-UI-SPEC.md",
				import.meta.url,
			),
			"utf8",
		);
		// The S3 Export bullet has to demand the display, or the next reader can
		// re-derive `email ?? id` as an improvement (G-03-62).
		expect(spec).toContain("DISPLAYS the tenant id alongside the email");
	});
});

describe("an empty-string email is not an email, in the picker too (W-13)", () => {
	test("a result row with an empty email renders its identifier, never a blank label", () => {
		// W-13: WR-06 fixed two of the three disagreeing predicates. This one
		// survived: the row label used `email ?? id` ("" passes through `??`)
		// while the copy affordance used truthiness ("" is falsy), so for an
		// empty-string email NEITHER rendered — the row showed nothing at all.
		// That is G-03-62's own defect inside the picker built to close it.
		const blank = tenant({ email: "", name: null });
		// A term that shares no substring with the id, so the search box cannot
		// supply the value — the same anti-echo discipline as the block above.
		const text = renderedText(renderPicker({ term: "Partner", results: [blank] }));
		expect(text).toContain(blank.id);
	});

	test("the identifier really does always render, across all three email shapes", () => {
		for (const email of [null, "", "someone@example.com"]) {
			const row = tenant({ email, name: null });
			const text = renderedText(renderPicker({ term: "Partner", results: [row] }));
			expect(text).toContain(row.id);
		}
	});

	test("an empty email never renders as the row's label", () => {
		const blank = tenant({ email: "", name: null });
		const markup = renderPicker({ term: "Partner", results: [blank] });
		// Guard the guard: this assertion is a `not.toContain`, which an empty
		// render satisfies vacuously. Prove the row rendered at all first.
		expect(renderedText(markup)).toContain(blank.id);
		expect(markup).not.toContain('<span class="min-w-0 flex-1 truncate font-mono text-xs"></span>');
	});
});
