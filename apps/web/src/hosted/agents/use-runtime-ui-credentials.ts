import type { RuntimeUiCredentials } from "@clawdi/shared/api";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
	HERMES_OIDC_BROWSER_SESSION_REFRESH_MS,
	hermesOidcAuthorityIdentity,
	primeHermesOidcBrowserSession,
} from "@/hosted/agents/hermes-oidc-browser-session";
import { primeOpenClawBrowserSession } from "@/hosted/agents/openclaw-browser-session";
import {
	forgetOpenClawNativeHandoffLoaded,
	hasOpenClawNativeHandoffLoaded,
	markOpenClawNativeHandoffLoaded,
	resolveRuntimeUiCredentials,
	runtimeUiLocalStorage,
} from "@/hosted/agents/runtime-ui-credentials";
import {
	BILLING_API_ORIGIN,
	type BillingClient,
	useBillingClient,
} from "@/hosted/billing/billing-client";
import type { HostedDeployment } from "@/hosted/billing/contracts";
import { useAuthToken, useSessionIdentity } from "@/lib/auth-client";

const HERMES_OIDC_BROWSER_SESSION_RETRY_MS = 60_000;

export type RuntimeUiAccessRequest = {
	id: string;
	resourceVersion: string;
	generation: number;
	runtime: HostedDeployment["resource"]["spec"]["runtime"];
	endpoint: string;
	storageScope: string;
	openClawBrowserSessionUrl: string | null | undefined;
	hermesOidcBrowserSessionUrl: string | null | undefined;
	trustedApiOrigin: string;
	fresh: boolean;
	signal: AbortSignal;
	/** False once the hook has moved on (unmount, clear, or a newer attempt). */
	current: () => boolean;
};

export type RuntimeUiAccessDependencies = {
	client: Pick<BillingClient, "getRuntimeUiCredentials">;
	getToken: () => Promise<string>;
	storage: Storage | null;
};

export type RuntimeUiAccessOutcome =
	| { kind: "superseded" }
	| { kind: "hermes_oidc_primed" }
	| { kind: "native_handoff_loaded" }
	| { kind: "credentials"; credentials: RuntimeUiCredentials };

const SUPERSEDED: RuntimeUiAccessOutcome = { kind: "superseded" };

async function issueRuntimeUiCredentials(
	{ id, resourceVersion, endpoint, runtime, current }: RuntimeUiAccessRequest,
	client: RuntimeUiAccessDependencies["client"],
): Promise<RuntimeUiCredentials | null> {
	const result = await client.getRuntimeUiCredentials(id, resourceVersion);
	if (!current()) return null;
	const resolved = resolveRuntimeUiCredentials(result, endpoint, resourceVersion);
	if (!resolved || resolved.runtime !== runtime)
		throw new Error("Invalid runtime access response.");
	return resolved;
}

/**
 * One console access attempt: browser-session priming and credential issue.
 * The hook owns attempt bookkeeping and React state; this function owns the
 * request order, so that order is testable without a DOM renderer.
 */
export async function loadRuntimeUiAccess(
	request: RuntimeUiAccessRequest,
	{ client, getToken, storage }: RuntimeUiAccessDependencies,
): Promise<RuntimeUiAccessOutcome> {
	const { id, resourceVersion, endpoint, signal, current } = request;
	if (request.hermesOidcBrowserSessionUrl) {
		const token = await getToken();
		if (!current()) return SUPERSEDED;
		await primeHermesOidcBrowserSession(
			request.hermesOidcBrowserSessionUrl,
			id,
			request.trustedApiOrigin,
			token,
			resourceVersion,
			signal,
		);
		if (!current()) return SUPERSEDED;
		return { kind: "hermes_oidc_primed" };
	}
	// Every attempt (first load, load(true)/reconnect, automatic re-run) issues a
	// fresh single-use grant before anything reaches the runtime origin, and the
	// browser session is primed with that grant. The Clerk session token never
	// crosses to the runtime origin (UI-SPEC G1).
	let issued: RuntimeUiCredentials | null = null;
	if (request.openClawBrowserSessionUrl) {
		issued = await issueRuntimeUiCredentials(request, client);
		if (!issued) return SUPERSEDED;
		await primeOpenClawBrowserSession(
			request.openClawBrowserSessionUrl,
			endpoint,
			issued.token,
			resourceVersion,
			signal,
		);
		if (!current()) return SUPERSEDED;
	}
	if (
		!request.fresh &&
		request.runtime === "openclaw" &&
		hasOpenClawNativeHandoffLoaded(storage, request.storageScope, endpoint, request.generation)
	) {
		return { kind: "native_handoff_loaded" };
	}
	const resolved = issued ?? (await issueRuntimeUiCredentials(request, client));
	if (!resolved) return SUPERSEDED;
	return { kind: "credentials", credentials: resolved };
}

