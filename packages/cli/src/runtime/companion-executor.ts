/**
 * The runtime-side companion executor: claim a pre-authorized call, MINT a credential, execute.
 *
 * D-02 splits the companion call path — the control plane polices, the runtime executes. This is
 * the runtime half. It is the only place in the system where a companion credential exists in
 * usable form, and it never travels: the control plane sends a call DESCRIPTOR carrying no secret
 * material, and this module authenticates each call in the way the companion's KIND declares.
 * `filebrowser` gets a fresh short-lived `X-JWT-Assertion` per call, derived from the host-resident
 * manifest and discarded. `postiz` gets its static API key in a bare `Authorization` header, read
 * from the root-only cached secret values that the manifest's `credentials.apiKeySecretRef` names
 * (06-07; `public.auth.middleware.ts` at v2.24.0 takes no `Bearer` prefix). The key is set on the
 * request and nowhere else: never logged, never reported, never placed in an error message.
 *
 * **Mint, never forward.** The agent's own token is never sent downstream — the MCP specification
 * is explicit that a server MUST NOT pass through a client's token to an upstream service, and
 * the same reasoning applies here with more force because the downstream is a filesystem
 * (T-5-03). Each assertion carries `sub`, `groups` and a seconds-scale `exp`, so a copy that
 * escaped a log would be worthless within two minutes. It is never logged, never returned and
 * never placed in the reported result body.
 *
 * **No URL comes from the envelope.** The host is loopback and the port is the companion's
 * registry row; only the method and the companion-relative path template travel on the wire, and
 * the agent supplies no URL-shaped argument at all. That is what makes SSRF structurally absent
 * here rather than filtered (T-5-07).
 *
 * **Nor does the argument PLACEMENT.** Where a verb's arguments travel is a declared registry
 * property read on the host — `COMPANION_VERBS[...].argsIn` — never a branch on `call.method`
 * (05-13, WR-06b). It used to be the latter: GET and DELETE sent `?path=` while POST and PATCH
 * sent a JSON body with no query string, so three verbs sent their target path somewhere their
 * sibling verbs did not and no reader could see the decision in a diff. Declared, the two
 * readers' parity gate covers WHERE an argument travels as well as which arguments exist, and a
 * verb the host's registry copy does not own is refused before any request is issued rather than
 * executed with a guessed placement.
 *
 * Structured exactly like `observation-producer.ts`: a fully-injectable options interface, a
 * discriminated-union outcome, a Zod schema that validates the envelope BEFORE anything is
 * executed, and snake_case structured log names.
 *
 * **Why the default transport is `fetch` and not the typed `ApiClient` verbs.** The two dispatch
 * endpoints are new in this phase and are therefore absent from
 * `packages/shared/src/api/api.generated.ts`, which `AGENTS.md` forbids hand-editing. `ApiClient`
 * still supplies the base URL and the bearer token, so authentication and origin pinning are
 * unchanged; only the call itself is a plain request, exactly as `manifest-source.ts` already
 * does for the runtime's own manifest poll. When the generated client is next regenerated from
 * the backend's OpenAPI document these can move onto `api.POST`.
 */

import { readFileSync } from "node:fs";
import { z } from "zod";
import { ApiClient } from "../lib/api-client";
import { log, toErrorMessage } from "../serve/log";
import {
	COMPANION_REGISTRY,
	COMPANION_VERBS,
	FILE_BROWSER_COMPANION_KIND,
} from "./companion-registry";
import {
	FILE_BROWSER_SOURCE_NAME,
	fileBrowserAuthSchema,
	postizCompanionSchema,
} from "./manifest-contract";
import { loadCachedSecretValues } from "./manifest-source";
import { getRuntimePaths, type RuntimePaths } from "./paths";
import { runtimeSecretValue } from "./secret-values";

/** The companion kind that authenticates with a static API key rather than a minted assertion. */
const POSTIZ_COMPANION_KIND = "postiz";

/** Every companion listens on loopback only. Never a routable host, and never one from the wire. */
const COMPANION_HOST = "127.0.0.1";

