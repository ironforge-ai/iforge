"use client";

import { ApiError, ApiNetworkError } from "@/lib/api-errors";
import { env } from "@/lib/env";

/**
 * Hand-rolled typed client for the operator admin endpoints.
 *
 * The admin router ships with `include_in_schema=False`, so `bun run
 * generate-api` never emits typed bindings for it — this module is the
 * deliberate, hand-maintained exception (see the rationale comment in
 * `backend/app/routes/admin.py`). Every call carries the operator-entered
 * `X-Admin-Key` header; the key lives in sessionStorage ONLY — never
 * durable web storage, never appended to URLs, never logged or sent to
 * analytics (T-03-28).
 */

const ADMIN_KEY_STORAGE_KEY = "clawdi-admin-key";
const API_URL = env.VITE_IFORGE_API_URL;
const REQUEST_TIMEOUT_MS = 20_000;

/** Read the operator key for this browser session, or null when unset. */
export function getAdminKey(): string | null {
	if (typeof window === "undefined") return null;
	return window.sessionStorage.getItem(ADMIN_KEY_STORAGE_KEY);
}

/** Store the operator key for this browser session only. */
export function setAdminKey(key: string): void {
	if (typeof window === "undefined") return;
	window.sessionStorage.setItem(ADMIN_KEY_STORAGE_KEY, key);
}

/** Drop the session-scoped operator key (rejected key / explicit reset). */
export function clearAdminKey(): void {
	if (typeof window === "undefined") return;
	window.sessionStorage.removeItem(ADMIN_KEY_STORAGE_KEY);
}

/** 401/403 from an admin endpoint — the stored key is absent or wrong.
 * Surfaces the needs-key state so the page can re-prompt the operator. */
export function isAdminKeyError(error: unknown): boolean {
	return error instanceof ApiError && (error.status === 401 || error.status === 403);
}

// ---------------------------------------------------------------------------
// Response contracts — hand-mirrored from backend/app/schemas/admin_export.py
// and admin_tenant_lifecycle.py. Coordinated changes only.
//
// The admin router is `include_in_schema=False`, so NOTHING generated checks
// that these shapes still match the server: a renamed field fails silently at
// runtime as `undefined` (T-03-75). The endpoints mirrored below:
//   GET  /v1/admin/users?q=…                             (03-16, W-05: id | clerk id | email;
//                                                         03-20, G-03-62: clerk_id emitted)
//   POST /v1/admin/tenants/{id}/exports, GET …/exports
//   GET  /v1/admin/tenants/{id}/exports/{job}/download   (03-18, W-03: presign on demand)
//   POST /v1/admin/tenants/{id}/delete, POST …/purge/cancel, GET …/lifecycle
//   PUT  /v1/admin/auth/billing-exemptions               (06.3 WR-02)
//
// This comment is NO LONGER the only safeguard: `admin-contract.test.ts` reads
// the two Pydantic modules and this file and asserts field-name-set equality
// per interface, so a rename on either side now fails a test rather than
// becoming `undefined` at runtime.
// ---------------------------------------------------------------------------

export type TenantExportStatus = "queued" | "running" | "complete" | "failed";

export interface AdminTenantExportCreatedResponse {
	job_id: string;
	status: TenantExportStatus;
}

export interface AdminTenantExportListItem {
	job_id: string;
	status: TenantExportStatus;
	created_at: string;
	completed_at: string | null;
	include_vault_values: boolean;
	/** Always null since 03-18 — the list mints nothing. Typed `null` (mirroring
	 * the Pydantic `download_url: None`) so any read of it is visibly dead code;
	 * the field is still emitted so the mirrored shape did not lose a field
	 * mid-phase. Branch on `downloadable` instead. */
	download_url: null;
	/** True exactly when `GET …/exports/{job_id}/download` will answer 200
	 * rather than 409 — the job is complete AND carries an artifact key. */
	downloadable: boolean;
	error: string | null;
}

/** Body of `GET /v1/admin/tenants/{user_id}/exports/{job_id}/download` (03-18). */
export interface AdminTenantExportDownload {
	job_id: string;
	download_url: string;
	expires_in_seconds: number;
}

export interface AdminTenantExportListResponse {
	items: AdminTenantExportListItem[];
}

export interface AdminUserListItem {
	id: string;
	email: string | null;
	/** The third identifier `GET /v1/admin/users?q=` accepts, now emitted as
	 * well as matched (03-20, G-03-62). ALWAYS PRESENT, sometimes null: the
	 * column is nullable and partner-tenant rows are constrained to NULL. */
	clerk_id: string | null;
	name: string | null;
	created_at: string;
	/** The billing gate lets this tenant through without a subscription read:
	 * operator-exempted, partner tenant, or grandfathered (06.3 WR-02). */
	billing_exempt: boolean;
}

export interface AdminUserListResponse {
	items: AdminUserListItem[];
}

export type TenantPurgeStatus = "pending" | "purging" | "complete" | "failed";

export interface AdminTenantDeleteResponse {
	lifecycle_id: string;
	purge_after: string;
}

export interface AdminPurgeCancelResponse {
	canceled: boolean;
}

export interface AdminTenantPurgeInfo {
	status: TenantPurgeStatus;
	purge_after: string;
	completed_at: string | null;
	/** Last recorded sweep failure; null until an attempt fails (03-16). */
	error: string | null;
	/** 0 until the worker claims the row for the first time — an int, never null. */
	attempt_count: number;
	/** When the most recent attempt was claimed; null for a never-claimed row. */
	claimed_at: string | null;
}

