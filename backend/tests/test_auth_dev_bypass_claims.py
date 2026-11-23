"""The dev bypass on the CLAIMS-only auth dependency.

``require_clerk_claims`` verifies a Clerk JWT without provisioning, and serves
the one route that exists for identities with no ``users`` row yet:
``GET /access/status``, its only claims-only consumer. Before this branch existed
it consulted only ``_verify_clerk_token``, so ``dev-bypass`` -- honoured by every
route on ``get_auth`` -- failed signature verification here and returned 401.
The dashboard's access gate runs on this dependency, so the documented local
bypass could authenticate the whole API except the gate in front of it.

These tests pin the guards rather than the happy path alone: a bypass that
matched in staging or production, or that matched the wrong token, would be a
route around authentication rather than a development convenience.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.auth import _dev_bypass_claims
from app.core.config import settings


def test_returns_the_configured_dev_principals_claims_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    monkeypatch.setattr(settings, "environment", "development")

    claims = _dev_bypass_claims(settings.dev_auth_token)

    assert claims is not None
    assert claims.subject == settings.dev_auth_clerk_id
    assert claims.email == settings.dev_auth_email
    assert claims.name == settings.dev_auth_name


def test_carries_no_issuer_because_nothing_verified_one(monkeypatch):
    """An unverified identity must stay distinguishable from a verified one.

    The JWT path fills ``issuer`` from a signed ``iss`` it checked. The bypass
    has no signature to read, so inventing a value here would hand every
    downstream reader a claim nothing established.
    """
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    monkeypatch.setattr(settings, "environment", "development")

    claims = _dev_bypass_claims(settings.dev_auth_token)

    assert claims is not None
    assert claims.issuer is None


def test_declines_when_the_bypass_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "dev_auth_bypass", False)
    monkeypatch.setattr(settings, "environment", "development")

    assert _dev_bypass_claims(settings.dev_auth_token) is None


def test_declines_a_token_that_is_not_the_configured_one(monkeypatch):
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    monkeypatch.setattr(settings, "environment", "development")

    assert _dev_bypass_claims("not-the-dev-token") is None


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_raises_rather_than_declining_outside_development(monkeypatch, environment):
    """A bypass enabled outside development is a misconfiguration, not a miss.

    Returning ``None`` would fall through to the JWT path and quietly serve a
    401 -- indistinguishable from an ordinary bad token, and the deployment
    would keep running with the bypass armed. 500 is what gets it noticed.
    """
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    monkeypatch.setattr(settings, "environment", environment)

    with pytest.raises(HTTPException) as excinfo:
        _dev_bypass_claims(settings.dev_auth_token)

    assert excinfo.value.status_code == 500


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_a_wrong_token_outside_development_still_declines_quietly(monkeypatch, environment):
    """The token check precedes the environment check, and must stay that way.

    A stranger's bearer token arriving at a production deployment that happens
    to have the flag set must not be told the bypass exists. Only the correct
    token reaches the loud refusal.
    """
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    monkeypatch.setattr(settings, "environment", environment)

    assert _dev_bypass_claims("not-the-dev-token") is None