/**
 * How long a minted companion assertion lives.
 *
 * Seconds-scale on purpose: the whole round trip is budgeted at under 30 seconds, so anything
 * longer buys nothing and widens the window in which a leaked assertion is still a credential.
 */
export const COMPANION_ASSERTION_TTL_SECONDS = 60;

/** The upper bound the executor's own tests pin, so a widened TTL fails loudly. */
export const COMPANION_ASSERTION_MAX_TTL_SECONDS = 120;

/** How long to ask the control plane to hold an idle claim open. */
const CLAIM_WAIT_SECONDS = 20;

/** How long a companion request may take before it is abandoned. */
const COMPANION_REQUEST_TIMEOUT_MS = 15_000;

const IDLE_RETRY_INTERVAL_MS = 1_000;

/**
 * How many times one call may be re-attempted against a rate-limited companion.
 *
 * Bounded, and the bound is the point. An unbounded retry loop against a companion that is already
 * saying "too fast" is a client generating exactly the load it is being asked to reduce — and the
 * façade is holding a request open on this call for at most 25 seconds regardless, so attempts past
 * a handful are retries nobody is waiting for.
 */
export const COMPANION_MAX_RATE_LIMIT_ATTEMPTS = 5;

/** The first jitter ceiling. Doubles on each subsequent attempt. */
export const COMPANION_BACKOFF_BASE_MS = 1_000;

/**
 * The ceiling's own ceiling, so the doubling cannot outgrow the request holding this call open.
 * Reached only if `COMPANION_MAX_RATE_LIMIT_ATTEMPTS` is ever raised; stated anyway, because a
 * doubling sequence with no cap is a hang waiting for somebody to change one constant.
 */
export const COMPANION_BACKOFF_CEILING_MS = 30_000;

/**
 * One JSON value in a dispatch envelope's `arguments`.
 *
 * Nested since 06-07: `postiz__create_post` sends a JSON body of posts, and the façade validated
 * that body against the kind's argument contract before minting. A query-placed verb still sends
 * scalars only — `execute` refuses a nested value there rather than stringifying it.
 */
export type CompanionDispatchJson =
	| string
	| number
	| boolean
	| null
	| CompanionDispatchJson[]
	| { [key: string]: CompanionDispatchJson };

const companionDispatchJsonSchema: z.ZodType<CompanionDispatchJson> = z.lazy(() =>
	z.union([
		z.string(),
		z.number(),
		z.boolean(),
		z.null(),
		z.array(companionDispatchJsonSchema),
		z.record(z.string(), companionDispatchJsonSchema),
	]),
);

/**
 * The signed pre-authorized call's SHAPE, validated before anything is executed.
 *
 * SHAPE only, and the distinction matters (WR-04). `decodeDispatchCall` base64url-decodes segment
 * 1 and hands the payload here; this schema checks the fields and their bounds. It does NOT check
 * the signature, the audience or the expiry, and nothing on this host does. What authenticates the
 * instruction is the authenticated TLS claim call that delivered it — the envelope arrived as the
 * body of a response to this runtime's own poll, not from an unauthenticated caller.
 * `verify_dispatch_call` in `backend/app/services/companion_dispatch.py` is the control-plane
 * function that DOES verify signature, audience and expiry.
 *
 * `.strict()` is load-bearing: this object crosses from off-substrate hosting into the tenant's
 * network, so an unexpected field is a wire-contract mismatch to fail on, not a value to ignore.
 * The method is narrowed to the registry's own vocabulary so a malformed envelope cannot turn a
 * read into a delete, and the path is required to be companion-relative so it cannot carry an
 * authority component.
 */
export const companionDispatchEnvelopeSchema = z
	.object({
		companion_key: z.string().min(1).max(31),
		verb: z.string().min(1).max(61),
		method: z.enum(["GET", "POST", "PATCH", "PUT", "DELETE"]),
		path_template: z.string().regex(/^\/(?!\/)/, "path_template must be companion-relative"),
		arguments: z.record(z.string(), companionDispatchJsonSchema),
		jti: z.string().min(1).max(64),
		aud: z.string().min(1),
		exp: z.number().int(),
	})
	.strict();

