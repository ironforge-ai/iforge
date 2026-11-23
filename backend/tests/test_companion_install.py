"""A tenant asks for Postiz on their own agent and gets a queued install (APP-01, APP-03).

The install request is the first step a person takes toward a companion, and the property it
must hold is structural rather than presentational: the platform generates every Postiz credential
server-side (D-27), stores each one encrypted in an iforge-owned table the tenant cannot read back
(CK-D: ``clawdi-store-set-password-link``), and returns none of them -- not in the response, not
in a log line, not in an audit event. A tenant vault item would have been readable in plaintext by
``/v1/vault/resolve`` and by any agent key holding ``vault:read``, which is exactly what APP-03
exists to prevent, so these tests decrypt the stored rows and then prove the response text never
contains a single one of the decrypted values.

Tenant isolation is asserted in both halves: a query broken badly enough to return nothing would
satisfy "no foreign rows" perfectly, so the caller's own rows are asserted present in the same test.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.auth import AuthContext, get_auth
from app.core.config import settings
from app.main import app
from app.models.project import PROJECT_KIND_PERSONAL, Project
from app.models.user import PRINCIPAL_KIND_PARTNER_TENANT, User
from app.schemas.runtime import (
    POSTIZ_APP_IMAGE,
    POSTIZ_CLOUDFLARED_IMAGE,
    POSTIZ_POSTGRES_IMAGE,
    POSTIZ_REDIS_IMAGE,
)
from app.services.companion_install import create_installation
from app.services.tenant_purge import PURGE_COVERAGE
from app.services.vault_crypto import decrypt
from tests.companion_billing_stub import quote_for, route_billing_to_mock
from tests.conftest import create_env_with_project
from tests.doc_contract import REPO_ROOT

ZONE = "companions.example"
INSTALLATIONS_PATH = "/v1/companions/installations"
SECRET_NAMES = ("admin-password", "api-key", "db-password", "jwt-secret")


@pytest.fixture
def companion_zone(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "companion_edge_zone_name", ZONE)
    return ZONE


@pytest.fixture
async def quote_id(monkeypatch: pytest.MonkeyPatch, seed_user) -> str:
    """A current /v2 quote for the caller -- every install request must carry one (plan 06-17)."""
    return await quote_for(monkeypatch, seed_user.clerk_id)


async def _agent(db_session, user_id, *, name: str = "postiz-host"):
    return await create_env_with_project(
        db_session,
        user_id=user_id,
        machine_id=f"machine-{uuid.uuid4().hex[:12]}",
        machine_name=name,
    )


def _install_body(
    agent_id, quote_id: str, *, companion_key: str = "postiz", tier: str = "standard"
) -> dict:
    return {
        "agent_id": str(agent_id),
        "companion_key": companion_key,
        "isolation_tier": tier,
        "quote_id": quote_id,
    }


async def test_tenant_requests_postiz_and_gets_a_queued_installation(
    client, db_session, seed_user, companion_zone, quote_id
) -> None:
    agent = await _agent(db_session, seed_user.id)

    response = await client.post(INSTALLATIONS_PATH, json=_install_body(agent.id, quote_id))

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["operation"] == "install"
    assert body["companion_key"] == "postiz"
    assert body["isolation_tier"] == "standard"
    assert body["agent_id"] == str(agent.id)
    assert body["hostname"].endswith(f".{companion_zone}")
    assert body["credential"] == "stored_in_vault"

    from app.models.companion_installation import CompanionInstallationSecret

    rows = (
        (
            await db_session.execute(
                select(CompanionInstallationSecret).where(
                    CompanionInstallationSecret.installation_id == uuid.UUID(body["id"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(row.secret_name for row in rows) == list(SECRET_NAMES)
    for row in rows:
        assert row.key_version == "vault.v1"
        value = decrypt(row.encrypted_value, row.nonce)
        assert len(value) >= 40
        assert value not in response.text, f"{row.secret_name} leaked into the response"


def test_the_installation_response_has_no_field_that_can_carry_a_credential() -> None:
    """APP-03 by construction: the only credential-shaped field is a one-value literal."""
    from typing import Literal, get_args, get_origin

    from app.schemas.companions import CompanionInstallationResponse, CompanionInstallRequest

    fields = CompanionInstallationResponse.model_fields
    credential = fields["credential"].annotation
    assert get_origin(credential) is Literal
    assert get_args(credential) == ("stored_in_vault",)
    forbidden = ("secret", "password", "api_key", "apikey", "token", "jwt", "value")
    for name in fields:
        assert not any(word in name for word in forbidden), name
    # Nor can a request smuggle one in: the body is sealed and names no credential field.
    assert CompanionInstallRequest.model_config.get("extra") == "forbid"
    assert set(CompanionInstallRequest.model_fields) == {
        "agent_id",
        "companion_key",
        "isolation_tier",
        "quote_id",
    }


# ---------------------------------------------------------------------------
# The catalog, the zone and every refusal (Task 3).
# ---------------------------------------------------------------------------

CATALOG_PATH = "/v1/companions/catalog"
LABEL_RE = re.compile(r"^postiz-[a-z2-7]{12}$")


async def _other_tenant(db_session) -> User:
    """A second tenant with the minimum graph an agent needs (copied, not imported, per suite)."""
    other = User(
        clerk_id=f"test_other_{uuid.uuid4().hex[:12]}",
        email=f"other-{uuid.uuid4().hex[:12]}@iforge.local",
        name="Other Tenant",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        Project(user_id=other.id, name="Personal", slug="personal", kind=PROJECT_KIND_PERSONAL)
    )
    await db_session.commit()
    return other


async def _partner_tenant(db_session) -> User:
    """A partner-tenant principal: no Clerk id, so no hosted owner on the /v2 wire (06.2 D-09)."""
    ref = uuid.uuid4().hex[:12]
    partner = User(
        principal_kind=PRINCIPAL_KIND_PARTNER_TENANT,
        clerk_id=None,
        clerk_issuer=None,
        partner_tenant_ref=f"partner-companion-{ref}",
        email=f"partner-companion-{ref}@iforge.local",
    )
    db_session.add(partner)
    await db_session.flush()
    return partner


@contextmanager
def _signed_in_as(user: User) -> Iterator[None]:
    """The dashboard ``client`` signed in as ``user`` for the block, restored after it."""
    previous = app.dependency_overrides.get(get_auth)
    app.dependency_overrides[get_auth] = lambda: AuthContext(user=user)
    try:
        yield
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_auth, None)
        else:
            app.dependency_overrides[get_auth] = previous


async def _catalog_item(client, key: str = "postiz") -> dict:
    response = await client.get(CATALOG_PATH)
    assert response.status_code == 200, response.text
    items = {item["companion_key"]: item for item in response.json()["items"]}
    assert key in items, sorted(items)
    return items[key]


async def test_catalog_lists_postiz_with_one_offered_tier(client) -> None:
    postiz = await _catalog_item(client)

    assert postiz["display_name"] == "Postiz"
    assert postiz["version"] == "v2.24.0"
    assert {tier["tier"]: tier["offered"] for tier in postiz["tiers"]} == {
        "standard": True,
        "dedicated": False,
        "shared": False,
    }


async def test_catalog_postgres_pin_matches_the_repository_authority(client) -> None:
    authority = (REPO_ROOT / "config/postgres-image.txt").read_text(encoding="utf-8").strip()
    reference, digest = authority.split("@sha256:")
    repository, tag = reference.rsplit(":", 1)

    postgres = (await _catalog_item(client))["images"]["postgres"]

    assert postgres == {"image": repository, "tag": tag, "sha256": digest}


async def test_catalog_images_are_the_manifest_contract_repositories(client) -> None:
    """Every pinned image is the repository the postiz manifest contract accepts (plan 06-05)."""
    images = (await _catalog_item(client))["images"]

    assert images["app"]["image"] == POSTIZ_APP_IMAGE
    assert images["postgres"]["image"] == POSTIZ_POSTGRES_IMAGE
    assert images["redis"]["image"] == POSTIZ_REDIS_IMAGE
    assert images["cloudflared"]["image"] == POSTIZ_CLOUDFLARED_IMAGE
    for slot in images.values():
        assert re.fullmatch(r"[a-f0-9]{64}", slot["sha256"]), slot


async def test_install_pins_the_catalog_app_digest(
    client, db_session, seed_user, companion_zone, quote_id
) -> None:
    postiz = await _catalog_item(client)
    agent = await _agent(db_session, seed_user.id)

    response = await client.post(INSTALLATIONS_PATH, json=_install_body(agent.id, quote_id))

    assert response.status_code == 202, response.text
    assert response.json()["pinned_digest"] == postiz["images"]["app"]["sha256"]
    assert response.json()["version"] == postiz["version"]


@pytest.mark.parametrize("tier", ["dedicated", "shared"])
async def test_unoffered_tier_is_refused_by_name(
    client, db_session, seed_user, companion_zone, quote_id, tier: str
) -> None:
    agent = await _agent(db_session, seed_user.id)

    response = await client.post(
        INSTALLATIONS_PATH, json=_install_body(agent.id, quote_id, tier=tier)
    )

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "companion_tier_not_offered"
    assert tier in detail["message"]
    assert await _installation_count(db_session, agent.id) == 0


async def test_unknown_companion_is_404(
    client, db_session, seed_user, companion_zone, quote_id
) -> None:
    agent = await _agent(db_session, seed_user.id)

    response = await client.post(
        INSTALLATIONS_PATH, json=_install_body(agent.id, quote_id, companion_key="twenty")
    )

    assert response.status_code == 404, response.text
    assert await _installation_count(db_session, agent.id) == 0


async def test_another_tenants_agent_is_404(
    client, db_session, seed_user, companion_zone, quote_id
) -> None:
    other = await _other_tenant(db_session)
    foreign_agent = await _agent(db_session, other.id)

    response = await client.post(INSTALLATIONS_PATH, json=_install_body(foreign_agent.id, quote_id))

    assert response.status_code == 404, response.text
    assert await _installation_count(db_session, foreign_agent.id) == 0


async def test_archived_agent_is_404(
    client, db_session, seed_user, companion_zone, quote_id
) -> None:
    agent = await _agent(db_session, seed_user.id)
    agent.archived_at = datetime.now(UTC)
    await db_session.commit()

    response = await client.post(INSTALLATIONS_PATH, json=_install_body(agent.id, quote_id))

    assert response.status_code == 404, response.text
    assert await _installation_count(db_session, agent.id) == 0


async def test_second_live_install_is_409(
    client, db_session, seed_user, companion_zone, quote_id
) -> None:
    agent = await _agent(db_session, seed_user.id)
    first = await client.post(INSTALLATIONS_PATH, json=_install_body(agent.id, quote_id))
    assert first.status_code == 202, first.text

    second = await client.post(INSTALLATIONS_PATH, json=_install_body(agent.id, quote_id))

    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "companion_already_installed"
    assert await _installation_count(db_session, agent.id) == 1


async def test_install_refused_without_a_companion_zone(
    client, db_session, seed_user, quote_id, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "companion_edge_zone_name", "")
    agent = await _agent(db_session, seed_user.id)

    response = await client.post(INSTALLATIONS_PATH, json=_install_body(agent.id, quote_id))

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "companion_edge_zone_unconfigured"
    assert await _installation_count(db_session, agent.id) == 0


async def test_install_requires_dashboard_auth(cli_client, db_session, seed_user, quote_id) -> None:
    """An agent's own CLI key cannot grant itself a new application."""
    agent = await _agent(db_session, seed_user.id)

    response = await cli_client.post(INSTALLATIONS_PATH, json=_install_body(agent.id, quote_id))

    assert response.status_code == 403, response.text


