"""One-way tenant purge sweep (ACCESS-04 deletion half, D-16).

`PURGE_COVERAGE` gives EVERY tenant-bearing table an explicit deletion
disposition; `tests/test_tenant_lifecycle_completeness.py` (D-17) sweeps
`Base.metadata` and fails CI when a future model lands without one.

Dispositions:

- ``purge``            — rows deleted for the tenant (direct tenant column)
- ``via-parent:<t>``   — rows deleted through the FK chain to ``<t>``
- ``tombstone``        — rows RETAINED so the lifecycle fence survives
                         (Pitfall 5: the fence must outlive the purge)
- ``anonymize``        — rows retained; user refs are SET NULL by FK
- ``global``           — platform-level rows, not tenant data

The purge deletes NO backup data — backups age out on their retention
schedule (D-16); the OPS-04 backup credential/account boundary stays
intact, and this module deliberately contains no backup-target access.
The one backup-adjacent effect is indirect: a companion installation's
teardown (plan 06-20) revokes that installation's own no-delete write key,
through the companion backup module, and deletes no file.

External side effects run FIRST (deployment teardown + subscription
cancel over /v2, provider identity re-delete, and each live companion
installation's line-item stop and resource teardown), then S3 prefixes,
then DB rows in FK-safe child-first order. Every step is idempotent — 404s and
already-missing rows/objects are not errors — so a failed purge can be
retried by the worker until it converges (T-03-24).

The S3 sweep is driven by one registry, `TENANT_OBJECT_PREFIX_TEMPLATES`.
Any new writer of user-scoped objects must register its prefix there in
the same change, or the purge leaves those objects behind with nothing
pointing at them.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any, cast

import httpx
from sqlalchemy import Table, delete, or_, select, tuple_
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.config import settings
from app.models.base import Base
from app.models.tenant_purge import TenantPurge
from app.services.file_store import FileStore
from app.services.user_prov import get_user_provisioning_provider


def _register_all_models() -> None:
    """Import every model module so Base.metadata is fully populated.

    The sweep and the D-17 completeness guard both need the complete
    table inventory, independent of which modules the app has imported.
    """
    import app.models as models_package

    for module in pkgutil.iter_modules(models_package.__path__):
        importlib.import_module(f"app.models.{module.name}")


_register_all_models()

_DEPLOY_API_TIMEOUT_SECONDS = 10.0

# D-16 hosted-teardown branch markers, carried in the purge result and in the
# `tenant.purged` audit details (WR-04).
HOSTED_TEARDOWN_EXECUTED = "executed"
HOSTED_TEARDOWN_SKIPPED_UNCONFIGURED = "skipped:unconfigured_hosted_boundary"

# Test seam (03-04/03-06 transport-injection pattern): tests point this at
# an httpx.MockTransport standing in for /v2. None means the default
# network transport.
_transport_override: httpx.AsyncBaseTransport | None = None

# Column names that mark a table as tenant-scoped. Shared vocabulary with
# the D-17 completeness guard (column-name sweep, NOT FK introspection —
# Memory/Skill/Vault/RuntimeObservation/DeviceAuthorization carry FK-less
# user columns; Pitfall 7).
TENANT_SCOPE_COLUMNS = frozenset(
    {
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
)

# Every user-scoped object-store prefix, each named by the writer that produces keys under it.
# `execute_tenant_purge` sweeps exactly these (D-16); a new writer of user-scoped objects
# registers its prefix here in the same change.
TENANT_OBJECT_PREFIX_TEMPLATES: tuple[str, ...] = (
    # routes/sessions.py `_session_content_key` (the snapshot-v1 upload).
    "sessions/{user_id}/",
    # routes/session_events.py (the events-v1 chunk PUT and the append).
    "session-events/v1/{user_id}/",
    # session_shares.session_share_snapshot_key, written by `create_session_share`.
    "session-share-sources/{user_id}/",
    # routes/skills.py `_file_key`.
    "skills/{user_id}/",
    # The tenant_export_worker artifact and `tenant_export.companion_upload_key`.
    "exports/{user_id}/",
)


def tenant_object_prefixes(user_id: Any) -> tuple[str, ...]:
    """Every user-scoped object prefix for one tenant, from `TENANT_OBJECT_PREFIX_TEMPLATES`."""
    return tuple(template.format(user_id=user_id) for template in TENANT_OBJECT_PREFIX_TEMPLATES)


# Table name -> disposition. Every table flagged by the D-17 column sweep
# must appear here, and so must every table that reaches one of those
# through foreign keys (review finding 6, INT-3: the hosted_runtime,
# channel and 06.1 children carry no tenant column the sweep could flag,
# so they are covered by name, not by luck).
PURGE_COVERAGE: dict[str, str] = {
    # --- direct tenant tables -------------------------------------------
    "users": "purge",
    "user_settings": "purge",
    "agent_environments": "purge",
    "api_keys": "purge",
    "sessions": "purge",
    "session_permissions": "purge",
    "skills": "purge",
    "memories": "purge",  # embeddings are columns of memories — they die with the rows
    "projects": "purge",
    "project_invitations": "purge",
    "project_memberships": "purge",
    "project_share_links": "purge",
    "vaults": "purge",
    "vault_credential_profiles": "purge",
    "device_authorizations": "purge",
    "ai_providers": "purge",
    "ai_provider_auth_payloads": "purge",
    "ai_provider_oauth_attempts": "purge",
    "ai_provider_oauth_revoke_tombstones": "purge",
    "platform_mutation_idempotency": "purge",
    "tenant_export_jobs": "purge",
    "channel_accounts": "purge",
    # Cloud-deleted Session ids the tenant's agents must not re-sync (upstream, sync step 2). Tenant
    # activity keyed on user_id (FK CASCADE), not a platform fact.
    "session_sync_suppressions": "purge",
    # Upstream sync step 3: the search index of a tenant's session messages keys on a NOT NULL
    # user_id. It would also cascade from sessions, but the direct scope is exact.
    "session_message_search": "purge",
    # Upstream sync step 3: sync-subscription leases a tenant's daemons hold; user_id NOT NULL.
    "sync_subscription_leases": "purge",
    # --- transitively tenant-scoped (deleted through the FK chain) ------
    # An llm row keys on session_id alone; a compute or storage row (plan 06-21) carries user_id
    # and no session. `_tenant_scope_condition` takes the union, since user_id is nullable.
    "usage_events": "via-parent:sessions",
    "agent_project_bindings": "via-parent:agent_environments",
    # Upstream sync step 3: public views of a session revision. `created_by` is SET NULL, so the
    # scope is the union of shares the tenant created and shares of the tenant's sessions.
    "session_shares": "via-parent:sessions",
    # Upstream sync steps 3-4: the events-v1 tables -- a session's event generations, the index of
    # their chunk objects, and the append idempotency receipts. None carries a tenant column, so
    # they are named here explicitly rather than covered by the `sessions` FK cascade by luck.
    # The chunk blobs themselves are swept by the `session-events/v1/{user_id}/` entry in
    # `TENANT_OBJECT_PREFIX_TEMPLATES`.
    "session_event_generations": "via-parent:sessions",
    "session_event_chunks": "via-parent:sessions",
    "session_event_append_receipts": "via-parent:sessions",
    # Attempt telemetry keyed on the share link, no user column.
    "share_redeem_attempts": "via-parent:project_share_links",
    "vault_items": "via-parent:vaults",
    # 06.1: pending vault field reservations. No tenant column; named here rather than left to
    # the `vaults` FK cascade by luck (INT-3).
    "vault_secret_requests": "via-parent:vaults",
    # 06.1: an agent's catalog plugin installations and its references to project skills. Neither
    # carries a tenant column, so both are named by the agent they hang off (INT-3).
    "agent_plugin_installations": "via-parent:agent_environments",
    "agent_skill_references": "via-parent:agent_environments",
    "vault_project_attachments": "via-parent:vaults",
    "vault_project_slug_aliases": "via-parent:vaults",
    "hosted_runtime_states": "via-parent:agent_environments",
    # One row per companion tool-call attempt (phase 05). `purge`, not `anonymize`: the row
    # carries the tenant's own `arguments` -- a path inside their filesystem -- and a `result`
    # body of their own file listings, so it is tenant CONTENT and not an audit fact about the
    # platform. The `control_plane_audit_events` trail keeps the fact that calls happened.
    "companion_dispatch_calls": "purge",
    # The tenant's own approval decisions and their destroy switch (phase 05, plan 03). `purge`,
    # not `tombstone`: these say what THIS user chose to let an agent do to THEIR filesystem, which
    # is a preference rather than a platform fact worth retaining after they leave. Retaining them
    # would also be actively wrong on re-signup -- a resurrected `allow` row would silently
    # pre-authorise a write for whoever next holds that identity.
    "companion_tool_policies": "purge",
    "companion_destroy_enablements": "purge",
    # The tenant's companion tool-group blocks (phase 06, plan 24). `purge` for the same reason as
    # the destroy switch beside it: a block is this user's choice about their own agent, not a
    # platform fact, and the `control_plane_audit_events` trail keeps that it was set.
    "companion_group_blocks": "purge",
    # The tenant's blast-radius ceiling and what has been spent against it (phase 05, plan 04).
    # `purge` for the same reason as the rows above: the ceiling is a preference this user set for
    # their own filesystem, and a cap row surviving a purge would silently apply -- or worse, a
    # WIDENED one would silently pre-authorise -- for whoever next holds that identity. The spent
    # counters are tenant activity, not a platform fact; the `control_plane_audit_events` trail
    # keeps the record that calls happened.
    "companion_cap_policies": "purge",
    "companion_session_budgets": "purge",
    # The tenant's monthly companion spend ceiling (phase 06, plan 22). `purge` for the same reason
    # as the cap rows above: it is a preference this user set on their own bill. A ceiling row that
    # survived a purge would silently apply to whoever next holds that identity. The spend it is
    # compared against lives in `usage_events` and is purged there.
    "companion_spend_ceilings": "purge",
    # The repeated-call breaker's counters (phase 06, plan 23). `purge`: each row is a digest of
    # one of this tenant's own calls and how often it repeated, which is tenant activity rather than
    # a platform fact. A surviving cooldown would also stop a call for whoever next holds the
    # identity.
    "companion_repeat_breakers": "purge",
    # The tenant's own approval decisions for individual companion actions (phase 05, plan 06).
    # `purge`, and the reason is sharper than for the rows above: an approval row carries a
    # REMAINING EXECUTION BUDGET, so one surviving a purge in the `prepared` state would be a live
    # authorization to write inside a filesystem, sitting there for whoever next holds that Clerk
    # identity. The `arguments` column additionally holds the tenant's own paths. The
    # `control_plane_audit_events` trail keeps the fact that approvals were requested.
    "companion_approvals": "purge",
    # The provenance log: one row per tool-call attempt (phase 05, plan 07). `purge` for the same
    # reason as the dispatch queue it sits beside -- the row carries the tenant's own sanitized
    # `arguments` (a path inside their filesystem) and the façade's own reason text about their
    # files. It is tenant CONTENT, not a platform fact, and the `control_plane_audit_events` trail
    # keeps the record that calls happened.
    "companion_tool_calls": "purge",
    # Tenant-installed companions (phase 06, plan 09). The installation is the tenant's own
    # application on their own agent -- tenant data, not a platform fact -- so it is purged. Its
    # platform-generated credentials and its unit log carry no tenant column, so they are named
    # by their parent and deleted through the FK chain: a credential row surviving a purge would
    # be a live Postiz password for an application whose owner is gone, and the log lines are
    # output of the tenant's own workload.
    "companion_installations": "purge",
    "companion_installation_secrets": "via-parent:companion_installations",
    "companion_install_log_lines": "via-parent:companion_installations",
    # The companion billing reconciliation's findings about one installation's hosted line
    # (INT-4). No tenant column: named by the installation they hang off, which CASCADEs them.
    "billing_reconcile_findings": "via-parent:companion_installations",
    "hosted_runtime_secrets": "via-parent:hosted_runtime_states",
    "hosted_runtime_config_observations": "via-parent:hosted_runtime_states",
    # Channel children by name (review finding 6). Nearly all carry a
    # user_id column (which the delete machinery uses directly as the
    # superset-safe scope — e.g. onboarding sessions whose account link is
    # SET NULL), but the dispositions record each table's actual parent in
    # the FK chain read from app/models/channel.py.
    "channel_account_runtime_markers": "via-parent:channel_accounts",
    "channel_secrets": "via-parent:channel_accounts",
    "channel_bot_agent_links": "via-parent:channel_accounts",
    "channel_bindings": "via-parent:channel_accounts",
    "channel_binding_aliases": "via-parent:channel_bindings",
    "channel_pair_codes": "via-parent:channel_accounts",
    "channel_messages": "via-parent:channel_accounts",
    "channel_deliveries": "via-parent:channel_messages",
    "channel_debug_events": "via-parent:channel_accounts",
    "channel_attachment_uploads": "via-parent:channel_accounts",
    "channel_scheduled_messages": "via-parent:channel_accounts",
    "channel_agent_references": "via-parent:channel_accounts",
    "channel_agent_credentials": "via-parent:channel_accounts",
    "channel_whatsapp_auth_certs": "via-parent:channel_accounts",
    "channel_whatsapp_onboarding_sessions": "via-parent:channel_accounts",
    # --- retained -------------------------------------------------------
    # The fence must survive the purge: assert_clerk_principal_active
    # keeps rejecting the identity afterwards (Pitfall 5, T-03-23).
    "principal_lifecycles": "tombstone",
    # The purge row is its own audit anchor — marked complete, never erased.
    "tenant_purges": "tombstone",
    # Webhook evidence hangs off the retained lifecycle (FK CASCADE to a
    # row we keep) — part of the fence's durable evidence.
    "clerk_webhook_event_receipts": "tombstone",
    # Ban projection for the fenced subject — retained fence evidence.
    "clerk_principal_authorities": "tombstone",
    # Reversible platform suspension keyed on the Clerk issuer/subject (upstream #1135, sync step
    # 2); user_id is SET NULL on purge. Retained for the same reason as the authorities row above:
    # a suspension must keep applying to the subject after its data is gone.
    "clerk_principal_suspensions": "tombstone",
    # The v2 runtime observation boundary is append-only BY DESIGN: DB
    # triggers (a6d2f4c8b1e7) raise on DELETE for all four tables. The
    # fence rows prevent runtime-environment resurrection (same role as
    # principal_lifecycles for identities); observation payloads age out
    # via RuntimeObservationRetentionWorker's hard-retention compaction —
    # the same age-out-on-retention rule D-16 applies to backups. The
    # rows carry runtime bookkeeping (ids/generations/hashes), not user
    # content.
    "v2_runtime_environment_fences": "tombstone",
    "v2_runtime_observation_inbox": "tombstone",
    "v2_runtime_observation_heads": "tombstone",
    "v2_runtime_observation_consumer_cursors": "tombstone",
    # Audit survives with SET-NULL user refs (the FKs already provide it —
    # this disposition documents the decision).
    "control_plane_audit_events": "anonymize",
    # --- platform-level, not tenant data --------------------------------
    "app_settings": "global",
    # Workload-auth infrastructure: per-service clients/keys/replays for
    # platform workloads, owned by the operator, not by any tenant.
    "platform_workload_clients": "global",
    "platform_workload_signing_keys": "global",
    "platform_workload_assertion_replays": "global",
}

_PURGE_DISPOSITIONS = frozenset({"purge", "tombstone", "anonymize", "global"})


def _tenant_scope_condition(table: Table, user_id: Any) -> ColumnElement[bool] | None:
    """Scope condition selecting one tenant's rows in `table`.

    Direct tenant columns win (a superset of the parent-chain rows — e.g.
    channel_whatsapp_onboarding_sessions whose account link is SET NULL);
    otherwise the via-parent FK chain is followed recursively. When every
    direct column is nullable, a row may carry none of them, so the scope is
    the union of the direct columns and the via-parent chain.
    """
    if table.name == "users":
        return table.c["id"] == user_id
    direct = [table.c[name] for name in sorted(TENANT_SCOPE_COLUMNS) if name in table.c]
    if direct and not all(column.nullable for column in direct):
        return or_(*[column == user_id for column in direct])
    # A direct tenant column that every row may leave NULL is not the whole scope: those rows
    # reach their tenant through the parent chain instead. `usage_events` is the case (plan
    # 06-21): a compute or storage row carries `user_id`, while an llm row leaves it NULL and
    # belongs to its tenant through its session -- so the scope is the union of both.
    direct_condition = or_(*[column == user_id for column in direct]) if direct else None
    parent_scope = _parent_chain_condition(table, user_id)
    if direct_condition is not None and parent_scope is not None:
        return or_(direct_condition, parent_scope)
    return direct_condition if direct_condition is not None else parent_scope


def _parent_chain_condition(table: Table, user_id: Any) -> ColumnElement[bool] | None:
    """The rows of `table` whose via-parent FK points at one of the tenant's parent rows."""
    disposition = PURGE_COVERAGE.get(table.name, "")
    if disposition.startswith("via-parent:"):
        parent_name = disposition.split(":", 1)[1]
        parent = Base.metadata.tables[parent_name]
        parent_condition = _tenant_scope_condition(parent, user_id)
        if parent_condition is None:
            return None
        for constraint in table.foreign_key_constraints:
            if constraint.referred_table.name != parent_name:
                continue
            local_columns = list(constraint.columns)
            referred_columns = [element.column for element in constraint.elements]
            subquery = select(*referred_columns).where(parent_condition)
            if len(local_columns) == 1:
                return local_columns[0].in_(subquery)
            return tuple_(*local_columns).in_(subquery)
    return None


