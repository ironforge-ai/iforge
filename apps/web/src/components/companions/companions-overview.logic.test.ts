/**
 * S1 and S2 copy held against `06-UI-SPEC.md` §Copywriting Contract, and the
 * pure rules behind both surfaces.
 *
 * **Both sides are read.** Every shipped constant is compared with the string
 * the contract row quotes, read from the spec file at test time through
 * `ui-spec-contract.ts`, so editing either side by one character fails here.
 * A row that goes missing THROWS rather than comparing nothing to nothing.
 */

import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { ApiError, ApiNetworkError } from "@/lib/api-errors";
import { uiSpecCopy, uiSpecRowCells, uiSpecRowQuotes } from "@/lib/ui-spec-contract";
import {
	blockingFailure,
	COMPANION_DESCRIPTIONS,
	COMPANION_SECTION_TABS,
	type CompanionCatalogItem,
	type CompanionInstallation,
	deriveOverviewModel,
	fromPriceLabel,
	hasReachedComplete,
	INSTALL_DIALOG_COPY,
	ISOLATION_TIER_LABELS,
	ISOLATION_TIERS,
	installationStateChip,
	installCredentialNote,
	installDialogDescription,
	installDialogTitle,
	installPriceBody,
	OVERVIEW_COPY,
	OVERVIEW_EMPTY_COPY,
	OVERVIEW_ERROR_NORMALIZER,
	OVERVIEW_SEARCH_EMPTY_COPY,
	PRICE_PLACEHOLDER,
	reachabilityChip,
	TRANSPORT_FAILURE_COPY,
	tierOptions,
	usdMicrosToDecimal,
} from "./companions-overview.logic";

/** The phase-06 UI contract, resolved from this module rather than the cwd. */
const SPEC = fileURLToPath(
	new URL(
		"../../../../../.planning/phases/06-first-companion-end-to-end/06-UI-SPEC.md",
		import.meta.url,
	),
);

/**
 * The phase-06.1 UI contract. §T2 changes exactly one S2 row of 06-UI-SPEC:
 * the tier options become `EntityChoiceCard` titles and descriptions, and the
 * unavailable note becomes a chip (upstream deleted `ui/radio-group`).
 */
const SYNC_SPEC = (() => {
	// Located by the phase number: the directory slug names the upstream it syncs.
	const phases = fileURLToPath(new URL("../../../../../.planning/phases/", import.meta.url));
	const dir = readdirSync(phases).find((name) => name.startsWith("06.1-"));
	if (!dir) throw new Error(`no phase 06.1 directory under ${phases}`);
	return join(phases, dir, "06.1-UI-SPEC.md");
})();

