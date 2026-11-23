import { describe, expect, test } from "bun:test";
import { isTrustedRuntimeTokenUrl, parseRuntimeHostSuffixes } from "./runtime-token-hosts";

const suffixes = parseRuntimeHostSuffixes(" .agents.example , files.example ");

describe("runtime token hosts", () => {
	test("parses and normalizes suffixes", () => {
		expect(suffixes).toEqual(["agents.example", "files.example"]);
		expect(parseRuntimeHostSuffixes(undefined)).toEqual([]);
	});

	test("accepts an https host under a configured suffix", () => {
		expect(
			isTrustedRuntimeTokenUrl(
				"https://agent-42.agents.example/.well-known/openclaw/browser-session",
				suffixes,
				false,
			),
		).toBe(true);
		expect(isTrustedRuntimeTokenUrl("https://hdep-1.files.example/", suffixes, false)).toBe(true);
	});

	test("refuses other hosts, lookalikes, the bare suffix and non-https", () => {
		for (const url of [
			"https://evil.example/",
			"https://agents.example.evil.example/",
			"https://evilagents.example/",
			"https://agents.example/",
			"http://agent-42.agents.example/",
			"https://user@agent-42.agents.example/",
			"https://agent-42.agents.example/?next=x",
			"not a url",
		]) {
			expect(isTrustedRuntimeTokenUrl(url, suffixes, false)).toBe(false);
		}
	});

	test("unconfigured suffixes refuse unless explicitly allowed outside production", () => {
		expect(isTrustedRuntimeTokenUrl("https://any.example/", [], false)).toBe(false);
		expect(isTrustedRuntimeTokenUrl("https://any.example/", [], true)).toBe(true);
		expect(isTrustedRuntimeTokenUrl("http://any.example/", [], true)).toBe(false);
	});
});
