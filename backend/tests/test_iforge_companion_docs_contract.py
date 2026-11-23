"""Fork-owned doc-contract assertions over ``docs/managed-runtime.md``.

Phase 06 pinned its managed-runtime documentation claims in
``test_managed_runtime_docs_contract.py``. Upstream deleted that file at sync step 4 (#1431, which
retired its own two floating-dist-tag assertions as redundant), so every claim the fork added
lives here instead, under a fork-owned name an upstream merge cannot delete by accident. Upstream's
two original tests are deliberately NOT carried: upstream removed them on purpose.

Two kinds of gate live here.

The first is a one-directional list of required and forbidden phrases over the document's prose:
the platform roots and systemd names after the iforge rename, the retired workspace ACL, the
schema-marker hold without a receipt (06-51) and the update report identity rule (06-52). Each
claim describes behaviour the code still has, so a failing claim means the document must be
CORRECTED, not the assertion deleted.

The second, added for COMP-04 / D-09, is a **bidirectional parity gate** over the companion
registry. A one-directional substring list cannot detect a kind that exists in the registry and
is missing from the document -- the drift that matters most, because the document is what an
operator and an auditor read. So the registry table is parsed structurally and asserted equal to
both the Python embed and ``config/companion-registry.txt``, in both directions and column by
column.

The reader anchors on the exact ``### Companion Registry`` heading and **never on the word
"companion"**. This document uses "companion" in two unrelated senses: ``## Cloud API Runtime
Observation Companion`` is the v2 observation *subsystem*, while ``### Incus Files Companion`` is
a companion *kind*. A reader that matched the word would anchor on the subsystem and pass while
proving nothing. The Python schema shares the collision through
``RuntimeObservationEventV2.validate_companion_identity``.

Every reader failure RAISES, naming the path and the offending row. Returning an empty result
would let the parity assertions pass because they compared nothing to nothing -- a green test
that reads as evidence while proving nothing, which is strictly worse than having no test. That
is the rationale ``apps/web/src/lib/ui-spec-contract.ts`` states for the same shape, and the
rationale ``parse_companion_contract`` states for the contract file.

``REPO_ROOT`` and ``normalized_text`` are imported from ``tests.doc_contract`` and
``parse_companion_contract`` from ``app.core.companion_registry``. None of the three is
re-implemented here: byte-identical private copies of a shared primitive are the exact failure
mode these suites exist to catch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.companion_registry import (
    COMPANION_REGISTRY,
    COMPANION_VERBS,
    KNOWN_COMPANION_KINDS,
    CompanionRegistryEntry,
    CompanionVerbEntry,
    parse_companion_contract,
)
from tests.doc_contract import REPO_ROOT as _REPO_ROOT
from tests.doc_contract import normalized_text as _normalized_text

_MANAGED_RUNTIME_PATH = _REPO_ROOT / "docs" / "managed-runtime.md"
_COMPANION_CONTRACT_PATH = _REPO_ROOT / "config" / "companion-registry.txt"


# The anchor. Matched as a whole stripped line, so no substring of a longer heading can stand in
# for it and the observation-subsystem homonym can never be mistaken for it.
_REGISTRY_HEADING = "### Companion Registry"

# The table's columns, in order. The FIRST column is the registry key; its position is part of
# the contract because the reader takes cell 0 as the key.
_REGISTRY_COLUMNS = ("key", "artifact kind", "minimum cli version", "uid", "port", "egress")

# The verb table's anchor and columns (05-02). Same design as the kind table above, and for the
# same reason: a one-directional substring list cannot detect a verb that ships and is absent from
# the document, which is the drift that matters because the document is what an operator reads.
_VERB_HEADING = "### Companion Verb Table"
_VERB_COLUMNS = (
    "tool name",
    "class",
    "method",
    "path template",
    "read probe",
    "cap unit",
    "args in",
)

# The document's spelling of the contract file's `-` marker for "this verb has no read probe".
_NO_PROBE_CELLS = frozenset({"-", "—", ""})


def _table_under_heading(
    path: Path, heading: str, columns: tuple[str, ...], *, hint: str = ""
) -> list[list[str]]:
    """Every markdown table row under ``heading``, header and delimiter included.

    ONE reader for both companion tables. Parsing stops at the next heading, so a table further
    down the document cannot be absorbed into this one. Raises on an absent heading, an absent or
    data-less table, and a row whose cell count is not ``columns``' -- returning an empty result
    would let set equality pass by comparing nothing to nothing.
    """
    try:
        document = path.read_text(encoding="utf-8")
    except OSError as cause:  # pragma: no cover - an unreadable doc is an environment fault
        raise AssertionError(
            f"companion-registry contract: could not read {path}. "
            "A registry parity assertion cannot pass without it."
        ) from cause

    lines = document.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    if start is None:
        raise AssertionError(
            f"companion-registry contract: no '{heading}' heading in {path}.{hint} "
            "Add the section rather than deleting the assertion."
        )

    rows: list[list[str]] = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            break
        if not stripped.startswith("|"):
            continue
        rows.append([cell.strip().strip("`").strip() for cell in stripped.split("|")[1:-1]])

    if len(rows) < 2:
        raise AssertionError(
            f"companion-registry contract: the '{heading}' section in {path} has no "
            "markdown table. An empty result would let set equality pass by comparing nothing "
            "to nothing."
        )
    for row in rows:
        if len(row) != len(columns):
            raise AssertionError(
                f"companion-registry contract: malformed row in {path}: expected "
                f"{len(columns)} cells, got {len(row)}: {row!r}"
            )
    delimiter = rows[1]
    if not all(cell and set(cell) <= {"-", ":"} for cell in delimiter):
        raise AssertionError(
            f"companion-registry contract: the row after the header in {path} is not a markdown "
            f"delimiter row: {delimiter!r}"
        )
    if len(rows) < 3:
        raise AssertionError(
            f"companion-registry contract: the '{heading}' table in {path} has zero "
            "data rows. The registry cannot be empty, and returning an empty set here would let "
            "a parity assertion pass by comparing nothing to nothing."
        )
    return rows


def _companion_registry_table(path: Path = _MANAGED_RUNTIME_PATH) -> list[list[str]]:
    """The ``### Companion Registry`` table: the KIND rows."""
    return _table_under_heading(
        path,
        _REGISTRY_HEADING,
        _REGISTRY_COLUMNS,
        hint=(
            " The parity assertion anchors on that exact heading and never on the word "
            "'companion' -- '## Cloud API Runtime Observation Companion' in this same document "
            "is an unrelated subsystem."
        ),
    )


