"""End-to-end façade tests for every ``postiz`` verb (plan 06-07).

Each case drives a real call through ``_call_clawdi_mcp_tool`` or ``call_companion_tool``: the
registry row, the scope, argument, policy, cap and rate gates, the signed pre-authorized call, the
runtime-initiated dispatch channel, an executor stand-in, the Postiz stub, and the untrusted return
leg.

**The executor here is a stand-in, as in ``test_companion_tracer.py``.** The shipped executor is
TypeScript and its per-kind header logic is asserted by
``packages/cli/src/runtime/companion-executor.test.ts``. The stand-in below reads the stub's key
from the test and sends it the way the TypeScript executor does for postiz: a bare
``Authorization`` header, no ``Bearer`` prefix, and no JWT assertion. It parses the envelope with
the shipped ``verify_dispatch_call`` and places arguments by the verb row's ``args_in``, so an
envelope or placement change fails here too.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.api_scopes import RUNTIME_MCP_SCOPES
from app.core.auth import AuthContext
from app.core.companion_caps import CAP_UNIT_RECORDS
from app.core.companion_registry import COMPANION_VERBS, companion_verb
from app.core.database import get_session
from app.main import app
from app.models.api_key import ApiKey
from app.models.companion_dispatch import CompanionDispatchCall
from app.models.companion_tool_call import CallDecision, CompanionToolCall
from app.models.companion_tool_policy import CompanionToolPolicy
from app.models.hosted_runtime import HostedRuntimeState
from app.routes.mcp_bridge import _call_clawdi_mcp_tool
from app.schemas.companions import validate_dry_run_output
from app.services import companion_facade
from app.services.companion_arguments import argument_contract_for, argument_contract_kinds
from app.services.companion_dispatch import verify_dispatch_call
from app.services.companion_dryrun import (
    ProbeResponse,
    compute_dry_run,
    record_preview,
    reset_preview_ledger,
)
from app.services.companion_facade import (
    _previewed_target_count_resolver,
    call_companion_tool,
    list_companion_tools,
    reset_companion_tools_cache,
)
from app.services.companion_ratelimit import (
    SLOT_PROCEED,
    acquire_companion_slot,
    reset_companion_rate_limiter,
)
from app.services.companion_sanitize import UNTRUSTED_CLOSE_PREFIX, UNTRUSTED_OPEN_PREFIX
from tests.hosted_runtime_fixtures import deploy_companion_state
from tests.postiz_stub import POSTIZ_STUB_INTEGRATIONS, POSTIZ_STUB_POSTS, PostizStub

REPO_ROOT = Path(__file__).resolve().parents[2]
POSTIZ_ENTRY_FIXTURE = REPO_ROOT / "test-fixtures" / "companions" / "postiz-manifest-entry.json"

LIST_INTEGRATIONS = "postiz__list_integrations"

# A post item's content bound. Postiz itself accepts far longer content, but the dispatch row
# stores each argument string through the audit redactor, which clips at 500 characters, and
# `enqueue_call` refuses a call whose stored arguments differ from the validated ones (CR-01). The
# contract advertises the bound the pipeline can actually carry.
POSTIZ_CONTENT_MAX_LENGTH = 500


@pytest.fixture(autouse=True)
def _clear_companion_caches():
    reset_companion_tools_cache()
    reset_preview_ledger()
    reset_companion_rate_limiter()
    yield
    reset_companion_tools_cache()
    reset_preview_ledger()
    reset_companion_rate_limiter()


def _postiz_entry() -> dict[str, Any]:
    return json.loads(POSTIZ_ENTRY_FIXTURE.read_text())


def _platform_minted_auth(seed_user) -> AuthContext:
    """The credential the platform mints for an Agent: ``RUNTIME_MCP_SCOPES`` and nothing else."""
    return AuthContext(
        user=seed_user,
        api_key=ApiKey(user_id=seed_user.id, managed=True, scopes=list(RUNTIME_MCP_SCOPES)),
    )


async def _deploy_postiz(db_session, seed_user) -> uuid.UUID:
    environment_id, _companions = await deploy_companion_state(
        db_session,
        seed_user,
        machine_prefix="postiz",
        machine_name="Postiz Agent",
        extra_companions={"postiz": _postiz_entry()},
    )
    return environment_id


async def _allow(db_session, seed_user, verb: str) -> None:
    db_session.add(
        CompanionToolPolicy(user_id=seed_user.id, companion_key="postiz", verb=verb, mode="allow")
    )
    await db_session.commit()


async def _drive_postiz_executor(
    client: httpx.AsyncClient,
    *,
    environment_id: str,
    stub: PostizStub,
    calls: int = 1,
    max_rate_limit_attempts: int = 5,
    attempts: int = 400,
    facade_done: Callable[[], bool] = lambda: False,
) -> list[dict[str, Any]]:
    """The executor stand-in for postiz: claim, authenticate with the static key, call, report.

    Mirrors the TypeScript executor's postiz branch: host and port are the stub's (the registry's,
    in production), method and path come from the signed envelope, the arguments travel where the
    verb row's ``args_in`` says, and the ONLY credential is ``Authorization: <key>``.

    A 429 is re-attempted up to ``max_rate_limit_attempts`` times, as
    ``COMPANION_MAX_RATE_LIMIT_ATTEMPTS`` bounds the real executor. The stand-in does not sleep the
    ``Retry-After``: the real delay schedule is the TypeScript suite's to assert. When the attempts
    run out it reports the same error and ``retry_after_seconds`` the real executor reports.

    Returns every envelope it served, so a test can assert what was dispatched. It stops early
    once the façade call has returned and the queue is empty: a call the façade refused is never
    dispatched, and the test's own assertion on the refusal is the one that should report it.
    """
    served: list[dict[str, Any]] = []
    for _attempt in range(attempts):
        if len(served) == calls:
            return served
        claimed = await client.post(
            "/v1/runtime/companion-dispatch/claim",
            json={"environment_id": environment_id, "wait_seconds": 0},
        )
        claimed.raise_for_status()
        body = claimed.json()
        if body["call"] is None:
            if facade_done():
                return served
            await asyncio.sleep(0.01)
            continue
        envelope = verify_dispatch_call(body["call"])
        served.append(envelope)
        verb_entry = COMPANION_VERBS[f"{envelope['companion_key']}__{envelope['verb']}"]
        placement: dict[str, Any] = (
            {"json": envelope["arguments"]}
            if verb_entry.args_in == "body"
            else {"params": envelope["arguments"]}
        )
        tries = 0
        async with httpx.AsyncClient(base_url=stub.base_url) as companion:
            while True:
                tries += 1
                response = await companion.request(
                    envelope["method"],
                    envelope["path_template"],
                    headers={"Authorization": stub.api_key},
                    **placement,
                )
                if response.status_code != 429 or tries >= max_rate_limit_attempts:
                    break
        report: dict[str, Any] = {"call_jti": envelope["jti"], "claim_id": body["claim_id"]}
        retry_after = response.headers.get("retry-after")
        if retry_after is not None and retry_after.isdigit():
            report["retry_after_seconds"] = int(retry_after)
        if response.status_code == 429:
            report["error"] = f"companion rate limited after {tries} attempts"
        else:
            report["status"] = response.status_code
            report["body"] = response.text
        reported = await client.post("/v1/runtime/companion-dispatch/result", json=report)
        reported.raise_for_status()
    if len(served) == calls:
        return served
    raise AssertionError(f"executor stand-in served {len(served)} of {calls} dispatch calls")


async def _through_the_facade(
    db_session,
    seed_user,
    engine,
    stub: PostizStub,
    invoke: Callable[[AuthContext, Any], Any],
    *,
    calls: int = 1,
) -> tuple[Any, list[dict[str, Any]], list[CompanionToolCall]]:
    """Deploy postiz, run ``invoke`` against the façade while the stand-in serves the queue.

    Returns the façade's result, every envelope the stand-in served, and the provenance rows the
    attempt wrote, read before the committed-lane cleanup removes them.
    """
    environment_id = await _deploy_postiz(db_session, seed_user)
    auth = _platform_minted_auth(seed_user)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_get_session():
        async with sessionmaker() as runtime_db:
            yield runtime_db

    async def _override_get_auth() -> AuthContext:
        return _platform_minted_auth(seed_user)

    from app.core.auth import get_auth, get_auth_short_session

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_auth] = _override_get_auth
    app.dependency_overrides[get_auth_short_session] = _override_get_auth
    transport = ASGITransport(app=app)
    try:
        async with (
            sessionmaker() as facade_db,
            httpx.AsyncClient(transport=transport, base_url="http://test") as runtime_client,
        ):
            facade_call = asyncio.ensure_future(invoke(auth, facade_db))
            served = await _drive_postiz_executor(
                runtime_client,
                environment_id=str(environment_id),
                stub=stub,
                calls=calls,
                facade_done=facade_call.done,
            )
            result = await facade_call
        async with sessionmaker() as check_db:
            provenance = list(
                (
                    await check_db.execute(
                        select(CompanionToolCall)
                        .where(CompanionToolCall.user_id == seed_user.id)
                        .order_by(CompanionToolCall.created_at)
                    )
                ).scalars()
            )
        return result, served, provenance
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_auth, None)
        app.dependency_overrides.pop(get_auth_short_session, None)
        async with sessionmaker() as cleanup:
            await cleanup.execute(
                CompanionToolCall.__table__.delete().where(
                    CompanionToolCall.user_id == seed_user.id
                )
            )
            await cleanup.execute(
                CompanionDispatchCall.__table__.delete().where(
                    CompanionDispatchCall.environment_id == environment_id
                )
            )
            await cleanup.execute(
                HostedRuntimeState.__table__.delete().where(
                    HostedRuntimeState.environment_id == environment_id
                )
            )
            await cleanup.commit()


# ---------------------------------------------------------------------------
# The tracer: one read verb, end to end.
# ---------------------------------------------------------------------------


@pytest.mark.committed_db
async def test_agent_lists_integrations_through_the_facade(
    db_session, seed_user, engine, postiz_stub
) -> None:
    async def invoke(auth, facade_db):
        return await _call_clawdi_mcp_tool(LIST_INTEGRATIONS, {}, auth=auth, db=facade_db)

    result, served, _provenance = await _through_the_facade(
        db_session, seed_user, engine, postiz_stub, invoke
    )

    assert result.get("isError") is not True, result
    text = result["content"][0]["text"]
    assert UNTRUSTED_OPEN_PREFIX in text
    assert UNTRUSTED_CLOSE_PREFIX in text
    for integration in POSTIZ_STUB_INTEGRATIONS:
        assert str(integration["id"]) in text
    # Exactly one request reached Postiz, and it carried the bare key: no `Bearer ` prefix and no
    # filebrowser assertion. The stub answers 401 otherwise, so the listing above proves it too.
    assert len(postiz_stub.requests) == 1
    recorded = postiz_stub.requests[0]
    assert recorded.method == "GET"
    assert recorded.path == "/api/public/v1/integrations"
    assert recorded.header("Authorization") == postiz_stub.api_key
    assert recorded.header("X-JWT-Assertion") is None
    assert recorded.query == {}
    # The envelope carried no credential, and the agent-visible payload carries none either.
    assert served[0]["arguments"] == {}
    assert postiz_stub.api_key not in json.dumps(served)
    assert postiz_stub.api_key not in json.dumps(result)


def test_every_kind_with_verb_rows_owns_an_argument_contract() -> None:
    """A kind with verb rows and no contract would have every call refused and no tool listed.

    Held equal in both directions: a contract for a kind with no verb rows is dead code that would
    still be reachable through ``argument_contract_for``.
    """
    verb_kinds = {name.partition("__")[0] for name in COMPANION_VERBS}

    assert argument_contract_kinds() == verb_kinds


# ---------------------------------------------------------------------------
# Scope (D-09): tools/list needs companions:read, every postiz call needs companions:invoke.
# ---------------------------------------------------------------------------

POSTIZ_TOOLS = sorted(name for name in COMPANION_VERBS if name.startswith("postiz__"))
LIST_POSTS = "postiz__list_posts"
CREATE_POST = "postiz__create_post"

# The UTC day every create_post case below schedules into, and the channels it names.
PREVIEW_DAY = "2026-10-01"
NAMED_CHANNELS = ("int-bluesky-1", "int-linkedin-1")


def _create_post_arguments(**overrides: Any) -> dict[str, Any]:
    """A valid create_post body in the pinned DTO's field names, two post items on two channels."""
    arguments: dict[str, Any] = {
        "type": "schedule",
        "date": f"{PREVIEW_DAY}T12:00:00Z",
        "shortLink": False,
        "tags": [{"value": "launch", "label": "Launch"}],
        "posts": [
            {
                "integration": {"id": "int-bluesky-1"},
                "value": [{"content": "We ship today", "image": []}],
                "settings": {"__type": "bluesky"},
            },
            {
                "integration": {"id": "int-linkedin-1"},
                "value": [{"content": "We ship today, and here is what changed."}],
            },
        ],
    }
    arguments.update(overrides)
    return arguments


