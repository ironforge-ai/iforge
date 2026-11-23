import { describe, expect, it } from "bun:test";
import {
	type CompanionConditionReport,
	type CompanionFetch,
	type CompanionInstallReport,
	CompanionProgressReporter,
	CompanionReportRejectedError,
	MAX_REPORT_ERROR_CHARS,
	REDACTED,
	REPORT_MAX_ATTEMPTS,
	redactKnownSecrets,
} from "./companion-progress";

const API_URL = "https://cloud-api.example.test";
const INSTALLATION_ID = "5f0c6d2e-8a41-4b7f-9e3c-2d1a7b6e4c90";
const RUNTIME_TOKEN = "runtime-token-3a7c55e1";
const DB_PASSWORD = "db-password-4b8e02aa";
const API_KEY = "api-key-9d1e77c0";
const DATABASE_URL = `postgresql://postiz:${DB_PASSWORD}@iforge-postiz-db:5432/postiz`;

interface RecordedRequest {
	url: string;
	method: string;
	headers: Record<string, string>;
	body: unknown;
}

function recordingFetch(
	answer: (attempt: number, request: RecordedRequest) => Response | Error = () =>
		Response.json({ status: "running", step: null, stored_lines: 0 }),
): { fetch: CompanionFetch; requests: RecordedRequest[] } {
	const requests: RecordedRequest[] = [];
	const fetch: CompanionFetch = async (input, init = {}) => {
		const headers: Record<string, string> = {};
		new Headers(init.headers).forEach((value, key) => {
			headers[key] = value;
		});
		const request: RecordedRequest = {
			url: input,
			method: (init.method ?? "GET").toUpperCase(),
			headers,
			body: typeof init.body === "string" ? JSON.parse(init.body) : null,
		};
		requests.push(request);
		const result = answer(requests.length, request);
		if (result instanceof Error) throw result;
		return result;
	};
	return { fetch, requests };
}

function reporter(fetch: CompanionFetch, delays: number[] = []) {
	return new CompanionProgressReporter({
		apiUrl: API_URL,
		token: RUNTIME_TOKEN,
		installationId: INSTALLATION_ID,
		secrets: [DATABASE_URL, DB_PASSWORD, API_KEY],
		fetch,
		sleep: async (ms) => {
			delays.push(ms);
		},
	});
}

describe("companion progress reporter tracer", () => {
	it("POSTs a redacted report to the installation's report route with the runtime credential", async () => {
		const { fetch, requests } = recordingFetch();
		const report: CompanionInstallReport = {
			operation: "install",
			phase: "progress",
			step: "readiness",
			lines: [
				{ stream: "stdout", line: `connecting to ${DATABASE_URL}` },
				{ stream: "stderr", line: `apiKey=${API_KEY} rejected` },
			],
		};
		const result = await reporter(fetch).send(report);

		expect(result).toEqual({ outcome: "recorded", status: 200 });
		expect(requests).toHaveLength(1);
		const [request] = requests;
		expect(request?.url).toBe(
			`${API_URL}/v1/runtime/companion-installations/${INSTALLATION_ID}/report`,
		);
		expect(request?.method).toBe("POST");
		expect(request?.headers.authorization).toBe(`Bearer ${RUNTIME_TOKEN}`);
		expect(request?.headers["content-type"]).toBe("application/json");
		expect(request?.body).toEqual({
			operation: "install",
			phase: "progress",
			step: "readiness",
			lines: [
				{ stream: "stdout", line: `connecting to ${REDACTED}` },
				{ stream: "stderr", line: `apiKey=${REDACTED} rejected` },
			],
		});
		const wire = JSON.stringify(request?.body);
		for (const secret of [DB_PASSWORD, API_KEY, RUNTIME_TOKEN]) {
			expect(wire).not.toContain(secret);
		}
	});

	it("redacts a failed report's error as well as its lines", async () => {
		const { fetch, requests } = recordingFetch();
		await reporter(fetch).send({
			operation: "install",
			phase: "failed",
			step: "key-seed",
			error: `psql failed: LINE 1: UPDATE "Organization" SET "apiKey" = '${API_KEY}'`,
		});
		const body = requests[0]?.body as { error: string };
		expect(body.error).toBe(
			`psql failed: LINE 1: UPDATE "Organization" SET "apiKey" = '${REDACTED}'`,
		);
	});
});