def _companion_verb_table(path: Path = _MANAGED_RUNTIME_PATH) -> list[list[str]]:
    """The ``### Companion Verb Table`` table: the VERB rows (05-02)."""
    return _table_under_heading(path, _VERB_HEADING, _VERB_COLUMNS)


def _documented_verb_rows(path: Path = _MANAGED_RUNTIME_PATH) -> dict[str, tuple[str, ...]]:
    """Documented tool names mapped to their remaining columns, in table order."""
    _header, _delimiter, *data = _companion_verb_table(path)
    rows: dict[str, tuple[str, ...]] = {}
    for cells in data:
        name, *rest = cells
        if not name:
            raise AssertionError(
                f"companion-verb contract: a row in {path} has an empty tool-name cell: {cells!r}"
            )
        if name in rows:
            raise AssertionError(
                f"companion-verb contract: duplicate tool name in {path}: {name!r}"
            )
        rows[name] = tuple(rest)
    return rows


def _expected_verb_columns(entry: CompanionVerbEntry) -> tuple[str, ...]:
    return (
        entry.tool_class,
        entry.method,
        entry.path_template,
        entry.read_probe or "-",
        entry.cap_unit,
        entry.args_in,
    )


def _contract_file_verb_rows() -> dict[str, CompanionVerbEntry]:
    return parse_companion_contract(_COMPANION_CONTRACT_PATH.read_text(encoding="utf-8")).verbs


def _companion_registry_rows(path: Path = _MANAGED_RUNTIME_PATH) -> dict[str, tuple[str, ...]]:
    """Documented registry keys mapped to their remaining columns, in table order."""
    _header, _delimiter, *data = _companion_registry_table(path)
    rows: dict[str, tuple[str, ...]] = {}
    for cells in data:
        key, *rest = cells
        if not key:
            raise AssertionError(
                f"companion-registry contract: a row in {path} has an empty key cell: {cells!r}"
            )
        if key in rows:
            raise AssertionError(f"companion-registry contract: duplicate key in {path}: {key!r}")
        rows[key] = tuple(rest)
    if not rows:  # pragma: no cover - _companion_registry_table already raises on this
        raise AssertionError(f"companion-registry contract: no documented kinds in {path}.")
    return rows


def _documented_companion_kinds(path: Path = _MANAGED_RUNTIME_PATH) -> frozenset[str]:
    """The set of companion kinds ``docs/managed-runtime.md`` documents."""
    return frozenset(_companion_registry_rows(path))


def _expected_columns(entry: CompanionRegistryEntry) -> tuple[str, ...]:
    return (
        entry.artifact_kind,
        entry.min_cli_version,
        str(entry.uid),
        str(entry.port),
        entry.egress,
    )


def _contract_file_rows() -> dict[str, CompanionRegistryEntry]:
    # `.kinds`: phase 05 gave the contract file a second row type, so the parser returns both
    # tables. The doc table this suite guards is the KIND table; the verb table's doc row is
    # 05-02's work and gets its own assertion there.
    return parse_companion_contract(_COMPANION_CONTRACT_PATH.read_text(encoding="utf-8")).kinds


# The pre-rename platform roots, as whole path segments. `packages/cli/src/runtime/paths.ts`
# moved every platform root to the `iforge` spelling, and since 06.1-04 (user directive
# 2026-09-30, no Clawdi-name aliases) keeps no reader for these at all. A document that
# still names them as current describes a host layout the emitters cannot produce, which is the
# same class of decay the registry gate above exists to stop, in the very document that gate
# guards. 04-01 found this drift and deferred it as out of its scope; 04-07 owns this file.
#
# Matched with a negative lookahead on `[-\w]` rather than as bare substrings, so a longer path
# that merely starts with one of these is judged by its own entry rather than this one.
_PRE_RENAME_PLATFORM_ROOTS = (
    "/etc/clawdi",
    "/var/lib/clawdi",
    "/var/cache/clawdi",
    "/run/clawdi",
)

