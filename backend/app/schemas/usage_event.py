"""Request/response shapes for IronHermes usage telemetry ingest.

Mirrors the discipline in `schemas/session.py`: `ge=0` on every numeric
observable, an explicit batch cap derived from THIS table's row width, a
length cap on every free-text field, and the session reference reused rather
than re-declared.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.schemas.session import SafeLocalSessionId, clean_model_value

# `usage_events` binds roughly 12 parameters per inserted row (id, session_id,
# ts, provider, model, in_tok, out_tok, cache_read, cache_create,
# cost_usd_micros, error_kind, event_set_hash). asyncpg refuses queries with
# more than 32767 bound parameters total (PostgreSQL wire protocol limit), so
# the ceiling here is 32767 / 12 ≈ 2730 rows. 2000 leaves ~8700 parameters of
# headroom and is an order of magnitude above the heaviest observed session
# (a couple of hundred events).
#
# This is deliberately NOT the 500 that `SessionBatchRequest` uses: that
# number was calibrated against a ~17-column session row and copying it here
# would silently couple two independent chunking rules. Re-derive this when
# the column count changes.
USAGE_EVENT_BATCH_MAX = 2000

# Untrusted free text. Caps match the model columns so a request that
# validates cannot then fail at the database boundary.
# Deliberately re-declared rather than imported from `schemas/session.py`, whose
# copy is module-private. The pattern is the UTF-16 surrogate block itself — a
# Unicode constant, not a policy choice — so the duplication cannot drift into
# disagreement the way two hand-tuned rules would. See T-02-20.
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

_Provider = Annotated[str, StringConstraints(max_length=100)]
# No `min_length` — `model` fails open exactly as `provider` does (D-18). The
# source column is `model TEXT NOT NULL`, and NOT NULL does not forbid the empty
# string; that is precisely how 56 live rows carry a blank `provider`. Because
# the batch is one request, a lone blank model rejecting it would also discard
# every valid event beside it, and the CLI records no event-set hash on failure —
# so the push would retry and fail identically forever. A call with an
# unrecorded model still burned tokens and still cost money.
_Model = Annotated[str, StringConstraints(max_length=200)]
_ErrorKind = Annotated[str, StringConstraints(max_length=200)]
# SHA-256 hex in practice; capped rather than pattern-matched so the client
# can change its digest without a coordinated server release.
_EventSetHash = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class UsageEventCreate(BaseModel):
    """One recorded model call.

    `ge=0` covers `cost_usd_micros` as well as the token counters. A negative
    cost corrupts a future aggregate exactly as a negative token count does,
    and is the one that is easy to leave off.
    """

    ts: datetime
    # Nullable and blank-tolerant (D-18): the live store holds rows whose
    # provider is the empty string, and those calls burned real tokens.
    provider: _Provider | None = None
    model: _Model
    in_tok: int = Field(default=0, ge=0)
    out_tok: int = Field(default=0, ge=0)
    cache_read: int = Field(default=0, ge=0)
    cache_create: int = Field(default=0, ge=0)
    cost_usd_micros: int = Field(default=0, ge=0)
    # No enum, no Literal. The vocabulary is IronHermes's and provably open —
    # the recorded design named 4 kinds, the live store carries 9 (Pitfall 10).
    error_kind: _ErrorKind | None = None

    @field_validator("provider", "model", mode="before")
    @classmethod
    def _clean_model_like(cls, v: object) -> object:
        # T-02-26, the server half. The adapter parses this field too, but that
        # is not this control's redundant twin — the two disagreed about
        # whitespace, and only this side strips before testing for a blob. The
        # server half also covers the case the client half structurally cannot:
        # an older CLI build, or any principal holding `sessions:write`, posting
        # straight to the endpoint.
        #
        # `usage_events.model` is the same free-text column as `sessions.model`,
        # which has been cleaned here since before this phase — it can hold
        # IronHermes's whole model config including `headers` with API keys, and
        # the dashboard renders it.
        #
        # `mode="before"`, and for a DIFFERENT reason than the surrogate strip
        # below: these fields carry `StringConstraints(max_length=…)`, which
        # pydantic-core enforces before any after-validator runs. A 5000-char
        # config blob would 422 the whole batch, and `push.ts` records no
        # event-set hash on failure, so the daemon would re-send it forever —
        # the same permanent-retry shape that made the surrogate incident cost
        # ~34k retries.
        #
        # `or ""` and not `None`: the column is `model TEXT NOT NULL` and D-18 /
        # 02-REVIEW CR-01 require a blank model to fail OPEN. The batch is one
        # request, so a single unrecoverable model rejecting it would discard
        # every valid event beside it — and that call still burned tokens.
        # No length clamp here: `StringConstraints` owns the cap, so an
        # over-long model NAME is still rejected rather than quietly truncated
        # (T-02-19 / T-02-25).
        if not isinstance(v, str):
            return v
        return clean_model_value(v) or ""

    @field_validator("error_kind", mode="before")
    @classmethod
    def _strip_lone_surrogates(cls, v: object) -> object:
        # Same control as `SessionCreate`, mirrored onto this endpoint (T-02-20).
        # Covers `error_kind` only because `provider` and `model` get the strip
        # inside `clean_model_value` above — one pass over each field, and the
        # surrogate rule still has exactly one definition per grain.
        #
        # A lone UTF-16 surrogate is a valid Python `str` but cannot be encoded
        # as UTF-8, so asyncpg rejects the bound parameter and 500s the whole
        # batch. `schemas/session.py` records what that cost: a stuck daemon
        # retried the bad payload ~34k times in prod before the strip shipped.
        #
        # This endpoint has the identical permanent-retry shape — `push.ts`
        # records no event-set hash on a failed batch, so it re-sends forever —
        # and the CLI's codepoint-safe truncation is NOT sufficient on its own:
        # a surrogate already stored in the source column survives it intact.
        #
        # `mode="before"`, NOT `"after"` as in `session.py`. That difference is
        # load-bearing and not a style choice: this field is
        # `Annotated[str, StringConstraints(...)]`, and the constraint invokes
        # pydantic-core's strict string validation, which rejects a lone
        # surrogate outright with `string_unicode` BEFORE any after-validator
        # runs — so an after-validator here is unreachable code that silently
        # strips nothing. `session.py` gets away with `"after"` only because its
        # `summary` is a plain `str | None`; adding StringConstraints to that
        # field would quietly disable the very control this mirrors.
        #
        # Non-str input is passed through untouched so Pydantic still reports
        # its own type error rather than this validator masking it.
        if not isinstance(v, str):
            return v
        return _LONE_SURROGATE_RE.sub("", v)

    @field_validator("ts", mode="after")
    @classmethod
    def _coerce_to_utc(cls, v: datetime) -> datetime:
        # Same reasoning as `SessionCreate._coerce_to_utc`: a naive datetime
        # blows up any later comparison against `datetime.now(UTC)` with a
        # TypeError that surfaces as a 500. Interpret unmarked values as UTC,
        # the only safe assumption for a timestamp crossing a network.
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v


class UsageEventBatchRequest(BaseModel):
    """One session's WHOLE event set, plus a hash of that set.

    Ingest is replace-by-session, never append (D-22), so the request carries
    the complete set rather than a delta: the source table's event id is a
    local autoincrement that a store rebuild or vacuum can renumber, and
    appending against a renumberable id would silently double-count spend.
    """

    environment_id: uuid.UUID
    # Reused verbatim from `schemas/session.py`, NOT re-declared. This pattern
    # is the control that stops a client smuggling `/` or `..` into the
    # object-store key `sessions/{user_id}/{local_session_id}.json` and
    # escaping its own tenant prefix. IronHermes's composite `agent:main:...`
    # ids are transformed on the CLI side (plan 02-01); the server-side
    # pattern is never relaxed to admit new characters.
    local_session_id: SafeLocalSessionId
    event_set_hash: _EventSetHash
    events: list[UsageEventCreate] = Field(max_length=USAGE_EVENT_BATCH_MAX)


class UsageEventBatchResponse(BaseModel):
    inserted: int
    deleted: int
    # True when the incoming hash matched the stored one and nothing was
    # touched. Distinct from `inserted == 0`, which an empty body also
    # produces.
    unchanged: bool
