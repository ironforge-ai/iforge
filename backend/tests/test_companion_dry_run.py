"""The read-then-diff dry run (TOOL-05), asserted from its NEGATIVE edge first.

D-11 chose read-then-diff over a request-shaped preview for one reason, and it is the reason the
first test in this file is the first test in this file: a preview that never reads the target
renders a delete of a nonexistent path identically to a delete of a real one, so the preview
carries no information at all. A suite that opens with the happy path ends up asserting that a
preview was PRODUCED rather than that it was INFORMED, and those two are indistinguishable in
green.

**What each group proves.**

* *Negative preview.* A probe that 404s, and a probe that 403s, are error results carrying an
  actionable message -- never a clean diff. The two are kept apart because "there is nothing
  there" and "you may not look" are different answers and a preview that flattens them is lying
  about which.
* *Writes nothing.* Parameterized over every mutating verb drawn from ``COMPANION_VERBS`` filtered
  by class, rather than a hand-written list, so a verb added later is covered without a new test.
  The assertion filters the SHARED stub's recorded requests by mutating method -- the stub records
  real bytes on a real socket, so this is evidence rather than a mock's self-report.
* *Informed before-state.* The before-state must carry a field the stub supplies and the request
  does not (``size``, ``modified``). A preview that echoed its own arguments back would pass a
  shape assertion and fail this one.
* *Partial diffs.* A diff the façade could only partly compute states what it could not determine.
  The natural case is real: a one-level listing cannot see inside a subdirectory, so a recursive
  delete's after-state is genuinely unknown below that line.
* *Schema conformance.* The diff goes out through the structured-content channel against
  ``DRY_RUN_OUTPUT_SCHEMA``, and a malformed diff is an error rather than a value the agent reads
  as truth.
* *Budget.* A dry run debits ``calls`` and leaves ``records`` and ``bytes`` alone; a preview that
  spent the budget of the mutation it previews would make previewing a cost the agent avoids.
* *Redemption.* The count a dry run RESOLVED is what the subsequent real call's ``records`` check
  consumes (T-5-34), asserted end to end against the database, so an approval for a two-entry
  delete cannot be redeemed against a two-thousand-entry one.

The module runs on the committed lane for the same reason ``test_companion_blast_radius.py`` does:
the redemption case needs an executor stand-in writing its result on an INDEPENDENT connection
while the façade polls, and that is invisible to any test whose writes never leave one
transaction.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.auth import AuthContext
from app.core.companion_registry import COMPANION_VERBS, companion_verb
from app.models.companion_dispatch import CompanionDispatchCall
from app.models.companion_session_budget import CompanionCapPolicy, CompanionSessionBudget
from app.models.companion_tool_policy import CompanionDestroyEnablement, CompanionToolPolicy
from app.schemas.companions import DRY_RUN_OUTPUT_SCHEMA, validate_dry_run_output
from app.services.companion_dispatch import canonical_action_hash
from app.services.companion_dryrun import (
    DRY_RUN_GUARANTEE,
    MUTATING_TOOL_CLASSES,
    DryRunResult,
    ProbeResponse,
    compute_dry_run,
    reset_preview_ledger,
)
from app.services.companion_facade import (
    _COMPANION_INPUT_SCHEMA,
    _input_schema_for,
    _single_path_target_count,
    _tool_definition,
    call_companion_tool,
    list_companion_tools,
    reset_companion_tools_cache,
)
from tests.companion_stub import (
    COMPANION_STUB_FORBIDDEN_PATH,
    COMPANION_STUB_LISTING,
    COMPANION_STUB_MISSING_PATH,
    CompanionStub,
)
from tests.hosted_runtime_fixtures import deploy_companion_state

pytestmark = pytest.mark.committed_db

READ_TOOL = "filebrowser__list_dir"
DELETE_TOOL = "filebrowser__delete_path"
MOVE_TOOL = "filebrowser__move_path"

# Drawn from the verb table rather than written out, so a mutating verb added later is covered by
# every parameterized assertion below without anyone remembering to extend a list.
MUTATING_TOOLS = [
    name
    for name, entry in sorted(COMPANION_VERBS.items())
    if entry.tool_class in MUTATING_TOOL_CLASSES
]

# Every mutating verb's dry run is EXECUTED against its kind's shared stub, with arguments its
# kind's argument contract accepts (06-07 closed WINDOWS #201, which had narrowed this to the
# filebrowser verbs while `compute_dry_run` was path-shaped). A kind with mutating verbs and no
# entry here fails `test_every_mutating_kind_has_a_preview_case` rather than dropping out.
PREVIEW_ARGUMENTS_BY_KIND: dict[str, dict[str, object]] = {
    "filebrowser": {"path": "/"},
    "postiz": {
        "type": "schedule",
        "date": "2026-10-01T12:00:00Z",
        "shortLink": False,
        "tags": [],
        "posts": [
            {"integration": {"id": "int-bluesky-1"}, "value": [{"content": "We ship today"}]}
        ],
    },
}
STUB_EXECUTABLE_MUTATING_TOOLS = MUTATING_TOOLS

# The mutating HTTP methods. A dry run that issued any one of these would be the attack the preview
# exists to prevent, so this is the filter every writes-nothing assertion applies.
MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


@pytest.fixture(autouse=True)
def _clear_companion_caches():
    reset_companion_tools_cache()
    reset_preview_ledger()
    yield
    reset_companion_tools_cache()
    reset_preview_ledger()


def _stub_probe(stub: CompanionStub):
    """A probe executor that issues the REAL request the registry declares for the probe verb.

    Injected rather than mocked: the whole writes-nothing claim is about the method that lands on
    the wire, and a mock would only report the method the test asked it to expect.
    """

    async def execute(probe_tool: str, probe_arguments: dict[str, object]) -> ProbeResponse:
        entry = companion_verb(probe_tool)
        async with httpx.AsyncClient(base_url=stub.base_url) as companion:
            response = await companion.request(
                entry.method,
                entry.path_template,
                params=probe_arguments,
                headers={"X-JWT-Assertion": "stand-in"},
            )
        return ProbeResponse(status=response.status_code, body=response.text)

    return execute


def _canned_probe(*, status: int = 200, body: object = None, error: str | None = None):
    """A probe executor with a fixed answer, for the cases a stub route cannot express."""

    async def execute(probe_tool: str, probe_arguments: dict[str, object]) -> ProbeResponse:
        del probe_tool, probe_arguments
        payload = COMPANION_STUB_LISTING if body is None else body
        return ProbeResponse(
            status=status,
            body="" if error is not None else json.dumps(payload),
            error=error,
        )

    return execute


# ---------------------------------------------------------------------------
# The negative preview. FIRST, deliberately: this is the assertion D-11 exists for.
# ---------------------------------------------------------------------------


async def test_a_dry_run_against_a_nonexistent_target_is_an_error_not_a_clean_diff(
    companion_stub,
) -> None:
    """An impossible mutation and a valid one must never produce the same preview.

    This is the whole acceptance test for TOOL-05's edge-probe row. The error FLAG is asserted, not
    merely that some result came back -- a preview rendered as a clean diff with an empty
    before-state would satisfy "a result was returned" while being exactly the failure mode.
    """
    result = await compute_dry_run(
        DELETE_TOOL,
        companion_verb(DELETE_TOOL),
        {"path": COMPANION_STUB_MISSING_PATH},
        execute_probe=_stub_probe(companion_stub),
    )

    assert result.is_error is True
    assert result.message is not None
    assert COMPANION_STUB_MISSING_PATH in result.message
    # The reason is NAMED. "The preview failed" is not something an agent can self-correct from.
    assert "404" in result.message or "not found" in result.message.lower()
    assert result.before == []
    assert result.after == []


async def test_a_probe_refused_for_permission_is_a_different_error_than_a_missing_target(
    companion_stub,
) -> None:
    """ "Nothing is there" and "you may not look" are different answers.

    A preview that returned the same message for both would send the agent to fix the wrong thing.
    """
    result = await compute_dry_run(
        DELETE_TOOL,
        companion_verb(DELETE_TOOL),
        {"path": COMPANION_STUB_FORBIDDEN_PATH},
        execute_probe=_stub_probe(companion_stub),
    )

    assert result.is_error is True
    assert result.message is not None
    assert "403" in result.message or "permission" in result.message.lower()


async def test_a_probe_that_never_answered_is_an_error_rather_than_an_empty_preview(
    companion_stub,
) -> None:
    """A transport failure is not evidence that the target is empty."""
    result = await compute_dry_run(
        DELETE_TOOL,
        companion_verb(DELETE_TOOL),
        {"path": "/"},
        execute_probe=_canned_probe(error="no executor claimed the read probe"),
    )

    assert result.is_error is True
    assert result.message is not None
    assert "no executor claimed the read probe" in result.message


# ---------------------------------------------------------------------------
# A dry run writes nothing.
# ---------------------------------------------------------------------------


def test_every_mutating_kind_has_a_preview_case() -> None:
    kinds = {name.partition("__")[0] for name in MUTATING_TOOLS}

    assert kinds <= set(PREVIEW_ARGUMENTS_BY_KIND)


def _kind_stub_probe(tool_name: str, companion_stub, postiz_stub):
    """The real-request probe executor for this tool's kind, and the stub it records into."""
    if tool_name.startswith("postiz__"):

        async def execute(probe_tool: str, probe_arguments: dict[str, object]) -> ProbeResponse:
            entry = companion_verb(probe_tool)
            async with httpx.AsyncClient(base_url=postiz_stub.base_url) as companion:
                response = await companion.request(
                    entry.method,
                    entry.path_template,
                    params=probe_arguments,
                    headers={"Authorization": postiz_stub.api_key},
                )
            return ProbeResponse(status=response.status_code, body=response.text)

        return execute, postiz_stub
    return _stub_probe(companion_stub), companion_stub


