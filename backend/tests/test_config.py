import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from pydantic import ValidationError

from app.core.config import Settings


def _public_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )


def test_settings_normalizes_literal_escaped_clerk_pem_newlines():
    pem = _public_pem()
    settings = Settings(clerk_pem_public_key=pem.replace("\n", "\\n"))

    assert settings.clerk_pem_public_key == pem
    load_pem_public_key(settings.clerk_pem_public_key.encode("utf-8"))


def test_settings_decodes_base64_clerk_pem():
    pem = _public_pem()
    encoded = base64.b64encode(pem.encode("utf-8")).decode("ascii")

    settings = Settings(clerk_pem_public_key=encoded)

    assert settings.clerk_pem_public_key == pem
    load_pem_public_key(settings.clerk_pem_public_key.encode("utf-8"))


def test_settings_normalizes_line_continuation_clerk_pem_newlines():
    pem = _public_pem()
    settings = Settings(clerk_pem_public_key=pem.replace("\n", "\\" + "\n"))

    assert settings.clerk_pem_public_key == pem
    load_pem_public_key(settings.clerk_pem_public_key.encode("utf-8"))


def test_settings_normalizes_line_continuation_clerk_pem_from_env(monkeypatch):
    pem = _public_pem()
    monkeypatch.setenv("CLERK_PEM_PUBLIC_KEY", pem.replace("\n", "\\" + "\n"))

    settings = Settings(_env_file=None)

    assert settings.clerk_pem_public_key == pem
    load_pem_public_key(settings.clerk_pem_public_key.encode("utf-8"))


def test_settings_redacts_whatsapp_sidecar_service_token():
    settings = Settings(
        _env_file=None,
        channel_whatsapp_baileys_sidecar_token="sidecar-secret",
    )

    assert settings.channel_whatsapp_baileys_sidecar_token.get_secret_value() == "sidecar-secret"
    assert "sidecar-secret" not in repr(settings)


def test_plugin_catalog_sync_is_deployment_opt_in(monkeypatch):
    monkeypatch.delenv("PLUGIN_CATALOG_SYNC_ENABLED", raising=False)
    assert Settings(_env_file=None).plugin_catalog_sync_enabled is False

    monkeypatch.setenv("PLUGIN_CATALOG_SYNC_ENABLED", "true")
    assert Settings(_env_file=None).plugin_catalog_sync_enabled is True


def test_settings_canonicalizes_browser_clerk_issuer():
    settings = Settings(_env_file=None, clerk_jwt_issuer="  https://Clerk.Example.test/ ")

    assert settings.clerk_jwt_issuer == "https://clerk.example.test"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://Clerk.Example.test", "https://clerk.example.test"),
        ("https://Clerk.Example.test:443/", "https://clerk.example.test"),
        ("https://Clerk.Example.test:8443/", "https://clerk.example.test:8443"),
        ("http://LOCALHOST:80/", "http://localhost"),
        ("http://127.0.0.1:43119/", "http://127.0.0.1:43119"),
        ("http://[::1]:43120/", "http://[::1]:43120"),
        ("http://[0:0:0:0:0:0:0:1]:80/", "http://[::1]"),
        ("https://BÜCHER.example:443/", "https://xn--bcher-kva.example"),
        ("https://faß.example/", "https://xn--fa-hia.example"),
        ("https://[2001:0DB8:0:0:0:0:0:1]:443/", "https://[2001:db8::1]"),
    ],
)
def test_settings_canonicalizes_clerk_issuer_origins(value: str, expected: str):
    settings = Settings(_env_file=None, clerk_jwt_issuer=value)

    assert settings.clerk_jwt_issuer == expected


@pytest.mark.parametrize(
    "value",
    [
        "clerk.example.test",
        "ftp://clerk.example.test",
        "http://clerk.example.test",
        "http://localhost.example.test",
        "http://127.0.0.2",
        "http://[::2]",
        "https://user@clerk.example.test",
        "https://clerk.example.test/oauth",
        "https://clerk.example.test///",
        "https://clerk.example.test?tenant=secret",
        "https://clerk.example.test#fragment",
        "https://clerk.example.test?",
        "https://clerk.example.test#",
        "https://clerk.example.test.",
        "https://bad_host.example.test",
        "https://-bad.example.test",
        "https://[2001:db8::gg]",
    ],
)
def test_settings_rejects_invalid_clerk_issuer_origins(value: str):
    with pytest.raises(ValidationError, match="Clerk issuer"):
        Settings(_env_file=None, clerk_jwt_issuer=value)


@pytest.mark.parametrize(
    "field",
    [
        "runtime_observation_freshness_seconds",
        "runtime_observation_max_future_skew_seconds",
        "runtime_observation_max_capture_age_days",
        "runtime_observation_replay_horizon_days",
        "runtime_observation_hard_retention_days",
        "runtime_observation_cleanup_batch_size",
    ],
)
def test_runtime_observation_settings_require_positive_bounds(field: str):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


