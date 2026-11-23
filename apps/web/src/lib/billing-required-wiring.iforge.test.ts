import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";

/**
 * Fork-owned pins for the G-06.3-1b wiring (06.3 D-03): the cloud-api fetch
 * path feeds the per-identity billing-required signal, `AuthRouterBridge`
 * creates one signal per identity scope, and `BillingLockProvider` turns a
 * refusal into a bounded gate re-run.
 *
 * The sources are read with comments stripped so a comment cannot satisfy a
 * pin, and every extracted slice is asserted non-empty and unique, so a pin
 * that matches nothing fails. They live beside the signal module, not in an
 * upstream-tracked test, so an upstream merge cannot drop them (03.1 D-02).
 */

function source(relative: string): string {
	return readFileSync(new URL(relative, import.meta.url), "utf8");
}

function stripComments(code: string): string {
	return code
		.replace(/\/\*[\s\S]*?\*\//g, "")
		.split("\n")
		.map((line) => {
			const comment = line.indexOf("//");
			return comment === -1 ? line : line.slice(0, comment);
		})
		.join("\n");
}

const api = stripComments(source("./api.ts"));
const bridge = stripComments(source("../components/auth-router-bridge.tsx"));
const billingLock = stripComments(source("./billing-lock.tsx"));

/** The one top-level `function <name>(` declaration, through its closing `}` at column 0. */
function topLevelFunction(code: string, name: string): string {
	const pattern = new RegExp(`^(?:export )?function ${name}\\(`, "gm");
	const matches = [...code.matchAll(pattern)];
	expect(matches).toHaveLength(1);
	const start = matches[0]?.index ?? -1;
	const end = code.indexOf("\n}\n", start);
	expect(end).toBeGreaterThan(start);
	const slice = code.slice(start, end + 2);
	expect(slice.length).toBeGreaterThan(name.length + 12);
	return slice;
}

function occurrences(code: string, needle: string): number {
	return code.split(needle).length - 1;
}

describe("api.ts: every cloud-api response reaches the billing-required signal", () => {
	const body = topLevelFunction(api, "useAccountFetch");

	test("useAccountFetch reads the identity scope's signal once", () => {
		expect(occurrences(body, "const billingRequired = useBillingRequiredSignal();")).toBe(1);
		expect(api).toContain('from "@/lib/billing-required-signal"');
	});

	test("the signal observes the response after the suspension store", () => {
		const suspension = body.indexOf("await suspension.observeResponse(response);");
		const billing = body.indexOf("await billingRequired?.observeResponse(response);");
		expect(suspension).toBeGreaterThan(-1);
		expect(billing).toBeGreaterThan(suspension);
		expect(occurrences(body, "billingRequired?.observeResponse(response)")).toBe(1);
		expect(body.indexOf("return response;")).toBeGreaterThan(billing);
	});

	test("the signal is a useCallback dependency", () => {
		expect(body).toMatch(/\[\s*suspension,\s*billingRequired\s*,?\s*\]/);
	});
});

describe("auth-router-bridge.tsx: one signal per identity scope", () => {
	const component = topLevelFunction(bridge, "AuthRouterBridge");

	test("both scope constructions create a fresh signal", () => {
		expect(occurrences(component, "billingRequired: createBillingRequiredSignal(),")).toBe(2);
		const initial = component.slice(component.indexOf("useState(() => ({"));
		expect(initial.indexOf("billingRequired: createBillingRequiredSignal()")).toBeGreaterThan(0);
		const change = component.slice(component.indexOf("setScope({"));
		expect(change.indexOf("billingRequired: createBillingRequiredSignal()")).toBeGreaterThan(0);
	});

	test("the signal is provided beside the account-suspension store", () => {
		const provider = "<BillingRequiredSignalContext value={scope.billingRequired}>";
		expect(occurrences(component, provider)).toBe(1);
		const suspension = component.indexOf("<AccountSuspensionContext value={scope.suspension}>");
		const signal = component.indexOf(provider);
		const queryClient = component.indexOf("<QueryClientProvider client={scope.queryClient}>");
		expect(suspension).toBeGreaterThan(-1);
		expect(signal).toBeGreaterThan(suspension);
		expect(queryClient).toBeGreaterThan(signal);
		expect(component.indexOf("</BillingRequiredSignalContext>")).toBeGreaterThan(queryClient);
	});
});

describe("billing-lock.tsx: a refusal while unlocked re-runs the gate", () => {
	const provider = topLevelFunction(billingLock, "BillingLockProvider");
	const recheck = topLevelFunction(billingLock, "useBillingRequiredRecheck");

	test("BillingLockProvider hosts the recheck hook with its lock state", () => {
		expect(occurrences(provider, "useBillingRequiredRecheck(locked);")).toBe(1);
	});

	test("the controller forgets the provisioned verdict and re-runs the gate via router.invalidate()", () => {
		expect(occurrences(recheck, "createGateRecheckController(")).toBe(1);
		expect(recheck).toContain("forgetVerdict: forgetProvisionedVerdict,");
		expect(recheck).toMatch(/rerunGate: \(\) => router\.invalidate\(\),/);
		expect(recheck).toContain("now: Date.now,");
		expect(billingLock).toContain(
			'import { forgetProvisionedVerdict } from "@/lib/access-verdict-memo";',
		);
	});

	test("the controller is keyed on the identity scope's signal", () => {
		expect(occurrences(recheck, "const signal = useBillingRequiredSignal();")).toBe(1);
		expect(recheck).toMatch(/\[\s*signal,\s*router\s*\]/);
		expect(recheck).toMatch(/\[\s*signal,\s*controller\s*\]/);
	});

	test("the listener passes the current lock state and returns the unsubscribe", () => {
		expect(recheck).toContain("return signal.subscribe(");
		expect(occurrences(recheck, "controller.onRefusal(lockedRef.current)")).toBe(1);
	});
});