@pytest.mark.parametrize("tool_name", STUB_EXECUTABLE_MUTATING_TOOLS)
async def test_a_dry_run_issues_no_mutating_request_against_the_companion(
    companion_stub, postiz_stub, tool_name: str
) -> None:
    """Zero POST, PATCH, PUT or DELETE requests for the WHOLE dry-run path, per mutating verb.

    Parameterized from the verb table so a verb added later cannot slip past this by not being on
    somebody's list. The positive half matters too: at least one GET must have landed, or the
    preview never read anything and the assertion would be green against a no-op.
    """
    execute_probe, stub = _kind_stub_probe(tool_name, companion_stub, postiz_stub)
    result = await compute_dry_run(
        tool_name,
        companion_verb(tool_name),
        PREVIEW_ARGUMENTS_BY_KIND[tool_name.partition("__")[0]],
        execute_probe=execute_probe,
    )

    assert result.is_error is False, result.message
    mutating = [recorded for recorded in stub.requests if recorded.method in MUTATING_METHODS]
    assert mutating == []
    assert [recorded.method for recorded in stub.requests] == ["GET"]


async def test_the_dry_run_probe_is_a_read_class_verb_of_the_same_kind(companion_stub) -> None:
    """The probe resolved is the parser-enforced one (D-13), not a second mutation (T-5-13)."""
    for tool_name in MUTATING_TOOLS:
        entry = companion_verb(tool_name)
        assert entry.read_probe is not None, tool_name
        kind = tool_name.partition("__")[0]
        probe = companion_verb(f"{kind}__{entry.read_probe}")
        assert probe.tool_class == "read", tool_name
        assert probe.method == "GET", tool_name


