# Infrastructure layout

This document records which vendor, which account and which project holds each part of the
platform, so that an operator can walk through a vendor suspension and show where it stops. It is
grounded in the decisions taken for Phase 1, in the 06.4 close-out of 2026-11-19 (06.4 D-28, D-29,
D-34), and in the primary sources re-read while writing it: Hetzner's Terms and Conditions (quoted
in the terms position document below), Hetzner's published Cloud API document at
`https://docs.hetzner.cloud/cloud.spec.json`, the Hetzner Cloud FAQ at
`https://docs.hetzner.com/cloud/general/faq/`, and Hostinger's published API spec at
`https://developers.hostinger.com/openapi/openapi.json`, retrieved 2026-11-19. The object-store path
it describes is the existing `backend/app/services/file_store_s3.py` and its settings in
`backend/app/core/config.py`.

For the ownership decision this layout implements, read
[ADR-0005](adr/0005-clawdi-owns-tenancy-billing-and-vault-the-provisioner-owns-compute.md). For the
quoted §8.3 and §8.4 lock clauses that make the separation necessary, read
[Hetzner terms position](hetzner-terms-position.md). For the document frame this file follows, read
[`api-compatibility.md`](api-compatibility.md).

**This records what runs today, and the constraints later phases keep.** The control plane runs on
one Hostinger VPS and tenant workloads run in the Hetzner tenant-workload project. A row changes
only by a recorded decision; update this document on the same change that makes a row different.

**No credential value appears in this document.** Credentials are named by variable name and vendor
only. Never write a token, a key fragment, an account number or a card number into this file:
`backend/tests/test_infra_layout_contract.py` fails the suite on four value shapes — a Stripe
live-mode secret or restricted key prefix, a value assigned to any credential name the vendor table
below declares (with or without spaces around the `=` or `:`), a run of 13 to 19 digits, and an
undelimited hex run of 32 characters or more — and a value committed here is the one mistake on this
page that an edit cannot undo. The assignment shape is built from the table itself, so a credential
added to a row is guarded on the same edit that declares it. The literal patterns are deliberately
not transcribed here: writing them out would trip the guard that this paragraph describes.

## Vendor and account table

The rows sit on two vendors, each under its own account, paid separately: Hetzner Cloud holds the
tenant workloads in the shared tenant-workload project, and Hostinger holds the control plane on one
VPS. Separation is by vendor and account. The credential-names column lists the variable names under
which credentials **for that row** are configured. The separation property this layout rests on is
that those name sets do not overlap: `test_infra_layout_credential_columns_are_disjoint` parses this
table and fails if a credential appears against two rows.