async def test_list_and_detail_are_tenant_scoped(
    client, db_session, seed_user, companion_zone, quote_id, monkeypatch: pytest.MonkeyPatch
) -> None:
    own_agent = await _agent(db_session, seed_user.id)
    own = await client.post(INSTALLATIONS_PATH, json=_install_body(own_agent.id, quote_id))
    assert own.status_code == 202, own.text
    own_id = own.json()["id"]

    other = await _other_tenant(db_session)
    foreign_agent = await _agent(db_session, other.id)
    foreign = await create_installation(
        db_session,
        auth=AuthContext(user=other),
        agent_id=foreign_agent.id,
        companion_key="postiz",
        isolation_tier="standard",
        quote_id=await quote_for(monkeypatch, other.clerk_id),
    )

    listed = await client.get(INSTALLATIONS_PATH)
    assert listed.status_code == 200, listed.text
    ids = {item["id"] for item in listed.json()["items"]}
    assert own_id in ids
    assert str(foreign.id) not in ids

    filtered = await client.get(INSTALLATIONS_PATH, params={"agent_id": str(own_agent.id)})
    assert [item["id"] for item in filtered.json()["items"]] == [own_id]
    foreign_filter = await client.get(
        INSTALLATIONS_PATH, params={"agent_id": str(foreign_agent.id)}
    )
    assert foreign_filter.status_code == 404, foreign_filter.text

    detail = await client.get(f"{INSTALLATIONS_PATH}/{own_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == own_id
    assert detail.json()["credential"] == "stored_in_vault"
    foreign_detail = await client.get(f"{INSTALLATIONS_PATH}/{foreign.id}")
    assert foreign_detail.status_code == 404, foreign_detail.text


async def test_hostname_label_is_unique_and_unguessable(
    client, db_session, seed_user, companion_zone, quote_id
) -> None:
    labels = []
    for index in range(2):
        agent = await _agent(db_session, seed_user.id, name=f"host-{index}")
        response = await client.post(INSTALLATIONS_PATH, json=_install_body(agent.id, quote_id))
        assert response.status_code == 202, response.text
        hostname = response.json()["hostname"]
        label, zone = hostname.split(".", 1)
        assert zone == companion_zone
        assert LABEL_RE.fullmatch(label), label
        # No tenant or agent identifier is ever placed in DNS.
        assert str(seed_user.id).split("-")[0] not in label
        assert agent.id.hex[:8] not in label
        labels.append(label)

    assert labels[0] != labels[1]


async def test_partner_tenant_quote_is_503_without_a_v2_call(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """06.2 D-09: a principal with no Clerk id has no hosted owner, so it is never quoted."""
    billing = route_billing_to_mock(monkeypatch)
    partner = await _partner_tenant(db_session)
    with _signed_in_as(partner):
        response = await client.post(
            "/v1/companions/quotes",
            json={"companion_key": "postiz", "isolation_tier": "standard"},
        )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "companion_billing_unavailable"
    assert billing.calls == []


async def test_partner_tenant_catalog_is_unpriced_without_a_v2_call(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """06.2 D-09: the catalog shows a partner tenant no price, never one under another owner."""
    billing = route_billing_to_mock(monkeypatch)
    partner = await _partner_tenant(db_session)
    with _signed_in_as(partner):
        postiz = await _catalog_item(client)

    assert postiz["from_monthly_price_usd_micros"] is None
    assert billing.calls == []


def test_purge_covers_the_three_companion_tables() -> None:
    assert PURGE_COVERAGE["companion_installations"] == "purge"
    assert PURGE_COVERAGE["companion_installation_secrets"] == (
        "via-parent:companion_installations"
    )
    assert PURGE_COVERAGE["companion_install_log_lines"] == ("via-parent:companion_installations")


async def _installation_count(db_session, agent_id) -> int:
    from app.models.companion_installation import CompanionInstallation

    return (
        await db_session.execute(
            select(func.count())
            .select_from(CompanionInstallation)
            .where(CompanionInstallation.environment_id == agent_id)
        )
    ).scalar_one()
