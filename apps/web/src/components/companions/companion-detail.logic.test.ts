/**
 * S3 and S5 copy held against `06-UI-SPEC.md` §Copywriting Contract, and the
 * pure rules behind both surfaces.
 *
 * **Both sides are read.** Every shipped constant with quoted contract copy is
 * compared with the string its row quotes, read from the spec file at test
 * time through `ui-spec-contract.ts`, so editing either side by one character
 * fails here. A row that goes missing THROWS rather than comparing nothing to
 * nothing.
 *
 * Two recorded deltas are NOT held to the approved row, because the user's
 * CK-D decision (06-09) amended it after approval: the credential row links
 * nowhere, and the Access section gains "Sign in as {email}" / "Set your
 * Postiz password". The row's two quoted strings — "Admin credential" and
 * "Stored in your vault" — still are.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { ApiError } from "@/lib/api-errors";
import { uiSpecCopy, uiSpecRowCells, uiSpecRowQuotes } from "@/lib/ui-spec-contract";
import {
	ACCESS_COPY,
	backupFailedCopy,
	type CompanionLogLine,
	type CompanionLogPage,
	companionName,
	DATA_COPY,
	DETAIL_COPY,
	digestPrefix,
	INSTALL_FAILED_COPY,
	installationRefetchInterval,
	isScrolledToBottom,
	jobSteps,
	LOG_EMPTY_COPY,
	mergeLogLines,
	notAnsweringCopy,
	openCompanionLabel,
	readLogAfter,
	resolveInstallation,
	runUninstall,
	sourcePointer,
	tierRowValue,
	tlsNote,
	UNINSTALL_COPY,
	UPDATES_COPY,
	uninstallConsequences,
	uninstallExportInProgressCopy,
	uninstallRejectedCopy,
	uninstallTitle,
	updateAvailableBody,
	updateCard,
	updateChip,
	updateConfirmDescription,
	updateConfirmTitle,
	updatingNote,
	upToDateBody,
} from "./companion-detail.logic";
import type { CompanionInstallation } from "./companions-overview.logic";

/** The phase-06 UI contract, resolved from this module rather than the cwd. */
const SPEC = fileURLToPath(
	new URL(
		"../../../../../.planning/phases/06-first-companion-end-to-end/06-UI-SPEC.md",
		import.meta.url,
	),
);

const copy = (element: string) => uiSpecCopy(element, SPEC);
const quotes = (element: string) => uiSpecRowQuotes(element, SPEC);
const cells = (element: string) => uiSpecRowCells(element, SPEC);
/** Widen a literal-typed constant so it compares with a string read from the spec. */
const shipped = (value: string): string => value;
/** The contract's placeholder names, as the rows spell them. */
const NAME = "Postiz";

