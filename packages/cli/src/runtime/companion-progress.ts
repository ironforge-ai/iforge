/**
 * The runtime's install-progress client (plan 06-12): report one installation's progress,
 * readiness or failure to the control plane, with every known secret value removed first.
 *
 * The report route is plan 06-11's `POST /v1/runtime/companion-installations/{id}/report`. It is
 * runtime-initiated, like the dispatch channel: the control plane never opens a connection into a
 * tenant network, so everything it learns about an install arrives through this client.
 *
 * **Redaction happens HERE, before anything leaves the host** (research Pitfall 9, T-06-12-03).
 * Journal lines and error strings are untrusted text produced right next to the installation's
 * credentials: Postiz prints environment-derived errors, `DATABASE_URL` carries a password, and a
 * failing psql statement can echo the literal it was given. The control plane redacts again at
 * storage time (plan 06-11), but a secret that reaches the wire has already left the host, so the
 * runtime removes every value it knows before sending. The two redactions are deliberately
 * independent: neither trusts the other to have run.
 *
 * **A 409 is an answer, never a transport error, and it is never retried** (plan 06-40, CR-03).
 * The route answers 409 with a `detail` naming a `code` and, for a terminal operation, its
 * `status`. Exactly one 409 is success: `companion_install_terminal` with status `complete`, which
 * means an earlier converge already completed this operation — re-converging (a crash between the
 * ready report and the local marker) must never flip a finished install, so it is
 * `already-terminal`. Every other 409 is `refused`, carrying the code and terminal status: an
 * operation the control plane already FAILED (its reporting deadline passed), a report naming an
 * operation it is not running, a job not running yet, a `ready` whose digest is not the pin, an
 * update report whose `to_digest` names another update than the one in flight (G-06-4). The
 * caller decides what a refusal means for its report; none may count it as delivered success.
 */
import { runtimeSecretValue } from "./secret-values";

export const REDACTED = "[redacted]";

/** The route's bounds (backend `MAX_INSTALL_REPORT_*`), applied AFTER redaction. */
export const MAX_REPORT_LINES = 200;
export const MAX_REPORT_LINE_CHARS = 2000;
export const MAX_REPORT_ERROR_CHARS = 1000;
export const MAX_REPORT_STEP_CHARS = 64;
/** The route's `to_digest` (backend `CompanionInstallReport.to_digest`): 64 lowercase hex. */
const REPORT_DIGEST_PATTERN = /^[a-f0-9]{64}$/;

/**
 * Env-file values shorter than this are configuration words (`true`, `90`, `postiz`), never a
 * generated credential. Replacing them would rewrite every line that mentions the companion by
 * name and leave the install log unreadable, while protecting nothing. Every WHOLE composed value
 * is redacted regardless of length, and so is the admin password whatever its length.
 */
export const MIN_ENV_SECRET_CHARS = 8;

export const REPORT_MAX_ATTEMPTS = 5;
export const REPORT_BASE_DELAY_MS = 1_000;
export const REPORT_MAX_DELAY_MS = 30_000;
export const REPORT_TIMEOUT_MS = 30_000;

export type CompanionInstallOperation = "install" | "update" | "export" | "uninstall";

export interface CompanionInstallLogLine {
	stream: "stdout" | "stderr";
	line: string;
}

/** What Postiz ANSWERED with the seeded key, never an HTTP status from `/` (D-16). */
export interface CompanionReadyAssertion {
	connected: boolean;
	organizations: number;
	/**
	 * After an update (plan 06-18): the organization count taken just before the digest swap. The
	 * update is ready only when `organizations` still equals it.
	 */
	previous_organizations?: number;
}

