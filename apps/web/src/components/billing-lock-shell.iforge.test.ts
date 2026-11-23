import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";

/**
 * Fork-owned pins for 06.3 UI-SPEC L1 (D-03): a billing-locked shell sends no
 * gated `/v1` request.
 *
 * While locked every gated route answers 403 `billing_required` (plan 06.3-01),
 * so the sidebar, notification center and command palette disable those
 * queries instead of polling them into error toasts and endless skeletons. The
 * lock is UX only; widening the backend exemption is the rejected alternative.
 *
 * These tests read the shipped sources and assert on the extracted call or
 * initializer, with comments stripped so a comment cannot satisfy a pin. Every
 * extracted slice is asserted non-empty and unique, so a pin that matches
 * nothing fails. They live beside the components, not in an upstream-tracked
 * test, so an upstream merge cannot drop them (03.1 D-02).
 */

function source(relative: string): string {
	return readFileSync(new URL(relative, import.meta.url), "utf8");
}

const sidebar = source("./app-sidebar.tsx");
const notificationCenter = source("./notification-center.tsx");
const commandPalette = source("./command-palette.tsx");
const suspensionBoundary = source("./account-suspension-boundary.tsx");

function stripLineComments(code: string): string {
	return code
		.split("\n")
		.map((line) => {
			const comment = line.indexOf("//");
			return comment === -1 ? line : line.slice(0, comment);
		})
		.join("\n");
}

function escapeRegExp(text: string): string {
	return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** From the opening `(` at `open` to its matching `)`, inclusive. */
function balancedCall(code: string, open: number): string {
	expect(code[open]).toBe("(");
	let depth = 0;
	for (let i = open; i < code.length; i++) {
		if (code[i] === "(") depth++;
		else if (code[i] === ")") {
			depth--;
			if (depth === 0) return code.slice(open, i + 1);
		}
	}
	throw new Error("unbalanced call");
}

/** The one `useQuery("get", "<endpoint>", ...)` call, from the endpoint literal to its closing paren. */
function useQueryCall(code: string, endpoint: string): string {
	const pattern = new RegExp(`useQuery\\(\\s*"get",\\s*"${escapeRegExp(endpoint)}"`, "g");
	const matches = [...code.matchAll(pattern)];
	expect(matches).toHaveLength(1);
	const match = matches[0];
	const open = (match.index ?? -1) + "useQuery".length;
	const call = balancedCall(code, open);
	const slice = stripLineComments(call.slice(call.indexOf(`"${endpoint}"`)));
	expect(slice.length).toBeGreaterThan(endpoint.length + 2);
	return slice;
}

/** The one `const <name> =` initializer, up to its terminating `;`. */
function initializer(code: string, name: string): string {
	const binding = `const ${name} =`;
	expect(code.split(binding)).toHaveLength(2);
	const start = code.indexOf(binding) + binding.length;
	const end = code.indexOf(";", start);
	expect(end).toBeGreaterThan(start);
	const slice = stripLineComments(code.slice(start, end)).trim();
	expect(slice.length).toBeGreaterThan(0);
	return slice;
}

/** An `enabled:` option whose expression carries the `!billingLocked` term. */
const LOCK_TERM = /enabled:[^,}]*!billingLocked\b/;

describe("06.3 L1: a billing-locked shell sends no gated /v1 request", () => {
	test("each component reads the lock exactly once", () => {
		for (const code of [sidebar, notificationCenter, commandPalette]) {
			expect(code.match(/const billingLocked = useBillingLocked\(\);/g) ?? []).toHaveLength(1);
			expect(code).toContain('import { useBillingLocked } from "@/lib/billing-lock";');
			expect(code).toContain("iforge (06.3 L1)");
		}
	});

	test.each([
		["app sidebar", "/v1/agents"],
		["app sidebar", "/v1/projects"],
	])("%s: GET %s is disabled while locked", (_site, endpoint) => {
		expect(useQueryCall(sidebar, endpoint)).toMatch(LOCK_TERM);
	});

	test("app sidebar: the default-project bindings query is disabled while locked", () => {
		const marker = "useAgentProjectBindings(activeAgent?.id,";
		expect(sidebar.split(marker)).toHaveLength(2);
		const open = sidebar.indexOf(marker) + "useAgentProjectBindings".length;
		const call = stripLineComments(balancedCall(sidebar, open));
		expect(call.length).toBeGreaterThan(marker.length);
		expect(call).toMatch(LOCK_TERM);
	});

	test("app sidebar: a disabled agents query never reads as a cold-loading rail", () => {
		// The lock term is read by agentRailLoadState, whose logic test pins the
		// locked rail as settled (app-sidebar.logic.test.ts).
		expect(initializer(sidebar, "agentRailState")).toMatch(
			/agentRailLoadState\(\{[^}]*billingLocked,/,
		);
	});

	test.each([["/v1/me/invitations"], ["/v1/companions/approvals"], ["/v1/companions/tools"]])(
		"notification center: GET %s is disabled while locked",
		(endpoint) => {
			expect(useQueryCall(notificationCenter, endpoint)).toMatch(LOCK_TERM);
		},
	);

	test("notification center: the invitations query keeps its refetch-on-focus option", () => {
		expect(useQueryCall(notificationCenter, "/v1/me/invitations")).toContain(
			"refetchOnWindowFocus: true",
		);
	});

	test("notification center: the companion queries keep retry: false", () => {
		expect(useQueryCall(notificationCenter, "/v1/companions/approvals")).toContain("retry: false");
		expect(useQueryCall(notificationCenter, "/v1/companions/tools")).toContain("retry: false");
	});

	test("command palette: remoteSearchReady carries the lock term", () => {
		expect(initializer(commandPalette, "remoteSearchReady")).toContain("!billingLocked");
	});

	test("command palette: GET /v1/search is gated by remoteSearchReady", () => {
		expect(useQueryCall(commandPalette, "/v1/search")).toMatch(/enabled:[^,}]*remoteSearchReady/);
	});

	test("AccountSuspensionBoundary (GET /v1/auth/me) is untouched by the lock", () => {
		expect(suspensionBoundary.length).toBeGreaterThan(0);
		expect(suspensionBoundary).not.toContain("billingLocked");
		expect(suspensionBoundary).not.toContain("useBillingLocked");
	});
});