describe("companion progress: redaction and delivery", () => {
	it("replaces overlapping secrets longest-first and leaves unrelated text intact", () => {
		const text = `url=${DATABASE_URL} pw=${DB_PASSWORD} status=ok postiz is starting`;
		const out = redactKnownSecrets(text, [DB_PASSWORD, DATABASE_URL, ""]);
		// The URL is replaced whole, not as "postgresql://postiz:[redacted]@…".
		expect(out).toBe(`url=${REDACTED} pw=${REDACTED} status=ok postiz is starting`);
		expect(redactKnownSecrets("nothing secret here", [DB_PASSWORD])).toBe("nothing secret here");
		expect(redactKnownSecrets("", [DB_PASSWORD])).toBe("");
	});

	const READY_REPORT: CompanionInstallReport = {
		operation: "install",
		phase: "ready",
		step: "ready",
		assertion: { connected: true, organizations: 1 },
		applied_digest: "0".repeat(64),
		admin_user_id: "7c1d9a64-2b3e-4f5a-8c6d-0e1f2a3b4c5d",
	};

	it("treats a 409 answer to a ready report as success and does not retry it", async () => {
		const { fetch, requests } = recordingFetch(() =>
			Response.json(
				{
					detail: {
						code: "companion_install_terminal",
						message: "The install operation already ended (complete).",
						operation: "install",
						status: "complete",
					},
				},
				{ status: 409 },
			),
		);
		const result = await reporter(fetch).send(READY_REPORT);
		expect(result).toEqual({ outcome: "already-terminal", status: 409 });
		expect(requests).toHaveLength(1);
	});

	it("settles a re-sent update ready the control plane already recorded, and does not retry it", async () => {
		// Plan 06-44 (WR-01): the exact detail the backend sends for an update ready whose digest it
		// already recorded as applied while a later update runs. already-terminal, never a refusal,
		// so the runtime never rolls a completed update back.
		const { fetch, requests } = recordingFetch(() =>
			Response.json(
				{
					detail: {
						code: "companion_install_terminal",
						message: "This update's ready was already recorded; the installation runs that digest.",
						operation: "update",
						status: "complete",
					},
				},
				{ status: 409 },
			),
		);
		const result = await reporter(fetch).send({ ...READY_REPORT, operation: "update" });
		expect(result).toEqual({ outcome: "already-terminal", status: 409 });
		expect(requests).toHaveLength(1);
	});

	it("answers refused, not success, when the operation already FAILED, and does not retry it", async () => {
		const { fetch, requests } = recordingFetch(() =>
			Response.json(
				{
					detail: {
						code: "companion_install_terminal",
						message: "The update operation already ended (failed).",
						operation: "update",
						status: "failed",
					},
				},
				{ status: 409 },
			),
		);
		const result = await reporter(fetch).send({ ...READY_REPORT, operation: "update" });
		expect(result).toEqual({
			outcome: "refused",
			status: 409,
			code: "companion_install_terminal",
			terminalStatus: "failed",
		});
		expect(requests).toHaveLength(1);
	});

	it("answers refused for an operation mismatch, naming the code, and does not retry it", async () => {
		const { fetch, requests } = recordingFetch(() =>
			Response.json(
				{
					detail: {
						code: "companion_report_operation_mismatch",
						message: "This installation's current operation is update, not install.",
						operation: "update",
					},
				},
				{ status: 409 },
			),
		);
		const result = await reporter(fetch).send(READY_REPORT);
		expect(result).toEqual({
			outcome: "refused",
			status: 409,
			code: "companion_report_operation_mismatch",
			terminalStatus: null,
		});
		expect(requests).toHaveLength(1);
	});

	it("answers refused with no code for a 409 whose detail is a plain string, and does not retry it", async () => {
		const { fetch, requests } = recordingFetch(() =>
			Response.json({ detail: "operation already complete" }, { status: 409 }),
		);
		const result = await reporter(fetch).send(READY_REPORT);
		expect(result).toEqual({ outcome: "refused", status: 409, code: null, terminalStatus: null });
		expect(requests).toHaveLength(1);
	});

	it("answers refused with no code for a 409 whose body is not JSON", async () => {
		const { fetch, requests } = recordingFetch(() => new Response("conflict", { status: 409 }));
		const result = await reporter(fetch).send(READY_REPORT);
		expect(result).toEqual({ outcome: "refused", status: 409, code: null, terminalStatus: null });
		expect(requests).toHaveLength(1);
	});

	it("retries a 5xx up to its bound with doubling delays, then surfaces a redacted error", async () => {
		const delays: number[] = [];
		const { fetch, requests } = recordingFetch(
			() => new Response(`upstream leaked ${API_KEY}`, { status: 503 }),
		);
		const send = reporter(fetch, delays).send({
			operation: "install",
			phase: "progress",
			step: "readiness",
		});
		let message = "";
		try {
			await send;
		} catch (error) {
			message = error instanceof Error ? error.message : String(error);
		}
		expect(requests).toHaveLength(5);
		expect(delays).toEqual([1000, 2000, 4000, 8000]);
		expect(message).toContain("HTTP 503");
		expect(message).toContain("5 attempts");
		expect(message).not.toContain(API_KEY);
	});

	it("retries a transport failure and succeeds once the control plane answers", async () => {
		const { fetch, requests } = recordingFetch((attempt) =>
			attempt < 3 ? new TypeError("fetch failed") : Response.json({ status: "running" }),
		);
		const result = await reporter(fetch).send({ operation: "install", phase: "progress" });
		expect(result.outcome).toBe("recorded");
		expect(requests).toHaveLength(3);
	});

	it("surfaces a 4xx refusal at once without retrying", async () => {
		const { fetch, requests } = recordingFetch(
			() => new Response("ready needs connected", { status: 422 }),
		);
		await expect(
			reporter(fetch).send({ operation: "install", phase: "failed", error: "x" }),
		).rejects.toThrow("HTTP 422");
		expect(requests).toHaveLength(1);
	});

	it.each([400, 422])(
		"marks a %i answer as the control plane's final rejection, carrying its status (06-REVIEW WR-02)",
		async (status) => {
			const { fetch } = recordingFetch(() => new Response("unknown phase", { status }));
			await expect(
				reporter(fetch).send({ operation: "install", phase: "failed", error: "x" }),
			).rejects.toMatchObject({ status, final: true, keepDue: false });
		},
	);

	it("never marks a 404 as final: the key's binding may resolve, so the report is kept due (06-REVIEW WR-04)", async () => {
		const { fetch, requests } = recordingFetch(
			() => new Response("Installation not found", { status: 404 }),
		);
		await expect(
			reporter(fetch).send({ operation: "install", phase: "failed", error: "x" }),
		).rejects.toMatchObject({ status: 404, final: false, keepDue: true });
		expect(requests).toHaveLength(1);
	});

	it.each([401, 403])(
		"never marks a %i answer as final: the credential may be fixed and the report sent again",
		async (status) => {
			const { fetch } = recordingFetch(() => new Response("denied", { status }));
			await expect(
				reporter(fetch).send({ operation: "install", phase: "failed", error: "x" }),
			).rejects.toMatchObject({ status, final: false, keepDue: false });
		},
	);

	it("bounds a report to the route's limits after redaction", async () => {
		const { fetch, requests } = recordingFetch();
		const lines = Array.from({ length: 250 }, (_, index) => ({
			stream: "stdout" as const,
			line: `${index} ${API_KEY} ${"x".repeat(2100)}`,
		}));
		await reporter(fetch).send({
			operation: "install",
			phase: "failed",
			step: "s".repeat(80),
			error: `${API_KEY} ${"e".repeat(1200)}`,
			lines,
		});
		const body = requests[0]?.body as {
			step: string;
			error: string;
			lines: Array<{ line: string }>;
		};
		expect(body.step).toHaveLength(64);
		expect(body.error.length).toBeLessThanOrEqual(1000);
		expect(body.error.startsWith(REDACTED)).toBe(true);
		expect(body.lines).toHaveLength(200);
		// The newest lines are the ones kept.
		expect(body.lines[199]?.line.startsWith(`249 ${REDACTED}`)).toBe(true);
		for (const line of body.lines) expect(line.line.length).toBeLessThanOrEqual(2000);
	});
});