# Claims that must be present, not merely un-drifted. A forbidden-substring list alone cannot
# tell a corrected document from one that deleted the sentence.
_CURRENT_PLATFORM_ROOT_CLAIMS = (
    "systemd owns `/etc/iforge`, `/var/lib/iforge`, `/var/cache/iforge`, and `/run/iforge`",
    "`/run/iforge` is root-owned `0711`",
    "a resolved home of exactly `/home/iforge`",
    # `WorkingDirectory=/home/iforge` was retired at sync step 2 (06.1-05): upstream #1107 took
    # `WorkingDirectory` out of the hosted drop-in, which the official base unit now owns alone,
    # so the document's drop-in example correctly no longer states it.
    # The npm-package segments deliberately did NOT move: the managed CLI is still the `clawdi`
    # npm package, so its path segment stays `clawdi` UNDER the renamed platform root. This is
    # asserted so a later pass that "finishes the rename" cannot quietly break the resolver.
    "/var/lib/iforge/maintained/clawdi/bin/clawdi",
    "/var/cache/iforge/npm",
)

# Sentences that asserted a pre-rename systemd identity AS CURRENT. Forbidden as exact claims,
# NOT as bare name substrings, and the distinction is the whole design here.
#
# `RUNTIME_SYSTEMD_OWNED_NAME_PREFIX` is `iforge-`, the three system units are
# `iforge-runtime-watch` / `iforge-daemon` / `iforge-runtime-sidecar`, the Hermes compatibility
# unit is `iforge-hermes-dashboard` and the drop-in this product WRITES is
# `10-iforge-hosted.conf` (packages/cli/src/runtime/systemd-user.ts). Since 06.1-04 (user
# directive 2026-09-30, no Clawdi-name aliases) no `clawdi-` spelling of those is read, collected
# or drained: `test_docs_identity_contract.py` asserts the document no longer names the three
# pre-rename unit filenames. What is forbidden here is the CLAIM, phrase by phrase, including the
# retired claim that convergence collects a pre-rename unit.
_PRE_RENAME_SYSTEMD_CLAIMS = (
    "systemd/system/clawdi-*.service",
    "iforge support processes use `clawdi-*` service names",
    "the unit uses a `clawdi-*` name and is documented as compatibility",
    "deliberately keep the pre-rename prefix",
    "support units keep `clawdi-*` names",
    "<unit>.service.d/10-clawdi-hosted.conf",
    "compatibility unit, if required, must use a `clawdi-*` name",
    "*.service.d/10-clawdi-hosted.conf",
    "`clawdi-runtime-watch` and `clawdi-daemon` consume that file",
    "openclaw-gateway.service.d/10-clawdi-hosted.conf",
    "`clawdi-hermes-dashboard` compatibility unit",
    "only as an explicit `clawdi-*`",
    "`clawdi-*` support units",
    "convergence collects a pre-rename unit",
    "the old spelling drains off a host by itself",
)

_CURRENT_SYSTEMD_NAME_CLAIMS = (
    "`iforge-*` service names",
    "`iforge-runtime-watch.service`",
    "`iforge-daemon.service`",
    "`iforge-runtime-sidecar.service`",
    "`iforge-hermes-dashboard`",
    "10-iforge-hosted.conf",
)

