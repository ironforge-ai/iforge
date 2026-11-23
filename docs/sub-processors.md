# Sub-Processors

This document lists the third-party processors that handle tenant data on the
hosted platform. It is a disclosure list, not a contract: entries record what
each processor is used for and which data categories reach it. It is
groundwork for a Data Processing Agreement (DPA) and for the tenant-data-rights
work (ACCESS-04 adjacency).

**No DPA exists yet.** Neither a customer-facing DPA nor a check of each
processor's own DPA has been done. Both need legal review before this list is
offered to tenants as part of one.

Consistent with this repository's OSS boundary (`AGENTS.md`), infrastructure
that is deliberately not named here is described by its binding property
instead, the same convention as
[`docs/infrastructure-layout.md`](infrastructure-layout.md). A hosting vendor
is named here where that layout names it; hosts and accounts stay in the
private hosted repository.

**Last reviewed:** 2026-11-19

## Processor table

| Processor | Purpose | Data categories processed | Data location note |
| --- | --- | --- | --- |
| Clerk | Identity and authentication | Account identifiers, email address, display name | Clerk-hosted identity service |
| Stripe | Card payments for compute subscriptions and wallet top-ups, through the hosted billing service (`/v2`) | Billing identity, payment metadata (customers, subscriptions, invoices, checkout sessions) | Stripe-hosted Checkout, payment and customer-portal pages; card data never touches this platform |
| LLM endpoint (`LLM_BASE_URL`) | Memory extraction: chat completions over a session's content | Session content submitted for extraction, and the memories returned | OpenAI-compatible endpoint set per deployment. The shipped Kamal config points at RedPill (`api.redpill.ai`) with an OpenAI model, so the model vendor behind that gateway is a further processor |
| Managed model provider | Inference for hosted agents that use iforge Managed AI | Agent prompts, tool results and model responses | Configured by the hosted service; the vendor is not recorded in this repository |
| Embedding API (optional) | Memory embeddings | Memory text submitted for vectorization | Used only when `MEMORY_EMBEDDING_MODE=api`; the shipped Kamal config uses `local`, which embeds on the backend host and sends nothing out |
| S3-compatible object storage | Binary payload storage | Session content bodies, skill archives, export artifacts | Configured per deployment; the storage vendor is a deployment property, not named in this repository |
| Hetzner Cloud | Tenant runtime hosts (one per deployment), in the tenant-workload project | All tenant runtime data | Hetzner Cloud, the tenant-workload project of one Hetzner account; locations are operator configuration |
| Hostinger | The control-plane VPS: cloud-api with its PostgreSQL, and staging billing and compute | Billing identity, account identifiers and vault ciphertext in that PostgreSQL | One Hostinger VPS in the operator's Hostinger account, a different vendor and account from the tenant hosts; its location is operator configuration |
| Cloudflare | Edge proxy and TLS for the hosted API; DNS, Tunnels and R2 media buckets for companion installations | All API request and response traffic in transit; companion media files | Cloudflare network. The API origin sits behind Cloudflare's proxy with an Origin CA certificate |
| Backblaze B2 | Backups for companion installations | Encrypted pgBackRest backups of companion databases | B2 bucket in an account separate from the compute and Cloudflare accounts; used only when companion backups are configured |
| Sentry | Error telemetry (backend and dashboard) | Error events and stack traces, which may incidentally contain request metadata | Sentry-hosted error tracking |
| PostHog | Product analytics (hosted dashboard) | Product usage events, account identifiers (Clerk user ID) | PostHog-hosted analytics, reached through a first-party proxy path |
| Chatwoot | Support live chat in the dashboard | Clerk user ID, name, email address, chat messages | Chatwoot instance at `VITE_CHATWOOT_BASE_URL`; whether that is Chatwoot Cloud or self-hosted is a deployment property |
| Composio | Connector tool routing | Tool call parameters and connector account bindings for agent connectors | Composio-hosted tool routing |
| Tailscale (optional) | Egress network for managed WhatsApp sessions | Network metadata for the WhatsApp sidecar's traffic | Used only when `WHATSAPP_TAILSCALE_EGRESS_ENABLED=true`; traffic leaves through an operator-chosen exit node |

## Notes

- The control plane and its database run on a Hostinger VPS, a different
  vendor and a different account from the Hetzner tenant hosts; see
  [`docs/infrastructure-layout.md`](infrastructure-layout.md).
- 06.4 configures no control-plane backup (06.4 D-39): staging data is
  disposable, and no backup vendor holds control-plane data. The production
  backup target is listed here when production's control plane chooses it.
- Messaging channels (Telegram, Discord, WhatsApp) are connected by the
  tenant, who chooses to send messages through them. They appear here only
  as a reminder that channel traffic reaches those providers.
- Agent-side AI providers a tenant configures themselves (Custom or Native
  providers in [`docs/ai-providers.md`](ai-providers.md)) are the tenant's
  own processors, not the platform's.
- Changes to this list (adding, removing, or repurposing a processor) should
  update the table and the **Last reviewed** date in the same change.
