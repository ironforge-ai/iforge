"""The /v2 owner identity (06.2 D-09).

The hosted control plane keys every owner by the Clerk user id, the same
value the dashboard's Clerk JWT carries as `sub`. `hosted_owner_id` is the
single place the backend derives it; a principal with no Clerk id (a partner
tenant) has no hosted owner and must never reach hosted billing.
"""

from __future__ import annotations

import pytest

from app.models.user import PRINCIPAL_KIND_CLERK, PRINCIPAL_KIND_PARTNER_TENANT, User
from app.services.hosted_owner import hosted_owner_id


def test_clerk_principal_owner_is_its_clerk_id() -> None:
    user = User(principal_kind=PRINCIPAL_KIND_CLERK, clerk_id="user_abc")

    assert hosted_owner_id(user) == "user_abc"


def test_partner_tenant_principal_has_no_hosted_owner() -> None:
    user = User(
        principal_kind=PRINCIPAL_KIND_PARTNER_TENANT,
        clerk_id=None,
        clerk_issuer=None,
        partner_tenant_ref="partner-ref-1",
    )

    assert hosted_owner_id(user) is None


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_clerk_principal_with_blank_clerk_id_has_no_hosted_owner(blank: str) -> None:
    user = User(principal_kind=PRINCIPAL_KIND_CLERK, clerk_id=blank)

    assert hosted_owner_id(user) is None


def test_clerk_principal_owner_is_stripped() -> None:
    user = User(principal_kind=PRINCIPAL_KIND_CLERK, clerk_id="  user_abc  ")

    assert hosted_owner_id(user) == "user_abc"