export type CompanionDispatchCall = z.infer<typeof companionDispatchEnvelopeSchema>;

/**
 * The claim endpoint's response. Separate from the envelope above because it is the CHANNEL's
 * shape, not the call's: an idle poll returns a well-formed response carrying no call at all.
 */
export const companionDispatchClaimSchema = z
	.object({
		call: z.string().min(1).nullable(),
		claim_id: z.string().min(1).nullable(),
		poll_after_seconds: z.number().int().min(0),
	})
	.strict();

export type CompanionExecutorOutcome =
	| { outcome: "idle" }
	| { outcome: "dispatched"; jti: string }
	| { outcome: "executed"; jti: string; status: number }
	// A call the companion rate-limited until the attempts ran out. A SEPARATE variant from
	// `failed` on purpose: "the companion is busy, come back" and "the companion is broken" call
	// for different responses, and collapsing them would hide a recoverable condition inside an
	// unrecoverable-looking one.
	| { outcome: "backed_off"; jti: string; attempts: number; retryAfterSeconds: number | null }
	| { outcome: "failed"; jti?: string; error: string };

interface ClaimedDispatch {
	call: CompanionDispatchCall;
	claimId: string;
}

interface CompanionExecution {
	status: number;
	body: string;
	/**
	 * The raw `Retry-After` header, when the companion sent one. Carried UNPARSED so the retry
	 * policy — not the transport — decides what an unreadable value means; a transport that parsed
	 * it would have to pick a number for "soon", and every number it could pick is wrong.
	 */
	retryAfter?: string | null;
}

interface CompanionAttempts {
	execution: CompanionExecution;
	attempts: number;
	retryAfterSeconds: number | null;
}

export interface CompanionExecutorOptions {
	abort: AbortSignal;
	environmentId?: string;
	paths?: RuntimePaths;
	/** Long-poll the control plane for the next pre-authorized call. */
	claim?: (environmentId: string) => Promise<ClaimedDispatch | null>;
	/**
	 * Report an outcome back to the control plane.
	 *
	 * `retry_after_seconds` is snake_case because it is a WIRE field, matching
	 * `CompanionDispatchResultRequest` in `backend/app/schemas/companions.py`. The control plane's
	 * own token bucket is configured from a guess; the companion's `Retry-After` is authoritative,
	 * so reporting it back is what lets `companion_ratelimit.observe_retry_after` learn the real
	 * limit instead of letting every tenant rediscover it one 429 at a time.
	 */
	report?: (
		jti: string,
		claimId: string,
		outcome: {
			status?: number;
			body?: string;
			error?: string;
			retry_after_seconds?: number;
		},
	) => Promise<void>;
	/** Execute one call against the companion. Injected whole in tests; no network in them. */
	execute?: (call: CompanionDispatchCall) => Promise<CompanionExecution>;
	/**
	 * How to authenticate a call to this kind, from host-resident material only. The test seam for
	 * `readCompanionRequestAuth`; a bare `CompanionAuthMaterial` is still accepted and means the
	 * `jwt-assertion` variant, so the Phase 5 filebrowser tests keep their seam unchanged.
	 */
	readCompanionAuth?: (companionKey: string) => CompanionRequestAuth | CompanionAuthMaterial | null;
	/** Sign one assertion. Injected so tests can decode it without a real HS256 dependency swap. */
	signAssertion?: (auth: CompanionAuthMaterial, nowSeconds: number) => string;
	delay?: (ms: number, abort: AbortSignal) => Promise<void>;
	now?: () => number;
	/**
	 * The source of the backoff jitter, in `[0, 1)`. Injectable beside `delay` and `now` so a test
	 * can assert the EXACT delay sequence: full jitter is only provably full if the draw is
	 * observable, and a randomised delay nobody can pin is a delay nobody can assert bounds on.
	 */
	random?: () => number;
}

export type CompanionAuthMaterial = z.infer<typeof fileBrowserAuthSchema>;