def _deletion_order() -> list[Table]:
    """Tables child-first. The agent_environments <-> projects FK cycle is
    unresolvable for the topological sort (SQLAlchemy warns and ignores
    those constraints); that is safe here because the cycle's FKs are
    ondelete CASCADE / SET NULL, so either relative order deletes cleanly."""
    import warnings

    from sqlalchemy.exc import SAWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SAWarning)
        ordered = list(Base.metadata.sorted_tables)
    return list(reversed(ordered))


async def _delete_covered_rows(db: AsyncSession, user_id: Any) -> dict[str, int]:
    """Delete tenant rows child-first; retain tombstone/anonymize/global."""
    counts: dict[str, int] = {}
    for table in _deletion_order():
        disposition = PURGE_COVERAGE.get(table.name)
        if disposition is None or not (
            disposition == "purge" or disposition.startswith("via-parent:")
        ):
            continue
        condition = _tenant_scope_condition(table, user_id)
        if condition is None:
            continue
        # Core DELETE returns a CursorResult; the generic Result type does
        # not expose rowcount, so narrow explicitly.
        result = cast(CursorResult[Any], await db.execute(delete(table).where(condition)))
        deleted = result.rowcount if result.rowcount and result.rowcount > 0 else 0
        if deleted:
            counts[table.name] = deleted
    return counts