# One entry per documented registry rule. Deleting a rule's prose turns this suite red, so the
# document cannot quietly lose a guarantee the code still makes. Phrases are lowercase,
# single-spaced and written with ASCII apostrophes because `normalized_text` casefolds, collapses
# whitespace and folds typographic apostrophes before matching.
_REGISTRY_RULE_PHRASES: dict[str, tuple[str, ...]] = {
    "graceful-degradation": (
        "an unrecognised companion key whose value carries a kind identity and a digest-pinned "
        "artifact is admitted and skipped",
        "a malformed or mis-keyed companion is rejected and fails the whole manifest parse",
        "reported in the runtime's observation as unsupported, carrying only the key and the "
        "cli version",
    ),
    "downgrade-behaviour": (
        "a running companion whose kind the currently-installed cli does not recognise is left "
        "running and reported unmanaged, not withdrawn",
        "a downgrade is not a deprovision",
        "a recognised kind that becomes ineligible is still withdrawn",
    ),
    "consumer-first-emission": (
        "each kind declares the minimum cli version that can parse it",
        "the control plane refuses to emit that kind to a runtime reporting an older version",
        "a runtime that has not reported a version receives only the ungated legacy set",
        # 06-03: the gate stops being theoretical. postiz is withheld from every deployed CLI.
        "postiz is the first gated kind",
    ),
    "digest-pinning": (
        "every kind's artifact is pinned by a content digest",
        # 06-03: the container seam Phase 4 D-02 left open, now used. A tag is a floating ref.
        "a container kind is located by an oci content digest, never by a tag",
        "fails closed rather than fetching a floating reference",
        "the pin's field name is per-kind",
    ),
    "identity-and-never-reclaim": (
        "each kind runs under its own non-login system uid",
        "a retired kind's uid is permanently burned and never reclaimed",
        "reserved uid block 10100-10199",
        "must be confirmed against the provisioning image before the first numeric allocation",
        # 06-03, CK-B: the first numeric allocation, and the honest state of its confirmation.
        "the first numeric allocation is postiz at uid 10101",
        "explicitly deferred that confirmation to host uat",
        "pre-allocated and exempt from the reserved block",
        # The account NAME is a named exemption too, on the same install-history grounds. It does
        # NOT derive from the registry key, and the document must not imply that it does.
        "the filebrowser account, group and state directory are named iforge-files rather than a "
        "name derived from its registry key",
    ),
    "sandbox-baseline": (
        "companion units carry one shared sandbox directive set",
        "a path allow-list defaulting to empty, not an opt-out",
        "an empty allow-list yields a read-only empty home",
        "bindpaths= can re-open a path it hides",
    ),
    "credentials-two-properties": (
        # Property one: per-UID isolation, already OS-enforced, and what COMP-05 names.
        "read-only, backed by non-swappable memory, and accessible only to the unit's own uid "
        "and root",
        # Property two: at-rest encryption, an OPEN Phase 6 obligation. A reader must not be able
        # to conclude from this document that credentials are already encrypted at rest.
        "is a separate property and is not yet enabled",
        "tracked as phase 6 work",
        "re-open the tpm device node",
        "accessible to unprivileged processes over ipc",
    ),
    "egress-participation": (
        "a companion uid is outside the transparent-egress redirect",
        "accepted residual recorded here rather than an unstated gap",
        "container traffic leaves through podman's network nat",
    ),
    # 06-03, CK-B (`rootful-static-map`): the subordinate-range scheme is one-way, so the document
    # has to carry it, not only the contract file's header.
    "subordinate-uid-ranges": (
        "a static user-namespace map, never an automatically allocated one",
        "a 65536-wide subordinate range starting at 1000000000 + (uid - 10100) * 65536",
        "a subordinate range is never reclaimed",
    ),
    "ports": (
        "each kind declares one port, unique across rows",
        "socket-exclusivity residual already documented for port 9120",
    ),
}


def test_companion_registry_table_names_the_registry_columns_in_order() -> None:
    header = _companion_registry_table()[0]

    assert tuple(cell.casefold() for cell in header) == _REGISTRY_COLUMNS


def test_companion_registry_section_precedes_the_single_companion_section() -> None:
    lines = _MANAGED_RUNTIME_PATH.read_text(encoding="utf-8").splitlines()
    registry = next(i for i, line in enumerate(lines) if line.strip() == _REGISTRY_HEADING)
    instance_heading = "### Incus Files Companion"
    instance = next(i for i, line in enumerate(lines) if line.strip() == instance_heading)

    assert registry < instance, (
        "the general registry contract must precede the one documented instance of it"
    )


def test_the_postiz_row_is_documented_with_its_registry_columns() -> None:
    # 06-03 (D-01). Pinned by value as well as by the generic parity below, because every column
    # of this row is one-way: the artifact kind, the gate minimum, the first numeric UID.
    rows = _companion_registry_rows()

    assert rows.get("postiz") == ("oci-digest", "0.14.0", "10101", "9130", "bypass")
    assert list(rows) == ["filebrowser", "postiz"], "the documented rows must be sorted by key"


def test_every_registry_kind_is_documented() -> None:
    documented = _documented_companion_kinds()

    missing = set(KNOWN_COMPANION_KINDS) - documented
    assert not missing, (
        f"companion kinds in the registry but missing from {_MANAGED_RUNTIME_PATH}: "
        f"{sorted(missing)}. Add a row to the '### Companion Registry' table rather than "
        "deleting this assertion."
    )


def test_every_documented_kind_exists_in_the_registry() -> None:
    documented = _documented_companion_kinds()

    extra = documented - set(KNOWN_COMPANION_KINDS)
    assert not extra, (
        f"companion kinds documented in {_MANAGED_RUNTIME_PATH} but absent from the registry: "
        f"{sorted(extra)}. The document describes a kind the code does not have."
    )


def test_documented_columns_equal_the_python_registry_embed() -> None:
    rows = _companion_registry_rows()

    for key, documented in rows.items():
        assert documented == _expected_columns(COMPANION_REGISTRY[key]), (
            f"documented columns for {key!r} drifted from app.core.companion_registry"
        )


def test_every_contract_file_kind_is_documented() -> None:
    documented = _documented_companion_kinds()

    missing = set(_contract_file_rows()) - documented
    assert not missing, (
        f"companion kinds in {_COMPANION_CONTRACT_PATH} but missing from "
        f"{_MANAGED_RUNTIME_PATH}: {sorted(missing)}"
    )


def test_every_documented_kind_exists_in_the_contract_file() -> None:
    documented = _documented_companion_kinds()

    extra = documented - set(_contract_file_rows())
    assert not extra, (
        f"companion kinds documented in {_MANAGED_RUNTIME_PATH} but absent from "
        f"{_COMPANION_CONTRACT_PATH}: {sorted(extra)}"
    )