describe("companion progress: the host-condition report (plan 06-44)", () => {
	function condition(state: "raised" | "cleared", detail: string | null): CompanionConditionReport {
		return {
			operation: "update",
			phase: "condition",
			condition: { code: "update_rollback_incomplete", state, detail },
		};
	}

	it("sends a raised condition with its detail redacted and bounded, and no step or lines", async () => {
		const { fetch, requests } = recordingFetch();
		const detail = `restore failed against ${DATABASE_URL} with ${API_KEY} ${"d".repeat(1200)}`;

		const result = await reporter(fetch).send(condition("raised", detail));

		expect(result).toEqual({ outcome: "recorded", status: 200 });
		const body = requests[0]?.body as Record<string, unknown>;
		expect(Object.keys(body).sort()).toEqual(["condition", "operation", "phase"]);
		const sent = body.condition as { code: string; state: string; detail: string };
		expect(sent.code).toBe("update_rollback_incomplete");
		expect(sent.state).toBe("raised");
		expect(sent.detail.startsWith(`restore failed against ${REDACTED} with ${REDACTED} `)).toBe(
			true,
		);
		expect(sent.detail).toHaveLength(MAX_REPORT_ERROR_CHARS);
		const wire = JSON.stringify(body);
		for (const secret of [DB_PASSWORD, API_KEY, RUNTIME_TOKEN]) {
			expect(wire).not.toContain(secret);
		}
	});

	it("sends a null detail for a cleared condition, whatever the caller passed", () => {
		const sanitized = reporter(recordingFetch().fetch).sanitize(
			condition("cleared", `left over ${API_KEY}`),
		);
		expect(sanitized).toEqual({
			operation: "update",
			phase: "condition",
			condition: { code: "update_rollback_incomplete", state: "cleared", detail: null },
		});
	});

	it("sends a null detail when the detail is empty after redaction", () => {
		const sink = reporter(recordingFetch().fetch);
		for (const detail of ["", null]) {
			const sanitized = sink.sanitize(condition("raised", detail)) as CompanionConditionReport;
			expect(sanitized.condition.detail).toBeNull();
		}
	});
});

