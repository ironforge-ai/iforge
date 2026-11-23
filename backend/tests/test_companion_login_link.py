"""The tenant's own Postiz sign-in link, and the measured reachability read-out (APP-02, APP-03).

CK-D (plan 06-09) settled how a person signs into their own Postiz without ever seeing the
password the platform generated: the control plane mints Postiz's own set-password link,
``https://<hostname>/auth/forgot/<jwt>``, signed with the installation's ``jwt-secret`` atom, and
the human chooses a password there (research Pattern 5). These tests decode that JWT with the
decrypted atom -- the same key Postiz verifies with -- so a link signed with anything else, or
carrying the wrong user or expiry, fails here rather than at a person's browser.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import jwt
import pytest
from sqlalchemy import select

from app.core.auth import AuthContext
from app.core.config import settings
from app.models.audit import ControlPlaneAuditEvent
from app.models.companion_installation import CompanionInstallation, CompanionInstallationSecret
from app.models.project import PROJECT_KIND_PERSONAL, Project
from app.models.user import User
from app.services.companion_install import create_installation
from app.services.vault_crypto import decrypt
from tests.companion_billing_stub import billing_quote
from tests.conftest import create_env_with_project

ZONE = "companions.example"
INSTALLATIONS_PATH = "/v1/companions/installations"
ADMIN_USER_ID = "postiz-admin-7f3c"
EXPIRES_FORMAT = "%Y-%m-%d %H:%M:%S"


@pytest.fixture
def companion_zone(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "companion_edge_zone_name", ZONE)
    return ZONE


async def _requested_installation(client, db_session, user_id) -> CompanionInstallation:
    """One installation requested through the install route, as its owner would request it."""
    agent = await create_env_with_project(
        db_session,
        user_id=user_id,
        machine_id=f"machine-{uuid.uuid4().hex[:12]}",
        machine_name="postiz-host",
    )
    async with billing_quote((await db_session.get(User, user_id)).clerk_id) as quote_id:
        response = await client.post(
            INSTALLATIONS_PATH,
            json={
                "agent_id": str(agent.id),
                "companion_key": "postiz",
                "isolation_tier": "standard",
                "quote_id": quote_id,
            },
        )
    assert response.status_code == 202, response.text
    return await _reload(db_session, uuid.UUID(response.json()["id"]))


async def _reload(db_session, installation_id: uuid.UUID) -> CompanionInstallation:
    return (
        await db_session.execute(
            select(CompanionInstallation)
            .where(CompanionInstallation.id == installation_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _complete(db_session, installation: CompanionInstallation) -> CompanionInstallation:
    """Drive the job to where the lifecycle worker leaves it: complete, with the admin user id.

    The ready report records ``admin_user_id`` (plan 06-11) and the worker then marks the job
    complete (plan 06-12); both are written directly here, as the install-job suite does, because
    this suite's subject is what the dashboard can do AFTER that point.
    """
    installation.external_resources = {
        **(installation.external_resources or {}),
        "admin_user_id": ADMIN_USER_ID,
    }
    installation.status = "complete"
    installation.step = None
    installation.completed_at = datetime.now(UTC)
    await db_session.commit()
    return await _reload(db_session, installation.id)


async def _atoms(db_session, installation_id: uuid.UUID) -> dict[str, str]:
    rows = (
        (
            await db_session.execute(
                select(CompanionInstallationSecret).where(
                    CompanionInstallationSecret.installation_id == installation_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.secret_name: decrypt(row.encrypted_value, row.nonce) for row in rows}


def _login_link_path(installation_id) -> str:
    return f"{INSTALLATIONS_PATH}/{installation_id}/login-link"


async def test_login_link_is_a_twenty_minute_jwt_signed_with_the_installation_secret(
    client, db_session, seed_user, companion_zone
) -> None:
    installation = await _complete(
        db_session, await _requested_installation(client, db_session, seed_user.id)
    )
    atoms = await _atoms(db_session, installation.id)

    before = datetime.now(UTC).replace(microsecond=0)
    response = await client.post(_login_link_path(installation.id))
    after = datetime.now(UTC)

    assert response.status_code == 200, response.text
    body = response.json()
    url = urlsplit(body["url"])
    assert url.scheme == "https"
    assert url.hostname == installation.hostname
    prefix = "/auth/forgot/"
    assert url.path.startswith(prefix), url.path
    token = url.path.removeprefix(prefix)

    # Verified with the installation's OWN jwt-secret: the key Postiz itself verifies with. A link
    # signed with any other key raises here.
    payload = jwt.decode(token, atoms["jwt-secret"], algorithms=["HS256"])
    assert payload["id"] == ADMIN_USER_ID
    expires = datetime.strptime(payload["expires"], EXPIRES_FORMAT).replace(tzinfo=UTC)
    assert before + timedelta(minutes=20) <= expires <= after + timedelta(minutes=20)
    assert datetime.fromisoformat(body["expires_at"]) == expires


# ---------------------------------------------------------------------------
# Refusals, redaction and the audit trail (Task 2).
# ---------------------------------------------------------------------------

MINTED = "companion.login_link.minted"


async def _minted_events(db_session) -> list[ControlPlaneAuditEvent]:
    return list(
        (
            await db_session.execute(
                select(ControlPlaneAuditEvent).where(ControlPlaneAuditEvent.action == MINTED)
            )
        )
        .scalars()
        .all()
    )


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


async def test_login_link_before_completion_is_409(
    client, db_session, seed_user, companion_zone
) -> None:
    """No link until the data-asserted ready report and completion (T-06-34-03)."""
    installation = await _requested_installation(client, db_session, seed_user.id)
    assert installation.status == "queued"

    response = await client.post(_login_link_path(installation.id))

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "companion_not_ready"
    assert "/auth/forgot/" not in response.text
    assert await _minted_events(db_session) == []

    # Running at the ready step, with the admin user id already recorded, is still not complete.
    installation.status = "running"
    installation.step = "ready"
    installation.external_resources = {"admin_user_id": ADMIN_USER_ID}
    await db_session.commit()
    running = await client.post(_login_link_path(installation.id))
    assert running.status_code == 409, running.text
    assert await _minted_events(db_session) == []


async def test_login_link_for_another_tenant_is_404(
    client, db_session, seed_user, companion_zone
) -> None:
    """A foreign installation is 404, never 403, even when it is complete (T-06-34-01)."""
    other = await _other_tenant(db_session)
    foreign_agent = await create_env_with_project(
        db_session,
        user_id=other.id,
        machine_id=f"machine-{uuid.uuid4().hex[:12]}",
        machine_name="their-host",
    )
    async with billing_quote(other.clerk_id) as quote_id:
        foreign = await create_installation(
            db_session,
            auth=AuthContext(user=other),
            agent_id=foreign_agent.id,
            companion_key="postiz",
            isolation_tier="standard",
            quote_id=quote_id,
        )
    await _complete(db_session, foreign)

    response = await client.post(_login_link_path(foreign.id))

    assert response.status_code == 404, response.text
    assert "/auth/forgot/" not in response.text
    assert await _minted_events(db_session) == []

    # Both halves: the caller's own complete installation still gets its link.
    own = await _complete(
        db_session, await _requested_installation(client, db_session, seed_user.id)
    )
    assert (await client.post(_login_link_path(own.id))).status_code == 200


async def test_login_link_response_contains_no_password(
    client, db_session, seed_user, companion_zone
) -> None:
    """Only url, expires_at and sign_in_email: no generated credential ever leaves (T-06-34-02)."""
    installation = await _complete(
        db_session, await _requested_installation(client, db_session, seed_user.id)
    )
    atoms = await _atoms(db_session, installation.id)
    assert set(atoms) >= {"admin-password", "api-key", "jwt-secret", "db-password"}

    response = await client.post(_login_link_path(installation.id))

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"url", "expires_at", "sign_in_email"}
    assert body["sign_in_email"] == installation.owner_email
    for name, value in atoms.items():
        assert value not in response.text, f"{name} leaked into the login-link response"


async def test_login_link_is_audited_without_the_token(
    client, db_session, seed_user, companion_zone
) -> None:
    """One event per mint, naming the installation, with no URL, JWT or atom in it (T-06-34-04)."""
    installation = await _complete(
        db_session, await _requested_installation(client, db_session, seed_user.id)
    )
    atoms = await _atoms(db_session, installation.id)

    response = await client.post(_login_link_path(installation.id))
    assert response.status_code == 200, response.text
    url = response.json()["url"]
    token = urlsplit(url).path.removeprefix("/auth/forgot/")

    events = await _minted_events(db_session)
    assert len(events) == 1
    event = events[0]
    assert event.resource_id == str(installation.id)
    assert event.actor_user_id == seed_user.id
    assert event.environment_id == installation.environment_id
    serialized = json.dumps(
        {
            "details": event.details,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "source": event.source,
            "action": event.action,
        }
    )
    assert token not in serialized
    assert url not in serialized
    for name, value in atoms.items():
        assert value not in serialized, f"{name} leaked into the audit event"
    # Only the installation is named: nothing token-shaped, not even redacted.
    assert "[REDACTED]" not in serialized


# ---------------------------------------------------------------------------
# The measured reachability read-out (APP-02).
# ---------------------------------------------------------------------------


async def _record_probe(db_session, installation, *, state: str, checked_at: datetime) -> None:
    """Record a probe result exactly where plan 06-14's lifecycle worker writes it."""
    resources = dict(installation.external_resources or {})
    resources["edge"] = {
        **dict(resources.get("edge") or {}),
        "reachability": {"state": state, "checked_at": checked_at.isoformat()},
    }
    installation.external_resources = resources
    await db_session.commit()