# ---------------------------------------------------------------------------
# The before-state has to come from a read.
# ---------------------------------------------------------------------------


async def test_the_before_state_carries_a_field_only_the_companion_could_supply(
    companion_stub,
) -> None:
    """A preview that merely echoed the request back cannot pass this.

    ``size`` and ``modified`` are in the stub's listing and in no argument this façade accepts --
    the input schema admits ``path`` and ``dry_run`` and nothing else. Asserting on them is what
    separates a read-then-diff preview from a rendered intention.
    """
    result = await compute_dry_run(
        DELETE_TOOL,
        companion_verb(DELETE_TOOL),
        {"path": "/"},
        execute_probe=_stub_probe(companion_stub),
    )

    assert result.is_error is False, result.message
    assert result.before, "a preview with no before-state read nothing"
    names = {entry["name"] for entry in result.before}
    assert names == {"notes.md", "projects"}
    notes = next(entry for entry in result.before if entry["name"] == "notes.md")
    assert notes["size"] == 128
    assert notes["modified"] == "2026-03-01T10:15:00Z"


async def test_a_destroy_preview_removes_the_read_entries_from_the_after_state(
    companion_stub,
) -> None:
    result = await compute_dry_run(
        DELETE_TOOL,
        companion_verb(DELETE_TOOL),
        {"path": "/"},
        execute_probe=_stub_probe(companion_stub),
    )

    assert result.is_error is False, result.message
    assert result.after == []
    assert result.target_count == len(COMPANION_STUB_LISTING["items"])  # pyright: ignore[reportArgumentType]


