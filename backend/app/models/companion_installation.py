"""Tenant-installed companions: the installation, its credentials and its log (APP-01, APP-03).

**Why these are their own tables (research Pattern 1).** The provisioner's runtime-state upsert
replaces the whole ``hosted_runtime_states.companions`` column and deletes every
``hosted_runtime_secrets`` row it did not list, so a tenant install written there would be erased
by the next provisioner write. An installation is therefore an iforge-owned row keyed by agent and
companion kind, merged beside the provisioner-declared companions at projection (plan 06-10). The
assumption-delta decision recorded in plan 06-09 is **add-alongside**: filebrowser stays a
provisioner declaration; when a kind is declared by BOTH sources, the installation wins.

**Why every column is declared now.** Every later lifecycle plan in phase 06 -- the state machine
and its logs, digest pin/defer, billing, backups, export and uninstall -- reads and writes columns
defined here, so no later plan re-opens this schema.

**Where the credentials live (CK-D, ``clawdi-store-set-password-link``).** Platform-generated
credentials are rows of ``companion_installation_secrets``, encrypted with the vault key. They are
NEVER tenant vault items: ``/v1/vault/resolve`` returns those in plaintext to the tenant's CLI and
to any agent key holding ``vault:read``, which would break APP-03 and Phase 5's TOOL-01. No tenant-
or agent-readable API returns a row of that table; the tenant's human signs into Postiz through a
platform-minted 20-minute set-password link instead.

The job vocabulary is the house one (D-26): ``queued``/``running``/``complete``/``failed``, as
``tenant_export`` uses, with the same claim-id fencing and backoff columns.
"""

import uuid
from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    and_,
    func,
    or_,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import ColumnElement

from app.models.base import Base, TimestampMixin

COMPANION_INSTALLATION_STATUS_QUEUED = "queued"
COMPANION_INSTALLATION_STATUS_RUNNING = "running"
COMPANION_INSTALLATION_STATUS_COMPLETE = "complete"
COMPANION_INSTALLATION_STATUS_FAILED = "failed"
COMPANION_INSTALLATION_STATUSES = (
    COMPANION_INSTALLATION_STATUS_QUEUED,
    COMPANION_INSTALLATION_STATUS_RUNNING,
    COMPANION_INSTALLATION_STATUS_COMPLETE,
    COMPANION_INSTALLATION_STATUS_FAILED,
)

COMPANION_OPERATION_INSTALL = "install"
COMPANION_OPERATION_UPDATE = "update"
COMPANION_OPERATION_EXPORT = "export"
COMPANION_OPERATION_UNINSTALL = "uninstall"
COMPANION_INSTALLATION_OPERATIONS = (
    COMPANION_OPERATION_INSTALL,
    COMPANION_OPERATION_UPDATE,
    COMPANION_OPERATION_EXPORT,
    COMPANION_OPERATION_UNINSTALL,
)

# Every tier the contract admits (D-06); which of them this build OFFERS is the catalog's answer.
COMPANION_ISOLATION_TIERS = ("standard", "dedicated", "shared")

COMPANION_LOG_STREAMS = ("stdout", "stderr")

# The active key version for companion credential writes (plan 06-25 moves writes to the active
# keyring version; until then every row is written under the single vault key).
COMPANION_SECRET_KEY_VERSION = "vault.v1"

# A secret atom's name: lowercase, starts with a letter, at most 64 characters.
COMPANION_SECRET_NAME_PATTERN = r"^[a-z][a-z0-9-]{0,63}$"

