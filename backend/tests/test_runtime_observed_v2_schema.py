from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from app.routes.sessions import _runtime_observed_columns
from app.schemas.runtime_observed import HostedRuntimeObserved, HostedRuntimeObservedV2


def _payload() -> dict:
    return {
        "schemaVersion": "clawdi.hostedRuntimeObserved.v2",
        "reportedAt": "2026-07-13T00:00:00Z",
        "runtimeMode": "hosted",
        "status": "ok",
        "activeCliVersion": "1.2.3",
        "applied": {
            "etag": '"bundle"',
            "sourceRevision": "a" * 64,
            "generation": 3,
            "instanceId": "instance",
            "appliedProviderIds": ["provider"],
        },
        "boot": None,
        "cli": None,
    }


def test_runtime_observed_v2_is_strict_and_applied_is_complete() -> None:
    adapter = TypeAdapter(HostedRuntimeObserved)
    assert isinstance(adapter.validate_python(_payload()), HostedRuntimeObservedV2)

    partial = _payload()
    del partial["applied"]["sourceRevision"]
    with pytest.raises(ValidationError):
        adapter.validate_python(partial)

    extra = _payload()
    extra["channels"] = {"etag": '"legacy"'}
    with pytest.raises(ValidationError):
        adapter.validate_python(extra)

    projected = _payload()
    projected["applied"]["projectedProviderIds"] = ["target-specific"]
    with pytest.raises(ValidationError):
        adapter.validate_python(projected)


def test_runtime_observed_v2_columns_come_from_applied_authority() -> None:
    value = TypeAdapter(HostedRuntimeObserved).validate_python(_payload())
    assert isinstance(value, HostedRuntimeObservedV2)
    columns = _runtime_observed_columns(
        value,
        observed_at=value.reported_at,
    )
    assert columns["observed_config_generation"] == 3
    assert columns["observed_manifest_etag"] == '"bundle"'
    assert columns["observed_source_revision"] == "a" * 64


# --------------------------------------------------------------------------
# D-06: the unsupported-companion diagnostic (COMP-02).
#
# This field flows CLI -> control plane, the opposite direction from the
# manifest, so the BACKEND is the consumer and must ship first. See
# 04-RESEARCH.md "Consumer-First Has Two Directions": _StrictObservedWireModel
# is extra="forbid", so a CLI that sends this field to a backend that does not
# know it has its ENTIRE observation rejected -- losing the diagnostic and the
# convergence report together.
# --------------------------------------------------------------------------


def _unsupported(**overrides: object) -> dict:
    record: dict = {"kind": "future", "cliVersion": "1.2.3"}
    record.update(overrides)
    return record


def test_unsupported_companions_accepts_a_kind_and_cli_version_record() -> None:
    payload = _payload()
    payload["unsupportedCompanions"] = [_unsupported()]

    value = TypeAdapter(HostedRuntimeObserved).validate_python(payload)

    assert isinstance(value, HostedRuntimeObservedV2)
    assert value.unsupported_companions is not None
    (record,) = value.unsupported_companions
    assert record.kind == "future"
    assert record.cli_version == "1.2.3"
    # The wire casing must survive the round trip, or the CLI and the
    # control plane are not speaking the same contract.
    assert value.model_dump(mode="json", by_alias=True, exclude_unset=True)[
        "unsupportedCompanions"
    ] == [{"kind": "future", "cliVersion": "1.2.3"}]


def test_unsupported_companions_absent_leaves_the_attribute_none() -> None:
    """None, never [] -- exclude_unset must drop the key for a deployed CLI.

    A default of `[]` would make the field "set" on every observation and
    change the stored diagnostics object for every runtime that has never
    heard of it. `None` is what keeps that serialization byte-identical.
    """

    value = TypeAdapter(HostedRuntimeObserved).validate_python(_payload())

    assert isinstance(value, HostedRuntimeObservedV2)
    assert value.unsupported_companions is None
    assert "unsupportedCompanions" not in value.model_dump(
        mode="json", by_alias=True, exclude_unset=True
    )


def test_unsupported_companions_accepts_an_empty_array() -> None:
    payload = _payload()
    payload["unsupportedCompanions"] = []

    value = TypeAdapter(HostedRuntimeObserved).validate_python(payload)

    assert isinstance(value, HostedRuntimeObservedV2)
    assert value.unsupported_companions == []