def _invalid(verb: str, arguments: dict[str, Any]) -> str | None:
    contract = argument_contract_for("postiz")
    return contract.invalid(verb, COMPANION_VERBS[f"postiz__{verb}"], arguments)


async def test_postiz_tools_list_requires_companions_read(db_session, seed_user) -> None:
    await _deploy_postiz(db_session, seed_user)
    without_read = AuthContext(
        user=seed_user,
        api_key=ApiKey(
            user_id=seed_user.id,
            managed=True,
            scopes=[scope for scope in RUNTIME_MCP_SCOPES if scope != "companions:read"],
        ),
    )

    assert await list_companion_tools(without_read, db_session) == []

    reset_companion_tools_cache()
    listed = await list_companion_tools(_platform_minted_auth(seed_user), db_session)
    names = {tool["name"] for tool in listed}
    assert set(POSTIZ_TOOLS) <= names


@pytest.mark.parametrize("tool_name", POSTIZ_TOOLS)
async def test_every_postiz_call_requires_companions_invoke(
    db_session, seed_user, tool_name: str
) -> None:
    await _deploy_postiz(db_session, seed_user)
    without_invoke = AuthContext(
        user=seed_user,
        api_key=ApiKey(
            user_id=seed_user.id,
            managed=True,
            scopes=[scope for scope in RUNTIME_MCP_SCOPES if scope != "companions:invoke"],
        ),
    )

    result = await call_companion_tool(tool_name, {}, auth=without_invoke, db=db_session)

    assert result["isError"] is True
    assert "companions:invoke" in result["content"][0]["text"]


