/**
 * Held-out UI-state tests for S5 — uninstall with the export offer (APP-07).
 * One or more named tests per `06-UI-SPEC.md` §UI Considerations row for S5.
 *
 * **SCOPE LIMIT.** No DOM library: the body renders through
 * `renderToStaticMarkup` inside an `AlertDialog` root (the exact element
 * `UninstallCompanionDialog` mounts, less the portal), and the three writes
 * run through `runExportFirst` / `runUninstall` / `settleUninstall`, the exact
 * functions the buttons call, over a REAL generated client with a fake
 * `fetch`. Nothing is clicked.
 *
 * The S5 rows in §Destructive confirmations are asserted literally here.
 */

import { describe, expect, test } from "bun:test";
import type { paths } from "@clawdi/shared/api";
import createClient from "openapi-fetch";
import { renderToStaticMarkup } from "react-dom/server";
import { AlertDialog } from "@/components/ui/alert-dialog";
import { renderedText } from "@/lib/rendered-text";
import {
	companionRequests,
	runExportFirst,
	runUninstall,
	settleUninstall,
	UNINSTALL_COPY,
	uninstallConfirmMatches,
} from "./companion-detail.logic";
import {
	UninstallCompanionDialogBody,
	type UninstallCompanionDialogBodyProps,
} from "./uninstall-companion-dialog";

const INSTALLATION_ID = "0b7e2d1c-0000-4000-8000-000000000002";

function render(overrides: Partial<UninstallCompanionDialogBodyProps> = {}): string {
	return renderToStaticMarkup(
		<AlertDialog open>
			<UninstallCompanionDialogBody name="Postiz" typed="" {...overrides} />
		</AlertDialog>,
	);
}

/** The FIRST element carrying `data-slot="name"`, sliced to its matching close tag. */
function slot(markup: string, name: string): string {
	const attribute = markup.indexOf(`data-slot="${name}"`);
	if (attribute === -1) return "";
	const start = markup.lastIndexOf("<", attribute);
	const tag = /^<([a-z0-9]+)/.exec(markup.slice(start))?.[1];
	if (!tag) return "";
	if (tag === "input") return markup.slice(start, markup.indexOf(">", attribute) + 1);
	const pattern = new RegExp(`<${tag}[\\s>]|</${tag}>`, "g");
	pattern.lastIndex = start;
	let depth = 0;
	for (let match = pattern.exec(markup); match; match = pattern.exec(markup)) {
		depth += match[0].startsWith("</") ? -1 : 1;
		if (depth === 0) return markup.slice(start, match.index + match[0].length);
	}
	return markup.slice(start);
}

function isDisabled(button: string): boolean {
	return /^<button[^>]*\sdisabled[\s=>]/.test(button);
}

type Captured = { method: string; url: string; body: unknown };

function fakeApi(respond: (request: Captured) => { status: number; body: unknown }) {
	const captured: Captured[] = [];
	const client = createClient<paths>({
		baseUrl: "http://api.test",
		fetch: async (input: Request) => {
			const text = await input.text();
			const request = {
				method: input.method,
				url: input.url,
				body: text === "" ? null : JSON.parse(text),
			};
			captured.push(request);
			const { status, body } = respond(request);
			return new Response(JSON.stringify(body), {
				status,
				headers: { "content-type": "application/json" },
			});
		},
	});
	return { client, captured };
}

describe("S5 Uninstall — the destructive confirmation row", () => {
	test("populated: title, description, typed prompt and confirm label are the contract's", () => {
		const markup = render();
		const text = renderedText(markup);
		expect(text).toContain("Uninstall Postiz");
		expect(renderedText(slot(markup, "uninstall-description"))).toBe(
			[
				"This permanently removes Postiz, its database and its uploaded media. Your backups are kept to their normal schedule but this companion's live data is gone. Billing for it stops today.",
				"Export your data first — you cannot export it after this.",
				"Type Postiz to confirm.",
			].join(" "),
		);
		// The export sentence is the emphasised one.
		expect(slot(markup, "uninstall-export-sentence")).toContain("font-semibold");
		expect(renderedText(slot(markup, "uninstall-confirm"))).toBe("Uninstall and delete data");
		expect(slot(markup, "uninstall-confirm")).toContain("bg-destructive/10 text-destructive");
		expect(renderedText(slot(markup, "uninstall-export"))).toBe("Export data first");
	});
});

