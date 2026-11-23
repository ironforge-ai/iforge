"""The three-way companion registry parity gate (D-07).

Three representations answer to one file, ``config/companion-registry.txt``: the contract file
itself, the TypeScript embed in ``packages/cli/src/runtime/companion-registry.ts`` and the Python
embed in ``app/core/companion_registry.py``. That is the whole design, so it is asserted here
rather than assumed -- and asserted in BOTH directions, because a one-directional check cannot
detect a kind that exists in one representation and not another.

This module is the single artifact that makes one authority out of three copies: it reads the
contract file, compares the Python embed to it, and extracts the TypeScript embed's key set by
source read (the ``test_brand_env.py`` idiom) so neither language can drift alone.

**Mutation checks performed by hand while writing the verb-table parity assertions (05-02).**
A parity assertion that cannot fail is worse than no assertion at all -- Phase 4 registered that
as T-4-19 -- so each direction below was exercised by deleting exactly one row from one
representation, confirming a red, and restoring it. The deletions used, recorded so a later
reader can repeat them rather than trust this paragraph:

* ``COMPANION_VERBS["filebrowser__read_file"]`` deleted from
  ``app/core/companion_registry.py`` -> ``test_python_verb_embed_equals_the_contract_file`` and
  ``test_python_verb_keys_equal_the_contract_file_verb_rows_in_both_directions`` fail on the
  "missing from Python" direction, and
  ``test_python_and_typescript_verb_key_sets_are_equal_in_both_directions`` fails on the
  "missing from Python" direction.
* ``filebrowser__write_file`` deleted from ``COMPANION_VERBS`` in
  ``packages/cli/src/runtime/companion-registry.ts`` ->
  ``test_python_and_typescript_verb_key_sets_are_equal_in_both_directions`` fails on the
  "missing from TypeScript" direction, and
  ``test_typescript_verb_keys_equal_the_contract_file_verb_rows_in_both_directions`` fails on the
  same direction.
* The ``verb|filebrowser|delete_path|...`` row deleted from
  ``config/companion-registry.txt`` -> both contract-file directions fail, from the Python side
  and from the TypeScript side, and
  ``test_iforge_companion_docs_contract.py::test_every_documented_verb_exists_in_the_contract_file``
  fails from the prose side.

Each deletion was restored immediately; the file this docstring ships with carries all six rows.
"""

from __future__ import annotations

import re

import pytest

from app.core.companion_registry import (
    COMPANION_KEY_PATTERN,
    COMPANION_REGISTRY,
    COMPANION_VERBS,
    KNOWN_COMPANION_KINDS,
    RESERVED_COMPANION_UID_BLOCK,
    CompanionContract,
    CompanionRegistryEntry,
    CompanionVerbEntry,
    companion_kinds_at_or_below,
    companion_min_cli_version,
    companion_verb,
    default_reserved_tool_names,
    derive_annotations,
    parse_companion_contract,
)
from tests.doc_contract import REPO_ROOT

CONTRACT_PATH = REPO_ROOT / "config" / "companion-registry.txt"
TYPESCRIPT_REGISTRY = REPO_ROOT / "packages" / "cli" / "src" / "runtime" / "companion-registry.ts"

# The Hermes dashboard's port. It is not a companion, so it owns no row -- but nothing may
# collide with it.
HERMES_DASHBOARD_PORT = 9119

# The two row types, as literal lines. Every parser case below is built from these so a grammar
# change shows up as one edit rather than as a scatter of near-identical strings.
KIND_ROW = "filebrowser|binary-sha256|0.0.0|preallocated|9120|bypass"
VERB_ROW = "verb|filebrowser|list_dir|read|GET|/api/resources|-|calls|query"
MINIMAL_CONTRACT = f"{KIND_ROW}\n{VERB_ROW}\n"


def _parse_contract() -> CompanionContract:
    return parse_companion_contract(CONTRACT_PATH.read_text(encoding="utf-8"))


def _parse_contract_file() -> dict[str, CompanionRegistryEntry]:
    return _parse_contract().kinds


def _typescript_registry_keys() -> set[str]:
    """Extract the TypeScript embed's key set by source read.

    Deliberately a source read rather than an execution: it is what pins the two LANGUAGES to
    each other from a test that can only run one of them, and it is the same mechanism
    ``test_brand_env.py::_typescript_alias_keys`` already uses for the environment contract.
    """
    source = TYPESCRIPT_REGISTRY.read_text(encoding="utf-8")
    marker = "export const COMPANION_REGISTRY"
    assert marker in source, f"{TYPESCRIPT_REGISTRY} no longer declares {marker}"
    body = source.split(marker, 1)[1].split("\n};", 1)[0]
    keys = set(re.findall(r"^\t([a-z][a-z0-9-]*):\s*\{", body, flags=re.MULTILINE))
    assert keys, f"no registry keys parsed out of {TYPESCRIPT_REGISTRY}"
    return keys