def test_runtime_observation_freshness_allows_two_heartbeat_intervals(monkeypatch):
    monkeypatch.delenv("RUNTIME_OBSERVATION_FRESHNESS_SECONDS", raising=False)
    assert Settings(_env_file=None).runtime_observation_freshness_seconds == 150


def test_runtime_observation_hard_retention_cannot_precede_replay_horizon():
    with pytest.raises(ValidationError, match="hard_retention_days"):
        Settings(
            _env_file=None,
            runtime_observation_replay_horizon_days=8,
            runtime_observation_hard_retention_days=7,
        )


@pytest.mark.parametrize(
    "field",
    [
        "channel_message_retention_days",
        "channel_unbound_message_retention_hours",
        "channel_message_cleanup_batch_size",
        "channel_message_cleanup_max_batches",
        "channel_message_stuck_pending_hours",
    ],
)
def test_channel_retention_settings_require_positive_bounds(field: str):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channel_message_retention_days", 3_651),
        ("channel_unbound_message_retention_hours", 87_601),
        ("channel_message_cleanup_batch_size", 10_001),
        ("channel_message_cleanup_max_batches", 1_001),
        ("channel_message_stuck_pending_hours", 8_761),
    ],
)
def test_channel_retention_settings_reject_unbounded_work(field: str, value: int):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


# --------------------------------------------------------------------------
# The two settings that protect the one-way tenant purge (CR-02, WR-09).
#
# Both are read LATE — `tenant_purge_grace_days` by `admin_delete_tenant`
# when it schedules the irreversible sweep, `user_prov` by
# `get_user_provisioning_provider()` AFTER that fence-and-purge has already
# committed. An invalid value therefore first surfaces as a half-applied,
# irreversible delete. These tests pin the refusal at settings load instead.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, -1, -30, 366, 100_000])
def test_tenant_purge_grace_days_rejects_uncancellable_or_unbounded_windows(value: int):
    # 0/negative make the purge row due the instant it commits, so the
    # 1-second worker poll runs the one-way sweep before an operator could
    # cancel; an unbounded upper end parks a tombstone row in the claim
    # query effectively forever.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, tenant_purge_grace_days=value)


@pytest.mark.parametrize("value", [1, 30, 365])
def test_tenant_purge_grace_days_accepts_in_bounds_windows(value: int):
    assert Settings(_env_file=None, tenant_purge_grace_days=value).tenant_purge_grace_days == value


def test_tenant_purge_grace_days_default_is_unchanged():
    assert Settings(_env_file=None).tenant_purge_grace_days == 30


def test_tenant_purge_grace_days_rejected_through_the_environment(monkeypatch):
    # The case that actually happens in production: pydantic-settings parses
    # the env string through the same validator.
    monkeypatch.setenv("TENANT_PURGE_GRACE_DAYS", "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", ["clrek", "Clerk", "", "postgres", "supabase"])
def test_user_prov_rejects_values_the_seam_cannot_dispatch(value: str):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, user_prov=value)


def test_user_prov_rejects_supa_until_phase_3_1_lands():
    with pytest.raises(ValidationError, match="3.1"):
        Settings(_env_file=None, user_prov="supa")


def test_user_prov_supa_rejected_through_the_environment(monkeypatch):
    monkeypatch.setenv("USER_PROV", "supa")

    with pytest.raises(ValidationError, match="3.1"):
        Settings(_env_file=None)


def test_user_prov_default_is_clerk_and_still_resolves_the_provider():
    from app.services.user_prov import get_user_provisioning_provider
    from app.services.user_prov.clerk import ClerkUserProvisioningProvider

    assert Settings(_env_file=None).user_prov == "clerk"
    assert Settings(_env_file=None, user_prov="clerk").user_prov == "clerk"
    # The factory's own ValueError / NotImplementedError arms are untouched —
    # they stay defence in depth for a Settings object built elsewhere.
    assert isinstance(get_user_provisioning_provider(), ClerkUserProvisioningProvider)


# --------------------------------------------------------------------------
# Pytest report header: which database is under test, and from where
#
# The header exists because `TEST_DATABASE_URL = os.getenv("DATABASE_URL",
# settings.database_url)` fell back to the application's own database
# SILENTLY. `scripts/test.sh backend` provisions a migrated throwaway
# Postgres and never sees it, but a direct `uv run pytest` against a
# developer's shared database produces count-assertion failures that read as
# product defects (03-VERIFICATION.md I-03, deferred-items.md items 2 and 3).
#
# The content lives in a PURE helper so it is gateable: a deliverable
# verified by "read the header manually" passes identically whether the
# header is right, wrong or absent.
# --------------------------------------------------------------------------