async def test_each_postiz_verb_lists_its_own_closed_schema(db_session, seed_user) -> None:
    await _deploy_postiz(db_session, seed_user)

    listed = await list_companion_tools(_platform_minted_auth(seed_user), db_session)

    schemas = {tool["name"]: tool["inputSchema"] for tool in listed}
    for name in POSTIZ_TOOLS:
        assert schemas[name]["additionalProperties"] is False, name
        assert "path" not in schemas[name]["properties"], name
    assert set(schemas[LIST_POSTS]["required"]) == {"startDate", "endDate"}
    assert set(schemas[CREATE_POST]["required"]) == {"type", "date", "shortLink", "tags", "posts"}
    assert schemas[CREATE_POST]["properties"]["dry_run"]["type"] == "boolean"


# ---------------------------------------------------------------------------
# list_posts validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["startDate", "endDate"])
def test_list_posts_refuses_a_missing_date(missing: str) -> None:
    arguments = {"startDate": "2026-10-01T00:00:00Z", "endDate": "2026-10-02T00:00:00Z"}
    del arguments[missing]

    problem = _invalid("list_posts", arguments)

    assert problem is not None
    assert missing in problem


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-01",
        "2026-10-01T00:00:00",
        "2026-10-01T00:00:00+02:00",
        "2026-13-01T00:00:00Z",
        "yesterday",
        20261001,
    ],
)
def test_list_posts_refuses_a_date_that_is_not_iso_8601_utc(value: object) -> None:
    problem = _invalid("list_posts", {"startDate": value, "endDate": "2026-10-02T00:00:00Z"})

    assert problem is not None
    assert "startDate" in problem


