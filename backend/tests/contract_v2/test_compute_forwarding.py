"""Billing forwards ``/v2`` deployment reads to the compute role (06.4 D-14, D-16).

Naming, so later plans can select by ``-k``: every case is ``test_compute_forwarding_*``, and
``-k compute_forwarding`` selects them all.

Against the mock the mock is the provisioner, so the declared state is always ``forwarded``. On
either target each listed item is validated against the pinned ``V2HostedDeploymentReadResponse``.
Against a hosted target the operator declares the compute state in ``V2_CONTRACT_COMPUTE``:

- ``forwarded`` -- billing has a compute destination; each read answers what compute answers.
- ``unset`` -- billing has no compute destination; each read fails closed with 503
  ``compute_unavailable``.

An undeclared state (the variable missing, blank, or any other value) FAILS the case, before
any request is sent. It never skips: a skipped forwarding case reads as a pass in a run summary
while proving nothing about the forward.
"""

from __future__ import annotations

import os

import httpx
import pytest

from tests.contract_v2.conftest import HOSTED
from tests.contract_v2.schema import validate_component

COMPUTE_STATE_ENV = "V2_CONTRACT_COMPUTE"
FORWARDED = "forwarded"
UNSET = "unset"
UNKNOWN_DEPLOYMENT_ID = "hdep_contract_forwarding_unknown"


def _declared_compute_state() -> str:
    """``forwarded`` on the mock; hosted, the operator's ``V2_CONTRACT_COMPUTE`` or a failure."""
    if not HOSTED:
        return FORWARDED
    state = os.environ.get(COMPUTE_STATE_ENV, "").strip()
    if state not in (FORWARDED, UNSET):
        pytest.fail(
            f"hosted compute-forwarding cases need {COMPUTE_STATE_ENV} set to "
            f"'{FORWARDED}' or '{UNSET}' (got {state!r}); an undeclared compute state is a "
            "failure, never a skip"
        )
    return state


def _code(response: httpx.Response) -> str | None:
    """The problem code: top level on the hosted problem+json, under ``detail`` on billing's own
    errors and the mock."""
    body = response.json()
    if isinstance(body.get("code"), str):
        return body["code"]
    detail = body.get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def _assert_compute_unavailable(response: httpx.Response) -> None:
    assert response.status_code == 503, response.text
    assert _code(response) == "compute_unavailable", response.text


async def test_compute_forwarding_deployment_list_answers_the_declared_state(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    state = _declared_compute_state()

    response = await v2_dashboard.get("/v2/deployments")

    if state == UNSET:
        _assert_compute_unavailable(response)
        return
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list), f"expected a list of deployment reads, got {type(body)}"
    # Each forwarded read is a pinned deployment read, hosted and on the mock alike.
    for item in body:
        validate_component("V2HostedDeploymentReadResponse", item)


async def test_compute_forwarding_unknown_deployment_answers_the_declared_state(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    state = _declared_compute_state()

    response = await v2_dashboard.get(f"/v2/deployments/{UNKNOWN_DEPLOYMENT_ID}")

    if state == UNSET:
        _assert_compute_unavailable(response)
        return
    # Compute answered. The mock's 404 body differs from the hosted one; the status is the proof.
    assert response.status_code == 404, response.text
