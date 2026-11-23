/**
 * Settings → Data — tenant self-export (APP-07, D-31) over plan 06-19's
 * `/v1/me/exports` routes.
 *
 * **SCOPE LIMIT.** There is no DOM library here, so nothing is clicked. Every
 * state is rendered through `DataPanelView` (the exact element `DataPanel`
 * mounts), and the two actions through `runStartExport` / `runDownloadExport`
 * (the exact functions the Start export and Download buttons call), driven by
 * a REAL openapi-fetch client over a fake `fetch`, so the method and path the
 * panel sends are the ones asserted. What static markup cannot show — the
 * click itself — is the only thing not exercised.
 */

import { afterEach, describe, expect, spyOn, test } from "bun:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { paths } from "@clawdi/shared/api";
import createClient from "openapi-fetch";
import { isValidElement, type ReactElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ApiErrorPanel } from "@/components/api-error-panel";
import { renderedText } from "@/lib/rendered-text";
import { DataPanelView, type DataPanelViewProps } from "./data-panel";
import {
	DATA_PANEL_COPY,
	EXPORT_POLL_INTERVAL_MS,
	exportsRefetchInterval,
	runDownloadExport,
	runStartExport,
	type TenantExportListItem,
	tenantExportRequests,
} from "./data-panel.logic";

const JOB_ID = "4f1d2c3b-0000-4000-8000-000000000001";
const PRESIGNED_URL = "https://exports.example.test/tenant/archive.tar.gz?X-Amz-Signature=secret";

function job(overrides: Partial<TenantExportListItem> = {}): TenantExportListItem {
	return {
		job_id: JOB_ID,
		status: "queued",
		created_at: "2026-09-24T04:00:00Z",
		completed_at: null,
		downloadable: false,
		error: null,
		...overrides,
	};
}

function viewProps(overrides: Partial<DataPanelViewProps> = {}): DataPanelViewProps {
	return {
		items: [],
		isLoading: false,
		listError: null,
		onRetry: () => undefined,
		onStart: () => undefined,
		startPending: false,
		startError: null,
		onDownload: () => undefined,
		downloadingJobId: null,
		downloadError: null,
		...overrides,
	};
}

function render(overrides: Partial<DataPanelViewProps> = {}): string {
	return renderToStaticMarkup(<DataPanelView {...viewProps(overrides)} />);
}

/** The export list's markup alone — the rows, without the panel's intro and Start button. */
function exportList(markup: string): string {
	const start = markup.indexOf('<ul data-slot="export-list"');
	if (start === -1) return "";
	return markup.slice(start, markup.indexOf("</ul>", start) + "</ul>".length);
}

type Captured = { method: string; url: string };

