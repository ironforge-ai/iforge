"""The per-installation Temporal namespace step: clean, adopted on retry, and writer-only (06-13).

Runs against the same stateful fake as ``test_companion_media_store`` (``FakeOperatorApis``), whose
Temporal half models the frontend's HTTP API: RegisterNamespace, DescribeNamespace,
ListSearchAttributes and UpdateNamespace, with SQL visibility's alias allocation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import jwt
import pytest

from app.core.config import settings
from app.services import companion_temporal
from app.services.companion_lifecycle_steps import CompanionStepError
from app.services.companion_lifecycle_worker import CompanionLifecycleWorker
from app.services.companion_temporal import TEMPORAL_NAMESPACE_STEP
from tests import test_companion_media_store as media_tests
from tests.test_companion_install_job import _reload, _sessionmaker
from tests.test_companion_media_store import (
    BASE_ATOMS,
    TEMPORAL_FRONTEND_ADDRESS,
    TEMPORAL_KEY_ID,
    TEXT,
    FakeNamespace,
    _decrypted_atoms,
    _new_installation,
    _step_context,
    _step_resources,
    _suffix,
)

# The shared fixtures: the fake operator APIs, both adapters wired to them, and the fake
# operator settings with the test's own RS256 signing key.
signing_key = media_tests.signing_key
operator_settings = media_tests.operator_settings
fake_apis = media_tests.fake_apis
media_objects = media_tests.media_objects


async def test_retry_adopts_the_existing_namespace(
    db_session, seed_user, operator_settings, fake_apis, media_objects
) -> None:
    """A crashed attempt registered the namespace and one attribute; the retry finishes it."""
    installation = await _new_installation(db_session, seed_user)
    namespace = f"postiz-{_suffix(installation)}"
    fake_apis.namespaces[namespace] = FakeNamespace(
        name=namespace, retention="604800s", aliases={"Text01": "organizationId"}
    )
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session), provisioning_steps=(TEMPORAL_NAMESPACE_STEP,)
    )

    assert await worker.run_once() == installation.id

    row = await _reload(db_session, installation.id)
    assert row.status == "running", row.error
    assert fake_apis.created_namespace_count == 0
    assert set(fake_apis.namespaces) == {namespace}
    # Only the missing attribute was added, on the next free Text field.
    assert fake_apis.namespaces[namespace].aliases == {
        "Text01": "organizationId",
        "Text02": "postId",
    }
    assert _step_resources(row, "temporal") == {
        "namespace": namespace,
        "address": TEMPORAL_FRONTEND_ADDRESS,
        "tls": True,
    }
    assert "temporal-api-key" in await _decrypted_atoms(db_session, row.id)


async def test_a_retry_with_both_attributes_registered_adds_nothing(
    db_session, seed_user, operator_settings, fake_apis, media_objects
) -> None:
    installation = await _new_installation(db_session, seed_user)
    namespace = f"postiz-{_suffix(installation)}"
    fake_apis.namespaces[namespace] = FakeNamespace(
        name=namespace, aliases={"Text01": "organizationId", "Text02": "postId"}
    )

    resources = await companion_temporal.provision_temporal_namespace(
        _step_context(db_session, installation)
    )

    assert resources["namespace"] == namespace
    assert not [call for call in fake_apis.calls if call.endswith("/update")]


async def test_prepopulated_namespace_is_refused_by_name(
    db_session, seed_user, operator_settings, fake_apis, media_objects
) -> None:
    """An auto-setup-style namespace (two Text attributes) would fail Postiz's boot (F4)."""
    installation = await _new_installation(db_session, seed_user)
    namespace = f"postiz-{_suffix(installation)}"
    fake_apis.namespaces[namespace] = FakeNamespace(
        name=namespace, preexisting={"CustomStringField": TEXT, "CustomTextField": TEXT}
    )
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session), provisioning_steps=(TEMPORAL_NAMESPACE_STEP,)
    )

    assert await worker.run_once() == installation.id

    row = await _reload(db_session, installation.id)
    assert row.status == "failed"
    assert row.error is not None
    assert "temporal_namespace_not_clean" in row.error
    # No JWT was minted, nothing was added, and a namespace this step did not create is untouched.
    assert set(await _decrypted_atoms(db_session, row.id)) == BASE_ATOMS
    assert fake_apis.namespaces[namespace].aliases == {}
    assert fake_apis.namespaces[namespace].state == "NAMESPACE_STATE_REGISTERED"