export interface AdminTenantLifecycleResponse {
	fenced: boolean;
	purge: AdminTenantPurgeInfo | null;
	/** The server's LIVE `tenant_purge_grace_days`. REQUIRED and present even
	 * when `purge` is null, because the typed-confirmation dialog needs the
	 * window before anything is scheduled (03-16, CR-02 / WR-13). */
	grace_days: number;
}

/** Body of `PUT /v1/admin/auth/billing-exemptions` (06.3 WR-02). */
export interface AdminBillingExemptionUpdate {
	target_clerk_id: string;
	exempt: boolean;
	reason: string;
}

export interface AdminBillingExemptionResponse {
	target_clerk_id: string;
	user_id: string;
	exempt: boolean;
	exempted_at: string | null;
	/** False when the tenant was already in the requested state. */
	changed: boolean;
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------

async function adminFetch<T>(
	path: string,
	options: { method?: "GET" | "POST" | "PUT"; body?: unknown; query?: Record<string, string> } = {},
): Promise<T> {
	const base = API_URL.endsWith("/") ? API_URL : `${API_URL}/`;
	const url = new URL(path.replace(/^\/+/, ""), base);
	for (const [name, value] of Object.entries(options.query ?? {})) {
		url.searchParams.set(name, value);
	}
	const headers = new Headers();
	// Header-only transmission: the key never appears in the URL, the body,
	// or anything an access log or analytics event would capture.
	headers.set("X-Admin-Key", getAdminKey() ?? "");
	if (options.body !== undefined) headers.set("Content-Type", "application/json");

	const controller = new AbortController();
	let timedOut = false;
	const timeoutId = setTimeout(() => {
		timedOut = true;
		controller.abort();
	}, REQUEST_TIMEOUT_MS);
	let response: Response;
	try {
		response = await fetch(url.toString(), {
			method: options.method ?? "GET",
			headers,
			body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
			signal: controller.signal,
		});
	} catch (cause: unknown) {
		throw new ApiNetworkError(timedOut ? "timeout" : "offline", { cause });
	} finally {
		clearTimeout(timeoutId);
	}
	if (!response.ok) {
		let detail = response.statusText;
		try {
			detail = (await response.text()) || response.statusText;
		} catch {
			// Unreadable error body — keep the status text.
		}
		throw new ApiError(response.status, detail);
	}
	const parsed: unknown = await response.json();
	return parsed as T;
}

// ---------------------------------------------------------------------------
// Tenant picker + exports (ACCESS-04, 03-07 contract)
// ---------------------------------------------------------------------------

/**
 * Tenant lookup by ANY identifier an operator holds: user id, clerk id, or an
 * email fragment (03-16, W-05 / IN-04). The endpoint's `email` parameter still
 * exists for older callers; `q` is the identifier-agnostic one and wins.
 */
export function lookupUsers(term: string): Promise<AdminUserListResponse> {
	return adminFetch("/v1/admin/users", { query: { q: term } });
}

export function triggerTenantExport(
	userId: string,
	includeVaultValues: boolean,
): Promise<AdminTenantExportCreatedResponse> {
	return adminFetch(`/v1/admin/tenants/${encodeURIComponent(userId)}/exports`, {
		method: "POST",
		body: { include_vault_values: includeVaultValues },
	});
}

export function listTenantExports(userId: string): Promise<AdminTenantExportListResponse> {
	return adminFetch(`/v1/admin/tenants/${encodeURIComponent(userId)}/exports`);
}

/**
 * Mint ONE presigned download link for one export job (03-18).
 *
 * The list is a pure read since 03-18, so the console holds no URL: it asks
 * for one at the moment the operator clicks. Each call writes exactly one
 * `tenant.export_link_issued` audit row, which is the point — the trail
 * records operator intent rather than a 4-second poll.
 */
export function downloadTenantExport(
	userId: string,
	jobId: string,
): Promise<AdminTenantExportDownload> {
	return adminFetch(
		`/v1/admin/tenants/${encodeURIComponent(userId)}/exports/${encodeURIComponent(jobId)}/download`,
	);
}

// ---------------------------------------------------------------------------
// Tenant deletion lifecycle (ACCESS-04, 03-08 contract)
// ---------------------------------------------------------------------------

export function deleteTenant(userId: string): Promise<AdminTenantDeleteResponse> {
	return adminFetch(`/v1/admin/tenants/${encodeURIComponent(userId)}/delete`, {
		method: "POST",
	});
}

export function cancelTenantPurge(userId: string): Promise<AdminPurgeCancelResponse> {
	return adminFetch(`/v1/admin/tenants/${encodeURIComponent(userId)}/purge/cancel`, {
		method: "POST",
	});
}

export function getTenantLifecycle(userId: string): Promise<AdminTenantLifecycleResponse> {
	return adminFetch(`/v1/admin/tenants/${encodeURIComponent(userId)}/lifecycle`);
}

// ---------------------------------------------------------------------------
// Billing exemption (06.3 WR-02)
// ---------------------------------------------------------------------------

/**
 * Grant or revoke a Clerk tenant's billing exemption. Idempotent; the reason
 * is written to the `billing.exempt_granted` / `billing.exempt_revoked`
 * audit event. Partner tenants have no Clerk id and are always exempt.
 */
export function setBillingExemption(
	body: AdminBillingExemptionUpdate,
): Promise<AdminBillingExemptionResponse> {
	return adminFetch("/v1/admin/auth/billing-exemptions", { method: "PUT", body });
}
