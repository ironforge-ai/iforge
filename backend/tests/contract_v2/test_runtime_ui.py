"""The OpenClaw runtime-UI access surface (06.4 PROV-14, consumer-first per RESEARCH A8).

Naming, so later plans can select by ``-k``: every case is ``test_runtime_ui_*`` --
``-k runtime_ui_`` selects the whole surface, ``-k runtime_ui_credentials`` the issuance route
(``POST /v2/deployments/{id}/runtime-ui/credentials``) and ``-k runtime_ui_access_reset`` the
reset route (``POST /v2/deployments/{id}/runtime-ui/access/reset``).

The mock is the executable reference for both routes (ADR-0005). Single-use redemption is a
property of the compute edge, not of issuance, so it is asserted by the hosted compute grants
suite, never here: two issuances in a row are both answered, each with a fresh token. These
cases assert wire shapes only, never the mock's grant store.

Against a hosted target the cases need a live openclaw deployment owned by the dashboard user:
``V2_CONTRACT_OPENCLAW_DEPLOYMENT_ID`` names it, and without it the cases that read one skip.
One case PATCHes its language, which redeploys it.
"""

from __future__ import annotations

import json
import os
import urllib.parse

import httpx
import pytest

from scripts import mock_deploy_api
from tests.contract_v2.conftest import HOSTED, new_key
from tests.contract_v2.schema import validate_component

CREDENTIAL_FIELDS = {
    "runtime",
    "url",
    "deployment_resource_version",
    "auth_mode",
    "token",
    "handoff_url",
}
UNKNOWN_DEPLOYMENT_ID = "hdep_contract_runtime_ui_unknown"


def _code(response: httpx.Response) -> str | None:
    """The problem code: top level on the hosted problem+json, under ``detail`` on the mock."""
    body = response.json()
    if isinstance(body.get("code"), str):
        return body["code"]
    detail = body.get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


@pytest.fixture
def openclaw_deployment_id() -> str:
    """A live openclaw deployment the dashboard user owns: named by the operator on a hosted
    target, the one the mock seeds otherwise."""
    if HOSTED:
        deployment_id = os.environ.get("V2_CONTRACT_OPENCLAW_DEPLOYMENT_ID", "").strip()
        if not deployment_id:
            pytest.skip("hosted runtime-ui cases need V2_CONTRACT_OPENCLAW_DEPLOYMENT_ID")
        return deployment_id
    for deployment_id, record in mock_deploy_api.DEPLOYMENTS.items():
        if record["config_info"].get("runtime") == "openclaw":
            return deployment_id
    raise AssertionError("the mock seeds no openclaw deployment")