async def test_a_deleted_namespace_is_never_adopted(
    db_session, seed_user, operator_settings, fake_apis, media_objects
) -> None:
    installation = await _new_installation(db_session, seed_user)
    namespace = f"postiz-{_suffix(installation)}"
    fake_apis.namespaces[namespace] = FakeNamespace(name=namespace, state="NAMESPACE_STATE_DELETED")

    with pytest.raises(CompanionStepError) as refusal:
        await companion_temporal.provision_temporal_namespace(
            _step_context(db_session, installation)
        )

    assert refusal.value.code == "temporal_namespace_unavailable"
    assert set(await _decrypted_atoms(db_session, installation.id)) == BASE_ATOMS


async def test_installation_jwt_is_writer_on_its_namespace_only(
    db_session, seed_user, operator_settings, fake_apis, media_objects, signing_key
) -> None:
    installation = await _new_installation(db_session, seed_user)
    namespace = f"postiz-{_suffix(installation)}"
    before = int(datetime.now(UTC).timestamp())

    await companion_temporal.provision_temporal_namespace(_step_context(db_session, installation))

    token = (await _decrypted_atoms(db_session, installation.id))["temporal-api-key"]
    assert jwt.get_unverified_header(token)["kid"] == TEMPORAL_KEY_ID
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(token, signing_key[1], algorithms=["RS256"])
    assert claims["permissions"] == [f"{namespace}:writer"]
    assert claims["sub"] == f"postiz:{installation.id}"
    # One year, not forever.
    assert before + 364 * 86400 <= claims["exp"] <= before + 366 * 86400


TEMPORAL_SETTINGS = (
    ("companion_temporal_frontend_address", "COMPANION_TEMPORAL_FRONTEND_ADDRESS"),
    ("companion_temporal_http_url", "COMPANION_TEMPORAL_HTTP_URL"),
    ("companion_temporal_jwt_private_key", "COMPANION_TEMPORAL_JWT_PRIVATE_KEY"),
    ("companion_temporal_jwt_key_id", "COMPANION_TEMPORAL_JWT_KEY_ID"),
)
# The teardown dials only the HTTP API; the address tenant VMs use is not needed to delete.
TEARDOWN_UNNEEDED = {"companion_temporal_frontend_address"}


@pytest.mark.parametrize(("setting", "env_name"), TEMPORAL_SETTINGS)
async def test_missing_settings_fail_the_step_before_any_call(
    db_session,
    seed_user,
    operator_settings,
    fake_apis,
    media_objects,
    monkeypatch,
    setting: str,
    env_name: str,
) -> None:
    installation = await _new_installation(db_session, seed_user)
    monkeypatch.setattr(settings, setting, "")
    context = _step_context(db_session, installation)

    with pytest.raises(CompanionStepError) as provision_refusal:
        await companion_temporal.provision_temporal_namespace(context)
    assert provision_refusal.value.code == "temporal_settings_missing"
    assert env_name in str(provision_refusal.value)

    if setting not in TEARDOWN_UNNEEDED:
        with pytest.raises(CompanionStepError) as teardown_refusal:
            await companion_temporal.teardown_temporal_namespace(
                context, {"namespace": "postiz-x", "address": "a", "tls": True}
            )
        assert teardown_refusal.value.code == "temporal_settings_missing"

    assert fake_apis.calls == []
