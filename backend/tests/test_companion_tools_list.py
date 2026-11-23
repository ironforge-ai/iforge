"""The exposed toolset, the curated registry table and the COVERAGE decision records, pinned.

TOOL-08 says each companion exposes a CURATED verb-level toolset rather than auto-generated
coverage of its full API. That is not an absence -- it is a decision record, ONE PER KIND, and
this module is what makes each record binding:

* ``filebrowser`` -> ``.planning/phases/05-companion-tool-contract-mcp-fa-ade/COVERAGE.md``
* ``postiz``      -> ``.planning/phases/06-first-companion-end-to-end/COVERAGE.md`` (06-06)

A second companion starts from its own full-coverage baseline (the Phase 5 matrix's closing note),
so correspondence is asserted PER KIND: each kind's verb rows against its own matrix, never a
union against a union, where one kind's surplus could hide another's gap. Three things must
agree, and each pair is held by an assertion that fails when either side moves:

* ``COMPANION_VERBS`` (and therefore ``config/companion-registry.txt``, which the registry parity
  suite pins it to) and the names ``list_companion_tools`` actually returns, for the kinds the
  tenant actually deployed.
* Each kind's ``COMPANION_VERBS`` rows and its ``COVERAGE.md``'s ``INTEGRATE`` rows, one to one, in
  both directions -- so a capability cannot reach the agent without appearing in that kind's
  decision record (T-5-15, T-06-06-01), and a row cannot be dropped without the record noticing.
* Every returned ``annotations`` block and ``derive_annotations`` of that tool's class, compared
  against the FUNCTION rather than against a hand-written expectation, so the class vocabulary and
  the annotation vocabulary cannot drift (D-07).

**No expected set is derived from the endpoint's own output.** A test that asked
``list_companion_tools`` what it exposes and then asserted the answer equals itself would be green
forever and would prove nothing -- Phase 4 registered that shape as T-4-19 and this module
inherits the bar. The expected toolset is built from the REGISTRY rows of the kinds the fixture
deployed (``deploy_companion_state`` returns its companions record), never from the response.

**Mutation checks for the per-kind correspondence (06-06).** Performed by hand, confirmed red,
restored immediately, and recorded so a later reader can repeat them rather than trust this
paragraph:

* The ``posts.list`` INTEGRATE row deleted from the Phase 6 ``COVERAGE.md`` ->
  ``test_toolset_matches_coverage_matrix[postiz]`` fails ("verb row without an INTEGRATE row:
  ['posts.list']") while the ``[filebrowser]`` case stays green.
* The ``postiz__list_groups`` row deleted from the Phase 6 derived verb table ->
  ``test_toolset_matches_coverage_matrix[postiz]`` and
  ``test_the_coverage_decision_table_and_the_derived_table_are_readable[postiz]`` fail.

**Annotations are never the gate.** Nothing here asserts that a refusal happened because an
annotation said so. Annotations are advisory client-side hints layered on top of the server-side
class check; refusals are proven against that gate in plans 05-03 and 05-04.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.auth import AuthContext
from app.core.companion_registry import COMPANION_VERBS, derive_annotations
from app.models.companion_tool_policy import CompanionDestroyEnablement
from app.services import companion_facade
from app.services.companion_facade import list_companion_tools, reset_companion_tools_cache
from tests.doc_contract import REPO_ROOT
from tests.hosted_runtime_fixtures import deploy_companion_state

# One decision record per companion kind. Each file's FIRST `| capability | decision | reason |`
# table and FIRST derived-verb-table section are that kind's TOOL-08 record; the Phase 6 file's
# operator-API tables come after both and are not agent tools.
COVERAGE_PATHS: dict[str, Path] = {
    "filebrowser": (
        REPO_ROOT / ".planning" / "phases" / "05-companion-tool-contract-mcp-fa-ade" / "COVERAGE.md"
    ),
    "postiz": (
        REPO_ROOT / ".planning" / "phases" / "06-first-companion-end-to-end" / "COVERAGE.md"
    ),
}

# The decision table's header, casefolded, as the anchor. Matched on the whole header rather than
# on the word "capability", which appears in prose throughout that document.
_DECISION_HEADER = ("capability", "decision", "reason")

# The heading above the derived verb table, which is the join between a capability and a tool
# name. Without it the INTEGRATE rows and the verb rows have no common key to correspond on.
_DERIVED_TABLE_HEADING = "## Derived verb table"
_DERIVED_COLUMNS = ("tool name (d-04)", "class", "capability", "read probe (d-13)", "cap unit")

# The classes the façade can execute, and therefore the classes `tools/list` CONSIDERS. Written as
# a LITERAL rather than imported, so widening the gate stays a deliberate edit HERE and in
# `companion_facade.py` together -- rather than a change that silently updates its own test.
#
# 05-03 widened it from `{"read"}` to the registry's full vocabulary, because what now decides
# whether a write may run is the server-side POLICY, not this set. `destroy` being in the set does
# NOT mean a destroy verb is listed: it is additionally gated on the tenant's enablement row, which
# is the first half of D-06 and is asserted by `test_destroy_verb_absent_until_enabled` below.
EXPOSED_TOOL_CLASSES = frozenset({"read", "create", "mutate", "destroy"})

# The class whose exposure needs a tenant opt-in on top of the class gate, and the one verb that
# carries it today. Named rather than inlined, because two tests below must agree on which verb the
# enablement gate is supposed to be hiding.
ENABLEMENT_GATED_CLASS = "destroy"
DESTROY_TOOL = "filebrowser__delete_path"

# A companion kind the registry does not own, used to prove a deployed kind with zero verb rows
# contributes zero tools without suppressing another kind's. Every kind the registry DOES own has
# verb rows, so this case has no natural fixture.
KIND_WITHOUT_VERBS = "zzz-no-verbs"


@pytest.fixture(autouse=True)
def _clear_companion_tools_cache():
    reset_companion_tools_cache()
    yield
    reset_companion_tools_cache()


def _kind_of(tool_name: str) -> str:
    return tool_name.partition("__")[0]


def _verbs_of_kinds(kinds: Iterable[str]) -> set[str]:
    """The registry's tool names for ``kinds`` -- the verb rows those kinds own, nothing else."""
    wanted = set(kinds)
    return {name for name in COMPANION_VERBS if _kind_of(name) in wanted}


