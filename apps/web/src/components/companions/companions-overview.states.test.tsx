/**
 * Held-out UI-state tests for S1 — the Companions overview. One assertion per
 * `06-UI-SPEC.md` §UI Considerations row for S1.
 *
 * **SCOPE LIMIT, same as `companion-tools.states.test.tsx`.** There is no DOM
 * library and no viewport harness here, so every assertion reads
 * `renderToStaticMarkup` output: which component rendered, which copy is
 * present, which classes and `title` attributes are present, and in which
 * ORDER elements were emitted. It does not measure pixels. The `overflow` and
 * `long-text` rows are asserted as their markup-level preconditions (the
 * truncation class is present and the full value is in a `title`), never as an
 * observed width.
 *
 * Most states render the view without the section tab strip. The strip and
 * the Installed card's whole-card link are router `Link`s, which need a
 * router; those two are rendered inside a throwaway memory router
 * (`renderInRouter`) so their real `href`s are asserted.
 */

import { describe, expect, test } from "bun:test";
import {
	createMemoryHistory,
	createRootRoute,
	createRouter,
	RouterProvider,
} from "@tanstack/react-router";
import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ENTITY_GRID_CLASS } from "@/components/entity-card";
import { ApiError, ApiNetworkError } from "@/lib/api-errors";
import { renderedText } from "@/lib/rendered-text";
import {
	CompanionsOverviewView,
	type CompanionsOverviewViewProps,
	installedCompanionLink,
} from "./companions-overview";
import {
	type CompanionCatalogItem,
	type CompanionInstallation,
	OVERVIEW_EMPTY_COPY,
	OVERVIEW_SEARCH_EMPTY_COPY,
	TRANSPORT_FAILURE_COPY,
} from "./companions-overview.logic";
import { CompanionSectionTabs } from "./tool-activity-table";

const AGENT_ID = "6c3b1a2e-0000-4000-8000-000000000001";

const DIGEST = `sha256:${"a".repeat(64)}`;

function image(name: string) {
	return { image: `ghcr.io/example/${name}`, tag: "v1", sha256: DIGEST };
}