def _typescript_verb_keys() -> set[str]:
    """Extract the TypeScript verb table's key set by the same source read.

    Same mechanism, same reason: it is what pins the two LANGUAGES to each other from a test
    that can only execute one of them. The declaration shape (rows at one tab, a ``\\n};``
    terminator) is therefore load-bearing and is documented as such in the TypeScript module.
    """
    source = TYPESCRIPT_REGISTRY.read_text(encoding="utf-8")
    marker = "export const COMPANION_VERBS"
    assert marker in source, f"{TYPESCRIPT_REGISTRY} no longer declares {marker}"
    body = source.split(marker, 1)[1].split("\n};", 1)[0]
    keys = set(re.findall(r"^\t([a-z][a-z0-9-]*__[a-z][a-z0-9_]*):\s*\{", body, flags=re.MULTILINE))
    assert keys, f"no verb keys parsed out of {TYPESCRIPT_REGISTRY}"
    return keys


def _typescript_verb_args_in() -> dict[str, str]:
    """Extract each TypeScript verb entry's declared ``argsIn`` by the same source read (05-13).

    The key-set parity above cannot see a FIELD that drifted -- two embeds can carry identical
    keys and disagree about where every argument travels. Placement is the one field whose drift
    changes what the executor puts on the wire, so it is pinned by value rather than by presence.
    """
    source = TYPESCRIPT_REGISTRY.read_text(encoding="utf-8")
    marker = "export const COMPANION_VERBS"
    assert marker in source, f"{TYPESCRIPT_REGISTRY} no longer declares {marker}"
    body = source.split(marker, 1)[1].split("\n};", 1)[0]
    entries = re.findall(
        r"^\t([a-z][a-z0-9-]*__[a-z][a-z0-9_]*):\s*\{(.*?)^\t\},",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert entries, f"no verb entries parsed out of {TYPESCRIPT_REGISTRY}"
    placements: dict[str, str] = {}
    for name, entry_body in entries:
        match = re.search(r'argsIn:\s*"([a-z]+)"', entry_body)
        assert match is not None, f"{name} declares no argsIn in {TYPESCRIPT_REGISTRY}"
        placements[name] = match.group(1)
    return placements


def _typescript_key_pattern() -> str:
    source = TYPESCRIPT_REGISTRY.read_text(encoding="utf-8")
    match = re.search(r"export const COMPANION_KEY_PATTERN = /(.+?)/;", source)
    assert match is not None, f"{TYPESCRIPT_REGISTRY} no longer declares COMPANION_KEY_PATTERN"
    return match.group(1)


def test_parser_raises_on_a_line_without_exactly_six_columns() -> None:
    # Never skipped. A reader that skips a malformed row lets the parity assertions below pass
    # by comparing nothing to nothing.
    with pytest.raises(ValueError, match="expected 6 columns, got 5"):
        parse_companion_contract("filebrowser|binary-sha256|0.0.0|preallocated|9120")
    with pytest.raises(ValueError, match="expected 6 columns, got 7"):
        parse_companion_contract("filebrowser|binary-sha256|0.0.0|preallocated|9120|bypass|x")


def test_parser_raises_on_a_contract_with_zero_data_rows() -> None:
    with pytest.raises(ValueError, match="no data rows"):
        parse_companion_contract("# only a header\n\n")


def test_parser_raises_on_a_column_outside_its_declared_vocabulary() -> None:
    # `oci-tag` is the floating reference D-01 forbids: a tag names whatever an upstream registry
    # serves today, so it is exactly the value the artifactKind vocabulary must never admit.
    with pytest.raises(ValueError, match="unknown artifactKind"):
        parse_companion_contract("filebrowser|oci-tag|0.0.0|preallocated|9120|bypass")
    with pytest.raises(ValueError, match="unknown egress"):
        parse_companion_contract("filebrowser|binary-sha256|0.0.0|preallocated|9120|drop")
    with pytest.raises(ValueError, match="not a decimal"):
        parse_companion_contract("filebrowser|binary-sha256|0.0.0|preallocated|http|bypass")


def test_parser_raises_on_a_key_outside_the_bounding_pattern() -> None:
    with pytest.raises(ValueError, match="does not match"):
        parse_companion_contract("../etc|binary-sha256|0.0.0|preallocated|9120|bypass")


def test_python_embed_equals_the_contract_file() -> None:
    assert COMPANION_REGISTRY == _parse_contract_file()


def test_known_kinds_is_the_sorted_key_set_of_the_embed() -> None:
    assert KNOWN_COMPANION_KINDS
    assert list(KNOWN_COMPANION_KINDS) == sorted(COMPANION_REGISTRY)


def test_python_and_typescript_key_sets_are_equal_in_both_directions() -> None:
    python_keys = set(COMPANION_REGISTRY)
    typescript_keys = _typescript_registry_keys()

    assert python_keys <= typescript_keys, (
        f"missing from TypeScript: {python_keys - typescript_keys}"
    )
    assert typescript_keys <= python_keys, f"missing from Python: {typescript_keys - python_keys}"
    assert python_keys == typescript_keys


def test_the_key_pattern_is_identical_in_both_languages() -> None:
    # Plan 04-03 mirrors this exact expression in `app/schemas/runtime_observed.py` and asserts
    # equality against this module. A drift here goes red across two plans at once.
    assert COMPANION_KEY_PATTERN == _typescript_key_pattern()
    assert COMPANION_KEY_PATTERN == r"^[a-z][a-z0-9-]{0,30}$"


def test_every_key_matches_the_bounding_pattern() -> None:
    pattern = re.compile(COMPANION_KEY_PATTERN)
    for key in COMPANION_REGISTRY:
        assert pattern.fullmatch(key) is not None, key


def test_every_port_is_unique_and_clear_of_the_hermes_dashboard() -> None:
    ports = [entry.port for entry in COMPANION_REGISTRY.values()]

    assert len(set(ports)) == len(ports)
    assert HERMES_DASHBOARD_PORT not in ports


def test_every_uid_is_preallocated_or_inside_the_reserved_block() -> None:
    first, last = RESERVED_COMPANION_UID_BLOCK
    for key, entry in COMPANION_REGISTRY.items():
        if entry.uid == "preallocated":
            continue
        assert isinstance(entry.uid, int), key
        assert first <= entry.uid <= last, f"{key} uid {entry.uid} outside {first}-{last}"


def test_companion_min_cli_version_reads_the_registry_and_raises_on_an_unknown_kind() -> None:
    assert companion_min_cli_version("filebrowser") == "0.0.0"
    with pytest.raises(KeyError, match="nosuchkind"):
        companion_min_cli_version("nosuchkind")


def test_companion_kinds_at_or_below_gates_on_the_reported_cli_version() -> None:
    # The helper 04-05's emit gate consumes. `filebrowser` is ungated (0.0.0), so it is emitted
    # to every reporting CLI. `postiz` is the first GATED kind (06-03): withheld below 0.14.0,
    # including from a prerelease of 0.14.0 itself, which semver orders below the release.
    assert companion_kinds_at_or_below("0.0.0") == ("filebrowser",)
    assert companion_kinds_at_or_below("0.13.47") == ("filebrowser",)
    assert companion_kinds_at_or_below("0.14.0-rc.1") == ("filebrowser",)
    assert companion_kinds_at_or_below("0.14.0") == ("filebrowser", "postiz")
    assert companion_kinds_at_or_below("1.2.3") == ("filebrowser", "postiz")
    assert "filebrowser" in companion_kinds_at_or_below("0.0.1-alpha.1")


def test_companion_kinds_at_or_below_fails_closed_on_an_absent_observation() -> None:
    # D-15: no observed CLI version is no proof the CLI can parse a kind, so only the ungated
    # floor is emitted. "Absent means current" was rejected precisely because a stale client that
    # never reports would otherwise receive kinds it cannot parse.
    gated = companion_kinds_at_or_below(None)

    assert gated == tuple(
        kind
        for kind, entry in sorted(COMPANION_REGISTRY.items())
        if entry.min_cli_version == "0.0.0"
    )
    assert "filebrowser" in gated
    assert "postiz" not in gated


# ---------------------------------------------------------------------------
# The postiz kind (phase 06, plan 03, D-01). The first container kind, the first gated kind and
# the first numeric allocation from the reserved UID block -- each of which is one-way under D-13,
# so the row is pinned here value for value rather than only by parity.
# ---------------------------------------------------------------------------

POSTIZ_ROW = "postiz|oci-digest|0.14.0|10101|9130|bypass"


def test_the_artifact_kind_vocabulary_admits_the_oci_digest_container_seam() -> None:
    # Phase 4 D-02 left artifactKind open for a Quadlet/Podman container kind; `oci-digest` is
    # that seam, now used. The kind row parses on its own, with no verb rows beside it.
    contract = parse_companion_contract(POSTIZ_ROW)

    assert contract.kinds == {
        "postiz": CompanionRegistryEntry(
            artifact_kind="oci-digest",
            min_cli_version="0.14.0",
            uid=10101,
            port=9130,
            egress="bypass",
        )
    }


def test_parser_raises_naming_an_artifact_kind_outside_the_vocabulary_and_its_line() -> None:
    # The vocabulary is exactly {binary-sha256, oci-digest}. Anything else is a parse ERROR that
    # names both the value and the line, so a reader can find the row to fix in one look.
    bad = "postiz|oci-tag|0.14.0|10101|9130|bypass"
    with pytest.raises(ValueError, match="unknown artifactKind") as refused:
        parse_companion_contract(bad)
    assert "'oci-tag'" in str(refused.value)
    assert bad in str(refused.value)


def test_the_shipped_postiz_row_is_exactly_the_d01_row() -> None:
    expected = CompanionRegistryEntry(
        artifact_kind="oci-digest",
        min_cli_version="0.14.0",
        uid=10101,
        port=9130,
        egress="bypass",
    )

    assert COMPANION_REGISTRY.get("postiz") == expected
    assert _parse_contract_file().get("postiz") == expected
    assert companion_min_cli_version("postiz") == "0.14.0"


def test_the_shipped_kind_set_is_filebrowser_and_postiz() -> None:
    assert KNOWN_COMPANION_KINDS == ("filebrowser", "postiz")
    assert _typescript_registry_keys() == {"filebrowser", "postiz"}


def test_postiz_is_the_first_numeric_allocation_from_the_reserved_block() -> None:
    # Phase 4 deferred obligation #2: the first numeric allocation. Every other row is
    # `preallocated`, so 10101 is the ONLY number in the block in use, and it is inside it.
    first, last = RESERVED_COMPANION_UID_BLOCK
    numeric = {
        key: entry.uid for key, entry in COMPANION_REGISTRY.items() if entry.uid != "preallocated"
    }

    assert numeric == {"postiz": 10101}
    assert first <= 10101 <= last


def test_the_contract_file_carries_the_postiz_row_directly_after_filebrowser() -> None:
    rows = [
        line.strip()
        for line in CONTRACT_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#") and not line.startswith("verb|")
    ]

    assert rows == [KIND_ROW, POSTIZ_ROW]


# ---------------------------------------------------------------------------
# The verb table (phase 05, D-04/D-05/D-07/D-13).
#
# Every rule below is a RAISE, never a skip and never a default. A verb that ships unclassified,
# a duplicate that silently wins, or a tool name that shadows a native tool are all failures the
# parser is the single enforcement point for -- ``05-CONTEXT.md`` says so in as many words.
# ---------------------------------------------------------------------------


def test_parser_returns_both_tables_and_leaves_the_kind_table_byte_identical() -> None:
    contract = parse_companion_contract(MINIMAL_CONTRACT)

    assert contract.kinds == parse_companion_contract(KIND_ROW).kinds
    assert set(contract.verbs) == {"filebrowser__list_dir"}
    assert contract.verbs["filebrowser__list_dir"] == CompanionVerbEntry(
        tool_class="read",
        method="GET",
        path_template="/api/resources",
        read_probe=None,
        cap_unit="calls",
        args_in="query",
    )


def test_parser_raises_on_a_verb_line_without_exactly_nine_columns() -> None:
    # 05-13: the grammar widened from eight columns to nine when `argsIn` was appended. A row
    # written to the OLD eight-column shape is a column-count error, not a row that inherits a
    # default placement -- a default is exactly the invisible rule the column exists to remove.
    with pytest.raises(ValueError, match="expected 9 columns, got 8"):
        parse_companion_contract(
            f"{KIND_ROW}\nverb|filebrowser|list_dir|read|GET|/api/resources|-|calls"
        )
    with pytest.raises(ValueError, match="expected 9 columns, got 10"):
        parse_companion_contract(f"{KIND_ROW}\n{VERB_ROW}|extra")


def test_parser_raises_on_a_duplicate_verb_row_rather_than_letting_the_last_one_win() -> None:
    with pytest.raises(ValueError, match="filebrowser__list_dir"):
        parse_companion_contract(f"{KIND_ROW}\n{VERB_ROW}\n{VERB_ROW}")


def test_parser_raises_on_a_kind_row_whose_key_is_the_reserved_verb_marker() -> None:
    with pytest.raises(ValueError, match="reserved"):
        parse_companion_contract(f"{KIND_ROW}\nverb|binary-sha256|0.0.0|preallocated|9121|bypass")


def test_parser_raises_on_a_source_with_zero_data_rows_of_either_type() -> None:
    with pytest.raises(ValueError, match="no data rows"):
        parse_companion_contract("# only a header\n\n")


def test_parser_raises_on_a_verb_row_naming_a_kind_that_owns_no_row() -> None:
    with pytest.raises(ValueError, match="unknown companion kind"):
        parse_companion_contract(VERB_ROW)


def test_parser_raises_on_a_class_or_cap_unit_outside_its_vocabulary() -> None:
    with pytest.raises(ValueError, match="unknown tool class"):
        parse_companion_contract(
            f"{KIND_ROW}\nverb|filebrowser|list_dir|browse|GET|/api/resources|-|calls|query"
        )
    with pytest.raises(ValueError, match="unknown capUnit"):
        parse_companion_contract(
            f"{KIND_ROW}\nverb|filebrowser|list_dir|read|GET|/api/resources|-|joules|query"
        )


def test_parser_raises_on_a_mutating_row_declared_without_a_read_probe() -> None:
    # D-13. This is what makes TOOL-05's "any mutating tool has a preview path" structurally
    # true rather than aspirational: a `mutate` or `destroy` row without a probe cannot ship.
    with pytest.raises(ValueError, match="read probe"):
        parse_companion_contract(
            f"{KIND_ROW}\nverb|filebrowser|move_path|mutate|PATCH|/api/resources|-|records|query"
        )
    with pytest.raises(ValueError, match="read probe"):
        parse_companion_contract(
            f"{KIND_ROW}\nverb|filebrowser|delete_path|destroy|DELETE|/api/resources|-|records|query"
        )


def test_parser_raises_on_a_read_probe_that_names_no_verb_of_the_same_kind() -> None:
    with pytest.raises(ValueError, match="read probe"):
        parse_companion_contract(
            f"{KIND_ROW}\n{VERB_ROW}\n"
            "verb|filebrowser|move_path|mutate|PATCH|/api/resources|nosuchverb|records|query"
        )


def test_parser_raises_on_a_tool_name_colliding_with_a_reserved_native_tool() -> None:
    with pytest.raises(ValueError, match="filebrowser__list_dir"):
        parse_companion_contract(
            MINIMAL_CONTRACT, reserved_tool_names=frozenset({"filebrowser__list_dir"})
        )


def test_the_default_reserved_name_set_is_the_live_native_tool_registry() -> None:
    # The collision rule is only worth anything if its default set is the REAL one. Compared
    # against the live registry rather than a copied list, so a native tool added later is
    # covered without touching this suite.
    from app.routes.mcp_bridge import _NATIVE_TOOL_REGISTRY

    assert default_reserved_tool_names() == frozenset(_NATIVE_TOOL_REGISTRY)


def test_parsing_the_same_source_twice_returns_equal_tables() -> None:
    # The parser holds no state between calls; two parses of one source compare equal.
    assert parse_companion_contract(MINIMAL_CONTRACT) == parse_companion_contract(MINIMAL_CONTRACT)


def test_derive_annotations_maps_every_class_mechanically() -> None:
    # D-07: advisory client-side UX derived by a pure function so the two vocabularies cannot
    # drift. Never the gate -- the server-side class check is.
    assert derive_annotations("read") == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    }
    assert derive_annotations("create") == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
    }
    assert derive_annotations("mutate") == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
    }
    assert derive_annotations("destroy") == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
    }
    with pytest.raises(ValueError, match="unknown tool class"):
        derive_annotations("browse")


