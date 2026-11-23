"""Back-fill companions:read and companions:invoke onto stored managed runtime keys.

Revision ID: cdadb5203b54
Revises: b8d5f0a31c72
Create Date: 2026-09-23 00:00:00.000000

06-02 widened ``RUNTIME_MCP_SCOPES`` with the companion pair (D-09, WINDOWS #199). A tuple edit
reaches only keys minted afterwards: ``api_keys.scopes`` is a per-row array, so every key the
platform already issued would keep being refused ``missing scope: companions:read``.

The row set is deliberately the runtime bundle's signature and nothing wider (T-06-02-02): a
managed, unrevoked key whose stored scopes contain BOTH ``connectors:read`` and
``connectors:invoke``. Unmanaged keys (user-minted, sometimes deliberately narrowed), NULL-scope
keys (already full access), revoked keys and any managed key that is not a runtime bundle are left
exactly as they are. Each scope is appended only where absent, so re-running is a no-op and a key
minted after the tuple widened is not given a second copy.

``downgrade()`` removes exactly the two scopes from the same row set and nothing else;
``array_remove`` preserves the order of every remaining element.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "cdadb5203b54"
down_revision: str | Sequence[str] | None = "b8d5f0a31c72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Appended in the order `RUNTIME_MCP_SCOPES` declares them: read, then invoke.
_COMPANION_SCOPES = ("companions:read", "companions:invoke")

# The runtime bundle's signature. Literal here rather than imported: a revision must keep meaning
# what it meant when written, whatever the application constants later become.
_RUNTIME_KEY_ROWS = (
    "managed "
    "AND revoked_at IS NULL "
    "AND scopes IS NOT NULL "
    "AND scopes @> ARRAY['connectors:read', 'connectors:invoke']::varchar[]"
)


def upgrade() -> None:
    # One statement per scope, in order, so a key missing both gains read before invoke.
    for scope in _COMPANION_SCOPES:
        op.execute(
            sa.text(
                "UPDATE api_keys "
                "SET scopes = array_append(scopes, CAST(:scope AS varchar)) "
                f"WHERE {_RUNTIME_KEY_ROWS} "
                "AND NOT (CAST(:scope AS varchar) = ANY(scopes))"
            ).bindparams(scope=scope)
        )


def downgrade() -> None:
    for scope in _COMPANION_SCOPES:
        op.execute(
            sa.text(
                "UPDATE api_keys "
                "SET scopes = array_remove(scopes, CAST(:scope AS varchar)) "
                f"WHERE {_RUNTIME_KEY_ROWS} "
                "AND CAST(:scope AS varchar) = ANY(scopes)"
            ).bindparams(scope=scope)
        )