@pytest.mark.parametrize("third_key", ["entry", "value", "auth", "secret", "manifest"])
def test_unsupported_companion_record_rejects_any_third_key(third_key: str) -> None:
    """T-4-07: the record structurally cannot carry the companion's entry.

    The filebrowser companion entry embeds a live HS256 secret at
    `auth.secret`. A "record the unparsed entry so we can diagnose it"
    implementation would ship that secret into the diagnostics JSONB column.
    extra="forbid" makes it impossible to send, not merely discouraged.
    """

    payload = _payload()
    payload["unsupportedCompanions"] = [_unsupported(**{third_key: {"secret": "s" * 43}})]

    with pytest.raises(ValidationError):
        TypeAdapter(HostedRuntimeObserved).validate_python(payload)


@pytest.mark.parametrize(
    "kind",
    ["", "a" * 32, "Future", "1future", "has_underscore", "trailing space"],
)
def test_unsupported_companion_kind_is_bounded_by_the_registry_key_pattern(kind: str) -> None:
    payload = _payload()
    payload["unsupportedCompanions"] = [_unsupported(kind=kind)]

    with pytest.raises(ValidationError):
        TypeAdapter(HostedRuntimeObserved).validate_python(payload)


@pytest.mark.parametrize("cli_version", ["", "9" * 201])
def test_unsupported_companion_cli_version_is_bounded(cli_version: str) -> None:
    payload = _payload()
    payload["unsupportedCompanions"] = [_unsupported(cliVersion=cli_version)]

    with pytest.raises(ValidationError):
        TypeAdapter(HostedRuntimeObserved).validate_python(payload)


def test_unsupported_companions_array_is_length_bounded() -> None:
    payload = _payload()
    payload["unsupportedCompanions"] = [_unsupported(kind=f"kind-{index}") for index in range(31)]

    with pytest.raises(ValidationError):
        TypeAdapter(HostedRuntimeObserved).validate_python(payload)


def test_unsupported_companion_model_is_sealed_at_two_fields() -> None:
    from app.schemas.runtime_observed import HostedRuntimeUnsupportedCompanionV1

    assert set(HostedRuntimeUnsupportedCompanionV1.model_fields) == {"kind", "cli_version"}


def test_unsupported_companion_kind_pattern_matches_the_companion_registry() -> None:
    """Pin the local bound to the registry the moment the registry exists.

    `app.core.companion_registry` is introduced by plan 04-02, which runs in
    the SAME wave as this plan, so it is not importable from this worktree.
    `runtime_observed.py` therefore carries a provisional local mirror of
    `COMPANION_KEY_PATTERN`. This assertion is what stops the mirror from
    drifting: it activates automatically once 04-02 merges, and goes red if
    the two spellings ever disagree.
    """

    from app.schemas.runtime_observed import COMPANION_KEY_PATTERN

    try:
        from app.core.companion_registry import (  # type: ignore[import-not-found]
            COMPANION_KEY_PATTERN as REGISTRY_KEY_PATTERN,
        )
    except ImportError:  # pragma: no cover - exercised only pre-04-02 merge
        pytest.skip("app.core.companion_registry lands in plan 04-02 (same wave)")

    # The registry may export a str or a compiled pattern; compare the source.
    registry_source = getattr(REGISTRY_KEY_PATTERN, "pattern", REGISTRY_KEY_PATTERN)
    assert registry_source == COMPANION_KEY_PATTERN


def test_unsupported_companion_kind_pattern_admits_a_well_formed_unknown_kind() -> None:
    """Guard the parametrized rejection cases against a vacuously strict bound.

    Every case above asserts a rejection. Without this, a pattern that
    rejected everything would read as a fully green suite.
    """

    from app.schemas.runtime_observed import COMPANION_KEY_PATTERN

    assert re.match(COMPANION_KEY_PATTERN, "future") is not None
    assert re.match(COMPANION_KEY_PATTERN, "a" * 31) is not None
    assert re.match(COMPANION_KEY_PATTERN, "file-browser-2") is not None


# --------------------------------------------------------------------------
# The real ingest-and-persist path.
#
# The schema cases above prove the field validates. These prove it survives
# the heartbeat route into `hosted_runtime_config_observations.diagnostics`,
# and -- the load-bearing one -- that an observation which OMITS the field
# stores a byte-identical object to what the pre-04-03 code stored. That
# equality is the whole reason this plan is ordered before the CLI emitter.
# --------------------------------------------------------------------------