@pytest.mark.parametrize("end", ["2026-10-01T00:00:00Z", "2026-09-30T23:59:59Z"])
def test_list_posts_refuses_an_end_date_not_after_its_start(end: str) -> None:
    problem = _invalid("list_posts", {"startDate": "2026-10-01T00:00:00Z", "endDate": end})

    assert problem is not None
    assert "after" in problem


def test_list_posts_refuses_a_window_longer_than_31_days() -> None:
    assert (
        _invalid(
            "list_posts", {"startDate": "2026-10-01T00:00:00Z", "endDate": "2026-11-01T00:00:00Z"}
        )
        is None
    )
    problem = _invalid(
        "list_posts", {"startDate": "2026-10-01T00:00:00Z", "endDate": "2026-11-01T00:00:01Z"}
    )

    assert problem is not None
    assert "31 days" in problem


def test_list_posts_accepts_an_optional_customer_of_at_most_64_characters() -> None:
    window = {"startDate": "2026-10-01T00:00:00Z", "endDate": "2026-10-02T00:00:00Z"}

    assert _invalid("list_posts", window) is None
    assert _invalid("list_posts", {**window, "customer": "c" * 64}) is None
    too_long = _invalid("list_posts", {**window, "customer": "c" * 65})
    assert too_long is not None
    assert "customer" in too_long


@pytest.mark.committed_db
async def test_list_posts_sends_both_dates_as_query_parameters(
    db_session, seed_user, engine, postiz_stub
) -> None:
    window = {"startDate": "2026-10-01T00:00:00Z", "endDate": "2026-10-02T00:00:00Z"}

    async def invoke(auth, facade_db):
        return await call_companion_tool(LIST_POSTS, window, auth=auth, db=facade_db)

    result, _served, _provenance = await _through_the_facade(
        db_session, seed_user, engine, postiz_stub, invoke
    )

    assert result.get("isError") is not True, result
    assert "post-existing-1" in result["content"][0]["text"]
    assert "post-existing-3" not in result["content"][0]["text"]
    (recorded,) = postiz_stub.requests
    assert recorded.method == "GET"
    assert recorded.path == "/api/public/v1/posts"
    assert recorded.query == {"startDate": [window["startDate"]], "endDate": [window["endDate"]]}
    assert recorded.body == b""


@pytest.mark.committed_db
async def test_a_companion_answer_longer_than_500_characters_reaches_the_agent_whole(
    db_session, seed_user, engine, postiz_stub
) -> None:
    """The dispatch row once clipped every result body at the audit redactor's 500 characters.

    That truncated the answer silently, with no notice in the envelope, and broke any preview whose
    listing ran longer. The last post below sits past character 500 of the listing, so it only
    reaches the agent if the whole body does.
    """
    postiz_stub.posts = [
        {**post, "id": f"post-bulk-{index}", "content": f"bulk post {index} " + "x" * 80}
        for index, post in enumerate(POSTIZ_STUB_POSTS[:3] * 4)
    ]
    window = {"startDate": "2026-10-01T00:00:00Z", "endDate": "2026-10-02T00:00:00Z"}

    async def invoke(auth, facade_db):
        return await call_companion_tool(LIST_POSTS, window, auth=auth, db=facade_db)

    result, _served, _provenance = await _through_the_facade(
        db_session, seed_user, engine, postiz_stub, invoke
    )

    assert result.get("isError") is not True, result
    text = result["content"][0]["text"]
    assert len(json.dumps({"posts": postiz_stub.posts})) > 2000
    assert "post-bulk-11" in text
    assert "truncated" not in text


