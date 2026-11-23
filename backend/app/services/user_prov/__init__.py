"""Provider factory for the user-provisioning seam (D-02, D-03).

Dispatches on `settings.user_prov`. Phase 3 supports "clerk" only;
"supa" is the phase 3.1 self-hosted Supabase provider and fails loudly
until it lands.
"""

from __future__ import annotations

from app.core.config import settings
from app.services.user_prov.base import UserProvisioningProvider
from app.services.user_prov.clerk import ClerkUserProvisioningProvider


def get_user_provisioning_provider() -> UserProvisioningProvider:
    provider = settings.user_prov
    if provider == "clerk":
        return ClerkUserProvisioningProvider()
    if provider == "supa":
        raise NotImplementedError(
            "user_prov='supa' is the self-hosted Supabase identity provider, "
            "implemented in phase 3.1 — configure user_prov='clerk' until then"
        )
    raise ValueError(f"Unknown user_prov value: {provider!r} (expected 'clerk' or 'supa')")


__all__ = [
    "UserProvisioningProvider",
    "get_user_provisioning_provider",
]
