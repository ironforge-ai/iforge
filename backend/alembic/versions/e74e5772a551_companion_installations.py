"""companion_installations: tenant-installed companions, their credentials and their log

Revision ID: e74e5772a551
Revises: cdadb5203b54
Create Date: 2026-09-23 23:30:00.000000

Phase 06 plan 09 (APP-01, APP-03). A tenant installation is an iforge-owned row keyed by agent
and companion kind rather than an entry in ``hosted_runtime_states.companions``: the provisioner's
runtime-state upsert replaces that whole column and deletes unlisted ``hosted_runtime_secrets``
rows, so an install written there would be erased (research Pattern 1). Every column the phase's
lifecycle plans need is created here so no later plan re-opens this schema.

Platform-generated credentials live in ``companion_installation_secrets`` (CK-D), encrypted with
the vault key, and never as tenant vault items ``/v1/vault/resolve`` could return in plaintext.

``downgrade()`` drops the three tables (children first) and their indexes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e74e5772a551"
down_revision: str | Sequence[str] | None = "cdadb5203b54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHA256 = "~ '^[a-f0-9]{64}$'"
_OPERATIONS = "('install', 'update', 'export', 'uninstall')"


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "companion_installations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("environment_id", sa.UUID(), nullable=False),
        sa.Column("companion_key", sa.String(length=31), nullable=False),
        sa.Column("isolation_tier", sa.String(length=16), nullable=False),
        sa.Column("operation", sa.String(length=16), server_default="install", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("step", sa.String(length=64), nullable=True),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("pinned_digest", sa.String(length=64), nullable=False),
        sa.Column("offered_digest", sa.String(length=64), nullable=True),
        sa.Column("deferred_digest", sa.String(length=64), nullable=True),
        sa.Column("applied_digest", sa.String(length=64), nullable=True),
        sa.Column("hostname_label", sa.String(length=63), nullable=False),
        sa.Column("hostname", sa.String(length=253), nullable=False),
        sa.Column("owner_email", sa.String(length=320), nullable=False),
        sa.Column("quote_id", sa.String(length=128), nullable=True),
        sa.Column("quoted_monthly_usd_micros", sa.BigInteger(), nullable=True),
        sa.Column("quoted_storage_usd_micros_per_gib_month", sa.BigInteger(), nullable=True),
        sa.Column("billing_line_item_id", sa.String(length=200), nullable=True),
        sa.Column("billing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("billing_stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "external_resources",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "backup_state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "operation_started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_report_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_id", sa.UUID(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["environment_id"], ["agent_environments.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("hostname_label", name="uq_companion_installations_hostname_label"),
        sa.CheckConstraint(
            "isolation_tier IN ('standard', 'dedicated', 'shared')",
            name="ck_companion_installations_isolation_tier",
        ),
        sa.CheckConstraint(
            f"operation IN {_OPERATIONS}",
            name="ck_companion_installations_operation",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'complete', 'failed')",
            name="ck_companion_installations_status",
        ),
        sa.CheckConstraint(
            f"pinned_digest {_SHA256}",
            name="ck_companion_installations_pinned_digest",
        ),
        sa.CheckConstraint(
            f"offered_digest IS NULL OR offered_digest {_SHA256}",
            name="ck_companion_installations_offered_digest",
        ),
        sa.CheckConstraint(
            f"deferred_digest IS NULL OR deferred_digest {_SHA256}",
            name="ck_companion_installations_deferred_digest",
        ),
        sa.CheckConstraint(
            f"applied_digest IS NULL OR applied_digest {_SHA256}",
            name="ck_companion_installations_applied_digest",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_companion_installations_attempt_count",
        ),
    )
    op.create_index(
        op.f("ix_companion_installations_user_id"),
        "companion_installations",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_companion_installations_environment_id"),
        "companion_installations",
        ["environment_id"],
        unique=False,
    )
    op.create_index(
        "ix_companion_installations_due",
        "companion_installations",
        ["status", "next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "uq_companion_installations_live",
        "companion_installations",
        ["environment_id", "companion_key"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )

    op.create_table(
        "companion_installation_secrets",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("installation_id", sa.UUID(), nullable=False),
        sa.Column("secret_name", sa.String(length=64), nullable=False),
        sa.Column("encrypted_value", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.String(length=32), server_default="vault.v1", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["installation_id"], ["companion_installations.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "installation_id",
            "secret_name",
            name="uq_companion_installation_secrets_installation_name",
        ),
        sa.CheckConstraint(
            "secret_name ~ '^[a-z][a-z0-9-]{0,63}$'",
            name="ck_companion_installation_secrets_name",
        ),
    )
    op.create_index(
        op.f("ix_companion_installation_secrets_installation_id"),
        "companion_installation_secrets",
        ["installation_id"],
        unique=False,
    )

    op.create_table(
        "companion_install_log_lines",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("installation_id", sa.UUID(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("stream", sa.String(length=8), nullable=False),
        sa.Column("line", sa.String(length=2000), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["installation_id"], ["companion_installations.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "installation_id",
            "seq",
            name="uq_companion_install_log_lines_installation_seq",
        ),
        sa.CheckConstraint(
            f"operation IN {_OPERATIONS}",
            name="ck_companion_install_log_lines_operation",
        ),
        sa.CheckConstraint(
            "stream IN ('stdout', 'stderr')",
            name="ck_companion_install_log_lines_stream",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("companion_install_log_lines")
    op.drop_index(
        op.f("ix_companion_installation_secrets_installation_id"),
        table_name="companion_installation_secrets",
    )
    op.drop_table("companion_installation_secrets")
    op.drop_index("uq_companion_installations_live", table_name="companion_installations")
    op.drop_index("ix_companion_installations_due", table_name="companion_installations")
    op.drop_index(
        op.f("ix_companion_installations_environment_id"),
        table_name="companion_installations",
    )
    op.drop_index(op.f("ix_companion_installations_user_id"), table_name="companion_installations")
    op.drop_table("companion_installations")
