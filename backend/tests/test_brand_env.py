"""The Python half of the public environment contract boundary.

Both halves answer to one file, ``config/rebrand-env-contract.txt``. That is the whole design,
so it is asserted here rather than assumed: the map this module exposes must equal the contract
file, and its key set must equal the TypeScript map in ``packages/cli/src/lib/brand-env.ts``.
"""

from __future__ import annotations

import re

from app.core.brand_env import (
    BRAND_ENV_ALIASES,
    apply_brand_env_aliases,
    refuse_user_supplied_internal_names,
)
from tests.doc_contract import REPO_ROOT

CONTRACT_PATH = REPO_ROOT / "config" / "rebrand-env-contract.txt"
TYPESCRIPT_BOUNDARY = REPO_ROOT / "packages" / "cli" / "src" / "lib" / "brand-env.ts"


def _parse_contract_file() -> dict[str, str]:
    pairs: dict[str, str] = {}
    for raw in CONTRACT_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        public_name, separator, internal_name = line.partition("=")
        assert separator, f"malformed contract line: {line!r}"
        pairs[public_name] = internal_name
    return pairs


def _typescript_alias_keys() -> set[str]:
    source = TYPESCRIPT_BOUNDARY.read_text(encoding="utf-8")
    body = source.split("export const BRAND_ENV_ALIASES", 1)[1].split("};", 1)[0]
    return set(re.findall(r"^\t(IFORGE_[A-Z0-9_]+):", body, flags=re.MULTILINE))


def test_public_name_reaches_the_internal_register():
    """INVERTED deliberately (plan 03.1-16, BRAND-02), mirroring the TypeScript half.

    This used to assert the public key was removed once consumed. Keeping it is now
    load-bearing: the public spelling is what a child process inherits and re-derives its own
    register from, which is what lets ``refuse_user_supplied_internal_names`` run
    unconditionally at every entrypoint with no provenance marker.
    """
    env = {"IFORGE_PROCESS_ROLE": "channels-worker"}

    assert apply_brand_env_aliases(env) == 1
    assert env["CLAWDI_PROCESS_ROLE"] == "channels-worker"
    assert env["IFORGE_PROCESS_ROLE"] == "channels-worker"


def test_a_bare_internal_name_is_neither_read_nor_deleted():
    """RESTATED, not deleted (plan 03.1-16): this now describes ``apply_brand_env_aliases`` alone.

    Applying still never deletes an internal name it did not set -- but it is no longer the
    whole story, because deletion now belongs to ``refuse_user_supplied_internal_names``, which
    runs first at the entrypoint (``app/runtime_entrypoint.py``). The old reason for the additive
    behaviour (parent processes write mapped internal names into child environments) is
    preserved by the new design rather than abandoned: those children now receive the PUBLIC
    spelling. See ``test_a_bare_internal_name_is_refused_when_the_public_name_is_absent`` for the
    composed behaviour at the boundary.
    """
    env = {"CLAWDI_PROCESS_ROLE": "channels-worker"}

    assert apply_brand_env_aliases(env) == 0
    assert env == {"CLAWDI_PROCESS_ROLE": "channels-worker"}


def test_a_bare_internal_name_is_refused_when_the_public_name_is_absent():
    """The exclusive half of BRAND-02: an internal name with no public counterpart is deleted."""
    env = {"CLAWDI_PROCESS_ROLE": "channels-worker"}

    refused = refuse_user_supplied_internal_names(env)

    assert "CLAWDI_PROCESS_ROLE" in refused
    assert env == {}


def test_a_paired_internal_name_survives_the_refusal():
    """The parent set the public spelling, so the register value is this boundary's own work."""
    env = {
        "IFORGE_PROCESS_ROLE": "channels-worker",
        "CLAWDI_PROCESS_ROLE": "channels-worker",
    }

    refused = refuse_user_supplied_internal_names(env)

    assert refused == []
    assert env["CLAWDI_PROCESS_ROLE"] == "channels-worker"
    assert env["IFORGE_PROCESS_ROLE"] == "channels-worker"


def test_the_refusal_follows_the_contract_and_not_the_clawdi_prefix():
    """``CLAWDI_HOME`` is a non-member (D-07/X-1); it has no public spelling to be supplied under.

    A refusal driven by the ``CLAWDI_`` prefix rather than by the contract would strip the
    deferred state root and every other internal plumbing name the process legitimately reads.
    """
    env = {"CLAWDI_HOME": "/var/lib/clawdi", "CLAWDI_WA_EGRESS": "chain"}

    refused = refuse_user_supplied_internal_names(env)

    assert refused == []
    assert env == {"CLAWDI_HOME": "/var/lib/clawdi", "CLAWDI_WA_EGRESS": "chain"}


def test_an_empty_value_is_not_treated_as_set():
    env = {"IFORGE_PROCESS_ROLE": ""}

    assert apply_brand_env_aliases(env) == 0
    assert "CLAWDI_PROCESS_ROLE" not in env


def test_defaults_to_os_environ_and_is_idempotent(monkeypatch):
    """The no-argument call reads the real process environment, so it has to be isolated.

    The clean test runner exports IFORGE_NO_AUTO_UPDATE and IFORGE_NO_UPDATE_CHECK into the
    container (docker-compose.test.yml), and both are public names in the contract. A count
    asserted over an environment that already carries them measures the host, not the
    boundary: this test passed on a developer machine and failed inside `bun run test` with
    `assert 3 == 1`. Clearing every contract name first makes the assertion mean the same
    thing wherever it runs.
    """
    for public_name, internal_name in BRAND_ENV_ALIASES.items():
        monkeypatch.delenv(public_name, raising=False)
        monkeypatch.delenv(internal_name, raising=False)
    monkeypatch.setenv("IFORGE_PROCESS_ROLE", "channels-worker")

    assert apply_brand_env_aliases() == 1
    import os

    assert os.environ["CLAWDI_PROCESS_ROLE"] == "channels-worker"
    # The public key now SURVIVES (plan 03.1-16), so idempotence is in EFFECT rather than in the
    # returned count: a second call re-applies the same value and changes nothing. Asserting
    # `== 0` here would be asserting that the public spelling had been consumed away, which is
    # the behaviour this phase deliberately removed.
    assert os.environ["IFORGE_PROCESS_ROLE"] == "channels-worker"
    before = dict(os.environ)
    assert apply_brand_env_aliases() == 1
    assert dict(os.environ) == before
    assert os.environ["CLAWDI_PROCESS_ROLE"] == "channels-worker"


def test_alias_map_equals_the_contract_file():
    assert BRAND_ENV_ALIASES == _parse_contract_file()


def test_alias_map_key_set_equals_the_typescript_boundary():
    assert set(BRAND_ENV_ALIASES) == _typescript_alias_keys()


def test_every_pair_maps_a_public_name_onto_an_internal_one():
    assert BRAND_ENV_ALIASES
    for public_name, internal_name in BRAND_ENV_ALIASES.items():
        assert public_name.startswith("IFORGE_")
        assert internal_name.startswith("CLAWDI_")
        assert public_name != internal_name
