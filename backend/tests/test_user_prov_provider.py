"""D-02 user-provisioning provider seam coverage.

Pins the Clerk implementation's `delete_identity` semantics (httpx
MockTransport, no live network) and the `get_user_provisioning_provider`
factory dispatch on `settings.user_prov`. Phase 3.1 implements the same
Protocol for the self-hosted Supabase provider (`user_prov="supa"`).

RESEARCH A4: Clerk delete semantics under API version 2026-05-12 are
docs-cited; the first live call is the real proof.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.services.clerk_backend import CLERK_BACKEND_API_VERSION
from app.services.user_prov import get_user_provisioning_provider
from app.services.user_prov.clerk import ClerkUserProvisioningProvider

_SECRET = "sk_test_user_prov_seam"


def _recording_provider(
    status_code: int,
) -> tuple[ClerkUserProvisioningProvider, list[httpx.Request]]:
    """Provider wired to a MockTransport that records every request."""
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code)

    return (
        ClerkUserProvisioningProvider(transport=httpx.MockTransport(_handler)),
        requests,
    )


# --------------------------------------------------------------------------
# delete_identity
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_identity_200_issues_one_delete_with_clerk_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 → returns None; exactly one DELETE to the Clerk user URL with
    Authorization and Clerk-API-Version headers present (D-14)."""
    monkeypatch.setattr(settings, "clerk_secret_key", _SECRET)
    provider, requests = _recording_provider(200)

    result = await provider.delete_identity("user_2abcDEF")

    assert result is None
    assert len(requests) == 1
    (request,) = requests
    assert request.method == "DELETE"
    assert str(request.url) == "https://api.clerk.com/v1/users/user_2abcDEF"
    assert request.headers["Authorization"] == f"Bearer {_SECRET}"
    assert request.headers["Clerk-API-Version"] == CLERK_BACKEND_API_VERSION


@pytest.mark.asyncio
async def test_delete_identity_url_quotes_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subject is URL-quoted via clerk_user_url — path metacharacters can
    never re-target the request (T-03-11)."""
    monkeypatch.setattr(settings, "clerk_secret_key", _SECRET)
    provider, requests = _recording_provider(200)

    await provider.delete_identity("user/../instance")

    (request,) = requests
    assert str(request.url) == "https://api.clerk.com/v1/users/user%2F..%2Finstance"


@pytest.mark.asyncio
async def test_delete_identity_404_is_already_gone_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """404 → returns None: the identity is already gone; the webhook
    reconciliation net owns the rest (D-14)."""
    monkeypatch.setattr(settings, "clerk_secret_key", _SECRET)
    provider, requests = _recording_provider(404)

    assert await provider.delete_identity("user_gone") is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_delete_identity_500_raises_http_status_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-2xx (other than 404) raises httpx.HTTPStatusError — pinned so
    03-08's lifecycle caller can rely on the failure mode."""
    monkeypatch.setattr(settings, "clerk_secret_key", _SECRET)
    provider, _ = _recording_provider(500)

    with pytest.raises(httpx.HTTPStatusError):
        await provider.delete_identity("user_boom")


# --------------------------------------------------------------------------
# Factory + settings vocabulary
# --------------------------------------------------------------------------


def test_settings_expose_user_prov_vocabulary() -> None:
    """The user's exact key names (D-02 — locked): user_prov defaults to
    "clerk"; user_prov_url / user_prov_api default empty (supa connection
    settings, wired in phase 3.1)."""
    assert settings.user_prov == "clerk"
    assert settings.user_prov_url == ""
    assert settings.user_prov_api == ""


def test_factory_returns_clerk_provider_by_default() -> None:
    provider = get_user_provisioning_provider()
    assert isinstance(provider, ClerkUserProvisioningProvider)


def test_factory_supa_raises_not_implemented_naming_3_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "user_prov", "supa")
    with pytest.raises(NotImplementedError, match="3.1"):
        get_user_provisioning_provider()


def test_factory_unknown_value_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "user_prov", "garbage")
    with pytest.raises(ValueError, match="user_prov"):
        get_user_provisioning_provider()