async def _tenant_deployment_ids(db: AsyncSession, user_id: Any) -> list[str]:
    """Deployment ids for teardown, from both linkage sources.

    hosted_runtime_states rows are deleted by the lifecycle CLEANUP stage
    (which runs at fence time, weeks before the purge), so the surviving
    v2_runtime_environment_fences rows — retired but retained — are the
    authoritative post-cleanup source; the states join covers any tenant
    whose cleanup has not run yet.
    """
    from app.models.hosted_runtime import HostedRuntimeState
    from app.models.runtime_observation import V2RuntimeEnvironmentFence
    from app.models.session import AgentEnvironment

    from_states = (
        await db.scalars(
            select(HostedRuntimeState.deployment_id)
            .join(
                AgentEnvironment,
                HostedRuntimeState.environment_id == AgentEnvironment.id,
            )
            .where(AgentEnvironment.user_id == user_id)
        )
    ).all()
    from_fences = (
        await db.scalars(
            select(V2RuntimeEnvironmentFence.deployment_id).where(
                V2RuntimeEnvironmentFence.owner_id == user_id
            )
        )
    ).all()
    return sorted({*from_states, *from_fences})


def _deploy_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.deploy_api_key}",
        "User-Agent": "iforge-backend/1.0",
    }


async def _teardown_deployments(
    client: httpx.AsyncClient, base_url: str, purge: TenantPurge, deployment_ids: list[str]
) -> tuple[list[str], list[str]]:
    """DELETE each deployment via /v2; 404 at any step is already-gone.

    Returns the ids torn down and the ids /v2 answered 404 for. The second list is recorded in
    the `tenant.purged` audit row: a 404 is trusted, so the record is what lets an operator find
    a deployment the billing service wrongly said was gone (e.g. after a billing DB restore).
    """
    torn_down: list[str] = []
    not_found: list[str] = []
    for deployment_id in deployment_ids:
        read = await client.get(
            f"{base_url}/v2/deployments/{deployment_id}", headers=_deploy_headers()
        )
        if read.status_code == httpx.codes.NOT_FOUND:
            not_found.append(deployment_id)
            continue
        read.raise_for_status()
        payload = read.json()
        resource = payload.get("resource") if isinstance(payload, dict) else None
        metadata = resource.get("metadata") if isinstance(resource, dict) else None
        resource_version = metadata.get("resourceVersion") if isinstance(metadata, dict) else None
        headers = {
            **_deploy_headers(),
            # Stable per (purge, deployment): a retried purge re-issues the
            # same operation instead of forking a new one.
            "Idempotency-Key": f"tenant-purge-{purge.id}-{deployment_id}",
        }
        if isinstance(resource_version, str) and resource_version:
            headers["If-Match"] = f'"{resource_version}"'
        response = await client.delete(
            f"{base_url}/v2/deployments/{deployment_id}", headers=headers
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            not_found.append(deployment_id)
            continue
        response.raise_for_status()
        torn_down.append(deployment_id)
    return torn_down, not_found


async def _cancel_subscription(client: httpx.AsyncClient, base_url: str, owner_id: str) -> None:
    """POST /v2/subscription/cancel for the hosted owner `owner_id`.

    The owner is the purge row's `subject`: the Clerk user id captured at
    schedule time, because the users row itself dies in the purge. The hosted
    control plane keys every owner by that id (06.2 D-09), never by the
    internal users.id UUID. Hosted-side semantics are D-23's: the subscription
    ends immediately and invoices are retained. A 404 means there is nothing
    left to cancel and counts as done.
    """
    response = await client.post(
        f"{base_url}/v2/subscription/cancel",
        headers=_deploy_headers(),
        json={"owner_user_id": owner_id},
    )
    if response.status_code == httpx.codes.NOT_FOUND:
        return
    response.raise_for_status()


async def _teardown_companion_installations(db: AsyncSession, user_id: Any) -> list[str]:
    """Stop billing and tear down every live companion installation's resources (plan 06-20).

    For each installation not yet removed: its line item is stopped over /v2 (idempotent; a 404 is
    already stopped), then ``teardown_installation_resources`` undoes every step the installation
    recorded, newest first -- the same teardown an uninstall runs. It all happens while the rows
    still exist, because the rows are what records the resources to undo. Any failure raises, so
    the purge is failed and retried rather than reported complete over orphaned buckets,
    namespaces, tunnels, backup keys or a line item still billing (T-06-20-03, T-06-20-04).
    """
    # Imported here: the lifecycle modules are heavy, and this is the purge's only use of them.
    from app.models.companion_installation import CompanionInstallation
    from app.services.companion_billing import stop_companion_line_item
    from app.services.companion_lifecycle_worker import teardown_installation_resources

    installations = (
        await db.scalars(
            select(CompanionInstallation)
            .where(
                CompanionInstallation.user_id == user_id,
                CompanionInstallation.removed_at.is_(None),
            )
            .order_by(CompanionInstallation.created_at, CompanionInstallation.id)
        )
    ).all()
    torn_down: list[str] = []
    for installation in installations:
        if installation.billing_line_item_id:
            await stop_companion_line_item(
                installation_id=installation.id,
                line_item_id=installation.billing_line_item_id,
            )
        await teardown_installation_resources(db, installation)
        torn_down.append(str(installation.id))
    return torn_down


async def execute_tenant_purge(
    db: AsyncSession,
    file_store: FileStore,
    purge: TenantPurge,
) -> dict[str, Any]:
    """Run the one-way sweep for one claimed purge row.

    Does NOT commit and does NOT mark the row complete — the worker owns
    both, fenced on the claim id. Raises on any non-tolerated failure so
    the worker records failed-with-backoff and a later run retries; every
    step is idempotent so the retry converges.
    """
    if purge.user_id is None:
        raise RuntimeError("tenant purge row has no user id")
    user_id = purge.user_id

    # 0. Resolve the identity provider BEFORE the first side effect. The
    # value is load-validated (config.py), so this is defence in depth — but
    # the ordering is what makes the guarantee independent of how the
    # Settings object was built: a misconfigured seam must not tear down
    # deployments and cancel a subscription and only THEN discover it cannot
    # delete the identity (WR-09's purge-side half).
    identity_provider = get_user_provisioning_provider()

    # 0b. Decide the D-16 hosted-teardown branch explicitly, before any
    # destructive work — an unconfigured hosted boundary used to skip both
    # external calls silently and still reach the completion claim (WR-04).
    if settings.deploy_api_url:
        hosted_teardown = HOSTED_TEARDOWN_EXECUTED
    elif settings.tenant_purge_skip_hosted_teardown:
        hosted_teardown = HOSTED_TEARDOWN_SKIPPED_UNCONFIGURED
    else:
        raise RuntimeError(
            "tenant purge cannot complete: deploy_api_url is unset, so D-16's "
            "subscription cancel (POST /v2/subscription/cancel) and deployment "
            "teardown (DELETE /v2/deployments/{id}) can be neither performed nor "
            "verified. Set DEPLOY_API_URL, or set "
            "TENANT_PURGE_SKIP_HOSTED_TEARDOWN=true to opt this deployment out "
            "explicitly (the skip is then recorded in the tenant.purged audit row)."
        )

    # 1. External calls first — while the enumeration rows still exist.
    deployments_torn_down: list[str] = []
    deployments_not_found: list[str] = []
    if hosted_teardown == HOSTED_TEARDOWN_EXECUTED:
        base_url = settings.deploy_api_url.rstrip("/")
        deployment_ids = await _tenant_deployment_ids(db, user_id)
        async with httpx.AsyncClient(
            transport=_transport_override, timeout=_DEPLOY_API_TIMEOUT_SECONDS
        ) as client:
            deployments_torn_down, deployments_not_found = await _teardown_deployments(
                client, base_url, purge, deployment_ids
            )
            await _cancel_subscription(client, base_url, purge.subject)

    # Identity deletion already ran at trigger time; re-issue 404-tolerantly
    # for the webhook-first order (delete_identity treats 404 as success).
    # Resolution was hoisted above; the CALL stays here, after the /v2
    # enumeration, per the 404-tolerant re-issue rule.
    await identity_provider.delete_identity(purge.subject)

    # 1c. Companion installations (plan 06-20): billing stopped and every external resource torn
    # down through the uninstall's own teardown, BEFORE any row that records them is deleted.
    companion_installations = await _teardown_companion_installations(db, user_id)

    # 2. S3 prefixes (delete_prefix raises on per-key DeleteObjects Errors —
    # 03-02 — so partial S3 failure lands the purge failed, never silently
    # "complete"; T-03-24).
    prefixes: dict[str, int] = {}
    for prefix in tenant_object_prefixes(user_id):
        prefixes[prefix] = await file_store.delete_prefix(prefix)

    # 3. DB rows, child-first, keeping tombstone/anonymize/global tables.
    tables = await _delete_covered_rows(db, user_id)

    return {
        "tables": tables,
        "prefixes": prefixes,
        "deployments": deployments_torn_down,
        "deployments_not_found": deployments_not_found,
        "companion_installations": companion_installations,
        # Which D-16 branch this sweep took. `deployments: []` alone is
        # ambiguous — it means "this tenant had none" under EXECUTED and
        # "we never asked" under the skip.
        "hosted_teardown": hosted_teardown,
    }


__all__ = [
    "HOSTED_TEARDOWN_EXECUTED",
    "HOSTED_TEARDOWN_SKIPPED_UNCONFIGURED",
    "PURGE_COVERAGE",
    "TENANT_OBJECT_PREFIX_TEMPLATES",
    "TENANT_SCOPE_COLUMNS",
    "execute_tenant_purge",
    "tenant_object_prefixes",
]
