"""The companion dispatch queue -- one row per tool-call ATTEMPT (D-02, T-5-10).

D-02 splits the companion call path: the control plane polices a call and the runtime executes
it. Nothing in this backend could reach a runtime before phase 05 -- every existing runtime flow
is runtime-initiated -- so this table is the durable hand-off between the two halves. A row is
written for every attempt, INCLUDING attempts refused before a companion is ever reached, because
a repudiation record that only covers successful calls is a record of the wrong thing.

Column style, the CHECK-constraint status discriminator, ``expires_at`` and ``poll_claim_id`` are
taken from ``AiProviderOAuthAttempt`` in ``app/models/ai_provider.py`` -- the other place in this
codebase where an independently-polled row is claimed, fenced and completed. Reusing that shape
means the dequeue can reuse its ``with_for_update(skip_locked=True)`` discipline rather than
inventing a second concurrency story.

``arguments`` and ``result`` are JSONB and are written through ``_sanitize_audit_details`` by
``app/services/companion_dispatch.py`` before they land here. The sanitizer is the shipped one,
not a copy: it redacts on ``SECRETISH_KEY_PARTS`` and truncates strings at 500 characters and
sequences at 100 items. The one exception is ``result["body"]``, the companion's answer, which is
stored whole (bounded by the result route's ``MAX_DISPATCH_RESULT_BODY``) because the façade
answers the agent from it; see ``companion_dispatch._stored_result`` (06-07).
"""

import uuid
from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.session import AgentEnvironment
from app.models.user import User

# The lifecycle a dispatch row moves through, mirrored by the CHECK constraint below so an
# unknown status cannot be persisted by any code path, including a future one.
COMPANION_DISPATCH_STATUSES = ("queued", "claimed", "completed", "failed", "expired")

# The statuses from which no further transition is legal.
COMPANION_DISPATCH_TERMINAL_STATUSES = frozenset({"completed", "failed", "expired"})


class CompanionDispatchCall(Base, TimestampMixin):
    __tablename__ = "companion_dispatch_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(User.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    environment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(AgentEnvironment.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    companion_key: Mapped[str] = mapped_column(String(31), nullable=False)
    verb: Mapped[str] = mapped_column(String(61), nullable=False)
    tool_class: Mapped[str] = mapped_column(String(16), nullable=False)
    # The signed call's `jti`. UNIQUE, so a replayed envelope cannot enqueue a second row and a
    # completion can be addressed by the identifier the executor already holds.
    call_jti: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    arguments: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB(none_as_null=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="queued")
    poll_claim_id: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    error_text: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'claimed', 'completed', 'failed', 'expired')",
            name="ck_companion_dispatch_calls_status",
        ),
        CheckConstraint(
            "(status = 'claimed' AND poll_claim_id IS NOT NULL) OR status <> 'claimed'",
            name="ck_companion_dispatch_calls_claim",
        ),
    )