/**
 * How one call authenticates to its companion, by kind (06-07).
 *
 * `jwt-assertion` carries the manifest material a fresh assertion is minted from per call.
 * `static-header` carries a value set verbatim on one header — Postiz's API key, sent as
 * `Authorization: <key>`. The value exists only in this object and in the request it is set on.
 */
export type CompanionRequestAuth =
	| { kind: "jwt-assertion"; material: CompanionAuthMaterial }
	| { kind: "static-header"; header: "Authorization"; value: string };

/**
 * The environment this runtime claims dispatches for, from the process environment the watch unit
 * sets (`IFORGE_ENVIRONMENT_ID`, re-derived to the internal register at the entrypoint boundary).
 * Upstream #1189 retired the instance-data replica this used to read. Null when unset, so the
 * executor idles rather than claiming for a guessed environment.
 */
function readCompanionExecutorEnvironmentId(): string | null {
	const fromEnvironment = process.env.CLAWDI_ENVIRONMENT_ID;
	if (fromEnvironment === undefined) return null;
	const normalized = fromEnvironment.trim();
	if (!normalized || normalized !== fromEnvironment) {
		throw new Error("CLAWDI_ENVIRONMENT_ID must be a non-empty canonical identity");
	}
	return normalized;
}

export class CompanionExecutor {
	private readonly options: CompanionExecutorOptions;
	private readonly paths: RuntimePaths;
	private readonly now: () => number;

	constructor(options: CompanionExecutorOptions) {
		this.options = options;
		this.paths = options.paths ?? getRuntimePaths();
		this.now = options.now ?? Date.now;
	}

	/** One claim-execute-report cycle. Returns what happened; never throws for a call failure. */
	async runOnce(): Promise<CompanionExecutorOutcome> {
		const environmentId = this.options.environmentId ?? readCompanionExecutorEnvironmentId();
		if (!environmentId) return { outcome: "idle" };
		let claimed: ClaimedDispatch | null;
		try {
			claimed = await this.claim(environmentId);
		} catch (error) {
			const message = toErrorMessage(error);
			log.info("daemon.companion_dispatch_claim_failed", { error: message });
			return { outcome: "failed", error: message };
		}
		if (!claimed) return { outcome: "idle" };
		const { call, claimId } = claimed;
		log.info("daemon.companion_dispatch_claimed", {
			jti: call.jti,
			companion_key: call.companion_key,
			verb: call.verb,
			method: call.method,
		});
		try {
			const attempted = await this.executeWithBackoff(call);
			const { execution, attempts, retryAfterSeconds } = attempted;
			const observed = retryAfterSeconds === null ? {} : { retry_after_seconds: retryAfterSeconds };
			if (execution.status === 429) {
				// Attempts exhausted. Reported rather than dropped: the façade is holding a request
				// open on this call, and a silent drop turns a nameable, recoverable condition into
				// a 25-second hang.
				const error = `companion rate limited after ${attempts} attempts`;
				await this.report(call.jti, claimId, { error, ...observed });
				log.info("daemon.companion_dispatch_rate_limited", {
					jti: call.jti,
					companion_key: call.companion_key,
					verb: call.verb,
					attempts,
					retry_after_seconds: retryAfterSeconds,
				});
				return { outcome: "backed_off", jti: call.jti, attempts, retryAfterSeconds };
			}
			await this.report(call.jti, claimId, {
				status: execution.status,
				body: execution.body,
				...observed,
			});
			log.info("daemon.companion_dispatch_executed", {
				jti: call.jti,
				companion_key: call.companion_key,
				verb: call.verb,
				status: execution.status,
			});
			return { outcome: "executed", jti: call.jti, status: execution.status };
		} catch (error) {
			const message = toErrorMessage(error);
			// Reported back rather than swallowed: the façade is holding a request open on this
			// call, and a silent failure turns a nameable error into a 25-second hang.
			try {
				await this.report(call.jti, claimId, { error: message });
			} catch (reportError) {
				log.warn("daemon.companion_dispatch_report_failed", {
					jti: call.jti,
					error: toErrorMessage(reportError),
				});
			}
			log.info("daemon.companion_dispatch_failed", { jti: call.jti, error: message });
			return { outcome: "failed", jti: call.jti, error: message };
		}
	}