/** A real generated client whose transport records each request and answers from `respond`. */
function fakeApi(respond: (request: Captured) => { status: number; body: unknown }) {
	const captured: Captured[] = [];
	const client = createClient<paths>({
		baseUrl: "http://api.test",
		fetch: async (input: Request) => {
			const request = { method: input.method, url: input.url };
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

describe("Settings → Data tracer", () => {
	test("tracer: a tenant starts an export, follows it from Preparing to Ready, and downloads it", async () => {
		// 1. Empty: the panel invites the first export.
		const empty = renderedText(render({ items: [] }));
		expect(empty).toContain(DATA_PANEL_COPY.sectionTitle);
		expect(empty).toContain(DATA_PANEL_COPY.intro);
		expect(empty).toContain(DATA_PANEL_COPY.startButton);
		expect(empty).toContain(DATA_PANEL_COPY.emptyTitle);

		// 2. Start export: one POST to the caller's own export route, then the toast.
		const { client, captured } = fakeApi(({ method, url }) =>
			method === "POST"
				? { status: 202, body: { job_id: JOB_ID, status: "queued" } }
				: url.endsWith(`/v1/me/exports/${JOB_ID}/download`)
					? { status: 200, body: { url: PRESIGNED_URL, expires_at: "2026-09-24T05:00:00Z" } }
					: { status: 404, body: { detail: "not found" } },
		);
		const requests = tenantExportRequests(client);
		const toasts: string[] = [];
		let refreshed = 0;
		const started = await runStartExport({
			requestStart: requests.start,
			onStarted: async () => {
				toasts.push(DATA_PANEL_COPY.startedToast);
				refreshed += 1;
			},
		});
		expect(started).toEqual({ ok: true });
		expect(captured).toEqual([{ method: "POST", url: "http://api.test/v1/me/exports" }]);
		expect(toasts).toEqual(["Export started"]);
		expect(refreshed).toBe(1);

		// 3. The refetched list: one job in progress, polled, with no Download.
		const preparing = [job({ status: "running" })];
		const preparingList = exportList(render({ items: preparing }));
		expect(renderedText(preparingList)).toContain("Preparing");
		expect(preparingList).not.toContain("<button");
		expect(exportsRefetchInterval(preparing)).toBe(EXPORT_POLL_INTERVAL_MS);

		// 4. Complete and downloadable: Ready, with Download, and polling stops.
		const ready = [
			job({ status: "complete", completed_at: "2026-09-24T04:05:00Z", downloadable: true }),
		];
		const readyList = exportList(render({ items: ready }));
		expect(renderedText(readyList)).toContain("Ready");
		expect(readyList).toContain("<button");
		expect(renderedText(readyList)).toContain(DATA_PANEL_COPY.download);
		expect(exportsRefetchInterval(ready)).toBe(false);

		// 5. Download: one GET to this job's download route, then the minted link is opened.
		const opened: string[] = [];
		const downloaded = await runDownloadExport({
			jobId: JOB_ID,
			requestDownload: requests.downloadLink,
			openUrl: (url) => opened.push(url),
		});
		expect(downloaded).toEqual({ ok: true });
		expect(captured[1]).toEqual({
			method: "GET",
			url: `http://api.test/v1/me/exports/${JOB_ID}/download`,
		});
		expect(opened).toEqual([PRESIGNED_URL]);
	});

	test("polling: the list polls every 5 seconds while any job is in progress and stops when none is", () => {
		expect(EXPORT_POLL_INTERVAL_MS).toBe(5_000);
		expect(exportsRefetchInterval(undefined)).toBe(false);
		expect(exportsRefetchInterval([])).toBe(false);
		expect(exportsRefetchInterval([job({ status: "queued" })])).toBe(5_000);
		expect(exportsRefetchInterval([job({ status: "failed" }), job({ status: "running" })])).toBe(
			5_000,
		);
		expect(
			exportsRefetchInterval([
				job({ status: "complete", downloadable: true }),
				job({ status: "failed", error: "boom" }),
				job({ status: "complete", downloadable: false }),
			]),
		).toBe(false);
	});

	test("error: a failed start renders the inline sentence beneath the button and no toast", async () => {
		const { client } = fakeApi(() => ({ status: 500, body: { detail: "internal" } }));
		const toasts: string[] = [];
		const outcome = await runStartExport({
			requestStart: tenantExportRequests(client).start,
			onStarted: () => {
				toasts.push(DATA_PANEL_COPY.startedToast);
			},
		});
		expect(outcome).toEqual({ ok: false, message: "The export was not started. Try again." });
		expect(toasts).toEqual([]);

		const markup = render({ startError: outcome.ok ? null : outcome.message });
		const text = renderedText(markup);
		expect(text).toContain("The export was not started. Try again.");
		// Beneath the button: the sentence follows the Start export control in document order.
		expect(markup.indexOf("The export was not started. Try again.")).toBeGreaterThan(
			markup.indexOf(DATA_PANEL_COPY.startButton),
		);
	});
});

/** Every element in a rendered-but-not-mounted tree (`DataPanelView` is hook-free, so it can be called). */
function elementsIn(node: ReactNode): ReactElement<Record<string, unknown>>[] {
	const found: ReactElement<Record<string, unknown>>[] = [];
	const walk = (current: ReactNode) => {
		if (Array.isArray(current)) {
			for (const child of current) walk(child);
			return;
		}
		if (!isValidElement<Record<string, unknown>>(current)) return;
		found.push(current);
		walk(current.props.children as ReactNode);
	};
	walk(node);
	return found;
}

/** The first <button> in the panel, whole — the Start export control. */
function firstButton(markup: string): { tag: string; text: string } {
	const start = markup.indexOf("<button");
	const end = markup.indexOf("</button>", start) + "</button>".length;
	return {
		tag: markup.slice(start, markup.indexOf(">", start) + 1),
		text: renderedText(markup.slice(start, end)),
	};
}

describe("Settings → Data states", () => {
	test("empty: the inset heading and body render and Start export stays enabled", () => {
		const markup = render({ items: [] });
		const text = renderedText(markup);
		expect(text).toContain("No exports yet");
		expect(text).toContain("Exports you start appear here.");
		expect(exportList(markup)).toBe("");
		const start = firstButton(markup);
		expect(start.text).toBe("Start export");
		expect(start.tag).not.toMatch(/\sdisabled=""/);
		// Positive control: the same probe does see the attribute while a start is in flight.
		expect(firstButton(render({ items: [], startPending: true })).tag).toMatch(/\sdisabled=""/);
	});

	test("loading: skeleton rows render in place of the list, with no empty copy", () => {
		const text = renderedText(render({ items: undefined, isLoading: true }));
		expect(text).toContain("Loading exports");
		expect(text).not.toContain("No exports yet");
	});

	test("error: a failed list query renders ApiErrorPanel whose retry refetches", () => {
		let refetches = 0;
		const props = viewProps({
			items: undefined,
			listError: new Error("network down"),
			onRetry: () => {
				refetches += 1;
			},
		});
		const text = renderedText(renderToStaticMarkup(<DataPanelView {...props} />));
		expect(text).toContain("Couldn't load exports");
		expect(text).toContain("Retry");
		expect(text).not.toContain("No exports yet");

		const panels = elementsIn(DataPanelView(props)).filter((el) => el.type === ApiErrorPanel);
		expect(panels).toHaveLength(1);
		(panels[0].props.onRetry as () => void)();
		expect(refetches).toBe(1);
	});

	test("in progress: a queued or running job reads Preparing, on the info tone, with no Download", () => {
		for (const status of ["queued", "running"] as const) {
			const list = exportList(render({ items: [job({ status })] }));
			expect(list).toContain('data-status="info"');
			expect(renderedText(list)).toContain("Preparing");
			expect(renderedText(list)).toContain("This can take a few minutes.");
			expect(list).not.toContain("<button");
		}
	});

	test("ready: a complete, downloadable job reads Ready, on the success tone, and its Download asks for that job", () => {
		const downloads: string[] = [];
		const props = viewProps({
			items: [job({ status: "complete", downloadable: true })],
			onDownload: (jobId) => downloads.push(jobId),
		});
		const list = exportList(renderToStaticMarkup(<DataPanelView {...props} />));
		expect(list).toContain('data-status="success"');
		expect(renderedText(list)).toContain("Ready");
		expect(renderedText(list)).toContain("Download");
		expect(list.match(/<button/g)).toHaveLength(1);

		const rows = elementsIn(DataPanelView(props)).filter(
			(el) => (el.props.job as TenantExportListItem | undefined)?.job_id === JOB_ID,
		);
		expect(rows).toHaveLength(1);
		(rows[0].props.onDownload as () => void)();
		expect(downloads).toEqual([JOB_ID]);
	});

	test("failed: a failed job reads Failed, on the destructive tone, with the failure copy and no raw error", () => {
		const generic = exportList(render({ items: [job({ status: "failed", error: null })] }));
		expect(generic).toContain('data-status="destructive"');
		expect(renderedText(generic)).toContain("Failed");
		expect(renderedText(generic)).toContain("Export failed. Start a new export to try again.");
		expect(generic).not.toContain("<button");

		// An exception string is operator detail (T-03-76): the tenant gets the sentence, not the trace.
		const internal =
			"EndpointConnectionError: Could not connect to https://internal-r2.example/bucket-7f3a";
		const leaked = renderedText(
			exportList(render({ items: [job({ status: "failed", error: internal })] })),
		);
		expect(leaked).toContain("Export failed. Start a new export to try again.");
		expect(leaked).not.toContain("internal-r2");

		// A named companion failure is explained, without its internal installation id.
		const timeout = renderedText(
			exportList(
				render({
					items: [
						job({
							status: "failed",
							error:
								"companion_export_component_timeout: installation 9d1e did not deliver its database within 30 minutes",
						}),
					],
				}),
			),
		);
		expect(timeout).toContain(
			"Export failed: a companion did not send its data in time. Start a new export to try again.",
		);
		expect(timeout).not.toContain("9d1e");

		const oversize = renderedText(
			exportList(
				render({
					items: [
						job({
							status: "failed",
							error:
								"companion_media_exceeds_export_bound: installation 9d1e holds 600000000 bytes of media",
						}),
					],
				}),
			),
		);
		expect(oversize).toContain(
			"Export failed: a companion's uploaded media is larger than an export can carry.",
		);
	});

	test("link expired: a complete job that is no longer downloadable reads Link expired, neutral, with no Download", () => {
		const list = exportList(render({ items: [job({ status: "complete", downloadable: false })] }));
		expect(list).toContain('data-status="neutral"');
		expect(renderedText(list)).toContain("Link expired");
		expect(renderedText(list)).toContain("Start a new export to download this data again.");
		expect(list).not.toContain("<button");
	});

	test("many: jobs render newest first and the list adds no second scroll region", () => {
		const ids = ["a", "b", "c", "d", "e", "f"].map(
			(letter) => `00000000-0000-4000-8000-00000000000${letter}`,
		);
		const items = [
			job({ job_id: ids[0], created_at: "2026-09-20T10:00:00Z", status: "complete" }),
			job({ job_id: ids[1], created_at: "2026-09-24T03:00:00Z", status: "running" }),
			job({ job_id: ids[2], created_at: "2026-09-22T09:00:00Z", status: "failed" }),
			job({
				job_id: ids[3],
				created_at: "2026-09-23T12:00:00Z",
				status: "complete",
				downloadable: true,
			}),
			job({ job_id: ids[4], created_at: "2026-09-21T08:00:00Z", status: "complete" }),
			job({ job_id: ids[5], created_at: "2026-09-24T04:30:00Z", status: "queued" }),
		];
		const list = exportList(render({ items }));
		const order = [...list.matchAll(/data-job-id="([^"]+)"/g)].map((match) => match[1]);
		expect(order).toEqual([ids[5], ids[1], ids[3], ids[2], ids[4], ids[0]]);
		expect(list).not.toMatch(/overflow-(y-)?(auto|scroll)/);
		expect(list).not.toMatch(/max-h-/);
	});

	describe("download link hygiene", () => {
		const restores: Array<() => void> = [];
		afterEach(() => {
			while (restores.length > 0) restores.pop()?.();
		});

		test("download: the minted URL is opened and never written to storage or the console", async () => {
			const written: string[] = [];
			const fakeStorage = {
				setItem: (key: string, value: string) => written.push(`${key}=${value}`),
				getItem: () => null,
				removeItem: () => undefined,
				clear: () => undefined,
				key: () => null,
				length: 0,
			};
			const globals = globalThis as Record<string, unknown>;
			for (const name of ["localStorage", "sessionStorage"]) {
				const previous = globals[name];
				globals[name] = fakeStorage;
				restores.push(() => {
					globals[name] = previous;
				});
			}
			const consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map((method) =>
				spyOn(console, method),
			);
			restores.push(() => {
				for (const spy of consoleSpies) spy.mockRestore();
			});

			const { client } = fakeApi(() => ({
				status: 200,
				body: { url: PRESIGNED_URL, expires_at: "2026-09-24T05:00:00Z" },
			}));
			const opened: string[] = [];
			const outcome = await runDownloadExport({
				jobId: JOB_ID,
				requestDownload: tenantExportRequests(client).downloadLink,
				openUrl: (url) => opened.push(url),
			});

			expect(opened).toEqual([PRESIGNED_URL]);
			expect(JSON.stringify(outcome)).not.toContain("X-Amz-Signature");
			expect(written).toEqual([]);
			for (const spy of consoleSpies) expect(spy).not.toHaveBeenCalled();
		});

		test("download: neither the panel nor its logic names a storage or logging sink", () => {
			for (const file of ["./data-panel.tsx", "./data-panel.logic.ts"]) {
				const source = readFileSync(fileURLToPath(new URL(file, import.meta.url)), "utf8");
				expect(source).not.toMatch(
					/localStorage|sessionStorage|indexedDB|document\.cookie|console\.|captureException/,
				);
			}
		});
	});
});