_LEAKY_URL = (
    "postgresql+asyncpg://clawdi_admin:sup3r-s3cret-pw@db.internal.example:6543"
    "/clawdi_prod?sslmode=require&application_name=clawdi"
)
_LEAKED_LITERALS = (
    "clawdi_admin",
    "sup3r-s3cret-pw",
    "clawdi_admin:sup3r-s3cret-pw",
    "@db.internal.example",
    "sslmode",
    "require",
    "application_name",
    "?",
)


def test_database_header_names_the_database_and_the_explicit_source():
    from tests.conftest import DATABASE_URL_SOURCE_ENV, database_header_lines

    lines = database_header_lines(
        "postgresql+asyncpg://someone@db.example.test:5433/clawdi_test",
        source=DATABASE_URL_SOURCE_ENV,
    )
    blob = "\n".join(lines)

    assert "db.example.test" in blob
    assert "5433" in blob
    assert "clawdi_test" in blob
    assert "DATABASE_URL" in blob
    # The explicit path is the CI path; it must add no noise.
    assert "throwaway" not in blob.lower()
    assert "warning" not in blob.lower()


def test_database_header_warns_only_on_the_application_default_source():
    from tests.conftest import (
        DATABASE_URL_SOURCE_APP_DEFAULT,
        SHARED_DATABASE_WARNING_MARKER,
        database_header_lines,
    )

    url = "postgresql+asyncpg://someone@db.example.test:5433/clawdi_test"
    lines = database_header_lines(url, source=DATABASE_URL_SOURCE_APP_DEFAULT)
    blob = "\n".join(lines)

    # Same database, named the same way.
    assert "db.example.test" in blob
    assert "5433" in blob
    assert "clawdi_test" in blob
    # ...but sourced from the application default, and said so.
    assert SHARED_DATABASE_WARNING_MARKER in blob
    assert "throwaway" in blob.lower()
    assert "count-assertion" in blob.lower()


@pytest.mark.parametrize(
    "source_name", ["DATABASE_URL_SOURCE_ENV", "DATABASE_URL_SOURCE_APP_DEFAULT"]
)
def test_database_header_never_leaks_userinfo_or_query_string(source_name: str):
    """T-03-69: this header goes into CI logs.

    The URL carries a real username, a real password and two query
    parameters. None of those literals may appear on either path — and the
    host, port and database name must, so the assertion cannot pass merely
    because the parser dropped everything.
    """
    from tests import conftest as tests_conftest

    lines = tests_conftest.database_header_lines(
        _LEAKY_URL, source=getattr(tests_conftest, source_name)
    )
    blob = "\n".join(lines)

    for leaked in _LEAKED_LITERALS:
        assert leaked not in blob, leaked
    # The header still identifies the database it is warning about.
    assert "db.internal.example" in blob
    assert "6543" in blob
    assert "clawdi_prod" in blob


@pytest.mark.parametrize(
    "url",
    ["", "not a url at all", "postgresql://host:not-a-port/db", "://", "postgresql+asyncpg://"],
)
def test_database_header_degrades_on_an_unparseable_url_without_raising(url: str):
    """A header hook that raises takes the whole session with it."""
    from tests.conftest import DATABASE_URL_SOURCE_APP_DEFAULT, database_header_lines

    lines = database_header_lines(url, source=DATABASE_URL_SOURCE_APP_DEFAULT)

    assert lines and all(isinstance(line, str) for line in lines)
    assert "not-a-port" not in "\n".join(lines)


def test_pytest_report_header_is_the_helper_over_the_real_resolved_source():
    """Wiring: the hook returns the helper's output verbatim, so the content
    tests above gate the SHIPPED header rather than a parallel implementation.
    """
    import os

    from app.core.config import settings
    from tests.conftest import (
        DATABASE_URL_SOURCE_APP_DEFAULT,
        DATABASE_URL_SOURCE_ENV,
        SHARED_DATABASE_WARNING_MARKER,
        TEST_DATABASE_URL,
        TEST_DATABASE_URL_SOURCE,
        database_header_lines,
        pytest_report_header,
    )

    lines = pytest_report_header(None)
    assert lines == database_header_lines(TEST_DATABASE_URL, source=TEST_DATABASE_URL_SOURCE)

    # The resolution rule itself, checkable on BOTH lanes.
    explicit = os.getenv("DATABASE_URL")
    if explicit is None:
        assert TEST_DATABASE_URL_SOURCE == DATABASE_URL_SOURCE_APP_DEFAULT
        assert TEST_DATABASE_URL == settings.database_url
    else:
        assert TEST_DATABASE_URL_SOURCE == DATABASE_URL_SOURCE_ENV
        assert TEST_DATABASE_URL == explicit

    # The CI-noise regression: under scripts/test.sh backend, DATABASE_URL is
    # set by the compose lane and the warning must NOT appear.
    warned = SHARED_DATABASE_WARNING_MARKER in "\n".join(lines)
    assert warned is (TEST_DATABASE_URL_SOURCE == DATABASE_URL_SOURCE_APP_DEFAULT)
