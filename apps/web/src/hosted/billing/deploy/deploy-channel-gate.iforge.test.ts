import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { resolveDeployChannel } from "@/lib/deploy-channel";

/**
 * Fork-owned pins for D-10 (06.1-UI-SPEC §T3): the Sui partner deploy channel
 * is unreachable in iforge.
 *
 * Upstream routes `?deploy_profile=sui`, `?utm_source=sui` and a sign-in or
 * sign-up `redirect_url` nesting either one to a "Preinstalled plugins" section
 * with a "Sui bundle" card, and sends `plugin_bundle: "sui"` with the deploy
 * request. The fork gates the channel to `null` at its single consumer, the
 * wizard's channel binding line, and leaves `lib/deploy-channel.ts` and its
 * test byte-identical to upstream. This file proves both halves: the gate
 * holds, and the resolver it bypasses still recognises every Sui URL, so a
 * resolver edit is not what blocks the path.
 *
 * The file lives beside the wizard, not in the upstream-tracked
 * `deploy-wizard.test.ts` or `deploy-channel.test.ts`, so an upstream rewrite
 * cannot drop it in a merge resolution (03.1 D-02).
 */

const GATE_MARKER = "iforge (06.1 D-10)";
const wizardSource = readFileSync(new URL("./deploy-wizard.tsx", import.meta.url), "utf8");

/** Every URL shape upstream resolves to the Sui channel. */
const SUI_SEARCHES = [
	"?deploy_profile=sui",
	"?utm_source=sui",
	`?redirect_url=${encodeURIComponent("/deploy?deploy_profile=sui")}`,
	`?redirect_url=${encodeURIComponent("/deploy?utm_source=sui")}`,
] as const;

/** The wizard's one `const channel = …` line. */
function channelBindingLine(): string {
	const lines = wizardSource.split("\n").filter((line) => /\bconst channel\b/.test(line));
	expect(lines).toHaveLength(1);
	return lines[0] as string;
}

/**
 * Evaluate the binding line's right-hand side with a stand-in resolver that
 * records every call, so the test sees what the shipped expression does at
 * runtime rather than only how it reads.
 */
function evaluateChannelBinding(search: string): { channel: unknown; resolverCalls: number } {
	const line = channelBindingLine();
	const comment = line.indexOf("//");
	const statement = (comment === -1 ? line : line.slice(0, comment)).trim();
	const js = new Bun.Transpiler({ loader: "ts" }).transformSync(
		`export function bind(resolveDeployChannel, search) { ${statement} return channel; }`,
	);
	const body = js.slice(js.indexOf("{") + 1, js.lastIndexOf("}"));
	let resolverCalls = 0;
	const recordingResolver = (value: string) => {
		resolverCalls += 1;
		return resolveDeployChannel(value);
	};
	const bind = new Function("resolveDeployChannel", "search", body) as (
		resolver: typeof resolveDeployChannel,
		search: string,
	) => unknown;
	return { channel: bind(recordingResolver, search), resolverCalls };
}

/** The JSX block rendered only under `{channel ? ( … ) : null}`. */
function channelGatedBlock(): string {
	const start = wizardSource.indexOf("{channel ? (");
	expect(start).toBeGreaterThan(-1);
	const end = wizardSource.indexOf(") : null}", start);
	expect(end).toBeGreaterThan(start);
	return wizardSource.slice(start, end);
}

describe("D-10: the partner deploy channel is gated at its single consumer", () => {
	test("the channel binding line carries the iforge D-10 marker", () => {
		expect(channelBindingLine()).toContain(GATE_MARKER);
		expect(wizardSource.split(GATE_MARKER)).toHaveLength(2);
	});

	test("for every Sui URL the binding yields null without calling the resolver", () => {
		for (const search of SUI_SEARCHES) {
			expect(evaluateChannelBinding(search)).toEqual({ channel: null, resolverCalls: 0 });
		}
	});

	test("the wizard reads the resolver only on the gated line", () => {
		const reads = wizardSource.split("\n").filter((line) => line.includes("resolveDeployChannel"));
		expect(reads).toEqual([
			'import { resolveDeployChannel } from "@/lib/deploy-channel";',
			channelBindingLine(),
		]);
	});
});

describe("D-10: the upstream resolver is untouched", () => {
	test("resolveDeployChannel still returns the Sui channel for every Sui URL", () => {
		for (const search of SUI_SEARCHES) {
			expect(resolveDeployChannel(search)).toBe("sui");
		}
		expect(resolveDeployChannel("?deploy_profile=other")).toBeNull();
	});
});

describe("D-10: nothing Sui renders or ships without a channel", () => {
	test("every plugin_bundle emission is conditioned on the channel", () => {
		const emissions = wizardSource.split("\n").filter((line) => line.includes("plugin_bundle"));
		expect(emissions.length).toBeGreaterThan(0);
		for (const line of emissions) {
			expect(line).toMatch(/\bchannel &&/);
		}
	});

	test("the Preinstalled plugins section and the Sui bundle card render only under the channel", () => {
		const block = channelGatedBlock();
		for (const copy of ['"Preinstalled plugins"', '"Sui bundle"']) {
			expect(block).toContain(copy);
			// Nowhere else: no heading without cards, no card without a heading.
			expect(wizardSource.split(copy)).toHaveLength(2);
		}
	});
});
