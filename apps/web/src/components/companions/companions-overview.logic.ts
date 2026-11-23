import type { components } from "@clawdi/shared/api";
import type { ApiErrorNormalizer } from "@/components/api-error-panel";
import type { StatusTone } from "@/components/ui/status-badge";
import { isApiAuthError, isApiNetworkError, normalizeApiError } from "@/lib/api-errors";
import { shouldBlockQueryError } from "@/lib/query-state";
import { formatUsdExact } from "@/lib/usd-exact";
import { relativeTime } from "@/lib/utils";

/**
 * S1 — the Companions overview — as data rather than as markup.
 *
 * Every customer-facing string on S1 lives here, verbatim from
 * `06-UI-SPEC.md` §Copywriting Contract, so `companions-overview.logic.test.ts`
 * can hold each one against its contract row. Every rule that decides what a
 * card shows (which group it lands in, which chip it wears, whether a hostname
 * exists yet) is a pure function here, so the rules are testable without
 * rendering a card.
 */

type Schemas = components["schemas"];

export type CompanionCatalogItem = Schemas["CompanionCatalogItemResponse"];
export type CompanionInstallation = Schemas["CompanionInstallationResponse"];
export type IsolationTier = CompanionInstallation["isolation_tier"];
export type InstallationStatus = CompanionInstallation["status"];
export type InstallationOperation = CompanionInstallation["operation"];
export type ReachabilityState = CompanionInstallation["reachability"]["state"];

/** openapi-react-query cache keys for S1's two reads. */
export const COMPANION_CATALOG_QUERY_KEY = ["get", "/v1/companions/catalog"] as const;
export const COMPANION_INSTALLATIONS_QUERY_KEY = ["get", "/v1/companions/installations"] as const;

export const OVERVIEW_COPY = {
	/** Page title, Title Case. Also the tab label and the route head title. */
	title: "Companions",
	description:
		"Applications your agent can run and drive, each on its own hostname with its own data.",
	installedGroup: "Installed",
	availableGroup: "Available",
	searchPlaceholder: "Search companions…",
	searchLabel: "Search companions",
	/** The Available card's trailing button — it opens S2, it installs nothing. */
	installButton: "Install",
} as const;

/** S1, nothing installed and the catalog available: `EmptyState variant="page"`. */
export const OVERVIEW_EMPTY_COPY = {
	title: "No companions yet",
	body: "A companion is an application your agent runs and drives — on its own hostname, with its own data and its own bill.",
	/** The phase's primary CTA. Here it focuses the Available group; it installs nothing. */
	action: "Install companion",
} as const;

/** S1, the Available group filtered to zero: `EmptyState variant="inset"`. */
export const OVERVIEW_SEARCH_EMPTY_COPY = {
	title: "No companions match this search",
	body: "Clear the search to see everything available.",
	action: "Clear search",
} as const;

/** §Error states — "Transport failure (no HTTP response at all)". */
export const TRANSPORT_FAILURE_COPY =
	"We couldn't reach iforge. Check your connection and try again.";

/**
 * The one-line OUTCOME description on an Available card, keyed by catalog key.
 *
 * The catalog API carries no description (it describes images and tiers), and
 * this copy is authored in the UI contract rather than served, so it lives
 * here. A companion without an authored line renders no description rather
 * than a mechanism sentence invented at build time.
 */
export const COMPANION_DESCRIPTIONS: Readonly<Record<string, string>> = {
	postiz: "Schedule and publish social posts, and let your agent draft and queue them.",
};

/** Sentence-case tier names. The wire value travels in `title`, never as text. */
export const ISOLATION_TIER_LABELS: Readonly<Record<IsolationTier, string>> = {
	standard: "Standard",
	dedicated: "Dedicated",
	shared: "Shared",
};

/** The three companion tabs, in the fixed order `06-UI-SPEC.md` §Navigation sets. */
export const COMPANION_SECTION_TABS = [
	{ key: "companions", label: "Companions" },
	{ key: "tools", label: "Tools" },
	{ key: "activity", label: "Activity" },
] as const;

