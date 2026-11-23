"""Red and green proof for the phase 03.3 machine-local coexistence guard.

``scripts/check_rebrand_coexistence.py`` is the only automated protection against this fork and
upstream ``clawdi`` claiming the same id on one user's machine. GitHub Actions never execute
against this origin (``lefthook.yml``), so a guard whose *failing* behaviour was never
demonstrated is not protection at all -- it is a green light with no bulb behind it. That is the
same reason ``test_rebrand_guards.py`` exists, applied to the guard 03.3 adds.

The ledger is asserted in BOTH directions, so both directions need a red case. A ``MOVED`` entry
that is still spelled the old way is the collision itself; a ``PENDING`` entry whose literal has
already gone is the ledger rotting into a memory of the tree rather than a measurement of it.
Only the first is intuitive, and only the second keeps the other eleven plans of this phase
honest, so each gets a red case and a green case here.

Every red case injects ``contents`` (and, where a different ledger is needed, monkeypatches
``COEXISTENCE_IDS``) rather than mutating the working tree. A guard proven by half-migrating a
real id on disk would leave the repository one crashed test away from a broken install, and
would be unrunnable in parallel.

The script is not an importable package -- ``scripts/`` has no ``__init__.py`` and the guards
must stay runnable under bare ``python3``. It is loaded by path through
``importlib.util.spec_from_file_location``, with the repository root resolved through
``tests.doc_contract.REPO_ROOT`` so this module agrees with the other repository-root suites.

HOST-SIDE COVERAGE LEDGER (WR-06 discipline, stated rather than implied). Unlike
``test_rebrand_guards.py``, NO assertion in this module shells out to ``git``: the coexistence
ledger reads named paths and injected mappings only. Nothing here is therefore gated behind a
Docker-vs-host skip, and every test below runs identically inside ``scripts/test.sh``'s clean
runner and on a developer's checkout. There is no assertion in this file that the host-side run
compensates for, because there is no assertion in this file that the container cannot make.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from types import ModuleType

import pytest

from tests.doc_contract import REPO_ROOT as _REPO_ROOT

_SCRIPTS = _REPO_ROOT / "scripts"


def _load_guard(name: str) -> ModuleType:
    """Load a bare ``scripts/*.py`` guard by path, under a namespaced module name."""
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_rebrand_guard_{name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load guard script {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


coexistence = _load_guard("check_rebrand_coexistence")
# Loaded so the scope fence below is DERIVED from the carve-out guard that owns those literals
# rather than restated here. A fence written from memory drifts away from the thing it fences,
# and this file's whole subject is ledgers that drift (P33-11 / WINDOWS #62).
carveouts = _load_guard("check_rebrand_carveouts")

_MCP_SERVER = "packages/cli/src/mcp/server.ts"


def _entry(status: str, path: str = _MCP_SERVER, literal: str = 'name: "clawdi",') -> tuple:
    """One synthetic ledger record in the guard's own ``(path, literal, status, owner)`` shape."""
    return (path, literal, status, "03.3-01")


def test_coexistence_guard_passes_against_the_untouched_working_tree() -> None:
    """Green, against the real tree. D-10 requires the guard be green at every commit."""
    checked = coexistence.validate_coexistence()

    assert isinstance(checked, int)
    assert checked > 0
    assert checked == len(coexistence.COEXISTENCE_IDS)


def test_a_moved_id_still_spelled_the_old_way_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red, MOVED direction. This is the collision the ledger exists to catch.

    Pinned to a one-entry synthetic ledger, like every other semantic case below, so the
    assertion stays about the DIRECTION rather than about however many entries the real ledger
    happens to hold on the day it is run.
    """
    monkeypatch.setattr(coexistence, "COEXISTENCE_IDS", (_entry(coexistence.MOVED),))
    contents = {_MCP_SERVER: 'const server = new Server({ name: "clawdi", version: "0.0.1" });'}

    with pytest.raises(coexistence.CoexistenceError, match=re.escape(_MCP_SERVER)):
        coexistence.validate_coexistence(contents=contents)


def test_a_moved_id_that_is_gone_is_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """Green, MOVED direction, driven by injection rather than by the real file."""
    monkeypatch.setattr(coexistence, "COEXISTENCE_IDS", (_entry(coexistence.MOVED),))
    contents = {_MCP_SERVER: 'const server = new Server({ name: "iforge", version: "0.0.1" });'}

    assert coexistence.validate_coexistence(contents=contents) == 1


def test_a_pending_id_that_is_still_present_is_green(monkeypatch: pytest.MonkeyPatch) -> None:
    """Green, PENDING direction. An id awaiting its plan must still be spelled the old way."""
    monkeypatch.setattr(coexistence, "COEXISTENCE_IDS", (_entry(coexistence.PENDING),))
    contents = {_MCP_SERVER: 'name: "clawdi",'}

    assert coexistence.validate_coexistence(contents=contents) == 1


def test_a_pending_id_that_has_already_moved_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red, PENDING direction -- the half that stops the ledger becoming a memory.

    A guard that only asserted absence would go green the moment an id moved without its entry
    being flipped, and every later plan would read a ledger that no longer describes the tree.
    """
    monkeypatch.setattr(coexistence, "COEXISTENCE_IDS", (_entry(coexistence.PENDING),))
    contents = {_MCP_SERVER: 'name: "iforge",'}

    with pytest.raises(coexistence.CoexistenceError, match="flip the entry to MOVED"):
        coexistence.validate_coexistence(contents=contents)


def test_the_failure_names_the_owning_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator hitting this guard must learn which plan owns the id, not just that it moved."""
    monkeypatch.setattr(coexistence, "COEXISTENCE_IDS", (_entry(coexistence.PENDING),))

    with pytest.raises(coexistence.CoexistenceError, match="03.3-01"):
        coexistence.validate_coexistence(contents={_MCP_SERVER: "nothing here"})


def test_a_path_absent_from_the_injected_contents_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence is not absence. A path the guard cannot read must fail, never pass vacuously."""
    monkeypatch.setattr(coexistence, "COEXISTENCE_IDS", (_entry(coexistence.MOVED),))

    with pytest.raises(coexistence.CoexistenceError, match="absent from the contents"):
        coexistence.validate_coexistence(contents={})


def test_an_empty_ledger_is_itself_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guard with nothing to check passes every assertion it makes, which is the defect."""
    monkeypatch.setattr(coexistence, "COEXISTENCE_IDS", ())

    with pytest.raises(coexistence.CoexistenceError, match="empty"):
        coexistence.validate_coexistence()


def test_a_ledger_entry_naming_the_guard_itself_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-reference makes the assertion vacuous -- the ``_SCHEMA_SCAN_EXCLUDES`` failure mode."""
    for path in coexistence._LEDGER_SELF_EXCLUDES:
        ledger = (_entry(coexistence.MOVED, path=path),)
        monkeypatch.setattr(coexistence, "COEXISTENCE_IDS", ledger)
        with pytest.raises(coexistence.CoexistenceError, match="guard's own source"):
            coexistence.validate_coexistence(contents={path: ""})


def test_every_ledger_entry_declares_a_status_the_guard_knows() -> None:
    """A typo'd status must not silently skip the entry it was written to protect."""
    unknown = [
        (path, literal, status)
        for path, literal, status, _ in coexistence.COEXISTENCE_IDS
        if status not in (coexistence.MOVED, coexistence.PENDING)
    ]

    assert unknown == []


_PLAN_ID_SHAPE = re.compile(r"^\d{2}(?:\.\d+)?-\d{2}$")


def test_every_pending_entry_names_a_plan_id_of_the_right_shape() -> None:
    """An owner field reading `someone will do it` is not an owner.

    A PENDING entry is a promise that a NAMED plan moves this id. An owner field carrying prose,
    a phase number without a plan, or a typo is a promise nobody can be held to, and the whole
    ledger is only worth what its weakest entry promises.
    """
    malformed = sorted(
        (path, literal, owner)
        for path, literal, status, owner in coexistence.COEXISTENCE_IDS
        if status == coexistence.PENDING and not _PLAN_ID_SHAPE.match(owner)
    )

    assert malformed == []


def test_every_carve_out_carries_a_non_empty_reason() -> None:
    """A carve-out without its reason is indistinguishable from an id somebody forgot."""
    reasonless = sorted(
        (path, literal)
        for path, literal, reason in coexistence.COEXISTENCE_CARVE_OUTS
        if not reason.strip()
    )

    assert reasonless == []
    assert coexistence.COEXISTENCE_CARVE_OUTS, "the carve-out record is empty"


def test_the_ledger_and_the_carve_out_record_are_disjoint() -> None:
    """One id cannot be both something that moves and something that deliberately does not.

    The sharpest case is the file browser: its systemd UNIT NAME is in the ledger, because a unit
    name lands in a shared namespace, while its service USER and GROUP are carve-outs, because
    they are OS accounts on a provisioned server. Two nearly identical strings, opposite
    dispositions -- exactly the pair a later editor would collapse by accident.
    """
    carved = {(path, literal) for path, literal, _ in coexistence.COEXISTENCE_CARVE_OUTS}
    overlap = sorted(
        (path, literal)
        for path, literal, _, _ in coexistence.COEXISTENCE_IDS
        if (path, literal) in carved
    )

    assert overlap == []


def test_the_hosted_only_identities_are_carve_outs_and_never_ledger_entries() -> None:
    """P33-21 and the port-18789 measurement, asserted rather than remembered."""
    carved = "\n".join(literal for _, literal, _ in coexistence.COEXISTENCE_CARVE_OUTS)
    ledger = "\n".join(literal for _, literal, _, _ in coexistence.COEXISTENCE_IDS)

    for fenced in ('HOSTED_RUNTIME_HOME = "/home/clawdi"', 'HOSTED_RUNTIME_USER = "clawdi"'):
        assert fenced in carved
        assert fenced not in ledger
    for uid_or_gid in ("HOSTED_RUNTIME_UID = 10_001", "HOSTED_RUNTIME_GID = 10_001"):
        assert uid_or_gid in carved
    assert "/etc/clawdi" in carved
    assert "/var/lib/clawdi" in carved
    assert "18789" in carved
    assert "18789" not in ledger


# Keyed by (path, literal), never by path alone. `serve/installer.ts` carries TWO entries with
# OPPOSITE dispositions -- the state root, moved here by 03.3-02, and the user-scope daemon unit
# filename, still PENDING for 03.3-05 -- so a path-keyed lookup silently reads whichever came
# last. That is the same collapse-by-accident hazard the disjointness test's docstring names for
# the file browser, and it is why these are pinned as pairs.
_STATE_ROOT_RESOLVERS: tuple[tuple[str, str], ...] = (
    ("packages/cli/src/lib/config.ts", 'join(process.env.HOME || homedir(), ".clawdi")'),
    ("packages/cli/src/serve/paths.ts", 'return join(home, ".clawdi");'),
    ("packages/cli/src/serve/installer.ts", 'return join(home(), ".clawdi");'),
    ("packages/cli/src/share/tokens.ts", 'return join(userHome(), ".clawdi");'),
)


def test_the_collapsed_state_root_resolvers_are_recorded_moved() -> None:
    """Plan 03.3-02's collapse, asserted here and not only by the guard.

    The guard fails when the tree and the ledger disagree. It cannot fail when BOTH regress
    together -- a later change that reintroduced a legacy read AND flipped the entry back to
    PENDING to make the guard green again would be a clean run and a shared credential tree. This
    test is the assertion the guard cannot make about itself: these four resolvers are MOVED, full
    stop, and turning one back is a test failure regardless of what the tree says.
    """
    by_id = {(path, literal): status for path, literal, status, _ in coexistence.COEXISTENCE_IDS}

    for entry in _STATE_ROOT_RESOLVERS:
        assert entry in by_id, f"{entry[0]} lost its state-root ledger entry"
        assert by_id[entry] == coexistence.MOVED, f"{entry[0]} must stay MOVED"


def test_the_state_root_authority_is_pinned_against_a_legacy_fallback() -> None:
    """P33-04 / T-03.3-05: the resolver is unconditional, and that is checked rather than argued.

    A compatibility read reintroduced in `state-root.ts` is the single edit that would point this
    fork and upstream clawdi at one credential tree. One line, in one file, with no other guard
    over it -- so the ledger carries a MOVED entry naming exactly that file and literal.
    """
    entries = [
        (literal, status)
        for path, literal, status, _ in coexistence.COEXISTENCE_IDS
        if path == "packages/cli/src/lib/state-root.ts"
    ]

    assert entries == [('".clawdi"', coexistence.MOVED)]


def test_the_hosted_user_state_tree_is_a_carve_out_not_a_pending_move() -> None:
    """The fifth resolver 03.3-01 recorded, reclassified by 03.3-02 rather than flipped.

    Asserted in both registers on purpose. Present in the carve-outs is the decision; ABSENT from
    the ledger is what stops a later reader restoring it as PENDING and re-promising a move that
    P33-21 forbids -- the promise would then be unkeepable, and the guard would fail for a reason
    that is nobody's bug.
    """
    hosted_branch = 'process.env.CLAWDI_HOME || join(userHome, ".clawdi")'
    carved = {(path, literal) for path, literal, _ in coexistence.COEXISTENCE_CARVE_OUTS}
    ledger = {(path, literal) for path, literal, _, _ in coexistence.COEXISTENCE_IDS}

    assert ("packages/cli/src/runtime/paths.ts", hosted_branch) in carved
    assert ("packages/cli/src/runtime/paths.ts", hosted_branch) not in ledger


def test_the_failure_report_is_sorted_by_path_then_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two failing runs against one tree must diff cleanly, showing only real changes.

    Declared in reverse order on purpose: if the guard reported in declaration order this would
    pass by coincidence, and the property under test is that it does not.
    """
    monkeypatch.setattr(
        coexistence,
        "COEXISTENCE_IDS",
        (
            ("zzz/last.ts", 'name: "clawdi",', coexistence.MOVED, "03.3-01"),
            ("aaa/first.ts", 'name: "clawdi",', coexistence.MOVED, "03.3-01"),
        ),
    )
    contents = {"zzz/last.ts": 'name: "clawdi",', "aaa/first.ts": 'name: "clawdi",'}

    with pytest.raises(coexistence.CoexistenceError) as failure:
        coexistence.validate_coexistence(contents=contents)

    message = str(failure.value)
    assert message.index("aaa/first.ts") < message.index("zzz/last.ts")


def test_every_ledger_entry_names_an_owning_plan() -> None:
    """An id with no owner is an id nobody moves."""
    ownerless = [
        (path, literal)
        for path, literal, _, owner in coexistence.COEXISTENCE_IDS
        if not owner.strip()
    ]

    assert ownerless == []


# ---------------------------------------------------------------------------
# The P33-02 scope fence, asserted structurally rather than by a comment.
# ---------------------------------------------------------------------------
# Widening this ledger onto the C-1/C-4..C-7 wire carve-outs would reopen 03.1's D-01 under a
# coexistence label. The three checks below make that breach fail a test rather than pass review.

_SCHEMA_SHAPE = re.compile(carveouts._SCHEMA_IDENTIFIER_SHAPE)
_API_KEY_PREFIX = next(
    match.group(1)
    for _, literal in carveouts.CARVE_OUTS
    if (match := re.search(r'API_KEY_PREFIX = "([^"]+)"', literal))
)


def test_no_ledger_entry_reaches_into_the_wire_protocol_schemas() -> None:
    """``backend/app/schemas`` is where the C-4 discriminators live; none is machine-local."""
    trespassing = [
        (path, literal)
        for path, literal, _, _ in coexistence.COEXISTENCE_IDS
        if path.startswith("backend/app/schemas")
    ]

    assert trespassing == []


def test_no_ledger_literal_carries_a_wire_protocol_carve_out() -> None:
    """The API-key prefix, the ``X-Clawdi-*`` headers and the ``clawdi.<name>.vN`` shape.

    Each is a name a matched client and its own backend share, so neither product can collide
    with the other over it on a shared machine -- and each is already pinned as PRESENT by
    ``check_rebrand_carveouts.py``. An entry asserting one is ABSENT would put the two guards in
    direct contradiction, and the first one to run would decide which released clients break.
    """
    breaches = [
        (path, literal)
        for path, literal, _, _ in coexistence.COEXISTENCE_IDS
        if _API_KEY_PREFIX in literal or "X-Clawdi-" in literal or _SCHEMA_SHAPE.search(literal)
    ]

    assert breaches == []


def test_the_two_guards_never_pin_the_same_path_and_literal() -> None:
    """One literal cannot be required present by one guard and absent by the other."""
    carved = {(path, literal) for path, literal in carveouts.CARVE_OUTS}
    overlap = sorted(
        (path, literal)
        for path, literal, _, _ in coexistence.COEXISTENCE_IDS
        if (path, literal) in carved
    )

    assert overlap == []


def test_the_backend_mcp_route_is_not_in_the_coexistence_ledger() -> None:
    """``MCP_ENDPOINT_PATH`` is the sibling that deliberately did NOT move (T-03.3-04).

    Its server-name neighbour moved in 03.3-01 because it lands in a third-party config on a
    user's machine. The route does not: it is called by this CLI on its own backend.
    """
    assert any(
        literal.startswith("const MCP_ENDPOINT_PATH") for _, literal in carveouts.CARVE_OUTS
    ), "the backend MCP route is no longer pinned as a carve-out"
    assert not any(
        "MCP_ENDPOINT_PATH" in literal for _, literal, _, _ in coexistence.COEXISTENCE_IDS
    )


def test_the_coexistence_guard_is_wired_into_the_canonical_test_run() -> None:
    """Asserting the wiring is what keeps a later phase from removing the guard as cleanup.

    ``scripts/test.sh`` runs it host-side beside the existing carve-out and residual guards,
    ahead of the compose build, so it gates the canonical run rather than only a hand-run command.
    Comments are stripped first: a mention in a comment is not an invocation.
    """
    runner = (_REPO_ROOT / "scripts/test.sh").read_text(encoding="utf-8")
    executable = [line for line in runner.splitlines() if not line.strip().startswith("#")]

    assert any("scripts/check_rebrand_coexistence.py" in line for line in executable)


# ---------------------------------------------------------------------------
# Plan 03.3-14 -- the phase's completion claim, made falsifiable.
# ---------------------------------------------------------------------------
#
# Through phase 03.3 a ``PENDING`` row was a LEGAL state: it recorded a machine-local id whose
# move an in-flight plan owned, and the guard's job was to assert the id was still spelled the
# old way until that plan landed. The phase is now closing, so the same row means the opposite
# thing -- a move that was promised and did not happen -- and the guard must say so rather than
# leaving it to be read off a success line nobody diffs.
#
# The refusal is a SEPARATE assertion rather than a new branch inside ``validate_coexistence``,
# and that separation is load-bearing rather than stylistic. ``validate_coexistence`` is what
# proves the ledger in BOTH directions, and the PENDING direction is the half that stops the
# ledger rotting into a memory of the tree (see ``test_a_pending_id_that_has_already_moved_is_
# red``). Folding a zero-pending refusal into it would make every one of those directional cases
# unreachable -- the guard would reject the synthetic ledger before it ever asserted a direction
# on it -- which is weakening the guard in the very act of tightening it, and is what this plan's
# MIGR-01 prohibition forbids. The two assertions answer two different questions: "does the
# ledger describe the tree" and "does the ledger still promise anything".


def test_a_pending_entry_is_now_refused_outright(monkeypatch: pytest.MonkeyPatch) -> None:
    """Red. After this phase a PENDING row is a defect, not a legal in-flight state."""
    monkeypatch.setattr(coexistence, "COEXISTENCE_IDS", (_entry(coexistence.PENDING),))

    with pytest.raises(coexistence.CoexistenceError, match="pending"):
        coexistence.assert_no_pending_entries()


def test_the_pending_refusal_names_every_offending_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A count alone is not actionable: the reader must learn which rows, and whose plan."""
    ledger = (
        _entry(coexistence.MOVED),
        _entry(coexistence.PENDING, path="packages/cli/src/a.ts", literal='id: "clawdi-a"'),
        _entry(coexistence.PENDING, path="packages/cli/src/b.ts", literal='id: "clawdi-b"'),
    )
    monkeypatch.setattr(coexistence, "COEXISTENCE_IDS", ledger)

    with pytest.raises(coexistence.CoexistenceError) as caught:
        coexistence.assert_no_pending_entries()

    message = str(caught.value)
    assert "packages/cli/src/a.ts" in message
    assert "packages/cli/src/b.ts" in message
    assert "03.3-01" in message


def test_a_ledger_with_no_pending_entry_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Green. The state the phase claims, asserted rather than read off a success line."""
    monkeypatch.setattr(coexistence, "COEXISTENCE_IDS", (_entry(coexistence.MOVED),))

    assert coexistence.assert_no_pending_entries() == 0


def test_the_real_ledger_carries_no_pending_entry() -> None:
    """The phase's own completion claim, machine-checked against the committed ledger.

    This is the assertion that makes the claim falsifiable. Every earlier plan in the phase could
    leave a row PENDING and still report a green guard; from here a row left PENDING fails the
    canonical suite, and the failure names the plan that owed the move.
    """
    pending = [
        (path, literal, owner)
        for path, literal, status, owner in coexistence.COEXISTENCE_IDS
        if status == coexistence.PENDING
    ]

    assert pending == []
    assert coexistence.assert_no_pending_entries() == 0


def test_the_pending_refusal_leaves_both_directional_assertions_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prohibition this plan carries: no fence comes down by widening what a guard tolerates.

    ``validate_coexistence`` must STILL accept a PENDING row and assert the PENDING direction on
    it. If a future edit moves the refusal into that function, the directional cases above stop
    exercising a direction and start exercising the refusal; this fails first, with the reason.
    """
    monkeypatch.setattr(coexistence, "COEXISTENCE_IDS", (_entry(coexistence.PENDING),))

    assert coexistence.validate_coexistence(contents={_MCP_SERVER: 'name: "clawdi",'}) == 1

    with pytest.raises(coexistence.CoexistenceError, match="flip the entry to MOVED"):
        coexistence.validate_coexistence(contents={_MCP_SERVER: 'name: "iforge",'})


def test_the_guard_exits_non_zero_on_a_ledger_that_still_promises_a_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through ``main``: the canonical suite runs the script, not the function."""
    monkeypatch.setattr(coexistence, "COEXISTENCE_IDS", (_entry(coexistence.PENDING),))

    with pytest.raises(SystemExit) as caught:
        coexistence.main()

    assert "rebrand coexistence failed:" in str(caught.value)
