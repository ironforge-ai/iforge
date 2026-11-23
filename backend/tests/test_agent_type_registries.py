"""Agent-type registries the compiler cannot reach.

Per-agent-type fan-out that is not exhaustiveness-checked: the CLI's
`agentSkillTargetDir` branch, the backend's `_AGENT_TYPE_LABELS`, and linked-Project
Skill delivery to a Connected Agent. Each one degrades quietly rather than failing
loudly when a new runtime is missed — a raw lowercase key in the dashboard, or a
silently refused Project-to-Agent skill delivery. These tests turn each omission
into a failure.

Upstream #1091 made Project Skills desired state and removed the Connected
capability allowlist of agent types together with its readiness check, so
delivery is now asserted end to end: every CLI agent type
registers as a Connected Agent, links a Project and receives its Skill in the
desired inventory. If a later upstream step reintroduces a per-type gate that
misses IronHermes, the delivery tests below fail rather than a set lookup.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthContext
from app.models.project import Project
from app.models.user import User
from app.services.agent_environments import agent_type_default_label
from tests.conftest import create_env_with_project
from tests.test_project_runtime_skills import (
    _connected_agent_auth,
    _link,
    _report_connected_project_skill_capability,
    _set_auth,
    _upload_project_skill,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Upstream #972 moved the AGENT_TYPES tuple out of registry.ts (which re-exports it).
_CLI_REGISTRY = _REPO_ROOT / "packages" / "cli" / "src" / "adapters" / "agent-types.ts"


def _cli_agent_types() -> list[str]:
    """Read the CLI's AGENT_TYPES tuple, the single source of truth for the set.

    A regex over the tuple literal keeps the drift guard honest without adding a
    Node build step to the Python suite. If the declaration is ever reshaped so
    this stops matching, the assertion below fails loudly rather than silently
    checking an empty set.
    """
    source = _CLI_REGISTRY.read_text(encoding="utf-8")
    match = re.search(r"export const AGENT_TYPES = \[(.*?)\] as const", source, re.DOTALL)
    assert match is not None, f"AGENT_TYPES tuple not found in {_CLI_REGISTRY}"
    types = re.findall(r'"([a-z_]+)"', match.group(1))
    assert types, f"AGENT_TYPES tuple parsed as empty in {_CLI_REGISTRY}"
    return types


def test_cli_agent_types_are_readable_and_include_ironhermes():
    # Guards the guard: if this list ever parses empty or loses IronHermes, the
    # two drift assertions below would pass vacuously.
    assert "ironhermes" in _cli_agent_types()


def test_ironhermes_label_is_the_branded_proper_noun():
    # Casing is load-bearing: capital I, capital H, no space, no hyphen.
    # The `.get(agent_type, agent_type)` fallback means a missing key degrades
    # to the raw lowercase string instead of raising, so only an assertion
    # catches it.
    assert agent_type_default_label("ironhermes") == "IronHermes"


def test_ironhermes_label_is_not_the_raw_key():
    assert agent_type_default_label("ironhermes") != "ironhermes"


def test_hermes_label_is_unchanged_by_the_ironhermes_addition():
    # The two runtimes are distinct agent types and must never be conflated.
    assert agent_type_default_label("hermes") == "Hermes"


def test_every_cli_agent_type_has_a_backend_label():
    """Drift guard, so the next runtime cannot land label-less."""
    missing = [t for t in _cli_agent_types() if agent_type_default_label(t) == t]
    assert missing == [], f"agent types with no backend label: {missing}"


async def _delivered_project_skill_keys(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    *,
    user: User,
    project: Project,
    agent_type: str,
) -> list[str]:
    """Register a Connected Agent of `agent_type`, link `project`, read its desired Skills."""
    skill_key = f"registry-{agent_type.replace('_', '-')}"
    uploaded = await _upload_project_skill(client, project.id, skill_key)
    assert uploaded.status_code == 200, uploaded.text
    agent = await create_env_with_project(
        db_session,
        user_id=user.id,
        machine_id=f"registry-{agent_type}",
        machine_name=f"Registry {agent_type}",
        agent_type=agent_type,
    )
    # POST /v1/agents as a Connected principal: the registration itself must accept
    # the type, and it writes the Connected origin marker the desired read requires.
    capability = await _report_connected_project_skill_capability(
        client, db_session, user=user, agent_id=agent.id
    )
    assert capability.status_code == 204, capability.text
    linked = await _link(client, agent_id=agent.id, project_id=project.id)
    assert linked.status_code == 200, linked.text

    _set_auth(_connected_agent_auth(user))
    try:
        desired = await client.get(
            "/v1/runtime/project-skills",
            params={"environment_id": str(agent.id)},
        )
    finally:
        _set_auth(AuthContext(user=user))
    assert desired.status_code == 200, desired.text
    return [item["skill_key"] for item in desired.json()["skills"]]


@pytest.mark.asyncio
async def test_ironhermes_receives_linked_project_skills(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed_user: User,
    workspace_project: Project,
):
    # A refused type is silent: no error, no compile failure, just skills that
    # never arrive.
    keys = await _delivered_project_skill_keys(
        client,
        db_session,
        user=seed_user,
        project=workspace_project,
        agent_type="ironhermes",
    )
    assert keys == ["registry-ironhermes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", _cli_agent_types())
async def test_every_cli_agent_type_receives_linked_project_skills(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed_user: User,
    workspace_project: Project,
    agent_type: str,
):
    keys = await _delivered_project_skill_keys(
        client,
        db_session,
        user=seed_user,
        project=workspace_project,
        agent_type=agent_type,
    )
    assert keys == [f"registry-{agent_type.replace('_', '-')}"]


def test_hosted_runtime_literals_are_untouched():
    """Phase 7 owns the hosted-runtime enumerations; this phase is local-only.

    Advertising a deploy target that does not exist is worse than not
    advertising it, so `ironhermes` must NOT appear in the hosted runtime schema.
    """
    runtime_schema = _REPO_ROOT / "backend" / "app" / "schemas" / "runtime.py"
    assert "ironhermes" not in runtime_schema.read_text(encoding="utf-8")