interface CompanionInstallReportBase {
	operation: CompanionInstallOperation;
	step?: string;
	lines?: CompanionInstallLogLine[];
	/**
	 * The app digest of the update this report was made for: 64 lowercase hex, the digest the swap
	 * moves to (G-06-4, D-28). Only `update` reports carry it, and it is set when the report is
	 * MADE, from the swap's own target -- never from the entry when the report is sent. A report
	 * kept in the receipt keeps it, so after an outage the control plane can refuse a report that
	 * names another update than the one it runs, instead of failing that later update with it.
	 * `sanitize` drops it from any other report and from a malformed value.
	 *
	 * `null` is stored in a queued report to record that the update's identity was left out on
	 * purpose (06-REVIEW WR-05), so it is never filled in from the receipt later. `sanitize` drops
	 * it, so it is never sent.
	 */
	to_digest?: string | null;
}

/**
 * The rehearsal marker written just before a backup (plan 06-15): a row in
 * `iforge_rehearsal.marker` whose token plan 06-16's rehearsal later looks for in a restore.
 */
export interface CompanionBackupMarker {
	id: number;
	/** 32 lowercase hex characters. */
	token: string;
	written_at: string;
}

/** One backup's outcome, backend `CompanionBackupReport`. */
export interface CompanionBackupResult {
	ok: boolean;
	/** pgBackRest's backup label, or `null` when none was produced or it was unreadable. */
	label: string | null;
	started_at: string;
	completed_at: string;
	db_bytes: number | null;
	marker: CompanionBackupMarker | null;
}

/**
 * A backup report (plan 06-15). Not a job phase: the control plane accepts it in any status,
 * because scheduled backups run long after the install completed. It carries no log lines and no
 * error text, which the route refuses on this phase.
 */
export interface CompanionBackupReport {
	operation: CompanionInstallOperation;
	phase: "backup";
	backup: CompanionBackupResult;
	step?: undefined;
	lines?: undefined;
}

/**
 * The companion is gone from this host (plan 06-20): its units, sources, credentials, data
 * directory and receipt were removed on the manifest's explicit `lifecycle: "remove"`. Carries no
 * payload; the control plane then tears down the companion's platform resources.
 */
export interface CompanionRemovedReport {
	operation: CompanionInstallOperation;
	phase: "removed";
	step?: undefined;
	lines?: undefined;
}

/**
 * The host conditions the runtime raises and clears (backend `CompanionHostConditionCode`, plan
 * 06-44): a rollback that did not finish (WR-07), a pinned app that would start on a schema
 * another digest wrote (WR-02), and a companion network whose live subnet is unverified (WR-06).
 */
export type CompanionHostConditionCode =
	| "update_rollback_incomplete"
	| "schema_digest_mismatch"
	| "network_subnet_unverified";

/**
 * A host condition raised or cleared (plan 06-44). Not a job phase: the control plane accepts it
 * in any status and it never moves the job. It records the condition on the installation, so the
 * host can report a state it must be repaired from even after its job ended. `detail` is
 * free text, redacted and bounded before sending, and always `null` for `cleared`.
 */
export interface CompanionConditionReport {
	operation: CompanionInstallOperation;
	phase: "condition";
	condition: {
		code: CompanionHostConditionCode;
		state: "raised" | "cleared";
		detail: string | null;
	};
	step?: undefined;
	lines?: undefined;
}

/** The runtime's half of backend `CompanionInstallReport`. */
export type CompanionInstallReport =
	| (CompanionInstallReportBase & { phase: "progress" })
	| (CompanionInstallReportBase & {
			phase: "ready";
			assertion: CompanionReadyAssertion;
			applied_digest: string;
			admin_user_id: string;
	  })
	| (CompanionInstallReportBase & { phase: "failed"; error: string })
	| CompanionBackupReport
	| CompanionRemovedReport
	| CompanionConditionReport;

/** The 409 code whose `complete` status is the one conflict that means "already recorded". */
export const COMPANION_INSTALL_TERMINAL_CODE = "companion_install_terminal";