def _curated_names_in_registry_order(
    kinds: Iterable[str], *, destroy_enabled: bool = False
) -> list[str]:
    """The names the façade should expose, built from the REGISTRY, in ascending `(kind, verb)`.

    Built from ``COMPANION_VERBS`` rows of the DEPLOYED kinds and the two published gates -- the
    class set and, for the enablement-gated class, the tenant's opt-in -- never from a response.
    ``kinds`` is the fixture's deployed companions record, so a kind the registry carries but the
    tenant never installed contributes nothing to the expectation. A list rather than a set
    because the order is itself asserted below.
    """
    wanted = set(kinds)
    return [
        name
        for name, entry in sorted(COMPANION_VERBS.items())
        if _kind_of(name) in wanted
        and entry.tool_class in EXPOSED_TOOL_CLASSES
        and (destroy_enabled or entry.tool_class != ENABLEMENT_GATED_CLASS)
    ]


async def _enable_destroy(db_session, seed_user, *, companion_key: str = "filebrowser") -> None:
    """Turn the tenant's destroy group on for one companion.

    The switch is a ROW, so enabling is an insert and revoking is a delete. There is no boolean to
    flip, which is what keeps "enabled" and "a row saying disabled" from being two spellings of the
    same state.
    """
    db_session.add(
        CompanionDestroyEnablement(
            user_id=seed_user.id,
            companion_key=companion_key,
            enabled_at=datetime.now(UTC),
            enabled_by_user_id=seed_user.id,
        )
    )
    await db_session.commit()
    reset_companion_tools_cache()


def _coverage_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError as cause:  # pragma: no cover - an unreadable record is an environment fault
        raise AssertionError(
            f"the coverage decision record is unreadable at {path}. The one-to-one "
            "assertion below cannot pass without it, and skipping would let curation become "
            "undisclosed capability removal."
        ) from cause