@pytest.mark.parametrize("state", ["reachable", "tls_pending", "not_answering"])
async def test_installation_response_carries_the_recorded_reachability(
    client, db_session, seed_user, companion_zone, state
) -> None:
    installation = await _complete(
        db_session, await _requested_installation(client, db_session, seed_user.id)
    )
    checked_at = datetime(2026, 9, 23, 12, 34, 56, tzinfo=UTC)
    await _record_probe(db_session, installation, state=state, checked_at=checked_at)

    detail = await client.get(f"{INSTALLATIONS_PATH}/{installation.id}")
    assert detail.status_code == 200, detail.text
    reachability = detail.json()["reachability"]
    assert reachability["state"] == state
    assert datetime.fromisoformat(reachability["checked_at"]) == checked_at

    listed = await client.get(INSTALLATIONS_PATH)
    assert listed.status_code == 200, listed.text
    [item] = [item for item in listed.json()["items"] if item["id"] == str(installation.id)]
    assert item["reachability"] == reachability


async def test_reachability_is_no_hostname_yet_before_completion(
    client, db_session, seed_user, companion_zone
) -> None:
    """Never inferred: before completion, and before any probe, the state is no_hostname_yet."""
    agent = await create_env_with_project(
        db_session,
        user_id=seed_user.id,
        machine_id=f"machine-{uuid.uuid4().hex[:12]}",
        machine_name="postiz-host",
    )
    async with billing_quote(seed_user.clerk_id) as quote_id:
        created = await client.post(
            INSTALLATIONS_PATH,
            json={
                "agent_id": str(agent.id),
                "companion_key": "postiz",
                "isolation_tier": "standard",
                "quote_id": quote_id,
            },
        )
    assert created.status_code == 202, created.text
    no_hostname_yet = {"state": "no_hostname_yet", "checked_at": None}
    assert created.json()["reachability"] == no_hostname_yet

    installation = await _reload(db_session, uuid.UUID(created.json()["id"]))
    # A stale probe result on a not-yet-complete installation is not reported as measured.
    await _record_probe(db_session, installation, state="reachable", checked_at=datetime.now(UTC))
    detail = await client.get(f"{INSTALLATIONS_PATH}/{installation.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["reachability"] == no_hostname_yet

    # Complete, but no probe has run yet: still no_hostname_yet.
    fresh = await _complete(
        db_session, await _requested_installation(client, db_session, seed_user.id)
    )
    assert "edge" not in (fresh.external_resources or {})
    fresh_detail = await client.get(f"{INSTALLATIONS_PATH}/{fresh.id}")
    assert fresh_detail.json()["reachability"] == no_hostname_yet


async def test_login_link_is_minted_for_an_installation_rotated_to_a_newer_key(
    client, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    # WINDOWS #210: after plan 06-25's rotation an installation's atoms carry a key_version other
    # than vault.v1. The link must be signed with the atom decrypted under the row's own version,
    # not refused as unreadable.
    import secrets as _secrets

    from app.services import vault_crypto

    installation = await _complete(
        db_session, await _requested_installation(client, db_session, seed_user.id)
    )
    jwt_secret = (await _atoms(db_session, installation.id))["jwt-secret"]

    monkeypatch.setitem(
        settings.__dict__, "vault_encryption_keys", {"vault.v2": _secrets.token_hex(32)}
    )
    monkeypatch.setitem(settings.__dict__, "vault_active_key_version", "vault.v2")
    ciphertext, nonce, version = vault_crypto.encrypt_with_active_key(jwt_secret)
    assert version == "vault.v2"
    row = (
        await db_session.execute(
            select(CompanionInstallationSecret).where(
                CompanionInstallationSecret.installation_id == installation.id,
                CompanionInstallationSecret.secret_name == "jwt-secret",
            )
        )
    ).scalar_one()
    row.encrypted_value, row.nonce, row.key_version = ciphertext, nonce, version
    await db_session.commit()

    response = await client.post(_login_link_path(installation.id))

    assert response.status_code == 200, response.text
    token = urlsplit(response.json()["url"]).path.removeprefix("/auth/forgot/")
    assert jwt.decode(token, jwt_secret, algorithms=["HS256"])["id"] == ADMIN_USER_ID
