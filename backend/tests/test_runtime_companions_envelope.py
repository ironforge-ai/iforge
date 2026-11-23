"""The Pydantic half of the companions envelope (D-07).

Eight behaviours, each one the mirror of a named test in
``packages/cli/src/runtime/manifest-contract.test.ts``. The two halves are asserted against the
same payload shapes on purpose: ``HostedRuntimeCompanions`` is what the backend validates before
rendering a manifest and ``runtimeCompanionsSchema`` is what the CLI validates after receiving
one, so a divergence between them is a contract only one side honors -- the divergence class
phase 03.3 already paid for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.runtime import (
    OFFERED_ISOLATION_TIERS,
    POSTIZ_POSTGRES_IMAGE,
    HostedPostizCompanion,
    HostedPostizDependencies,
    HostedRuntimeCompanions,
    assert_offered_isolation_tier,
)
from tests.hosted_runtime_fixtures import filebrowser_companion

SHA256_HEX = "a" * 64

# The cross-language golden entry. `packages/cli/src/runtime/manifest-contract.test.ts` parses the
# SAME file, so the Pydantic half and the Zod half are pinned to each other by one wire payload
# rather than by two hand-written copies that can drift apart.
POSTIZ_FIXTURE_PATH = (
    Path(__file__).parents[2] / "test-fixtures" / "companions" / "postiz-manifest-entry.json"
)


def _postiz_fixture() -> dict[str, object]:
    return json.loads(POSTIZ_FIXTURE_PATH.read_text(encoding="utf-8"))


def _error_locs(exc: pytest.ExceptionInfo[ValidationError]) -> list[str]:
    return [".".join(str(part) for part in error["loc"]) for error in exc.value.errors()]


def _reject(payload: dict[str, object]) -> list[str]:
    """Return the error locations, THROWING if the envelope accepted the payload.

    Returning an empty list on acceptance would let a membership assertion fail with a message
    about the wrong thing, which is how a parity suite ends up proving nothing.
    """
    with pytest.raises(ValidationError) as exc:
        HostedRuntimeCompanions.model_validate(payload)
    return _error_locs(exc)


def test_admits_an_unrecognised_kind_with_a_kind_identity_and_a_pinned_artifact() -> None:
    wire = {"future": {"kind": "future", "artifact": {"sha256": SHA256_HEX}}}

    companions = HostedRuntimeCompanions.model_validate(wire)

    # COMP-02: the entry survives the parse verbatim so the rest of the manifest reconciles.
    assert companions.model_extra == wire
    assert companions.filebrowser is None


def test_rejects_a_typo_key_whose_value_is_malformed() -> None:
    locs = _reject({"filebrowzer": {"a": 1}})

    # D-05: a typo still fails loudly, and the offending key names itself in the error location.
    assert any(loc.startswith("filebrowzer") for loc in locs)


def test_rejects_an_unrecognised_kind_whose_artifact_digest_is_not_64_hex() -> None:
    locs = _reject({"future": {"kind": "future", "artifact": {"sha256": "nope"}}})

    # COMP-06: fail closed rather than deploy an artifact that is not digest-pinned.
    assert "future.artifact.sha256" in locs


def test_rejects_an_unrecognised_kind_that_carries_no_artifact() -> None:
    locs = _reject({"future": {"kind": "future"}})

    # COMP-06: an absent pin is the same wire event as a malformed one.
    assert "future.artifact" in locs


def test_rejects_a_well_formed_entry_whose_kind_disagrees_with_its_key() -> None:
    locs = _reject({"filebrowzer": {"kind": "filebrowser", "artifact": {"sha256": SHA256_HEX}}})

    # The hole D-05 omits. Without this guard the mis-keyed entry parses and is silently treated
    # as a future kind to skip, which is a spoofing surface (T-4-03), not a typo.
    assert any(loc.startswith("filebrowzer") for loc in locs)


def test_admits_a_companion_less_manifest() -> None:
    companions = HostedRuntimeCompanions.model_validate({})

    assert companions.filebrowser is None
    assert companions.model_dump(exclude_none=True, exclude_unset=True, mode="json") == {}


def test_rejects_a_key_outside_the_registry_charset() -> None:
    # T-4-01: the key becomes a systemd unit name, a UID lookup name and a path component. It is
    # bounded at the trust boundary, not downstream where a traversal would already have happened.
    locs = _reject({"../etc": {"kind": "../etc", "artifact": {"sha256": SHA256_HEX}}})

    assert any(loc.startswith("../etc") for loc in locs)


def test_leaves_the_serialized_filebrowser_entry_byte_identical() -> None:
    # D-04 binds the WIRE JSON. This is the exact dump call `runtime_source.render_runtime_source`
    # makes at the `companions` render site, on the exact fixture the manifest suites use.
    wire = filebrowser_companion("hdep_files_envelope")

    dumped = HostedRuntimeCompanions.model_validate(wire).model_dump(
        exclude_none=True, exclude_unset=True, mode="json"
    )

    assert json.dumps(dumped, sort_keys=True) == json.dumps(wire, sort_keys=True)
    assert dumped == wire


def test_every_entry_model_stays_sealed_while_only_the_envelope_is_loosened() -> None:
    # T-4-18: the loosening is the ENVELOPE's alone. A filebrowser entry that grows an
    # undeclared field must still be rejected, or `extra="allow"` has leaked one level down.
    wire = filebrowser_companion("hdep_files_envelope")
    wire["filebrowser"]["kind"] = "filebrowser"  # type: ignore[index]

    _reject(wire)


def test_the_envelope_declares_postiz_so_a_postiz_entry_is_governed_by_its_own_model() -> None:
    # Before this field existed a postiz entry fell into `model_extra` and was judged by the
    # minimal unknown-kind shape, which rejects every real postiz field as undeclared.
    assert "postiz" in HostedRuntimeCompanions.model_fields


def test_the_envelope_round_trips_the_shared_postiz_fixture_byte_identically() -> None:
    wire = {"postiz": _postiz_fixture()}

    dumped = HostedRuntimeCompanions.model_validate(wire).model_dump(
        exclude_unset=True, mode="json"
    )

    assert json.dumps(dumped, sort_keys=True) == json.dumps(wire, sort_keys=True)


def test_hosted_postiz_companion_parses_the_shared_fixture_and_re_serializes_it() -> None:
    fixture = _postiz_fixture()

    dumped = HostedPostizCompanion.model_validate(fixture).model_dump(
        exclude_unset=True, mode="json"
    )

    assert dumped == fixture
    assert json.dumps(dumped, sort_keys=True) == json.dumps(fixture, sort_keys=True)


def test_the_dependency_set_is_exactly_postgres_redis_and_cloudflared() -> None:
    # D-04 plus CK-A = own-redis-shared-temporal: Redis is a per-installation container, Temporal
    # is connection material with no image slot, and no search-engine dependency exists.
    assert sorted(HostedPostizDependencies.model_fields) == ["cloudflared", "postgres", "redis"]
    entry = _postiz_fixture()
    del entry["dependencies"]["redis"]  # type: ignore[attr-defined]

    with pytest.raises(ValidationError) as exc:
        HostedPostizCompanion.model_validate(entry)
    assert "dependencies.redis" in _error_locs(exc)


def test_the_postgres_slot_names_the_house_image_repository() -> None:
    authority = (Path(__file__).parents[2] / "config" / "postgres-image.txt").read_text(
        encoding="utf-8"
    )

    assert authority.strip().split(":pg18@")[0] == POSTIZ_POSTGRES_IMAGE


# The refusal matrix. Its cases live in ONE shared file that
# `packages/cli/src/runtime/manifest-contract.test.ts` also reads, so the Pydantic half and the Zod
# half are asserted against the same mutations by construction rather than by two hand-kept lists.
POSTIZ_REFUSAL_CASES: list[dict[str, object]] = json.loads(
    (POSTIZ_FIXTURE_PATH.parent / "postiz-refusal-cases.json").read_text(encoding="utf-8")
)


def _mutated_postiz_fixture(mutation: dict[str, object]) -> dict[str, object]:
    entry = _postiz_fixture()
    path = list(mutation["path"])  # type: ignore[call-overload]
    target: dict[str, object] = entry
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment]
    if mutation["op"] == "delete":
        del target[path[-1]]
    else:
        target[path[-1]] = mutation["value"]
    return entry


def _is_under(loc: str, at: str) -> bool:
    return at == "" or loc == at or loc.startswith(f"{at}.")


def test_the_refusal_matrix_covers_every_refusal_the_contract_must_make() -> None:
    assert {mutation["refusal"] for mutation in POSTIZ_REFUSAL_CASES} == {
        "missing-digest",
        "malformed-digest",
        "tag-field",
        "foreign-repository",
        "undeclared-dependency",
        "unknown-top-level-field",
        "non-canonical-credential-ref",
        "lifecycle-outside-vocabulary",
        "uppercase-hostname-label",
        "single-label-hostname",
        "too-many-export-requests",
        "wrong-port",
    }


@pytest.mark.parametrize(
    "mutation",
    POSTIZ_REFUSAL_CASES,
    ids=[f"{mutation['refusal']}: {mutation['case']}" for mutation in POSTIZ_REFUSAL_CASES],
)
def test_hosted_postiz_companion_refuses(mutation: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as exc:
        HostedPostizCompanion.model_validate(_mutated_postiz_fixture(mutation))

    locs = _error_locs(exc)
    # Every error sits under the mutated object, so the refusal is for the intended reason and
    # not an unrelated field the mutation happened to disturb.
    assert locs
    assert [loc for loc in locs if not _is_under(loc, str(mutation["at"]))] == []


def test_the_envelope_refuses_a_postiz_entry_that_fails_its_own_model() -> None:
    locs = _reject({"postiz": _mutated_postiz_fixture(POSTIZ_REFUSAL_CASES[0])})

    assert locs
    assert all(loc.startswith("postiz.") for loc in locs)


def test_the_contract_admits_all_three_tiers_and_offers_only_standard() -> None:
    tier_field = HostedPostizCompanion.model_fields["isolationTier"]

    assert tier_field.annotation is not None
    assert list(tier_field.annotation.__args__) == ["standard", "dedicated", "shared"]
    assert OFFERED_ISOLATION_TIERS == ("standard",)


@pytest.mark.parametrize("tier", ["dedicated", "shared"])
def test_an_unoffered_tier_parses_but_is_refused_by_name(tier: str) -> None:
    entry = HostedPostizCompanion.model_validate({**_postiz_fixture(), "isolationTier": tier})

    assert entry.isolationTier == tier
    with pytest.raises(ValueError, match=f"isolation tier {tier}"):
        assert_offered_isolation_tier(entry.isolationTier)


def test_standard_is_offered() -> None:
    entry = HostedPostizCompanion.model_validate(_postiz_fixture())

    assert_offered_isolation_tier(entry.isolationTier)


@pytest.mark.parametrize("field", ["backupEnvSecretRef", "tunnelTokenSecretRef"])
def test_an_optional_credential_ref_must_be_omitted_rather_than_null(field: str) -> None:
    # Parity with the Zod half, whose `.optional()` admits absent but refuses `null`.
    entry = _postiz_fixture()
    entry["credentials"][field] = None  # type: ignore[index]

    with pytest.raises(ValidationError) as exc:
        HostedPostizCompanion.model_validate(entry)
    assert _error_locs(exc) == [f"credentials.{field}"]


# CR-01 (plan 06-35): a backup repository path is the installation's own directory or one
# abandoned-backups generation inside it, `/r<n>` with no leading zero and at most six digits.
# These two lists are repeated value for value in
# `packages/cli/src/runtime/manifest-contract.test.ts`, so a drift between the two parsers fails
# one side.
BACKUP_REPO_PATHS_ACCEPTED = ["/t/i", "/t/i/r1", "/t/i/r999999"]
BACKUP_REPO_PATHS_REFUSED = ["/t/i/r0", "/t/i/r01", "/t/i/x1", "/t/i/r1/extra", "/t/i/r1000000"]


def _postiz_with_backup_repo_path(repo_path: str) -> dict[str, object]:
    entry = _postiz_fixture()
    entry["backup"] = {"stanza": "postiz-abc", "repoPath": repo_path}
    return entry


@pytest.mark.parametrize("repo_path", BACKUP_REPO_PATHS_ACCEPTED)
def test_the_backup_repo_path_admits_the_installation_directory_and_its_generations(
    repo_path: str,
) -> None:
    entry = HostedPostizCompanion.model_validate(_postiz_with_backup_repo_path(repo_path))

    assert entry.backup is not None
    assert entry.backup.repoPath == repo_path


@pytest.mark.parametrize("repo_path", BACKUP_REPO_PATHS_REFUSED)
def test_the_backup_repo_path_refuses_anything_else(repo_path: str) -> None:
    with pytest.raises(ValidationError) as exc:
        HostedPostizCompanion.model_validate(_postiz_with_backup_repo_path(repo_path))

    assert _error_locs(exc) == ["backup.repoPath"]


# Plan 06-41 (CR-03): the announced app-digest transition, `{fromDigest, toDigest}` while an
# accepted update runs and null otherwise. These lists are repeated value for value in
# `packages/cli/src/runtime/manifest-contract.test.ts`, so a drift between the two parsers fails
# one side. Absent is accepted too, and defaults to null on both sides.
UPDATE_ANNOUNCEMENTS_ACCEPTED: list[dict[str, str] | None] = [
    None,
    {"fromDigest": "a" * 64, "toDigest": "b" * 64},
]
UPDATE_ANNOUNCEMENTS_REFUSED: list[dict[str, str]] = [
    {"fromDigest": "A" * 64, "toDigest": "b" * 64},
    {"fromDigest": "a" * 63, "toDigest": "b" * 64},
    {"fromDigest": "a" * 64, "toDigest": "b" * 64, "version": "v2.25.0"},
]


@pytest.mark.parametrize("update", UPDATE_ANNOUNCEMENTS_ACCEPTED)
def test_the_update_announcement_admits_null_and_a_digest_pair(
    update: dict[str, str] | None,
) -> None:
    entry = HostedPostizCompanion.model_validate({**_postiz_fixture(), "update": update})

    dumped = entry.model_dump(mode="json")
    assert dumped["update"] == update


def test_an_absent_update_announcement_defaults_to_null() -> None:
    fixture = _postiz_fixture()
    del fixture["update"]

    assert HostedPostizCompanion.model_validate(fixture).update is None


@pytest.mark.parametrize("update", UPDATE_ANNOUNCEMENTS_REFUSED)
def test_the_update_announcement_refuses_anything_else(update: dict[str, str]) -> None:
    with pytest.raises(ValidationError) as exc:
        HostedPostizCompanion.model_validate({**_postiz_fixture(), "update": update})

    locs = _error_locs(exc)
    assert locs
    assert [loc for loc in locs if not _is_under(loc, "update")] == []


# Plan 06-42 (CR-04): the agent's retired installations of the kind, at most 8 `{installationId}`
# objects with a lowercase UUID. These lists are repeated value for value in
# `packages/cli/src/runtime/manifest-contract.test.ts`, so a drift between the two parsers fails
# one side. Absent is accepted too, and defaults to [] on both sides.
def _retired_installations(count: int) -> list[dict[str, str]]:
    return [
        {"installationId": f"{index:08x}-6d2f-4e8b-a7c3-5f1d2e3a4b6c"} for index in range(count)
    ]


RETIRED_INSTALLATIONS_ACCEPTED: list[list[dict[str, str]]] = [
    [],
    _retired_installations(1),
    _retired_installations(8),
]
RETIRED_INSTALLATIONS_REFUSED: list[list[dict[str, str]]] = [
    _retired_installations(9),
    [{"installationId": "0B9E4C1A-6D2F-4E8B-A7C3-5F1D2E3A4B6C"}],
    [{"installationId": "0b9e4c1a-6d2f-4e8b-a7c3-5f1d2e3a4b6c", "kind": "postiz"}],
]


@pytest.mark.parametrize("retired", RETIRED_INSTALLATIONS_ACCEPTED)
def test_retired_installations_admit_up_to_eight_lowercase_ids(
    retired: list[dict[str, str]],
) -> None:
    entry = HostedPostizCompanion.model_validate(
        {**_postiz_fixture(), "retiredInstallations": retired}
    )

    assert entry.model_dump(mode="json")["retiredInstallations"] == retired


def test_absent_retired_installations_default_to_empty() -> None:
    fixture = _postiz_fixture()
    del fixture["retiredInstallations"]

    assert HostedPostizCompanion.model_validate(fixture).retiredInstallations == []


@pytest.mark.parametrize("retired", RETIRED_INSTALLATIONS_REFUSED)
def test_retired_installations_refuse_anything_else(retired: list[dict[str, str]]) -> None:
    with pytest.raises(ValidationError) as exc:
        HostedPostizCompanion.model_validate({**_postiz_fixture(), "retiredInstallations": retired})

    locs = _error_locs(exc)
    assert locs
    assert [loc for loc in locs if not _is_under(loc, "retiredInstallations")] == []
