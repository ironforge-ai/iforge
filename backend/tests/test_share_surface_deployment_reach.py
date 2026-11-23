"""D-08 pin suite: no public/share surface exposes deployment reach.

ACCESS-03's shared-link half, locked the way D-08 decided it: the shipped
sharing features stay exactly as shipped, and "they expose no deployment
reach" becomes a build-breaking fact rather than a review-time hope.

Three routers form the complete public/share surface:

  - ``app.routes.sharing``          — owner-facing project sharing (10 routes)
  - ``app.routes.public_sessions``  — anonymous shared-session reads (4 routes)
  - ``app.routes.share_redeem``     — public share-token endpoints (3 routes)

The suite pins two facts, mirroring the test-enforced-contract discipline of
``test_iforge_companion_docs_contract.py``:

  1. **Route inventory** — the exact ``(method, path)`` set of all three
     routers equals a pinned literal. Adding a route to any of them without
     adding a pin entry (and therefore a reach assertion) fails CI.
  2. **No deployment reach** — no pinned response schema exposes a field
     whose name matches runtime-UI / credential / deployment-identifier
     patterns, and rendered response bodies (one representative fixture per
     surface) contain neither reach-shaped keys nor URLs pointing anywhere
     but the configured web origin.

A negative control proves the detector is non-vacuous: a real
deployment-bearing schema (``AdminRuntimeStateResponse``) and a synthetic
runtime-UI payload MUST match it. A reach detector that matches nothing
pins nothing.

Extending the surface: add the new route to ``PINNED_ROUTES`` with its exact
response-model annotation. Extending the detector: both pattern sets are
module-level constants — one edit each.
"""

from __future__ import annotations

import re
import typing
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.core.config import settings
from app.models.project_share_link import ProjectShareLink
from app.routes.public_sessions import router as public_sessions_router
from app.routes.share_redeem import router as share_redeem_router
from app.routes.sharing import router as sharing_router
from app.schemas.admin import AdminRuntimeStateResponse
from app.schemas.session import (
    PublicSessionExportResponse,
    PublicSessionResponse,
    SessionMessagesPage,
)
from app.schemas.sharing import (
    InvitationCancelResponse,
    InvitationResponse,
    MemberResponse,
    ProjectLeaveResponse,
    ProjectMemberRemoveResponse,
    ShareLinkCreated,
    ShareLinkResponse,
    ShareLinkRevokeResponse,
    ShareRedeemResponse,
    ShareUpgradeResponse,
    UnshareResponse,
)
from app.services.sharing import generate_share_token, hash_share_token
from tests.test_public_sessions import _enable_link, _seed_session_with_content

# --- Reach detector ---------------------------------------------------------
#
# Field-name patterns of the {runtime_ui_url, hermes_control_ui_url,
# gateway_token, credential, endpoint}-class. A response field whose lowered
# name contains any of these is deployment reach. Deliberately NOT in the
# set: bare "url" / "token" — share links legitimately carry a share URL and
# a share token; those point at the web origin, which the URL-shape check
# below verifies.
REACH_FIELD_NAME_PATTERNS: tuple[str, ...] = (
    "runtime_ui",
    "control_ui",
    "gateway",
    "credential",
    "deployment",
    "instance_id",
    "api_key",
    "base_url",
    "endpoint",
    "secret",
    "hcloud",
)

_URL_RE = re.compile(r"https?://[^\s)\"'<>]+")


def _allowed_url_hosts() -> set[str]:
    """Hosts a public/share response body may legitimately point at.

    Share URLs are built from ``settings.web_origin`` (with an
    ``example.invalid`` fallback in ``sharing._share_url``); anything
    else — in particular a per-deployment host — is reach.
    """
    hosts = {"example.invalid"}
    origin_host = urlsplit(settings.web_origin).hostname if settings.web_origin else None
    if origin_host:
        hosts.add(origin_host)
    return hosts


def find_reach_field_names(field_names: typing.Iterable[str]) -> dict[str, list[str]]:
    """Map offending field name -> matched patterns. Empty dict == clean."""
    findings: dict[str, list[str]] = {}
    for name in field_names:
        matched = [p for p in REACH_FIELD_NAME_PATTERNS if p in name.lower()]
        if matched:
            findings[name] = matched
    return findings