| Vendor | Account | Purpose | Payment method | Resources held | Credential names |
|---|---|---|---|---|---|
| Hetzner Cloud | One Hetzner account; the shared tenant-workload project | Tenant workloads only — one instance per tenant | The Hetzner invoice for that account | Tenant instances, per-tenant firewalls, per-tenant volumes; no shared private network | `hetzner_token` (the compute role's envelope-encrypted credential row, 06.4 D-19, handed to the OpenTofu run as the provider's token) |
| Hostinger | The operator's Hostinger account; one VPS, the cloud-api VPS | Control plane and its PostgreSQL (cloud-api, and staging billing and compute) | Hostinger's billing for that account, separate from Hetzner | The Kamal host, PostgreSQL with pgvector, encrypted vault ciphertext, and the object-store client settings for session and skill payloads | `DATABASE_URL`, `VAULT_ENCRYPTION_KEY`, `ENCRYPTION_KEY`, `FILE_STORE_S3_ACCESS_KEY_ID`, `FILE_STORE_S3_SECRET_ACCESS_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |

**Retired: HashiCorp Terraform Cloud.** An earlier version of this table had a Terraform Cloud row
(`TFC_TOKEN`, the provisioner's own organisation) for the runs that create and destroy tenant
instances. Decision D-13 replaced it: the hosted provisioner runs OpenTofu itself and keeps its
state in its own PostgreSQL (the `pg` backend), so there is no Terraform Cloud account and no
vendor row for it.

**Retired: Backblaze B2.** An earlier version of this table had a Backblaze B2 row: a dedicated
account holding backups and nothing else, paid on a separate instrument (Phase 1 D-16). The layout
has no backup row in 06.4: 06.4 delivers no control-plane backup (06.4 D-39), so there is no backup
vendor to list. Companion installations keep their own separate Backblaze account for companion
pgBackRest (Phase 06, hosted gap G-41); that account is outside this layout's control-plane scope
and is listed in the sub-processor register.

**Re-decided 2026-11-19 (06.4 D-28, D-29; hosted gap G-16 re-decided).** The control plane stays on
the existing Hostinger VPS, and tenant runtimes stay in the Hetzner tenant-workload project; the
records in this repository and in the hosted repository agree on that layout. Moving the control
plane onto a Hetzner project of its own is planned as backlog 999.5, with 06.4 D-22 and D-23 as its
starting design (06.4 D-38). Until that item is promoted and built, nothing in this table runs on a
Hetzner control-plane project.

**Why the vendors are named now.** `AGENTS.md` sets an OSS boundary for this repository: no hosted
runbooks, no private addresses, no internal deployment details. An earlier version of this page
withheld the control plane's hosting vendor on that ground and stated only a property of it. The
walkthrough below rests on which vendor holds which row, so both vendors are named. Hosts, sizes,
project names and server ids stay in the private hosted repository. In the project's core-value
claim, "the control plane is deliberately hosted off that substrate" means a separate vendor and a
separate account: the control plane is on Hostinger, and the tenant substrate is Hetzner.

No iforge service holds a Hostinger API token. Hostinger API tokens carry the owning user's full
rights, with no project or resource scope (06.4 D-24), so a token in any service would reach the
whole account, control plane included. The operator manages the VPS by hand: creating or resizing it
is an operator step, because the published VPS API has no delete and no plan change (06.4 D-29). The
compute role holds only the tenant-workload project's token.

**The object store is configuration.** `backend/app/services/file_store_s3.py` drives every
object-store operation through vendor-agnostic `boto3` against a caller-supplied `endpoint_url`, so
any S3-compatible vendor is reached by setting `FILE_STORE_S3_ENDPOINT_URL` and the two key
variables in `backend/app/core/config.py`. When those two settings are left empty the adapter builds
the client without explicit keys and boto3 resolves credentials from its standard chain, which is
why the `AWS_*` names are declared against the control-plane row too. Swapping the object-store
vendor is therefore a settings change and a credential rotation, with no client code to write.

## The suspension walkthrough

This is the part an operator has to be able to show, so it is written as a walk rather than as an
inventory.

Hetzner's Terms and Conditions Version 2.0.0 entitle Hetzner, at §8.3 and again at §8.4, to lock
"the Customer's access to their Hetzner services or account". Both clauses are quoted in full, with
their retrieval instants, in [Hetzner terms position](hetzner-terms-position.md). Read the object of
that sentence carefully: the entitlement runs to the account. The enforcement unit is the account,
not the project. Nothing in the quoted text scopes a lock to a project, a server or a workload.

Walk it:

1. A tenant's agent misbehaves — an aggressive discovery run that presents as network scanning, say,
   which the Cloud and vServer Service Agreement lists among its prohibited actions.
2. Hetzner sends an abuse notice. Under §7.3 the tenant's conduct is contractually our conduct, so
   the notice is ours to answer.
3. The notice is not answered inside the window, or the conduct recurs, and Hetzner exercises §8.3
   or §8.4 and locks the account.
4. An account-scoped Hetzner suspension reaches the tenant workloads and nothing else in the table.
   The control plane is on Hostinger, under another vendor and another account, so it keeps serving:
   the dashboard answers, the database answers and billing state is intact, while the tenants
   become unreachable until Hetzner lifts the lock.

This is why Phase 1 kept the control plane off Hetzner (Phase 1 D-14), and the 06.4 close-out keeps
it there. Two Hetzner projects would not survive step 3, because the lock entitlement names the
account and a project sits inside one; two Hetzner accounts under one customer relationship are not
demonstrably better, because nothing in Version 2.0.0 says a lock stops at one account of a customer
who holds two. Only a vendor with no contractual relationship to Hetzner is out of Hetzner's reach.

The reverse walk holds as well. An account-scoped Hostinger suspension reaches the control plane
only: the dashboard, the database and billing stop, and the tenant workloads on Hetzner keep
running, unmanaged, until the control plane returns. Because 06.4 delivers no control-plane backup
(06.4 D-39), recovery waits on Hostinger lifting the lock or on a rebuild. Staging data is
disposable — every staging user is a test account — so this is accepted for staging. Production's
backup target is chosen when production's control plane is built (06.2-26, hosted gap G-43), and it
sits outside the Hostinger account, because Hostinger's own backups share the account they protect.

## Why one shared tenant project, and what it costs

Tenant workloads live in a single shared Hetzner project rather than one project per tenant, and
that is a trade-off taken with its cost recorded rather than smoothed over.

**The mechanics that force it.** Hetzner Cloud API tokens are scoped to a project — the Cloud FAQ
describes managing "API tokens and S3 credentials" as a per-project Admin capability — and projects
are created in the Console. Re-checked against the published Cloud API document on 2026-08-14:
`https://docs.hetzner.cloud/cloud.spec.json` (OpenAPI 3.1.2, server `https://api.hetzner.cloud/v1`)
declares 151 paths, of which the string `project` appears in none. **No project-creation endpoint is
documented in the Cloud API reference as of 2026-08-14.** That is a statement about the published
document, not a claim about Hetzner's platform; an undocumented or internal endpoint would not
appear in it. On the strength of the document, per-tenant projects cannot be automated, and a
provisioning path that required a Console step per signup would not be a provisioning path.

