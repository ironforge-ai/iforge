import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";

/**
 * Fork-owned pin for 06.2-UI-SPEC H1 (D-02): billing settings render while
 * agent compute answers 503.
 *
 * A 503 on the hosted inventory leaves `cloudInventoryResolved` false. Upstream's
 * `billingAccessPending` waits on that inventory for every user, so Wallet,
 * Subscription and Usage would show `RouteLoadingSkeleton` for as long as compute
 * is down. The inventory only decides `hasExistingCloudAgents` for users whose
 * access does not grant v2; a v2 user already sees billing from access alone. The
 * fork narrows the inventory term to those users and changes nothing else.
 *
 * This test reads the shipped `settings-dialog.tsx`, extracts the one
 * `billingAccessPending` initializer and evaluates it over its truth table, so it
 * proves what the expression does at runtime rather than how it reads. It lives
 * beside the dialog, not in an upstream-tracked test, so an upstream merge cannot
 * drop it (03.1 D-02).
 */

const H1_MARKER = "iforge (06.2 H1)";
const BINDING = "const billingAccessPending =";
const dialogSource = readFileSync(new URL("./settings-dialog.tsx", import.meta.url), "utf8");

type HostedAccessStandIn = { isLoading: boolean; canCreateCloudAgents: boolean };

type PendingInputs = {
	requestedBillingSection: boolean;
	IS_HOSTED_BUILD: boolean;
	mounted: boolean;
	hostedAccess: HostedAccessStandIn;
	cloudInventoryResolved: boolean;
};

/** The binding from `const billingAccessPending =` through the end of its terminating line. */
function bindingStatement(): { initializer: string; statement: string } {
	expect(dialogSource.split(BINDING)).toHaveLength(2);
	const start = dialogSource.indexOf(BINDING);
	const terminator = dialogSource.indexOf(";", start);
	expect(terminator).toBeGreaterThan(start);
	const lineEnd = dialogSource.indexOf("\n", terminator);
	return {
		initializer: dialogSource.slice(start + BINDING.length, terminator),
		statement: dialogSource.slice(start, lineEnd === -1 ? undefined : lineEnd),
	};
}

/** Evaluate the shipped initializer with stand-ins for every name it reads. */
function evaluatePending(inputs: PendingInputs): unknown {
	const initializer = bindingStatement()
		.initializer.split("\n")
		.map((line) => {
			const comment = line.indexOf("//");
			return comment === -1 ? line : line.slice(0, comment);
		})
		.join("\n");
	const js = new Bun.Transpiler({ loader: "ts" }).transformSync(
		`export function pending(requestedBillingSection, IS_HOSTED_BUILD, mounted, hostedAccess, cloudInventoryResolved) { return (${initializer}); }`,
	);
	const body = js.slice(js.indexOf("{") + 1, js.lastIndexOf("}"));
	const pending = new Function(
		"requestedBillingSection",
		"IS_HOSTED_BUILD",
		"mounted",
		"hostedAccess",
		"cloudInventoryResolved",
		body,
	) as (...args: unknown[]) => unknown;
	return pending(
		inputs.requestedBillingSection,
		inputs.IS_HOSTED_BUILD,
		inputs.mounted,
		inputs.hostedAccess,
		inputs.cloudInventoryResolved,
	);
}

/** A hosted build, mounted, access loaded, a billing section requested, inventory 503. */
function inputs(overrides: Partial<PendingInputs> = {}): PendingInputs {
	return {
		requestedBillingSection: true,
		IS_HOSTED_BUILD: true,
		mounted: true,
		hostedAccess: { isLoading: false, canCreateCloudAgents: true },
		cloudInventoryResolved: false,
		...overrides,
	};
}

describe("06.2 H1: billing settings do not wait on compute for a v2 user", () => {
	test("a v2 user with the inventory unresolved is not pending, so the Wallet panel renders", () => {
		expect(evaluatePending(inputs())).toBe(false);
	});

	test("a user whose access does not grant v2 still waits on the inventory", () => {
		expect(
			evaluatePending(inputs({ hostedAccess: { isLoading: false, canCreateCloudAgents: false } })),
		).toBe(true);
	});

	test("an unmounted dialog is still pending", () => {
		expect(evaluatePending(inputs({ mounted: false }))).toBe(true);
	});

	test("loading access is still pending", () => {
		expect(
			evaluatePending(inputs({ hostedAccess: { isLoading: true, canCreateCloudAgents: true } })),
		).toBe(true);
	});

	test("a non-billing section is never pending", () => {
		expect(evaluatePending(inputs({ requestedBillingSection: false }))).toBe(false);
	});

	test("an OSS build is never pending", () => {
		expect(evaluatePending(inputs({ IS_HOSTED_BUILD: false }))).toBe(false);
	});

	test("a resolved inventory is not pending for either access", () => {
		expect(evaluatePending(inputs({ cloudInventoryResolved: true }))).toBe(false);
		expect(
			evaluatePending(
				inputs({
					cloudInventoryResolved: true,
					hostedAccess: { isLoading: false, canCreateCloudAgents: false },
				}),
			),
		).toBe(false);
	});
});

describe("06.2 H1: the fork edit is the one marked binding", () => {
	test("the billingAccessPending binding carries the iforge H1 marker, once in the file", () => {
		expect(bindingStatement().statement).toContain(H1_MARKER);
		expect(dialogSource.split(H1_MARKER)).toHaveLength(2);
	});
});