# ---------------------------------------------------------------------------
# create_post validation. Field names from the pinned DTO: create.post.dto.ts at v2.24.0.
# ---------------------------------------------------------------------------


def test_a_valid_create_post_body_is_accepted() -> None:
    assert _invalid("create_post", _create_post_arguments()) is None
    assert _invalid("create_post", _create_post_arguments(dry_run=True)) is None


@pytest.mark.parametrize("post_type", ["update", "publish", "", None, 1])
def test_create_post_refuses_a_type_outside_the_dto_enum(post_type: object) -> None:
    problem = _invalid("create_post", _create_post_arguments(type=post_type))

    assert problem is not None
    assert "'type'" in problem


@pytest.mark.parametrize("post_type", ["draft", "schedule", "now"])
def test_create_post_accepts_each_creating_type(post_type: str) -> None:
    assert _invalid("create_post", _create_post_arguments(type=post_type)) is None


@pytest.mark.parametrize("date", ["2026-10-01", "2026-10-01T12:00:00+01:00", "soon", None])
def test_create_post_refuses_a_date_that_is_not_iso_8601_utc(date: object) -> None:
    problem = _invalid("create_post", _create_post_arguments(date=date))

    assert problem is not None
    assert "'date'" in problem


@pytest.mark.parametrize("short_link", ["false", 0, None])
def test_create_post_refuses_a_short_link_that_is_not_a_boolean(short_link: object) -> None:
    problem = _invalid("create_post", _create_post_arguments(shortLink=short_link))

    assert problem is not None
    assert "shortLink" in problem


@pytest.mark.parametrize(
    "tags",
    [
        [{"value": f"t{index}", "label": f"T{index}"} for index in range(21)],
        [{"value": "v" * 65, "label": "ok"}],
        [{"value": "ok", "label": "l" * 65}],
        [{"value": "ok"}],
        [{"value": "ok", "label": "ok", "color": "red"}],
        [{"value": 1, "label": "ok"}],
        "launch",
    ],
)
def test_create_post_bounds_its_tags(tags: object) -> None:
    problem = _invalid("create_post", _create_post_arguments(tags=tags))

    assert problem is not None
    assert "tags" in problem


def test_create_post_accepts_twenty_tags_and_an_empty_tag_list() -> None:
    twenty = [{"value": f"t{index}", "label": f"T{index}"} for index in range(20)]

    assert _invalid("create_post", _create_post_arguments(tags=twenty)) is None
    assert _invalid("create_post", _create_post_arguments(tags=[])) is None


