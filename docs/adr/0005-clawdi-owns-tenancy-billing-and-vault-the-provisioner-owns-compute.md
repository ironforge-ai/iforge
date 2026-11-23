# ADR-0005: Clawdi Owns Tenancy, Billing and Vault; the Provisioner Owns Compute

**Status:** Accepted
**Date:** 2026-08-14
**Deciders:** Clawdi maintainers

## Context

Two systems were independently building the same half of one product.

This repository already carries Clerk-backed tenancy, an encrypted vault, and the web surfaces
of a subscription and wallet billing flow whose server side is implemented in a separate
first-party backend. Alongside it, a sibling project shipped a working v1.0 that provisions
agent runtimes onto Hetzner through Supabase Edge Functions, Terraform Cloud and Stripe, and
had begun planning a v2.0 milestone that would have added its own tenancy, subscription
billing, signup path and secret storage.

That overlap was not theoretical. Both projects had independently reached, and recorded, the
same blocker: neither could safely plan an infrastructure phase without knowing which of them
owned payment, entitlement, tenancy and vault, and which of the two provisioning paths was
canonical. Two roadmaps were stalled on one unanswered question, and every week the question
stayed open increased the amount of duplicated tenancy and billing machinery that would later
have to be unbuilt.

The decisive argument for resolving it the way this ADR does is that the seam already exists
in this codebase and is already enforced by tests. Hosted surfaces render only under
`VITE_CLAWDI_HOSTED=true`, every hosted component carries `data-hosted="true"`, and
`apps/web/src/hosted/oss-clean.test.ts` statically fails the build on an import that crosses
out of `apps/web/src/hosted/`; the boundary is documented in `apps/web/src/hosted/README.md`.
The CONFIG plane and the COMPUTE plane are therefore already separated, with a filtered
OpenAPI subset as the wire between them. Assigning compute to a different implementer does not
move that seam; it changes which system sits on the far side of it. This decision is an
implementation swap, not an architectural change, and that is precisely why it is affordable.

## Terms

This ADR is mirrored into a second repository, and its intended reader has neither checkout in
front of them. "The provisioner" in particular is ambiguous in context: this codebase already
uses it to mean the external system that serves `/v2/deployments/*` today. The vocabulary is
therefore fixed here, before any verdict is stated.

Clawdi means this repository together with clawdi-hosted, the private first-party backend
served at `api.clawdi.ai`, treated throughout as a single product boundary. The two are
separate repositories with a shared owner, a shared release discipline and a shared user-facing
identity; splitting them for the purpose of this ADR would answer a question nobody is asking.
The private backend is cited by its public repository URL,
<https://github.com/Clawdi-AI/clawdi-hosted>, and by endpoint path only.

The provisioner means the hermes-deploy repository, and only that. Where this document says the
provisioner owns, holds or executes something, it names that repository's deployed service and
never the first-party backend above.

A reader who holds only one of the two checkouts should be able to act on every sentence below.
Where a claim cannot be grounded in something readable, it is recorded as an open item rather
than asserted.

## Decision

Clawdi owns payment, entitlement, tenancy and vault. The provisioner owns compute. Each concern
is answered below with the evidence that supports it, or with an explicit statement that no
evidence exists.

**Tenancy.** Tenancy is Clerk-backed in this repository. Identity ingestion, session exchange
and the CLI OAuth loopback flow are implemented here, in `backend/app/routes/clerk_webhooks.py`
and `backend/app/services/clerk_backend.py`, with CLI device-flow settings in
`backend/app/services/clerk_cli_oauth_settings.py`. No tenancy record is created, mutated or
authoritatively read anywhere else, and the provisioner receives a tenant only as an opaque
identifier attached to a deployment request.

**Vault.** The encrypted vault is this repository's. Its routes live in
`backend/app/routes/vault.py`, its cryptography in `backend/app/services/vault_crypto.py` using
AES-256-GCM keyed by `VAULT_ENCRYPTION_KEY`, and the hosted runtime secret projection built on
the same key in `backend/app/services/hosted_runtime_secrets.py`. The vault is the sole store
for anything tenant-facing.