const copy = (element: string) => uiSpecCopy(element, SPEC);
const quotes = (element: string) => uiSpecRowQuotes(element, SPEC);
/** A status-table cell such as `` `Queued` `` without its backticks. */
const cell = (element: string, index: number) =>
	(uiSpecRowCells(element, SPEC)[index] ?? "").replace(/`/g, "");

/** Widen a literal-typed constant so it compares with a string read from the spec. */
const shipped = (value: string): string => value;
const shippedAll = (values: readonly string[]): string[] => [...values];

const DIGEST = `sha256:${"c".repeat(64)}`;

function catalogItem(overrides: Partial<CompanionCatalogItem> = {}): CompanionCatalogItem {
	return {
		companion_key: "postiz",
		display_name: "Postiz",
		version: "v2.24.0",
		images: {
			app: { image: "postiz", tag: "v2.24.0", sha256: DIGEST },
			postgres: { image: "postgres", tag: "17", sha256: DIGEST },
			redis: { image: "redis", tag: "7", sha256: DIGEST },
			cloudflared: { image: "cloudflared", tag: "1", sha256: DIGEST },
		},
		tiers: [
			{ tier: "standard", offered: true },
			{ tier: "dedicated", offered: false },
			{ tier: "shared", offered: false },
		],
		from_monthly_price_usd_micros: 12_000_000,
		...overrides,
	};
}

function installation(overrides: Partial<CompanionInstallation> = {}): CompanionInstallation {
	return {
		id: "0b7e2d1c-0000-4000-8000-000000000002",
		agent_id: "6c3b1a2e-0000-4000-8000-000000000001",
		companion_key: "postiz",
		isolation_tier: "standard",
		operation: "install",
		status: "complete",
		step: null,
		error: null,
		version: "v2.24.0",
		pinned_digest: DIGEST,
		hostname: "postiz-acme.companions.iforge.dev",
		owner_email: "owner@example.com",
		credential: "stored_in_vault",
		reachability: { state: "reachable", checked_at: "2026-09-23T12:00:00Z" },
		created_at: "2026-09-23T11:00:00Z",
		updated_at: "2026-09-23T12:00:00Z",
		completed_at: "2026-09-23T11:05:00Z",
		update: {
			state: "up_to_date",
			offered_version: null,
			offered_digest: null,
			deferred_digest: null,
			last_backup_at: null,
			error_code: null,
		},
		...overrides,
	};
}

describe("S1 copy parity with the contract", () => {
	test("page title, tab label and primary CTA", () => {
		expect(shipped(OVERVIEW_COPY.title)).toBe(copy("S1 page title"));
		expect(shipped(COMPANION_SECTION_TABS[0].label)).toBe(copy("Nav / tab label"));
		// The contract bolds this row's element cell.
		expect(shipped(OVERVIEW_EMPTY_COPY.action)).toBe(copy("**Primary CTA (phase)**"));
	});

	test("page description", () => {
		expect(shipped(OVERVIEW_COPY.description)).toBe(copy("S1 page description"));
	});

	test("group headers, in order", () => {
		expect(shippedAll([OVERVIEW_COPY.installedGroup, OVERVIEW_COPY.availableGroup])).toEqual(
			quotes("S1 group headers"),
		);
	});

	test("search placeholder", () => {
		expect(shipped(OVERVIEW_COPY.searchPlaceholder)).toBe(copy("S1 search placeholder"));
	});

	test("Postiz's available-card description", () => {
		expect(shipped(COMPANION_DESCRIPTIONS.postiz)).toBe(
			copy("S1 available card description (Postiz)"),
		);
	});

	test("available card meta row and trailing button", () => {
		const [meta, button] = quotes("S1 available card");
		expect(fromPriceLabel(12_000_000)).toBe((meta ?? "").replace(PRICE_PLACEHOLDER, "$12.00"));
		expect(shipped(OVERVIEW_COPY.installButton)).toBe(button as string);
	});

	test("installed card meta row reads 'Installed {relative} · {tier}'", () => {
		const [template] = quotes("S1 installed card");
		expect(template).toBe("Installed {relative} · {tier}");
		const model = deriveOverviewModel({
			catalog: [catalogItem()],
			installations: [installation()],
			search: "",
		}).installed[0];
		expect(model?.installedLabel).toMatch(/^Installed \S/);
		expect(model?.tierLabel).toBe("Standard");
	});

	test("empty state — nothing installed, catalog available", () => {
		expect(
			shippedAll([OVERVIEW_EMPTY_COPY.title, OVERVIEW_EMPTY_COPY.body, OVERVIEW_EMPTY_COPY.action]),
		).toEqual(quotes("S1, nothing installed, catalog available"));
	});

	test("empty state — Available group filtered to zero", () => {
		expect(
			shippedAll([
				OVERVIEW_SEARCH_EMPTY_COPY.title,
				OVERVIEW_SEARCH_EMPTY_COPY.body,
				OVERVIEW_SEARCH_EMPTY_COPY.action,
			]),
		).toEqual(quotes("S1, Available group filtered to zero"));
	});

	test("transport failure", () => {
		expect(shipped(TRANSPORT_FAILURE_COPY)).toBe(
			copy("Transport failure (no HTTP response at all)"),
		);
	});
});

describe("S2 copy parity with the contract", () => {
	test("dialog title and description", () => {
		expect(installDialogTitle("Postiz")).toBe(copy("S2 dialog title"));
		expect(installDialogDescription("Postiz")).toBe(copy("S2 dialog description"));
	});

	test("tier group label, options in order, and the unavailable chip", () => {
		expect(shipped(INSTALL_DIALOG_COPY.tierGroupLabel)).toBe(copy("S2 tier group label"));
		expect(shipped(INSTALL_DIALOG_COPY.tierGroupLabel)).toBe(uiSpecCopy("Group legend", SYNC_SPEC));
		expect(Object.keys(INSTALL_DIALOG_COPY.tierOptions)).toEqual([...ISOLATION_TIERS]);
		for (const tier of ISOLATION_TIERS) {
			const [, title, description] = uiSpecRowCells(`\`${tier}\``, SYNC_SPEC);
			expect(shipped(INSTALL_DIALOG_COPY.tierOptions[tier].title)).toBe(title as string);
			expect(shipped(INSTALL_DIALOG_COPY.tierOptions[tier].description)).toBe(
				description as string,
			);
		}
		expect(shipped(INSTALL_DIALOG_COPY.unavailableNote)).toBe(
			uiSpecCopy("Not-offered chip", SYNC_SPEC),
		);
	});

	test("the 06.1 split keeps each tier's Phase 06 meaning, word for word", () => {
		// 06.1-UI-SPEC §T2: "Only the em-dash-joined sentence is split into the
		// card's title and description."
		const phase06 = quotes("S2 tier options");
		ISOLATION_TIERS.forEach((tier, index) => {
			const { title, description } = INSTALL_DIALOG_COPY.tierOptions[tier];
			const lowered = `${description.charAt(0).toLowerCase()}${description.slice(1)}`;
			expect(`${title} — ${lowered}`).toBe(phase06[index] as string);
		});
	});

	test("price label and body", () => {
		expect(shipped(INSTALL_DIALOG_COPY.priceLabel)).toBe(copy("S2 price block label"));
		expect(shipped(INSTALL_DIALOG_COPY.priceBodyTemplate)).toBe(copy("S2 price block body"));
		expect(installPriceBody("$12.00")).toBe(
			copy("S2 price block body").replace("{price}", "$12.00"),
		);
	});

	test("credential note and confirm CTA", () => {
		expect(installCredentialNote("Postiz")).toBe(copy("S2 credential note"));
		expect(shipped(INSTALL_DIALOG_COPY.confirm)).toBe(copy("S2 confirm CTA"));
	});

	test("the install confirmation row agrees on title and confirm label", () => {
		const row = uiSpecRowCells("Install a companion", SPEC);
		expect(row[1]).toBe(`"${installDialogTitle("Postiz")}"`);
		expect(row[3]).toBe(`"${INSTALL_DIALOG_COPY.confirm}"`);
		expect(row[4]).toBe("`false`");
	});

	test("rejection sentence", () => {
		expect(shipped(INSTALL_DIALOG_COPY.rejected)).toBe(copy("Install could not be started"));
	});

	test("the success toast is one the contract lists", () => {
		const spec = readFileSync(SPEC, "utf8");
		const toasts = spec.slice(spec.indexOf("### Toasts"), spec.indexOf("## Screen Contracts"));
		expect(toasts).toContain(`"${INSTALL_DIALOG_COPY.startedToast}"`);
	});

	test("tier labels are each contract option's sentence-case title", () => {
		for (const tier of ISOLATION_TIERS) {
			expect(INSTALL_DIALOG_COPY.tierOptions[tier].title).toBe(ISOLATION_TIER_LABELS[tier]);
		}
	});
});