def _post_item(integration_id: str = "int-bluesky-1", **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "integration": {"id": integration_id},
        "value": [{"content": "hello"}],
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize(
    "posts",
    [[], [_post_item() for _ in range(21)], "a post", [["not", "an", "object"]]],
    ids=["empty", "twenty-one", "string", "array-item"],
)
def test_create_post_bounds_its_post_items(posts: object) -> None:
    problem = _invalid("create_post", _create_post_arguments(posts=posts))

    assert problem is not None
    assert "posts" in problem


def test_create_post_accepts_twenty_post_items() -> None:
    posts = [_post_item() for _ in range(20)]

    assert _invalid("create_post", _create_post_arguments(posts=posts)) is None


@pytest.mark.parametrize(
    "integration_id",
    [
        "http://169.254.169.254/latest",
        "//evil.example/x",
        "../integrations",
        "int:1",
        "int 1",
        "int\t1",
        "",
        "i" * 65,
    ],
)
def test_create_post_refuses_an_integration_id_that_could_address_something(
    integration_id: str,
) -> None:
    problem = _invalid(
        "create_post", _create_post_arguments(posts=[_post_item(integration_id=integration_id)])
    )

    assert problem is not None
    assert "integration.id" in problem


@pytest.mark.parametrize(
    "value",
    [
        [],
        [{"content": "x"} for _ in range(21)],
        [{"content": "c" * (POSTIZ_CONTENT_MAX_LENGTH + 1)}],
        [{"content": 1}],
        [
            {
                "content": "x",
                "image": [{"id": f"m{index}", "path": "/uploads/a.png"} for index in range(5)],
            }
        ],
        [{"content": "x", "image": [{"id": "m1", "path": "https://attacker.example/a.png"}]}],
        [{"content": "x", "image": [{"id": "m1", "path": "//attacker.example/a.png"}]}],
        [{"content": "x", "image": [{"id": "m/1", "path": "/uploads/a.png"}]}],
        [{"content": "x", "image": [{"id": "m1", "path": "/uploads/a.png", "alt": "x"}]}],
        [{"content": "x", "delay": 5}],
    ],
    ids=[
        "no-entries",
        "twenty-one-entries",
        "content-too-long",
        "content-not-a-string",
        "five-images",
        "image-url",
        "image-authority",
        "image-id-slash",
        "image-extra-key",
        "entry-extra-key",
    ],
)
def test_create_post_bounds_each_items_value_entries_content_and_images(value: object) -> None:
    problem = _invalid("create_post", _create_post_arguments(posts=[_post_item(value=value)]))

    assert problem is not None
    assert "value" in problem


def test_create_post_accepts_content_at_its_bound_and_four_images() -> None:
    images = [{"id": f"m{index}", "path": f"/uploads/{index}.png"} for index in range(4)]
    value = [{"content": "c" * POSTIZ_CONTENT_MAX_LENGTH, "image": images}]

    assert _invalid("create_post", _create_post_arguments(posts=[_post_item(value=value)])) is None


def test_create_post_bounds_settings_at_16_kib() -> None:
    small = {"__type": "bluesky", "notes": ["n" * 400] * 40}
    large = {"__type": "bluesky", "notes": ["n" * 400] * 42}

    assert len(json.dumps(small, separators=(",", ":")).encode()) <= 16 * 1024
    assert len(json.dumps(large, separators=(",", ":")).encode()) > 16 * 1024
    assert (
        _invalid("create_post", _create_post_arguments(posts=[_post_item(settings=small)])) is None
    )
    problem = _invalid("create_post", _create_post_arguments(posts=[_post_item(settings=large)]))
    assert problem is not None
    assert "settings" in problem
    not_an_object = _invalid(
        "create_post", _create_post_arguments(posts=[_post_item(settings=["bluesky"])])
    )
    assert not_an_object is not None
    assert "settings" in not_an_object


def test_create_post_refuses_a_value_that_would_not_survive_the_dispatch_row() -> None:
    """The dispatch row redacts secret-ish keys and clips strings, which would change the call.

    ``enqueue_call`` refuses such a call AFTER the gates (CR-01). The contract refuses it BEFORE
    them, so a preview cannot show a post the real call would then refuse.
    """
    redacted = {"__type": "bluesky", "access_token": "x"}
    clipped = {"__type": "bluesky", "note": "n" * 501}

    for settings in (redacted, clipped):
        problem = _invalid(
            "create_post", _create_post_arguments(posts=[_post_item(settings=settings)])
        )
        assert problem is not None
        assert "stored unchanged" in problem


@pytest.mark.parametrize(
    "arguments",
    [
        _create_post_arguments(url="https://evil.example"),
        _create_post_arguments(republish=True),
        _create_post_arguments(posts=[_post_item(group="g-1")]),
        _create_post_arguments(posts=[{**_post_item(), "integration": {"id": "i", "x": 1}}]),
    ],
    ids=["top-level-url", "top-level-republish", "post-group", "integration-extra"],
)
def test_create_post_refuses_any_key_it_does_not_declare(arguments: dict[str, Any]) -> None:
    assert _invalid("create_post", arguments) is not None


# ---------------------------------------------------------------------------
# A refused argument is refused before any mint.
# ---------------------------------------------------------------------------

_REFUSED_ARGUMENTS: list[tuple[str, dict[str, Any]]] = [
    *[(name, {"url": "http://169.254.169.254/"}) for name in POSTIZ_TOOLS],
    (LIST_POSTS, {"startDate": "http://x/", "endDate": "2026-10-02T00:00:00Z"}),
    (
        LIST_POSTS,
        {
            "startDate": "2026-10-01T00:00:00Z",
            "endDate": "2026-10-02T00:00:00Z",
            "customer": "http://evil.example",
        },
    ),
    (CREATE_POST, _create_post_arguments(posts=[_post_item("http://evil.example/")])),
]


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    _REFUSED_ARGUMENTS,
    ids=[f"{name}-{index}" for index, (name, _) in enumerate(_REFUSED_ARGUMENTS)],
)
async def test_an_unknown_or_url_shaped_argument_is_refused_before_any_mint(
    db_session, seed_user, monkeypatch, tool_name: str, arguments: dict[str, Any]
) -> None:
    await _deploy_postiz(db_session, seed_user)
    minted: list[object] = []

    def _recording_mint(**kwargs: object) -> object:
        minted.append(kwargs)
        raise AssertionError("a refused call must never be minted")

    monkeypatch.setattr(companion_facade, "mint_dispatch_call", _recording_mint)

    result = await call_companion_tool(
        tool_name, arguments, auth=_platform_minted_auth(seed_user), db=db_session
    )

    assert result["isError"] is True
    assert "rejected its arguments" in result["content"][0]["text"]
    assert minted == []
    row = (
        await db_session.execute(
            select(CompanionToolCall).where(CompanionToolCall.user_id == seed_user.id)
        )
    ).scalar_one()
    assert row.decision == CallDecision.REFUSED_POLICY.value


# ---------------------------------------------------------------------------
# create_post, for real.
# ---------------------------------------------------------------------------


