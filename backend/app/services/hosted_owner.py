"""The owner identity on the hosted /v2 wire (06.2 D-09).

The hosted control plane keys every owner by the Clerk user id: the value the
dashboard's Clerk JWT carries as `sub`, and the value the Stripe Customer the
hosted service creates carries in its metadata (D-10). The backend therefore
names the owner by `users.clerk_id`, never by the internal `users.id` UUID, so
the dashboard's subscription and the backend's gate and purge reads all land on
one hosted owner (RESEARCH F1, Pitfall 1).

Partner-tenant principals have no Clerk id (`ck_users_principal_identity`) and
never reach hosted billing: `hosted_owner_id` returns None for them and callers
must not make a /v2 call on their behalf.

This module is the single place that identity is derived.
"""

from __future__ import annotations

from app.models.user import PRINCIPAL_KIND_CLERK, User


def hosted_owner_id(user: User) -> str | None:
    """Return the /v2 owner id for `user`, or None when it has no hosted owner.

    A Clerk principal's owner is its stripped, non-empty `clerk_id`. Any other
    principal (partner tenants), or a Clerk row with a blank `clerk_id`, has
    none.
    """
    if user.principal_kind != PRINCIPAL_KIND_CLERK:
        return None
    clerk_id = (user.clerk_id or "").strip()
    return clerk_id or None
