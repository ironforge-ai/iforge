"""D-17 completeness guard: deletion coverage is a test-enforced contract.

Sweeps `Base.metadata.tables` for tenant-scoped columns BY NAME — not by
FK introspection, because Memory/Skill/Vault/V2RuntimeEnvironmentFence/
DeviceAuthorization carry FK-less user columns an FK-based guard would
miss (RESEARCH Pattern 5 / Pitfall 7). Any flagged table without an
explicit `PURGE_COVERAGE` disposition fails CI, naming the offenders: a
future model with a tenant column and no deletion handling breaks the
build.

Same test-enforced-contract discipline as
`test_iforge_companion_docs_contract.py`.
"""

from __future__ import annotations

import importlib
import pkgutil

from sqlalchemy import MetaData

from app.models.base import Base
from app.services.tenant_purge import PURGE_COVERAGE

# The column-name vocabulary that marks a table as tenant-scoped (D-17).
# Kept as a module constant here so the guard's contract is visible in
# one place; `tenant_purge.TENANT_SCOPE_COLUMNS` mirrors it for the
# delete machinery.
TENANT_COLUMNS = {
    "user_id",
    "owner_id",
    "owner_user_id",
    "invited_by",
    "actor_user_id",
    "target_user_id",
    "invitee_user_id",
    "member_user_id",
    "created_by",
}

VALID_SIMPLE_DISPOSITIONS = {"purge", "tombstone", "anonymize", "global"}


def _register_all_models() -> None:
    """Import every model module the way alembic's env.py / app startup do,
    so the metadata sweep sees the full table inventory."""
    import app.models as models_package

    for module in pkgutil.iter_modules(models_package.__path__):
        importlib.import_module(f"app.models.{module.name}")


_register_all_models()


def _flagged_tables() -> dict[str, set[str]]:
    """table name -> the tenant columns that flagged it."""
    flagged: dict[str, set[str]] = {}
    for name, table in Base.metadata.tables.items():
        matches = TENANT_COLUMNS & {column.name for column in table.columns}
        if matches:
            flagged[name] = matches
    return flagged


def test_every_tenant_column_table_has_a_purge_disposition() -> None:
    """The CI tripwire: a future model with a tenant-scoped column and no
    PURGE_COVERAGE entry fails here, naming the offenders."""
    flagged = _flagged_tables()
    assert flagged, "expected tenant-scoped tables in metadata"
    missing = set(flagged) - set(PURGE_COVERAGE)
    assert not missing, (
        "Tables carry tenant columns but have no PURGE_COVERAGE disposition "
        f"(add one to backend/app/services/tenant_purge.py): "
        f"{sorted((name, sorted(flagged[name])) for name in missing)}"
    )


def test_purge_coverage_has_no_stale_entries() -> None:
    """Every PURGE_COVERAGE key exists in Base.metadata (no dead entries
    surviving a table rename/removal)."""
    stale = set(PURGE_COVERAGE) - set(Base.metadata.tables)
    assert not stale, f"PURGE_COVERAGE names tables not in metadata: {sorted(stale)}"


def test_dispositions_use_the_closed_vocabulary() -> None:
    """Values are purge|tombstone|anonymize|global|via-parent:<table>, and
    every via-parent target exists in the metadata."""
    for table_name, disposition in PURGE_COVERAGE.items():
        if disposition in VALID_SIMPLE_DISPOSITIONS:
            continue
        assert disposition.startswith("via-parent:"), (table_name, disposition)
        parent = disposition.split(":", 1)[1]
        assert parent in Base.metadata.tables, (
            f"{table_name} declares via-parent:{parent} but {parent!r} is not in metadata"
        )
        assert parent in PURGE_COVERAGE, (
            f"{table_name} declares via-parent:{parent} but {parent!r} has no disposition"
        )


def test_fkless_tenant_models_are_flagged_by_the_column_sweep() -> None:
    """Regression pins for the FK-less five (RESEARCH Pattern 5): these
    carry plain UUID tenant columns with NO ForeignKey — the sweep must
    catch what FK introspection misses (Pitfall 7).

    `v2_runtime_environment_fences` is the RuntimeObservation model's
    fence table (runtime_observation.py `owner_id`, FK-less).
    """
    flagged = _flagged_tables()
    assert "memories" in flagged and "user_id" in flagged["memories"]
    assert "skills" in flagged and "user_id" in flagged["skills"]
    assert "vaults" in flagged and "user_id" in flagged["vaults"]
    assert "v2_runtime_environment_fences" in flagged
    assert "owner_id" in flagged["v2_runtime_environment_fences"]
    assert "device_authorizations" in flagged
    assert "user_id" in flagged["device_authorizations"]
    for name in (
        "memories",
        "skills",
        "vaults",
        "v2_runtime_environment_fences",
        "device_authorizations",
    ):
        assert name in PURGE_COVERAGE, name