def _table_after(path: Path, start: int, columns: tuple[str, ...]) -> list[list[str]]:
    """The contiguous markdown table beginning at ``start``, header and delimiter consumed.

    Raises on an absent delimiter, zero data rows, or a row whose cell count is not ``columns``'.
    An empty result would let every set comparison below pass by comparing nothing to nothing.
    """
    lines = _coverage_lines(path)
    delimiter = [cell.strip() for cell in lines[start + 1].strip().split("|")[1:-1]]
    if not delimiter or not all(cell and set(cell) <= {"-", ":"} for cell in delimiter):
        raise AssertionError(
            f"the row after the header at line {start + 1} of {path} is not a markdown "
            f"delimiter row: {delimiter!r}"
        )
    rows: list[list[str]] = []
    for line in lines[start + 2 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        # Backticks are NOT stripped here. The decision table's capability cell carries several
        # backticked spans (`resources.list` — `GET /api/resources`), so a blanket strip would
        # leave a fragment that looks like a capability and is not; each call site extracts what
        # it needs from the raw cell instead.
        cells = [cell.strip() for cell in stripped.split("|")[1:-1]]
        if len(cells) != len(columns):
            raise AssertionError(
                f"malformed row in {path}: expected {len(columns)} cells, "
                f"got {len(cells)}: {cells!r}"
            )
        rows.append(cells)
    if not rows:
        raise AssertionError(
            f"the table at line {start} of {path} has zero data rows. An empty result "
            "would let a one-to-one assertion pass by comparing nothing to nothing."
        )
    return rows


def _decision_rows(path: Path) -> list[list[str]]:
    """Every row of the FIRST capability decision table: ``[capability, decision, reason]``.

    The first table only, on purpose: it is the kind's agent-tool record in both files, and the
    Phase 6 file carries a later operator-API table under the same header that is not TOOL-08.
    """
    lines = _coverage_lines(path)
    for index, line in enumerate(lines):
        header = tuple(cell.strip().casefold() for cell in line.strip().split("|")[1:-1])
        if header == _DECISION_HEADER:
            return _table_after(path, index, _DECISION_HEADER)
    raise AssertionError(
        f"no '| capability | decision | reason |' table in {path}. That table IS "
        "TOOL-08; add it rather than deleting this assertion."
    )


def _derived_verb_rows(path: Path) -> list[list[str]]:
    """Every row of the derived verb table, backticks stripped: ``[name, class, capability, ...]``.

    The join between a capability and a tool name. Without it the ``INTEGRATE`` rows and the verb
    rows have no common key to correspond on at all. The FIRST such section of the file.
    """
    lines = _coverage_lines(path)
    for index, line in enumerate(lines):
        if line.strip().startswith(_DERIVED_TABLE_HEADING):
            rows = _table_after(path, index + 2, _DERIVED_COLUMNS)
            return [[cell.strip("`").strip() for cell in row] for row in rows]
    raise AssertionError(f"no '{_DERIVED_TABLE_HEADING}' section in {path}")


def _leading_capability(cell: str) -> str:
    """The FIRST backticked span of a decision-table capability cell, e.g. ``resources.list``.

    That cell carries the capability AND its endpoint -- ``` `resources.list` — `GET
    /api/resources` ``` -- while the derived table carries the capability alone. The leading
    backticked span is the only value common to both, and it is what the two tables correspond on.
    Anchored at the START of the cell, because the second span is the endpoint and matching it
    would silently correspond every INTEGRATE row on the same string.
    """
    match = re.match(r"`([^`]+)`", cell)
    if match is None:
        raise AssertionError(
            f"a COVERAGE.md capability cell does not begin with a backticked capability: {cell!r}"
        )
    return match.group(1)


# ---------------------------------------------------------------------------
# The exposed toolset equals the registry table.
# ---------------------------------------------------------------------------


def test_the_exposed_class_gate_is_the_one_this_plan_documents() -> None:
    """Pin the gate itself, so widening it is a decision rather than a side effect.

    `_EXECUTABLE_TOOL_CLASSES` is both the `tools/list` filter and the call-time gate. Reading it
    into the expected set below without pinning it here would make every assertion in this module
    re-derive itself from whatever the façade currently allows.
    """
    assert companion_facade._EXECUTABLE_TOOL_CLASSES == EXPOSED_TOOL_CLASSES


async def test_toolset_is_exactly_the_curated_set(db_session, seed_user) -> None:
    _env_id, deployed = await deploy_companion_state(
        db_session, seed_user, machine_prefix="toolset"
    )
    auth = AuthContext(user=seed_user)

    exposed = {tool["name"] for tool in await list_companion_tools(auth, db_session)}
    # From the registry rows of the kinds the fixture DEPLOYED -- never from the response.
    curated = set(_curated_names_in_registry_order(deployed))

    assert set(deployed) == {"filebrowser"}, "the fixture's deployed kinds changed; re-derive"
    assert curated, "the curated set is empty; a set comparison against it would prove nothing"
    assert exposed <= curated, f"exposed but not curated: {sorted(exposed - curated)}"
    assert curated <= exposed, f"curated but not exposed: {sorted(curated - exposed)}"
    assert exposed == curated


async def test_a_filebrowser_only_tenant_sees_no_postiz_tool(db_session, seed_user) -> None:
    """A registry row is not an installation (06-06).

    The registry carries postiz verb rows for every tenant; a tenant that deployed filebrowser alone
    must see none of them. Asserted against the registry's postiz rows -- literally non-empty, so
    the disjointness check cannot pass by comparing against nothing.
    """
    _env_id, deployed = await deploy_companion_state(
        db_session, seed_user, machine_prefix="nopostiz"
    )
    auth = AuthContext(user=seed_user)

    names = [str(tool["name"]) for tool in await list_companion_tools(auth, db_session)]
    postiz_rows = _verbs_of_kinds({"postiz"})

    assert "postiz" not in deployed
    assert len(postiz_rows) == 6
    assert not [name for name in names if name.startswith("postiz__")], names
    assert set(names).isdisjoint(postiz_rows)
    # ... and not because the list is blank: the deployed kind's tools are all there.
    assert names == _curated_names_in_registry_order(deployed)


async def test_every_verb_the_toolset_withholds_is_withheld_by_a_gate_this_module_names(
    db_session, seed_user
) -> None:
    """The complement of the assertion above, stated rather than left as a silent gap.

    Six filebrowser verbs are curated. Before 05-03 the façade executed `read` only, so the
    complement was four verbs withheld by CLASS. Now every class is executable and the complement
    is one verb withheld by the tenant's destroy ENABLEMENT -- a different gate, and the test says
    which, so a verb that disappears for some other reason (a lost row, a typo in a name) fails
    here instead of looking like the documented filter. The complement is taken over the DEPLOYED
    kinds' rows: a postiz row is absent because postiz is not installed, which is not a gate.
    """
    _env_id, deployed = await deploy_companion_state(
        db_session, seed_user, machine_prefix="withheld"
    )
    auth = AuthContext(user=seed_user)

    exposed = {tool["name"] for tool in await list_companion_tools(auth, db_session)}
    withheld = {name for name in _verbs_of_kinds(deployed) if name not in exposed}

    assert withheld == {DESTROY_TOOL}
    for name in withheld:
        tool_class = COMPANION_VERBS[name].tool_class
        # Either the class is not executable at all, or it is the enablement-gated class and this
        # tenant has not opted in. Anything withheld for a third reason is a defect, not a filter.
        assert tool_class not in EXPOSED_TOOL_CLASSES or tool_class == ENABLEMENT_GATED_CLASS, name


async def test_destroy_verb_absent_until_enabled(db_session, seed_user) -> None:
    """The FIRST half of D-06: a destroy verb is not in the list until the tenant enables it.

    The absence is asserted against the full expected set in both states, never as "the name is
    missing". A general list failure -- an empty toolset, a resolution error swallowed into `[]` --
    would satisfy a bare absence check and read as a correct refusal, which is exactly the shape of
    green test this phase keeps refusing to write.

    The second half, refusal by NAME at call time, deliberately lives in
    ``backend/tests/test_companion_tools_call.py`` and never reads a name from this endpoint. Two
    gates, two files: hiding a tool is not the enforcement (T-5-18).
    """
    _env_id, deployed = await deploy_companion_state(
        db_session, seed_user, machine_prefix="destroygate"
    )
    auth = AuthContext(user=seed_user)

    before = [tool["name"] for tool in await list_companion_tools(auth, db_session)]

    assert before == _curated_names_in_registry_order(deployed, destroy_enabled=False)
    assert DESTROY_TOOL not in before
    # The other five are all present: absence of ONE name, not a blank list.
    assert set(before) == _verbs_of_kinds(deployed) - {DESTROY_TOOL}

    await _enable_destroy(db_session, seed_user)
    after = [tool["name"] for tool in await list_companion_tools(auth, db_session)]

    assert after == _curated_names_in_registry_order(deployed, destroy_enabled=True)
    assert DESTROY_TOOL in after
    assert set(after) == _verbs_of_kinds(deployed)


def test_the_enabling_plan_pointer_covers_the_whole_class_vocabulary() -> None:
    """A stale pointer is worse than no pointer, so the pointer is pinned.

    ``_CLASS_ENABLING_PLAN["create"]`` read `"05-02"` from the moment 05-01 wrote it until 05-03
    corrected it -- wrong for the whole of 05-02, and wrong in a user-facing refusal message. This
    asserts the map is total over the classes that actually ship and that the three write classes
    name the plan that enabled them, so the next drift is a failure rather than a surprise.
    """
    shipped_classes = {entry.tool_class for entry in COMPANION_VERBS.values()}

    assert shipped_classes <= set(companion_facade._CLASS_ENABLING_PLAN)
    assert companion_facade._CLASS_ENABLING_PLAN["read"] == "05-01"
    for write_class in ("create", "mutate", "destroy"):
        assert companion_facade._CLASS_ENABLING_PLAN[write_class] == "05-03", write_class


async def test_toolset_order_is_the_registry_order(db_session, seed_user) -> None:
    """An ordered SEQUENCE, not a set.

    Set equality leaves the exposed order unspecified, so a client that renders the catalog
    positionally could watch it reshuffle between two identical reads. The contract file's
    ascending `(kind, verb)` order is the answer, and the parser enforces that order at the file.
    """
    _env_id, deployed = await deploy_companion_state(db_session, seed_user, machine_prefix="order")
    auth = AuthContext(user=seed_user)

    first = [tool["name"] for tool in await list_companion_tools(auth, db_session)]
    second = [tool["name"] for tool in await list_companion_tools(auth, db_session)]
    reset_companion_tools_cache()
    uncached = [tool["name"] for tool in await list_companion_tools(auth, db_session)]

    assert first == _curated_names_in_registry_order(deployed)
    # Twice inside the TTL, and once with the cache dropped: a stable order that only holds
    # because the answer was memoised is not a stable order.
    assert second == first
    assert uncached == first


async def test_every_annotation_block_equals_derive_annotations_of_its_class(
    db_session, seed_user
) -> None:
    await deploy_companion_state(db_session, seed_user, machine_prefix="annotations")
    auth = AuthContext(user=seed_user)

    tools = await list_companion_tools(auth, db_session)

    assert tools, "no companion tools to check annotations on"
    for tool in tools:
        name = tool["name"]
        expected = derive_annotations(COMPANION_VERBS[str(name)].tool_class)
        # Compared against the pure function, not against a hand-written triple: a hand-written
        # expectation is a second authority for annotation values and the drift D-07 forbids.
        assert tool["annotations"] == expected, name


async def test_a_deployed_kind_with_no_verb_rows_contributes_nothing_and_suppresses_nothing(
    db_session, seed_user
) -> None:
    _env_id, deployed = await deploy_companion_state(
        db_session,
        seed_user,
        machine_prefix="twokinds",
        extra_companions={KIND_WITHOUT_VERBS: {"version": "v0", "port": 9199}},
    )
    auth = AuthContext(user=seed_user)

    names = [tool["name"] for tool in await list_companion_tools(auth, db_session)]

    assert KIND_WITHOUT_VERBS in deployed
    assert not _verbs_of_kinds({KIND_WITHOUT_VERBS})
    # Zero tools from the verb-less kind ...
    assert not [name for name in names if str(name).startswith(f"{KIND_WITHOUT_VERBS}__")]
    # ... and it does not blank the kind that does have rows.
    assert names == _curated_names_in_registry_order(deployed)


# ---------------------------------------------------------------------------
# The toolset equals the COVERAGE decision record -- PER KIND (06-06).
# ---------------------------------------------------------------------------

# Parametrized over `COVERAGE_PATHS` with the kind as the test id, so a failure names the kind
# whose record disagrees (`test_toolset_matches_coverage_matrix[postiz]`).
_PER_KIND = pytest.mark.parametrize(
    ("kind", "coverage_path"), list(COVERAGE_PATHS.items()), ids=list(COVERAGE_PATHS)
)


def test_every_kind_with_verb_rows_has_its_own_coverage_matrix() -> None:
    # A kind that ships verb rows with no decision record would have no correspondence test at
    # all -- its whole toolset would be undisclosed. Asserted in both directions, so a stale
    # matrix for a kind that no longer ships verbs is caught too.
    kinds_with_verbs = {_kind_of(name) for name in COMPANION_VERBS}

    assert kinds_with_verbs == set(COVERAGE_PATHS), (
        f"verb rows without a matrix: {sorted(kinds_with_verbs - set(COVERAGE_PATHS))}; "
        f"matrix without verb rows: {sorted(set(COVERAGE_PATHS) - kinds_with_verbs)}"
    )


@_PER_KIND
def test_the_coverage_decision_table_and_the_derived_table_are_readable(
    kind: str, coverage_path: Path
) -> None:
    # Asserted separately so a reader that silently returned nothing fails HERE, with a message
    # about the document, rather than making the correspondence assertions below vacuous.
    assert len(_decision_rows(coverage_path)) >= 2
    assert len(_derived_verb_rows(coverage_path)) == len(_verbs_of_kinds({kind}))


@_PER_KIND
def test_toolset_matches_coverage_matrix(kind: str, coverage_path: Path) -> None:
    """Every INTEGRATE capability maps to exactly one verb row of THIS kind, and every row back.

    This is what stops curation from becoming undisclosed capability expansion OR undisclosed
    capability removal (T-5-15, T-06-06-01): a verb row with no INTEGRATE row means the agent
    gained reach nobody recorded deciding to give it, and an INTEGRATE row with no verb row means
    the record promises a capability that does not ship. Per kind, so one companion's record can
    never vouch for another companion's rows.
    """
    kind_verbs = _verbs_of_kinds({kind})
    assert kind_verbs, f"no {kind} verb rows; the correspondence would compare nothing"

    integrated = [
        _leading_capability(capability)
        for capability, decision, _reason in _decision_rows(coverage_path)
        if decision.casefold() == "integrate"
    ]
    assert len(integrated) == len(set(integrated)), f"duplicate INTEGRATE rows: {integrated}"

    derived = {
        tool_name: capability
        for tool_name, _cls, capability, _probe, _unit in _derived_verb_rows(coverage_path)
    }
    assert len(set(derived.values())) == len(derived), "two verb rows claim one capability"

    # Verb rows <-> derived table, both directions.
    assert set(derived) == kind_verbs, (
        f"only in {coverage_path}: {sorted(set(derived) - kind_verbs)}; "
        f"only in the registry: {sorted(kind_verbs - set(derived))}"
    )
    # Derived table <-> INTEGRATE rows, both directions. Composed with the above, this is the
    # one-to-one correspondence between an INTEGRATE row and a verb row.
    assert set(derived.values()) == set(integrated), (
        f"INTEGRATE without a verb row: {sorted(set(integrated) - set(derived.values()))}; "
        f"verb row without an INTEGRATE row: {sorted(set(derived.values()) - set(integrated))}"
    )


@_PER_KIND
def test_the_derived_coverage_table_agrees_with_the_registry_cell_for_cell(
    kind: str, coverage_path: Path
) -> None:
    rows = _derived_verb_rows(coverage_path)
    assert rows
    for tool_name, tool_class, _capability, read_probe, cap_unit in rows:
        assert _kind_of(tool_name) == kind, f"{tool_name} is in the {kind} matrix"
        entry = COMPANION_VERBS[tool_name]
        assert tool_class == entry.tool_class, tool_name
        assert cap_unit == entry.cap_unit, tool_name
        # `—` is the document's spelling of the contract file's `-` marker for "no probe".
        documented_probe = None if read_probe in {"-", "—", ""} else read_probe
        assert documented_probe == entry.read_probe, tool_name


@_PER_KIND
def test_every_opt_out_row_carries_a_reason(kind: str, coverage_path: Path) -> None:
    """An opt-out with an empty reason is capability removal nobody can disagree with.

    The prohibition this plan carries in as many words: curation must not become undisclosed
    capability removal, so every excluded capability records a sentence a reader can argue with.
    """
    opted_out = [row for row in _decision_rows(coverage_path) if row[1].casefold() == "opt-out"]

    assert opted_out, f"no {kind} OPT-OUT rows; full coverage would make the curation claim vacuous"
    for capability, _decision, reason in opted_out:
        assert reason.strip(), f"{kind}: {capability} is excluded with no recorded reason"