def test_companion_verb_reads_the_embed_and_raises_on_an_unknown_name() -> None:
    entry = companion_verb("filebrowser__list_dir")

    assert entry.tool_class == "read"
    assert entry.method == "GET"
    assert entry.path_template == "/api/resources"
    with pytest.raises(KeyError, match="filebrowser__nosuchverb"):
        companion_verb("filebrowser__nosuchverb")


def test_python_verb_embed_equals_the_contract_file() -> None:
    assert COMPANION_VERBS == _parse_contract().verbs


def test_every_verb_name_is_kind_double_underscore_verb_over_a_known_kind() -> None:
    for name, entry in COMPANION_VERBS.items():
        kind, separator, verb = name.partition("__")
        assert separator == "__", name
        assert kind in COMPANION_REGISTRY, name
        assert verb, name
        assert entry.tool_class in {"read", "create", "mutate", "destroy"}, name


def test_verb_rows_are_sorted_by_kind_then_verb_in_the_contract_file() -> None:
    columns = [
        line.split("|")
        for line in CONTRACT_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("verb|")
    ]
    keys = [f"{row[1]}__{row[2]}" for row in columns]

    assert keys
    assert keys == sorted(keys)


def test_python_and_typescript_verb_key_sets_are_equal_in_both_directions() -> None:
    python_keys = set(COMPANION_VERBS)
    typescript_keys = _typescript_verb_keys()

    assert python_keys <= typescript_keys, (
        f"missing from TypeScript: {python_keys - typescript_keys}"
    )
    assert typescript_keys <= python_keys, f"missing from Python: {typescript_keys - python_keys}"
    assert python_keys == typescript_keys


