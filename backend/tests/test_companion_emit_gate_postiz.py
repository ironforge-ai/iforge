"""The emit gate, run against the SHIPPED gated companion kind (06-03).

Phase 4 wired the consumer-first emit gate (D-08/D-15) while every registry row was ungated, so
the gate's refusal path was only ever exercised against a stand-in kind injected into the
registry for the duration of a test. Phase 4 deferred obligation #3 records exactly that gap: the
gate had never run against a gated kind the product actually ships. ``postiz`` is that kind --
``minCliVersion`` ``0.14.0``, above every deployed CLI -- so this module exercises the gate
against the real registry embed and NOTHING ELSE.

The rule for this module is therefore structural, not stylistic: no test here swaps, patches,
injects or replaces any entry of ``COMPANION_REGISTRY``, and no fixture does it on a test's
behalf. A substituted row would re-create the very gap this module exists to close, because it
would prove the gate works for a row somebody wrote in a test rather than for the row the
contract file ships. Every test first asserts, through ``_shipped_postiz``, that the embed's
``postiz`` row is the one ``config/companion-registry.txt`` declares.

What this module does NOT claim: that a real runtime reporting a stale CLI has been observed
receiving a manifest without ``postiz``. That is the host half of obligation #3, and it stays a
host UAT item -- a repository test cannot observe a provisioned runtime.

**Mutation check performed by hand while writing this module.** ``min_cli_version`` of the
``postiz`` entry in ``app/core/companion_registry.py`` was set to ``"0.0.0"``: the two
withholding tests and the key-only record test went red (the gate admitted ``postiz`` and
recorded no omission), and ``_shipped_postiz`` went red in every test (the embed no longer
equalled the contract file). The value was restored immediately.
"""

from __future__ import annotations

import dataclasses
import json
from uuid import UUID

from app.core.companion_registry import (
    COMPANION_REGISTRY,
    CompanionRegistryEntry,
    parse_companion_contract,
)
from app.models.hosted_runtime import HostedRuntimeConfigObservation
from app.services.runtime_source import (
    CompanionEmitDecision,
    CompanionEmitOmission,
    CompanionEmitOmissionRecord,
    _gate_companion_kinds,
)
from tests.doc_contract import REPO_ROOT

CONTRACT_PATH = REPO_ROOT / "config" / "companion-registry.txt"
ENV_ID = UUID("20000000-0000-0000-0000-000000000603")

# The CLI version every deployed runtime reports today: `packages/cli/package.json`.
DEPLOYED_CLI_VERSION = "0.13.47"
POSTIZ_MIN_CLI_VERSION = "0.14.0"

# The two kinds a manifest can declare. Sorted, and passed in this order, so `admitted` compares
# as a sequence.
DECLARED = ("filebrowser", "postiz")

# A value no omission record may ever carry. Planted inside the declared ENTRY VALUES below, so a
# gate that read an entry rather than a key would carry it into its output.
ENTRY_VALUE_SENTINEL = "entry-value-sentinel-06-03"


def _shipped_postiz() -> CompanionRegistryEntry:
    """The ``postiz`` row of the SHIPPED embed, proven equal to the contract file's row."""
    contract = parse_companion_contract(CONTRACT_PATH.read_text(encoding="utf-8")).kinds
    entry = COMPANION_REGISTRY.get("postiz")

    assert entry is not None, "the shipped registry has no postiz row"
    assert entry == contract["postiz"], "the embed's postiz row is not the contract file's row"
    assert entry.min_cli_version == POSTIZ_MIN_CLI_VERSION
    assert COMPANION_REGISTRY["filebrowser"] == contract["filebrowser"]
    return entry


def _observation(active_cli_version: str | None) -> HostedRuntimeConfigObservation:
    return HostedRuntimeConfigObservation(
        environment_id=ENV_ID,
        diagnostics={
            "schemaVersion": "clawdi.hostedRuntimeObserved.v2",
            "reportedAt": "2026-09-23T00:00:00+00:00",
            "runtimeMode": "hosted",
            "status": "ok",
            "activeCliVersion": active_cli_version,
            "applied": None,
            "boot": None,
            "cli": None,
        },
    )


def _only_omission(decision: CompanionEmitDecision) -> CompanionEmitOmissionRecord:
    assert len(decision.omissions) == 1, decision.omissions
    return decision.omissions[0]


def test_shipped_postiz_row_is_withheld_from_a_stale_runtime() -> None:
    _shipped_postiz()

    decision = _gate_companion_kinds(DECLARED, _observation(DEPLOYED_CLI_VERSION))

    assert decision.admitted == ("filebrowser",)
    record = _only_omission(decision)
    assert record.kind == "postiz"
    assert record.cause is CompanionEmitOmission.STALE_REPORTED_VERSION
    assert record.reported_version == DEPLOYED_CLI_VERSION
    assert record.required_min_cli_version == POSTIZ_MIN_CLI_VERSION


def test_shipped_postiz_row_is_admitted_at_its_minimum() -> None:
    _shipped_postiz()

    decision = _gate_companion_kinds(DECLARED, _observation(POSTIZ_MIN_CLI_VERSION))

    assert decision.admitted == DECLARED
    assert decision.omissions == ()


def test_shipped_postiz_row_is_withheld_without_an_observation() -> None:
    # D-15: no observation is no proof the CLI can parse anything, so the gate fails CLOSED and
    # emits only the ungated floor -- and says why, rather than dropping postiz silently.
    _shipped_postiz()

    decision = _gate_companion_kinds(DECLARED, None)

    assert decision.admitted == ("filebrowser",)
    record = _only_omission(decision)
    assert record.kind == "postiz"
    assert record.cause is CompanionEmitOmission.NO_OBSERVATION
    assert record.reported_version is None
    assert record.required_min_cli_version == POSTIZ_MIN_CLI_VERSION


def test_omission_record_carries_the_key_only() -> None:
    # The gate takes KEYS. Handing it a mapping whose VALUES carry a planted sentinel -- the shape
    # a manifest's companions object has, where filebrowser embeds a live HS256 secret -- proves
    # structurally that nothing from an entry value can reach the record: iterating a mapping
    # yields its keys, and the record has no field an entry value could land in.
    _shipped_postiz()
    declared_entries = {
        "filebrowser": {"auth": {"secret": ENTRY_VALUE_SENTINEL}},
        "postiz": {"credentials": {"apiKeySecretRef": ENTRY_VALUE_SENTINEL}},
    }

    decision = _gate_companion_kinds(declared_entries, _observation(DEPLOYED_CLI_VERSION))

    record = _only_omission(decision)
    serialized = json.dumps(dataclasses.asdict(record), default=str, sort_keys=True)
    assert {field.name for field in dataclasses.fields(record)} == {
        "kind",
        "cause",
        "reported_version",
        "required_min_cli_version",
    }
    assert '"kind": "postiz"' in serialized
    assert ENTRY_VALUE_SENTINEL not in serialized
    assert "secret" not in serialized
    assert "credentials" not in serialized