export type CompanionSectionTab = (typeof COMPANION_SECTION_TABS)[number]["key"];

// ---------------------------------------------------------------------------
// Money
// ---------------------------------------------------------------------------

const MICROS_PER_DOLLAR = 1_000_000;

/**
 * Integer micro-dollars → the exact decimal-dollar string `formatUsdExact`
 * takes. Integer arithmetic only: the API sends micros precisely so that no
 * price is ever a float, and dividing by a million would reintroduce one.
 */
export function usdMicrosToDecimal(micros: number): string {
	const whole = Math.trunc(micros);
	const sign = whole < 0 ? "-" : "";
	const magnitude = Math.abs(whole);
	const dollars = Math.floor(magnitude / MICROS_PER_DOLLAR);
	const fraction = String(magnitude % MICROS_PER_DOLLAR).padStart(6, "0");
	return `${sign}${dollars}.${fraction}`;
}

/** Micros → "$12.00" through the one sanctioned money formatter. */
export function formatUsdMicros(micros: number): string {
	return formatUsdExact(usdMicrosToDecimal(micros));
}

/**
 * The Available card's meta row, "From {price}/month", or null when the
 * catalog carries no price. A price is never invented: with /v2 unconfigured
 * or unreachable the catalog says null, and the card says nothing about money.
 */
export function fromPriceLabel(micros: number | null | undefined): string | null {
	if (micros === null || micros === undefined || !Number.isFinite(micros)) return null;
	return `From ${formatUsdMicros(micros)}/month`;
}

// ---------------------------------------------------------------------------
// Chips
// ---------------------------------------------------------------------------

export interface ChipModel {
	tone: StatusTone;
	label: string;
}

const STATE_TONES: Readonly<Record<InstallationStatus, StatusTone>> = {
	queued: "neutral",
	running: "info",
	complete: "success",
	failed: "destructive",
};

/**
 * Sentence-case chip labels per operation (§Status token mapping). The enum is
 * the same four states for every job; only the label changes. `complete` reads
 * "Running" because the user's word for a finished job is the companion's
 * steady state, not the job's.
 *
 * An export never stops the companion, so every export state reads as the
 * steady state; the export's own progress belongs to Settings → Data.
 */
const STATE_LABELS: Readonly<Record<InstallationOperation, Record<InstallationStatus, string>>> = {
	install: {
		queued: "Queued",
		running: "Installing",
		complete: "Running",
		failed: "Install failed",
	},
	update: { queued: "Queued", running: "Updating", complete: "Running", failed: "Update failed" },
	uninstall: {
		queued: "Queued",
		running: "Removing",
		complete: "Removing",
		failed: "Removal failed",
	},
	export: { queued: "Running", running: "Running", complete: "Running", failed: "Running" },
};

export function installationStateChip(
	installation: Pick<CompanionInstallation, "operation" | "status">,
): ChipModel {
	if (installation.operation === "export") {
		return { tone: "success", label: STATE_LABELS.export[installation.status] };
	}
	return {
		tone: STATE_TONES[installation.status],
		label: STATE_LABELS[installation.operation][installation.status],
	};
}

const REACHABILITY_CHIPS: Readonly<Record<ReachabilityState, ChipModel>> = {
	reachable: { tone: "success", label: "Reachable" },
	tls_pending: { tone: "warning", label: "TLS pending" },
	not_answering: { tone: "warning", label: "Not answering" },
	no_hostname_yet: { tone: "neutral", label: "No hostname yet" },
};

export function reachabilityChip(state: ReachabilityState): ChipModel {
	return REACHABILITY_CHIPS[state];
}

/**
 * Has this companion ever reached a running install? Only then do a hostname
 * row and a reachability chip exist to render (S1 `partial`). Any operation
 * other than `install` runs against an installation that already completed.
 */