describe("S3 copy parity with the contract", () => {
	test("section headings", () => {
		expect(quotes("S3 section headings")).toEqual([
			shipped(DETAIL_COPY.sectionInstall),
			shipped(DETAIL_COPY.sectionAccess),
			shipped(DETAIL_COPY.sectionUpdates),
			shipped(DETAIL_COPY.sectionData),
		]);
	});

	test("breadcrumb root", () => {
		expect(quotes("S3 breadcrumb")[0]).toBe(shipped(DETAIL_COPY.breadcrumbRoot));
	});

	test("install-log heading and live note", () => {
		expect(copy("S3 install-log heading")).toBe(shipped(DETAIL_COPY.installLogHeading));
		expect(copy("S3 install-log live note")).toBe(shipped(DETAIL_COPY.installLogLiveNote));
	});

	test("hostname label, TLS note and open CTA", () => {
		expect(copy("S3 hostname label")).toBe(shipped(ACCESS_COPY.hostnameLabel));
		expect(copy("S3 TLS note")).toBe(tlsNote(NAME));
		expect(copy("S3 open CTA")).toBe(openCompanionLabel(NAME));
	});

	test("credential row's quoted strings (the link is the recorded CK-D delta)", () => {
		expect(quotes("S3 credential row")).toEqual([
			shipped(ACCESS_COPY.credentialLabel),
			shipped(ACCESS_COPY.credentialValue),
		]);
	});

	test("tier row", () => {
		const [label, value] = quotes("S3 tier row");
		expect(label).toBe(shipped(ACCESS_COPY.tierLabel));
		expect(value).toBe(tierRowValue("{tier}"));
	});

	test("version and backup rows", () => {
		expect(copy("S3 version row")).toBe(shipped(DATA_COPY.versionLabel));
		expect(quotes("S3 backup row")).toEqual([
			shipped(DATA_COPY.lastBackupLabel),
			shipped(DATA_COPY.backupHelper),
		]);
	});

	test("update card heading, bodies, actions, deferred body and in-progress notes", () => {
		expect(copy("S3 update card heading")).toBe(shipped(DETAIL_COPY.sectionUpdates));
		expect(copy("S3 up-to-date body")).toBe(upToDateBody(NAME));
		expect(copy("S3 update-available body")).toBe(updateAvailableBody(NAME));
		expect(quotes("S3 update actions")).toEqual([
			shipped(UPDATES_COPY.updateNow),
			shipped(UPDATES_COPY.notNow),
		]);
		expect(copy("S3 deferred body")).toBe(shipped(UPDATES_COPY.deferredBody));
		expect(quotes("S3 update-in-progress note")).toEqual([
			shipped(UPDATES_COPY.inProgressBackup),
			updatingNote(NAME),
		]);
	});

	test("the empty install log", () => {
		expect(quotes("S3, install log has no lines yet")).toEqual([
			shipped(LOG_EMPTY_COPY.title),
			shipped(LOG_EMPTY_COPY.body),
		]);
	});

	test("the error sentences S3 and S5 render", () => {
		expect(copy("Install job failed (terminal, rendered on S3)")).toBe(
			shipped(INSTALL_FAILED_COPY),
		);
		expect(copy("Hostname or certificate not ready after the install completed")).toBe(
			notAnsweringCopy(NAME),
		);
		expect(copy("Update refused because the pre-update backup failed")).toBe(
			backupFailedCopy(NAME),
		);
		expect(copy("Update job failed after the backup succeeded")).toBe(
			shipped(UPDATES_COPY.failedAfterBackup),
		);
		expect(copy("Export could not be started")).toBe(shipped(UNINSTALL_COPY.exportRejected));
		expect(copy("Uninstall could not be started")).toBe(uninstallRejectedCopy(NAME));
	});

	test("the two update-failure sentences are deliberately different", () => {
		expect(backupFailedCopy(NAME)).not.toBe(UPDATES_COPY.failedAfterBackup);
	});

	test("Update now confirmation row", () => {
		const [, title, description, confirmLabel, destructive] = cells("Update now");
		expect(title).toBe(`"${updateConfirmTitle(NAME)}"`);
		expect(description).toBe(`"${updateConfirmDescription(NAME)}"`);
		expect(confirmLabel).toBe(`"${UPDATES_COPY.confirmLabel}"`);
		expect(destructive).toBe("`false`");
	});

	test("S3 update chips and tones match the status token table", () => {
		const rows: [string, Parameters<typeof updateChip>[0]][] = [
			["Running the pinned digest, no newer one offered", "up_to_date"],
			["A newer digest is offered and undecided", "available"],
			["A newer digest was explicitly deferred", "deferred"],
			["The pre-update backup failed and the update aborted", "aborted_backup_failed"],
		];
		for (const [element, state] of rows) {
			const [, tone, label] = cells(element).map((value) => value.replace(/`/g, ""));
			expect(updateChip(state)).toEqual({ tone: tone as never, label: label as string });
		}
		expect(updateChip("in_progress")).toBeNull();
		expect(updateChip("failed")).toBeNull();
	});
});

describe("S5 copy parity with the contract", () => {
	const spec = readFileSync(SPEC, "utf8");

	test("Uninstall a companion row — title, description, confirm label, destructive", () => {
		const [, title, description, confirmLabel, destructive, typed] = cells("Uninstall a companion");
		expect(title).toBe(`"${uninstallTitle(NAME)}"`);
		expect(confirmLabel).toBe(`"${UNINSTALL_COPY.confirmLabel}"`);
		expect(destructive).toBe("`true`");
		expect(typed).toBe("**yes**");
		// The row's description, with its markdown emphasis and placeholder resolved.
		const rowDescription = (description ?? "")
			.replace(/^"|"$/g, "")
			.replace(/\*\*/g, "")
			.replace("`{companion name}`", NAME);
		expect(rowDescription).toBe(
			`${uninstallConsequences(NAME)} ${UNINSTALL_COPY.exportFirst} Type ${NAME} to confirm.`,
		);
	});

	test("the export offer's label and its swapped sentence", () => {
		expect(spec).toContain(`**"${UNINSTALL_COPY.exportAction}"**`);
		expect(spec).toContain(`"${UNINSTALL_COPY.exportPreparing}"`);
	});

	test("the amended swapped sentence says the uninstall waits for the export (CR-05)", () => {
		expect(shipped(UNINSTALL_COPY.exportPreparing)).toBe(
			"Your export is being prepared. You can uninstall once it is ready to download from Settings → Data.",
		);
	});

	test("Uninstall refused while an export is in progress", () => {
		expect(copy("Uninstall refused while an export is in progress")).toBe(
			uninstallExportInProgressCopy(NAME),
		);
	});

	test("every success toast S3 and S5 raise is one the contract lists", () => {
		const toasts = spec.slice(spec.indexOf("### Toasts"), spec.indexOf("## Screen Contracts"));
		for (const toast of [
			UPDATES_COPY.startedToast,
			UPDATES_COPY.deferredToast,
			UNINSTALL_COPY.exportStartedToast,
			UNINSTALL_COPY.uninstallStartedToast,
		]) {
			expect(toasts).toContain(`"${toast}"`);
		}
	});
});

describe("S5 uninstall outcomes", () => {
	const detail = (code: string) =>
		JSON.stringify({ detail: { code, message: "server prose is never shown" } });

	test("a 409 companion_export_in_progress is an inline refusal naming the companion", async () => {
		const outcome = await runUninstall({
			typed: NAME,
			name: NAME,
			requestUninstall: async () => {
				throw new ApiError(409, detail("companion_export_in_progress"));
			},
			onStarted: () => undefined,
		});
		expect(outcome).toEqual({ kind: "rejected", message: uninstallExportInProgressCopy(NAME) });
	});

	test("any other 409 is still the already-running conflict", async () => {
		for (const error of [
			new ApiError(409, detail("companion_operation_in_progress")),
			new ApiError(409, "companion_operation_in_progress"),
			new ApiError(409, "not json {"),
		]) {
			const outcome = await runUninstall({
				typed: NAME,
				name: NAME,
				requestUninstall: async () => {
					throw error;
				},
				onStarted: () => undefined,
			});
			expect(outcome).toEqual({ kind: "conflict" });
		}
	});
});

const DIGEST = `sha256:${"0123456789abcdef".repeat(4)}`;

function installation(overrides: Partial<CompanionInstallation> = {}): CompanionInstallation {
	return {
		id: "0b7e2d1c-0000-4000-8000-000000000002",
		agent_id: "6c3b1a2e-0000-4000-8000-000000000001",
		companion_key: "postiz",
		isolation_tier: "standard",
		operation: "install",
		status: "complete",
		step: null,
		error: null,
		version: "v2.24.0",
		pinned_digest: DIGEST,
		hostname: "postiz-acme.companions.iforge.dev",
		owner_email: "owner@example.com",
		credential: "stored_in_vault",
		reachability: { state: "reachable", checked_at: null },
		created_at: "2026-09-23T11:00:00Z",
		updated_at: "2026-09-23T12:00:00Z",
		completed_at: "2026-09-23T11:05:00Z",
		update: {
			state: "up_to_date",
			offered_version: null,
			offered_digest: null,
			deferred_digest: null,
			last_backup_at: null,
			error_code: null,
		},
		...overrides,
	};
}

describe("S3 rules", () => {
	test("the job enum never becomes a visible label, for any operation", () => {
		const enumWords = new Set(["queued", "running", "complete", "failed"]);
		for (const operation of ["install", "update", "uninstall"] as const) {
			for (const step of jobSteps({ operation, status: "running" })) {
				expect(enumWords.has(step.label)).toBe(false);
				expect(step.label).toMatch(/^[A-Z]/);
			}
		}
		expect(jobSteps({ operation: "install", status: "queued" }).map((s) => s.label)).toEqual([
			"Queued",
			"Installing",
			"Running",
			"Install failed",
		]);
		expect(jobSteps({ operation: "update", status: "failed" }).map((s) => s.label)).toEqual([
			"Queued",
			"Updating",
			"Running",
			"Update failed",
		]);
		const current = jobSteps({ operation: "install", status: "failed" }).filter((s) => s.current);
		expect(current.map((s) => s.status)).toEqual(["failed"]);
	});

	test("polling follows only a moving job", () => {
		expect(installationRefetchInterval(installation({ status: "queued" }))).toBe(2_000);
		expect(installationRefetchInterval(installation({ status: "running" }))).toBe(2_000);
		expect(installationRefetchInterval(installation({ status: "complete" }))).toBe(false);
		expect(installationRefetchInterval(installation({ status: "failed" }))).toBe(false);
	});

	test("the page resolves the newest live installation of its companion; a finished uninstall is none", () => {
		const older = installation({ id: "a", created_at: "2026-09-01T00:00:00Z" });
		const newer = installation({ id: "b", created_at: "2026-09-20T00:00:00Z" });
		const other = installation({ id: "c", companion_key: "other" });
		expect(resolveInstallation([older, newer, other], "postiz")?.id).toBe("b");
		expect(
			resolveInstallation([installation({ operation: "uninstall", status: "complete" })], "postiz"),
		).toBeNull();
		expect(resolveInstallation(undefined, "postiz")).toBeNull();
		expect(companionName([{ companion_key: "postiz", display_name: "Postiz" }], "postiz")).toBe(
			"Postiz",
		);
		expect(companionName(undefined, "postiz")).toBe("postiz");
	});

	test("log lines merge by seq: no duplicates, in order, text untouched", () => {
		const line = (seq: number, text = `line ${seq}`): CompanionLogLine => ({
			seq,
			stream: "stdout",
			line: text,
			created_at: "2026-09-23T11:00:00Z",
		});
		const merged = mergeLogLines([line(1), line(3)], [line(3, "dup"), line(2, "  <b>x</b>  ")]);
		expect(merged.map((l) => l.seq)).toEqual([1, 2, 3]);
		expect(merged[1]?.line).toBe("  <b>x</b>  ");
		expect(merged[2]?.line).toBe("line 3");
	});

	test("the log is read by after_seq, page by page, until a short page", async () => {
		const all = Array.from({ length: 5 }, (_, index) => ({
			seq: index + 1,
			stream: "stdout" as const,
			line: `l${index + 1}`,
			created_at: "2026-09-23T11:00:00Z",
		}));
		const asked: number[] = [];
		const fetchPage = async (afterSeq: number, limit: number): Promise<CompanionLogPage> => {
			asked.push(afterSeq);
			const items = all.filter((l) => l.seq > afterSeq).slice(0, limit);
			return { items, next_after_seq: items.at(-1)?.seq ?? afterSeq };
		};
		const read = await readLogAfter({ afterSeq: 1, fetchPage, limit: 2 });
		expect(asked).toEqual([1, 3, 5]);
		expect(read.lines.map((l) => l.seq)).toEqual([2, 3, 4, 5]);
		expect(read.nextAfterSeq).toBe(5);
	});

	test("the well follows the newest line only while the reader is at the bottom", () => {
		expect(isScrolledToBottom({ scrollTop: 600, scrollHeight: 984, clientHeight: 384 })).toBe(true);
		expect(isScrolledToBottom({ scrollTop: 590, scrollHeight: 984, clientHeight: 384 })).toBe(true);
		expect(isScrolledToBottom({ scrollTop: 200, scrollHeight: 984, clientHeight: 384 })).toBe(
			false,
		);
	});

	test("the digest shows a 12-character prefix after the sha256 marker", () => {
		expect(digestPrefix(DIGEST)).toBe("0123456789ab");
		expect(digestPrefix("0123456789abcdef")).toBe("0123456789ab");
	});

	test("the source pointer names the upstream tag of the unmodified image", () => {
		expect(sourcePointer("postiz", "Postiz", "v2.24.0")).toEqual({
			label: "Postiz v2.24.0 — source",
			href: "https://github.com/gitroomhq/postiz-app/tree/v2.24.0",
		});
		expect(sourcePointer("unknown", "X", "v1")).toBeNull();
	});

	test("the update card: never offered reads as pinned; an offer without a digest offers nothing", () => {
		expect(updateCard(installation(), NAME)).toEqual({
			kind: "up_to_date",
			body: upToDateBody(NAME),
		});
		expect(
			updateCard(
				installation({
					update: {
						state: "available",
						offered_version: "v2.25.0",
						offered_digest: null,
						deferred_digest: null,
						last_backup_at: null,
						error_code: null,
					},
				}),
				NAME,
			).kind,
		).toBe("up_to_date");
		expect(
			updateCard(installation({ operation: "update", status: "queued", step: null }), NAME),
		).toEqual({ kind: "in_progress", note: UPDATES_COPY.inProgressBackup });
		expect(
			updateCard(installation({ operation: "update", status: "running", step: "pull" }), NAME),
		).toEqual({ kind: "in_progress", note: "Updating Postiz." });
	});
});