_SHA256_CHECK = "~ '^[a-f0-9]{64}$'"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class CompanionInstallation(Base, TimestampMixin):
    __tablename__ = "companion_installations"
    __table_args__ = (
        CheckConstraint(
            _in("isolation_tier", COMPANION_ISOLATION_TIERS),
            name="ck_companion_installations_isolation_tier",
        ),
        CheckConstraint(
            _in("operation", COMPANION_INSTALLATION_OPERATIONS),
            name="ck_companion_installations_operation",
        ),
        CheckConstraint(
            _in("status", COMPANION_INSTALLATION_STATUSES),
            name="ck_companion_installations_status",
        ),
        CheckConstraint(
            f"pinned_digest {_SHA256_CHECK}",
            name="ck_companion_installations_pinned_digest",
        ),
        CheckConstraint(
            f"offered_digest IS NULL OR offered_digest {_SHA256_CHECK}",
            name="ck_companion_installations_offered_digest",
        ),
        CheckConstraint(
            f"deferred_digest IS NULL OR deferred_digest {_SHA256_CHECK}",
            name="ck_companion_installations_deferred_digest",
        ),
        CheckConstraint(
            f"applied_digest IS NULL OR applied_digest {_SHA256_CHECK}",
            name="ck_companion_installations_applied_digest",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_companion_installations_attempt_count",
        ),
        UniqueConstraint("hostname_label", name="uq_companion_installations_hostname_label"),
        # Claim scan support: due queued/failed rows plus stale running claims.
        Index("ix_companion_installations_due", "status", "next_attempt_at"),
        # One LIVE installation per agent per companion kind. A removed installation keeps its
        # row (removed_at set) so its history survives, and no longer blocks a reinstall.
        Index(
            "uq_companion_installations_live",
            "environment_id",
            "companion_key",
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    companion_key: Mapped[str] = mapped_column(String(31), nullable=False)
    isolation_tier: Mapped[str] = mapped_column(String(16), nullable=False)
    operation: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=COMPANION_OPERATION_INSTALL,
        server_default=COMPANION_OPERATION_INSTALL,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=COMPANION_INSTALLATION_STATUS_QUEUED,
        server_default=COMPANION_INSTALLATION_STATUS_QUEUED,
    )
    step: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(String(1000))
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    # The app image digest the tenant accepted at request time (D-28 pin by default).
    pinned_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # A newer catalog digest offered to the tenant, one they deferred, and the one last applied.
    offered_digest: Mapped[str | None] = mapped_column(String(64))
    deferred_digest: Mapped[str | None] = mapped_column(String(64))
    applied_digest: Mapped[str | None] = mapped_column(String(64))
    # One generated, non-guessable DNS label; no tenant identifier ever appears in DNS.
    hostname_label: Mapped[str] = mapped_column(String(63), nullable=False)
    hostname: Mapped[str] = mapped_column(String(253), nullable=False)
    owner_email: Mapped[str] = mapped_column(String(320), nullable=False)
    quote_id: Mapped[str | None] = mapped_column(String(128))
    quoted_monthly_usd_micros: Mapped[int | None] = mapped_column(BigInteger)
    quoted_storage_usd_micros_per_gib_month: Mapped[int | None] = mapped_column(BigInteger)
    billing_line_item_id: Mapped[str | None] = mapped_column(String(200))
    billing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billing_stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_resources: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    backup_state: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    operation_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_report_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def installation_is_serving(row: CompanionInstallation) -> bool:
    """Whether this installation's install completed and its companion is serving (plan 06-18).

    ``complete``, or anywhere in an UPDATE operation: an update only ever starts on an installation
    whose install completed, and the previous digest keeps serving until the runtime swaps it --
    and again after a failed update, which is aborted or rolled back. Such an installation keeps
    its tools, its hostname, its login link and its place in its agent's manifest.

    An UNINSTALL never serves (plan 06-20): its tools, hostname and login link go the moment the
    tenant confirms it, and a completed uninstall is a removed installation, not a serving one.
    """
    if row.operation == COMPANION_OPERATION_UNINSTALL:
        return False
    return (
        row.status == COMPANION_INSTALLATION_STATUS_COMPLETE
        or row.operation == COMPANION_OPERATION_UPDATE
    )


def serving_installation_filter() -> ColumnElement[bool]:
    """``installation_is_serving`` as a SQL predicate."""
    return and_(
        CompanionInstallation.operation != COMPANION_OPERATION_UNINSTALL,
        or_(
            CompanionInstallation.status == COMPANION_INSTALLATION_STATUS_COMPLETE,
            CompanionInstallation.operation == COMPANION_OPERATION_UPDATE,
        ),
    )


# Where the uninstall pipeline (plan 06-20) records the host side's end: the runtime's ``removed``
# report, or the deadline passing without one.
REMOVED_REPORTED_AT_KEY = "removed_reported_at"
HOST_REMOVAL_UNCONFIRMED_KEY = "host_removal_unconfirmed"


def uninstall_host_side_settled(row: CompanionInstallation) -> bool:
    """Whether an uninstall no longer needs the runtime: it reported ``removed``, or the deadline
    passed without that report (plan 06-20).

    Until then the manifest carries the installation with ``lifecycle: "remove"`` -- the only
    instruction that destroys its data on the host. Afterwards the entry is dropped: the host is
    already clean, or it stopped answering and the control plane finishes without it.
    """
    if row.operation != COMPANION_OPERATION_UNINSTALL:
        return False
    resources = row.external_resources or {}
    return (
        isinstance(resources.get(REMOVED_REPORTED_AT_KEY), str)
        or resources.get(HOST_REMOVAL_UNCONFIRMED_KEY) is True
    )


class CompanionInstallationSecret(Base, TimestampMixin):
    """One platform-generated credential atom, encrypted with the vault key (D-27, CK-D).

    Mirrors ``HostedRuntimeSecret``'s ciphertext/nonce/key_version columns. No tenant- or
    agent-readable surface returns a row of this table.
    """

    __tablename__ = "companion_installation_secrets"
    __table_args__ = (
        CheckConstraint(
            f"secret_name ~ '{COMPANION_SECRET_NAME_PATTERN}'",
            name="ck_companion_installation_secrets_name",
        ),
        UniqueConstraint(
            "installation_id",
            "secret_name",
            name="uq_companion_installation_secrets_installation_name",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companion_installations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    secret_name: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_value: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=COMPANION_SECRET_KEY_VERSION,
        server_default=COMPANION_SECRET_KEY_VERSION,
    )


class CompanionInstallLogLine(Base):
    """One line of a companion operation's unit log, streamed beside the job state (D-26)."""

    __tablename__ = "companion_install_log_lines"
    __table_args__ = (
        CheckConstraint(
            _in("operation", COMPANION_INSTALLATION_OPERATIONS),
            name="ck_companion_install_log_lines_operation",
        ),
        CheckConstraint(
            _in("stream", COMPANION_LOG_STREAMS),
            name="ck_companion_install_log_lines_stream",
        ),
        UniqueConstraint(
            "installation_id",
            "seq",
            name="uq_companion_install_log_lines_installation_seq",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companion_installations.id", ondelete="CASCADE"),
        nullable=False,
    )
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    stream: Mapped[str] = mapped_column(String(8), nullable=False)
    line: Mapped[str] = mapped_column(String(2000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
