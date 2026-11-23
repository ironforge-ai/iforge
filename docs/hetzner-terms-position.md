# Hetzner terms position

This document is the append-only record of Hetzner's written position on reselling, sub-customer
terms and agent-driven trading workloads, together with the operating constraints iforge derives
from it. Every clause below is quoted from a primary Hetzner source that was re-fetched and
re-extracted in the session that wrote this file; each block carries its document title, document
version, source URL and retrieval instant.

For the ownership decision this record supports, read
[ADR-0005](adr/0005-clawdi-owns-tenancy-billing-and-vault-the-provisioner-owns-compute.md). For the
API compatibility policy whose document frame this file follows, read
[`api-compatibility.md`](api-compatibility.md).

**This document is append-only.** Vendor replies, later retrievals and enforcement events are
appended — to the ticket log at the bottom, or as a new dated subsection. Quoted-clause sections are
never edited in place, because the value of a quotation is that it can be checked against the
retrieval instant recorded beside it.

## Sources

| Source | Document version | Retrieved | Role |
|---|---|---|---|
| `https://cdn.hetzner.com/assets/Uploads/AGB-en.pdf` | Terms and Conditions Version 2.0.0 / Last updated 27 October 2021 | 2026-08-15T01:16Z | Primary PDF. Fetched (HTTP 200, 94 532 bytes) and text-extracted from its content streams. |
| `https://www.hetzner.com/legal/terms-and-conditions/` | Terms and Conditions Version 2.0.0 / Last updated 27 October 2021 | 2026-08-15T01:17Z | Primary HTML rendering of the same declared version. Transcription source for the clauses below. |
| `https://www.hetzner.com/legal/cloud-server/` | **No version and no effective date are displayed on the page as of 2026-08-15.** | 2026-08-15T01:17Z | Cloud and vServer Service Agreement — service-specific prohibited-actions list. |
| `https://www.hetzner.com/legal/system-policies/` | No version displayed | 2026-08-15T01:20Z | Read directly; it is an index page and carries no response-window figures. |

**Document version:** Terms and Conditions Version 2.0.0 / Last updated 27 October 2021.
**Retrieved:** 2026-08-15T01:17Z (session date 2026-08-14 local).