export function hasReachedComplete(
	installation: Pick<CompanionInstallation, "operation" | "status">,
): boolean {
	return installation.operation !== "install" || installation.status === "complete";
}

// ---------------------------------------------------------------------------
// Grouping
// ---------------------------------------------------------------------------

export interface InstalledCardModel {
	installation: CompanionInstallation;
	name: string;
	stateChip: ChipModel;
	/** Null while the install has not completed — the row is omitted, not blank. */
	hostname: string | null;
	/** Null while the install has not completed — the chip is omitted, not blank. */
	reachability: ChipModel | null;
	tierLabel: string;
	/** "Installed {relative}" once complete, else null. */
	installedLabel: string | null;
	/** The absolute instant behind `installedLabel`, for its `title`. */
	installedAt: string | null;
}

export interface AvailableCardModel {
	item: CompanionCatalogItem;
	name: string;
	description: string | null;
	priceLabel: string | null;
	/** False when the catalog offers no tier at all: nothing could be quoted. */
	installable: boolean;
}

export interface OverviewModel {
	installed: InstalledCardModel[];
	available: AvailableCardModel[];
	/** The Available set before search, so a filtered-to-zero group is told apart from an empty one. */
	availableTotal: number;
	/** Nothing installed, something available, no search: the page empty state. */
	showPageEmpty: boolean;
	/** The catalog is empty too: the page empty state renders without an action. */
	catalogEmpty: boolean;
	/** The Available group had cards and the search filtered every one away. */
	showSearchEmpty: boolean;
}

function matchesSearch(needle: string, ...haystack: (string | null | undefined)[]): boolean {
	if (needle === "") return true;
	return haystack.some((value) => (value ?? "").toLowerCase().includes(needle));
}

/** An uninstall that finished is not an installed companion. */
function isOnAgent(installation: CompanionInstallation): boolean {
	return !(installation.operation === "uninstall" && installation.status === "complete");
}

export function installedCardModel(
	installation: CompanionInstallation,
	name: string,
): InstalledCardModel {
	const complete = hasReachedComplete(installation);
	const installedAt = complete ? (installation.completed_at ?? installation.created_at) : null;
	return {
		installation,
		name,
		stateChip: installationStateChip(installation),
		hostname: complete && installation.hostname ? installation.hostname : null,
		reachability: complete ? reachabilityChip(installation.reachability.state) : null,
		tierLabel: ISOLATION_TIER_LABELS[installation.isolation_tier],
		installedLabel: installedAt ? `Installed ${relativeTime(installedAt)}` : null,
		installedAt,
	};
}

export function availableCardModel(item: CompanionCatalogItem): AvailableCardModel {
	return {
		item,
		name: item.display_name,
		description: COMPANION_DESCRIPTIONS[item.companion_key] ?? null,
		priceLabel: fromPriceLabel(item.from_monthly_price_usd_micros),
		installable: item.tiers.some((tier) => tier.offered),
	};
}

/**
 * Decide both groups from the two queries and the search term.
 *
 * A companion already on this agent is Installed, not Available: installing it
 * twice on one agent is not an action the page should offer.
 */
export function deriveOverviewModel({
	catalog,
	installations,
	search,
}: {
	catalog: readonly CompanionCatalogItem[];
	installations: readonly CompanionInstallation[];
	search: string;
}): OverviewModel {
	const needle = search.trim().toLowerCase();
	const names = new Map(catalog.map((item) => [item.companion_key, item.display_name]));
	const live = installations.filter(isOnAgent);
	const installedKeys = new Set(live.map((installation) => installation.companion_key));

	const installed = live
		.map((installation) =>
			installedCardModel(
				installation,
				names.get(installation.companion_key) ?? installation.companion_key,
			),
		)
		.filter((card) =>
			matchesSearch(needle, card.name, card.installation.companion_key, card.hostname),
		);

	const availableAll = catalog
		.filter((item) => !installedKeys.has(item.companion_key))
		.map(availableCardModel);
	const available = availableAll.filter((card) =>
		matchesSearch(needle, card.name, card.item.companion_key, card.description),
	);

	return {
		installed,
		available,
		availableTotal: availableAll.length,
		showPageEmpty: live.length === 0 && needle === "",
		catalogEmpty: catalog.length === 0,
		showSearchEmpty: needle !== "" && availableAll.length > 0 && available.length === 0,
	};
}