describe("status chips follow §Status token mapping", () => {
	test("install job states: tone and label per row", () => {
		for (const status of ["queued", "running", "complete", "failed"] as const) {
			const chip = installationStateChip({ operation: "install", status });
			expect(chip.tone).toBe(cell(`\`${status}\``, 1) as typeof chip.tone);
			expect(chip.label).toBe(cell(`\`${status}\``, 2));
		}
	});

	test("update and uninstall swap only the label; the tone stays the job's", () => {
		expect(installationStateChip({ operation: "update", status: "running" })).toEqual({
			tone: "info",
			label: "Updating",
		});
		expect(installationStateChip({ operation: "update", status: "failed" }).label).toBe(
			"Update failed",
		);
		expect(installationStateChip({ operation: "uninstall", status: "running" }).label).toBe(
			"Removing",
		);
		expect(installationStateChip({ operation: "uninstall", status: "failed" }).label).toBe(
			"Removal failed",
		);
	});

	test("reachability chips", () => {
		const rows = {
			reachable: "Hostname resolves, TLS valid, app answers",
			tls_pending: "Hostname provisioned, certificate not yet live at the edge",
			not_answering: "Hostname provisioned, app not answering",
			no_hostname_yet: "No hostname yet (pre-`complete`)",
		} as const;
		for (const [state, element] of Object.entries(rows)) {
			const chip = reachabilityChip(state as keyof typeof rows);
			expect(chip.tone).toBe(cell(element, 1) as typeof chip.tone);
			expect(chip.label).toBe(cell(element, 2));
		}
	});

	test("the job enum is never a visible label", () => {
		for (const operation of ["install", "update", "uninstall", "export"] as const) {
			for (const status of ["queued", "running", "complete", "failed"] as const) {
				const { label } = installationStateChip({ operation, status });
				expect(label).not.toBe(status);
				expect(label).toMatch(/^[A-Z]/);
			}
		}
		for (const tier of ISOLATION_TIERS) {
			expect(ISOLATION_TIER_LABELS[tier]).not.toBe(tier);
		}
	});
});

