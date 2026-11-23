"""User-provisioning provider seam (D-02, D-03).

The identity provider is swappable behind this Protocol. Phase 3 ships
Clerk as the only implementation (`user_prov="clerk"`, the default);
phase 3.1 implements the same Protocol for the self-hosted Supabase
provider (`user_prov="supa"`, connecting via `user_prov_url` /
`user_prov_api`).

Seam contract — the full intended surface, recorded as prose rather
than unused protocol methods (a method with no producer and no consumer
is dead weight; Phase 2 review precedent):

- **JWT verification** stays config-driven in `app/core/auth.py`'s
  `_verify_clerk_token`: the signing key comes from the JWKS endpoint
  derived from `clerk_jwt_issuer` (or the static PEM override), with a
  claimed-issuer fallback for legacy PEM-only deployments. A provider
  swap points that configuration at the new issuer; no code moves here.
- **Provisioning** stays at
  `app.services.user_provisioning.lazy_create_user_with_personal_project`,
  the single choke point that is provider-agnostic by construction — it
  consumes an already-verified subject and never talks to the provider.
- **Lifecycle-event ingestion** arrives via provider webhooks into
  `app.services.principal_lifecycle`'s fence entries (terminations,
  identity conflicts). A new provider wires its webhook surface to the
  same fence primitives.
- **Identity deletion** is the one operation that must call OUT to the
  provider, so it is the seam's first (and currently only) protocol
  method: 03-08's tenant-data-deletion path calls
  `get_user_provisioning_provider().delete_identity(subject)`.
"""

from __future__ import annotations

from typing import Protocol


class UserProvisioningProvider(Protocol):
    """Outbound identity-provider operations Clawdi initiates."""

    async def delete_identity(self, subject: str) -> None:
        """Delete the provider-side identity for `subject`.

        Already-gone identities (provider 404) are success — the
        deletion is idempotent and the provider's webhook reconciliation
        net owns any stragglers. Other provider failures raise.
        """
        ...