/**
 * What the control plane answered.
 *
 * - `recorded`: the report landed.
 * - `already-terminal`: 409 `companion_install_terminal` with status `complete`; this operation
 *   already finished successfully, so nothing was recorded and nothing needs to be.
 * - `refused`: any other 409. `code` and `terminalStatus` are the detail's `code` and `status`, or
 *   `null` when the body did not carry them. Never success, never retried.
 */
export type CompanionReportResult =
	| { outcome: "recorded"; status: number }
	| { outcome: "already-terminal"; status: 409 }
	| { outcome: "refused"; status: 409; code: string | null; terminalStatus: string | null };

/**
 * The HTTP statuses that are the control plane's final answer about one report (06-REVIEW WR-02):
 * the report itself is malformed or unknown to this backend (400, 422). Re-sending the same report
 * cannot change them. 401 and 403 are not final: a fixed credential may send the same report
 * again.
 *
 * 404 is not final either (06-REVIEW WR-04). The backend answers it whenever the runtime key's
 * agent binding does not resolve: a key not bound yet, one being re-bound, or an environment that
 * was archived and then restored. A later pass can change that answer.
 */
const FINAL_REJECTION_STATUSES: ReadonlySet<number> = new Set([400, 422]);

/** The status that answers "the key reaches no installation right now" (06-REVIEW WR-04). */
const KEEP_DUE_REJECTION_STATUS = 404;

/**
 * A report the control plane rejected with a 4xx other than 409 or 429 (409 answers as a
 * `refused` result, 429 is retried). The message is the redacted
 * `install report refused: HTTP <status>: <detail>`. Two flags classify it:
 *
 * - `final`: a rejection re-sending cannot change (400, 422).
 * - `keepDue`: a 404, whose answer may change once the key's binding resolves.
 *
 * Neither flag is set for 401 or 403.
 */
export class CompanionReportRejectedError extends Error {
	readonly final: boolean;

	/**
	 * The caller should skip this report, keep it due for the next pass, and continue with the
	 * next one (06-REVIEW WR-04).
	 */
	readonly keepDue: boolean;

	constructor(
		readonly status: number,
		message: string,
	) {
		super(message);
		this.name = "CompanionReportRejectedError";
		this.final = FINAL_REJECTION_STATUSES.has(status);
		this.keepDue = status === KEEP_DUE_REJECTION_STATUS;
	}
}

/** Where install reports go. The production sink is `CompanionProgressReporter`. */
export interface CompanionReportSink {
	send(report: CompanionInstallReport): Promise<CompanionReportResult>;
}

export type CompanionFetch = (input: string, init?: RequestInit) => Promise<Response>;

/**
 * Replace every non-empty secret value in `text` with `[redacted]`, longest first, so a value that
 * contains another (a connection string and the password inside it) is replaced whole.
 *
 * The re-scan is bounded: a replacement cannot create a new occurrence in practice, but "no
 * reported text contains a secret" is checked rather than assumed, and a text that still carries
 * one after three passes is replaced entirely.
 */
export function redactKnownSecrets(text: string, secrets: readonly string[]): string {
	const ordered = [...new Set(secrets.filter((secret) => secret.length > 0))].sort(
		(left, right) => right.length - left.length,
	);
	let out = text;
	for (let pass = 0; pass < 3; pass += 1) {
		let found = false;
		for (const secret of ordered) {
			if (out.includes(secret)) {
				out = out.split(secret).join(REDACTED);
				found = true;
			}
		}
		if (!found) return out;
	}
	return REDACTED;
}

/**
 * Every spelling of one value a log line can plausibly carry: the value, and its URL-encoded
 * forms (component, form-style, and each with lowercase percent escapes). A connection string or
 * a query parameter is exactly where a companion prints a password. Mirrors the backend's
 * `credential_forms`.
 */
export function secretForms(value: string): string[] {
	if (value.length === 0) return [];
	const component = encodeURIComponent(value);
	const encoded = [component, encodeURI(value), component.replace(/%20/g, "+")];
	const lowered = encoded.map((form) =>
		form.replace(/%[0-9A-F]{2}/g, (percent) => percent.toLowerCase()),
	);
	return [...new Set([value, ...encoded, ...lowered])];
}