Both primary renderings were compared clause by clause. They agree on every clause quoted below
except §8.4, whose divergence is recorded under
[Divergence between two primary renderings](#divergence-between-two-primary-renderings).

## Quoted clauses

### §7 — Use by third parties

Source: Terms and Conditions Version 2.0.0 / Last updated 27 October 2021,
`https://www.hetzner.com/legal/terms-and-conditions/`, retrieved 2026-08-15T01:17Z.

> **7.1.** The Customer is entitled to grant third parties a contractual term of use to any services
> the Customer orders from Hetzner. In this case,
> the Customer nevertheless remains the sole contractual partner. The Customer continues to be
> solely and fully liable for compliance with the contractual agreements between us and the Customer.

> **7.2.** If the Customer transfers user rights to their Hetzner services to a third party, the
> Customer is obligated at the time of transfer to ensure that all legal and contractual provisions
> are followed. This is true for any changes that require the cooperation of the third party.

> **7.3.** If the third party violates the contractual obligations or does not comply with the
> obligation to cooperate, if the data provided by the third party is incorrect or incomplete, or if
> other problems arise with the granting of user rights to third parties, the Customer assumes full
> liability for all resulting damages and, in addition, indemnifies us from all claims made against
> us by the third party or others.

### §8 — Use of the services / content

Source: Terms and Conditions Version 2.0.0 / Last updated 27 October 2021,
`https://www.hetzner.com/legal/terms-and-conditions/`, retrieved 2026-08-15T01:17Z.

> **8.3.** The transmission of spam mail is prohibited. This includes in particular the sending of
> unauthorized, unsolicited advertising to third parties. When sending emails, it is also prohibited
> to provide false sender data or to disguise the identity of the sender in any other way.
> The operation of applications for mining cryptocurrencies remains prohibited. These include, but
> are not limited to, mining, farming and plotting of cryptocurrencies. We are entitled to lock the
> Customer’s access to their Hetzner services or account in the event of non-compliance.

> **8.4.** If we become aware of illegal activities, we are obligated under Art. 6 Abs. 1 DSA
> (Digital Services Act) to request that the Customer immediately removes the offending content and
> we are entitled to lock the Customer’s access to their Hetzner services or account.

### Transcription note

The source renders the possessive in §8.3 and §8.4 with a typographic apostrophe (U+2019); the
quoted blocks above preserve it. Rendered with an ASCII apostrophe, the lock sentence reads: "We are
entitled to lock the Customer's access to their Hetzner services or account in the event of
non-compliance."

Two further transcription facts, recorded so a later reader can reproduce this work rather than
trust it:

- §8.3's wording is `remains prohibited`, not "is prohibited". A summarising intermediary rendered
  it as the latter during phase research. Both primary sources say `remains prohibited`, and the
  doc-contract test asserts that exact string so the drift cannot recur silently.
- The PDF content-stream extraction used here is lossy in §7.3: it drops the clause "and, in
  addition, indemnifies". The §7.3 text above is transcribed from the HTML rendering, which carries
  the full sentence. This is a limitation of the extraction method, not a divergence between the two
  sources.

### Cloud and vServer Service Agreement — §2 Prohibited actions

Source: `https://www.hetzner.com/legal/cloud-server/`, retrieved 2026-08-15T01:17Z. **No version
string and no effective date are displayed anywhere on that page**, which was an open assumption
carried into this phase and is now a checked finding.

> **2. PROHIBITED ACTIONS**
>
> We strive to keep our networks operating at the highest possible level, so all of our clients
> benefit from it. Therefore the following actions are prohibited:
>
> - Operating applications that are used to mine crypto currencies
> - The scanning of foreign networks or foreign IP addresses
> - The use of fake source IPs.

This service-specific agreement carries no third-party-use clause and no account-locking clause;
those live in the Terms and Conditions quoted above. Its scanning bullet matters operationally: an
aggressive market-data or discovery agent running on a tenant's box can present as network scanning.

### Divergence between two primary renderings

The two primary sources declare the same document version — Terms and Conditions Version 2.0.0 /
Last updated 27 October 2021 — but state §8.4's statutory basis differently:

| Source | §8.4 statutory basis as rendered |
|---|---|
| `https://www.hetzner.com/legal/terms-and-conditions/` (retrieved 2026-08-15T01:17Z) | "obligated under Art. 6 Abs. 1 DSA (Digital Services Act)" |
| `https://cdn.hetzner.com/assets/Uploads/AGB-en.pdf` (retrieved 2026-08-15T01:16Z) | "obligated under § 10 Telemedia Act (TMG)" |

Recorded, not resolved. It changes nothing about the operative half of §8.4 — the lock entitlement
is worded identically in both — but it means the version string alone does not identify a unique
text, so any future citation of this document must name which rendering it came from. The clauses
quoted above are the HTML rendering except where stated.

## What the published terms do not say

A full-text scan of Terms and Conditions Version 2.0.0 was run in this session against both primary
renderings, searching for the terms that would have to appear if the document reached agent-driven
trading, validator operation or reselling:

| Searched term | Occurrences, HTML rendering | Occurrences, PDF extraction |
|---|---|---|
| `trading` | 0 | 0 |
| `blockchain` | 0 | 0 |
| `validat` (validator, validation) | 0 | 0 |
| `node hosting` | 0 | 0 |
| `resell` | 0 | 0 |

Scan scope was §1 through §14 inclusive, ending at the version line; the US-specific Special Terms
that follow it on the same page were scanned separately and return 0 for all five terms as well. The
only near-match in either source is the word sequence "is valid at the time" in §3.1, which matches
`validat` solely as an artefact of collapsing whitespace before searching; on word boundaries the
count is 0 in both.

For contrast, the terms that *are* present: `mining` appears twice in §8.3, `farming` once and
`plotting` once.

**The published terms are silent on trading.** That silence is itself the written position OWN-03
asks for on this half of the question — there is a written answer, and the answer is that the
document does not reach the subject. §8.3 reaches mining, farming and plotting; §7 reaches third-party
use; nothing in Version 2.0.0 addresses an agent placing orders on an exchange.

## Extra-textual enforcement record

**This section is press-reported, not a Hetzner-published document.** Nothing below was retrieved
from a Hetzner domain, and none of it carries the standing of the clauses quoted above. It is
recorded because Hetzner's enforcement behaviour is a real input to a risk decision even when it is
not a contractual input — but it is fenced into its own section so that no reader can mistake it for
the quoted material.

| Event | Date | Reported by |
|---|---|---|
| Hetzner support statement read as extending §8.3 beyond mining, reported in the form "Using our products for any application related to mining, even remotely related, is not permitted" and "It includes proof-of-stake and proof-of-work and related applications" | late August 2022 (coverage dated 26 August 2022) | CoinDesk, Decrypt, CryptoSlate |
| Hetzner acted against Solana validators; 1000-plus validator nodes and roughly 20% of active stake offline within hours | 2 November 2022 | The Block, Unchained, CryptoSlate |
| Hetzner reported to host roughly 16% of Ethereum validator nodes at the time of the August statement | August 2022 | CryptoSlate |

Provenance and confidence: these three rows are carried from `.planning/research/` (phase research,
MEDIUM confidence), which cited the outlets named above. The outlet articles themselves were **not**
re-fetched in the session that wrote this file, and the quoted support-statement wording is a press
rendering of a support reply, not a document Hetzner published. Anyone relying on this section
should re-verify it at the outlets before treating it as more than a risk signal.

## Derived operating constraints

### Trading workloads are excluded from the Hetzner substrate

Vibe-Trading and any other agent-driven trading workload will not have a Hetzner option. This is
**a risk-based decision, not a compliance one**: the record above shows that the published terms are
silent on trading, that the operative crypto clause reaches mining, farming and plotting, and that
Hetzner's reported enforcement has been broader than the clause text. Excluding the workload from
this substrate removes a suspension risk to every other tenant on the account; it does not assert
anything about what Hetzner's terms require.

Where trading products run instead is out of scope for this document and belongs to the Vibe-Trading
milestone. Phase 1 records only the exclusion and the basis for it.

### Reselling to sub-customers is permitted, with liability retained by us

§7.1 permits granting third parties a contractual term of use to services we order, unconditionally
in the text. §7.1 and §7.3 together mean we remain the sole contractual partner and carry full
liability for every tenant's conduct, plus an indemnity obligation for third-party claims.

The operating consequence is that abuse-notice handling is a first-class operational path, not an
exception path. One tenant's misbehaving agent is, contractually, our breach. Because Hetzner's
enforcement unit is the account rather than the workload, the failure mode is not "that tenant is
suspended" but "every tenant on the account is suspended".

### Abuse-notice response targets

Response windows of roughly **6 hours** for port scanning, **24 hours** for network attacks and
**48 hours** for email abuse, with the right to suspend access to all products on breach. Our
internal response target sits inside those windows: an abuse notice must be triaged, and the
offending tenant workload stopped if triage is inconclusive, well before the shortest window
expires.

Provenance: these figures are carried from `.planning/research/PITFALLS.md` and were **not**
confirmed against a primary Hetzner document in the session that wrote this file —
`https://www.hetzner.com/legal/system-policies/` was read directly and carries no response-window
figures. Treat them as planning targets until a primary source is appended here.

### Account and project mechanics that follow from the above

Hetzner Cloud API tokens are scoped to a project and projects are created in the Console, so
per-tenant project isolation cannot be automated. That constraint, and the account/vendor
segmentation chosen in response to it, are recorded in the infrastructure layout document rather
than here.

## Ticket log

Support correspondence with Hetzner is appended here as it happens. Replies are appended to this
table, never edited into the quoted-clause sections above. Rows already in the table are never
edited or removed either — a later ticket is added as a new row beneath this one.

| Ticket ID | Date filed | Question asked (verbatim) | Reply (verbatim) or dated no-reply |
|---|---|---|---|
| *none — no ticket was filed* | *not filed as of 2026-08-15* | *no question was put to Hetzner* | no reply as of 2026-08-15 — no ticket exists, so no reply is outstanding |

**No ticket has been filed as of 2026-08-15** (UTC; session date 2026-08-14 local). No written
Hetzner position on the reselling / sub-customer reading of §7.1 has therefore been obtained, and
none is in progress. Read this row precisely: it records that we did not ask, not that we asked and
heard nothing. Nothing is pending, queued or awaiting a reply at Hetzner, and no clock is running.

### Escalation policy, for when a ticket is filed

The escalation timebox starts at the filing date, so it has not started. When a ticket is filed,
escalate at approximately 10 business days from that date. If the ticket then lapses without a
reply, leave the dated no-reply line standing in this table beside the quoted §7.1 clause rather
than removing it — a lapsed request is evidence about the vendor relationship and is worth as much
to a later reader as a reply would be.

Exactly one ticket is contemplated, covering reselling and sub-customer terms only. No question
about trading, mining, blockchain, validators or node hosting is to be put to Hetzner on an account
this platform depends on; the trading exclusion recorded above was decided internally and is not a
matter to negotiate with the vendor.

### Why this does not block anything

The ticket is **confirmation of a reading, not discovery**. §7.1 already permits granting third
parties a contractual term of use, unconditionally in the quoted text, and §7.3 already states where
the liability lands. A confirmation would raise our confidence in a reading we can already support
by quotation; it would not change what the document says.

Accordingly, **neither the reply nor the ticket blocks this phase**, nor Phases 4, 6 and 7. Blocking
downstream work on a support queue, in order to confirm a clause that already permits the use, would
buy nothing. What this phase owes is the quoted clause and an honest record of what was and was not
asked — both of which are above.