async def _read(client: httpx.AsyncClient, deployment_id: str) -> dict:
    response = await client.get(f"/v2/deployments/{deployment_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _resource_version(read: dict) -> str:
    return read["resource"]["metadata"]["resourceVersion"]


async def _issue(
    client: httpx.AsyncClient, deployment_id: str, resource_version: str
) -> httpx.Response:
    return await client.post(
        f"/v2/deployments/{deployment_id}/runtime-ui/credentials",
        headers={"If-Match": f'"{resource_version}"'},
    )


async def _reset(
    client: httpx.AsyncClient, deployment_id: str, resource_version: str
) -> httpx.Response:
    return await client.post(
        f"/v2/deployments/{deployment_id}/runtime-ui/access/reset",
        headers={
            "Idempotency-Key": new_key("runtime-ui-reset"),
            "If-Match": f'"{resource_version}"',
        },
    )


async def test_runtime_ui_credentials_issue_pinned_shape(
    v2_dashboard: httpx.AsyncClient, openclaw_deployment_id: str
) -> None:
    read = await _read(v2_dashboard, openclaw_deployment_id)
    resource_version = _resource_version(read)

    response = await _issue(v2_dashboard, openclaw_deployment_id, resource_version)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == CREDENTIAL_FIELDS
    assert body["runtime"] == "openclaw"
    assert body["auth_mode"] == "openclaw_token"
    assert body["deployment_resource_version"] == resource_version
    assert body["url"] == read["runtime_ui_endpoint"]["url"]
    assert isinstance(body["token"], str) and body["token"]
    assert isinstance(body["handoff_url"], str) and body["handoff_url"]
    validate_component("V2OpenClawRuntimeUiCredentials", body)


async def test_runtime_ui_credentials_handoff_is_the_native_owner_bootstrap(
    v2_dashboard: httpx.AsyncClient, openclaw_deployment_id: str
) -> None:
    """The handoff is OpenClaw's native owner bootstrap (D-42 option A).

    This mirrors the native branch of packages/shared ``isOpenClawLaunchHandoff``, the dashboard's
    validator: with the fragment removed the handoff is the clean endpoint ``url`` exactly, and
    the fragment holds exactly ``bootstrapToken`` (non-empty, never the edge grant ``token``) and
    ``bootstrapProfile=owner``, plus optionally ``gatewayUrl`` naming the endpoint over ``wss``
    without a trailing slash. The legacy ``#token=`` launch is refused. The hosted target
    (06.4-51) answers the runtime's own single-use bootstrap from ``openclaw dashboard --json``;
    the mock answers a recognisable fake.
    """
    resource_version = _resource_version(await _read(v2_dashboard, openclaw_deployment_id))

    response = await _issue(v2_dashboard, openclaw_deployment_id, resource_version)

    assert response.status_code == 200, response.text
    body = response.json()
    base, _, fragment = body["handoff_url"].partition("#")
    assert base == body["url"]
    pairs = urllib.parse.parse_qsl(fragment, keep_blank_values=True)
    keys = [key for key, _ in pairs]
    assert len(keys) == len(set(keys)), "the handoff fragment repeats a key"
    assert "token" not in keys, "the legacy #token= launch is refused (D-42 option A)"
    assert set(keys) in (
        {"bootstrapToken", "bootstrapProfile"},
        {"bootstrapToken", "bootstrapProfile", "gatewayUrl"},
    ), f"handoff fragment keys: {sorted(keys)}"
    fields = dict(pairs)
    assert fields["bootstrapToken"], "the bootstrap is empty"
    assert fields["bootstrapToken"] != body["token"], "the bootstrap is the edge grant"
    assert fields["bootstrapProfile"] == "owner"
    if "gatewayUrl" in fields:
        endpoint = urllib.parse.urlsplit(body["url"])
        assert fields["gatewayUrl"] == f"wss://{endpoint.netloc}{endpoint.path.rstrip('/')}"


async def test_runtime_ui_credentials_if_match_mismatch_is_412(
    v2_dashboard: httpx.AsyncClient, openclaw_deployment_id: str
) -> None:
    resource_version = _resource_version(await _read(v2_dashboard, openclaw_deployment_id))

    response = await _issue(v2_dashboard, openclaw_deployment_id, f"{resource_version}_stale")

    assert response.status_code == 412, response.text


async def test_runtime_ui_credentials_previous_version_after_generation_bump_is_412(
    v2_dashboard: httpx.AsyncClient, openclaw_deployment_id: str
) -> None:
    """Adjacency: the version immediately before a spec change no longer issues, so a grant
    cannot migrate across a redeploy."""
    read = await _read(v2_dashboard, openclaw_deployment_id)
    previous = _resource_version(read)
    # A contract language that differs from the current one, so the PATCH is a real spec change
    # (a hosted deployment keeps the last run's value).
    language = "fr" if '"language": "es"' in json.dumps(read) else "es"
    bumped = await v2_dashboard.patch(
        f"/v2/deployments/{openclaw_deployment_id}",
        headers={"Idempotency-Key": new_key("runtime-ui-bump"), "If-Match": f'"{previous}"'},
        json={"language": language},
    )
    assert bumped.is_success, bumped.text
    current = _resource_version(await _read(v2_dashboard, openclaw_deployment_id))
    assert current != previous

    stale = await _issue(v2_dashboard, openclaw_deployment_id, previous)
    fresh = await _issue(v2_dashboard, openclaw_deployment_id, current)

    assert stale.status_code == 412, stale.text
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["deployment_resource_version"] == current


async def test_runtime_ui_credentials_unknown_deployment_is_404(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    response = await _issue(v2_dashboard, UNKNOWN_DEPLOYMENT_ID, "rv_unknown")

    assert response.status_code == 404, response.text
    assert _code(response) == "deployment_not_found"


async def test_runtime_ui_credentials_repeated_issuance_returns_fresh_tokens(
    v2_dashboard: httpx.AsyncClient, openclaw_deployment_id: str
) -> None:
    resource_version = _resource_version(await _read(v2_dashboard, openclaw_deployment_id))

    first = await _issue(v2_dashboard, openclaw_deployment_id, resource_version)
    second = await _issue(v2_dashboard, openclaw_deployment_id, resource_version)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["token"] != second.json()["token"]


async def test_runtime_ui_access_reset_without_grants_is_success(
    v2_dashboard: httpx.AsyncClient, openclaw_deployment_id: str
) -> None:
    resource_version = _resource_version(await _read(v2_dashboard, openclaw_deployment_id))

    response = await _reset(v2_dashboard, openclaw_deployment_id, resource_version)

    assert response.is_success, response.text


async def test_runtime_ui_access_reset_then_reissue_returns_a_fresh_token(
    v2_dashboard: httpx.AsyncClient, openclaw_deployment_id: str
) -> None:
    """Reset invalidates outstanding grants, not the ability to issue."""
    before = _resource_version(await _read(v2_dashboard, openclaw_deployment_id))
    issued = await _issue(v2_dashboard, openclaw_deployment_id, before)
    assert issued.status_code == 200, issued.text

    reset = await _reset(v2_dashboard, openclaw_deployment_id, before)
    assert reset.is_success, reset.text

    after = _resource_version(await _read(v2_dashboard, openclaw_deployment_id))
    reissued = await _issue(v2_dashboard, openclaw_deployment_id, after)

    assert reissued.status_code == 200, reissued.text
    assert set(reissued.json()) == CREDENTIAL_FIELDS
    assert reissued.json()["token"] != issued.json()["token"]


async def test_runtime_ui_access_reset_stale_if_match_is_412(
    v2_dashboard: httpx.AsyncClient, openclaw_deployment_id: str
) -> None:
    """An If-Match naming a version the deployment does not have is a mismatch, not a reset."""
    resource_version = _resource_version(await _read(v2_dashboard, openclaw_deployment_id))

    response = await _reset(v2_dashboard, openclaw_deployment_id, f"{resource_version}_stale")

    assert response.status_code == 412, response.text


async def test_runtime_ui_access_reset_without_if_match_is_428(
    v2_dashboard: httpx.AsyncClient, openclaw_deployment_id: str
) -> None:
    """A reset that names no version is refused before any version comparison (the pinned
    contract lists 428; the hosted compute role answers ``if_match_required``)."""
    await _read(v2_dashboard, openclaw_deployment_id)  # the deployment exists: not a 404

    response = await v2_dashboard.post(
        f"/v2/deployments/{openclaw_deployment_id}/runtime-ui/access/reset",
        headers={"Idempotency-Key": new_key("runtime-ui-reset")},
    )

    assert response.status_code == 428, response.text
    if HOSTED:
        assert _code(response) == "if_match_required"


async def test_runtime_ui_access_reset_unknown_deployment_is_404(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    response = await _reset(v2_dashboard, UNKNOWN_DEPLOYMENT_ID, "rv_unknown")

    assert response.status_code == 404, response.text
    assert _code(response) == "deployment_not_found"