// ---------------------------------------------------------------------------
// Load failures
// ---------------------------------------------------------------------------

export interface OverviewQuery {
	error: unknown;
	data: unknown;
	refetch: () => unknown;
}

/**
 * The first query whose failure blocks the page, and the retry that re-asks
 * THAT query. Retrying a query that did not fail would leave the panel up.
 */
export function blockingFailure(
	queries: readonly OverviewQuery[],
): { error: unknown; retry: () => void } | null {
	for (const query of queries) {
		if (shouldBlockQueryError(query.error, query.data)) {
			return {
				error: query.error,
				retry: () => {
					void query.refetch();
				},
			};
		}
	}
	return null;
}

/**
 * `ApiErrorPanel`'s normalizer for S1: a request that produced no HTTP
 * response at all reads as the contract's transport sentence, naming iforge;
 * everything else keeps the house normalization.
 */
export const OVERVIEW_ERROR_NORMALIZER: ApiErrorNormalizer = {
	isAuthError: isApiAuthError,
	normalizeError: (error) =>
		isApiNetworkError(error) ? TRANSPORT_FAILURE_COPY : normalizeApiError(error),
};

// ---------------------------------------------------------------------------
// S2 — the install dialog
// ---------------------------------------------------------------------------

export type CompanionQuote = Schemas["CompanionQuoteResponse"];
export type CompanionInstallRequest = Schemas["CompanionInstallRequest"];

/** The tier order the contract lists, independent of the order the catalog sends. */
export const ISOLATION_TIERS: readonly IsolationTier[] = ["standard", "dedicated", "shared"];

/** Placeholder the price body's template carries in place of the amount. */
export const PRICE_PLACEHOLDER = "{price}";

export const INSTALL_DIALOG_COPY = {
	tierGroupLabel: "Isolation",
	/** 06.1-UI-SPEC §T2: each tier is an `EntityChoiceCard` title and description. */
	tierOptions: {
		standard: { title: "Standard", description: "Your own database, shared queue and cache." },
		dedicated: { title: "Dedicated", description: "Everything yours alone." },
		shared: { title: "Shared", description: "Everything shared, scoped to you." },
	} satisfies Record<IsolationTier, { title: string; description: string }>,
	/** The neutral chip on a not-offered tier card. A chip carries no period. */
	unavailableNote: "Not available yet",
	priceLabel: "You will be charged",
	/** Rendered with the amount in `tabular-nums` where `{price}` stands. */
	priceBodyTemplate:
		"{price} per month, billed as its own line on your invoice. Billing starts when the install finishes and stops when you uninstall.",
	confirm: "Install companion",
	cancel: "Cancel",
	/** §Error states — "Install could not be started". Inline at the action, never a toast. */
	rejected: "The install was not started. Nothing was provisioned and you have not been charged.",
	/** §Toasts — success only. */
	startedToast: "Install started",
} as const;

/** S2 dialog title — "Install Postiz" for the one companion this phase ships. */
export function installDialogTitle(name: string): string {
	return `Install ${name}`;
}

/** S2 dialog description, naming the companion being installed. */
export function installDialogDescription(name: string): string {
	return `${name} runs on its own hostname with its own database. Pick how isolated you want it — that choice sets the price and cannot be changed after install.`;
}

/** S2 credential note. Fixed copy: no field or control on S2 renders a credential. */
export function installCredentialNote(name: string): string {
	return `${name}'s admin credential is generated for you and stored in your vault. It is never shown here and never sent to your browser.`;
}

