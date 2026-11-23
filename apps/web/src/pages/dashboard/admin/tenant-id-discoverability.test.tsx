/**
 * THE OPERATOR CAN OBTAIN A TENANT ID FROM THE CONSOLE.
 *
 * This file's subject is the OPERATOR, not a component. Every other admin test
 * asserts a component's inputs, which is how a capability reachable only
 * through `psql` survived nineteen plans of green tests (G-03-62, test_gap 3).
 * The contract here, in one paragraph: an identifier may enter a step ONLY by
 * being extracted from the previous step's rendered text; a step that consults
 * a tenant directory does so through `lookupUsers`, never by hand-picking
 * rows; and an assertion that reads a value out of a fixture and then finds it
 * in markup proves nothing at all about discoverability. If a future change
 * makes a journey below convenient to "fix" by handing a step a value from a
 * fixture, that change is the defect, not the test.
 *
 * THE ONE OUT-OF-BAND HOP. The console has no invite table any more (06.3,
 * D-01), so nothing in it names a tenant to an operator who holds nothing. Each
 * journey therefore starts from exactly one identifier the operator brings
 * from outside the console, declared as a literal and never read off a
 * fixture: for an email-less tenant, the Clerk user id copied from the Clerk
 * Dashboard (06.3 RESEARCH Pitfall 8); for a tenant with an email, a fragment
 * of the address the customer wrote in from. Everything after that first hop
 * is extracted from rendered text.
 *
 * `TenantPickerView` is purely presentational: it renders whatever `results`
 * it is handed and never filters them by `term`. So a journey that
 * hand-authors `results` and passes an extracted value as `term` is
 * decorative — the picker would render the same tenant for any term at all,
 * and the identifier→id hop would be enforced by nothing but the test author's
 * good intentions. That is this gap's own defect, one level up, installed in
 * the file a future reader consults to learn what "discoverable" means. Hence
 * the lookup mirror below: every picker render in this file is fed
 * `lookupUsers(extractedTerm, UNIVERSE)` and nothing else, so the hop is
 * COMPUTED from the term rather than asserted about it.
 */

import { describe, expect, test } from "bun:test";
import { renderToStaticMarkup } from "react-dom/server";
import type { AdminUserListItem } from "@/lib/admin-client";
import { renderedText } from "@/lib/rendered-text";
import { TENANT_SEARCH_LABEL, TenantPickerView } from "@/pages/dashboard/admin/export-section";

const noop = () => {};

// ---------------------------------------------------------------------------
// Instrument 1 — the lookup mirror.
// ---------------------------------------------------------------------------

/**
 * A local, network-free mirror of `admin_lookup_users`' three predicates
 * (`backend/app/routes/admin.py`, the `predicates` list): `email` non-null and
 * containing `term` case-insensitively, `clerk_id` equal to `term` exactly, or
 * `id` equal to `term` exactly. A row matching ANY predicate is returned.
 *
 * DELIBERATELY NOT the `lookupUsers` exported by `@/lib/admin-client` — that
 * one performs a fetch, and importing it here would be a different test
 * entirely.
 *
 * Two things this mirror is NOT:
 *
 * - It is not a gate against the server. The per-predicate pins below prove
 *   the mirror behaves as specified; they do not compare it to `admin.py`, so
 *   a server-side predicate change leaves them green. The real server-side
 *   gate for this capability is plan 03-20's API-level chain test,
 *   `test_operator_holding_nothing_obtains_a_tenant_id_and_resolves_the_tenant`.
 * - It is not byte-parity with the server's SQL: the server escapes `%`, `_`
 *   and `\` before `ilike`, and this mirror does not. Irrelevant to the inputs
 *   these journeys use — recorded so the fidelity gap is not mistaken for
 *   parity by a later reader.
 */