describe("companion progress: an update report names the update it belongs to (G-06-4)", () => {
	const TO_DIGEST = "a".repeat(64);
	type JobReport = Extract<CompanionInstallReport, { phase: "progress" | "failed" | "ready" }>;
	const UPDATE_REPORTS: JobReport[] = [
		{ operation: "update", phase: "progress", step: "swap", to_digest: TO_DIGEST },
		{
			operation: "update",
			phase: "failed",
			step: "backup",
			error: "pgbackrest diff backup failed",
			to_digest: TO_DIGEST,
		},
		{
			operation: "update",
			phase: "ready",
			step: "ready",
			assertion: { connected: true, organizations: 1, previous_organizations: 1 },
			applied_digest: TO_DIGEST,
			admin_user_id: "7c1d9a64-2b3e-4f5a-8c6d-0e1f2a3b4c5d",
			to_digest: TO_DIGEST,
		},
	];

	function sink() {
		return reporter(recordingFetch().fetch);
	}

	it.each(UPDATE_REPORTS.map((report) => [report.phase, report] as const))(
		"keeps a valid to_digest on an update %s report, with every other field unchanged",
		(_phase, report) => {
			expect(sink().sanitize(report)).toStrictEqual(report);
		},
	);

	it.each(UPDATE_REPORTS.map((report) => [report.phase, report] as const))(
		"drops to_digest from an install %s report, with every other field unchanged",
		(_phase, report) => {
			const install: JobReport = { ...report, operation: "install" };
			const { to_digest: _dropped, ...expected } = install;

			const sanitized = sink().sanitize(install);

			expect(sanitized).toStrictEqual(expected);
			expect("to_digest" in sanitized).toBe(false);
		},
	);

	it.each([
		["uppercase hex", "A".repeat(64)],
		["63 characters", "a".repeat(63)],
		["65 characters", "a".repeat(65)],
		["a non-hex character", `${"a".repeat(63)}g`],
		["an empty string", ""],
		["a sha256: prefix", `sha256:${"a".repeat(64)}`],
		["null (left out on purpose, 06-REVIEW WR-05)", null],
	])("drops a to_digest that is %s, with every other field unchanged", (_case, value) => {
		for (const valid of UPDATE_REPORTS) {
			const malformed: JobReport = { ...valid, to_digest: value };
			const { to_digest: _dropped, ...expected } = malformed;

			const sanitized = sink().sanitize(malformed);

			expect(sanitized).toStrictEqual(expected);
			expect("to_digest" in sanitized).toBe(false);
		}
	});
});