# ---------------------------------------------------------------------------
# 05-02: one assertion per parser rule, each pinned to the TEXT of its message.
#
# A rule is only enforceable if its failure NAMES what is wrong. Every test below therefore
# matches on the offending value rather than on a generic prefix: a rule that silently stopped
# firing, or one whose message stopped naming the offender, fails here rather than passing
# vacuously (T-5-14, inherited from Phase 4's T-4-19).
# ---------------------------------------------------------------------------

# Well-formed rows used to build the negative cases. Written as literals rather than rendered
# from `COMPANION_VERBS`, so a wrong value in the embed cannot make a negative case pass.
CREATE_DIR_ROW = "verb|filebrowser|create_dir|create|POST|/api/resources|list_dir|calls|query"
WRITE_FILE_ROW = "verb|filebrowser|write_file|create|POST|/api/resources|list_dir|bytes|query"

# The twelve curated names: six from the Phase 5 `COVERAGE.md`'s INTEGRATE capabilities
# (`filebrowser`) and six from the Phase 6 one's (`postiz`, 06-06). Literals, because a set derived
# from `COMPANION_VERBS` would assert `x == x`.
CURATED_TOOL_NAMES = frozenset(
    {
        "filebrowser__create_dir",
        "filebrowser__delete_path",
        "filebrowser__list_dir",
        "filebrowser__move_path",
        "filebrowser__read_file",
        "filebrowser__write_file",
        "postiz__create_post",
        "postiz__is_connected",
        "postiz__list_groups",
        "postiz__list_integrations",
        "postiz__list_notifications",
        "postiz__list_posts",
    }
)


