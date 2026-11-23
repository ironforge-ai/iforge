"""The host-condition report (plan 06-44, G-06-2): a channel for the host to tell the control plane.

A ``condition`` report is NOT a job phase (D-26: the job states stay queued/running/complete/
failed). Like ``backup`` and ``removed`` it is accepted in any installation status, and it never
moves the job: ``raised`` records ``external_resources["host_conditions"][code]`` with the detail
redacted against the installation's own credentials, and ``cleared`` removes that code.

Plans 06-45 to 06-49 raise and clear the three codes through it: ``update_rollback_incomplete``
(WR-07), ``schema_digest_mismatch`` (WR-02) and ``network_subnet_unverified`` (WR-06).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from app.core.config import settings
from app.services.companion_install import REDACTED
from tests.test_companion_install_job import (
    _atoms,
    _complete_installation,
    _hosted_agent,
    _post_report,
    _reload,
    _running_installation,
    _runtime_key,
)

ZONE = "companions.example"
ROLLBACK = "update_rollback_incomplete"
SCHEMA = "schema_digest_mismatch"


@pytest.fixture
def companion_zone(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "companion_edge_zone_name", ZONE)
    return ZONE


def _condition(
    code: str = ROLLBACK,
    state: str = "raised",
    detail: str | None = "the rollback restore did not finish",
    *,
    operation: str = "install",
) -> dict:
    condition: dict = {"code": code, "state": state}
    if detail is not None:
        condition["detail"] = detail
    return {"operation": operation, "phase": "condition", "condition": condition}


def _job_fields(row) -> tuple:
    return (row.operation, row.status, row.step, row.error, row.next_attempt_at)


def _conditions(row) -> dict:
    return (row.external_resources or {}).get("host_conditions", {})


async def test_a_raised_condition_is_recorded_redacted_and_never_moves_the_job(
    db_session, seed_user, companion_zone, caplog
) -> None:
    complete = await _complete_installation(db_session, seed_user)
    password = (await _atoms(db_session, complete.installation_id))["db-password"]
    before = await _reload(db_session, complete.installation_id)
    job = _job_fields(before)
    reported_before = before.last_report_at

    with caplog.at_level(logging.WARNING, logger="app.services.companion_install"):
        response = await _post_report(
            db_session,
            complete.runtime_key,
            complete.installation_id,
            _condition(detail=f"restore failed: psql -W {password} exited 2"),
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "complete", "step": before.step, "stored_lines": 0}
    row = await _reload(db_session, complete.installation_id)
    recorded = _conditions(row)[ROLLBACK]
    assert recorded["detail"] == f"restore failed: psql -W {REDACTED} exited 2"
    assert datetime.fromisoformat(recorded["raised_at"]).tzinfo is not None
    assert _job_fields(row) == job
    assert row.last_report_at is not None and row.last_report_at != reported_before
    # Logged with the code, never the detail (T-06-44-03).
    messages = [record.getMessage() for record in caplog.records]
    assert any(ROLLBACK in message for message in messages), messages
    assert not any("restore failed" in message for message in messages), messages
    assert not any(password in message for message in messages)


async def test_raising_again_replaces_the_entry_and_a_second_code_is_kept_alongside(
    db_session, seed_user, companion_zone
) -> None:
    complete = await _complete_installation(db_session, seed_user)
    first = await _post_report(
        db_session, complete.runtime_key, complete.installation_id, _condition(detail="first")
    )
    assert first.status_code == 200, first.text
    row = await _reload(db_session, complete.installation_id)
    # An old raise time, so the replacement is observable.
    resources = dict(row.external_resources or {})
    resources["host_conditions"] = {
        ROLLBACK: {"detail": "first", "raised_at": "2020-01-01T00:00:00+00:00"}
    }
    row.external_resources = resources
    await db_session.commit()

    again = await _post_report(
        db_session, complete.runtime_key, complete.installation_id, _condition(detail="second")
    )
    other = await _post_report(
        db_session,
        complete.runtime_key,
        complete.installation_id,
        _condition(SCHEMA, detail="schema written by another digest"),
    )

    assert again.status_code == 200, again.text
    assert other.status_code == 200, other.text
    conditions = _conditions(await _reload(db_session, complete.installation_id))
    assert set(conditions) == {ROLLBACK, SCHEMA}
    assert conditions[ROLLBACK]["detail"] == "second"
    assert conditions[ROLLBACK]["raised_at"] != "2020-01-01T00:00:00+00:00"
    assert conditions[SCHEMA]["detail"] == "schema written by another digest"


async def test_clearing_removes_only_that_code_and_clearing_an_absent_code_is_a_no_op(
    db_session, seed_user, companion_zone
) -> None:
    complete = await _complete_installation(db_session, seed_user)
    for body in (_condition(), _condition(SCHEMA, detail=None)):
        raised = await _post_report(
            db_session, complete.runtime_key, complete.installation_id, body
        )
        assert raised.status_code == 200, raised.text
    # A raise without a detail is recorded with a null detail.
    assert (
        _conditions(await _reload(db_session, complete.installation_id))[SCHEMA]["detail"] is None
    )
    job = _job_fields(await _reload(db_session, complete.installation_id))

    cleared = await _post_report(
        db_session,
        complete.runtime_key,
        complete.installation_id,
        _condition(state="cleared", detail=None),
    )
    assert cleared.status_code == 200, cleared.text
    row = await _reload(db_session, complete.installation_id)
    assert set(_conditions(row)) == {SCHEMA}
    assert _job_fields(row) == job

    absent = await _post_report(
        db_session,
        complete.runtime_key,
        complete.installation_id,
        _condition("network_subnet_unverified", state="cleared", detail=None),
    )
    assert absent.status_code == 200, absent.text
    row = await _reload(db_session, complete.installation_id)
    assert set(_conditions(row)) == {SCHEMA}
    assert _job_fields(row) == job


async def test_a_condition_is_accepted_while_the_job_runs(
    db_session, seed_user, companion_zone
) -> None:
    running = await _running_installation(db_session, seed_user)
    before = _job_fields(await _reload(db_session, running.installation_id))

    response = await _post_report(
        db_session, running.runtime_key, running.installation_id, _condition()
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "running"
    row = await _reload(db_session, running.installation_id)
    assert ROLLBACK in _conditions(row)
    # The reporting deadline is not pushed out: a condition is not job progress.
    assert _job_fields(row) == before


async def test_a_condition_is_accepted_after_the_job_failed(
    db_session, seed_user, companion_zone
) -> None:
    running = await _running_installation(db_session, seed_user)
    failed = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        {"operation": "install", "phase": "failed", "error": "boot failed"},
    )
    assert failed.status_code == 200, failed.text
    before = _job_fields(await _reload(db_session, running.installation_id))
    assert before[1] == "failed"

    response = await _post_report(
        db_session, running.runtime_key, running.installation_id, _condition()
    )

    assert response.status_code == 200, response.text
    row = await _reload(db_session, running.installation_id)
    assert ROLLBACK in _conditions(row)
    assert _job_fields(row) == before


async def test_a_condition_from_another_agents_key_is_404(
    db_session, seed_user, companion_zone
) -> None:
    complete = await _complete_installation(db_session, seed_user)
    other_agent_id, other_deployment_id = await _hosted_agent(db_session, seed_user)
    other_key = await _runtime_key(db_session, seed_user, other_agent_id, other_deployment_id)

    refused = await _post_report(db_session, other_key, complete.installation_id, _condition())

    assert refused.status_code == 404, refused.text
    assert _conditions(await _reload(db_session, complete.installation_id)) == {}


async def test_a_condition_for_a_removed_installation_is_404(
    db_session, seed_user, companion_zone
) -> None:
    complete = await _complete_installation(db_session, seed_user)
    row = await _reload(db_session, complete.installation_id)
    row.removed_at = datetime.now(UTC)
    await db_session.commit()

    refused = await _post_report(
        db_session, complete.runtime_key, complete.installation_id, _condition()
    )

    assert refused.status_code == 404, refused.text


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_condition("disk_on_fire"), id="unknown-code"),
        pytest.param({"operation": "install", "phase": "condition"}, id="no-condition"),
        pytest.param({**_condition(), "step": "rollback"}, id="with-step"),
        pytest.param(
            {**_condition(), "lines": [{"stream": "stdout", "line": "x"}]}, id="with-lines"
        ),
        pytest.param({**_condition(), "error": "boom"}, id="with-error"),
        pytest.param(_condition(state="cleared", detail="still here"), id="cleared-with-detail"),
        pytest.param(_condition(state="lowered"), id="unknown-state"),
        pytest.param(
            {"operation": "install", "phase": "progress", "condition": _condition()["condition"]},
            id="progress-with-condition",
        ),
        pytest.param(
            {"operation": "install", "phase": "removed", "condition": _condition()["condition"]},
            id="removed-with-condition",
        ),
        pytest.param(
            {
                "operation": "install",
                "phase": "condition",
                "condition": {**_condition()["condition"], "extra": 1},
            },
            id="condition-extra-field",
        ),
        pytest.param(_condition(detail="x" * 1001), id="overlong-detail"),
    ],
)
async def test_a_malformed_condition_report_is_refused_by_the_schema(
    db_session, seed_user, companion_zone, body: dict
) -> None:
    complete = await _complete_installation(db_session, seed_user)

    refused = await _post_report(db_session, complete.runtime_key, complete.installation_id, body)

    assert refused.status_code == 422, refused.text
    assert _conditions(await _reload(db_session, complete.installation_id)) == {}
