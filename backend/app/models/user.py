import uuid
from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import CheckConstraint, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

USER_AVATAR_URL_MAX_LENGTH = 512

PRINCIPAL_KIND_CLERK = "clerk"
PRINCIPAL_KIND_PARTNER_TENANT = "partner_tenant"


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint(
            "partner_tenant_ref",
            name="uq_users_partner_tenant_ref",
        ),
        CheckConstraint(
            "(principal_kind = 'clerk' AND clerk_id IS NOT NULL "
            "AND partner_tenant_ref IS NULL) OR "
            "(principal_kind = 'partner_tenant' AND clerk_id IS NULL "
            "AND clerk_issuer IS NULL AND partner_tenant_ref IS NOT NULL)",
            name="ck_users_principal_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clerk_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    principal_kind: Mapped[str] = mapped_column(
        String(32),
        default=PRINCIPAL_KIND_CLERK,
        server_default=PRINCIPAL_KIND_CLERK,
        nullable=False,
    )
    partner_tenant_ref: Mapped[str | None] = mapped_column(String(255))
    # Existing Clerk rows start with NULL and bind to the configured issuer on
    # their next trusted JWT, create, or termination use. The released global
    # clerk_id uniqueness remains a deliberate single-Clerk-instance compatibility
    # constraint; clerk_issuer scopes lifecycle fences but does not enable
    # simultaneous same-subject users from multiple Clerk instances.
    clerk_issuer: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    name: Mapped[str | None] = mapped_column(String(200))
    # Profile picture URL captured from the Clerk JWT `picture` claim
    # on sign-in. Refreshed every time the user signs in (Clerk rotates
    # the underlying signed URL periodically). Public session-share
    # pages render this so visitors see the owner's avatar in the
    # message stream.
    avatar_url: Mapped[str | None] = mapped_column(String(USER_AVATAR_URL_MAX_LENGTH))
    # Monotonic counter incremented on any skill insert / update /
    # soft-delete (`is_active=False`). Exposed as a collection-level
    # ETag on `GET /v1/skills` and embedded in SSE `skill_changed`
    # event payloads so the daemon can detect missed events when
    # the stream drops mid-flight.
    skills_revision: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    # Billing exemption (06.3 D-01). When set, the billing gate passes this
    # user without any /v2 subscription read. Set for rows grandfathered by
    # the c7e2a9f4d5b8 backfill, admin-provisioned rows and partner-tenant
    # rows; NULL for self-signed-up Clerk users, who must subscribe. The
    # column keeps its historical name because the ACCESS-04 tenant export
    # carries it as a key.
    invite_exempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Billing gate (D-09, enforced in 03-06). Set on first verified
    # active/trialing subscription (or by the D-07 grandfathering backfill).
    # Cleared again once a lapse outlasts the grace period (INT-1, below).
    billing_unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # INT-1 lapse re-check (supersedes the sticky unlock of 06.3 D-03). An
    # unlocked user's subscription is re-read from /v2 at most once per
    # `billing_recheck_interval_seconds`; `billing_checked_at` is the last
    # such read. `billing_lapsed_at` is when a re-read first found it neither
    # active nor trialing; the unlock is cleared once that lapse outlasts
    # `billing_lapse_grace_seconds`, and a paid re-read clears it.
    billing_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    billing_lapsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSetting(Base, TimestampMixin):
    __tablename__ = "user_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    settings: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB, server_default="{}", nullable=False
    )