def _contract_verb_names_in_file_order() -> list[str]:
    """The verb names of ``config/companion-registry.txt``, in the order the file lists them.

    A list rather than a set on purpose: the sort-order rule below is about SEQUENCE, and a set
    would silently discard the property under test.
    """
    names = [
        f"{line.split('|')[1]}__{line.split('|')[2]}"
        for line in CONTRACT_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("verb|")
    ]
    assert names, f"no verb rows parsed out of {CONTRACT_PATH}"
    return names


def test_parser_raises_naming_the_offending_class_and_the_line_it_came_from() -> None:
    # "naming the offending class and line": the class alone is not actionable in a file whose
    # rows all share one shape, and the line is what a reader greps for.
    offending = "verb|filebrowser|list_dir|browse|GET|/api/resources|-|calls|query"

    with pytest.raises(ValueError) as raised:
        parse_companion_contract(f"{KIND_ROW}\n{offending}")

    message = str(raised.value)
    assert "unknown tool class" in message
    assert "'browse'" in message
    assert offending in message, f"the error does not name the offending line: {message}"


def test_parser_raises_naming_the_offending_cap_unit() -> None:
    with pytest.raises(ValueError, match=r"unknown capUnit.*'joules'"):
        parse_companion_contract(
            f"{KIND_ROW}\nverb|filebrowser|list_dir|read|GET|/api/resources|-|joules|query"
        )