describe("money", () => {
	test("micros convert with integer arithmetic and format through formatUsdExact", () => {
		expect(usdMicrosToDecimal(12_000_000)).toBe("12.000000");
		expect(usdMicrosToDecimal(12_345_678)).toBe("12.345678");
		expect(usdMicrosToDecimal(999)).toBe("0.000999");
		expect(fromPriceLabel(12_345_678)).toBe("From $12.35/month");
		expect(fromPriceLabel(1_234_567_000_000)).toBe("From $1,234,567.00/month");
	});

	test("a catalog with no price says nothing about money", () => {
		expect(fromPriceLabel(null)).toBeNull();
	});
});

describe("grouping", () => {
	test("a companion on this agent is Installed, not Available", () => {
		const model = deriveOverviewModel({
			catalog: [catalogItem()],
			installations: [installation()],
			search: "",
		});
		expect(model.installed).toHaveLength(1);
		expect(model.available).toHaveLength(0);
		expect(model.showPageEmpty).toBe(false);
	});

	test("a finished uninstall is not installed", () => {
		const model = deriveOverviewModel({
			catalog: [catalogItem()],
			installations: [installation({ operation: "uninstall", status: "complete" })],
			search: "",
		});
		expect(model.installed).toHaveLength(0);
		expect(model.available).toHaveLength(1);
		expect(model.showPageEmpty).toBe(true);
	});

	test("search filters both groups case-insensitively and flags a filtered-to-zero Available", () => {
		const model = deriveOverviewModel({
			catalog: [catalogItem()],
			installations: [],
			search: "  POSTIZ ",
		});
		expect(model.available).toHaveLength(1);
		const none = deriveOverviewModel({
			catalog: [catalogItem()],
			installations: [],
			search: "crm",
		});
		expect(none.available).toHaveLength(0);
		expect(none.showSearchEmpty).toBe(true);
		expect(none.showPageEmpty).toBe(false);
	});

	test("an install that has not completed has no hostname and no reachability yet", () => {
		for (const status of ["queued", "running", "failed"] as const) {
			const card = deriveOverviewModel({
				catalog: [catalogItem()],
				installations: [installation({ status, completed_at: null })],
				search: "",
			}).installed[0];
			expect(card?.hostname).toBeNull();
			expect(card?.reachability).toBeNull();
			expect(card?.installedLabel).toBeNull();
		}
		expect(hasReachedComplete({ operation: "update", status: "running" })).toBe(true);
	});

	test("tier options list all three in contract order with only offered ones selectable", () => {
		expect(tierOptions(catalogItem())).toEqual([
			{ tier: "standard", ...INSTALL_DIALOG_COPY.tierOptions.standard, offered: true },
			{ tier: "dedicated", ...INSTALL_DIALOG_COPY.tierOptions.dedicated, offered: false },
			{ tier: "shared", ...INSTALL_DIALOG_COPY.tierOptions.shared, offered: false },
		]);
	});
});