	private async claim(environmentId: string): Promise<ClaimedDispatch | null> {
		if (this.options.claim) return this.options.claim(environmentId);
		const api = new ApiClient({ abortSignal: this.options.abort });
		const response = await fetch(`${api.baseUrl}/v1/runtime/companion-dispatch/claim`, {
			method: "POST",
			headers: {
				"content-type": "application/json",
				authorization: `Bearer ${await api.getAccessToken()}`,
			},
			body: JSON.stringify({
				environment_id: environmentId,
				wait_seconds: CLAIM_WAIT_SECONDS,
			}),
			signal: this.options.abort,
		});
		if (!response.ok) {
			throw new Error(`companion dispatch claim failed: HTTP ${response.status}`);
		}
		const claimed = companionDispatchClaimSchema.parse(await response.json());
		if (claimed.call === null || claimed.claim_id === null) return null;
		return { call: decodeDispatchCall(claimed.call), claimId: claimed.claim_id };
	}

	private async report(
		jti: string,
		claimId: string,
		outcome: { status?: number; body?: string; error?: string },
	): Promise<void> {
		if (this.options.report) return this.options.report(jti, claimId, outcome);
		const api = new ApiClient({ abortSignal: this.options.abort });
		const response = await fetch(`${api.baseUrl}/v1/runtime/companion-dispatch/result`, {
			method: "POST",
			headers: {
				"content-type": "application/json",
				authorization: `Bearer ${await api.getAccessToken()}`,
			},
			body: JSON.stringify({ call_jti: jti, claim_id: claimId, ...outcome }),
			signal: this.options.abort,
		});
		if (!response.ok) {
			throw new Error(`companion dispatch result failed: HTTP ${response.status}`);
		}
	}

	/**
	 * Execute one call, re-attempting only while the companion answers 429.
	 *
	 * Two policies, and which one applies is decided by the companion rather than by this client.
	 * When a `Retry-After` is present it is honoured EXACTLY -- no jitter on top and no backoff of
	 * our own, because the server has given an authoritative answer and second-guessing it is how
	 * one rate limit becomes two. When it is absent the delay is FULL jitter: a value drawn
	 * uniformly from zero up to a ceiling that doubles per attempt. The randomness is the
	 * load-bearing part -- with a fixed schedule, every client that hit the limit at the same
	 * moment retries at the same moment, and the retries themselves become the burst.
	 *
	 * An unreadable `Retry-After` counts as ABSENT, never as zero. Zero would mean "retry
	 * immediately", which is the one thing a 429 asked this client not to do.
	 */
	private async executeWithBackoff(call: CompanionDispatchCall): Promise<CompanionAttempts> {
		let ceiling = COMPANION_BACKOFF_BASE_MS;
		let observed: number | null = null;
		let attempts = 1;
		let execution = await this.execute(call);
		while (execution.status === 429 && attempts < COMPANION_MAX_RATE_LIMIT_ATTEMPTS) {
			const authoritative = parseRetryAfterSeconds(execution.retryAfter);
			if (authoritative !== null) observed = authoritative;
			let delayMs: number;
			if (authoritative === null) {
				delayMs = this.jitter(ceiling);
				ceiling = Math.min(ceiling * 2, COMPANION_BACKOFF_CEILING_MS);
			} else {
				delayMs = authoritative * 1000;
			}
			// Logged under its own snake_case event, with the attempt and the delay, so a slowed
			// call reads as slowed in the runtime log rather than as silence.
			log.info("daemon.companion_dispatch_backed_off", {
				jti: call.jti,
				companion_key: call.companion_key,
				verb: call.verb,
				attempt: attempts,
				delay_ms: delayMs,
				retry_after_seconds: authoritative,
			});
			await this.pause(delayMs);
			execution = await this.execute(call);
			attempts += 1;
		}
		const final = parseRetryAfterSeconds(execution.retryAfter);
		if (final !== null) observed = final;
		return { execution, attempts, retryAfterSeconds: observed };
	}