function lookupUsers(term: string, universe: AdminUserListItem[]): AdminUserListItem[] {
	// VACUITY GUARD: a naive `includes("")` matches every row, which would make
	// the empty-term journey step below pass for the wrong reason. The server
	// never sees an empty term (`min_length=1` on the query parameter) and
	// neither does this mirror.
	if (term.length === 0) return [];
	const needle = term.toLowerCase();
	return universe.filter(
		(user) =>
			(user.email?.toLowerCase().includes(needle) ?? false) ||
			user.clerk_id === term ||
			user.id === term,
	);
}

// ---------------------------------------------------------------------------
// Instrument 2 — identifier extraction from rendered text.
// ---------------------------------------------------------------------------

const UUID_PATTERN = /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi;
const EMAIL_PATTERN = /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g;

interface Identifiers {
	uuids: string[];
	emails: string[];
}

/**
 * The uuid-shaped and email-shaped tokens an operator could copy out of a
 * rendered surface. Deduplicated: an identifier rendered twice is still one
 * identifier the operator holds.
 *
 * Pinned directly below in both directions, because an over-matching regex
 * (version numbers, timestamps, truncation ellipses) would turn every journey
 * assertion into a coin flip.
 */
function identifiersIn(text: string): Identifiers {
	return {
		uuids: [...new Set(text.match(UUID_PATTERN) ?? [])],
		emails: [...new Set(text.match(EMAIL_PATTERN) ?? [])],
	};
}

// ---------------------------------------------------------------------------
// The tenants that exist. One universe, shared by both journeys.
// ---------------------------------------------------------------------------

/** What the Clerk Dashboard shows the operator for the email-less tenant. A
 * literal, deliberately NOT `TENANT_NO_EMAIL.clerk_id`: it is the one value
 * that enters from outside the console, so it must not be read off a fixture. */
const CLERK_DASHBOARD_USER_ID = "user_2fNorthwindSubject";

/** A Clerk-backed tenant with no email on file — the population G-03-62 names
 * as unreachable. Its email-less state is why the Clerk user id, not an email
 * fragment, is its first hop. */
const TENANT_NO_EMAIL: AdminUserListItem = {
	id: "9c3b1f42-7e0a-4d18-9a6b-51cf20d8e3a7",
	email: null,
	clerk_id: "user_2fNorthwindSubject",
	name: "Northwind Partner",
	created_at: "2026-08-01T09:00:00Z",
	billing_exempt: false,
};

/** A tenant that has an email: reached by a fragment of that address. */
const TENANT_WITH_EMAIL: AdminUserListItem = {
	id: "4a7d5e19-2c86-4b03-8f51-6ed9c704b2aa",
	email: "dana@example.com",
	clerk_id: "user_2fDanaSubject",
	name: null,
	created_at: "2026-08-02T09:00:00Z",
	billing_exempt: false,
};

/** A third tenant nothing in either journey should ever match. */
const TENANT_UNRELATED: AdminUserListItem = {
	id: "1b2c3d4e-5f60-4718-8293-a4b5c6d7e8f9",
	email: "someone-else@example.net",
	clerk_id: "user_2fSomeoneElse",
	name: null,
	created_at: "2026-08-03T09:00:00Z",
	billing_exempt: false,
};

const UNIVERSE: AdminUserListItem[] = [TENANT_NO_EMAIL, TENANT_WITH_EMAIL, TENANT_UNRELATED];

/** A uuid no row in the universe carries. */
const UNMATCHED_UUID = "3f1b9c7e-0000-4000-8000-2d5a6b7c8d9e";

// ---------------------------------------------------------------------------

/** The picker, fed ONLY a term and whatever the lookup mirror returns for it. */
function renderPicker(term: string, results: AdminUserListItem[]): string {
	return renderToStaticMarkup(
		<TenantPickerView
			selected={null}
			onSelect={noop}
			searchLabel={TENANT_SEARCH_LABEL}
			term={term}
			onTermChange={noop}
			results={results}
			isLoading={false}
			error={null}
			onRetry={noop}
		/>,
	);
}