**Payment.** Payment is resolved in clawdi-hosted behind /v2/subscription/* and /v2/wallet*.
This repository holds the consuming clients only: the web surfaces under
`apps/web/src/hosted/billing/**`, including the Stripe checkout and wallet components, and the
generated client in `packages/shared/src/api/deploy.generated.ts`. There is no Stripe code in
`backend/app` at all, which is what makes the split checkable rather than merely asserted.

**Entitlement, recorded as an open item.** Entitlement is the mapping from subscription state
to what a tenant may provision. Its only observable surface is `/v2/subscription/*` together
with `/v2/usage`, both of which Clawdi publishes and owns. Beyond that surface, no entitlement
module is citable in either repository: a case-insensitive search for the term across
`backend/app`, `apps/web/src` and `packages/` matches exactly one file, and that file is a UI
projection test rather than an implementation. The honest verdict is therefore that entitlement
falls on Clawdi's side of the boundary by elimination and by surface, and that where it is
resolved has not been pinned. This is an open item, not a citation. It is recorded here rather
than smoothed over because an owner nobody can point at is a claim a future reader cannot act
on, and because a reader of either repository getting the same answer is the point of this
document, which "we have not pinned this yet" satisfies and a confident guess does not.

**Compute.** Compute moves to the provisioner behind /v2/deployments/*. The enumerated surface
is the allowlist in `scripts/filter-deploy-openapi.py` rather than a list transcribed into this
paragraph; see the canonical-contract section below for why that indirection is deliberate.

### Decisioning versus execution

Refund and shutoff decisioning stays Clawdi's; the provisioner only executes. This is the
operational half of the verdict above, and the verdict is incomplete without it: owning payment
and entitlement means nothing if the party holding the machines can independently decide to cut
a tenant off.

A provision failure is reported back through the contract, and Clawdi owns the resulting refund.
Clawdi emits the shutoff signals: the soft signal is `POST /v2/deployments/{id}/stop`, which
powers the instance down while preserving its state, and the hard signal is
`DELETE /v2/deployments/{id}`, which tears it down. Reactivation on repayment is likewise
Clawdi's call, issued as an ordinary lifecycle request. The provisioner carries all of these out
without deciding whether they are warranted, and has no view of subscription state with which it
could decide.

### Credential partition

The provisioner holds operational credentials only: its own Hetzner platform token, its
Terraform Cloud token, and per-request BYO-infra tenant tokens, which are passed by reference,
never by value. It never holds tenant application, runtime or companion secrets. Clawdi's
encrypted vault, keyed as described above, remains the sole store for anything tenant-facing,
and the provisioner receives from it only what a single deployment operation requires, in the
form of a reference it resolves through Clawdi rather than a secret it retains.

## Canonical Provisioning Contract

`/v2/deployments/*` is the single canonical provisioning contract. There is no second
provisioning path with equal standing, and no negotiation between two contracts at request
time.

The boundary this ADR draws is a responsibility boundary, not a document boundary. Compute and
billing operations are generated from one specification into one `deploy.generated.ts`, and the
enumerated canonical surface is `KEEP_OPERATIONS_BY_PATH` in `scripts/filter-deploy-openapi.py`.
That allowlist is cited here rather than transcribed: a second copy of the endpoint list living
in prose would go stale on the first change, and the script's own header already describes
adding an operation as a deliberate audit point. Anyone who wants the authoritative list should
read the allowlist, and anyone who diffs the allowlist against a summary in someone's roadmap
should trust the allowlist.

`backend/scripts/mock_deploy_api.py` is promoted from a development convenience to the
executable reference implementation of the contract. Once Clawdi authors the schema, the mock
becomes the only place in either repository where the contract's behaviour is expressed as code
that runs. That promotion carries an obligation: a `/v2` shape change lands in the mock before
the provisioner is asked to conform to it, so that "conform to the contract" always names
something executable. The mock also keeps serving the hosted-UI development flow it already
serves; the promotion adds a duty, it does not repurpose the file.

### The starting state, stated correctly

The CI-enforced spec source has always been Clawdi-side. `scripts/check-deploy-generated-api.sh`
defaults to `spec_source="${DEPLOY_OPENAPI_SOURCE:-https://api.clawdi.ai/openapi.json}"`, and
the `:50021` localhost source that appears in `scripts/generate-deploy-api.sh` is the local
development default only; `scripts/filter-deploy-openapi.py` documents that port as where a
developer serves the first-party backend's own checked-in specification. Any account of this
decision that describes it as reversing an existing flow is describing something that did not
happen.

What this ADR decides is narrower, and still worth deciding: when compute moves to the
provisioner, schema authorship does not move with it. Ownership of an implementation and
ownership of the document describing it are separable, and here they are deliberately separated.

### Publisher

api.clawdi.ai remains the canonical publisher of the `/v2` OpenAPI document, and the document
stays merged rather than splitting when compute moves. The provisioner conforms to a document
it does not publish and cannot unilaterally change. Naming a schema author without naming a
publisher would leave `check-deploy-generated-api.sh` pointing at nothing decidable, which is
the failure this paragraph exists to prevent.

### Conformance, and the limits of the existing check

Once the provisioner implements `/v2/deployments/*` while api.clawdi.ai publishes the document
describing them, `check-deploy-generated-api.sh` compares this repository's committed client
against the published document and never contacts the provisioner. It therefore cannot detect
the provisioner's implementation drifting from the specification. It also soft-fails by design:
`DEPLOY_CONTRACT_FETCH_MODE` defaults to `warn`, so a fetch failure emits a GitHub warning and
exits 0 rather than failing the build.

Both properties are acceptable, but only if stated. The conformance obligation lands on the
provisioner's side, in its QA-07 and QA-08 requirements, which run contract-conformance tests
against Clawdi's published document. This repository's drift check is not sufficient evidence on
its own, and a green Clawdi CI run must never be read as evidence that the provisioner conforms.

### Disposition of the superseded path

The payment-triggered provisioning entry point is hard-disabled: the Stripe webhook to
deployment-insert trigger, together with the standalone signup console that fronted it. Its
disposition is deliberate and open-ended, in these words: no removal planned - revisit only if
the ownership decision is revisited. A later reader should not mistake dormancy for oversight,
and should not treat the absence of a removal date as an unfinished decision.

The Terraform choreography beneath that entry point survives as provisioner-internal
implementation. The `pg_notify` to `trigger-deploy` to Terraform Cloud path, and the
`trigger-destroy`, `terraform-callback` and `store-secret` functions alongside it, are retained
and are the provisioner's business; the `/v2` handlers wrap them. Only the payment-triggered way
in is closed. Retaining a superseded surface rather than deleting it follows the precedent
already set in this codebase, where `/v1` is canonical while `/api/*` and `/v1/environments`
remain as documented compatibility aliases.

### Authentication and status posture

Clawdi-to-provisioner calls are HMAC-signed over the raw request body together with a timestamp
and a nonce, and fail closed on a missing, expired or replayed signature. mTLS is the named
upgrade path, and its trigger condition is the first moment a third party operates a provisioner
instance.

This is new construction on both sides, and calling it a reuse would understate it. The existing
`_shared/deploy-auth.ts` supplies a constant-time comparison and a static header check only, and
the HMAC verification that does exist there is inbound from Terraform Cloud rather than on the
Clawdi-to-provisioner path. The fail-closed property is the part worth reusing: a missing shared
secret rejects every caller rather than silently admitting them.

The HMAC client is clawdi-hosted's backend. This naming is a design assumption rather than an
observation: this repository's Python backend does not call the deploy API at all, and the only
existing clients are the CLI and the hosted web surfaces, so the server-side signer is being
specified here rather than described. It is called out so a later phase can contradict it
cheaply if the architecture puts the signer elsewhere.

Rotation is dual-secret overlap: both the outgoing and incoming secrets are valid at once,
verification runs in constant time against each in turn, and a metric is emitted whenever the
old secret is the one that matched, so the overlap window closes on evidence rather than on
optimism.

Polling is canonical and push is an optional latency optimization. Clawdi polls
`/v2/deployments/{id}` and `/v2/operations/{id}`, and that path is always correct on its own.
Provisioner callbacks may only advance state that polling would have reached anyway, and
reconciliation stays poll-driven. A dropped, duplicated or out-of-order callback therefore
degrades the system to "slower" and never to "stuck".

### Cross-references and rollout

Shutoff signals and the credential partition are ownership verdicts and are recorded once, in
the `## Decision` section above. They are not restated here, because a duplicated rule gives a
future editor two places to change and one to forget.

`docs/managed-runtime.md` already mandates consumer-first rollout for the runtime manifest. The
same discipline is ruled to apply here: the consumer-first protocol binds /v2 changes. The
reason is specific to this surface rather than borrowed by analogy. `/v2` is consumed by a CLI
that ships to users on their own upgrade cadence, so a producer-first change is live against
clients that predate it, and `extra="forbid"` on this codebase's companion schemas is already a
documented compatibility hazard where a new field breaks an older client outright.

## Options Considered

### Clawdi owns payment, entitlement, tenancy and vault (chosen)

This keeps the CONFIG and COMPUTE planes exactly where the codebase already draws them, so the
change is which system implements compute rather than where the seam runs. Clerk tenancy, the
Stripe subscription and wallet surfaces, and the encrypted vault all stay where they are
implemented and tested today. `backend/scripts/mock_deploy_api.py` stays a working development
double and `packages/shared/src/api/deploy.generated.ts` stays valid. The cost is that the
provisioner loses a billing product it had planned and not yet built.

### The provisioner owns billing and signup, Clawdi consumes compute

This was genuinely on the table: the sibling project had a working v1.0 with Stripe and a signup
path already shipped, so the machinery existed. It was rejected because it points the more
expensive migration the wrong way. Clawdi's tenancy, billing surfaces and vault are live, carry
real user data, and are covered by tests and a test-enforced hosted/OSS boundary; the
provisioner's equivalent v2.0 scope was zero percent built. Choosing this option would have
meant migrating working, populated systems onto Supabase in order to preserve unbuilt plans.

## Consequences

- Two roadmaps unblock at once. The provisioner's milestone reshapes to a compute-only scope,
  and this repository's infrastructure, operations and hosted-runtime phases can be planned
  against a fixed contract.
- The provisioner's requirement set gains a conformance obligation it must carry, because this
  repository's drift check cannot discharge it.
- The mock gains a maintenance duty: `/v2` shape changes land there first.
- Entitlement remains an open item. A later phase must either cite where it is resolved or
  record that it is being built, and this ADR should be amended when that happens.
- A downstream cost is now known rather than discovered later: adding a hosted IronHermes
  runtime requires extending the `consumer_runtime IN ('codex', 'hermes', 'openclaw')` CHECK
  constraint through an Alembic migration, plus roughly ten `Literal` and frozenset sites across
  the backend and `packages/shared`. That work belongs to the hosted-IronHermes phase and is
  recorded here so it is budgeted rather than rediscovered.
- This ADR is mirrored byte-for-byte into the provisioner's repository. Its in-repository path
  citations, such as `scripts/filter-deploy-openapi.py`, name Clawdi files and will not resolve
  in the mirror. That is accepted: they are citations, not links, and every external system is
  cited by public URL precisely so the mirrored copy stays readable.

## Reconsideration Criteria

- The superseded payment-triggered entry point is revisited only if the ownership decision
  itself is revisited. Nothing else reopens it, and no removal milestone is pending.
- HMAC is promoted to mTLS when a third party first operates a provisioner instance.
- The publisher decision is reconsidered only if the first-party backend stops publishing an
  OpenAPI document that covers the compute surface, since the drift check has exactly one
  source.
- The entitlement open item closes when its resolution point can be cited, at which point the
  verdict above is replaced with a citation.

## Related Documentation

- [Hetzner terms position](../hetzner-terms-position.md)
- [Infrastructure layout](../infrastructure-layout.md)
- [Managed runtime contract](../managed-runtime.md)
- [API compatibility](../api-compatibility.md)