	/** One delay drawn uniformly from `[0, ceiling]`, inclusive of both ends. */
	private jitter(ceiling: number): number {
		const draw = this.options.random ? this.options.random() : Math.random();
		return Math.min(ceiling, Math.floor(draw * (ceiling + 1)));
	}

	private pause(ms: number): Promise<void> {
		const delay = this.options.delay ?? abortableDelay;
		return delay(ms, this.options.abort);
	}

	private async execute(call: CompanionDispatchCall): Promise<CompanionExecution> {
		if (this.options.execute) return this.options.execute(call);
		// Read first, so a kind whose credential is missing fails before any request is built. A
		// missing postiz key throws from here with the REF it looked for, never a value.
		const auth = this.readRequestAuth(call.companion_key);
		if (!auth) {
			throw new Error(`no ${call.companion_key} companion auth material on this host`);
		}
		const entry = Object.hasOwn(COMPANION_REGISTRY, call.companion_key)
			? COMPANION_REGISTRY[call.companion_key]
			: undefined;
		if (!entry) throw new Error(`unknown companion kind: ${call.companion_key}`);
		// The verb's own row, by the assembled tool name. An envelope naming a verb this host's
		// registry copy does not own is refused HERE, before any request is issued — executing it
		// would mean guessing where its arguments travel, which is the guess this column removed.
		const toolName = `${call.companion_key}__${call.verb}`;
		const verbEntry = Object.hasOwn(COMPANION_VERBS, toolName)
			? COMPANION_VERBS[toolName]
			: undefined;
		if (!verbEntry) throw new Error(`unknown companion verb: ${toolName}`);
		// Host and port from the REGISTRY, method and path from the signed envelope. Nothing
		// addressable comes from an agent-supplied argument.
		const url = new URL(`http://${COMPANION_HOST}:${entry.port}${call.path_template}`);
		// Placement is DECLARED, not derived from the method (05-13, WR-06b).
		const body = verbEntry.argsIn === "body" ? JSON.stringify(call.arguments) : undefined;
		if (body === undefined) {
			for (const [key, value] of Object.entries(call.arguments)) {
				// A query string carries scalars. A nested value here is a malformed envelope, not
				// something to flatten into "[object Object]" and send.
				if (value === null || typeof value === "object") {
					throw new Error(`${toolName} argument ${key} is not a scalar query value`);
				}
				url.searchParams.set(key, String(value));
			}
		}
		// Fixed query parameters, applied AFTER the agent's arguments and therefore
		// not overridable by them. The ordering is the security property: these
		// name WHICH store the call addresses, and an agent that could set
		// `source` itself would be choosing its own target — the same class of
		// thing as choosing the host, which the line above already refuses.
		//
		// `filebrowser` needs `source` on every `/api/resources` verb: the endpoint
		// requires it alongside `path` and answers 500 "source not provided"
		// otherwise. It is not an agent argument and has no verb-row column,
		// because it does not vary by verb — it is the single source the deployed
		// companion config declares, and both read it from the same constant so
		// the request cannot address a store the deployment did not create.
		for (const [key, value] of Object.entries(companionFixedQuery(call.companion_key))) {
			url.searchParams.set(key, value);
		}
		const credential = this.credentialHeader(auth);
		const controller = new AbortController();
		const timer = setTimeout(() => controller.abort(), COMPANION_REQUEST_TIMEOUT_MS);
		try {
			const response = await fetch(url, {
				method: call.method,
				headers: {
					...credential,
					...(body === undefined ? {} : { "content-type": "application/json" }),
				},
				body,
				signal: controller.signal,
			});
			return {
				status: response.status,
				body: await response.text(),
				retryAfter: response.headers.get("retry-after"),
			};
		} finally {
			// Discarded with the request. The assertion exists for exactly one call, and the static
			// key is referenced by nothing once the request is built.
			clearTimeout(timer);
		}
	}

	private readRequestAuth(companionKey: string): CompanionRequestAuth | null {
		if (this.options.readCompanionAuth) {
			const seamed = this.options.readCompanionAuth(companionKey);
			if (seamed === null) return null;
			return "kind" in seamed ? seamed : { kind: "jwt-assertion", material: seamed };
		}
		return readCompanionRequestAuth(this.paths, companionKey);
	}