/** The price body with the formatted amount in place of `{price}`. */
export function installPriceBody(price: string): string {
	return INSTALL_DIALOG_COPY.priceBodyTemplate.replace(PRICE_PLACEHOLDER, price);
}

export interface TierOption {
	tier: IsolationTier;
	title: string;
	description: string;
	offered: boolean;
}

/**
 * All three tiers in contract order, each marked offered or not. A tier the
 * catalog omits is shown as not offered: the axis exists even when a build
 * offers one point on it.
 */
export function tierOptions(item: Pick<CompanionCatalogItem, "tiers">): TierOption[] {
	const offered = new Set(item.tiers.filter((tier) => tier.offered).map((tier) => tier.tier));
	return ISOLATION_TIERS.map((tier) => ({
		tier,
		...INSTALL_DIALOG_COPY.tierOptions[tier],
		offered: offered.has(tier),
	}));
}

/** The tier preselected on open: the first offered one, so S2 never opens with nothing chosen. */
export function defaultTier(item: Pick<CompanionCatalogItem, "tiers">): IsolationTier {
	return tierOptions(item).find((option) => option.offered)?.tier ?? "standard";
}

export type QuoteState =
	| { status: "loading" }
	| { status: "ready"; quote: CompanionQuote }
	| { status: "error"; error: unknown };

/**
 * What the price well may show for the selected tier.
 *
 * A quote only counts as READY when it is for the companion and tier on screen
 * and has not expired. A quote left over from another tier, or one past its
 * `expires_at`, is not a price the tenant may confirm against — it reads as
 * loading until the fresh quote lands, which keeps confirm disabled.
 */
export function deriveQuoteState({
	data,
	error,
	companionKey,
	tier,
	now = Date.now(),
}: {
	data: CompanionQuote | undefined;
	error: unknown;
	companionKey: string;
	tier: IsolationTier;
	now?: number;
}): QuoteState {
	const current =
		data !== undefined &&
		data.companion_key === companionKey &&
		data.isolation_tier === tier &&
		Date.parse(data.expires_at) > now;
	if (current) return { status: "ready", quote: data };
	// No usable quote for this tier; a failed request is the reason, not a wait.
	if (error !== null && error !== undefined) return { status: "error", error };
	return { status: "loading" };
}

/** Confirm is gated on the price being on screen — never on field completion. */
export function canConfirmInstall(state: QuoteState, pending: boolean): boolean {
	return state.status === "ready" && !pending;
}

/**
 * The install request for the quote the tenant is looking at. Companion and
 * tier are read FROM the quote, so the install cannot name a tier or a price
 * other than the one rendered.
 */
export function installRequestBody(
	agentId: string,
	quote: CompanionQuote,
): CompanionInstallRequest {
	return {
		agent_id: agentId,
		companion_key: quote.companion_key,
		isolation_tier: quote.isolation_tier,
		quote_id: quote.quote_id,
	};
}

export type InstallOutcome =
	| { ok: true; installation: CompanionInstallation }
	| { ok: false; message: string };

/**
 * Confirm S2. Nothing is requested unless a current quote is on screen; a
 * rejected request resolves to the contract's inline sentence and calls
 * nothing else, so a failure can never reach a toast. `onAccepted` runs only
 * for an accepted job.
 */
export async function runInstall({
	agentId,
	state,
	requestInstall,
	onAccepted,
}: {
	agentId: string;
	state: QuoteState;
	requestInstall: (body: CompanionInstallRequest) => Promise<CompanionInstallation>;
	onAccepted: (installation: CompanionInstallation) => unknown;
}): Promise<InstallOutcome> {
	if (state.status !== "ready") return { ok: false, message: INSTALL_DIALOG_COPY.rejected };
	let installation: CompanionInstallation;
	try {
		installation = await requestInstall(installRequestBody(agentId, state.quote));
	} catch {
		return { ok: false, message: INSTALL_DIALOG_COPY.rejected };
	}
	await onAccepted(installation);
	return { ok: true, installation };
}