_OBSERVED_CLI_PACKAGE_SPEC = "clawdi@1.2.3-test"
_OBSERVED_CLI_VERSION = "1.2.3-test"
_OBSERVED_INSTANCE_ID = "iid-observed"
_OBSERVED_SOURCE_REVISION = "b" * 64
# The v2 bundle ETag form (`expected_runtime_bundle_v2_etag`): the readiness reader
# compares the applied ETag against the one derived from the source revision.
_OBSERVED_ETAG = f'"sha256:{_OBSERVED_SOURCE_REVISION}"'
_OBSERVED_GENERATION = 4
_OBSERVED_REPORTED_AT = "2026-07-13T00:00:00Z"


def _heartbeat_observed(*, unsupported: list[dict] | None = None) -> dict:
    """A v2 observation. `unsupported=None` omits the key entirely."""
    payload: dict = {
        "schemaVersion": "clawdi.hostedRuntimeObserved.v2",
        "reportedAt": _OBSERVED_REPORTED_AT,
        "runtimeMode": "hosted",
        "status": "ok",
        "activeCliVersion": _OBSERVED_CLI_VERSION,
        "applied": {
            "etag": _OBSERVED_ETAG,
            "sourceRevision": _OBSERVED_SOURCE_REVISION,
            "generation": _OBSERVED_GENERATION,
            "instanceId": _OBSERVED_INSTANCE_ID,
            "appliedProviderIds": ["clawdi-managed"],
        },
        "boot": None,
        "cli": None,
    }
    if unsupported is not None:
        payload["unsupportedCompanions"] = unsupported
    return payload


async def _env_with_hosted_state(client, db_session) -> str:
    """Register an env and give it the HostedRuntimeState the route requires.

    The heartbeat only writes an observation row when a HostedRuntimeState
    exists for the agent (see the outerjoin in `sync_heartbeat`).
    """
    from tests.hosted_runtime_fixtures import canonical_hosted_runtime_state

    response = await client.post(
        "/v1/environments",
        json={
            "machine_id": uuid.uuid4().hex,
            "machine_name": "observed-04-03",
            "agent_type": "claude_code",
            "os": "linux",
        },
    )
    assert response.status_code == 200, response.text
    env_id = response.json()["id"]

    db_session.add(
        canonical_hosted_runtime_state(
            environment_id=uuid.UUID(env_id),
            deployment_id="dep-04-03",
            instance_id=_OBSERVED_INSTANCE_ID,
            generation=_OBSERVED_GENERATION,
            cli_package_spec=_OBSERVED_CLI_PACKAGE_SPEC,
            locale={"language": "en", "timezone": "UTC"},
            system={},
            live_sync={"enabled": False, "agents": []},
            recovery={"cacheManifest": True, "allowOfflineBoot": True},
            runtimes={
                "openclaw": {
                    "enabled": True,
                    "providerMode": "configured",
                    "provider_ids": ["clawdi-managed"],
                    "primary_model": {"provider_id": "clawdi-managed", "model": "gpt-5.5"},
                    "install": {"source": "official"},
                }
            },
        )
    )
    await db_session.commit()
    return env_id


async def _stored_diagnostics(client, db_session, observed: dict) -> dict:
    from app.models.hosted_runtime import HostedRuntimeConfigObservation

    env_id = await _env_with_hosted_state(client, db_session)
    response = await client.post(
        f"/v1/agents/{env_id}/sync-heartbeat",
        json={"queue_depth": 1, "runtime_observed": observed},
    )
    assert response.status_code == 204, response.text

    observation = await db_session.get(HostedRuntimeConfigObservation, uuid.UUID(env_id))
    assert observation is not None
    return observation.diagnostics


async def test_unsupported_companions_round_trips_through_the_ingest_path(
    client, db_session
) -> None:
    diagnostics = await _stored_diagnostics(
        client,
        db_session,
        _heartbeat_observed(
            unsupported=[
                {"kind": "future", "cliVersion": "9.9.9"},
                {"kind": "second-future", "cliVersion": "9.9.9"},
            ]
        ),
    )

    assert diagnostics["unsupportedCompanions"] == [
        {"kind": "future", "cliVersion": "9.9.9"},
        {"kind": "second-future", "cliVersion": "9.9.9"},
    ]