	/** The one credential header this call carries, by the kind's declared way. */
	private credentialHeader(auth: CompanionRequestAuth): Record<string, string> {
		if (auth.kind === "static-header") return { [auth.header]: auth.value };
		return { [auth.material.header]: this.signAssertion(auth.material) };
	}

	private signAssertion(auth: CompanionAuthMaterial): string {
		const nowSeconds = Math.floor(this.now() / 1000);
		if (this.options.signAssertion) return this.options.signAssertion(auth, nowSeconds);
		return signCompanionAssertion(auth, nowSeconds);
	}
}

/**
 * Mint one companion assertion: `sub` is the manifest's subject, `groups` is the single-element
 * required-group array the companion's JWT method matches on, `aud` is the manifest's audience,
 * and `exp` is seconds-scale.
 *
 * HS256 by hand rather than through a JWT dependency: the CLI ships as a single native binary and
 * this is three base64url segments and one HMAC. `auth.secret` is read here and nowhere else.
 */
export function signCompanionAssertion(
	auth: CompanionAuthMaterial,
	nowSeconds: number,
	ttlSeconds: number = COMPANION_ASSERTION_TTL_SECONDS,
): string {
	const header = base64Url(JSON.stringify({ alg: auth.algorithm, typ: "JWT" }));
	const payload = base64Url(
		JSON.stringify({
			[auth.userIdentifier]: auth.subject,
			[auth.groupsClaim]: [auth.requiredGroup],
			aud: auth.audience,
			iat: nowSeconds,
			exp: nowSeconds + ttlSeconds,
		}),
	);
	const signingInput = `${header}.${payload}`;
	const hasher = new Bun.CryptoHasher("sha256", auth.secret);
	hasher.update(signingInput);
	return `${signingInput}.${base64UrlFromBytes(hasher.digest())}`;
}

/**
 * Read a `Retry-After` header as whole delta-seconds, or `null` when it does not say one.
 *
 * `null` means ABSENT, and absent routes to the jittered path. That distinction is the whole
 * function: an unreadable value coerced to zero would mean "retry immediately", which is precisely
 * what a 429 asked this client not to do, and it would do it on every attempt.
 *
 * The HTTP-date form is deliberately unsupported rather than half-supported. Honouring it correctly
 * needs a trustworthy clock on both ends, and a companion on tenant loopback is exactly where that
 * assumption does not hold; falling back to jitter for it is the conservative, correct answer
 * rather than a gap.
 */
export function parseRetryAfterSeconds(value: string | null | undefined): number | null {
	if (value === null || value === undefined) return null;
	const trimmed = value.trim();
	if (!/^\d+$/.test(trimmed)) return null;
	const seconds = Number.parseInt(trimmed, 10);
	return Number.isFinite(seconds) ? seconds : null;
}

/** Decode a dispatch envelope's payload and validate it. Signature verification is the control plane's. */
/**
 * Query parameters a companion kind requires that no verb row declares.
 *
 * These do not vary by verb and are not agent arguments: they name WHICH store
 * inside the companion a call addresses. `filebrowser` needs `source` on every
 * `/api/resources` verb — the endpoint requires it beside `path` and answers
 * 500 "source not provided" otherwise — and the value is the single source the
 * deployed config declares, read from the same constant so the request cannot
 * address a store the deployment did not create.
 *
 * A kind with nothing fixed returns an empty object, which is the normal case.
 */
export function companionFixedQuery(companionKey: string): Record<string, string> {
	if (companionKey === FILE_BROWSER_COMPANION_KIND) {
		return { source: FILE_BROWSER_SOURCE_NAME };
	}
	return {};
}

export function decodeDispatchCall(token: string): CompanionDispatchCall {
	const segments = token.split(".");
	if (segments.length !== 3) throw new Error("companion dispatch call is not a JWT");
	const payload: unknown = JSON.parse(
		Buffer.from(segments[1] as string, "base64url").toString("utf-8"),
	);
	return companionDispatchEnvelopeSchema.parse(payload);
}