@pytest.mark.committed_db
async def test_create_post_sends_its_body_as_json_without_control_parameters(
    db_session, seed_user, engine, postiz_stub
) -> None:
    await _allow(db_session, seed_user, "create_post")
    arguments = _create_post_arguments(dry_run=False)

    async def invoke(auth, facade_db):
        return await call_companion_tool(CREATE_POST, arguments, auth=auth, db=facade_db)

    result, served, _provenance = await _through_the_facade(
        db_session, seed_user, engine, postiz_stub, invoke
    )

    assert result.get("isError") is not True, result
    (recorded,) = postiz_stub.requests
    assert recorded.method == "POST"
    assert recorded.path == "/api/public/v1/posts"
    assert recorded.query == {}
    assert recorded.header("content-type") == "application/json"
    expected = {key: value for key, value in arguments.items() if key != "dry_run"}
    assert recorded.json() == expected
    assert served[0]["arguments"] == expected
    assert "post-new-1" in result["content"][0]["text"]


@pytest.mark.committed_db
async def test_create_post_debits_records_by_its_post_items(
    db_session, seed_user, engine, postiz_stub
) -> None:
    await _allow(db_session, seed_user, "create_post")
    three = [_post_item(), _post_item("int-linkedin-1"), _post_item("int-mastodon-1")]

    async def invoke(auth, facade_db):
        return await call_companion_tool(
            CREATE_POST, _create_post_arguments(posts=three), auth=auth, db=facade_db
        )

    result, _served, provenance = await _through_the_facade(
        db_session, seed_user, engine, postiz_stub, invoke
    )

    assert result.get("isError") is not True, result
    (row,) = provenance
    assert row.decision == CallDecision.ALLOWED.value
    assert row.cap_unit == CAP_UNIT_RECORDS
    assert row.cap_consumed == 3


# ---------------------------------------------------------------------------
# create_post, previewed.
# ---------------------------------------------------------------------------


@pytest.mark.committed_db
async def test_create_post_dry_run_reads_list_posts_for_the_utc_day_of_its_date(
    db_session, seed_user, engine, postiz_stub
) -> None:
    async def invoke(auth, facade_db):
        return await call_companion_tool(
            CREATE_POST, _create_post_arguments(dry_run=True), auth=auth, db=facade_db
        )

    result, served, _provenance = await _through_the_facade(
        db_session, seed_user, engine, postiz_stub, invoke
    )

    assert result.get("isError") is not True, result
    # Exactly one read, no write, and it asked for the whole UTC day of `date`.
    (probe,) = served
    assert probe["verb"] == "list_posts"
    assert postiz_stub.mutating_requests() == []
    (recorded,) = postiz_stub.requests
    assert recorded.method == "GET"
    assert recorded.path == "/api/public/v1/posts"
    assert recorded.query == {
        "startDate": [f"{PREVIEW_DAY}T00:00:00.000Z"],
        "endDate": [f"{PREVIEW_DAY}T23:59:59.999Z"],
    }


@pytest.mark.committed_db
async def test_create_post_dry_run_lists_proposed_posts_beside_the_existing_ones(
    db_session, seed_user, engine, postiz_stub
) -> None:
    async def invoke(auth, facade_db):
        return await call_companion_tool(
            CREATE_POST, _create_post_arguments(dry_run=True), auth=auth, db=facade_db
        )

    result, _served, provenance = await _through_the_facade(
        db_session, seed_user, engine, postiz_stub, invoke
    )

    diff = result["structuredContent"]
    assert validate_dry_run_output(diff) is None
    assert diff["tool"] == CREATE_POST
    assert diff["target"] == f"{PREVIEW_DAY}|{','.join(sorted(NAMED_CHANNELS))}"
    assert diff["targetCount"] == 2
    # Before: what is already scheduled that day on the NAMED channels, and nothing else.
    before_names = sorted(entry["name"] for entry in diff["before"])
    assert before_names == ["Hiring: two engineers", "Launch week starts tomorrow"]
    assert all(entry["modified"].startswith(PREVIEW_DAY) for entry in diff["before"])
    # After: the same posts plus one entry per proposed post item.
    proposed = [entry for entry in diff["after"] if entry not in diff["before"]]
    assert len(diff["after"]) == len(diff["before"]) + 2
    assert sorted(entry["path"].split("/")[0] for entry in proposed) == sorted(NAMED_CHANNELS)
    assert {entry["name"] for entry in proposed} == {
        "We ship today",
        "We ship today, and here is what changed.",
    }
    assert all(entry["modified"] == f"{PREVIEW_DAY}T12:00:00Z" for entry in proposed)
    assert any("social provider" in item for item in diff["undetermined"])
    (row,) = provenance
    assert row.decision == CallDecision.DRY_RUN_COMPLETED.value
    assert row.dry_run is True


@pytest.mark.committed_db
async def test_create_post_dry_run_shows_only_the_first_200_characters_of_content(
    db_session, seed_user, engine, postiz_stub
) -> None:
    long_content = "a" * 150 + "b" * 150
    posts = [_post_item(value=[{"content": long_content}])]

    async def invoke(auth, facade_db):
        return await call_companion_tool(
            CREATE_POST,
            _create_post_arguments(posts=posts, dry_run=True),
            auth=auth,
            db=facade_db,
        )

    result, _served, _provenance = await _through_the_facade(
        db_session, seed_user, engine, postiz_stub, invoke
    )

    diff = result["structuredContent"]
    proposed = [entry for entry in diff["after"] if entry not in diff["before"]]
    assert [entry["name"] for entry in proposed] == [long_content[:200]]


