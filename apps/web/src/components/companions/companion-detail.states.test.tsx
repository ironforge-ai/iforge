/**
 * Held-out UI-state tests for S3 — the Companion detail page. One or more
 * named tests per `06-UI-SPEC.md` §UI Considerations row for S3.
 *
 * **SCOPE LIMIT, same as `companions-overview.states.test.tsx`.** There is no
 * DOM library here, so every assertion reads `renderToStaticMarkup` output:
 * which copy is present, which classes, attributes and `title`s are present,
 * and in which ORDER elements were emitted. Nothing is clicked and no pixel is
 * measured. Polling is asserted through `installationRefetchInterval`, the
 * exact function the page hands to its installation query as
 * `refetchInterval`; the writes are asserted through the runners the buttons
 * call, driven by a REAL generated client over a fake `fetch`.
 *
 * The breadcrumb and the section tabs are router `Link`s, so the page renders
 * inside a throwaway memory router (`renderInRouter`) and their real `href`s
 * are asserted.
 */

import { describe, expect, test } from "bun:test";
import type { paths } from "@clawdi/shared/api";
import {
	createMemoryHistory,
	createRootRoute,
	createRouter,
	RouterProvider,
} from "@tanstack/react-router";
import createClient from "openapi-fetch";
import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ApiError } from "@/lib/api-errors";
import { renderedText } from "@/lib/rendered-text";
import { CompanionDetailView, type CompanionDetailViewProps } from "./companion-detail";
import {
	type CompanionLogLine,
	companionRequests,
	DETAIL_COPY,
	DETAIL_POLL_INTERVAL_MS,
	installationRefetchInterval,
	loginLinkFailedCopy,
	mediaNote,
	runLoginLink,
	runUpdateDecision,
	tlsNote,
	UPDATES_COPY,
	updateRejectedCopy,
} from "./companion-detail.logic";
import type { CompanionInstallation } from "./companions-overview.logic";

const AGENT_ID = "6c3b1a2e-0000-4000-8000-000000000001";
const INSTALLATION_ID = "0b7e2d1c-0000-4000-8000-000000000002";
const DIGEST = `sha256:${"a".repeat(64)}`;

