import { describe, expect, it } from "bun:test";

import {
	buildExactClawdiReference,
	parseClawdiReference,
	scanClawdiReferences,
	scanClawdiReferenceUses,
} from "../src/lib/secret-references";

describe("clawdi secret references", () => {
	it("parses project-relative references", () => {
		expect(parseClawdiReference("iforge://default/OPENAI_API_KEY")).toEqual({
			raw: "iforge://default/OPENAI_API_KEY",
			vault: "default",
			section: "",
			field: "OPENAI_API_KEY",
			isExact: false,
		});
		expect(parseClawdiReference("iforge://prod/openai/api_key")).toEqual({
			raw: "iforge://prod/openai/api_key",
			vault: "prod",
			section: "openai",
			field: "api_key",
			isExact: false,
		});
		expect(parseClawdiReference("iforge://project/OPENAI_API_KEY")).toEqual({
			raw: "iforge://project/OPENAI_API_KEY",
			vault: "project",
			section: "",
			field: "OPENAI_API_KEY",
			isExact: false,
		});
	});

	it("parses exact project references", () => {
		expect(
			parseClawdiReference(
				"iforge://project/00000000-0000-0000-0000-000000000123/vault/default/field/OPENAI_API_KEY",
			),
		).toEqual({
			raw: "iforge://project/00000000-0000-0000-0000-000000000123/vault/default/field/OPENAI_API_KEY",
			project: "00000000-0000-0000-0000-000000000123",
			vault: "default",
			section: "",
			field: "OPENAI_API_KEY",
			isExact: true,
		});
		expect(
			parseClawdiReference("iforge://project/engineering/vault/prod/section/openai/field/api_key"),
		).toEqual({
			raw: "iforge://project/engineering/vault/prod/section/openai/field/api_key",
			project: "engineering",
			vault: "prod",
			section: "openai",
			field: "api_key",
			isExact: true,
		});
	});

	it("builds exact references with percent-encoded resource parts", () => {
		const reference = buildExactClawdiReference(
			"00000000-0000-0000-0000-000000000123",
			"prod vault",
			"api/providers",
			"OPENAI API KEY",
		);

		expect(reference).toBe(
			"iforge://project/00000000-0000-0000-0000-000000000123/vault/prod%20vault/section/api%2Fproviders/field/OPENAI%20API%20KEY",
		);
		expect(parseClawdiReference(reference)).toEqual({
			raw: reference,
			project: "00000000-0000-0000-0000-000000000123",
			vault: "prod vault",
			section: "api/providers",
			field: "OPENAI API KEY",
			isExact: true,
		});
	});

	it("scans exact and relative references from templates", () => {
		const refs = scanClawdiReferences(
			[
				"OPENAI_API_KEY=iforge://project/00000000-0000-0000-0000-000000000123/vault/default/field/OPENAI_API_KEY",
				"STRIPE_SECRET_KEY=iforge://prod/stripe/secret_key",
			].join("\n"),
		);

		expect(refs.map((ref) => ref.raw)).toEqual([
			"iforge://project/00000000-0000-0000-0000-000000000123/vault/default/field/OPENAI_API_KEY",
			"iforge://prod/stripe/secret_key",
		]);
	});

	it("scans reference locations for template diagnostics", () => {
		const uses = scanClawdiReferenceUses(
			[
				"OPENAI_API_KEY=iforge://project/00000000-0000-0000-0000-000000000123/vault/default/field/OPENAI_API_KEY",
				"STRIPE_SECRET_KEY=iforge://prod/stripe/secret_key",
			].join("\n"),
		);

		expect(uses.map((use) => ({ raw: use.ref.raw, line: use.line }))).toEqual([
			{
				raw: "iforge://project/00000000-0000-0000-0000-000000000123/vault/default/field/OPENAI_API_KEY",
				line: 1,
			},
			{ raw: "iforge://prod/stripe/secret_key", line: 2 },
		]);
	});
});