def test_documented_columns_equal_the_contract_file() -> None:
    contract = _contract_file_rows()
    rows = _companion_registry_rows()

    for key, documented in rows.items():
        assert documented == _expected_columns(contract[key]), (
            f"documented columns for {key!r} drifted from {_COMPANION_CONTRACT_PATH}"
        )


# ---------------------------------------------------------------------------
# The verb table (05-02). Same bidirectional design as the kind table above, extended to the
# contract file's second row type. The document is what an operator and an auditor read, so a
# verb that ships and is undocumented -- or is documented and does not ship -- is drift that has
# to be loud.
# ---------------------------------------------------------------------------


def _normalised_probe(cell: str) -> str:
    """The document's spelling of "no read probe", folded onto the contract file's marker."""
    return "-" if cell in _NO_PROBE_CELLS else cell


def test_companion_verb_table_names_its_columns_in_order() -> None:
    header = _companion_verb_table()[0]

    assert tuple(cell.casefold() for cell in header) == _VERB_COLUMNS


def test_every_contract_file_verb_is_documented() -> None:
    documented = set(_documented_verb_rows())

    missing = set(_contract_file_verb_rows()) - documented
    assert not missing, (
        f"verbs in {_COMPANION_CONTRACT_PATH} but missing from {_MANAGED_RUNTIME_PATH}: "
        f"{sorted(missing)}. Add a row to the '{_VERB_HEADING}' table rather than deleting this "
        "assertion."
    )


def test_every_documented_verb_exists_in_the_contract_file() -> None:
    documented = set(_documented_verb_rows())

    extra = documented - set(_contract_file_verb_rows())
    assert not extra, (
        f"verbs documented in {_MANAGED_RUNTIME_PATH} but absent from "
        f"{_COMPANION_CONTRACT_PATH}: {sorted(extra)}. The document describes a tool the agent "
        "cannot reach."
    )


def test_documented_verb_columns_equal_the_contract_file() -> None:
    contract = _contract_file_verb_rows()

    for name, documented in _documented_verb_rows().items():
        tool_class, method, path_template, probe, cap_unit, args_in = documented
        folded = (tool_class, method, path_template, _normalised_probe(probe), cap_unit, args_in)
        assert folded == _expected_verb_columns(contract[name]), (
            f"documented columns for {name!r} drifted from {_COMPANION_CONTRACT_PATH}"
        )


def test_documented_verb_columns_equal_the_python_verb_embed() -> None:
    for name, documented in _documented_verb_rows().items():
        tool_class, method, path_template, probe, cap_unit, args_in = documented
        folded = (tool_class, method, path_template, _normalised_probe(probe), cap_unit, args_in)
        assert folded == _expected_verb_columns(COMPANION_VERBS[name]), (
            f"documented columns for {name!r} drifted from app.core.companion_registry"
        )


def test_the_verb_table_documents_the_two_properties_a_reader_most_needs() -> None:
    """A table alone does not tell a reader what they will actually observe.

    Two claims, both surprising and both load-bearing: a `destroy` verb is absent until enabled
    AND refused by name even when named from memory (D-06 has two halves, and a reader who knows
    only the first will think hiding is the enforcement), and a newly installed companion's tools
    take up to ~60 seconds to appear because the list is cached for that long with no
    list-changed channel in either direction to shorten it.
    """
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    for claim in (
        "a `destroy`-class verb is absent from `tools/list` until the tenant enables it",
        "refused by name at call time even when an agent names it from memory",
        "up to roughly sixty seconds to appear",
        "there is no list-changed notification channel in either direction",
    ):
        assert claim in managed, f"the verb table no longer states {claim!r}"


def test_the_verb_table_documents_the_postiz_rows_and_how_they_are_reached() -> None:
    """06-06: the six postiz rows, and the four facts a reader needs to trust them.

    The row count is pinned as a literal (six filebrowser plus six postiz) so the table cannot
    shrink to agree with a contract file that also shrank. The prose claims are what the rows do
    not say: the loopback `/api` route, the one scope every call needs, what `records` counts for
    `create_post`, and where the excluded capabilities are listed with their reasons.
    """
    documented = _documented_verb_rows()
    postiz = {name for name in documented if name.startswith("postiz__")}
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    assert len(documented) == 12
    assert postiz == {
        "postiz__create_post",
        "postiz__is_connected",
        "postiz__list_groups",
        "postiz__list_integrations",
        "postiz__list_notifications",
        "postiz__list_posts",
    }
    # Casefolded, like every claim `normalized_text` is matched against.
    for claim in (
        "over the loopback port through the image's `/api` route",
        "every call requires `companions:invoke`",
        "`postiz__create_post` counts `records` as post items",
        "postiz verb table exclusions",
    ):
        assert claim in managed, f"the verb table no longer states {claim!r}"