function installation(overrides: Partial<CompanionInstallation> = {}): CompanionInstallation {
	return {
		id: INSTALLATION_ID,
		agent_id: AGENT_ID,
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
		reachability: { state: "reachable", checked_at: "2026-09-23T12:00:00Z" },
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

/** A running install: no hostname is serving yet, so reachability says so. */
function runningInstall(overrides: Partial<CompanionInstallation> = {}): CompanionInstallation {
	return installation({
		status: "running",
		step: "database",
		completed_at: null,
		reachability: { state: "no_hostname_yet", checked_at: null },
		...overrides,
	});
}

function line(seq: number, text: string, stream: CompanionLogLine["stream"] = "stdout") {
	return { seq, stream, line: text, created_at: "2026-09-23T11:00:00Z" } satisfies CompanionLogLine;
}

const THREE_LINES: CompanionLogLine[] = [
	line(1, "pulling ghcr.io/gitroomhq/postiz-app:v2.24.0"),
	line(2, "warning: redis is not answering yet, retrying", "stderr"),
	line(3, "database migrations applied"),
];

function viewProps(overrides: Partial<CompanionDetailViewProps> = {}): CompanionDetailViewProps {
	return {
		agentId: AGENT_ID,
		companionKey: "postiz",
		name: "Postiz",
		installation: installation(),
		logLines: [],
		...overrides,
	};
}

/** Static markup for a node that needs a router (Links), via a throwaway memory router. */
async function renderInRouter(node: ReactNode): Promise<string> {
	const rootRoute = createRootRoute({ component: () => node });
	const router = createRouter({
		routeTree: rootRoute,
		history: createMemoryHistory({ initialEntries: ["/"] }),
	});
	await router.load();
	return renderToStaticMarkup(<RouterProvider router={router} />);
}

function render(overrides: Partial<CompanionDetailViewProps> = {}): Promise<string> {
	return renderInRouter(<CompanionDetailView {...viewProps(overrides)} />);
}

/**
 * The markup of the FIRST element carrying `data-slot="name"`, from its opening
 * tag to the matching close tag (nesting of the same tag name is counted, so a
 * `div` region that contains `div`s is sliced whole).
 */
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

describe("S3 Companion detail — tracer", () => {
	test("tracer: a tenant watches their install move and its log stream, and the page stops polling when it completes", async () => {
		// 1. Running: breadcrumb, title, the Installing chip, the step read-out, the live log.
		const running = runningInstall();
		const markup = await render({ installation: running, logLines: THREE_LINES });
		const text = renderedText(markup);

		const breadcrumb = slot(markup, "companion-breadcrumb");
		expect(renderedText(breadcrumb)).toBe("Companions Postiz");
		expect(breadcrumb).toContain(`href="/agents/${AGENT_ID}/companions/installed"`);
		expect(markup).toMatch(/<h1[^>]*>Postiz<\/h1>/);

		const chips = slot(markup, "companion-status-chips");
		expect(renderedText(chips).startsWith("Installing")).toBe(true);

		const steps = slot(markup, "install-steps");
		expect(renderedText(steps)).toBe("Queued Installing Running Install failed");
		const current = steps.match(/<li[^>]*aria-current="step"[^>]*>[\s\S]*?<\/li>/g) ?? [];
		expect(current).toHaveLength(1);
		expect(renderedText(current[0] ?? "")).toBe("Installing");
		expect(current[0]).toContain('data-slot="status-badge"');

		const well = slot(markup, "install-log-well");
		const lines = well.match(/data-slot="install-log-line"/g) ?? [];
		expect(lines).toHaveLength(3);
		expect(renderedText(well)).toBe(
			[
				"STDOUT pulling ghcr.io/gitroomhq/postiz-app:v2.24.0",
				"STDERR warning: redis is not answering yet, retrying",
				"STDOUT database migrations applied",
			].join(" "),
		);
		expect(text).toContain(DETAIL_COPY.installLogHeading);
		expect(text).toContain(DETAIL_COPY.installLogLiveNote);

		// 2. While it runs, the page polls every two seconds.
		expect(DETAIL_POLL_INTERVAL_MS).toBe(2_000);
		expect(installationRefetchInterval(running)).toBe(2_000);

		// 3. Complete: polling stops and the log collapses behind "Show install log".
		const complete = installation();
		expect(installationRefetchInterval(complete)).toBe(false);
		const done = await render({ installation: complete, logLines: THREE_LINES });
		expect(renderedText(slot(done, "companion-status-chips")).startsWith("Running")).toBe(true);
		expect(slot(done, "install-log-well")).toBe("");
		expect(renderedText(slot(done, "install-log-disclosure"))).toContain(
			DETAIL_COPY.showInstallLog,
		);
	});
});

describe("S3 Companion detail — polling and untrusted log text", () => {
	test("loading: polling runs while queued or running and stops at complete and at failed", () => {
		expect(installationRefetchInterval(runningInstall({ status: "queued" }))).toBe(2_000);
		expect(installationRefetchInterval(runningInstall())).toBe(2_000);
		expect(installationRefetchInterval(installation({ status: "complete" }))).toBe(false);
		expect(installationRefetchInterval(runningInstall({ status: "failed" }))).toBe(false);
		// Nothing resolved (yet, or ever): no indefinite poll either.
		expect(installationRefetchInterval(undefined)).toBe(false);
		expect(installationRefetchInterval(null)).toBe(false);
	});

	test("long-text: a log line carrying markup renders as literal text — never HTML, markdown or a link", async () => {
		const hostile =
			'<img src=x onerror="alert(1)"><script>alert(2)</script> **bold** [click](https://evil.test) https://evil.test';
		const markup = await render({
			installation: runningInstall(),
			logLines: [line(1, hostile, "stderr")],
		});
		const well = slot(markup, "install-log-well");
		expect(well).not.toContain("<img");
		expect(well).not.toContain("<script");
		expect(well).not.toContain("<strong");
		expect(well).not.toMatch(/<a[\s>]/);
		expect(well).toContain("&lt;img src=x onerror=");
		expect(renderedText(well)).toBe(`STDERR ${hostile}`);
	});
});

/** The installation's credentials, as they would look if a response ever carried them. */
const LEAKED_SECRETS = {
	admin_password: "Adm1n-Pa55-9f8e7d",
	api_key: "pk_live_4b3c2a1f0e9d",
	jwt_secret: "jwt-secret-0a1b2c3d",
};

describe("S3 Access", () => {
	test("populated: hostname in mono with a copy button and the TLS note; tier read-only with its wire value in title", async () => {
		const markup = await render();
		const hostname = slot(markup, "access-hostname");
		expect(hostname).toMatch(
			/<span[^>]*class="truncate min-w-0 font-mono text-xs"[^>]*title="postiz-acme\.companions\.iforge\.dev"[^>]*>postiz-acme\.companions\.iforge\.dev<\/span>/,
		);
		expect(hostname).toContain('aria-label="Copy hostname"');
		expect(renderedText(hostname)).toContain(tlsNote("Postiz"));

		const tier = slot(markup, "access-tier");
		expect(renderedText(tier)).toBe(
			"Isolation Standard. Chosen at install and fixed for this companion.",
		);
		expect(tier).toContain('title="standard"');
		expect(tier).not.toMatch(/<(button|input|select)[\s>]/);
	});

	test("populated (CK-D): the credential row says 'Stored in your vault' with no link, and a ghost 'Set your Postiz password' sits beside 'Sign in as {email}'", async () => {
		const markup = await render();
		const credential = slot(markup, "access-credential");
		const text = renderedText(credential);
		expect(text).toContain("Admin credential");
		expect(text).toContain("Stored in your vault");
		expect(text).toContain("Sign in as owner@example.com");
		expect(text).toContain("Set your Postiz password");
		// No vault link (CK-D): nothing in the row is an anchor.
		expect(credential).not.toMatch(/<a[\s>]/);
		const button = slot(credential, "set-password");
		expect(button).toContain("hover:bg-muted");
		expect(button).not.toContain("bg-primary");
		expect(button).not.toMatch(/<button[^>]*\sdisabled[\s=>]/);
	});

	test("security: no credential value renders anywhere and no show or reveal control exists", async () => {
		const leaky = { ...installation(), ...LEAKED_SECRETS } as CompanionInstallation;
		const markup = await render({ installation: leaky, logLines: THREE_LINES });
		for (const secret of Object.values(LEAKED_SECRETS)) {
			expect(markup).not.toContain(secret);
		}
		// The only "show" on the page is the install log's own disclosure.
		const visible = renderedText(markup).replace(DETAIL_COPY.showInstallLog, "");
		expect(visible).not.toMatch(/\b(show|reveal)\b/i);
		expect(renderedText(slot(markup, "companion-access"))).not.toMatch(/\b(show|reveal)\b/i);
		expect(markup).not.toMatch(/aria-label="[^"]*\b(show|reveal)\b[^"]*"/i);
		expect(markup).not.toContain('type="password"');
	});

	test("partial: a running companion whose hostname is not answering keeps the chip and the hostname row, and says why", async () => {
		const markup = await render({
			installation: installation({
				reachability: { state: "not_answering", checked_at: "2026-09-23T12:00:00Z" },
			}),
		});
		expect(renderedText(slot(markup, "companion-status-chips"))).toContain("Not answering");
		const hostname = slot(markup, "access-hostname");
		expect(renderedText(hostname)).toContain("postiz-acme.companions.iforge.dev");
		expect(renderedText(slot(markup, "access-not-answering"))).toBe(
			"Postiz is running but its hostname is not answering yet. This usually settles within a few minutes.",
		);
	});

	test("set password: one POST to this installation's login-link route, and the minted URL opened as served", async () => {
		const minted = "https://postiz-acme.companions.iforge.dev/auth/forgot/eyJhbGciOi.jwt.sig";
		const { client, captured } = fakeApi(() => ({
			status: 200,
			body: {
				url: minted,
				expires_at: "2026-09-23T12:20:00Z",
				sign_in_email: "owner@example.com",
			},
		}));
		const opened: string[] = [];
		const outcome = await runLoginLink({
			name: "Postiz",
			requestLink: companionRequests(client, INSTALLATION_ID).loginLink,
			openUrl: (url) => opened.push(url),
		});
		expect(outcome).toEqual({ ok: true });
		expect(captured).toEqual([
			{
				method: "POST",
				url: `http://api.test/v1/companions/installations/${INSTALLATION_ID}/login-link`,
				body: null,
			},
		]);
		expect(opened).toEqual([minted]);
	});

	test("error: a refused or non-https link opens nothing and renders its sentence inline", async () => {
		const refused = fakeApi(() => ({ status: 409, body: { detail: "companion_not_ready" } }));
		const opened: string[] = [];
		const failed = await runLoginLink({
			name: "Postiz",
			requestLink: companionRequests(refused.client, INSTALLATION_ID).loginLink,
			openUrl: (url) => opened.push(url),
		});
		expect(failed).toEqual({ ok: false, message: loginLinkFailedCopy("Postiz") });
		const hostile = fakeApi(() => ({
			status: 200,
			body: { url: "javascript:alert(1)", expires_at: "x", sign_in_email: "owner@example.com" },
		}));
		const unsafe = await runLoginLink({
			name: "Postiz",
			requestLink: companionRequests(hostile.client, INSTALLATION_ID).loginLink,
			openUrl: (url) => opened.push(url),
		});
		expect(unsafe.ok).toBe(false);
		expect(opened).toEqual([]);

		const markup = await render({ loginLinkError: loginLinkFailedCopy("Postiz") });
		const error = slot(markup, "set-password-error");
		expect(error).toContain('role="alert"');
		expect(renderedText(error)).toBe(
			"The link was not created. Your Postiz password is unchanged.",
		);
	});
});

/** Count the page's accent (primary) buttons. */
function accentButtons(markup: string): number {
	return (markup.match(/<button[^>]*class="[^"]*bg-primary text-primary-foreground[^"]*"/g) ?? [])
		.length;
}

const OFFERED_DIGEST = "b".repeat(64);

function offered(overrides: Partial<CompanionInstallation["update"]> = {}) {
	return installation({
		update: {
			state: "available",
			offered_version: "v2.25.0",
			offered_digest: OFFERED_DIGEST,
			deferred_digest: null,
			last_backup_at: "2026-09-23T10:00:00Z",
			error_code: null,
			...overrides,
		},
	});
}

describe("S3 Updates", () => {
	test("empty: never offered a release, the card reads as pinned — no empty state, no actions", async () => {
		const markup = await render();
		const updates = slot(markup, "companion-updates");
		expect(renderedText(updates)).toBe(
			"Updates Postiz is pinned to the version you installed. Nothing updates on its own.",
		);
		expect(updates).not.toContain("<button");
		expect(renderedText(slot(markup, "companion-status-chips"))).toContain("Up to date");
	});

	test("populated: an offered version shows Update now (the page's only accent button) and a ghost Not now", async () => {
		const markup = await render({ installation: offered() });
		const updates = slot(markup, "companion-updates");
		expect(renderedText(updates)).toContain(
			"A newer version of Postiz is available. Nothing changes until you choose.",
		);
		expect(renderedText(updates)).toContain("v2.25.0");
		expect(renderedText(slot(updates, "update-now"))).toBe("Update now");
		expect(slot(updates, "update-not-now")).toContain("hover:bg-muted");
		expect(accentButtons(markup)).toBe(1);
		expect(accentButtons(slot(updates, "update-actions"))).toBe(1);
		expect(renderedText(slot(markup, "companion-status-chips"))).toContain("Update available");
	});

	test("populated: accepting and deferring send the ONE offered digest to the update route", async () => {
		const { client, captured } = fakeApi(() => ({ status: 200, body: installation() }));
		const toasts: string[] = [];
		const accepted = await runUpdateDecision({
			decision: "accept",
			digest: OFFERED_DIGEST,
			requestDecision: companionRequests(client, INSTALLATION_ID).decideUpdate,
			rejectedMessage: updateRejectedCopy("Postiz"),
			onDone: () => toasts.push(UPDATES_COPY.startedToast),
		});
		const deferred = await runUpdateDecision({
			decision: "defer",
			digest: OFFERED_DIGEST,
			requestDecision: companionRequests(client, INSTALLATION_ID).decideUpdate,
			rejectedMessage: UPDATES_COPY.deferRejected,
			onDone: () => toasts.push(UPDATES_COPY.deferredToast),
		});
		expect(accepted).toEqual({ ok: true });
		expect(deferred).toEqual({ ok: true });
		const url = `http://api.test/v1/companions/installations/${INSTALLATION_ID}/update`;
		expect(captured).toEqual([
			{ method: "POST", url, body: { decision: "accept", digest: OFFERED_DIGEST } },
			{ method: "POST", url, body: { decision: "defer", digest: OFFERED_DIGEST } },
		]);
		expect(toasts).toEqual(["Update started", "Update deferred"]);
	});

	test("error: a refused decision renders inline and toasts nothing", async () => {
		const { client } = fakeApi(() => ({
			status: 409,
			body: { detail: "companion_update_not_offered" },
		}));
		const toasts: string[] = [];
		const outcome = await runUpdateDecision({
			decision: "defer",
			digest: OFFERED_DIGEST,
			requestDecision: companionRequests(client, INSTALLATION_ID).decideUpdate,
			rejectedMessage: UPDATES_COPY.deferRejected,
			onDone: () => toasts.push("toast"),
		});
		expect(outcome).toEqual({ ok: false, message: UPDATES_COPY.deferRejected });
		expect(toasts).toEqual([]);
		const markup = await render({
			installation: offered(),
			deferError: UPDATES_COPY.deferRejected,
		});
		expect(slot(markup, "update-defer-error")).toContain('role="alert"');
	});

	test("populated: a deferred version says so and offers nothing", async () => {
		const markup = await render({
			installation: offered({
				state: "deferred",
				offered_digest: null,
				offered_version: null,
				deferred_digest: OFFERED_DIGEST,
			}),
		});
		const updates = slot(markup, "companion-updates");
		expect(renderedText(updates)).toBe(
			"Updates You deferred this version. It stays available, and the next release will be offered the same way.",
		);
		expect(updates).not.toContain("<button");
		expect(renderedText(slot(markup, "companion-status-chips"))).toContain("Update deferred");
	});

	test("loading: an update in flight reads 'Taking a backup before updating.' then 'Updating Postiz.'", async () => {
		const backingUp = await render({
			installation: installation({ operation: "update", status: "running", step: "backup" }),
		});
		expect(renderedText(slot(backingUp, "update-note"))).toBe("Taking a backup before updating.");
		expect(renderedText(slot(backingUp, "companion-status-chips")).startsWith("Updating")).toBe(
			true,
		);
		const updating = await render({
			installation: installation({ operation: "update", status: "running", step: "pull" }),
		});
		expect(renderedText(slot(updating, "update-note"))).toBe("Updating Postiz.");
		expect(slot(updating, "update-actions")).toBe("");
	});

	test("error: a backup-failed abort and a post-backup failure use their two different sentences", async () => {
		const aborted = await render({
			installation: installation({
				update: {
					state: "aborted_backup_failed",
					offered_version: null,
					offered_digest: null,
					deferred_digest: null,
					last_backup_at: null,
					error_code: "backup_failed",
				},
			}),
		});
		const abortedBody = slot(aborted, "update-body");
		expect(renderedText(abortedBody)).toBe(
			"The update was cancelled because the backup failed. Postiz is still running the version you had, and your data is untouched.",
		);
		// Protection, not breakage: the sentence does not borrow the error voice.
		expect(abortedBody).not.toContain("destructive");
		expect(abortedBody).not.toContain('role="alert"');
		expect(slot(aborted, "companion-status-chips")).toMatch(
			/data-status="destructive"[^>]*>Update aborted — backup failed</,
		);

		const failed = await render({
			installation: installation({
				operation: "update",
				status: "failed",
				update: {
					state: "failed",
					offered_version: null,
					offered_digest: null,
					deferred_digest: null,
					last_backup_at: "2026-09-23T10:00:00Z",
					error_code: "update_rolled_back",
				},
			}),
		});
		const failedBody = slot(failed, "update-body");
		expect(renderedText(failedBody)).toBe(
			"The update failed. A backup was taken before it started — contact support with this companion's name before retrying.",
		);
		expect(failedBody).toContain('role="alert"');
		expect(renderedText(failedBody)).not.toBe(renderedText(abortedBody));
	});
});

describe("S3 Data", () => {
	test("populated: last backup, media note, the Export data link into Settings → Data, and the version row with its source", async () => {
		const markup = await render({ installation: offered() });
		const data = slot(markup, "companion-data");
		const text = renderedText(data);
		expect(text).toContain("Last backup");
		expect(text).toContain("Taken automatically before every update.");
		expect(text).toContain(mediaNote("Postiz"));
		expect(slot(data, "data-export-link")).toContain("settings=data");
		expect(renderedText(slot(data, "data-export-link"))).toBe("Export data");

		const version = slot(data, "data-version");
		expect(renderedText(slot(version, "version-tag"))).toBe("v2.24.0");
		const digest = slot(version, "version-digest");
		expect(renderedText(digest)).toBe("aaaaaaaaaaaa");
		expect(digest).toContain(`title="${DIGEST}"`);
		expect(digest).toContain("font-mono text-xs");
		expect(version).toContain('aria-label="Copy image digest"');
		const source = slot(version, "version-source");
		expect(source).toContain('href="https://github.com/gitroomhq/postiz-app/tree/v2.24.0"');
		expect(source).toContain('target="_blank"');
		expect(source).toContain('rel="noopener noreferrer"');
		expect(renderedText(source)).toBe("Postiz v2.24.0 — source");
		// Uninstall is not in the Data section: it lives in the header row menu.
		expect(text).not.toContain("Uninstall");
	});
});

describe("S3 Companion detail — UI considerations", () => {
	test("empty: an install log with no lines shows the inset 'Waiting for the first log line' inside the well", async () => {
		const markup = await render({
			installation: runningInstall({ status: "queued" }),
			logLines: [],
		});
		const well = slot(markup, "install-log-well");
		expect(well).toContain('data-lines="0"');
		expect(renderedText(well)).toBe(
			"Waiting for the first log line The install has been queued. Output appears here as soon as it starts.",
		);
		expect(well).not.toContain('data-slot="install-log-line"');
	});

	test("loading: PageHeaderSkeleton plus four section-shaped skeletons, never a page spinner", async () => {
		const markup = await render({ installation: undefined, loading: true });
		expect(markup).toContain('data-slot="page-header-skeleton"');
		expect(markup.match(/data-slot="section-skeleton"/g) ?? []).toHaveLength(4);
		expect(markup).not.toContain('data-slot="spinner"');
		// The breadcrumb stays so the way back is never lost.
		expect(renderedText(slot(markup, "companion-breadcrumb"))).toBe("Companions Postiz");
	});

	test("error: a failed query renders ApiErrorPanel with a retry", async () => {
		const markup = await render({
			installation: undefined,
			error: new ApiError(500, "internal"),
			onRetry: () => undefined,
		});
		expect(markup).toContain('data-slot="alert"');
		expect(renderedText(markup)).toContain("Couldn't load this");
		expect(renderedText(markup)).toContain("Retry");
		expect(slot(markup, "companion-install")).toBe("");
	});

	test("error: a failed install says nothing runs and nothing is charged, beneath the log that shows where it stopped", async () => {
		const failed = runningInstall({ status: "failed", error: "database did not become ready" });
		expect(installationRefetchInterval(failed)).toBe(false);
		const markup = await render({ installation: failed, logLines: THREE_LINES });
		const install = slot(markup, "companion-install");
		const failure = slot(install, "install-failure");
		expect(failure).toContain('role="alert"');
		expect(renderedText(failure)).toBe(
			"The install failed. Nothing is running and you have not been charged. The log above shows where it stopped.",
		);
		expect(install.indexOf('data-slot="install-log-well"')).toBeLessThan(
			install.indexOf('data-slot="install-failure"'),
		);
		expect(renderedText(slot(markup, "companion-status-chips")).startsWith("Install failed")).toBe(
			true,
		);
		// Not live any more: no streaming note.
		expect(install).not.toContain(DETAIL_COPY.installLogLiveNote);
	});

	test("populated: four Separator-divided sections in order Install, Access, Updates, Data", async () => {
		const markup = await render();
		const order = [
			"companion-install",
			"companion-access",
			"companion-updates",
			"companion-data",
		].map((name) => markup.indexOf(`data-slot="${name}"`));
		expect(order.every((at) => at > -1)).toBe(true);
		expect([...order].sort((a, b) => a - b)).toEqual(order);
		for (let index = 1; index < order.length; index += 1) {
			const between = markup.slice(order[index - 1], order[index]);
			expect(between).toContain('data-slot="separator"');
		}
		expect(renderedText(markup)).toContain("Access Hostname");
		// A complete install collapses behind the disclosure, with its one-line summary.
		const disclosure = slot(markup, "install-log-disclosure");
		expect(renderedText(disclosure)).toMatch(/^Installed .+ Show install log$/);
		expect(disclosure).toContain('aria-expanded="false"');
		// The header row menu carries Uninstall behind an accessibly named trigger.
		expect(slot(markup, "companion-actions-trigger")).toContain('aria-label="Companion actions"');
		// Open Postiz: ghost, external, a new tab.
		const open = slot(markup, "open-companion");
		expect(open).toContain('href="https://postiz-acme.companions.iforge.dev"');
		expect(open).toContain('target="_blank"');
		expect(renderedText(open)).toBe("Open Postiz");
		expect(open).not.toContain("bg-primary");
		// No accent button anywhere while nothing is offered.
		expect(accentButtons(markup)).toBe(0);
	});

	test("partial: before the install completes there is no Open action and no hostname to copy", async () => {
		const markup = await render({ installation: runningInstall(), logLines: THREE_LINES });
		expect(slot(markup, "open-companion")).toBe("");
		const hostname = slot(markup, "access-hostname");
		expect(hostname).not.toContain("Copy hostname");
		expect(renderedText(hostname)).toContain("No hostname yet");
		expect(slot(markup, "set-password")).toMatch(/<button[^>]*\sdisabled[\s=>]/);
	});

	test("overflow: the log well is the scroll region, bounded at max-h-96, and never widens", async () => {
		const many = Array.from({ length: 120 }, (_, index) => line(index + 1, `line ${index + 1}`));
		const markup = await render({ installation: runningInstall(), logLines: many });
		const well = slot(markup, "install-log-well");
		const wellClasses = (/^<div[^>]*class="([^"]*)"/.exec(well)?.[1] ?? "").split(/\s+/);
		for (const token of [
			"max-h-96",
			"overflow-y-auto",
			"whitespace-pre-wrap",
			"break-all",
			"font-mono",
			"text-xs",
			"leading-relaxed",
			"bg-muted/30",
			"rounded-lg",
			"p-4",
		]) {
			expect(wellClasses).toContain(token);
		}
		expect(well).toContain('role="log"');
		expect(well).toContain('tabindex="0"');
		expect(well.match(/data-slot="install-log-line"/g) ?? []).toHaveLength(120);
		// Stream markers are the one ALL-CAPS meta label.
		expect(well).toContain("text-2xs tracking-wider text-muted-foreground");
	});

	test("zero-one-many: zero, one and many lines keep one well with one geometry", async () => {
		const classOf = (markup: string) =>
			/<div[^>]*data-slot="install-log-well"/.exec(markup)?.[0].match(/class="([^"]*)"/)?.[1];
		const zero = await render({ installation: runningInstall(), logLines: [] });
		const one = await render({ installation: runningInstall(), logLines: [line(1, "one")] });
		const many = await render({ installation: runningInstall(), logLines: THREE_LINES });
		expect(classOf(zero)).toBeDefined();
		expect(classOf(one)).toBe(classOf(zero));
		expect(classOf(many)).toBe(classOf(zero));
		expect(slot(one, "install-log-well").match(/data-slot="install-log-line"/g) ?? []).toHaveLength(
			1,
		);
	});

	test("long-text: an unbroken log line cannot widen the well; the digest is truncated; the title is the catalog name", async () => {
		const unbroken = "x".repeat(4_000);
		const markup = await render({
			installation: runningInstall(),
			logLines: [line(1, unbroken)],
		});
		const well = slot(markup, "install-log-well");
		expect(well).toContain("break-all");
		expect(renderedText(well)).toBe(`STDOUT ${unbroken}`);
		const done = await render();
		expect(renderedText(slot(done, "version-digest"))).toHaveLength(12);
		expect(done).toMatch(/<h1[^>]*>Postiz<\/h1>/);
	});

	test("empty: a companion not installed on this agent says so and points back to Companions", async () => {
		const markup = await render({ installation: null });
		const text = renderedText(markup);
		expect(text).toContain("Postiz is not installed on this agent");
		expect(markup).toContain(`href="/agents/${AGENT_ID}/companions/installed"`);
		expect(slot(markup, "companion-install")).toBe("");
	});

	test("the job enum never appears as visible text on the page", async () => {
		for (const status of ["queued", "running", "complete", "failed"] as const) {
			const markup = await render({
				installation: runningInstall({ status }),
				logLines: [],
			});
			// Every chip and every step reads as a sentence-case label, never the bare enum;
			// the enum travels only in data-* attributes and titles.
			const chipLabels = [
				...slot(markup, "companion-status-chips").matchAll(
					// Skip the status dot (an empty span) that a `withDot` chip opens with.
					/data-slot="status-badge"[^>]*>(?:<span[^>]*><\/span>)?([^<]*)<\/span>/g,
				),
			].map((match) => renderedText(match[1] ?? ""));
			const stepLabels = [
				...slot(markup, "install-steps").matchAll(/<li[^>]*>([\s\S]*?)<\/li>/g),
			].map((match) => renderedText(match[1] ?? ""));
			expect(chipLabels.length).toBeGreaterThan(0);
			expect(stepLabels).toHaveLength(4);
			for (const label of [...chipLabels, ...stepLabels]) {
				// Case-sensitive: "Queued" is the contract's sentence-case label; "queued" is the enum.
				expect(["queued", "running", "complete", "failed"]).not.toContain(label);
				expect(label).toMatch(/^[A-Z]/);
			}
			expect(slot(markup, "install-steps")).toContain(`data-state="${status}"`);
			expect(renderedText(markup)).not.toMatch(
				/\b(up_to_date|no_hostname_yet|not_answering|tls_pending|aborted_backup_failed|stored_in_vault)\b/,
			);
		}
	});
});

type Captured = { method: string; url: string; body: unknown };

/** A real generated client whose transport records each request and answers from `respond`. */
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
