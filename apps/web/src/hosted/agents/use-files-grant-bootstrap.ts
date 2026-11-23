import { isRuntimeUiCredentials } from "@clawdi/shared/api";
import type { ClawdiDesktopShellBridge } from "@clawdi/shared/desktop";
import { useCallback, useEffect, useRef, useState } from "react";
import { assertRuntimeTokenUrl } from "@/hosted/agents/runtime-token-hosts";
import { openSecureRuntimeWindow } from "@/hosted/agents/runtime-ui-credentials";
import { trackRuntimeWindow } from "@/hosted/agents/runtime-window-lifecycle";
import { type BillingClient, useBillingClient } from "@/hosted/billing/billing-client";
import { useDesktopBridge } from "@/lib/desktop";
import { toastError } from "@/lib/toast";

export type FilesGrantBootstrapState = "pending" | "ready" | "error";

/**
 * The Files origin is a distinct subdomain that never receives the Clerk
 * `__session` cookie. We prime a deployment-scoped `__Host-clawdi_files_grant`
 * HttpOnly cookie by sending a freshly issued, single-use runtime-UI grant in
 * the `Authorization` header (never in the URL/iframe src/DOM) to the Files
 * origin, then render the iframe only after that cookie is set. The Clerk
 * session token never reaches the Files origin. See the ForwardAuth bootstrap
 * contract and UI-SPEC G2.
 */
async function primeFilesGrant(url: string, token: string): Promise<void> {
	assertRuntimeTokenUrl(url);
	const response = await fetch(url, {
		method: "GET",
		headers: { Authorization: `Bearer ${token}` },
		credentials: "include",
		cache: "no-store",
	});
	if (!response.ok) {
		throw new Error(`Files bootstrap rejected with status ${response.status}`);
	}
}

export type FilesGrantRequest = {
	url: string;
	deploymentId: string;
	/** The deployment's current `metadata.resourceVersion`. */
	resourceVersion: string;
};

export type FilesGrantDependencies = {
	client: Pick<BillingClient, "getRuntimeUiCredentials">;
};

/**
 * One Files presentation: issue a fresh grant and prime the Files origin with
 * it. Every presentation (mount, refresh, click, retry) consumes its own grant;
 * neither surface reuses the console's credentials.
 */
export async function primeFilesForPresentation(
	{ url, deploymentId, resourceVersion }: FilesGrantRequest,
	{ client }: FilesGrantDependencies,
): Promise<void> {
	const credentials = await client.getRuntimeUiCredentials(deploymentId, resourceVersion);
	if (
		!isRuntimeUiCredentials(credentials) ||
		credentials.deployment_resource_version !== resourceVersion
	) {
		throw new Error("Invalid runtime access response.");
	}
	await primeFilesGrant(url, credentials.token);
}

export function useFilesGrantBootstrap(
	url: string,
	deploymentId: string,
	resourceVersion: string,
): FilesGrantBootstrapState {
	const client = useBillingClient();
	const [state, setState] = useState<FilesGrantBootstrapState>("pending");
	// Read at bootstrap time: a resource version bump alone must not remount the
	// iframe; the primed cookie carries the mounted session.
	const currentResourceVersion = useRef(resourceVersion);
	currentResourceVersion.current = resourceVersion;

	useEffect(() => {
		let cancelled = false;
		setState("pending");
		void (async () => {
			try {
				await primeFilesForPresentation(
					{ url, deploymentId, resourceVersion: currentResourceVersion.current },
					{ client },
				);
				if (!cancelled) setState("ready");
			} catch {
				if (!cancelled) setState("error");
			}
		})();
		return () => {
			cancelled = true;
		};
	}, [url, deploymentId, client]);

	return state;
}

export type OpenFilesWindowDependencies = FilesGrantDependencies & {
	desktopBridge: ClawdiDesktopShellBridge | null | undefined;
	openWindow: Parameters<typeof openSecureRuntimeWindow>[0];
	trackWindow: typeof trackRuntimeWindow;
	toastError: typeof toastError;
};

/**
 * Browsers reserve a blank window synchronously before the async grant request.
 * Desktop asks the trusted shell to create a direct HTTPS child only after the
 * grant succeeds, so the child never inherits the Dashboard preload.
 */
export async function openFilesInNewWindow(
	request: FilesGrantRequest,
	deps: OpenFilesWindowDependencies,
): Promise<void> {
	const { url, deploymentId } = request;
	const { desktopBridge } = deps;
	const popup = desktopBridge ? null : openSecureRuntimeWindow(deps.openWindow);
	if (!desktopBridge && !popup) {
		deps.toastError("Couldn't open Files", {
			id: "files-window-launch",
			description: "Allow pop-ups for iforge, then try again.",
		});
		return;
	}
	if (popup) deps.trackWindow(deploymentId, popup);
	try {
		await primeFilesForPresentation(request, deps);
		if (desktopBridge) {
			if (!(await desktopBridge.openFilesWindow(url))) throw new Error("Desktop child was denied");
		} else {
			popup?.location.replace(url);
		}
	} catch {
		try {
			popup?.close();
		} catch {
			// Browser isolation may have severed the WindowProxy.
		}
		deps.toastError("Couldn't open Files", {
			id: "files-window-launch",
			description: "Files access couldn't be authenticated. Refresh the page and try again.",
		});
	}
}

export function useOpenFilesInNewWindow(
	url: string,
	deploymentId: string,
	resourceVersion: string,
): () => Promise<void> {
	const client = useBillingClient();
	const desktopBridge = useDesktopBridge();
	return useCallback(
		() =>
			openFilesInNewWindow(
				{ url, deploymentId, resourceVersion },
				{
					client,
					desktopBridge,
					openWindow: window.open.bind(window),
					trackWindow: trackRuntimeWindow,
					toastError,
				},
			),
		[deploymentId, desktopBridge, url, resourceVersion, client],
	);
}
