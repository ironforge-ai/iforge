/**
 * The destination authority (P33-15).
 *
 * One record holds the family of places this fork sends a user, a browser or a shipped
 * binary: the apex domain, the two production API origins, the web origin, the assets
 * base, the support contact, the docs and GitHub destinations, and the Telegram slot.
 * `apps/web` and `packages/cli` both read it, so a move edits one place instead of
 * three unrelated mechanisms.
 *
 * **Written out verbatim, not read from a file at runtime**, for two reasons:
 *
 *  1. It is bundled into a native binary that has no repository beside it — the same
 *     reason `packages/cli/src/lib/brand-env.ts` states for its own explicit record.
 *  2. It must be importable by `apps/web`, which is why it lives in `packages/shared`
 *     and not in the CLI. The package stays `private: true` and its package name does
 *     NOT move (03.1 plan 04's verified prohibition).
 *
 * **This module holds defaults and non-environment destinations ONLY.** It must never
 * name, re-export or wrap any of the four build-time bundler keys behind a helper: the
 * bundler substitutes those statically at their read sites, so the literal read form is
 * load-bearing and is already asserted by `apps/web/src/hosted/oss-clean.test.ts`. A
 * helper here would break the substitution and that assertion together — which is why
 * this file does not spell any of those key names at all, not even in a comment.
 *
 * `destinations.test.ts` asserts this record against `config/deploy.yml`, the CLI
 * bundler define flags, the native build script and the CI workflow source URL — none
 * of which can import a TypeScript module, and none of which any CI job executes.
 */
export const DESTINATIONS = Object.freeze({
	/** The apex domain this fork owns, hosted on Cloudflare (P33-12). */
	apexDomain: "iforge.dev",

	/** The apex homepage — the sidebar logo link and the 404 page's "go home" button. */
	webOrigin: "https://iforge.dev",

	/** The `www` alias of the apex. Distinct from `webOrigin` only as a CORS member. */
	wwwOrigin: "https://www.iforge.dev",

	/**
	 * The signed-in dashboard — `config/deploy.yml`'s `WEB_ORIGIN`, from which the CLI
	 * device-flow `verification_uri` is built. Deliberately NOT `webOrigin`: that is the
	 * apex marketing homepage, and sending a device-flow user there would send them to
	 * the wrong host.
	 */
	dashboardOrigin: "https://cloud.iforge.dev",

	/**
	 * The production cloud API origin (P33-13). This is where a shipped CLI sends a
	 * user's bearer token, so it must name a host this fork controls.
	 *
	 * This and `deployApiOrigin` are TWO distinct origins, not one value spelled twice.
	 * Neither is derivable from the other.
	 */
	cloudApiOrigin: "https://cloud-api.iforge.dev",

	/**
	 * The production deploy API origin (P33-24) — the publisher of the `/v2` OpenAPI
	 * document and the default deploy target compiled into every build. It was unnamed
	 * by every artifact until P33-24 settled it, so it is written out explicitly and is
	 * never inferred from `cloudApiOrigin` or from the apex.
	 */
	deployApiOrigin: "https://api.iforge.dev",

	/** The static asset origin (P33-22). */
	assetsOrigin: "https://assets.iforge.dev",

	/** The channel app-icon base (P33-22), formerly a live dependency on upstream's CDN. */
	iconBase: "https://assets.iforge.dev/icons",

	/** The rendered support contact (P33-14, WINDOWS #70). */
	supportEmail: "support@iforge.dev",

	/**
	 * The GitHub organisation and the npm scope are DELIBERATELY INDEPENDENT (03.1 D-04).
	 * Neither is derived from the other, and neither may ever be reconstructed from the
	 * other: the npm scope is `@iforge` while the GitHub organisation is `ironforge-ai`,
	 * and a derivation would silently produce a destination nobody owns.
	 */
	githubOrg: "ironforge-ai",
	npmScope: "@iforge",

	/** The Help-menu Docs destination (P33-14, WINDOWS #34). */
	docsUrl: "https://deepwiki.com/ironforge-ai/iforge",

	/** The Help-menu GitHub destination (P33-14, WINDOWS #34). */
	githubUrl: "https://github.com/ironforge-ai/iforge",

	/**
	 * The one destination this fork deliberately does not have (P33-23, plan 03.3-07).
	 *
	 * The operator was asked what Telegram destination this fork owns. The answer was that
	 * there is none, and there will not be one for now. The Help-menu Telegram item was
	 * therefore REMOVED rather than repointed — reversing that plan's repoint-over-removal
	 * default for this one destination, deliberately: a link to a community that does not
	 * exist is worse than no link at all, because it is a dead affordance the user cannot
	 * fix by clicking.
	 *
	 * The slot stays here as an explicit `null` rather than being deleted, because an
	 * omitted key is indistinguishable from an oversight, and this is a decision.
	 *
	 * Fully reversible. Creating a Telegram presence later means setting this constant and
	 * restoring one `DropdownMenuItem` in `apps/web/src/components/app-sidebar.tsx` —
	 * `apps/web/src/lib/web-destinations-contract.test.ts` pins both halves of the current
	 * state, so a half-restore fails rather than shipping.
	 *
	 * Unrelated to the product's Telegram *integration*, where a per-account bot token is
	 * stored encrypted in the database. This is a public community link compiled into the
	 * shipped web client; it is not a credential and shares no mechanism with one.
	 */
	telegramUrl: null as string | null,

	/**
	 * The CORS origin family the backend allows, in the order `config/deploy.yml` lists
	 * it. Every member moves together or not at all: a partial move leaves an upstream
	 * origin allowed to call this fork's API (T-03.3-25).
	 */
	corsOrigins: [
		"https://www.iforge.dev",
		"https://iforge.dev",
		"https://api.iforge.dev",
		"https://cloud.iforge.dev",
	] as readonly string[],

	/**
	 * Hosts outside the apex family that members are allowed to name. Declaring them
	 * makes "no member names a host outside the domain family" a falsifiable claim: a
	 * new member pointing at an undeclared host fails the contract test.
	 */
	externalHosts: ["deepwiki.com", "github.com"] as readonly string[],
});