describe("load failures", () => {
	test("retry re-asks the query that failed, not the one that loaded", () => {
		const calls: string[] = [];
		const failure = blockingFailure([
			{ error: null, data: { items: [] }, refetch: () => calls.push("catalog") },
			{
				error: new ApiError(500, "boom"),
				data: undefined,
				refetch: () => calls.push("installations"),
			},
		]);
		expect(failure).not.toBeNull();
		failure?.retry();
		expect(calls).toEqual(["installations"]);
	});

	test("a failed refetch over cached data does not block the page", () => {
		expect(
			blockingFailure([
				{ error: new ApiError(500, "boom"), data: { items: [] }, refetch: () => {} },
			]),
		).toBeNull();
	});

	test("a transport failure reads as the iforge sentence; HTTP failures keep the house copy", () => {
		expect(OVERVIEW_ERROR_NORMALIZER.normalizeError(new ApiNetworkError("offline"))).toBe(
			TRANSPORT_FAILURE_COPY,
		);
		expect(OVERVIEW_ERROR_NORMALIZER.normalizeError(new ApiNetworkError("timeout"))).toBe(
			TRANSPORT_FAILURE_COPY,
		);
		expect(OVERVIEW_ERROR_NORMALIZER.normalizeError(new ApiError(500, "boom"))).not.toBe(
			TRANSPORT_FAILURE_COPY,
		);
	});
});

describe("house copy rules", () => {
	const strings = [
		...Object.values(OVERVIEW_COPY),
		...Object.values(OVERVIEW_EMPTY_COPY),
		...Object.values(OVERVIEW_SEARCH_EMPTY_COPY),
		...Object.values(COMPANION_DESCRIPTIONS),
		TRANSPORT_FAILURE_COPY,
		INSTALL_DIALOG_COPY.tierGroupLabel,
		...Object.values(INSTALL_DIALOG_COPY.tierOptions).flatMap((option) => [
			option.title,
			option.description,
		]),
		INSTALL_DIALOG_COPY.unavailableNote,
		INSTALL_DIALOG_COPY.priceLabel,
		INSTALL_DIALOG_COPY.priceBodyTemplate,
		INSTALL_DIALOG_COPY.confirm,
		INSTALL_DIALOG_COPY.rejected,
		INSTALL_DIALOG_COPY.startedToast,
		installDialogTitle("Postiz"),
		installDialogDescription("Postiz"),
		installCredentialNote("Postiz"),
	];

	test("no exclamation marks, no emoji, no 'Oops', no marketing verbs", () => {
		for (const string of strings) {
			expect(string).not.toContain("!");
			expect(string).not.toMatch(/\p{Extended_Pictographic}/u);
			expect(string).not.toMatch(/\b(oops|elevate|seamless|unleash|next-gen|delve)\b/i);
		}
	});

	test("install copy says what survives rather than 'remove' or 'delete'", () => {
		for (const string of strings) {
			expect(string).not.toMatch(/\b(remove|delete)\b/i);
		}
	});
});