def test_no_tenant_column_children_are_covered_by_prefix() -> None:
    """Review finding 6: hosted_runtime_states/secrets/config_observations
    and the channel_* children carry no tenant column the sweep could
    flag — a prefix test asserts every hosted_runtime_* / channel_* table
    in metadata has an explicit PURGE_COVERAGE entry, so these transitive
    tables are covered by name, not by luck."""
    prefixed = {
        name for name in Base.metadata.tables if name.startswith(("hosted_runtime_", "channel_"))
    }
    assert prefixed, "expected hosted_runtime_*/channel_* tables in metadata"
    missing = prefixed - set(PURGE_COVERAGE)
    assert not missing, f"prefix-covered tables missing dispositions: {sorted(missing)}"

    # Regression pins on the three hosted_runtime tables named in the plan.
    assert PURGE_COVERAGE["hosted_runtime_states"] == "via-parent:agent_environments"
    assert PURGE_COVERAGE["hosted_runtime_secrets"] == "via-parent:hosted_runtime_states"
    assert PURGE_COVERAGE["hosted_runtime_config_observations"] == (
        "via-parent:hosted_runtime_states"
    )


def _fk_closure(root: str) -> set[str]:
    """Every table reachable from `root` by following foreign keys backwards: a table with a
    foreign key whose referred table is already in the closure joins it, until nothing changes."""
    closure = {root}
    changed = True
    while changed:
        changed = False
        for name, table in Base.metadata.tables.items():
            if name in closure:
                continue
            if any(fk.column.table.name in closure for fk in table.foreign_keys):
                closure.add(name)
                changed = True
    return closure


def test_every_table_reachable_from_sessions_has_a_purge_disposition() -> None:
    """WR-05 (session half): a table hanging off `sessions` through foreign keys alone carries
    no tenant column for the name sweep above to flag, so it would die only through the FK
    cascade -- covered by luck. The closure is derived from the foreign keys, so the next
    session-descendant table fails here by name."""
    closure = _fk_closure("sessions")
    assert {"sessions", "session_shares", "usage_events"} <= closure, sorted(closure)
    missing = closure - set(PURGE_COVERAGE)
    assert not missing, (
        "Tables reachable from sessions through foreign keys have no PURGE_COVERAGE "
        f"disposition (add one to backend/app/services/tenant_purge.py): {sorted(missing)}"
    )

    # Regression pins: the upstream events-v1 tables are named explicitly (sync steps 3-4).
    assert PURGE_COVERAGE["session_event_generations"] == "via-parent:sessions"
    assert PURGE_COVERAGE["session_event_chunks"] == "via-parent:sessions"
    assert PURGE_COVERAGE["session_event_append_receipts"] == "via-parent:sessions"


def tenant_owned_tables(metadata: MetaData | None = None) -> dict[str, str]:
    """Every tenant-owned table -> why: a tenant column of its own, or a foreign key path to a
    table that has one (or to `users`). Shared with the export completeness guard in
    `test_tenant_export.py`, so purge and export sweep the same inventory (INT-3)."""
    tables = (metadata or Base.metadata).tables
    owned: dict[str, str] = {"users": "root"} if "users" in tables else {}
    for name, table in tables.items():
        columns = TENANT_COLUMNS & {column.name for column in table.columns}
        if columns:
            owned.setdefault(name, f"columns {sorted(columns)}")
    changed = True
    while changed:
        changed = False
        for name, table in tables.items():
            if name in owned:
                continue
            parents = sorted({fk.column.table.name for fk in table.foreign_keys} & set(owned))
            if parents:
                owned[name] = f"foreign key to {parents}"
                changed = True
    return owned


def test_every_table_reachable_from_a_tenant_table_has_a_purge_disposition() -> None:
    """INT-3: a table that reaches a tenant table only through foreign keys carries no tenant
    column for the name sweep to flag, so it would die only through the FK cascade -- covered by
    luck. The 06.1 tables `agent_plugin_installations`, `agent_skill_references` and
    `vault_secret_requests` were exactly that. The closure is derived from the foreign keys, so
    the next such table fails here by name."""
    owned = tenant_owned_tables()
    for name in ("agent_plugin_installations", "agent_skill_references", "vault_secret_requests"):
        assert owned.get(name, "").startswith("foreign key"), (name, owned.get(name))
    missing = set(owned) - set(PURGE_COVERAGE)
    assert not missing, (
        "Tenant-owned tables have no PURGE_COVERAGE disposition "
        f"(add one to backend/app/services/tenant_purge.py): "
        f"{sorted((name, owned[name]) for name in missing)}"
    )

    assert PURGE_COVERAGE["vault_secret_requests"] == "via-parent:vaults"
    assert PURGE_COVERAGE["agent_plugin_installations"] == "via-parent:agent_environments"
    assert PURGE_COVERAGE["agent_skill_references"] == "via-parent:agent_environments"