const ENV_LINE = /^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/;

function embeddedUrlPassword(value: string): string[] {
	try {
		const url = new URL(value);
		if (!url.password) return [];
		return [url.password, decodeURIComponent(url.password)];
	} catch {
		return [];
	}
}

/**
 * The secret values of one installation, as the runtime holds them: each composed credential
 * value named by `secretRefs`, each value inside those that are env files (`KEY=VALUE` lines),
 * the password inside any connection URL among them, and the string members of a JSON-object
 * credential (the admin bootstrap). `extra` adds values that are secret for other reasons, such as
 * the runtime's own control-plane credential. Every value is returned with its encoded forms.
 */
export function installationSecretValues(
	secretRefs: readonly string[],
	secretValues: Record<string, string>,
	extra: readonly string[] = [],
): string[] {
	const out = new Set<string>();
	const add = (value: string) => {
		for (const form of secretForms(value)) out.add(form);
	};
	for (const ref of secretRefs) {
		const value = runtimeSecretValue(secretValues, ref);
		if (value === null) continue;
		add(value);
		for (const raw of value.split("\n")) {
			const match = ENV_LINE.exec(raw.trim());
			const envValue = match?.[2]?.trim() ?? "";
			if (envValue.length >= MIN_ENV_SECRET_CHARS) add(envValue);
			for (const password of embeddedUrlPassword(envValue)) add(password);
		}
		for (const [key, member] of jsonStringMembers(value)) {
			if (key === "password" || member.length >= MIN_ENV_SECRET_CHARS) add(member);
		}
	}
	for (const value of extra) add(value);
	return [...out];
}

function jsonStringMembers(value: string): Array<[string, string]> {
	const trimmed = value.trim();
	if (!trimmed.startsWith("{")) return [];
	try {
		const parsed: unknown = JSON.parse(trimmed);
		if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return [];
		return Object.entries(parsed).filter(
			(pair): pair is [string, string] => typeof pair[1] === "string" && pair[1].length > 0,
		);
	} catch {
		return [];
	}
}

export interface CompanionProgressReporterOptions {
	/** The control plane's base URL (`manifest.controlPlane.apiUrl`). */
	apiUrl: string;
	/** The runtime deployment credential (`readRuntimeCredential`). Sent as a bearer token. */
	token: string;
	installationId: string;
	/** Every value to remove before sending (`installationSecretValues`). */
	secrets: readonly string[];
	fetch?: CompanionFetch;
	sleep?: (ms: number) => Promise<void>;
	maxAttempts?: number;
	baseDelayMs?: number;
	maxDelayMs?: number;
	timeoutMs?: number;
}

/**
 * Classify one 409 body: `already-terminal` only for `companion_install_terminal` with status
 * `complete`, `refused` (with whatever code and status the body named) for everything else,
 * including a plain-string detail or a body that is not JSON at all.
 */
function conflictResult(body: string): CompanionReportResult {
	let code: string | null = null;
	let terminalStatus: string | null = null;
	try {
		const parsed: unknown = JSON.parse(body);
		const detail =
			typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
				? (parsed as Record<string, unknown>).detail
				: undefined;
		if (typeof detail === "object" && detail !== null && !Array.isArray(detail)) {
			const fields = detail as Record<string, unknown>;
			code = typeof fields.code === "string" ? fields.code : null;
			terminalStatus = typeof fields.status === "string" ? fields.status : null;
		}
	} catch {
		// Not JSON: a refusal whose reason the runtime cannot read.
	}
	if (code === COMPANION_INSTALL_TERMINAL_CODE && terminalStatus === "complete") {
		return { outcome: "already-terminal", status: 409 };
	}
	return { outcome: "refused", status: 409, code, terminalStatus };
}