def test_verb_reader_raises_when_the_verb_heading_is_absent(tmp_path: Path) -> None:
    document = tmp_path / "no-verb-heading.md"
    document.write_text("# Some Doc\n\n| Tool name |\n| --- |\n| x |\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="no '### Companion Verb Table' heading"):
        _documented_verb_rows(document)


def test_verb_reader_raises_on_a_table_with_zero_data_rows(tmp_path: Path) -> None:
    document = tmp_path / "empty-verb-table.md"
    document.write_text(
        "### Companion Verb Table\n\n"
        "| Tool name | Class | Method | Path template | Read probe | Cap unit | Args in |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="zero data rows"):
        _documented_verb_rows(document)


def test_verb_reader_raises_on_a_row_with_the_wrong_cell_count(tmp_path: Path) -> None:
    document = tmp_path / "malformed-verb-row.md"
    document.write_text(
        "### Companion Verb Table\n\n"
        "| Tool name | Class | Method | Path template | Read probe | Cap unit | Args in |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| `filebrowser__list_dir` | `read` | `GET` | `/api/resources` | `-` |\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="malformed row"):
        _documented_verb_rows(document)


@pytest.mark.parametrize("rule", sorted(_REGISTRY_RULE_PHRASES), ids=lambda rule: rule)
def test_companion_registry_rules_are_documented(rule: str) -> None:
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    for phrase in _REGISTRY_RULE_PHRASES[rule]:
        assert phrase in managed, f"the '{rule}' registry rule is no longer documented: {phrase!r}"


def test_reader_raises_when_the_anchor_heading_is_absent(tmp_path: Path) -> None:
    document = tmp_path / "no-heading.md"
    document.write_text("# Some Doc\n\n| Key |\n| --- |\n| filebrowser |\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="no '### Companion Registry' heading"):
        _companion_registry_rows(document)


def test_reader_raises_on_the_observation_subsystem_homonym(tmp_path: Path) -> None:
    """Pointed at the other 'Companion' sense, the reader raises rather than returning it."""
    document = tmp_path / "homonym.md"
    document.write_text(
        "## Cloud API Runtime Observation Companion\n\n"
        "Four PostgreSQL tables form the additive companion boundary.\n\n"
        "| Table | Purpose |\n| --- | --- |\n| runtime_observations | companion inbox |\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="unrelated subsystem"):
        _companion_registry_rows(document)


def test_reader_raises_on_a_table_with_zero_data_rows(tmp_path: Path) -> None:
    document = tmp_path / "empty-table.md"
    document.write_text(
        "### Companion Registry\n\n"
        "| Key | Artifact kind | Minimum CLI version | UID | Port | Egress |\n"
        "| --- | --- | --- | --- | --- | --- |\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="zero data rows"):
        _companion_registry_rows(document)


def test_reader_raises_on_a_row_with_the_wrong_cell_count(tmp_path: Path) -> None:
    document = tmp_path / "malformed-row.md"
    document.write_text(
        "### Companion Registry\n\n"
        "| Key | Artifact kind | Minimum CLI version | UID | Port | Egress |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| filebrowser | binary-sha256 | 0.0.0 | preallocated | 9120 |\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match="malformed row"):
        _companion_registry_rows(document)


def test_reader_stops_at_the_next_heading(tmp_path: Path) -> None:
    document = tmp_path / "two-tables.md"
    document.write_text(
        "### Companion Registry\n\n"
        "| Key | Artifact kind | Minimum CLI version | UID | Port | Egress |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| filebrowser | binary-sha256 | 0.0.0 | preallocated | 9120 | bypass |\n\n"
        "### Something Else\n\n"
        "| Key | Artifact kind | Minimum CLI version | UID | Port | Egress |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| smuggled | binary-sha256 | 0.0.0 | preallocated | 9121 | bypass |\n",
        encoding="utf-8",
    )

    assert _documented_companion_kinds(document) == frozenset({"filebrowser"})


@pytest.mark.parametrize("root", _PRE_RENAME_PLATFORM_ROOTS, ids=lambda root: root)
def test_managed_runtime_doc_names_no_pre_rename_platform_root_as_current(root: str) -> None:
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    offenders = re.findall(rf"{re.escape(root)}(?![-\w])\S*", managed)
    assert not offenders, (
        f"{_MANAGED_RUNTIME_PATH} still names the pre-rename platform root {root!r} as current: "
        f"{sorted(set(offenders))}. packages/cli/src/runtime/paths.ts emits the iforge spelling "
        "and keeps no reader for the clawdi one (06.1-04, user directive 2026-09-30)."
    )


@pytest.mark.parametrize("claim", _CURRENT_PLATFORM_ROOT_CLAIMS, ids=lambda claim: claim)
def test_managed_runtime_doc_states_the_current_platform_roots(claim: str) -> None:
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    assert claim in managed, (
        f"{_MANAGED_RUNTIME_PATH} no longer states {claim!r}. The roots must be CORRECTED, not "
        "deleted -- a document that says nothing cannot drift, and cannot be relied on either."
    )


@pytest.mark.parametrize("claim", _PRE_RENAME_SYSTEMD_CLAIMS, ids=lambda claim: claim)
def test_managed_runtime_doc_claims_no_pre_rename_systemd_name_as_current(claim: str) -> None:
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    assert claim.casefold() not in managed, (
        f"{_MANAGED_RUNTIME_PATH} still asserts the pre-rename systemd identity as current: "
        f"{claim!r}. packages/cli/src/runtime/systemd-user.ts writes and reads only the iforge "
        "spelling (06.1-04, user directive 2026-09-30)."
    )


@pytest.mark.parametrize("claim", _CURRENT_SYSTEMD_NAME_CLAIMS, ids=lambda claim: claim)
def test_managed_runtime_doc_states_the_current_systemd_names(claim: str) -> None:
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    assert claim.casefold() in managed, (
        f"{_MANAGED_RUNTIME_PATH} no longer states {claim!r}. The names must be CORRECTED, not "
        "deleted."
    )


# WINDOWS #198 / D-08 made the workspace ACL converge once, then assert, and this test pinned the
# document to that rule. Upstream #939 (sync step 1) retired the ACL outright: Files runs as the
# tenant runtime user that owns the home, so there is no service account to grant. The document
# must now promise neither the converge-once rule nor the older unconditional repair.
_WORKSPACE_ACL_CONVERGE_ONCE_CLAIM = "applies the managed acl once, records a root-only receipt"
_WORKSPACE_ACL_REPAIR_PROMISE = "reconciliation repairs the managed acl baseline"


def test_managed_runtime_doc_makes_no_workspace_acl_promise() -> None:
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    assert _WORKSPACE_ACL_CONVERGE_ONCE_CLAIM not in managed, (
        f"{_MANAGED_RUNTIME_PATH} states the workspace ACL converge-once rule "
        f"({_WORKSPACE_ACL_CONVERGE_ONCE_CLAIM!r}), but #939 retired the ACL: Files runs as the "
        "tenant runtime user (packages/cli/src/runtime/file-browser-isolation.ts)."
    )
    assert _WORKSPACE_ACL_REPAIR_PROMISE not in managed, (
        f"{_MANAGED_RUNTIME_PATH} promises that every reconciliation repairs the managed ACL "
        "baseline, and there is no managed ACL since #939."
    )


# G-06-5 (review IN-03): the schema digest hold is keyed on the data directory, not the receipt. A
# receipt that is absent, corrupt or written by a newer CLI parses to nothing, and over populated
# data the schema marker alone decides which Postiz digest may start. The document once scoped the
# hold to a receipt that is "applied or withdrawn", which is exactly the gap the code had.
_SCHEMA_MARKER_WITHOUT_RECEIPT_CLAIMS = (
    "whether its receipt is applied, withdrawn, missing or unreadable",
    "a receipt that is missing or unreadable does not lift the hold",
    "over populated data the marker alone decides",
    "an empty data directory has no schema to protect and takes the restore path above",
)
_SCHEMA_MARKER_RECEIPT_ONLY_SCOPE = "whether its receipt is applied or withdrawn"


@pytest.mark.parametrize("claim", _SCHEMA_MARKER_WITHOUT_RECEIPT_CLAIMS)
def test_managed_runtime_doc_states_the_schema_marker_rule_without_a_receipt(claim: str) -> None:
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    assert claim in managed, (
        f"{_MANAGED_RUNTIME_PATH} no longer states the schema marker rule for a missing or "
        f"unreadable receipt ({claim!r}). The rule must be CORRECTED, not deleted: planHeld in "
        "packages/cli/src/runtime/container-companion.ts holds populated data on its schema "
        "marker whatever the receipt says, and hands an empty data directory to the restore path."
    )
    assert _SCHEMA_MARKER_RECEIPT_ONLY_SCOPE not in managed, (
        f"{_MANAGED_RUNTIME_PATH} again scopes the schema digest hold to a receipt that is "
        f"{_SCHEMA_MARKER_RECEIPT_ONLY_SCOPE.removeprefix('whether its receipt is ')!r}. "
        "planHeld in packages/cli/src/runtime/container-companion.ts also holds populated data "
        "whose receipt is missing or unreadable (G-06-5)."
    )


# G-06-4: an update report acts only on the update it was made for. The host stamps `to_digest`
# when it makes each report, the control plane refuses one naming another update, and the rollout
# order (backend first, then a CLI that re-sends once without the field) keeps a host from wedging.
_UPDATE_REPORT_IDENTITY_CLAIMS = (
    "every update report names the update it belongs to",
    "a report naming another update is refused and changes nothing",
    "a re-sent `ready` of an update the control plane already recorded is still answered as "
    "complete",
    "a report without it, from an older cli, is applied to the update in flight as before",
    "the backend that accepts `to_digest` ships before the cli that sends it",
    "re-sends the report once without it",
)


@pytest.mark.parametrize("claim", _UPDATE_REPORT_IDENTITY_CLAIMS)
def test_managed_runtime_doc_states_the_update_report_identity_rule(claim: str) -> None:
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    assert claim in managed, (
        f"{_MANAGED_RUNTIME_PATH} no longer states the update report identity rule ({claim!r}). "
        "The code still makes the guarantee: _check_job_report in "
        "backend/app/services/companion_install.py refuses an update report whose to_digest is not "
        "the in-flight update's, and the report builders in "
        "packages/cli/src/runtime/container-companion.ts stamp to_digest when each report is made."
    )


# 06-REVIEW WR-02: populated data with no readable receipt and no schema marker is held at the
# digest the generated app unit on disk names, and refused when nothing on the host names the
# digest that wrote it.
_SCHEMA_DIGEST_WITHOUT_MARKER_CLAIMS = (
    "with no readable receipt and no marker, the digest the generated app unit on disk names "
    "decides instead",
    "nothing on the host names the digest that wrote it, so the converge starts nothing over it "
    "and reports `schema_digest_mismatch`",
)


@pytest.mark.parametrize("claim", _SCHEMA_DIGEST_WITHOUT_MARKER_CLAIMS)
def test_managed_runtime_doc_states_the_schema_digest_rule_without_a_marker(claim: str) -> None:
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    assert claim in managed, (
        f"{_MANAGED_RUNTIME_PATH} no longer states the schema digest rule for populated data with "
        f"no receipt and no marker ({claim!r}). The document must be CORRECTED, not the claim "
        "deleted: planHeldOnMarker in packages/cli/src/runtime/container-companion.ts holds such "
        "data at the digest the generated app unit on disk names, and refuses it with "
        "schema_digest_mismatch when no app unit is on disk either (06-REVIEW WR-02)."
    )


# 06-REVIEW WR-03: removal lifts the masks a failed network migration left, before the data
# directory is deleted, and a removal whose unmask fails deletes nothing further.
_REMOVAL_UNMASK_CLAIMS = (
    "removing the companion unmasks them too",
    "it then lifts any mask a failed network migration left",
    "a removal whose unmask fails deletes nothing further and keeps the receipt",
)


@pytest.mark.parametrize("claim", _REMOVAL_UNMASK_CLAIMS)
def test_managed_runtime_doc_states_the_removal_unmask(claim: str) -> None:
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    assert claim in managed, (
        f"{_MANAGED_RUNTIME_PATH} no longer states that removal lifts a failed migration's masks "
        f"({claim!r}). The document must be CORRECTED, not the claim deleted: "
        "removeContainerCompanion in packages/cli/src/runtime/container-companion.ts unmasks the "
        "recorded units and every unit of the kind except the network after the withdrawal and "
        "before the data directory is deleted, and stops before deleting anything when the unmask "
        "fails (06-REVIEW WR-03)."
    )


# 06-REVIEW WR-04: a condition report answered 404 is not settled. The backend answers 404 while
# the runtime key's agent binding does not resolve, so the condition stays due for the next pass
# while the conditions after it are still sent. Only 400 and 422 count as refused.
_CONDITION_NOT_FOUND_CLAIMS = (
    "(http 400 or 422, for example from a backend older than the cli) counts as refused",
    "a 404 means the runtime key reaches no installation right now",
    "that condition stays due for the next pass",
)
_CONDITION_OLD_FINAL_STATUSES = "http 400, 404 or 422"


@pytest.mark.parametrize("claim", _CONDITION_NOT_FOUND_CLAIMS)
def test_managed_runtime_doc_states_the_condition_not_found_rule(claim: str) -> None:
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    assert claim in managed, (
        f"{_MANAGED_RUNTIME_PATH} no longer states how a host condition answered 404 is handled "
        f"({claim!r}). The document must be CORRECTED, not the claim deleted: "
        "CompanionReportRejectedError.keepDue in packages/cli/src/runtime/companion-progress.ts "
        "marks a 404 as kept due, and reportContainerCompanionConditions in "
        "packages/cli/src/runtime/container-companion.ts leaves that condition due for the next "
        "pass and still sends the ones after it (06-REVIEW WR-04)."
    )
    assert _CONDITION_OLD_FINAL_STATUSES not in managed, (
        f"{_MANAGED_RUNTIME_PATH} again lists 404 among the final rejections of a host condition "
        f"({_CONDITION_OLD_FINAL_STATUSES!r}). The document must be CORRECTED: only 400 and 422 "
        "are final in packages/cli/src/runtime/companion-progress.ts, and a 404 keeps the "
        "condition due (06-REVIEW WR-04)."
    )


# 06-REVIEW WR-05: a failed update report the host made without a digest, because nothing recorded
# which update an interrupted swap belonged to, stays unlabeled. It is never named from the
# receipt's refusal record, which then holds the entry's pin, or from the entry.
_UNLABELED_UPDATE_REPORT_CLAIMS = (
    "a report the host made without a digest, because nothing recorded which update an "
    "interrupted swap belonged to, is sent without one",
    "it is never named from the receipt or the entry",
)


@pytest.mark.parametrize("claim", _UNLABELED_UPDATE_REPORT_CLAIMS)
def test_managed_runtime_doc_states_the_unlabeled_update_report_rule(claim: str) -> None:
    managed = _normalized_text(_MANAGED_RUNTIME_PATH)

    assert claim in managed, (
        f"{_MANAGED_RUNTIME_PATH} no longer states that a deliberately unlabeled update report "
        f"stays unlabeled ({claim!r}). The document must be CORRECTED, not the claim deleted: "
        "refusePostizUpdate in packages/cli/src/runtime/container-companion.ts queues such a "
        "report with to_digest null, and identifiedUpdateReport names only a report whose key is "
        "absent, so it is sent without one (06-REVIEW WR-05)."
    )