def test_tenant_owned_closure_detects_a_foreign_key_only_table() -> None:
    """Self-test: a table with no tenant column whose only link to a tenant is a foreign key two
    hops away is part of the closure, and a table with no such path is not."""
    from sqlalchemy import Column, ForeignKey, Table
    from sqlalchemy.dialects.postgresql import UUID

    probe = MetaData()
    Table("users", probe, Column("id", UUID, primary_key=True))
    Table(
        "probe_parent",
        probe,
        Column("id", UUID, primary_key=True),
        Column("user_id", UUID, ForeignKey("users.id")),
    )
    Table(
        "probe_child",
        probe,
        Column("id", UUID, primary_key=True),
        Column("parent_id", UUID, ForeignKey("probe_parent.id")),
    )
    Table(
        "probe_grandchild",
        probe,
        Column("id", UUID, primary_key=True),
        Column("child_id", UUID, ForeignKey("probe_child.id")),
    )
    Table("probe_global", probe, Column("id", UUID, primary_key=True))

    owned = tenant_owned_tables(probe)
    assert set(owned) == {"users", "probe_parent", "probe_child", "probe_grandchild"}
    assert owned["probe_grandchild"] == "foreign key to ['probe_child']"


def test_tenant_lifecycle_tables_cover_themselves() -> None:
    """tenant_export_jobs and tenant_purges — the lifecycle tables Phase 3
    introduced that still exist — appear in metadata and carry their own map
    entries. (Phase 3's third table was dropped by 06.3 D-01.)"""
    for name in ("tenant_export_jobs", "tenant_purges"):
        assert name in Base.metadata.tables, name
        assert name in PURGE_COVERAGE, name
    assert PURGE_COVERAGE["tenant_export_jobs"] == "purge"
    assert PURGE_COVERAGE["tenant_purges"] == "tombstone"


def test_tenant_object_prefixes_cover_every_user_scoped_key_builder() -> None:
    """Every real user-scoped object-key builder writes under exactly one registered prefix, so a
    writer outside `TENANT_OBJECT_PREFIX_TEMPLATES` -- whose objects the purge would leave
    behind -- fails here (D-16). The registry also cannot over-reach: each prefix ends in the full
    user id and a slash, and none is a prefix of another."""
    import uuid

    from app.models.session import Session
    from app.routes.sessions import _session_content_key
    from app.routes.skills import _file_key
    from app.services.session_shares import session_share_snapshot_key
    from app.services.tenant_export import companion_upload_key
    from app.services.tenant_purge import tenant_object_prefixes

    uid = uuid.UUID("6f1c2d3e-4b5a-4968-8776-a5b4c3d2e1f0")
    prefixes = tenant_object_prefixes(uid)
    keys = {
        "_session_content_key (no origin environment)": _session_content_key(
            Session(user_id=uid, local_session_id="pin-session", origin_environment_id=None)
        ),
        "_session_content_key (origin environment)": _session_content_key(
            Session(
                user_id=uid,
                local_session_id="pin-session",
                origin_environment_id=uuid.UUID("0f9e8d7c-6b5a-4493-8271-605f4e3d2c1b"),
            )
        ),
        "session_share_snapshot_key": session_share_snapshot_key(
            Session(
                user_id=uid,
                id=uuid.UUID("1a2b3c4d-5e6f-4a0b-9c1d-2e3f4a5b6c7d"),
                content_hash="0" * 64,
            )
        ),
        "skills._file_key": _file_key(
            uid, uuid.UUID("2b3c4d5e-6f7a-4b1c-8d2e-3f4a5b6c7d8e"), "pin-skill", "0" * 64
        ),
        "companion_upload_key": companion_upload_key(
            uid,
            uuid.UUID("3c4d5e6f-7a8b-4c2d-9e3f-4a5b6c7d8e9f"),
            "postiz",
            uuid.UUID("4d5e6f7a-8b9c-4d3e-8f4a-5b6c7d8e9fa0"),
        ),
    }
    misplaced = {
        builder: (key, [prefix for prefix in prefixes if key.startswith(prefix)])
        for builder, key in keys.items()
        if len([prefix for prefix in prefixes if key.startswith(prefix)]) != 1
    }
    assert misplaced == {}, f"key builders not under exactly one tenant object prefix: {misplaced}"

    assert all(prefix.endswith(f"{uid}/") for prefix in prefixes), prefixes
    nested = [
        (outer, inner)
        for outer in prefixes
        for inner in prefixes
        if outer != inner and inner.startswith(outer)
    ]
    assert nested == [], f"registry prefixes nest: {nested}"
