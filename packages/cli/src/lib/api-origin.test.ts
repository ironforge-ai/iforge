import { describe, expect, test } from "bun:test";
import { DESTINATIONS } from "@clawdi/shared";
import {
	canonicalApiOrigin,
	isProductionCloudApiOrigin,
	normalizeCloudApiBaseUrl,
	normalizeHostedDeployApiBaseUrl,
} from "./api-origin";

describe("isProductionCloudApiOrigin", () => {
	test("accepts the authority's cloud API origin and nothing else in the family", () => {
		expect(isProductionCloudApiOrigin(DESTINATIONS.cloudApiOrigin)).toBe(true);
		// The deploy API is a sibling host, not a spelling of the same one (P33-24).
		expect(isProductionCloudApiOrigin(DESTINATIONS.deployApiOrigin)).toBe(false);
		expect(isProductionCloudApiOrigin(DESTINATIONS.webOrigin)).toBe(false);
		expect(isProductionCloudApiOrigin(DESTINATIONS.assetsOrigin)).toBe(false);
	});

	test("rejects a host that merely ends with the fork's domain", () => {
		for (const impostor of [
			`https://evil-${DESTINATIONS.apexDomain}`,
			`https://not${DESTINATIONS.apexDomain}`,
			`https://cloud-api.${DESTINATIONS.apexDomain}.attacker.example`,
			`https://xcloud-api.${DESTINATIONS.apexDomain}`,
		]) {
			expect(isProductionCloudApiOrigin(impostor)).toBe(false);
		}
	});

	test("rejects the production origin carrying a path", () => {
		expect(isProductionCloudApiOrigin(`${DESTINATIONS.cloudApiOrigin}/`)).toBe(false);
		expect(isProductionCloudApiOrigin(`${DESTINATIONS.cloudApiOrigin}/v1`)).toBe(false);
		expect(isProductionCloudApiOrigin(`${DESTINATIONS.cloudApiOrigin}/v1/auth/me`)).toBe(false);
	});

	test("rejects the production origin carrying a query or a fragment", () => {
		expect(isProductionCloudApiOrigin(`${DESTINATIONS.cloudApiOrigin}?token=leak`)).toBe(false);
		expect(isProductionCloudApiOrigin(`${DESTINATIONS.cloudApiOrigin}#leak`)).toBe(false);
	});

	test("rejects a case variant of the production origin", () => {
		expect(isProductionCloudApiOrigin(DESTINATIONS.cloudApiOrigin.toUpperCase())).toBe(false);
		expect(
			isProductionCloudApiOrigin(DESTINATIONS.cloudApiOrigin.replace("cloud-api", "Cloud-API")),
		).toBe(false);
	});

	test("rejects a scheme downgrade of the production origin", () => {
		expect(
			isProductionCloudApiOrigin(DESTINATIONS.cloudApiOrigin.replace("https://", "http://")),
		).toBe(false);
	});

	test("rejects an empty or non-origin input", () => {
		for (const junk of ["", " ", "cloud-api", "//cloud-api.iforge.dev"]) {
			expect(isProductionCloudApiOrigin(junk)).toBe(false);
		}
	});
});

describe("the authority's origins route through this module's validation", () => {
	test("the cloud API origin survives normalizeCloudApiBaseUrl unchanged", () => {
		expect(normalizeCloudApiBaseUrl(DESTINATIONS.cloudApiOrigin)).toBe(DESTINATIONS.cloudApiOrigin);
		expect(canonicalApiOrigin(DESTINATIONS.cloudApiOrigin)).toBe(DESTINATIONS.cloudApiOrigin);
	});

	test("the deploy API origin survives normalizeHostedDeployApiBaseUrl unchanged", () => {
		expect(normalizeHostedDeployApiBaseUrl(DESTINATIONS.deployApiOrigin)).toBe(
			DESTINATIONS.deployApiOrigin,
		);
		expect(normalizeHostedDeployApiBaseUrl(`${DESTINATIONS.deployApiOrigin}/v2`)).toBe(
			DESTINATIONS.deployApiOrigin,
		);
	});

	test("the validation the authority is routed through still refuses hostile authorities", () => {
		expect(() => normalizeCloudApiBaseUrl("https://user:pw@cloud-api.iforge.dev")).toThrow(
			"must not contain credentials",
		);
		expect(() => normalizeCloudApiBaseUrl("https://cloud-api.iforge.dev?x=1")).toThrow(
			"must not contain a query or fragment",
		);
		expect(() => normalizeCloudApiBaseUrl("http://cloud-api.iforge.dev")).toThrow("must use HTTPS");
		expect(() => normalizeCloudApiBaseUrl("https://cloud-api.iforge.dev/v1")).toThrow(
			"must not contain a non-root path",
		);
	});
});