function postiz(overrides: Partial<CompanionCatalogItem> = {}): CompanionCatalogItem {
	return {
		companion_key: "postiz",
		display_name: "Postiz",
		version: "v2.24.0",
		images: {
			app: image("postiz"),
			postgres: image("postgres"),
			redis: image("redis"),
			cloudflared: image("cloudflared"),
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
		agent_id: AGENT_ID,
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

/** A second catalog companion, so "many" and search-miss cases have something to count. */
function companion(key: string, name: string): CompanionCatalogItem {
	return postiz({ companion_key: key, display_name: name });
}

function render(props: Partial<CompanionsOverviewViewProps> = {}) {
	return renderToStaticMarkup(
		<CompanionsOverviewView catalog={[postiz()]} installations={[]} {...props} />,
	);
}

/** Static markup for a node that needs a router (Links), via a throwaway memory router. */
async function renderInRouter(node: ReactNode): Promise<string> {
	const rootRoute = createRootRoute({ component: () => node });
	const router = createRouter({
		routeTree: rootRoute,
		history: createMemoryHistory({ initialEntries: ["/"] }),
	});
	await router.load();
	return renderToStaticMarkup(<RouterProvider router={router} />);
}

/**
 * The markup of the FIRST element carrying `data-slot="name"`, from its opening
 * tag to its closing tag. The regions sliced here (`section`, `article`) never
 * nest inside an element of their own tag name, so the first matching close
 * tag is the element's own.
 */
function slot(markup: string, name: string): string {
	const attribute = markup.indexOf(`data-slot="${name}"`);
	if (attribute === -1) return "";
	const start = markup.lastIndexOf("<", attribute);
	const tag = /^<([a-z0-9]+)/.exec(markup.slice(start))?.[1];
	if (!tag) return "";
	const close = `</${tag}>`;
	const end = markup.indexOf(close, attribute);
	return markup.slice(start, end === -1 ? undefined : end + close.length);
}

/** Every `<button` and `<a` inside a region. */
function interactiveCount(region: string): number {
	return (region.match(/<(button|a)[\s>]/g) ?? []).length;
}

describe("S1 Companions overview — tracer", () => {
	test("empty: a tenant with nothing installed sees Postiz available with its price", () => {
		const markup = render();
		const text = renderedText(markup);
		expect(text).toContain("Companions");
		expect(text).toContain(
			"Applications your agent can run and drive, each on its own hostname with its own data.",
		);
		expect(text).toContain("No companions yet");
		expect(text).toContain("Available");
		expect(text).toContain("Postiz");
		expect(text).toContain("From $12.00/month");
		expect(text).toContain("Install");
		expect(text).not.toContain("Installed");
	});
});

describe("S1 navigation", () => {
	test("nav: the strip renders three links in the order Companions, Tools, Activity", async () => {
		const markup = await renderInRouter(
			<CompanionSectionTabs agentId={AGENT_ID} active="companions" />,
		);
		const links = [...markup.matchAll(/<a([^>]*)>([^<]*)<\/a>/g)].map((match) => ({
			attributes: match[1] ?? "",
			label: match[2],
		}));
		expect(links.map((link) => link.label)).toEqual(["Companions", "Tools", "Activity"]);
		expect(links[0]?.attributes).toContain(`href="/agents/${AGENT_ID}/companions/installed"`);
		expect(links[1]?.attributes).toContain(`href="/agents/${AGENT_ID}/companions"`);
		expect(links[2]?.attributes).toContain(`href="/agents/${AGENT_ID}/companions/activity"`);
		expect(links[0]?.attributes).toContain('aria-current="page"');
		expect(links[1]?.attributes).not.toContain("aria-current");
	});

	test("nav: a detail page renders the same strip with no tab active", async () => {
		const markup = await renderInRouter(<CompanionSectionTabs agentId={AGENT_ID} />);
		expect(markup).not.toContain("aria-current");
		expect(markup).not.toContain("bg-muted font-medium");
	});
});

describe("S1 empty", () => {
	test("empty: nothing installed, catalog available — the page variant offers Install companion", () => {
		const markup = render();
		const text = renderedText(markup);
		expect(text).toContain(OVERVIEW_EMPTY_COPY.title);
		expect(text).toContain(OVERVIEW_EMPTY_COPY.body);
		expect(text).toContain(OVERVIEW_EMPTY_COPY.action);
		expect(markup).toContain("min-h-[320px]");
		// The action's target: the Available group is focusable and follows the empty state.
		expect(markup).toMatch(/<section[^>]*tabindex="-1"[^>]*data-slot="companions-available"/);
		expect(markup.indexOf(OVERVIEW_EMPTY_COPY.title)).toBeLessThan(
			markup.indexOf('data-slot="companions-available"'),
		);
	});

	test("empty: a search matching nothing renders the inset variant with Clear search", () => {
		const markup = render({ search: "twenty" });
		const text = renderedText(markup);
		expect(text).toContain(OVERVIEW_SEARCH_EMPTY_COPY.title);
		expect(text).toContain(OVERVIEW_SEARCH_EMPTY_COPY.body);
		expect(text).toContain(OVERVIEW_SEARCH_EMPTY_COPY.action);
		const available = slot(markup, "companions-available");
		expect(available).toContain("bg-muted/30");
		expect(available).not.toContain('data-slot="available-companion-card"');
		expect(text).not.toContain(OVERVIEW_EMPTY_COPY.title);
	});

	test("empty: an empty Installed group is omitted, not rendered empty", () => {
		const markup = render();
		expect(markup).not.toContain('data-slot="companions-installed"');
		expect(renderedText(markup)).not.toContain("Installed");
	});

	test("empty: nothing installed and nothing available renders the page state with no action", () => {
		const text = renderedText(render({ catalog: [] }));
		expect(text).toContain(OVERVIEW_EMPTY_COPY.title);
		expect(text).not.toContain(OVERVIEW_EMPTY_COPY.action);
		expect(text).not.toContain("Available");
	});
});

describe("S1 loading", () => {
	test("loading: skeleton cards at entity-card geometry inside the same grid, never a spinner", () => {
		const markup = render({ loading: true, catalog: undefined, installations: undefined });
		const loading = slot(markup, "companions-loading");
		expect(loading).toContain(`class="${ENTITY_GRID_CLASS}"`);
		expect((loading.match(/data-slot="skeleton"/g) ?? []).length).toBeGreaterThan(0);
		expect(loading).toContain("rounded-lg border bg-card p-4");
		expect(markup).not.toContain('data-slot="spinner"');
		expect(markup).toContain('aria-busy="true"');
	});

	test("loading: the header, toolbar and grid classes do not change when data lands", () => {
		const loading = render({ loading: true, catalog: undefined, installations: undefined });
		const loaded = render();
		for (const markup of [loading, loaded]) {
			expect(markup).toContain('data-slot="page-header"');
			expect(markup).toContain("Search companions…");
			expect(markup).toContain(`class="${ENTITY_GRID_CLASS}"`);
		}
	});
});

describe("S1 error", () => {
	test("error: a failed query renders ApiErrorPanel with Retry and no cards", () => {
		const markup = render({ error: new ApiError(500, "boom"), onRetry: () => {} });
		expect(markup).toContain('data-slot="alert"');
		expect(renderedText(markup)).toContain("Retry");
		expect(markup).not.toContain('data-slot="available-companion-card"');
		expect(markup).not.toContain('data-slot="skeleton"');
	});

	test("error: a transport failure uses the iforge sentence", () => {
		const text = renderedText(render({ error: new ApiNetworkError("offline"), onRetry: () => {} }));
		expect(text).toContain(TRANSPORT_FAILURE_COPY);
		expect(text).toContain("We couldn't reach iforge. Check your connection and try again.");
	});
});

describe("S1 populated", () => {
	const catalog = [postiz(), companion("twenty", "Twenty")];
	const installations = [installation()];

	test("populated: Installed then Available, each with its count in the count slot", () => {
		const markup = render({ catalog, installations });
		const installedAt = markup.indexOf('data-slot="companions-installed"');
		const availableAt = markup.indexOf('data-slot="companions-available"');
		expect(installedAt).toBeGreaterThan(-1);
		expect(availableAt).toBeGreaterThan(installedAt);
		expect(slot(markup, "companions-installed")).toMatch(
			/Installed<\/span><span class="text-xs text-muted-foreground">1<\/span>/,
		);
		expect(slot(markup, "companions-available")).toMatch(
			/Available<\/span><span class="text-xs text-muted-foreground">1<\/span>/,
		);
		// A companion on this agent is not offered again.
		expect(slot(markup, "companions-available")).not.toContain(">Postiz<");
	});

	test("populated: an Installed card is a whole-card link with no nested interactive element", async () => {
		const markup = await renderInRouter(
			<CompanionsOverviewView
				catalog={catalog}
				installations={installations}
				installedLink={(item) => ({ to: "/agents/$id/companions", params: { id: item.agent_id } })}
			/>,
		);
		const card = slot(markup, "installed-companion-card");
		expect(interactiveCount(card)).toBe(1);
		expect(card).toMatch(/<a[^>]*class="absolute inset-0[^"]*"/);
		expect(card).toContain(`href="/agents/${AGENT_ID}/companions"`);
		expect(card).toContain("hover:bg-muted/50");
	});

	test("populated: the page links each Installed card to that companion's S3 page", async () => {
		// `installedCompanionLink` is what `CompanionsOverview` hands the view, and
		// where an accepted install in S2 navigates to.
		expect(installedCompanionLink(installation())).toEqual({
			to: "/agents/$id/companions/$companionKey",
			params: { id: AGENT_ID, companionKey: "postiz" },
		});
		const markup = await renderInRouter(
			<CompanionsOverviewView
				catalog={catalog}
				installations={installations}
				installedLink={installedCompanionLink}
			/>,
		);
		const card = slot(markup, "installed-companion-card");
		expect(interactiveCount(card)).toBe(1);
		expect(card).toContain(`href="/agents/${AGENT_ID}/companions/postiz"`);
	});

	test("populated: an Available card is not a link; its Install button is its only control", () => {
		const markup = render({ catalog, installations });
		const card = slot(markup, "available-companion-card");
		expect(card).not.toMatch(/<a[\s>]/);
		expect(interactiveCount(card)).toBe(1);
		expect(renderedText(card)).toContain("Install");
		// Accent is reserved for S2's confirm: the card's button is not the primary variant.
		expect(card).not.toContain("bg-primary text-primary-foreground");
	});

	test("populated: an installed card shows state, reachability, hostname and meta", () => {
		const markup = render({ catalog, installations });
		const card = slot(markup, "installed-companion-card");
		const text = renderedText(card);
		expect(text).toContain("Running");
		expect(text).toContain("Reachable");
		expect(text).toContain("postiz-acme.companions.iforge.dev");
		expect(text).toMatch(/Installed \S.* · Standard/);
		expect(card).toContain('title="standard"');
	});
});

describe("S1 partial", () => {
	test("partial: a card whose job has not completed shows the state chip alone", () => {
		for (const [status, label] of [
			["queued", "Queued"],
			["running", "Installing"],
			["failed", "Install failed"],
		] as const) {
			const markup = render({
				installations: [
					installation({
						status,
						completed_at: null,
						reachability: { state: "no_hostname_yet", checked_at: null },
					}),
				],
			});
			const card = slot(markup, "installed-companion-card");
			expect(renderedText(card)).toContain(label);
			expect(card).not.toContain('data-slot="companion-hostname"');
			expect(card).not.toContain("data-reachability");
			expect(renderedText(card)).not.toContain("No hostname yet");
			expect(renderedText(card)).not.toContain("postiz-acme");
			expect((card.match(/data-slot="status-badge"/g) ?? []).length).toBe(1);
		}
	});

	test("the job enum appears only in data attributes and titles, never as a label", () => {
		for (const status of ["queued", "running", "complete"] as const) {
			const markup = render({
				installations: [
					installation({
						status,
						completed_at: status === "complete" ? "2026-09-23T11:05:00Z" : null,
					}),
				],
			});
			const card = slot(markup, "installed-companion-card");
			expect(card).toContain(`data-state="${status}"`);
			expect(card).toContain(`title="${status}"`);
			expect(renderedText(card)).not.toMatch(new RegExp(`\\b${status}\\b`));
		}
		const failed = slot(
			render({ installations: [installation({ status: "failed", completed_at: null })] }),
			"installed-companion-card",
		);
		expect(failed).toContain('data-state="failed"');
		expect(renderedText(failed)).toContain("Install failed");
	});
});

describe("S1 overflow and long text", () => {
	const longName = `Postiz ${"Enterprise ".repeat(20).trim()}`;
	const longHost = `postiz-${"a".repeat(60)}.companions.iforge.dev`;

	test("overflow: card text truncates to one line with the full value in a title", () => {
		const markup = render({
			catalog: [postiz({ display_name: longName })],
			installations: [],
		});
		const card = slot(markup, "available-companion-card");
		expect(card).toMatch(new RegExp(`class="[^"]*truncate[^"]*" title="${longName}"`));
		expect(card).toMatch(/class="[^"]*truncate[^"]*" title="Schedule and publish/);
		expect(card).toMatch(
			/class="[^"]*truncate[^"]*tabular-nums[^"]*" title="From \$12\.00\/month"/,
		);
	});

	test("overflow: the grid wraps and the page scrolls — no inner scroll region", () => {
		const markup = render({
			catalog: [postiz(), companion("twenty", "Twenty"), companion("vibe", "Vibe-Trading")],
			installations: [installation()],
		});
		expect(markup).toContain(`class="${ENTITY_GRID_CLASS}"`);
		expect(markup).not.toMatch(/overflow-(y-)?(auto|scroll)/);
		expect(markup).not.toMatch(/max-h-/);
	});

	test("long-text: the hostname stays font-mono text-xs, truncated, never wrapped mid-host", () => {
		const markup = render({ installations: [installation({ hostname: longHost })] });
		const host = /<p class="([^"]*)" title="([^"]*)" data-slot="companion-hostname">/.exec(
			slot(markup, "installed-companion-card"),
		);
		expect(host?.[2]).toBe(longHost);
		expect(host?.[1]).toContain("truncate");
		expect(host?.[1]).toContain("font-mono text-xs");
		expect(host?.[1]).not.toMatch(/break-all|break-words|whitespace-normal|wrap-anywhere/);
	});

	test("long-text: a long installed name truncates with the full name in its title", () => {
		const markup = render({
			catalog: [postiz({ display_name: longName })],
			installations: [installation()],
		});
		expect(slot(markup, "installed-companion-card")).toMatch(
			new RegExp(`class="[^"]*truncate[^"]*" title="${longName}"`),
		);
	});
});

describe("S1 zero-one-many", () => {
	function installedCountSlot(markup: string): string | undefined {
		return /Installed<\/span><span class="text-xs text-muted-foreground">([^<]*)<\/span>/.exec(
			markup,
		)?.[1];
	}

	function installedOn(keys: readonly string[]) {
		return keys.map((key, index) =>
			installation({ id: `0b7e2d1c-0000-4000-8000-0000000000${10 + index}`, companion_key: key }),
		);
	}

	test("zero-one-many: the count slot is a bare integer, with no singular or plural copy", () => {
		const keys = ["postiz", "twenty", "vibe"] as const;
		const catalog = keys.map((key) => companion(key, key));
		const one = render({ catalog, installations: installedOn(["postiz"]) });
		const many = render({ catalog, installations: installedOn(keys) });
		expect(installedCountSlot(one)).toBe("1");
		expect(installedCountSlot(many)).toBe("3");
		// The group header reads the label and the integer, and nothing else.
		const header = (markup: string) => {
			const section = slot(markup, "companions-installed");
			const grid = section.lastIndexOf("<", section.indexOf(`class="${ENTITY_GRID_CLASS}"`));
			return renderedText(section.slice(0, grid));
		};
		expect(header(one)).toBe("Installed 1");
		expect(header(many)).toBe("Installed 3");
	});

	test("zero-one-many: many cards wrap in the same grid with no count-dependent layout", () => {
		const keys = ["postiz", "twenty", "vibe", "crm", "notes"] as const;
		const catalog = keys.map((key) => companion(key, key));
		const one = slot(
			render({ catalog, installations: installedOn(["postiz"]) }),
			"companions-installed",
		);
		const many = slot(
			render({ catalog, installations: installedOn(keys) }),
			"companions-installed",
		);
		const sectionClass = (region: string) => /^[^>]*/.exec(region)?.[0];
		expect(one).toContain(`class="${ENTITY_GRID_CLASS}"`);
		expect(many).toContain(`class="${ENTITY_GRID_CLASS}"`);
		expect(sectionClass(one)).toBe(sectionClass(many));
		expect((many.match(/data-slot="installed-companion-card"/g) ?? []).length).toBe(5);
	});
});
