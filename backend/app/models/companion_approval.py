"""Durable authorization state for the non-blocking approval gate (TOOL-03, D-08).

Column style, the CHECK-constraint status discriminator, the ``String(64)`` digest column with a
unique index and the ``expires_at`` / ``decided_at`` pair are all carried across verbatim from
``app/models/ai_provider.AiProviderOAuthAttempt``, which is the closest analog this backend
already has: a short-lived record of an out-of-band human step, keyed by a digest, claimed once and
then terminal.

**The one column the analog does NOT have, and the reason this table exists at all.**
``remaining_budget`` is the difference between an approval and a nonce.
``05-RESEARCH.md`` §Pitfall 2 records the finding: a perfect single-use approval token still
permits three destructive writes, because the agent's next move after a refusal is not to replay
the spent token -- it is to request a fresh, fully valid one for the same action.
*"Identifier-local token consumption cannot prevent fresh reissuance unless the issuer retains
monotonic durable state over the authorized action, confirmation event, and remaining execution
budget."* So the durable unit here is the ACTION, not the token: ``canonical_action_sha256`` is
what a request is looked up by, the uniqueness over it is what makes a repeat request REUSE this
row rather than mint a second authorization, and ``remaining_budget`` is what makes one human
decision authorize one execution rather than unlimited executions inside a window.

**The status machine, and which transitions exist.**

| From | To | By |
|---|---|---|
| (none) | ``issued`` | an ``ask`` decision with no continuation presented |
| ``issued`` | ``prepared`` | the tenant approving, from the dashboard and nowhere else |
| ``issued`` | ``denied`` | the tenant denying |
| ``prepared`` | ``committed`` | a redemption spending the last of ``remaining_budget`` |
| ``issued``/``prepared``/``committed`` past ``expires_at`` | ``issued`` | a fresh ask, re-issued |

``denied`` is deliberately absent from that last row. A denial is terminal for this exact action
hash, and re-asking returns the denial rather than a fresh request: turning "no" back into a
prompt is how an agent grinds a person down. The escape hatch is not a second ask but the tool's
own policy row (``app/models/companion_tool_policy.py``), which a tenant can set to ``allow``.

**Re-issue on expiry never returns ``prepared``.** An expired row resets to ``issued`` with a
fresh budget, so the agent must obtain a NEW human decision. Replenishing a budget without one
would be exactly the reissuance Pitfall 2 describes, arriving through the clock instead of through
the token.
"""

import uuid
from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Mirrored from `companion_tool_policy.py` so one companion key and one verb have one width
# everywhere: `COMPANION_KEY_PATTERN` caps a key at 31 characters and `_VERB_NAME_PATTERN` caps a
# verb at 61.
_COMPANION_KEY_LENGTH = 31
_VERB_LENGTH = 61

# The status vocabulary. `issued` is awaiting a human; `prepared` is approved and redeemable;
# `committed` is spent; `denied` and `expired` are terminal.
STATUS_ISSUED = "issued"
STATUS_PREPARED = "prepared"
STATUS_COMMITTED = "committed"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"
APPROVAL_STATUSES = (
    STATUS_ISSUED,
    STATUS_PREPARED,
    STATUS_COMMITTED,
    STATUS_DENIED,
    STATUS_EXPIRED,
)

# One decision, one execution. Higher values are representable so a later surface can offer "let
# this run three times", but the DEFAULT is the only value this plan ships, because a default
# above one would make the phrase "approve this call" mean something other than what a person
# reading it would assume.
DEFAULT_EXECUTION_BUDGET = 1

_STATUS_CHECK = ", ".join(f"'{status}'" for status in APPROVAL_STATUSES)


class CompanionApproval(Base, TimestampMixin):
    """One tenant's authorization for one canonical companion action.

    Rows are addressed by ``canonical_action_sha256`` rather than by identifier on the redemption
    path, and that is the security-relevant choice: the hash is recomputed from the arguments the
    agent PRESENTS, so an approval for ``delete /tmp/x`` cannot be redeemed against ``delete /``
    even with a perfectly valid token in hand (T-5-38).
    """

    __tablename__ = "companion_approvals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nullable because an approval can outlive the Agent it was requested through, and because a
    # row written for an action whose companion is no longer deployed anywhere still has to be
    # visible to the person deciding about it.
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_environments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    companion_key: Mapped[str] = mapped_column(String(_COMPANION_KEY_LENGTH), nullable=False)
    verb: Mapped[str] = mapped_column(String(_VERB_LENGTH), nullable=False)

    canonical_action_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        doc=(
            "SHA-256 over companion key, verb, canonicalized arguments with the façade's control "
            "parameters excluded, tenant and agent. UNIQUE, and the uniqueness is the mechanism: a "
            "repeat request for the same action reuses this row instead of minting a second "
            "authorization."
        ),
    )

    # What the diff a human approved against actually resolved. Re-checked at redemption, so a
    # three-file delete cannot be redeemed as a three-thousand-file one (T-5-39). NULL for a verb
    # whose cap unit is not `records`, where there is no target count to bind and inventing one
    # would make the check a fiction.
    resolved_target_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    remaining_budget: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_EXECUTION_BUDGET,
        server_default=str(DEFAULT_EXECUTION_BUDGET),
        doc=(
            "How many executions this one human decision still authorizes. The column the OAuth "
            "attempt analog does not have, and the one 05-RESEARCH.md Pitfall 2 requires: a "
            "single-use TOKEN does not stop semantic replay, because a re-planning agent requests "
            "a fresh valid token for the same action. Decremented by a conditional UPDATE inside "
            "the same transaction that mints the signed call, so two concurrent redemptions "
            "cannot both spend one remainder."
        ),
    )

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=STATUS_ISSUED, default=STATUS_ISSUED
    )

    # The arguments as the dashboard renders them, already through the SHIPPED audit redactor. Not
    # the hash input: the hash covers the values verbatim, and a redacted copy could not be
    # rehashed. Stored because a person deciding about a call needs to see what it would do, and
    # deriving it from the hash is by construction impossible.
    arguments: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB(none_as_null=True), nullable=False, default=dict
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # SET NULL rather than CASCADE, for the same reason `CompanionDestroyEnablement` uses it:
    # removing the account of whoever answered must not delete the record that it WAS answered.
    decided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_STATUS_CHECK})",
            name="ck_companion_approvals_status",
        ),
        CheckConstraint(
            "remaining_budget >= 0",
            name="ck_companion_approvals_budget",
        ),
        Index(
            "uq_companion_approval_action",
            "canonical_action_sha256",
            unique=True,
        ),
    )