describe("S5 Uninstall — UI considerations", () => {
	test("empty: with nothing typed confirm is disabled and the export offer is enabled", () => {
		const markup = render({ typed: "" });
		expect(isDisabled(slot(markup, "uninstall-confirm"))).toBe(true);
		expect(isDisabled(slot(markup, "uninstall-export"))).toBe(false);
	});

	test("partial: confirm stays disabled for 'postiz' (case differs) and a prefix, and enables for '  Postiz  '", () => {
		for (const typed of ["postiz", "Post", "POSTIZ", "Postiz!", "P ostiz"]) {
			expect(uninstallConfirmMatches(typed, "Postiz")).toBe(false);
			expect(isDisabled(slot(render({ typed }), "uninstall-confirm"))).toBe(true);
			// The offer is not gated by the typed name.
			expect(isDisabled(slot(render({ typed }), "uninstall-export"))).toBe(false);
		}
		for (const typed of ["Postiz", "  Postiz  ", "\tPostiz\n"]) {
			expect(uninstallConfirmMatches(typed, "Postiz")).toBe(true);
			expect(isDisabled(slot(render({ typed }), "uninstall-confirm"))).toBe(false);
		}
	});

	test("empty/populated: an untaken export offer is no gate — a matching name alone enables confirm", () => {
		const markup = render({ typed: "Postiz", exportState: "idle" });
		expect(isDisabled(slot(markup, "uninstall-confirm"))).toBe(false);
		expect(isDisabled(slot(markup, "uninstall-export"))).toBe(false);
	});

	test("loading (amended, CR-05): starting the export holds confirm disabled for the rest of the open and swaps the export sentence", async () => {
		const pendingMarkup = render({ typed: "Postiz", exportState: "pending" });
		expect(isDisabled(slot(pendingMarkup, "uninstall-confirm"))).toBe(true);
		expect(slot(pendingMarkup, "uninstall-export")).toContain('aria-busy="true"');

		const started = render({ typed: "Postiz", exportState: "started" });
		expect(isDisabled(slot(started, "uninstall-confirm"))).toBe(true);
		expect(isDisabled(slot(started, "uninstall-export"))).toBe(true);
		expect(renderedText(slot(started, "uninstall-export-sentence"))).toBe(
			"Your export is being prepared. You can uninstall once it is ready to download from Settings → Data.",
		);
		expect(renderedText(slot(started, "uninstall-export-sentence"))).toBe(
			UNINSTALL_COPY.exportPreparing,
		);
		expect(renderedText(started)).not.toContain(UNINSTALL_COPY.exportFirst);

		// The offer is the tenant export route, and it toasts "Export started".
		const { client, captured } = fakeApi(() => ({
			status: 202,
			body: { job_id: "4f1d2c3b-0000-4000-8000-000000000001", status: "queued" },
		}));
		const toasts: string[] = [];
		const outcome = await runExportFirst({
			requestExport: companionRequests(client, INSTALLATION_ID).startExport,
			onStarted: () => toasts.push(UNINSTALL_COPY.exportStartedToast),
		});
		expect(outcome).toEqual({ ok: true });
		expect(captured).toEqual([
			{ method: "POST", url: "http://api.test/v1/me/exports", body: null },
		]);
		expect(toasts).toEqual(["Export started"]);
	});

	test("loading: confirm holds the dialog with a spinner and cannot be dismissed while in flight", () => {
		const markup = render({ typed: "Postiz", pending: true });
		const confirm = slot(markup, "uninstall-confirm");
		expect(confirm).toContain('aria-busy="true"');
		expect(confirm).toContain('data-slot="spinner"');
		expect(isDisabled(confirm)).toBe(true);
		expect(markup).toMatch(
			/data-slot="alert-dialog-cancel"[^>]*disabled|disabled[^>]*data-slot="alert-dialog-cancel"/,
		);
	});

	test("populated: confirm sends the typed name (trimmed) to this installation's uninstall route and toasts", async () => {
		const { client, captured } = fakeApi(() => ({ status: 200, body: {} }));
		const toasts: string[] = [];
		const outcome = await runUninstall({
			typed: "  Postiz ",
			name: "Postiz",
			requestUninstall: companionRequests(client, INSTALLATION_ID).uninstall,
			onStarted: () => toasts.push(UNINSTALL_COPY.uninstallStartedToast),
		});
		expect(outcome).toEqual({ kind: "started" });
		expect(captured).toEqual([
			{
				method: "POST",
				url: `http://api.test/v1/companions/installations/${INSTALLATION_ID}/uninstall`,
				body: { confirm: "Postiz" },
			},
		]);
		expect(toasts).toEqual(["Uninstall started"]);
	});

	test("partial: a mismatched name sends nothing", async () => {
		const { client, captured } = fakeApi(() => ({ status: 200, body: {} }));
		const outcome = await runUninstall({
			typed: "postiz",
			name: "Postiz",
			requestUninstall: companionRequests(client, INSTALLATION_ID).uninstall,
			onStarted: () => undefined,
		});
		expect(outcome.kind).toBe("rejected");
		expect(captured).toEqual([]);
	});

	test("error: a 409 closes the dialog and refetches, with no success toast", async () => {
		const { client } = fakeApi(() => ({
			status: 409,
			body: { detail: "companion_operation_in_progress" },
		}));
		const calls: string[] = [];
		const outcome = await runUninstall({
			typed: "Postiz",
			name: "Postiz",
			requestUninstall: companionRequests(client, INSTALLATION_ID).uninstall,
			onStarted: () => calls.push("toast"),
		});
		expect(outcome).toEqual({ kind: "conflict" });
		await settleUninstall(outcome, {
			close: () => calls.push("close"),
			refetch: () => calls.push("refetch"),
			showRejection: (message) => calls.push(`inline:${message}`),
		});
		expect(calls).toEqual(["refetch", "close"]);
	});

	test("error (amended, CR-05): an uninstall refused while an export is in progress renders inline and keeps the dialog open", async () => {
		const { client, captured } = fakeApi(() => ({
			status: 409,
			body: {
				detail: {
					code: "companion_export_in_progress",
					message:
						"A data export that includes this companion is still being prepared. Uninstall it once the export is ready to download.",
				},
			},
		}));
		const calls: string[] = [];
		const outcome = await runUninstall({
			typed: "Postiz",
			name: "Postiz",
			requestUninstall: companionRequests(client, INSTALLATION_ID).uninstall,
			onStarted: () => calls.push("toast"),
		});
		expect(captured).toHaveLength(1);
		const sentence =
			"An export that includes Postiz's data is still being prepared. You can uninstall once it is ready to download from Settings → Data.";
		expect(outcome).toEqual({ kind: "rejected", message: sentence });
		await settleUninstall(outcome, {
			close: () => calls.push("close"),
			refetch: () => calls.push("refetch"),
			showRejection: (message) => calls.push(`inline:${message}`),
		});
		// Not closed, not refetched, no toast: the sentence stays in the still-open dialog.
		expect(calls).toEqual([`inline:${sentence}`]);

		const markup = render({ typed: "Postiz", rejection: sentence });
		expect(slot(markup, "uninstall-rejection")).toContain('role="alert"');
		expect(renderedText(slot(markup, "uninstall-rejection"))).toBe(sentence);
	});

	test("error: a 409 for another running operation (object detail) still closes and refetches", async () => {
		const { client } = fakeApi(() => ({
			status: 409,
			body: {
				detail: {
					code: "companion_operation_in_progress",
					message: "The uninstall operation is still in progress; try again when it ends.",
				},
			},
		}));
		const outcome = await runUninstall({
			typed: "Postiz",
			name: "Postiz",
			requestUninstall: companionRequests(client, INSTALLATION_ID).uninstall,
			onStarted: () => undefined,
		});
		expect(outcome).toEqual({ kind: "conflict" });
	});

	test("error: the export and uninstall refusals render inline in the still-open dialog, never as a toast", async () => {
		const refused = fakeApi(() => ({ status: 500, body: { detail: "internal" } }));
		const toasts: string[] = [];
		const exportOutcome = await runExportFirst({
			requestExport: companionRequests(refused.client, INSTALLATION_ID).startExport,
			onStarted: () => toasts.push("toast"),
		});
		expect(exportOutcome).toEqual({
			ok: false,
			message: "The export was not started. Nothing has been removed.",
		});
		const uninstallOutcome = await runUninstall({
			typed: "Postiz",
			name: "Postiz",
			requestUninstall: companionRequests(refused.client, INSTALLATION_ID).uninstall,
			onStarted: () => toasts.push("toast"),
		});
		const calls: string[] = [];
		await settleUninstall(uninstallOutcome, {
			close: () => calls.push("close"),
			refetch: () => calls.push("refetch"),
			showRejection: (message) => calls.push(message),
		});
		expect(calls).toEqual(["The uninstall was not started. Postiz is still running."]);
		expect(toasts).toEqual([]);

		const markup = render({
			typed: "Postiz",
			exportError: UNINSTALL_COPY.exportRejected,
			rejection: "The uninstall was not started. Postiz is still running.",
		});
		expect(slot(markup, "uninstall-export-error")).toContain('role="alert"');
		expect(renderedText(slot(markup, "uninstall-export-error"))).toBe(
			"The export was not started. Nothing has been removed.",
		);
		expect(renderedText(slot(markup, "uninstall-rejection"))).toBe(
			"The uninstall was not started. Postiz is still running.",
		);
	});

	test("overflow: the export offer is a ghost button left of the footer, subordinate to Cancel", () => {
		const markup = render({ typed: "Postiz" });
		const footer = slot(markup, "alert-dialog-footer");
		const exportAt = footer.indexOf('data-slot="uninstall-export"');
		const cancelAt = footer.indexOf('data-slot="alert-dialog-cancel"');
		const confirmAt = footer.indexOf('data-slot="uninstall-confirm"');
		expect(exportAt).toBeGreaterThan(-1);
		expect(exportAt).toBeLessThan(cancelAt);
		expect(cancelAt).toBeLessThan(confirmAt);
		const exportButton = slot(markup, "uninstall-export");
		expect(exportButton).toContain("sm:mr-auto");
		expect(exportButton).toContain("hover:bg-muted");
		expect(exportButton).not.toContain("bg-primary");
		expect(exportButton).not.toContain("border-border bg-background");
		expect(exportButton).not.toContain("text-destructive");
	});

	test("long-text: the only interpolated value is the catalog name, rendered in mono in the prompt", () => {
		const markup = render({ name: "Postiz" });
		const description = slot(markup, "uninstall-description");
		expect(description).toContain('<span class="font-mono text-foreground">Postiz</span>');
		// Remove every occurrence of the name: what is left is fixed copy.
		const fixed = renderedText(render({ name: "X" }));
		expect(fixed.replaceAll("X", "Postiz")).toBe(renderedText(markup));
	});
});