def test_parser_raises_on_a_mutating_row_whose_read_probe_column_is_the_empty_marker() -> None:
    # D-13 / TOOL-05. The message names the VERB, because "a mutating verb needs a read probe"
    # is a statement about one row and the fix is on that row.
    expected = r"filebrowser__delete_path is class 'destroy'.*read probe"

    with pytest.raises(ValueError, match=expected):
        parse_companion_contract(
            f"{KIND_ROW}\nverb|filebrowser|delete_path|destroy|DELETE|/api/resources|-|records|query"
        )


def test_parser_raises_on_a_read_probe_that_names_a_verb_the_kind_does_not_declare() -> None:
    # "naming both": the verb whose probe dangles, and the probe that does not resolve.
    with pytest.raises(ValueError, match=r"filebrowser__move_path.*'nosuchverb'"):
        parse_companion_contract(
            f"{KIND_ROW}\n{VERB_ROW}\n"
            "verb|filebrowser|move_path|mutate|PATCH|/api/resources|nosuchverb|records|query"
        )


def test_parser_raises_on_a_read_probe_whose_own_class_is_not_read() -> None:
    """A probe that itself writes is not a preview path -- it is a second mutation (T-5-13).

    This is the rule that keeps a dry run structurally incapable of mutating. D-13 requires a
    probe; without this rule a row could satisfy D-13 by naming another `create` verb.
    """
    with pytest.raises(ValueError, match=r"read probe.*'create'"):
        parse_companion_contract(
            f"{KIND_ROW}\n{VERB_ROW}\n"
            "verb|filebrowser|move_path|mutate|PATCH|/api/resources|write_file|records|query\n"
            f"{WRITE_FILE_ROW}"
        )


def test_parser_raises_on_verb_rows_out_of_ascending_kind_then_verb_order() -> None:
    """Two rows that compare equal on kind must still have a specified position.

    Without this the file's row order is unconstrained, and "the exposed order is the registry
    order" -- which `test_companion_tools_list.py` asserts as a SEQUENCE -- would be a property
    of whatever order somebody happened to paste rows in.
    """
    with pytest.raises(ValueError, match=r"filebrowser__create_dir.*filebrowser__list_dir"):
        parse_companion_contract(f"{KIND_ROW}\n{VERB_ROW}\n{CREATE_DIR_ROW}")


def test_parser_raises_on_a_verb_name_outside_its_pattern_or_a_kind_that_owns_no_row() -> None:
    with pytest.raises(ValueError, match=r"'List-Dir' does not match"):
        parse_companion_contract(
            f"{KIND_ROW}\nverb|filebrowser|List-Dir|read|GET|/api/resources|-|calls|query"
        )
    with pytest.raises(ValueError, match=r"unknown companion kind.*'nosuchkind'"):
        parse_companion_contract("verb|nosuchkind|list_dir|read|GET|/api/resources|-|calls|query")


@pytest.mark.parametrize(
    ("tool_class", "expected"),
    [
        ("read", {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}),
        ("create", {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}),
        ("mutate", {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}),
        ("destroy", {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}),
    ],
    ids=["read", "create", "mutate", "destroy"],
)
def test_derive_annotations_is_exact_for_each_class(
    tool_class: str, expected: dict[str, bool]
) -> None:
    # Exact dict equality, one case per class: a superset assertion would let a fifth advisory
    # hint appear without anybody deciding to add it.
    assert derive_annotations(tool_class) == expected


def test_the_contract_file_lists_every_verb_row_in_ascending_kind_then_verb_order() -> None:
    names = _contract_verb_names_in_file_order()

    assert names == sorted(names), f"{CONTRACT_PATH} lists its verb rows out of order: {names}"


def test_python_verb_keys_equal_the_contract_file_verb_rows_in_both_directions() -> None:
    python_keys = set(COMPANION_VERBS)
    contract_keys = set(_contract_verb_names_in_file_order())

    assert python_keys <= contract_keys, (
        f"missing from {CONTRACT_PATH}: {sorted(python_keys - contract_keys)}"
    )
    assert contract_keys <= python_keys, (
        f"missing from Python: {sorted(contract_keys - python_keys)}"
    )
    assert python_keys == contract_keys


def test_typescript_verb_keys_equal_the_contract_file_verb_rows_in_both_directions() -> None:
    typescript_keys = _typescript_verb_keys()
    contract_keys = set(_contract_verb_names_in_file_order())

    assert typescript_keys <= contract_keys, (
        f"missing from {CONTRACT_PATH}: {sorted(typescript_keys - contract_keys)}"
    )
    assert contract_keys <= typescript_keys, (
        f"missing from TypeScript: {sorted(contract_keys - typescript_keys)}"
    )
    assert typescript_keys == contract_keys