/**
 * The same sanitized job report without its `to_digest`, or `null` when it carries none (absent,
 * or a `null` left out on purpose, 06-REVIEW WR-05): what the one-shot resend sends to a backend
 * that predates the field (G-06-4).
 */
function withoutReportDigest(report: CompanionInstallReport): CompanionInstallReport | null {
	if (report.phase !== "progress" && report.phase !== "ready" && report.phase !== "failed") {
		return null;
	}
	if (typeof report.to_digest !== "string") return null;
	const { to_digest: _dropped, ...rest } = report;
	return rest;
}

/**
 * Whether a 422 body is FastAPI's `extra_forbidden` error for `to_digest` itself: the answer a
 * backend that predates the field gives (G-06-4). A substring match is not enough, because the
 * default validation handler echoes the request body as each error's `input`, so every body-level
 * 422 on an identified report contains the text `to_digest` (06-REVIEW WR-01).
 */
function namesExtraReportDigest(text: string): boolean {
	let parsed: unknown;
	try {
		parsed = JSON.parse(text);
	} catch {
		return false;
	}
	if (typeof parsed !== "object" || parsed === null) return false;
	const { detail } = parsed as { detail?: unknown };
	if (!Array.isArray(detail)) return false;
	return detail.some((entry: unknown) => {
		if (typeof entry !== "object" || entry === null) return false;
		const { type, loc } = entry as { type?: unknown; loc?: unknown };
		return type === "extra_forbidden" && Array.isArray(loc) && loc.at(-1) === "to_digest";
	});
}

function truncate(value: string, limit: number): string {
	return value.length <= limit ? value : value.slice(0, limit);
}

function defaultSleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * The production report sink.
 *
 * Transport failures, 429 and 5xx are retried with a doubling delay capped at `maxDelayMs`, up to
 * `maxAttempts` sends, then the error surfaces. Any other 4xx is a refusal the same payload can
 * never fix, so it surfaces at once. Every error message is redacted too: it is built from a
 * response the runtime does not control.
 */
export class CompanionProgressReporter implements CompanionReportSink {
	private readonly url: string;
	private readonly secrets: string[];

	constructor(private readonly options: CompanionProgressReporterOptions) {
		const base = new URL(options.apiUrl);
		if (base.protocol !== "https:" && base.protocol !== "http:") {
			throw new Error(`install reports need an http(s) control plane, not ${base.protocol}`);
		}
		this.url = `${options.apiUrl.replace(/\/+$/, "")}/v1/runtime/companion-installations/${encodeURIComponent(options.installationId)}/report`;
		this.secrets = [...options.secrets, ...secretForms(options.token)];
	}

	/** The report exactly as it will be sent: redacted, then cut to the route's bounds. */
	sanitize(report: CompanionInstallReport): CompanionInstallReport {
		const redact = (text: string) => redactKnownSecrets(text, this.secrets);
		if (report.phase === "backup") {
			// Numbers, timestamps, a validated label and a hex token: no free text to redact.
			return { ...report, backup: { ...report.backup } };
		}
		if (report.phase === "removed") {
			// No payload at all: nothing to redact.
			return { operation: report.operation, phase: "removed" };
		}
		if (report.phase === "condition") {
			// The detail is host-composed text: redacted, bounded, and never sent for a clear. The
			// route refuses an empty detail, so one that redacts to nothing is sent as null.
			const { code, state, detail } = report.condition;
			const bounded =
				state === "raised" && detail !== null
					? truncate(redact(detail), MAX_REPORT_ERROR_CHARS)
					: "";
			return {
				operation: report.operation,
				phase: "condition",
				condition: { code, state, detail: bounded.length > 0 ? bounded : null },
			};
		}
		const out: Exclude<
			CompanionInstallReport,
			CompanionBackupReport | CompanionRemovedReport | CompanionConditionReport
		> = {
			...report,
		};
		// Only an update report names the update it belongs to, and only as a digest the route
		// accepts: anything else would turn a report the control plane must learn into a final 422.
		// A `null` (the identity left out on purpose, 06-REVIEW WR-05) is never sent either.
		if (
			typeof out.to_digest !== "string" ||
			out.operation !== "update" ||
			!REPORT_DIGEST_PATTERN.test(out.to_digest)
		) {
			delete out.to_digest;
		}
		if (report.step !== undefined) {
			out.step = truncate(redact(report.step), MAX_REPORT_STEP_CHARS);
		}
		if (report.lines !== undefined) {
			out.lines = report.lines.slice(-MAX_REPORT_LINES).map((line) => ({
				stream: line.stream,
				line: truncate(redact(line.line), MAX_REPORT_LINE_CHARS),
			}));
		}
		if (out.phase === "failed" && report.phase === "failed") {
			out.error = truncate(redact(report.error), MAX_REPORT_ERROR_CHARS) || "failed";
		}
		return out;
	}