**Where isolation lives instead.** At the VM boundary: one instance per tenant, no shared private
network, per-tenant firewalls. Two tenants share a project and an account; they share no network, no
host-level trust and no disk.

**The residual risk, stated plainly.** This is not closed and should not be read as closed: because
one token administers the whole project, the provisioner's token reaches every tenant's instance. A
compromise of that single credential is a compromise of every tenant's compute. Two mitigations are
available and neither eliminates it — Hetzner tokens carry a Read or Read-and-Write permission
choice at creation, so any consumer that only observes can hold a Read token; and the ownership
boundary drawn in D-01 and D-03 keeps that token on the provisioner side, holding only operational
credentials, with no tenant application, runtime or companion secret beside it. Revisit this
decision if the blast radius stops being acceptable, if the resource ceiling below binds, or if
signup opens beyond invite gating.

**The ceilings, one known and one not.** Re-read directly from
`https://docs.hetzner.com/cloud/general/faq/` on 2026-08-14 (page last changed 2026-04-27): "we have
a default limit of 20 projects per account. In some cases, it is possible to increase the default
limit if requested." That number caps the escape hatch rather than the design — if the shared
project is later sharded, it shards twenty ways before a limit-increase request is needed, and the
same FAQ records that limit requests are "processed manually and reviewed on a case-to-case basis
during our business hours". The other ceiling is the one that binds first, and **the per-account
resource-count ceiling is unknown**. The FAQ states the limits exist and that "You can find these
limits on the 'Limits' tab on your overview page" — they are account-specific and unpublished. The
lookup location is the Hetzner Console Limits tab on the account overview page; read the numbers
there before sizing a tenant cohort. Research expects this ceiling to bind before CPU and RAM do.
No number is guessed here.

## Backups and the credential boundary

06.4 delivers no control-plane backup (06.4 D-39). No Hostinger backup is turned on and no off-vendor
backup credential exists, so this table declares no backup row. Staging data is disposable, and the
staging backup leg belongs to backlog 999.5. Production's backup target is chosen when production's
control plane is built (06.2-26, hosted gap G-43), outside the control plane's Hostinger account;
this section and the vendor table are updated on the change that configures it. Tenant hosts take
no server backup.

Be precise about what evidence exists for the credential boundary today, because two automated
checks touch it and neither one proves what the other does:

| Check | Where it runs | What it proves | What it does not prove |
|---|---|---|---|
| `test_infra_layout_credential_columns_are_disjoint` in `backend/tests/test_infra_layout_contract.py` | This repository's test suite | That this document declares no credential against two rows, and that a later edit cannot quietly give the control-plane row a tenant-workload credential | That the deployed configuration matches this document |
| `scripts/ci/assert-no-live-creds.sh` in the provisioner repository | The provisioner's CI, as its first job step | That the provisioner's CI process holds no live credential in its environment | Anything about configuration reachability, in either repository |

And be precise about what is missing: **no live revocation drill has been run.** A drill would revoke
the tenant-workload project's token and confirm that the control plane keeps serving and that no
control-plane path depended on it — but the tenant infrastructure it needs does not exist until
Phase 6, so a drill attempted now would be a promise rather than a rehearsal. The drill belongs with
OPS-04, and until it runs, the boundary is documented and statically checked, not exercised.

## Abuse-notice operating constraint

§7.3 makes every tenant's misbehaviour our liability: we remain the sole contractual partner and
carry full liability plus an indemnity obligation for third-party claims. Hetzner forwards abuse
notices with response windows of roughly 6 hours for port scanning, 24 hours for network attacks and
48 hours for email abuse. (Those figures are carried from phase research and are not yet confirmed
against a primary Hetzner document — see the provenance note in
[Hetzner terms position](hetzner-terms-position.md).)

Our internal response target sits inside the shortest of those windows: an abuse notice is triaged,
and the offending tenant workload stopped if triage is inconclusive, well before the 6-hour window
expires. Stopping one tenant costs that tenant an outage; missing a window risks step 3 of the
walkthrough above for every tenant at once. Abuse-notice handling is therefore a first-class
operational path with an owner and an alerting route, not an exception path — and the soft-shutoff
signal it uses is the one already named in
[ADR-0005](adr/0005-clawdi-owns-tenancy-billing-and-vault-the-provisioner-owns-compute.md).