/**
 * Read `companions.<kind>.auth` out of the last-good manifest on disk.
 *
 * Validated with the MANIFEST's own auth schema rather than a second copy of those literals, so
 * the executor and the manifest parser cannot disagree about what it is signing with.
 */
export function readCompanionAuthFromManifest(
	paths: RuntimePaths,
	companionKey: string,
): CompanionAuthMaterial | null {
	const entry = readManifestCompanionEntry(paths, companionKey) as { auth?: unknown } | undefined;
	const parsed = fileBrowserAuthSchema.safeParse(entry?.auth);
	return parsed.success ? parsed.data : null;
}

/**
 * How a call to `companionKey` authenticates on this host, or `null` when the last-good manifest
 * declares no usable entry for it.
 *
 * `postiz` resolves its entry's `credentials.apiKeySecretRef` against the root-only cached secret
 * values (research Pitfall 11). The key itself travels in the manifest bundle's `secretValues`,
 * which is Phase 4 obligation #4's accepted residual and stays open here. A declared entry whose
 * key is not cached THROWS, naming the ref: the call must fail before any request, and the
 * reported reason must say which reference is missing without carrying any value.
 *
 * Every other kind resolves to the `jwt-assertion` variant from `companions.<kind>.auth`, exactly
 * as `readCompanionAuthFromManifest` always has.
 */
export function readCompanionRequestAuth(
	paths: RuntimePaths,
	companionKey: string,
): CompanionRequestAuth | null {
	if (companionKey === POSTIZ_COMPANION_KIND) {
		const parsed = postizCompanionSchema.safeParse(readManifestCompanionEntry(paths, companionKey));
		if (!parsed.success) return null;
		const ref = parsed.data.credentials.apiKeySecretRef;
		const cached = loadCachedSecretValues(paths);
		if (!("secretValues" in cached)) {
			throw new Error(`the cached runtime secret values are unreadable, so ${ref} is unavailable`);
		}
		const value = runtimeSecretValue(cached.secretValues, ref);
		if (value === null) {
			throw new Error(`no cached value for the postiz API key ${ref} on this host`);
		}
		return { kind: "static-header", header: "Authorization", value };
	}
	const material = readCompanionAuthFromManifest(paths, companionKey);
	return material === null ? null : { kind: "jwt-assertion", material };
}

/** `companions.<kind>` from the last-good manifest on disk, or `undefined`. */
function readManifestCompanionEntry(paths: RuntimePaths, companionKey: string): unknown {
	let raw: unknown;
	try {
		raw = JSON.parse(readFileSync(paths.manifestLastGood, "utf-8"));
	} catch {
		return undefined;
	}
	const manifest = raw as { manifest?: { companions?: Record<string, unknown> } } & {
		companions?: Record<string, unknown>;
	};
	const companions = manifest.manifest?.companions ?? manifest.companions;
	if (!companions || !Object.hasOwn(companions, companionKey)) return undefined;
	return companions[companionKey];
}

export async function runCompanionExecutor(options: CompanionExecutorOptions): Promise<void> {
	const paths = options.paths ?? getRuntimePaths();
	if (paths.mode !== "hosted") return;
	const executor = new CompanionExecutor({ ...options, paths });
	const delay = options.delay ?? abortableDelay;
	while (!options.abort.aborted) {
		const result = await executor.runOnce();
		if (result.outcome === "idle" || result.outcome === "failed") {
			await delay(IDLE_RETRY_INTERVAL_MS, options.abort);
		}
	}
}

function base64Url(value: string): string {
	return Buffer.from(value, "utf-8").toString("base64url");
}

function base64UrlFromBytes(value: ArrayBuffer | Uint8Array): string {
	return Buffer.from(value as Uint8Array).toString("base64url");
}

function abortableDelay(ms: number, abort: AbortSignal): Promise<void> {
	return new Promise((resolve) => {
		const onAbort = () => {
			clearTimeout(timer);
			resolve();
		};
		const timer = setTimeout(() => {
			abort.removeEventListener("abort", onAbort);
			resolve();
		}, ms);
		abort.addEventListener("abort", onAbort, { once: true });
	});
}