# ---------------------------------------------------------------------------
# A diff that could only be partly computed says so.
# ---------------------------------------------------------------------------


async def test_a_partly_computable_diff_states_what_it_could_not_determine(
    companion_stub,
) -> None:
    """A one-level listing cannot see inside a subdirectory, and must not pretend it did.

    The stub's listing carries a ``projects`` directory. A recursive delete's after-state below
    that line is genuinely unknown, so the result carries the entries it DID compute plus an
    explicit statement of what it could not -- never a complete-looking diff.
    """
    result = await compute_dry_run(
        DELETE_TOOL,
        companion_verb(DELETE_TOOL),
        {"path": "/"},
        execute_probe=_stub_probe(companion_stub),
    )

    assert result.is_error is False, result.message
    assert result.undetermined, "a diff over an unlisted subtree claimed to be complete"
    assert any("projects" in item for item in result.undetermined)
    # The computed half survives alongside the disclosure rather than being discarded.
    assert result.before


async def test_a_diff_with_nothing_left_undetermined_says_nothing(companion_stub) -> None:
    """The contrast case. Without it, an implementation that always discloses would pass above."""
    flat = {
        "path": "/flat",
        "name": "flat",
        "type": "directory",
        "items": [{"name": "a.txt", "size": 4, "type": "text", "modified": "2026-01-01T00:00:00Z"}],
    }

    result = await compute_dry_run(
        DELETE_TOOL,
        companion_verb(DELETE_TOOL),
        {"path": "/flat"},
        execute_probe=_canned_probe(body=flat),
    )

    assert result.is_error is False, result.message
    assert result.undetermined == []


async def test_a_move_preview_with_no_destination_declares_it_undetermined(companion_stub) -> None:
    """A move whose arguments carry no destination cannot have a complete after-state.

    Reachable only from OUTSIDE the tool path now (05-13): ``destination`` is required by
    ``filebrowser__move_path``'s input schema and refused at argument validation when absent, so a
    call that arrives here without one did not come through ``call_companion_tool``. Stating the
    gap is still the honest answer for that caller. Inventing a destination would make the preview
    a fiction the agent would then act on.
    """
    result = await compute_dry_run(
        MOVE_TOOL,
        companion_verb(MOVE_TOOL),
        {"path": "/"},
        execute_probe=_stub_probe(companion_stub),
    )

    assert result.is_error is False, result.message
    assert any("destination" in item.lower() for item in result.undetermined)


async def test_a_move_preview_names_its_destination_in_the_after_state(companion_stub) -> None:
    """WR-06a. A move that CAN state its destination previews the real before and after.

    Every entry the probe listed under ``path`` appears under ``destination`` with its basename
    preserved and the ``size``/``modified`` it was READ with carried through -- neither of which is
    an argument this façade accepts, so an echo of the request cannot produce them. The original
    location is emptied, which is the other half of a move.
    """
    result = await compute_dry_run(
        MOVE_TOOL,
        companion_verb(MOVE_TOOL),
        {"path": "/", "destination": "/archive"},
        execute_probe=_stub_probe(companion_stub),
    )

    assert result.is_error is False, result.message
    # The blanket "where these entries would appear could not be determined" sentence is GONE.
    assert not any("destination" in item.lower() for item in result.undetermined)
    after_paths = {str(entry["path"]) for entry in result.after}
    assert after_paths == {"/archive/notes.md", "/archive/projects"}
    moved = next(entry for entry in result.after if entry["name"] == "notes.md")
    assert moved["size"] == 128
    assert moved["modified"] == "2026-03-01T10:15:00Z"
    # The one-level probe limit is REAL and is still disclosed.
    assert any("projects" in item for item in result.undetermined)
    # And the diff still conforms to the declared output schema.
    assert validate_dry_run_output(result.structured()) is None