describe("companion progress: a backend that predates to_digest cannot wedge a report (G-06-4 rollout)", () => {
	const TO_DIGEST = "b".repeat(64);
	const IDENTIFIED_FAILED: CompanionInstallReport = {
		operation: "update",
		phase: "failed",
		step: "backup",
		error: "pgbackrest diff backup failed",
		to_digest: TO_DIGEST,
	};

	/** What a backend without the field answers: FastAPI's 422 naming the extra input. */
	function extraForbidden(): Response {
		return Response.json(
			{
				detail: [
					{
						type: "extra_forbidden",
						loc: ["body", "to_digest"],
						msg: "Extra inputs are not permitted",
						input: TO_DIGEST,
					},
				],
			},
			{ status: 422 },
		);
	}

	async function rejection(promise: Promise<unknown>): Promise<unknown> {
		return promise.then(
			() => null,
			(error: unknown) => error,
		);
	}

	it("re-sends the report once, at once, without to_digest when a 422 names it", async () => {
		const delays: number[] = [];
		const { fetch, requests } = recordingFetch((attempt) =>
			attempt === 1 ? extraForbidden() : Response.json({ status: "failed" }),
		);

		const result = await reporter(fetch, delays).send(IDENTIFIED_FAILED);

		expect(result).toEqual({ outcome: "recorded", status: 200 });
		expect(requests).toHaveLength(2);
		const [first, second] = requests.map((request) => request.body as Record<string, unknown>);
		expect(first).toMatchObject({ to_digest: TO_DIGEST });
		expect(second).not.toHaveProperty("to_digest");
		const { to_digest: _dropped, ...rest } = first ?? {};
		expect(second).toEqual(rest);
		expect(delays).toEqual([]);
	});

	it("surfaces the same 422 on the resend as a final rejection, after exactly two POSTs", async () => {
		const { fetch, requests } = recordingFetch(() => extraForbidden());

		const error = await rejection(reporter(fetch).send(IDENTIFIED_FAILED));

		expect(error).toBeInstanceOf(CompanionReportRejectedError);
		expect(error).toMatchObject({ status: 422, final: true });
		expect(requests).toHaveLength(2);
		expect(requests[1]?.body).not.toHaveProperty("to_digest");
	});

	it("surfaces a body-level 422 that echoes to_digest in its input at once, after one POST", async () => {
		// FastAPI's default handler echoes the whole request body as a model-level error's `input`,
		// so the text `to_digest` appears in every body-level 422 on an identified report (WR-01).
		const { fetch, requests } = recordingFetch(() =>
			Response.json(
				{
					detail: [
						{
							type: "value_error",
							loc: ["body"],
							msg: "Value error, a failed report requires: error",
							input: { ...IDENTIFIED_FAILED },
						},
					],
				},
				{ status: 422 },
			),
		);

		const error = await rejection(reporter(fetch).send(IDENTIFIED_FAILED));

		expect(error).toBeInstanceOf(CompanionReportRejectedError);
		expect(error).toMatchObject({ status: 422, final: true });
		expect(requests).toHaveLength(1);
	});

	it("surfaces a field-level 422 whose input names to_digest at once, after one POST", async () => {
		const { fetch, requests } = recordingFetch(() =>
			Response.json(
				{
					detail: [
						{
							type: "missing",
							loc: ["body", "error"],
							msg: "Field required",
							input: { ...IDENTIFIED_FAILED },
						},
					],
				},
				{ status: 422 },
			),
		);

		const error = await rejection(reporter(fetch).send(IDENTIFIED_FAILED));

		expect(error).toBeInstanceOf(CompanionReportRejectedError);
		expect(error).toMatchObject({ status: 422, final: true });
		expect(requests).toHaveLength(1);
	});

	it("keeps every retry attempt for the resent report", async () => {
		const delays: number[] = [];
		const { fetch, requests } = recordingFetch((attempt) =>
			attempt === 1 ? extraForbidden() : new Response("unavailable", { status: 503 }),
		);

		const error = await rejection(reporter(fetch, delays).send(IDENTIFIED_FAILED));

		expect(error).toBeInstanceOf(Error);
		expect(error).not.toBeInstanceOf(CompanionReportRejectedError);
		expect(String((error as Error).message)).toContain(`after ${REPORT_MAX_ATTEMPTS} attempts`);
		expect(requests).toHaveLength(REPORT_MAX_ATTEMPTS + 1);
	});

	it("surfaces a 422 to a report without to_digest at once, after one POST", async () => {
		const { fetch, requests } = recordingFetch(() => extraForbidden());
		const { to_digest: _none, ...unidentified } = IDENTIFIED_FAILED as Extract<
			CompanionInstallReport,
			{ phase: "failed" }
		>;

		const error = await rejection(reporter(fetch).send(unidentified));

		expect(error).toBeInstanceOf(CompanionReportRejectedError);
		expect(error).toMatchObject({ status: 422, final: true });
		expect(requests).toHaveLength(1);
	});

	it("answers refused for a report naming another update, and never re-sends it without to_digest", async () => {
		const { fetch, requests } = recordingFetch(() =>
			Response.json(
				{
					detail: {
						code: "companion_update_identity_mismatch",
						message: "This report names another update than the one in flight.",
						operation: "update",
						status: "running",
					},
				},
				{ status: 409 },
			),
		);

		const result = await reporter(fetch).send(IDENTIFIED_FAILED);

		expect(result).toEqual({
			outcome: "refused",
			status: 409,
			code: "companion_update_identity_mismatch",
			terminalStatus: "running",
		});
		expect(requests).toHaveLength(1);
		expect(requests[0]?.body).toMatchObject({ to_digest: TO_DIGEST });
	});
});