// ---------------------------------------------------------------------------

describe("the lookup mirror behaves as specified — without these it is a costlier hand-author", () => {
	test("PREDICATE 1: an email fragment matches, case-insensitively", () => {
		expect(lookupUsers("dana@", UNIVERSE)).toEqual([TENANT_WITH_EMAIL]);
		expect(lookupUsers("DANA@EXAMPLE", UNIVERSE)).toEqual([TENANT_WITH_EMAIL]);
	});

	test("PREDICATE 2: a clerk id matches EXACTLY, never as a prefix", () => {
		expect(lookupUsers("user_2fDanaSubject", UNIVERSE)).toEqual([TENANT_WITH_EMAIL]);
		expect(lookupUsers("user_2fDana", UNIVERSE)).toEqual([]);
	});

	test("PREDICATE 3: a user id matches EXACTLY, never as a prefix", () => {
		expect(lookupUsers(TENANT_NO_EMAIL.id, UNIVERSE)).toEqual([TENANT_NO_EMAIL]);
		expect(lookupUsers(TENANT_NO_EMAIL.id.slice(0, 8), UNIVERSE)).toEqual([]);
	});

	test("an unrelated string returns nothing", () => {
		expect(lookupUsers("no-such-tenant", UNIVERSE)).toEqual([]);
		expect(lookupUsers(UNMATCHED_UUID, UNIVERSE)).toEqual([]);
	});

	test("VACUITY: an empty term returns NOTHING, not everything", () => {
		// A naive `includes("")` returns all three rows, and every tenant would
		// then be "reachable" from a picker nobody typed into.
		expect(lookupUsers("", UNIVERSE)).toEqual([]);
	});
});

describe("identifier extraction behaves as specified", () => {
	test("POSITIVE: a known text yields EXACTLY its token set", () => {
		const text = `Redeemed by ${TENANT_NO_EMAIL.id} and ${TENANT_WITH_EMAIL.email} on Sep 5, 2026, 12:04 PM`;
		const { uuids, emails } = identifiersIn(text);
		// Set equality, not `toContain` — an over-matching regex must fail here.
		expect(new Set([...uuids, ...emails])).toEqual(
			new Set([TENANT_NO_EMAIL.id, TENANT_WITH_EMAIL.email as string]),
		);
	});

	test("NEGATIVE: identifier-free text yields an empty set", () => {
		// Timestamps, version numbers, day counts and a truncation ellipsis —
		// everything a loosened regex starts collecting.
		const text = "Redeemed at Sep 5, 2026, 12:04 PM — grace window (30 days) — v1.2.3 — orbit-44…";
		expect(identifiersIn(text)).toEqual({ uuids: [], emails: [] });
	});

	test("an identifier rendered twice is still one identifier", () => {
		const text = `${TENANT_NO_EMAIL.id} ${TENANT_NO_EMAIL.id}`;
		expect(identifiersIn(text).uuids).toEqual([TENANT_NO_EMAIL.id]);
	});
});