async def test_only_a_mutate_verb_carries_a_destination_property() -> None:
    """WR-06a, the schema half. A ``mutate`` verb RELOCATES its target; no other class does.

    Iterated over the whole verb table so the complement is asserted too: a ``create`` verb that
    grew a ``destination`` would be an argument nothing reads, and a ``destroy`` verb that grew one
    would describe a second address a delete does not have.
    """
    for tool_name, entry in sorted(COMPANION_VERBS.items()):
        schema = _input_schema_for(entry)
        properties = schema["properties"]
        assert isinstance(properties, dict)
        if entry.tool_class == "mutate":
            assert properties["destination"]["type"] == "string"  # pyright: ignore[reportIndexIssue]
            assert "destination" in schema["required"], tool_name
        else:
            assert "destination" not in properties, tool_name
            assert "destination" not in schema["required"], tool_name


def test_injecting_destination_does_not_mutate_the_shared_required_list() -> None:
    """``_input_schema_for`` shallow-copies the module schema, whose ``required`` is SHARED.

    Appending to that list in the mutate branch would corrupt the module-level schema for every
    other verb on the first call -- and the corruption would be invisible until a read verb
    started demanding a destination.
    """
    _input_schema_for(COMPANION_VERBS[MOVE_TOOL])

    assert _COMPANION_INPUT_SCHEMA["required"] == ["path"]
    assert "destination" not in _COMPANION_INPUT_SCHEMA["properties"]
    assert "destination" not in _input_schema_for(COMPANION_VERBS[READ_TOOL])["properties"]


def test_two_destinations_produce_different_canonical_action_digests() -> None:
    """An approval granted for one destination must not authorize another (T-05-13-02).

    ``canonical_action_hash`` covers the whole argument map, so this holds with no code change --
    but "holds by construction" is exactly the kind of claim that stops holding silently when the
    argument map is filtered somewhere on the way in.
    """
    tenant = "tenant-1"
    first = canonical_action_hash(
        companion_key="filebrowser",
        verb="move_path",
        arguments={"path": "/notes.md", "destination": "/archive/notes.md"},
        tenant_id=tenant,
    )
    second = canonical_action_hash(
        companion_key="filebrowser",
        verb="move_path",
        arguments={"path": "/notes.md", "destination": "/elsewhere/notes.md"},
        tenant_id=tenant,
    )

    assert first != second


# ---------------------------------------------------------------------------
# The structured-content channel and its schema.
# ---------------------------------------------------------------------------


async def test_the_structured_diff_validates_against_the_declared_output_schema(
    companion_stub,
) -> None:
    result = await compute_dry_run(
        DELETE_TOOL,
        companion_verb(DELETE_TOOL),
        {"path": "/"},
        execute_probe=_stub_probe(companion_stub),
    )

    structured = result.structured()

    assert validate_dry_run_output(structured) is None
    assert structured["tool"] == DELETE_TOOL
    assert structured["toolClass"] == "destroy"
    assert structured["targetCount"] == 2
    # The prohibition, carried IN the payload rather than in a docstring nobody renders: a preview
    # is a point-in-time read, never a guarantee of the eventual outcome.
    assert structured["guarantee"] == DRY_RUN_GUARANTEE


def test_a_malformed_diff_is_rejected_rather_than_returned() -> None:
    """A diff that does not validate is an ERROR, not a value the agent reads as truth."""
    malformed: dict[str, object] = {
        "tool": DELETE_TOOL,
        "toolClass": "destroy",
        "target": "/",
        "targetCount": "two",  # the schema says integer
        "before": [],
        "after": [],
        "undetermined": [],
        "guarantee": DRY_RUN_GUARANTEE,
    }

    problem = validate_dry_run_output(malformed)

    assert problem is not None
    assert "targetCount" in problem