def _extract_models(annotation: object) -> list[type[BaseModel]]:
    """Pull every BaseModel subclass out of a (possibly generic) annotation."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [model for arg in typing.get_args(annotation) for model in _extract_models(arg)]


def collect_model_field_names(
    model: type[BaseModel], _seen: set[type[BaseModel]] | None = None
) -> set[str]:
    """All field names of a model, recursing into nested models."""
    seen = _seen if _seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        for nested in _extract_models(field.annotation):
            names |= collect_model_field_names(nested, seen)
    return names


def find_reach_in_rendered(payload: object, path: str = "$") -> list[str]:
    """Reach findings in a rendered JSON body: reach-shaped keys, or URL
    values pointing at a host outside the allowed web-origin set."""
    findings: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            for matched in find_reach_field_names([key]).get(key, []):
                findings.append(f"{path}.{key} matches reach pattern {matched!r}")
            findings.extend(find_reach_in_rendered(value, f"{path}.{key}"))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            findings.extend(find_reach_in_rendered(item, f"{path}[{index}]"))
    elif isinstance(payload, str):
        findings.extend(_find_disallowed_urls(payload, path))
    return findings


def _find_disallowed_urls(text: str, path: str) -> list[str]:
    allowed = _allowed_url_hosts()
    findings: list[str] = []
    for url in _URL_RE.findall(text):
        host = urlsplit(url).hostname
        if host is not None and host not in allowed:
            findings.append(f"{path} contains URL to non-web-origin host: {url}")
    return findings


# --- Pinned route inventory -------------------------------------------------
#
# (method, path) -> exact response-model annotation. ``None`` means the
# route serializes a raw ``Response`` (no Pydantic model) and MUST be
# covered by a body-level rendered test below instead.
PINNED_ROUTES: dict[tuple[str, str], object] = {
    # app/routes/sharing.py — owner-facing project sharing (10 routes)
    ("POST", "/projects/{project_id}/share-links"): ShareLinkCreated,
    ("GET", "/projects/{project_id}/share-links"): list[ShareLinkResponse],
    ("DELETE", "/projects/{project_id}/share-links/{link_id}"): ShareLinkRevokeResponse,
    ("POST", "/projects/{project_id}/invitations"): InvitationResponse,
    ("GET", "/projects/{project_id}/invitations"): list[InvitationResponse],
    ("DELETE", "/projects/{project_id}/invitations/{invitation_id}"): InvitationCancelResponse,
    ("GET", "/projects/{project_id}/members"): list[MemberResponse],
    ("DELETE", "/projects/{project_id}/members/{member_user_id}"): ProjectMemberRemoveResponse,
    ("POST", "/projects/{project_id}/leave"): ProjectLeaveResponse,
    ("POST", "/projects/{project_id}/unshare"): UnshareResponse,
    # app/routes/public_sessions.py — anonymous shared-session reads (4 routes)
    ("GET", "/public/sessions/{session_id}"): PublicSessionResponse,
    ("GET", "/public/sessions/{session_id}/messages"): SessionMessagesPage,
    ("GET", "/public/sessions/{session_id}/export.md"): None,  # markdown body, pinned below
    ("GET", "/public/sessions/{session_id}/export.json"): PublicSessionExportResponse,
    # app/routes/share_redeem.py — public share-token endpoints (3 routes)
    ("GET", "/share/{token}/preview"): ShareRedeemResponse,
    ("POST", "/share/{token}/redeem"): ShareRedeemResponse,
    ("POST", "/share/{token}/upgrade"): ShareUpgradeResponse,
}

_SURFACE_ROUTERS = (sharing_router, public_sessions_router, share_redeem_router)


def _live_route_inventory() -> set[tuple[str, str]]:
    inventory: set[tuple[str, str]] = set()
    for router in _SURFACE_ROUTERS:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods - {"HEAD", "OPTIONS"}:
                inventory.add((method, route.path))
    return inventory


def test_route_inventory_matches_pin_exactly():
    """Set equality, not subset: a new route on any of the three routers
    without a pin entry (and its reach assertion) fails here."""
    live = _live_route_inventory()
    pinned = set(PINNED_ROUTES)
    assert live == pinned, (
        "public/share surface drifted from the D-08 pin. "
        f"Unpinned live routes: {sorted(live - pinned)}; "
        f"pinned but no longer live: {sorted(pinned - live)}. "
        "Add a pin entry + reach assertion in "
        "tests/test_share_surface_deployment_reach.py for every new route."
    )


def test_pinned_response_models_match_live_routes():
    """Every pinned route serializes through exactly the pinned model, so the
    schema-level reach assertions below cover what the wire actually sends."""
    by_key: dict[tuple[str, str], APIRoute] = {}
    for router in _SURFACE_ROUTERS:
        for route in router.routes:
            if isinstance(route, APIRoute):
                for method in route.methods - {"HEAD", "OPTIONS"}:
                    by_key[(method, route.path)] = route

    for key, pinned_model in PINNED_ROUTES.items():
        route = by_key[key]
        assert route.response_model == pinned_model, (
            f"{key[0]} {key[1]} serializes {route.response_model!r}, "
            f"but the pin declares {pinned_model!r} — update the pin AND "
            "re-check the new model for deployment reach."
        )


def test_no_pinned_schema_exposes_reach_fields():
    """Schema-level reach assertion for every pinned route with a model:
    no field name (recursively, nested models included) matches the
    deployment/runtime-url/credential patterns."""
    for (method, path), annotation in PINNED_ROUTES.items():
        if annotation is None:
            continue
        for model in _extract_models(annotation):
            findings = find_reach_field_names(collect_model_field_names(model))
            assert not findings, (
                f"{method} {path} ({model.__name__}) exposes deployment-reach fields: {findings}"
            )


def test_non_model_routes_are_body_pinned():
    """Routes without a response model must be exactly the markdown export,
    which the rendered-body test below covers."""
    model_free = {key for key, annotation in PINNED_ROUTES.items() if annotation is None}
    assert model_free == {("GET", "/public/sessions/{session_id}/export.md")}


# --- Rendered-body reach checks (one representative fixture per surface) ----


@pytest.mark.asyncio
async def test_sharing_rendered_share_link_creation_has_no_reach(client, workspace_project):
    """sharing.py surface: the richest payload it ever returns (the one-time
    ShareLinkCreated with the raw token + share URL) carries no reach."""
    r = await client.post(
        f"/v1/projects/{workspace_project.id}/share-links",
        json={"label": "d08-pin"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    findings = find_reach_in_rendered(body)
    assert not findings, findings
    # The share URL is the only URL and it points at the web origin.
    assert urlsplit(body["url"]).hostname in _allowed_url_hosts()


@pytest.mark.asyncio
async def test_public_session_rendered_bodies_have_no_reach(client, anon_client):
    """public_sessions.py surface: anonymous detail + JSON export bodies and
    the markdown export text carry no reach-shaped keys and no URLs pointing
    off the web origin."""
    sid, _ = await _seed_session_with_content(client, local_session_id="sess-d08-reach")
    await _enable_link(client, sid)

    detail = await anon_client.get(f"/v1/public/sessions/{sid}")
    assert detail.status_code == 200, detail.text
    assert not find_reach_in_rendered(detail.json())

    export_json = await anon_client.get(f"/v1/public/sessions/{sid}/export.json")
    assert export_json.status_code == 200, export_json.text
    assert not find_reach_in_rendered(export_json.json())

    export_md = await anon_client.get(f"/v1/public/sessions/{sid}/export.md")
    assert export_md.status_code == 200, export_md.text
    md_findings = _find_disallowed_urls(export_md.text, "$.export_md")
    assert not md_findings, md_findings


@pytest.mark.asyncio
async def test_share_redeem_rendered_preview_has_no_reach(
    anon_client, db_session, seed_user, workspace_project
):
    """share_redeem.py surface: a rendered ShareRedeemResponse from the
    anonymous preview route (D-08's 'shared link' surface) carries no reach."""
    raw = generate_share_token()
    db_session.add(
        ProjectShareLink(
            project_id=workspace_project.id,
            token_hash=hash_share_token(raw),
            token_prefix=raw[:8],
            resolved_owner_handle="d08-owner",
            created_by=seed_user.id,
            created_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    r = await anon_client.get(f"/v1/share/{raw}/preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == set(ShareRedeemResponse.model_fields)
    findings = find_reach_in_rendered(body)
    assert not findings, findings


# --- Negative controls: the detector must have teeth ------------------------


def test_detector_matches_deployment_bearing_schema():
    """A real deployment-bearing response model in this codebase MUST match
    the field-name detector — deployment_id / instance_id are exactly the
    identifiers usable against the /v2 lifecycle."""
    findings = find_reach_field_names(collect_model_field_names(AdminRuntimeStateResponse))
    assert "deployment_id" in findings
    assert "instance_id" in findings


def test_detector_matches_deployment_bearing_rendered_payload():
    """A synthetic runtime-UI payload MUST trip both detector halves: the
    reach-shaped keys and the non-web-origin URL values."""
    payload = {
        "runtime_ui_url": "https://dep-1a2b3c4d.tenant-host.example/ui",
        "hermes_control_ui_url": "https://dep-1a2b3c4d.tenant-host.example/hermes",
        "gateway_token": "gw-not-a-real-credential",
        "deployment_id": "dep-1a2b3c4d",
    }
    findings = find_reach_in_rendered(payload)
    flagged_keys = {f.split(" ", 1)[0] for f in findings}
    for key in payload:
        assert f"$.{key}" in flagged_keys, (
            f"detector failed to flag {key!r}; a reach detector that matches "
            f"nothing pins nothing. Findings: {findings}"
        )
    assert any("non-web-origin host" in f for f in findings)