describe("an operator holding ONE out-of-band identifier obtains a tenant id (G-03-62, Pitfall 8)", () => {
	test("JOURNEY A — a tenant with NO email: type its Clerk user id from the Clerk Dashboard, read the tenant id off the picker", () => {
		// 1. The one out-of-band hop: the operator copies the tenant's Clerk user
		//    id from the Clerk Dashboard. Nothing in the console names it.
		const fromClerkDashboard = CLERK_DASHBOARD_USER_ID;

		// 2. The clerk-id branch of the lookup, EXECUTED, and the picker fed
		//    exactly what it returns.
		const results = lookupUsers(fromClerkDashboard, UNIVERSE);
		expect(results).toHaveLength(1);
		const pickerText = renderedText(renderPicker(fromClerkDashboard, results));

		// 3. The tenant id is now in front of the operator — extracted from the
		//    picker's rendered text, not from a fixture.
		const { uuids } = identifiersIn(pickerText);
		expect(uuids).toHaveLength(1);
		const obtained = uuids[0] as string;

		// 4. THE LOOKUP ACCEPTS IT. The id the picker shows resolves exactly
		//    that tenant and nothing else — computed, not assumed.
		const resolved = lookupUsers(obtained, UNIVERSE);
		expect(resolved).toHaveLength(1);
		expect(identifiersIn(renderedText(renderPicker(obtained, resolved))).uuids).toEqual([obtained]);

		// 5. Closing check: the id the operator copied is this tenant's.
		expect(obtained).toBe(TENANT_NO_EMAIL.id);
	});

	test("JOURNEY B — a tenant WITH an email: search a fragment of it, read the id", () => {
		// 1. The out-of-band hop for this population: a fragment of the address
		//    the customer wrote in from.
		const fromSupportRequest = "dana@";

		// 2. The email-fragment branch of the lookup, EXECUTED.
		const results = lookupUsers(fromSupportRequest, UNIVERSE);
		expect(results).toHaveLength(1);
		const pickerText = renderedText(renderPicker(fromSupportRequest, results));

		// 3. The id is now in front of the operator, present only because the
		//    lookup mirror put that tenant into `results`. Before plan 03-20 the
		//    picker rendered the email INSTEAD of the id and this step found
		//    nothing.
		const { uuids } = identifiersIn(pickerText);
		expect(uuids).toHaveLength(1);

		// 4. Closing check.
		expect(uuids[0]).toBe(TENANT_WITH_EMAIL.id);
	});

	test("AN EMAIL-LESS TENANT IS UNREACHABLE BY ANY EMAIL FRAGMENT — so the Clerk user id is its required first hop", () => {
		// Every fragment an operator could type while hunting by email or name:
		// every substring of every address in the universe, plus the email-less
		// tenant's own name. None of them reaches it.
		const fragments = new Set<string>();
		for (const text of [
			...UNIVERSE.map((user) => user.email ?? ""),
			(TENANT_NO_EMAIL.name ?? "").toLowerCase(),
		]) {
			for (let start = 0; start < text.length; start++) {
				for (let end = start + 1; end <= text.length; end++) {
					fragments.add(text.slice(start, end));
				}
			}
		}
		// Non-vacuity: the fragment set is real and DOES reach the emailed
		// tenants, so an empty result below is about this tenant, not a dead
		// lookup.
		expect(fragments.size).toBeGreaterThan(100);
		expect(lookupUsers("@", UNIVERSE)).toEqual([TENANT_WITH_EMAIL, TENANT_UNRELATED]);

		for (const fragment of fragments) {
			expect(lookupUsers(fragment, UNIVERSE)).not.toContain(TENANT_NO_EMAIL);
		}
		// And the picker an operator sees for the broadest fragment never shows
		// the email-less tenant's id.
		const text = renderedText(renderPicker("@", lookupUsers("@", UNIVERSE)));
		expect(identifiersIn(text).uuids).not.toContain(TENANT_NO_EMAIL.id);
	});

	test("A TYPED VALUE IS NOT EVIDENCE — the journey-level form of the tautology", () => {
		// The picker fed a uuid that matches no row: the mirror legitimately
		// returns nothing, so nothing is displayed.
		const results = lookupUsers(UNMATCHED_UUID, UNIVERSE);
		expect(results).toEqual([]);
		const markup = renderPicker(UNMATCHED_UUID, results);
		expect(identifiersIn(renderedText(markup)).uuids).toEqual([]);
		// And the assertion the debug probe caught: the raw markup DOES carry
		// the uuid, in the search input's own `value` attribute. Reading a
		// rendered claim off attributes is how a green test outlived the
		// capability it named.
		expect(markup).toContain(UNMATCHED_UUID);
	});
});