def test_a_diff_missing_a_required_key_is_rejected() -> None:
    incomplete: dict[str, object] = {"tool": DELETE_TOOL, "toolClass": "destroy"}

    problem = validate_dry_run_output(incomplete)

    assert problem is not None


def test_a_diff_carrying_an_undeclared_key_is_rejected() -> None:
    """Extras are forbidden here for the same reason they are on the way in."""
    extra: dict[str, object] = {
        "tool": DELETE_TOOL,
        "toolClass": "destroy",
        "target": "/",
        "targetCount": 0,
        "before": [],
        "after": [],
        "undetermined": [],
        "guarantee": DRY_RUN_GUARANTEE,
        "confidence": "high",
    }

    problem = validate_dry_run_output(extra)

    assert problem is not None
    assert "confidence" in problem


def test_the_output_schema_is_a_closed_object() -> None:
    assert DRY_RUN_OUTPUT_SCHEMA["type"] == "object"
    assert DRY_RUN_OUTPUT_SCHEMA["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Two identical dry runs against unchanged state are identical.
# ---------------------------------------------------------------------------


async def test_two_identical_dry_runs_against_unchanged_state_produce_equal_diffs(
    companion_stub,
) -> None:
    """A preview whose output moved on its own could not be used to approve anything."""
    first = await compute_dry_run(
        DELETE_TOOL,
        companion_verb(DELETE_TOOL),
        {"path": "/"},
        execute_probe=_stub_probe(companion_stub),
    )
    second = await compute_dry_run(
        DELETE_TOOL,
        companion_verb(DELETE_TOOL),
        {"path": "/"},
        execute_probe=_stub_probe(companion_stub),
    )

    assert first.structured() == second.structured()
    assert isinstance(first, DryRunResult)


# ---------------------------------------------------------------------------
# The injected `dry_run` property: on every mutating verb, and on nothing else.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_name", sorted(COMPANION_VERBS))
def test_only_mutating_verbs_carry_a_dry_run_property(tool_name: str) -> None:
    """D-12's per-call parameter, injected by class and by nothing else.

    Iterated over the whole verb table so the complement is asserted too: a ``read`` verb that
    grew a ``dry_run`` property would be a silent no-op parameter an agent could set forever.
    """
    entry = COMPANION_VERBS[tool_name]
    definition = _tool_definition(tool_name, entry)
    schema = definition["inputSchema"]
    assert isinstance(schema, dict)
    properties = schema["properties"]
    assert isinstance(properties, dict)

    if entry.tool_class in MUTATING_TOOL_CLASSES:
        assert properties["dry_run"]["type"] == "boolean"  # pyright: ignore[reportIndexIssue]
        assert definition["outputSchema"] == DRY_RUN_OUTPUT_SCHEMA
    else:
        assert "dry_run" not in properties
        assert "outputSchema" not in definition

    # Unchanged either way: extras stay forbidden, which is what makes a `dry_run` on a read verb
    # a validation REJECTION rather than an argument quietly ignored.
    assert schema["additionalProperties"] is False


async def test_a_dry_run_on_a_read_verb_is_rejected_by_argument_validation(
    db_session, seed_user, companion_stub
) -> None:
    await deploy_companion_state(db_session, seed_user, machine_prefix="dryread")
    auth = AuthContext(user=seed_user)

    result = await call_companion_tool(
        READ_TOOL,
        {"path": "/", "dry_run": True},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
    )

    assert result["isError"] is True
    assert "dry_run" in result["content"][0]["text"]
    assert companion_stub.requests == []


async def test_a_non_boolean_dry_run_is_rejected(db_session, seed_user, companion_stub) -> None:
    await deploy_companion_state(db_session, seed_user, machine_prefix="drytype")
    await _enable_destroy(db_session, seed_user)
    await _allow(db_session, seed_user, "delete_path")
    auth = AuthContext(user=seed_user)

    result = await call_companion_tool(
        DELETE_TOOL,
        {"path": "/", "dry_run": "yes"},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
    )

    assert result["isError"] is True
    assert "dry_run" in result["content"][0]["text"]


async def test_the_listed_toolset_carries_the_injected_property(db_session, seed_user) -> None:
    """The property has to be on the wire, not merely in a helper the façade calls."""
    await deploy_companion_state(db_session, seed_user, machine_prefix="drylist")
    await _enable_destroy(db_session, seed_user)
    auth = AuthContext(user=seed_user)

    tools = await list_companion_tools(auth, db_session)

    by_name = {str(tool["name"]): tool for tool in tools}
    assert DELETE_TOOL in by_name
    assert "dry_run" in by_name[DELETE_TOOL]["inputSchema"]["properties"]  # pyright: ignore[reportIndexIssue]
    assert "dry_run" not in by_name[READ_TOOL]["inputSchema"]["properties"]  # pyright: ignore[reportIndexIssue]


# ---------------------------------------------------------------------------
# The budget: a preview costs a call and nothing else.
# ---------------------------------------------------------------------------


async def _allow(db_session, seed_user, verb: str) -> None:
    db_session.add(
        CompanionToolPolicy(
            user_id=seed_user.id, companion_key="filebrowser", verb=verb, mode="allow"
        )
    )
    await db_session.commit()


async def _enable_destroy(db_session, seed_user) -> None:
    db_session.add(
        CompanionDestroyEnablement(
            user_id=seed_user.id,
            companion_key="filebrowser",
            enabled_at=datetime.now(UTC),
            enabled_by_user_id=seed_user.id,
        )
    )
    await db_session.commit()


async def _cap_policy(db_session, seed_user, **overrides) -> CompanionCapPolicy:
    values = {
        "per_call_calls": 5,
        "per_call_records": 50,
        "per_call_bytes": 1024,
        "per_session_calls": 10,
        "per_session_records": 100,
        "per_session_bytes": 4096,
    }
    values.update(overrides)
    row = CompanionCapPolicy(user_id=seed_user.id, companion_key="filebrowser", **values)
    db_session.add(row)
    await db_session.commit()
    return row


async def _budget_row(db_session, seed_user) -> CompanionSessionBudget | None:
    return (
        await db_session.execute(
            select(CompanionSessionBudget)
            .where(CompanionSessionBudget.user_id == seed_user.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def test_a_dry_run_debits_calls_and_leaves_records_and_bytes_untouched(
    db_session, seed_user, companion_stub
) -> None:
    """A preview must not spend the budget of the mutation it previews.

    ``filebrowser__delete_path`` is a ``records`` verb. Previewing it debits ONE ``calls`` unit --
    the read probe is a call -- and zero records, because nothing was deleted. A preview that
    charged records would make previewing something an agent learns to skip.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="drybudget")
    await _enable_destroy(db_session, seed_user)
    await _allow(db_session, seed_user, "delete_path")
    await _cap_policy(db_session, seed_user)
    auth = AuthContext(user=seed_user)

    await call_companion_tool(
        DELETE_TOOL,
        {"path": "/", "dry_run": True},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
    )

    budget = await _budget_row(db_session, seed_user)
    assert budget is not None
    assert budget.calls_consumed == 1
    assert budget.records_consumed == 0
    assert budget.bytes_consumed == 0


async def test_a_dry_run_queues_the_read_probe_and_never_the_mutation(
    db_session, seed_user, companion_stub
) -> None:
    """The row that reaches the dispatch queue is the PROBE: a GET, class ``read``.

    This is the structural half of "a dry run writes nothing". Even if every executor in the fleet
    obeyed its envelope perfectly, a queued mutating envelope would be one restart away from
    running. There must not be one.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="dryqueue")
    await _enable_destroy(db_session, seed_user)
    await _allow(db_session, seed_user, "delete_path")
    await _cap_policy(db_session, seed_user)
    auth = AuthContext(user=seed_user)

    await call_companion_tool(
        DELETE_TOOL,
        {"path": "/", "dry_run": True},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
    )

    rows = (
        (
            await db_session.execute(
                select(CompanionDispatchCall).where(CompanionDispatchCall.user_id == seed_user.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.verb for row in rows] == ["list_dir"]
    assert [row.tool_class for row in rows] == ["read"]


# ---------------------------------------------------------------------------
# Redemption: the count previewed is the count charged (T-5-34).
# ---------------------------------------------------------------------------


async def _complete_next_call(sessionmaker, *, environment_id, body: str, attempts: int = 500):
    """An executor stand-in that reports a result on an INDEPENDENT connection.

    Deliberately NOT the HTTP stand-in from ``test_companion_tracer.py``: what this case has to
    exercise is the façade's own probe-and-carry path, and standing the claim/result endpoints up
    again here would test the dispatch channel a second time while making the assertion that
    matters harder to read. The bytes-on-the-wire claim is covered by the stub-backed
    writes-nothing assertions above.
    """
    for _attempt in range(attempts):
        async with sessionmaker() as db:
            row = (
                (
                    await db.execute(
                        select(CompanionDispatchCall)
                        .where(
                            CompanionDispatchCall.environment_id == environment_id,
                            CompanionDispatchCall.status == "queued",
                        )
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .first()
            )
            if row is not None:
                row.status = "completed"
                row.result = {"status": 200, "body": body}
                db.add(row)
                await db.commit()
                return
        await asyncio.sleep(0.01)
    raise AssertionError("no queued dispatch call appeared for the executor stand-in")


async def test_the_previewed_target_count_is_what_the_real_call_debits(
    db_session, seed_user, engine, companion_stub
) -> None:
    """An approval for a two-entry delete cannot be redeemed against a one-entry debit.

    Before this plan the default resolver returned 1 for every bulk verb, so a thousand-record
    delete debited a single record unless a test injected a resolver. The dry run is the resolver:
    the count it RESOLVED from the read probe is what the subsequent real call's ``records`` check
    consumes.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="dryredeem"
    )
    await _enable_destroy(db_session, seed_user)
    await _allow(db_session, seed_user, "delete_path")
    await _cap_policy(db_session, seed_user, per_call_records=50, per_session_records=100)
    auth = AuthContext(user=seed_user)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    # Captured BEFORE the façade commits. This case commits on `db_session` several times, which
    # expires `seed_user`, and an expired attribute read outside an await is a `MissingGreenlet`
    # rather than a lazy reload. The same hazard the façade's SAVEPOINT comment records.
    user_id = seed_user.id

    preview, _ = await asyncio.gather(
        call_companion_tool(
            DELETE_TOOL,
            {"path": "/", "dry_run": True},
            auth=auth,
            db=db_session,
            deadline_seconds=5,
        ),
        _complete_next_call(
            sessionmaker,
            environment_id=environment_id,
            body=json.dumps(COMPANION_STUB_LISTING),
        ),
    )

    assert preview.get("isError") is not True, preview
    assert preview["structuredContent"]["targetCount"] == 2  # pyright: ignore[reportIndexIssue]

    await call_companion_tool(
        DELETE_TOOL,
        {"path": "/"},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
    )

    async with sessionmaker() as check:
        budget = (
            await check.execute(
                select(CompanionSessionBudget).where(CompanionSessionBudget.user_id == user_id)
            )
        ).scalar_one_or_none()
        assert budget is not None
        # Two, from the probe. Not one, which is what an unpreviewed single-path call resolves and
        # what every bulk delete debited before this plan.
        assert budget.records_consumed == 2

    async with sessionmaker() as cleanup:
        await cleanup.execute(
            CompanionDispatchCall.__table__.delete().where(
                CompanionDispatchCall.environment_id == environment_id
            )
        )
        await cleanup.commit()


def test_an_unpreviewed_call_never_debits_less_than_it_did_before() -> None:
    """The ledger may only RAISE a debit, never lower one.

    A per-process preview hint that could LOWER a debit would be one cache miss away from being a
    budget bypass, so the façade composes the previewed count with this floor using ``max``. The
    floor is asserted here, unchanged from what plan 05-04 shipped, so the composition cannot be
    simplified into a replacement without this going red.
    """
    entry = companion_verb(DELETE_TOOL)

    assert _single_path_target_count(entry, {"path": "/"}) == 1
    assert _single_path_target_count(entry, {}) == 0