def test_the_embed_carries_exactly_the_twelve_curated_verbs() -> None:
    assert set(COMPANION_VERBS) == set(CURATED_TOOL_NAMES)


def test_every_mutating_verb_names_a_read_class_probe_of_its_own_kind() -> None:
    # The embed-level statement of the parser rule above: TOOL-05 holds for the table that
    # actually ships, not merely for a synthetic source the parser was handed in a test.
    for name, entry in COMPANION_VERBS.items():
        kind, _, _verb = name.partition("__")
        if entry.tool_class in {"mutate", "destroy"}:
            assert entry.read_probe is not None, name
        if entry.read_probe is None:
            continue
        probe = COMPANION_VERBS[f"{kind}__{entry.read_probe}"]
        assert probe.tool_class == "read", f"{name} previews with a {probe.tool_class!r} verb"


# ---------------------------------------------------------------------------
# 05-13 (WR-06b): the ninth column, `argsIn`. Parameter placement was previously derived from
# the HTTP method inside `packages/cli/src/runtime/companion-executor.ts`, which meant three
# verbs sent their target path somewhere their sibling verbs did not and no reader could see the
# decision in a diff. It is now DECLARED per row and enforced by both language readers.
# ---------------------------------------------------------------------------

# A well-formed row declaring the OTHER placement. `body` must stay expressible even though all
# six shipped rows declare `query`, or the column is decoration rather than a vocabulary.
BODY_PLACEMENT_ROW = "verb|filebrowser|list_dir|read|GET|/api/resources|-|calls|body"


def test_parser_raises_naming_an_args_in_value_outside_the_closed_vocabulary() -> None:
    # Names BOTH the offending value and the two permitted ones: a message that says only
    # "invalid" makes the reader go find the vocabulary, and the vocabulary is the whole rule.
    with pytest.raises(ValueError, match=r"argsIn.*'sidecar'.*'body'.*'query'"):
        parse_companion_contract(
            f"{KIND_ROW}\nverb|filebrowser|list_dir|read|GET|/api/resources|-|calls|sidecar"
        )


def test_parser_raises_on_an_empty_args_in_column_rather_than_defaulting() -> None:
    # An absent placement is NOT a default. A default would reintroduce exactly the invisible
    # method-derived rule this column was added to remove.
    with pytest.raises(ValueError, match=r"argsIn.*''"):
        parse_companion_contract(
            f"{KIND_ROW}\nverb|filebrowser|list_dir|read|GET|/api/resources|-|calls|"
        )


def test_parser_accepts_a_row_declaring_body_placement() -> None:
    contract = parse_companion_contract(f"{KIND_ROW}\n{BODY_PLACEMENT_ROW}")

    assert contract.verbs["filebrowser__list_dir"].args_in == "body"


def test_every_shipped_verb_declares_a_placement_inside_the_vocabulary() -> None:
    for name, entry in COMPANION_VERBS.items():
        assert entry.args_in in {"query", "body"}, f"{name} declares {entry.args_in!r}"


def test_python_and_typescript_agree_on_every_verbs_declared_placement() -> None:
    """Both embeds must agree on WHERE each verb's arguments travel, not merely on key sets.

    Mutation-checked by hand (05-13): flipping `filebrowser__move_path` to ``body`` in
    ``packages/cli/src/runtime/companion-registry.ts`` alone reds this assertion, and flipping it
    in ``app/core/companion_registry.py`` alone reds it from the other side. Restored immediately.
    """
    python_placements = {name: entry.args_in for name, entry in COMPANION_VERBS.items()}
    typescript_placements = _typescript_verb_args_in()

    assert python_placements == typescript_placements


def test_python_verb_placements_equal_the_contract_file() -> None:
    contract_placements = {name: entry.args_in for name, entry in _parse_contract().verbs.items()}
    python_placements = {name: entry.args_in for name, entry in COMPANION_VERBS.items()}

    assert python_placements == contract_placements


# ---------------------------------------------------------------------------
# 06-06: the curated `postiz` toolset (TOOL-08 for the second kind). Six rows, one per INTEGRATE
# capability of `.planning/phases/06-first-companion-end-to-end/COVERAGE.md`, decided against the
# Postiz v2.24.0 public controller. Pinned here as LITERAL LINES, not only by parity: parity would
# pass on a row that was wrong identically in all three representations.
# ---------------------------------------------------------------------------

FILEBROWSER_VERB_ROWS = (
    "verb|filebrowser|create_dir|create|POST|/api/resources|list_dir|calls|query",
    "verb|filebrowser|delete_path|destroy|DELETE|/api/resources|list_dir|records|query",
    "verb|filebrowser|list_dir|read|GET|/api/resources|-|calls|query",
    "verb|filebrowser|move_path|mutate|PATCH|/api/resources|list_dir|records|query",
    "verb|filebrowser|read_file|read|GET|/api/raw|-|bytes|query",
    "verb|filebrowser|write_file|create|POST|/api/resources|list_dir|bytes|query",
)