/** Credentials and OIDC priming belong to the mounted console, never to a cross-route cache. */
export function useRuntimeUiCredentials(deployment: HostedDeployment, endpoint: string | null) {
	const client = useBillingClient();
	const { id, metadata, spec } = deployment.resource;
	const identity = useSessionIdentity();
	const { getToken } = useAuthToken();
	const runtimeEndpoint = deployment.runtime_ui_endpoint;
	const openClawBrowserSessionUrl =
		runtimeEndpoint?.runtime === "openclaw" ? runtimeEndpoint.browser_session_url : null;
	const hermesOidcBrowserSessionUrl =
		runtimeEndpoint?.runtime === "hermes" && runtimeEndpoint.auth_mode === "oidc"
			? runtimeEndpoint.browser_session_url
			: null;
	const hermesOidcAccessRevision =
		runtimeEndpoint?.runtime === "hermes" && runtimeEndpoint.auth_mode === "oidc"
			? runtimeEndpoint.access_revision
			: null;
	const isHermesOidc = hermesOidcBrowserSessionUrl != null && hermesOidcAccessRevision != null;
	const storageScope = JSON.stringify([identity, id]);
	const authorityIdentity = isHermesOidc
		? hermesOidcAuthorityIdentity(
				identity,
				id,
				endpoint ?? "",
				hermesOidcBrowserSessionUrl,
				hermesOidcAccessRevision,
			)
		: JSON.stringify([identity, id, metadata.resourceVersion, endpoint]);
	const [nativeHandoffLoaded, setNativeHandoffLoaded] = useState(false);
	const [hermesOidcPrimedAuthority, setHermesOidcPrimedAuthority] = useState<string | null>(null);
	const hermesOidcPrimed = isHermesOidc && hermesOidcPrimedAuthority === authorityIdentity;
	const [credentials, setCredentials] = useState<RuntimeUiCredentials | null>(null);
	const [error, setError] = useState<Error | null>(null);
	const [isLoading, setIsLoading] = useState(false);
	const [attempt, setAttempt] = useState(0);
	const pending = useRef<Promise<RuntimeUiCredentials | null> | null>(null);
	const active = useRef(false);
	const revision = useRef(0);
	const requestedVersion = useRef<string | null>(null);
	const requestAbort = useRef<AbortController | null>(null);
	const mountedAuthorityIdentity = useRef(authorityIdentity);
	const currentResourceVersion = useRef(metadata.resourceVersion);
	const currentGetToken = useRef(getToken);
	currentResourceVersion.current = metadata.resourceVersion;
	currentGetToken.current = getToken;

	useLayoutEffect(() => {
		active.current = true;
		return () => {
			active.current = false;
			requestAbort.current?.abort();
		};
	}, []);

	const clear = useCallback(() => {
		requestAbort.current?.abort();
		forgetOpenClawNativeHandoffLoaded(runtimeUiLocalStorage(), storageScope);
		setNativeHandoffLoaded(false);
		setHermesOidcPrimedAuthority(null);
		revision.current += 1;
		pending.current = null;
		requestedVersion.current = null;
		setCredentials(null);
		setError(null);
		setIsLoading(false);
	}, [storageScope]);

	useEffect(() => {
		if (mountedAuthorityIdentity.current === authorityIdentity) return;
		mountedAuthorityIdentity.current = authorityIdentity;
		clear();
	}, [authorityIdentity, clear]);

	const load = useCallback(
		(fresh = false): Promise<RuntimeUiCredentials | null> => {
			if (!active.current || !endpoint) return Promise.resolve(null);
			if (pending.current) return pending.current;
			if (isHermesOidc && !fresh && hermesOidcPrimed) return Promise.resolve(null);
			if (!fresh && credentials) return Promise.resolve(credentials);
			if (fresh) {
				forgetOpenClawNativeHandoffLoaded(runtimeUiLocalStorage(), storageScope);
				setNativeHandoffLoaded(false);
				setHermesOidcPrimedAuthority(null);
			}
			requestedVersion.current = metadata.resourceVersion;
			const requestRevision = ++revision.current;
			setAttempt(requestRevision);
			setCredentials(null);
			setError(null);
			setIsLoading(true);
			const current = () => active.current && revision.current === requestRevision;
			pending.current = Promise.resolve()
				.then(async () => {
					if (!current()) return null;
					const abort = new AbortController();
					requestAbort.current?.abort();
					requestAbort.current = abort;
					const outcome = await loadRuntimeUiAccess(
						{
							id,
							resourceVersion: metadata.resourceVersion,
							generation: metadata.generation,
							runtime: spec.runtime,
							endpoint,
							storageScope,
							openClawBrowserSessionUrl,
							hermesOidcBrowserSessionUrl,
							trustedApiOrigin: BILLING_API_ORIGIN,
							fresh,
							signal: abort.signal,
							current,
						},
						{ client, getToken, storage: runtimeUiLocalStorage() },
					);
					if (!current()) return null;
					switch (outcome.kind) {
						case "superseded":
							return null;
						case "hermes_oidc_primed":
							setHermesOidcPrimedAuthority(authorityIdentity);
							return null;
						case "native_handoff_loaded":
							setNativeHandoffLoaded(true);
							return null;
						case "credentials":
							setCredentials(outcome.credentials);
							return outcome.credentials;
					}
				})
				.catch(() => {
					if (current()) {
						setHermesOidcPrimedAuthority(null);
						setError(
							new Error(
								isHermesOidc
									? "iforge couldn't establish this browser session."
									: "iforge couldn't load the browser sign-in details.",
							),
						);
					}
					return null;
				})
				.finally(() => {
					if (current()) {
						pending.current = null;
						setIsLoading(false);
					}
				});
			return pending.current;
		},
		[
			client,
			id,
			metadata.resourceVersion,
			metadata.generation,
			spec.runtime,
			endpoint,
			credentials,
			storageScope,
			openClawBrowserSessionUrl,
			hermesOidcBrowserSessionUrl,
			hermesOidcPrimed,
			isHermesOidc,
			authorityIdentity,
			getToken,
		],
	);

	useEffect(() => {
		if (!isHermesOidc || !hermesOidcPrimed || !hermesOidcBrowserSessionUrl) return;
		let cancelled = false;
		let timer: ReturnType<typeof setTimeout> | null = null;
		let abort: AbortController | null = null;

		const schedule = (delay: number) => {
			timer = setTimeout(() => void refresh(), delay);
		};
		const refresh = async () => {
			let nextDelay = HERMES_OIDC_BROWSER_SESSION_REFRESH_MS;
			abort = new AbortController();
			try {
				const token = await currentGetToken.current();
				if (cancelled || mountedAuthorityIdentity.current !== authorityIdentity) return;
				await primeHermesOidcBrowserSession(
					hermesOidcBrowserSessionUrl,
					id,
					BILLING_API_ORIGIN,
					token,
					currentResourceVersion.current,
					abort.signal,
				);
			} catch {
				nextDelay = HERMES_OIDC_BROWSER_SESSION_RETRY_MS;
			} finally {
				abort = null;
				if (!cancelled && mountedAuthorityIdentity.current === authorityIdentity) {
					schedule(nextDelay);
				}
			}
		};

		schedule(HERMES_OIDC_BROWSER_SESSION_REFRESH_MS);
		return () => {
			cancelled = true;
			if (timer) clearTimeout(timer);
			abort?.abort();
		};
	}, [isHermesOidc, hermesOidcPrimed, hermesOidcBrowserSessionUrl, authorityIdentity, id]);

	useEffect(() => {
		const shouldPrimeOpenClaw = spec.runtime === "openclaw" && !nativeHandoffLoaded;
		const shouldPrimeHermes = isHermesOidc && !hermesOidcPrimed;
		if (
			endpoint &&
			(shouldPrimeOpenClaw || shouldPrimeHermes) &&
			!credentials &&
			!isLoading &&
			requestedVersion.current !== metadata.resourceVersion
		) {
			void load();
		}
	}, [
		spec.runtime,
		endpoint,
		metadata.resourceVersion,
		metadata.generation,
		credentials,
		isLoading,
		load,
		nativeHandoffLoaded,
		hermesOidcPrimed,
		isHermesOidc,
	]);

	return {
		credentials,
		error,
		isLoading,
		attempt,
		nativeHandoffLoaded,
		hermesOidcPrimed,
		markFrameLoaded: () => {
			if (
				active.current &&
				revision.current === attempt &&
				endpoint &&
				markOpenClawNativeHandoffLoaded(
					runtimeUiLocalStorage(),
					storageScope,
					endpoint,
					credentials,
					metadata.generation,
				)
			)
				setNativeHandoffLoaded(true);
		},
		load: () => load(),
		clear,
		reconnect: () => load(true),
	};
}