	async send(report: CompanionInstallReport): Promise<CompanionReportResult> {
		const fetchImpl = this.options.fetch ?? fetch;
		const sleep = this.options.sleep ?? defaultSleep;
		const maxAttempts = Math.max(1, this.options.maxAttempts ?? REPORT_MAX_ATTEMPTS);
		const maxDelay = this.options.maxDelayMs ?? REPORT_MAX_DELAY_MS;
		let delay = this.options.baseDelayMs ?? REPORT_BASE_DELAY_MS;
		const sanitized = this.sanitize(report);
		let body = JSON.stringify(sanitized);
		// Rollout compatibility (G-06-4). The backend that accepts `to_digest` ships before the CLI
		// that sends it, but a host that meets an older backend anyway gets a 422 `extra_forbidden`
		// naming the field. Re-sending the same body would get the same answer on every pass and
		// wedge the receipt's pending update report forever. So on exactly that answer the report
		// is re-sent once, at once, without the field: it then lands unchecked, as every report did
		// before the control plane could check it. Any other answer keeps its path, and a second
		// 422 surfaces as the final rejection it is.
		let unidentified = withoutReportDigest(sanitized);
		for (let attempt = 1; ; attempt += 1) {
			let response: Response | null = null;
			let lastFailure = "";
			try {
				response = await fetchImpl(this.url, {
					method: "POST",
					headers: {
						authorization: `Bearer ${this.options.token}`,
						"content-type": "application/json",
					},
					body,
					// A redirect would carry the bearer token somewhere this client never chose.
					redirect: "error",
					signal: AbortSignal.timeout(this.options.timeoutMs ?? REPORT_TIMEOUT_MS),
				});
			} catch (error) {
				lastFailure = error instanceof Error ? error.message : String(error);
			}
			if (response !== null) {
				if (response.ok) return { outcome: "recorded", status: response.status };
				if (response.status === 409) {
					return conflictResult(await response.text().catch(() => ""));
				}
				const text = await response.text().catch(() => "");
				if (response.status === 422 && unidentified !== null && namesExtraReportDigest(text)) {
					body = JSON.stringify(unidentified);
					unidentified = null;
					// The resend is not a retry: it keeps every attempt the report had (06-REVIEW IN-01).
					attempt -= 1;
					continue;
				}
				const detail = truncate(text.trim(), 300);
				lastFailure = `HTTP ${response.status}${detail ? `: ${detail}` : ""}`;
				if (response.status < 500 && response.status !== 429) {
					throw new CompanionReportRejectedError(
						response.status,
						this.redact(`install report refused: ${lastFailure}`),
					);
				}
			}
			if (attempt >= maxAttempts) {
				throw new Error(
					this.redact(`install report failed after ${attempt} attempts: ${lastFailure}`),
				);
			}
			await sleep(delay);
			delay = Math.min(delay * 2, maxDelay);
		}
	}

	private redact(text: string): string {
		return redactKnownSecrets(text, this.secrets);
	}
}