POSTIZ_VERB_ROWS = (
    "verb|postiz|create_post|create|POST|/api/public/v1/posts|list_posts|records|body",
    "verb|postiz|is_connected|read|GET|/api/public/v1/is-connected|-|calls|query",
    "verb|postiz|list_groups|read|GET|/api/public/v1/groups|-|calls|query",
    "verb|postiz|list_integrations|read|GET|/api/public/v1/integrations|-|calls|query",
    "verb|postiz|list_notifications|read|GET|/api/public/v1/notifications|-|calls|query",
    "verb|postiz|list_posts|read|GET|/api/public/v1/posts|-|calls|query",
)


def _contract_verb_lines() -> list[str]:
    return [
        line.strip()
        for line in CONTRACT_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("verb|")
    ]


def test_the_contract_file_carries_the_six_postiz_rows_after_the_filebrowser_rows() -> None:
    # A SEQUENCE, not a set: the postiz rows sit after every filebrowser row because the file is
    # sorted by kind then verb, and the exposed `tools/list` order is that order.
    assert _contract_verb_lines() == [*FILEBROWSER_VERB_ROWS, *POSTIZ_VERB_ROWS]


def test_postiz_create_post_is_create_class_and_previews_with_list_posts() -> None:
    entry = companion_verb("postiz__create_post")

    assert entry == CompanionVerbEntry(
        tool_class="create",
        method="POST",
        path_template="/api/public/v1/posts",
        read_probe="list_posts",
        cap_unit="records",
        args_in="body",
    )
    # The probe is a read-class verb of the SAME kind, so a dry run cannot itself post.
    assert companion_verb("postiz__list_posts").tool_class == "read"


def test_the_five_postiz_reads_are_query_placed_call_capped_and_probe_free() -> None:
    reads = {name: entry for name, entry in COMPANION_VERBS.items() if name.startswith("postiz__")}
    reads.pop("postiz__create_post", None)

    assert set(reads) == {
        "postiz__is_connected",
        "postiz__list_groups",
        "postiz__list_integrations",
        "postiz__list_notifications",
        "postiz__list_posts",
    }
    for name, entry in reads.items():
        assert entry.tool_class == "read", name
        assert entry.method == "GET", name
        assert entry.read_probe is None, name
        assert entry.cap_unit == "calls", name
        assert entry.args_in == "query", name
        # No path parameter: the executor issues pathTemplate verbatim (the COVERAGE.md OPT-OUTs).
        assert ":" not in entry.path_template, name
        assert entry.path_template.startswith("/api/public/v1/"), name


def test_no_postiz_verb_is_destroy_class_this_phase() -> None:
    # T-06-06-03: no Postiz tool can delete a remote post or channel. Every delete route is an
    # OPT-OUT with a reason in COVERAGE.md and in the registry's EXCLUSIONS block.
    postiz = {name: entry for name, entry in COMPANION_VERBS.items() if name.startswith("postiz__")}

    assert postiz
    assert all(entry.tool_class != "destroy" for entry in postiz.values())
    assert all(entry.method != "DELETE" for entry in postiz.values())


def test_python_and_typescript_verb_tables_each_carry_twelve_keys() -> None:
    typescript_keys = _typescript_verb_keys()

    assert len(COMPANION_VERBS) == 12
    assert len(typescript_keys) == 12
    assert set(COMPANION_VERBS) == typescript_keys


def test_the_contract_file_records_a_postiz_exclusions_block() -> None:
    # Curation must never become undisclosed capability removal (T-06-06-04): every OPT-OUT of
    # the Phase 6 matrix is restated, with its reason, beside the rows it shapes.
    source = CONTRACT_PATH.read_text(encoding="utf-8")

    assert "POSTIZ VERB TABLE EXCLUSIONS" in source
    for capability in (
        "integrations.oauth_url",
        "integrations.trigger",
        "integrations.delete",
        "uploads.file",
        "uploads.from_url",
        "users.list",
        "debug.post_timeline",
        "debug.account",
        "debug.channels",
        "debug.activity",
        "video.generate",
        "video.function",
        "clipping.start",
        "clipping.list",
        "clipping.get",
    ):
        assert f"`{capability}`" in source, capability


def _class_column_description() -> str:
    source = CONTRACT_PATH.read_text(encoding="utf-8")
    start = source.index("#   class ")
    end = source.index("#   method ", start)
    return source[start:end]


def test_the_class_column_description_names_the_d09_scope_mapping() -> None:
    # D-09 collapsed every class onto `companions:invoke`; `tools/list` needs `companions:read`.
    # The destroy gate is the tenant's enablement row, not a scope. A header that still names the
    # retired per-class scopes documents a gate that does not exist.
    description = _class_column_description()

    assert "companions:invoke" in description
    assert "companions:read" in description
    assert "companions:write" not in description
    assert "companions:destroy" not in description


def test_the_typescript_registry_names_no_retired_scope() -> None:
    source = TYPESCRIPT_REGISTRY.read_text(encoding="utf-8")

    assert "companions:write" not in source
    assert "companions:destroy" not in source
    assert "companions:invoke" in source