def _postiz_stub_probe(stub: PostizStub):
    """A probe executor issuing the REAL request the registry declares, with the stub's key."""

    async def execute(probe_tool: str, probe_arguments: dict[str, object]) -> ProbeResponse:
        entry = companion_verb(probe_tool)
        async with httpx.AsyncClient(base_url=stub.base_url) as companion:
            response = await companion.request(
                entry.method,
                entry.path_template,
                params=probe_arguments,
                headers={"Authorization": stub.api_key},
            )
        return ProbeResponse(status=response.status_code, body=response.text)

    return execute


@pytest.mark.parametrize(
    ("status", "raw", "named"),
    [
        (401, None, "401"),
        (404, None, "404"),
        (500, None, "500"),
        (None, b"<html>not json</html>", "JSON"),
    ],
    ids=["unauthorized", "not-found", "server-error", "non-json"],
)
async def test_create_post_dry_run_probe_failure_is_an_error_result_naming_the_probe(
    postiz_stub, status: int | None, raw: bytes | None, named: str
) -> None:
    postiz_stub.posts_listing_status = status
    postiz_stub.posts_listing_raw = raw

    result = await compute_dry_run(
        CREATE_POST,
        companion_verb(CREATE_POST),
        _create_post_arguments(dry_run=True),
        execute_probe=_postiz_stub_probe(postiz_stub),
    )

    assert result.is_error is True
    assert result.message is not None
    assert named in result.message
    assert result.before == []
    assert result.after == []
    assert postiz_stub.mutating_requests() == []


async def test_create_post_dry_run_with_a_listing_missing_its_posts_is_an_error() -> None:
    async def execute(probe_tool: str, probe_arguments: dict[str, object]) -> ProbeResponse:
        del probe_tool, probe_arguments
        return ProbeResponse(status=200, body=json.dumps({"comments": []}))

    result = await compute_dry_run(
        CREATE_POST,
        companion_verb(CREATE_POST),
        _create_post_arguments(dry_run=True),
        execute_probe=execute,
    )

    assert result.is_error is True
    assert result.message is not None
    assert "list_posts" in result.message


# ---------------------------------------------------------------------------
# The preview's count and the real call's records debit (T-5-34 continuity).
# ---------------------------------------------------------------------------


def test_a_create_post_preview_count_feeds_the_records_resolver(seed_user) -> None:
    arguments = _create_post_arguments()
    contract = argument_contract_for("postiz")
    target = contract.target_key("create_post", arguments)
    assert target == f"{PREVIEW_DAY}|{','.join(sorted(NAMED_CHANNELS))}"
    resolve = _previewed_target_count_resolver(seed_user.id, CREATE_POST)
    entry = COMPANION_VERBS[CREATE_POST]

    assert resolve(entry, arguments) == 2
    # A preview that counted more raises the debit; the composition is `max`, never replacement.
    record_preview(user_id=seed_user.id, tool_name=CREATE_POST, target_key=target, target_count=5)
    assert resolve(entry, arguments) == 5
    record_preview(user_id=seed_user.id, tool_name=CREATE_POST, target_key=target, target_count=1)
    assert resolve(entry, arguments) == 2


def test_a_create_post_without_a_preview_still_debits_its_post_items(seed_user) -> None:
    resolve = _previewed_target_count_resolver(seed_user.id, CREATE_POST)
    three = [_post_item(), _post_item("int-linkedin-1"), _post_item("int-mastodon-1")]

    assert resolve(COMPANION_VERBS[CREATE_POST], _create_post_arguments(posts=three)) == 3


# ---------------------------------------------------------------------------
# Postiz's per-hour create limit (TOOL-09 continuity).
# ---------------------------------------------------------------------------


@pytest.mark.committed_db
async def test_a_rate_limited_create_post_reaches_the_agent_as_the_retry_after_refusal(
    db_session, seed_user, engine, postiz_stub
) -> None:
    await _allow(db_session, seed_user, "create_post")
    postiz_stub.create_rate_limited = True
    postiz_stub.retry_after_seconds = 7

    async def invoke(auth, facade_db):
        return await call_companion_tool(
            CREATE_POST, _create_post_arguments(), auth=auth, db=facade_db
        )

    result, _served, provenance = await _through_the_facade(
        db_session, seed_user, engine, postiz_stub, invoke
    )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "rate limited after 5 attempts" in text
    # Not a transport failure: the call was claimed and answered, and the runtime said why.
    assert "not reachable" not in text
    assert [request.method for request in postiz_stub.requests] == ["POST"] * 5
    (row,) = provenance
    assert row.decision == CallDecision.ALLOWED.value
    # The companion's Retry-After drained this tenant's bucket, so the NEXT call is slowed or
    # refused by the façade instead of walking into a second 429.
    slot = acquire_companion_slot(user_id=seed_user.id, companion_key="postiz")
    assert slot.action != SLOT_PROCEED