async def test_observation_without_the_field_stores_byte_identical_diagnostics(
    client, db_session
) -> None:
    """The deployed-CLI guarantee (T-4-20), asserted as exact equality.

    Not a subset match: the stored object must equal this dict key for key.
    A `[]` default, or any eager population of the new field, would add a
    `unsupportedCompanions` key here and change the stored diagnostics for
    every runtime in the fleet that has never heard of it.
    """

    observed = _heartbeat_observed()
    assert "unsupportedCompanions" not in observed

    diagnostics = await _stored_diagnostics(client, db_session, observed)

    assert diagnostics == {
        "schemaVersion": "clawdi.hostedRuntimeObserved.v2",
        "reportedAt": _OBSERVED_REPORTED_AT,
        "runtimeMode": "hosted",
        "status": "ok",
        "activeCliVersion": _OBSERVED_CLI_VERSION,
        "applied": {
            "etag": _OBSERVED_ETAG,
            "sourceRevision": _OBSERVED_SOURCE_REVISION,
            "generation": _OBSERVED_GENERATION,
            "instanceId": _OBSERVED_INSTANCE_ID,
            "appliedProviderIds": ["clawdi-managed"],
        },
        "boot": None,
        "cli": None,
    }


def _readiness_inputs(observed: dict):
    """Unpersisted objects for the pure hosted readiness read.

    The reader was `agent_supports_project_skills` until upstream #1091 made Project
    Skills desired state and deleted it, then `runtime_source._codex_tool_runtime_env`
    until upstream retired that too (sync step 3). `_runtime_observed_evidence` in
    `runtime_observed_evidence.py` now evaluates the conjunction over the same rows
    (status, applied instance, generation, etag and source revision), so it carries
    the verdict.
    """
    from app.models.hosted_runtime import HostedRuntimeConfigObservation, HostedRuntimeState
    from app.models.session import AgentEnvironment

    now = datetime.now(UTC)
    agent = AgentEnvironment(
        last_sync_at=now,
        last_sync_error=None,
        registration_key="reg-04-03",
    )
    state = HostedRuntimeState(
        instance_id=_OBSERVED_INSTANCE_ID,
        generation=_OBSERVED_GENERATION,
        apply_generation=None,
        cli_package_spec=_OBSERVED_CLI_PACKAGE_SPEC,
    )
    observation = HostedRuntimeConfigObservation(
        observed_at=now,
        observed_config_generation=_OBSERVED_GENERATION,
        observed_manifest_etag=_OBSERVED_ETAG,
        observed_source_revision=_OBSERVED_SOURCE_REVISION,
        diagnostics=observed,
    )
    return agent, state, observation


@pytest.mark.parametrize(
    "unsupported",
    [None, [], [{"kind": "future", "cliVersion": "9.9.9"}]],
)
def test_readiness_reader_verdict_is_unchanged_by_unsupported_companions(
    unsupported: list[dict] | None,
) -> None:
    """The addition must not move any existing readiness decision.

    Asserts `True` rather than "both the same" on purpose: a pair of `False`
    verdicts would agree with each other while proving nothing about the
    field's effect on the path that actually matters.
    """

    from app.services.runtime_observed_evidence import _runtime_observed_evidence

    _agent, state, observation = _readiness_inputs(_heartbeat_observed(unsupported=unsupported))

    evidence = _runtime_observed_evidence(
        state=state,
        observation=observation,
        desired_source_revision=_OBSERVED_SOURCE_REVISION,
        current=observation.observed_at,
        freshness=timedelta(minutes=5),
    )
    # The ready verdict, not merely an unchanged one: the not-ready answers are
    # `converged=False` or a status other than "ok".
    assert (evidence.status, evidence.converged) == ("ok", True)


def test_purged_observation_diagnostics_behaviour_is_unchanged() -> None:
    """A purged row stores `{}`; the emit gate in 04-05 inherits this.

    `payload_purged_at IS NULL OR diagnostics = '{}'::jsonb` is a DB check
    constraint, so a purged observation yields an empty object rather than a
    version. Validation must still reject it exactly as it does today.
    """

    from app.models.hosted_runtime import HostedRuntimeConfigObservation
    from app.routes.sessions import _validated_runtime_observed_diagnostics

    purged = HostedRuntimeConfigObservation(diagnostics={})

    assert _validated_runtime_observed_diagnostics(purged) is None
