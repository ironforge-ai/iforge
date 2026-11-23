"""Wire shapes for the control-plane to runtime companion dispatch channel (D-02).

These two request/response pairs ARE a wire contract: every deployed runtime parses them, so
reshaping one later means the coordinated fleet migration Phase 4's D-08 min-CLI-version gate
exists to manage. They are declared here, in the style of ``app/schemas/audit.py``, rather than
inline in the route, so the contract is one file a reviewer can read end to end.

Every bound below is deliberate. The claim endpoint's ``wait_seconds`` is capped well inside the
CLI proxy's ``MCP_FORWARD_TIMEOUT_MS = 30_000`` budget; the result endpoint's ``body`` is capped
because it is companion-controlled input arriving from inside a tenant network, and an unbounded
string field is an unbounded write into JSONB.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from app.models.companion_tool_call import CallDecision

# The longest a runtime may ask the control plane to hold its claim open. Inside the façade's
# 25-second internal deadline, which is itself inside the 30-second proxy budget: a poll that
# outlived the request waiting on it would return a call nobody is listening for.
MAX_DISPATCH_POLL_SECONDS = 20

# The largest companion response body one result may carry. `companion_sanitize` truncates at
# 64 KiB and discloses it; this is the wire bound in front of that, so an oversized body is
# rejected at the boundary rather than after it has been read into memory and persisted.
MAX_DISPATCH_RESULT_BODY = 256 * 1024


class CompanionDispatchClaimRequest(BaseModel):
    """A runtime asking for the next pre-authorized call for one of its environments."""

    model_config = ConfigDict(extra="forbid")

    environment_id: UUID
    wait_seconds: StrictInt = Field(
        default=MAX_DISPATCH_POLL_SECONDS, ge=0, le=MAX_DISPATCH_POLL_SECONDS
    )


class CompanionDispatchClaimResponse(BaseModel):
    """The next signed call, or an empty body after the poll window closed."""

    model_config = ConfigDict(extra="forbid")

    call: StrictStr | None = None
    claim_id: StrictStr | None = None
    poll_after_seconds: StrictInt = 1


class CompanionDispatchResultRequest(BaseModel):
    """An executor reporting the outcome of a call it claimed."""

    model_config = ConfigDict(extra="forbid")

    call_jti: StrictStr = Field(min_length=1, max_length=64)
    claim_id: StrictStr = Field(min_length=1, max_length=64)
    status: StrictInt | None = Field(default=None, ge=100, le=599)
    body: StrictStr | None = Field(default=None, max_length=MAX_DISPATCH_RESULT_BODY)
    error: StrictStr | None = Field(default=None, max_length=1_000)
    # The `Retry-After` the executor OBSERVED on a 429 from the companion itself, in whole seconds.
    # An additive field, which is what `docs/api-compatibility.md` permits for a released surface:
    # an older runtime that never sends it is unchanged, and a newer one teaches the control
    # plane's bucket the companion's real limit -- see
    # `app/services/companion_ratelimit.observe_retry_after`.
    # Bounded at a day because it is companion-controlled input: an unbounded value would let a
    # misconfigured companion park a tenant's bucket indefinitely.
    retry_after_seconds: StrictInt | None = Field(default=None, ge=0, le=86_400)


class CompanionDispatchResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recorded: StrictBool = True


# ---------------------------------------------------------------------------
# The dashboard approvals surface (D-08, D-09), added by plan 05-06.
#
# Declared here in the style of ``app/schemas/audit.py``, whose route these mirror. What is NOT
# here is the point of the file: there is no continuation field, no action digest and no signing
# material of any kind. The continuation appears only in the refusal text returned to the agent and
# in no column -- the approval gate records a refusal built without it, through
# ``companion_facade._refuse``'s ``logged_message`` (WR-01) -- so there is nothing here for a
# serializer to leak; the action digest IS stored and is deliberately left out, because it is the
# correlation key redemption turns on rather than something a reader needs (T-5-44).
# ---------------------------------------------------------------------------

# Mirrored from `app/models/companion_approval.APPROVAL_STATUSES`. Spelled as a `Literal` rather
# than validated in the handler so an unknown value is a 422 at the boundary, and so the OpenAPI
# schema the dashboard generates against carries the vocabulary.
CompanionApprovalStatus = Literal["issued", "prepared", "committed", "denied", "expired"]


class CompanionApprovalResponse(BaseModel):
    """One pending or decided approval, as the dashboard renders it.

    Constructed field by field from named columns by ``app/routes/companions.py`` -- never by
    serializing the model. That is what keeps a column added later from arriving on the dashboard
    because nobody chose to exclude it.

    ``status`` is the EFFECTIVE status, with expiry applied, because the server refuses an expired
    approval at redemption and at decision time alike. Reporting a stale ``issued`` would put a
    decision in front of a person that the server would then answer with a conflict -- a mode
    displayed that the server does not actually gate on.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    tool: StrictStr
    tool_class: StrictStr
    companion_key: StrictStr
    verb: StrictStr
    # Already through the shipped audit redactor when the row was written, and carried as the
    # `arguments_preview` name rather than `arguments` so no reader mistakes it for the verbatim
    # payload a redemption is hashed against.
    arguments_preview: dict[str, JsonValue]
    resolved_target_count: StrictInt | None
    remaining_budget: StrictInt
    status: CompanionApprovalStatus
    expires_at: datetime
    decided_at: datetime | None
    created_at: datetime


class CompanionApprovalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CompanionApprovalResponse]


# ---------------------------------------------------------------------------
# The dry-run diff's output schema (TOOL-05, D-11).
#
# Declared as a plain JSON Schema object rather than as a Pydantic model because it goes out on
# the wire as an MCP tool's ``outputSchema``, where the consumer is a model reading JSON Schema --
# not a Python caller. Generating it from a model would put a Pydantic-shaped ``$defs`` graph in
# front of that reader for no gain.
#
# Attaching it is what turns "the preview is accurate" from a claim into a SCHEMA-CHECKABLE
# property: the façade validates the diff against this before returning it, so a malformed diff is
# an error rather than a value the agent reads as truth.
# ---------------------------------------------------------------------------

# One entry in a before- or after-state. ``size`` and ``modified`` are nullable rather than
# optional: a companion that reported neither must say so explicitly, because an ABSENT field and a
# field the companion did not supply are indistinguishable to a reader, and the whole point of the
# read-then-diff design is that the before-state carries what a read -- and only a read -- supplies.
_DRY_RUN_ENTRY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "path": {"type": "string"},
        "kind": {"type": "string"},
        "size": {"type": ["integer", "null"]},
        "modified": {"type": ["string", "null"]},
    },
    "required": ["name", "path", "kind", "size", "modified"],
    "additionalProperties": False,
}

DRY_RUN_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "description": (
        "The diff a dry run computed by reading the target through this verb's declared read "
        "probe. A point-in-time read, never a guarantee of the eventual outcome."
    ),
    "properties": {
        "tool": {"type": "string"},
        "toolClass": {"type": "string"},
        "target": {"type": "string"},
        "targetCount": {"type": "integer", "minimum": 0},
        "before": {"type": "array", "items": _DRY_RUN_ENTRY_SCHEMA},
        "after": {"type": "array", "items": _DRY_RUN_ENTRY_SCHEMA},
        "undetermined": {"type": "array", "items": {"type": "string"}},
        "guarantee": {"type": "string"},
    },
    "required": [
        "tool",
        "toolClass",
        "target",
        "targetCount",
        "before",
        "after",
        "undetermined",
        "guarantee",
    ],
    "additionalProperties": False,
}


def validate_dry_run_output(payload: object) -> str | None:
    """Validate a diff against ``DRY_RUN_OUTPUT_SCHEMA``, or name the FIRST thing wrong with it.

    Returns ``None`` when the payload conforms. Hand-rolled over the exact JSON Schema subset the
    schema above uses -- ``type`` (including a type union), ``properties``, ``required``,
    ``additionalProperties: false``, ``items`` and ``minimum`` -- rather than pulling in a
    validator library, because this plan adds no dependency and the alternative candidates were
    rejected on supply-chain grounds in ``05-RESEARCH.md``.

    It names the offending JSON POINTER, not merely that validation failed: a caller that only
    learned "the diff is invalid" would have to re-derive which field to look at, and this
    message is what reaches the agent as an error it can act on.
    """
    return _validate(payload, DRY_RUN_OUTPUT_SCHEMA, "diff")


def _validate(value: object, schema: dict[str, object], pointer: str) -> str | None:
    declared = schema.get("type")
    accepted = declared if isinstance(declared, list) else [declared]
    if not any(_matches(value, str(candidate)) for candidate in accepted):
        return f"{pointer}: expected {declared}, got {type(value).__name__}"
    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        for key in required if isinstance(required, list) else []:
            if key not in value:
                return f"{pointer}.{key}: required property is missing"
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    return f"{pointer}.{key}: undeclared property"
        for key, sub_schema in properties.items():
            if key in value and isinstance(sub_schema, dict):
                problem = _validate(value[key], sub_schema, f"{pointer}.{key}")
                if problem is not None:
                    return problem
    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                problem = _validate(item, item_schema, f"{pointer}[{index}]")
                if problem is not None:
                    return problem
    minimum = schema.get("minimum")
    if isinstance(minimum, int) and isinstance(value, int) and not isinstance(value, bool):
        if value < minimum:
            return f"{pointer}: {value} is below the minimum {minimum}"
    return None


def _matches(value: object, declared: str) -> bool:
    """One JSON Schema primitive type check.

    ``bool`` is excluded from ``integer`` deliberately: Python's ``bool`` is an ``int`` subclass,
    so a ``True`` in a count field would otherwise validate as a number and reach a renderer as
    the target count.
    """
    if declared == "object":
        return isinstance(value, dict)
    if declared == "array":
        return isinstance(value, list)
    if declared == "string":
        return isinstance(value, str)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "null":
        return value is None
    return True


# ---------------------------------------------------------------------------
# The activity log (TOOL-07), added by plan 05-07.
#
# ``CompanionApprovalStatus`` above MIRRORS its model's vocabulary as a ``Literal``. This one
# IMPORTS the enum instead, and the difference is deliberate rather than inconsistent: the plan
# makes ``CallDecision`` the single source of the decision vocabulary, read by the model's CHECK
# constraint, by the writer and by the route filter alike. A mirrored copy here would be a fourth
# place for it to drift, and the one place a drift would be invisible -- an unknown value would
# reach the database and be refused there, long after the dashboard had already offered it as a
# filter.
#
# What is NOT here is the same thing that is not there: no continuation, no signing material.
# The continuation appears in the refusal text returned to the AGENT and in no column: the
# ``error_text`` field below carries the refusal the LOG kept, which the approval gate builds
# without the token (``companion_facade._refuse``'s ``logged_message``, WR-01). So there is nothing
# for a field-by-field construction to pick up by accident (T-5-44).
# ---------------------------------------------------------------------------


class CompanionToolCallResponse(BaseModel):
    """One tool-call attempt as the activity table renders it.

    Every field is one a reader acts on. ``decision`` and ``error_text`` together are the "why
    not" the log exists to answer -- the decision is the bucket a filter facet offers, the reason
    is the sentence underneath it. ``session_id`` is nullable and RENDERED rather than omitted, so
    a renderer can say "not recorded" instead of inferring absence from a missing key.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    created_at: datetime
    session_id: StrictStr | None
    environment_id: UUID | None
    tool: StrictStr
    tool_class: StrictStr
    companion_key: StrictStr
    verb: StrictStr
    decision: CallDecision
    dry_run: StrictBool
    cap_unit: StrictStr | None
    cap_consumed: StrictInt | None
    cap_remaining_after: StrictInt | None
    error_text: StrictStr | None
    parent_call_id: UUID | None
    source_content_ref: StrictStr | None


class CompanionToolCallListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CompanionToolCallResponse]


class CompanionToolCallDetailResponse(CompanionToolCallResponse):
    """One attempt with its whole provenance chain, as the call-detail drawer renders it.

    ``arguments_preview`` carries the name rather than ``arguments`` for the same reason the
    approval response does: the stored payload has already been through the audit redactor, and no
    reader should mistake it for the verbatim bytes a digest was taken over.

    ``source_content_disclosure`` travels WITH ``source_content_ref`` and is present exactly when
    the reference is. The correlation is a heuristic -- the server observed an ordering, not a
    cause -- and a disclosure that had to be remembered by each renderer is a disclosure that will
    eventually be forgotten by one of them.
    """

    model_config = ConfigDict(extra="forbid")

    arguments_digest: StrictStr
    arguments_preview: dict[str, JsonValue]
    call_jti: StrictStr | None
    approval_id: UUID | None
    approved_by_user_id: UUID | None
    source_content_disclosure: StrictStr | None
    # The diff this preview showed, or null when the attempt was not a completed preview. Null and
    # an empty diff are DIFFERENT answers -- "nobody looked" versus "nothing would change" -- and a
    # renderer that collapses them will show the second when it means the first.
    dry_run_diff: dict[str, JsonValue] | None
    # The companion bytes this call returned, still inside the spotlighting envelope the agent
    # received them in. Wrapped on the way OUT rather than handed over raw: the reviewer has to see
    # what the agent saw, and this route would otherwise be the one place in the system where
    # companion-controlled text arrives unframed -- in front of the person least able to tell.
    companion_content: StrictStr | None
    # How many times the call had repeated when the repeated-call breaker stopped it (plan 06-23).
    # Present on a `refused_repeated_call` row and null on every other one: null means "not a
    # breaker stop", never "repeated zero times", and the detail sentence reads it as such.
    repeat_count: StrictInt | None


# ---------------------------------------------------------------------------
# The dashboard settings surface (TOOL-02, TOOL-04), added by plan 05-08a.
#
# What is deliberately ABSENT is the shape of this section. There is no tenant, user, owner or
# environment field on any REQUEST here: the scope is derived from the auth context on every call,
# and a field that does not exist cannot be preferred over it by mistake. There is no combined
# "settings" body either -- policy and caps are independent gates (``app/core/companion_caps.py``
# says so in its own module docstring), and giving them disjoint request models under
# ``extra="forbid"`` is what makes that independence a schema-level fact rather than a convention.
# A body that tried to express one gate in terms of the other cannot be parsed.
#
# And there is no single ``mode`` field on the catalog row. The STORED mode and the mode the server
# would RESOLVE right now are different facts -- ``evaluate_policy`` refuses a destroy verb for a
# companion whose switch is off before it ever reads the row -- so a surface carrying one field
# would necessarily display a mode the server does not gate on. Both travel, with the machine
# reason that produced the second.
# ---------------------------------------------------------------------------

# Mirrored from `app/core/companion_policy.POLICY_MODES`. Spelled as a `Literal` rather than
# imported the way the activity log imports `CallDecision`, because `POLICY_MODES` is a frozenset
# and not an enum -- the `CompanionApprovalStatus` precedent above. The mirror is asserted equal to
# the frozenset by `tests/test_companion_settings.py`, so it cannot drift unnoticed.
CompanionPolicyMode = Literal["allow", "ask", "deny"]


class CompanionToolSettingsResponse(BaseModel):
    """One companion verb as the settings surface renders it.

    ``stored_mode`` is what the tenant saved, or null when no active row exists. Null is neither a
    mode nor a refusal: it means "fall through to the class default", which is what
    ``resolved_reason`` then reports as ``class_default``.

    ``stored_mode`` is typed as a plain string rather than as ``CompanionPolicyMode`` on purpose.
    ``evaluate_policy`` deliberately tolerates a row carrying a mode outside the vocabulary -- the
    CHECK constraint should have refused it, and resolving it to ``ask`` keeps a corrupt row from
    taking the decision down. Typing this field as the closed vocabulary would undo that: the one
    row a tenant most needs to SEE in order to fix would instead 500 the whole catalog.

    ``exposed_to_agent`` is computed through the façade's own ``is_exposed_to_agent`` predicate. It
    is deliberately NOT equal to "this row is present": the catalog shows destroy verbs while they
    are hidden from the agent, because that is the only way the switch that reveals them can be
    explained.
    """

    model_config = ConfigDict(extra="forbid")

    tool: StrictStr
    companion_key: StrictStr
    verb: StrictStr
    tool_class: StrictStr
    cap_unit: StrictStr
    dry_run_available: StrictBool
    stored_mode: StrictStr | None
    resolved_mode: CompanionPolicyMode
    resolved_reason: StrictStr
    destroy_enabled: StrictBool
    exposed_to_agent: StrictBool


class CompanionSummaryResponse(BaseModel):
    """One deployed companion, as the card above its verb list renders it.

    ``has_destroy_verbs`` and ``destroy_enabled`` are separate fields because they are separate
    situations: a companion that declares no destroy verb at all and a companion whose destroy
    group is merely off both show zero usable destroy tools, and a renderer that cannot tell them
    apart will offer a switch that governs nothing.
    """

    model_config = ConfigDict(extra="forbid")

    companion_key: StrictStr
    environment_ids: list[UUID]
    has_destroy_verbs: StrictBool
    destroy_enabled: StrictBool
    # How many of this companion's verbs the AGENT can currently see -- not how many rows the
    # catalog carries for it. The two differ by exactly the destroy group while it is disabled,
    # which is the number the dashboard needs to say "6 tools, 5 visible to your agent".
    visible_verb_count: StrictInt
    # Whether the tenant blocked this companion's whole tool group (plan 06-24, APP-05). While it
    # is true `visible_verb_count` is zero and every call to the group is refused by name; the
    # verbs stay listed in the catalog, because "every tool your agent has" includes blocked ones.
    blocked: StrictBool


class CompanionToolCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    companions: list[CompanionSummaryResponse]
    tools: list[CompanionToolSettingsResponse]


class CompanionToolPolicyRequest(BaseModel):
    """One field, and nothing a cap body could also carry.

    ``extra="forbid"`` plus a single-field model is what makes "policy and caps cannot be expressed
    in terms of each other" checkable at the boundary instead of asserted in prose.
    """

    model_config = ConfigDict(extra="forbid")

    mode: CompanionPolicyMode


class CompanionDestroyEnablementRequest(BaseModel):
    """The per-companion destroy switch, as a position rather than as a toggle.

    ``enabled`` names the position to hold rather than asking for an inversion, so setting the
    switch to where it already is succeeds. A switch that errors on a redundant set cannot be
    driven by a UI that does not first read it, and turns a double-click into an error dialog.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


class CompanionGroupBlockRequest(BaseModel):
    """The per-companion group block, as a position rather than as a toggle (plan 06-24).

    ``blocked`` names the position to hold, for the reason the destroy switch gives: setting the
    block to where it already is succeeds, so a double-click or a second tab is not an error.
    ``StrictBool`` so a JSON string or number cannot coerce into a security control.
    """

    model_config = ConfigDict(extra="forbid")

    blocked: StrictBool


class CompanionCapVectorRequest(BaseModel):
    """All six ceilings, every one required, none with a default.

    Required rather than optional is what makes "caps save as one unit" true. An optional field
    falling back to a default lets a client that forgot one field silently widen a ceiling the
    tenant never touched -- and ``app/core/companion_caps.py`` names reading zero as falsy and
    substituting a default as the single most likely way this guarantee is lost, because it turns
    the tightest possible policy into no policy at all.

    ``StrictInt`` rather than ``int``: a JSON ``true`` is an ``int`` subclass in Python and a
    string ``"0"`` coerces, and either reaching a ceiling would be a cap nobody chose.
    """

    model_config = ConfigDict(extra="forbid")

    per_call_calls: StrictInt = Field(ge=0)
    per_call_records: StrictInt = Field(ge=0)
    per_call_bytes: StrictInt = Field(ge=0)
    per_session_calls: StrictInt = Field(ge=0)
    per_session_records: StrictInt = Field(ge=0)
    per_session_bytes: StrictInt = Field(ge=0)


class CompanionCapVectorResponse(BaseModel):
    """One companion's cap vector, and whether the tenant ever chose it.

    ``is_default`` is derived from ROW EXISTENCE and never by comparing the six values to
    ``DEFAULT_CAPS``. A tenant who read the shipped numbers, agreed with them and saved them made a
    decision; reporting that decision as an absence would show their choice as something nobody
    ever made -- and would additionally make this field's answer change whenever the shipped
    defaults were retuned.

    ``updated_at`` is null exactly when ``is_default`` is true, so the two facts always travel
    together and a renderer never has to infer one from the other.
    """

    model_config = ConfigDict(extra="forbid")

    companion_key: StrictStr
    per_call_calls: StrictInt
    per_call_records: StrictInt
    per_call_bytes: StrictInt
    per_session_calls: StrictInt
    per_session_records: StrictInt
    per_session_bytes: StrictInt
    is_default: StrictBool
    updated_at: datetime | None


class CompanionCapListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CompanionCapVectorResponse]


class CompanionConsumptionItemResponse(BaseModel):
    """What one companion has spent in the window this response names.

    ``session_id`` is present and explicitly null rather than omitted. The MCP protocol as this
    backend speaks it delivers no session identifier to the façade, so every counter today belongs
    to the rolling window rather than to a conversation. Rendering the null is what lets a read-out
    say "not recorded" instead of inferring a scope the server does not meter.

    The three per-session ceilings travel alongside the three counters so a fraction can be
    rendered from ONE response. Two requests whose results are divided by each other are two
    moments in time presented as one.
    """

    model_config = ConfigDict(extra="forbid")

    companion_key: StrictStr
    environment_id: UUID
    session_id: StrictStr | None
    calls_consumed: StrictInt
    records_consumed: StrictInt
    bytes_consumed: StrictInt
    per_session_calls: StrictInt
    per_session_records: StrictInt
    per_session_bytes: StrictInt


class CompanionConsumptionResponse(BaseModel):
    """The live read-out, labelled with the window it actually covers.

    The window is reported whether or not any budget row exists, so an empty ``items`` reads as
    "nothing spent since this time" rather than as "unknown". This phase has collapsed null into a
    neighbouring value three times; an unlabelled empty answer is how it happens a fourth.
    """

    model_config = ConfigDict(extra="forbid")

    window_started_at: datetime
    window_seconds: StrictInt
    items: list[CompanionConsumptionItemResponse]


# ---------------------------------------------------------------------------
# Tenant-installed companions (phase 06, plan 09: APP-01, APP-03).
#
# The response carries NO field that can hold a credential value: ``credential`` is the literal
# ``stored_in_vault`` and nothing else. The platform generates every credential server-side (D-27)
# and keeps it in an iforge-owned encrypted store (CK-D), so there is no value for a serializer to
# leak -- a test asserts this against ``model_fields`` rather than trusting the comment.
# ---------------------------------------------------------------------------

CompanionIsolationTierName = Literal["standard", "dedicated", "shared"]
CompanionInstallationStatus = Literal["queued", "running", "complete", "failed"]
CompanionInstallationOperation = Literal["install", "update", "export", "uninstall"]
# A /v2 quote id travels in a URL path on its way to /v2, so it is an opaque token of URL-safe
# characters and nothing else.
COMPANION_QUOTE_ID_PATTERN = r"^[A-Za-z0-9_-]+$"


class CompanionInstallRequest(BaseModel):
    """Ask for one companion on one of the caller's own agents. No credential field exists."""

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    companion_key: StrictStr = Field(min_length=1, max_length=31)
    isolation_tier: CompanionIsolationTierName = "standard"
    # The quote the tenant was shown (``POST /v1/companions/quotes``, plan 06-17, APP-08). Required:
    # nothing is provisioned before the tenant has seen a current price for exactly this
    # companion, tier and account, and the install verifies it against /v2 before writing a row.
    quote_id: StrictStr = Field(min_length=1, max_length=128, pattern=COMPANION_QUOTE_ID_PATTERN)


class CompanionQuoteRequest(BaseModel):
    """Ask for the price of one companion at one isolation tier (D-07: the tier is the price)."""

    model_config = ConfigDict(extra="forbid")

    companion_key: StrictStr = Field(min_length=1, max_length=31)
    isolation_tier: CompanionIsolationTierName = "standard"


class CompanionQuoteResponse(BaseModel):
    """A /v2 price for one companion and tier, valid until ``expires_at`` (APP-08).

    Amounts are integer micro-dollars (1 USD = 1,000,000), so no price is ever a float.
    """

    model_config = ConfigDict(extra="forbid")

    quote_id: StrictStr
    companion_key: StrictStr
    isolation_tier: CompanionIsolationTierName
    currency: Literal["usd"]
    monthly_price_usd_micros: int
    storage_price_usd_micros_per_gib_month: int
    expires_at: datetime


# What the edge answered on the installation's hostname (plan 06-14's probe, APP-02), plus
# ``no_hostname_yet`` for an installation that is not complete or has not been probed yet. The
# first three mirror ``app/services/companion_edge``'s constants.
CompanionReachabilityState = Literal["reachable", "tls_pending", "not_answering", "no_hostname_yet"]


class CompanionInstallationReachability(BaseModel):
    """The last MEASURED reachability, so the dashboard never infers one (plan 06-34).

    ``checked_at`` is when the probe ran, and is null exactly when ``state`` is
    ``no_hostname_yet``.
    """

    model_config = ConfigDict(extra="forbid")

    state: CompanionReachabilityState
    checked_at: datetime | None


# Where an installation stands against the catalog's current release (plan 06-18, APP-06, D-28).
CompanionUpdateState = Literal[
    "up_to_date", "available", "deferred", "in_progress", "aborted_backup_failed", "failed"
]
# Why the last update failed: its pre-update backup failed (nothing changed), it stopped before the
# swap (nothing changed), Postiz failed its post-update data assertion and was rolled back, or the
# runtime stopped reporting.
CompanionUpdateErrorCode = Literal[
    "backup_failed", "update_aborted", "update_rolled_back", "runtime_did_not_report"
]
COMPANION_DIGEST_PATTERN = r"^[a-f0-9]{64}$"


class CompanionInstallationUpdate(BaseModel):
    """The update card's facts (plan 06-18): pinned by default, a newer digest only an offer.

    ``offered_digest``/``offered_version`` name the catalog release the tenant may accept or defer,
    and are null when there is nothing to decide -- the pin already equals the catalog, the release
    was deferred, or an operation is in flight. ``deferred_digest`` is the one release the tenant
    deferred; a later release is offered again. ``last_backup_at`` is the newest successful backup,
    the one an update is taken after. ``error_code`` explains a failed update and is otherwise null.
    """

    model_config = ConfigDict(extra="forbid")

    state: CompanionUpdateState
    offered_version: StrictStr | None
    offered_digest: StrictStr | None
    deferred_digest: StrictStr | None
    last_backup_at: datetime | None
    error_code: CompanionUpdateErrorCode | None


class CompanionUpdateDecisionRequest(BaseModel):
    """Accept or defer ONE specific offered digest. Any other digest is refused (409)."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "defer"]
    digest: StrictStr = Field(pattern=COMPANION_DIGEST_PATTERN)


class CompanionUninstallRequest(BaseModel):
    """Uninstall one installation, confirmed by typing the companion's name (plan 06-20, APP-07).

    ``confirm`` must equal the catalog's display name exactly -- case-sensitive, after trimming
    surrounding whitespace -- or the request is refused 422 with nothing changed. The check is the
    server's, so no client can skip it: uninstalling destroys the companion's data.
    """

    model_config = ConfigDict(extra="forbid")

    confirm: StrictStr = Field(max_length=200)


class CompanionInstallationResponse(BaseModel):
    """One installation as its owner reads it: job state, pin and address, never a credential."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    agent_id: UUID
    companion_key: StrictStr
    isolation_tier: CompanionIsolationTierName
    operation: CompanionInstallationOperation
    status: CompanionInstallationStatus
    step: StrictStr | None
    error: StrictStr | None
    version: StrictStr
    pinned_digest: StrictStr
    hostname: StrictStr
    owner_email: StrictStr
    credential: Literal["stored_in_vault"] = "stored_in_vault"
    reachability: CompanionInstallationReachability
    update: CompanionInstallationUpdate
    created_at: datetime | None
    updated_at: datetime | None
    completed_at: datetime | None


class CompanionInstallationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CompanionInstallationResponse]


# ---------------------------------------------------------------------------
# Per-companion spend (phase 06, plan 21: OPS-05, D-23).
#
# Every amount is an integer of micro-dollars summed from recorded ledger rows. An amount nothing
# recorded is null, never zero: a zero would claim a measurement that did not happen.
# ---------------------------------------------------------------------------

COMPANION_SPEND_MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"


class CompanionSpendLineResponse(BaseModel):
    """One installation's month-to-date spend, by dimension.

    ``ai_usage_usd_micros`` is always null today: LLM spend a companion itself originates is not
    yet attributed to it (PLAT-03, research A13). It is declared now, as a nullable integer, so the
    attribution lands as a value in an existing field rather than as a new one.
    """

    model_config = ConfigDict(extra="forbid")

    installation_id: UUID
    agent_id: UUID
    companion_key: StrictStr
    isolation_tier: CompanionIsolationTierName
    total_usd_micros: StrictInt | None
    compute_usd_micros: StrictInt | None
    storage_usd_micros: StrictInt | None
    ai_usage_usd_micros: StrictInt | None


class CompanionSpendResponse(BaseModel):
    """The tenant's month-to-date spend: per companion, plus its agents' nested LLM spend.

    ``nested_llm_usd_micros`` is the month's recorded llm rows for the tenant's own sessions --
    the agent runtime's model calls (research A13). ``total_usd_micros`` sums every recorded
    amount above it and is null only when nothing at all was recorded for the month.
    """

    model_config = ConfigDict(extra="forbid")

    month: StrictStr
    currency: Literal["usd"]
    companions: list[CompanionSpendLineResponse]
    nested_llm_usd_micros: StrictInt | None
    total_usd_micros: StrictInt | None


# The largest value `companion_spend_ceilings.ceiling_usd_micros` (BIGINT) can hold. Refused at the
# boundary so an oversized amount is a 422 rather than a database error.
SPEND_CEILING_MAX_USD_MICROS = 2**63 - 1


class CompanionSpendCeilingRequest(BaseModel):
    """The tenant's monthly companion spend ceiling, in integer micro-dollars (OPS-06, D-20).

    ``StrictInt`` with ``ge=0``: a float, a string or a negative amount is a 422 at the boundary.
    Zero is a value meaning "refuse every agent call", as a zero cap is. ``extra="forbid"`` keeps
    the body to exactly this field, so the request cannot name a tenant. The tenant is always the
    caller.
    """

    model_config = ConfigDict(extra="forbid")

    ceiling_usd_micros: StrictInt = Field(ge=0, le=SPEND_CEILING_MAX_USD_MICROS)


class CompanionSpendCeilingResponse(BaseModel):
    """The ceiling that applies to the tenant, where it came from, and this month's standing.

    ``source`` is ``tenant`` for the tenant's own setting, ``operator_default`` for a default the
    operator configured, and ``none`` when no ceiling applies (``ceiling_usd_micros`` is then
    null). ``spent_usd_micros`` is the month's recorded spend (compute, storage and nested LLM)
    plus billed compute hours that have ended but are not yet recorded, and ``reached`` is whether
    that spend is at or over the ceiling, which is when agent tool calls and new installs are
    refused.
    """

    model_config = ConfigDict(extra="forbid")

    ceiling_usd_micros: StrictInt | None
    source: Literal["tenant", "operator_default", "none"]
    spent_usd_micros: StrictInt
    reached: StrictBool
    month: StrictStr


class CompanionLoginLinkResponse(BaseModel):
    """A short-lived link at which the owner sets their own Postiz password (CK-D, plan 06-34).

    ``url`` is Postiz's own set-password page, ``https://<hostname>/auth/forgot/<jwt>``, signed
    with the installation's jwt-secret so that only this installation accepts it. It expires at
    ``expires_at``. ``sign_in_email`` is the account the new password belongs to. The response
    carries no platform-generated credential: the admin password and the API key stay in the
    iforge-owned store.
    """

    model_config = ConfigDict(extra="forbid")

    url: StrictStr
    expires_at: datetime
    sign_in_email: StrictStr


class CompanionCatalogImageResponse(BaseModel):
    """One pinned image: repository, the tag it was resolved from, and its sha256 index digest."""

    model_config = ConfigDict(extra="forbid")

    image: StrictStr
    tag: StrictStr
    sha256: StrictStr


class CompanionCatalogImagesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: CompanionCatalogImageResponse
    postgres: CompanionCatalogImageResponse
    redis: CompanionCatalogImageResponse
    cloudflared: CompanionCatalogImageResponse


class CompanionCatalogTierResponse(BaseModel):
    """Every tier the contract admits, with whether this build offers it (D-06)."""

    model_config = ConfigDict(extra="forbid")

    tier: CompanionIsolationTierName
    offered: StrictBool


class CompanionCatalogItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    companion_key: StrictStr
    display_name: StrictStr
    version: StrictStr
    images: CompanionCatalogImagesResponse
    tiers: list[CompanionCatalogTierResponse]
    # The lowest /v2 monthly price of an offered tier, in USD micros (plan 06-17, APP-08). Null
    # when /v2 prices no offered tier or cannot be reached -- never an invented price.
    from_monthly_price_usd_micros: int | None


class CompanionCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CompanionCatalogItemResponse]


# ---------------------------------------------------------------------------
# The runtime's install report (phase 06, plan 11: APP-01, D-26).
#
# Declared IN FULL here -- every phase the phase-06 lifecycle plans send -- so no later plan has to
# reopen a wire contract every deployed runtime parses. Each phase admits exactly its own payload
# and refuses the others' fields, so a runtime that sends a ``ready`` field on a ``progress``
# report learns it at the boundary instead of having it silently dropped.
#
# The bounds are the log-flood mitigation (T-06-11-04): at most 200 lines of at most 2000
# characters per report. A line is companion-emitted text from inside a tenant network; the
# control plane redacts it against the installation's own credentials before storing it.
# ---------------------------------------------------------------------------

MAX_INSTALL_REPORT_LINES = 200
MAX_INSTALL_LOG_LINE_CHARS = 2000
MAX_INSTALL_REPORT_ERROR_CHARS = 1000
MAX_INSTALL_REPORT_STEP_CHARS = 64

CompanionInstallReportPhase = Literal[
    "progress", "ready", "failed", "backup", "removed", "condition"
]
CompanionInstallLogStream = Literal["stdout", "stderr"]

# The host conditions a runtime can raise and clear (plan 06-44, G-06-2). A condition is host
# health, never a job state (D-26): ``update_rollback_incomplete`` (WR-07: a rollback did not
# finish, so the host needs repair), ``schema_digest_mismatch`` (WR-02: the pinned app would start
# on a schema another digest wrote) and ``network_subnet_unverified`` (WR-06: the companion
# network's live subnet could not be verified against the guard).
CompanionHostConditionCode = Literal[
    "update_rollback_incomplete", "schema_digest_mismatch", "network_subnet_unverified"
]
CompanionHostConditionState = Literal["raised", "cleared"]

_REPORT_PAYLOAD_FIELDS = (
    "step",
    "lines",
    "error",
    "assertion",
    "applied_digest",
    "admin_user_id",
    "backup",
    "condition",
    "to_digest",
)
# Per phase: (required fields, permitted fields). A field outside ``permitted`` is refused.
# ``to_digest`` is permitted (never required) on the three job phases, and only on an update's
# report: ``_payload_matches_phase`` refuses it on any other operation (plan 06-50, G-06-4).
_REPORT_PHASE_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "progress": (frozenset(), frozenset({"step", "lines", "to_digest"})),
    "ready": (
        frozenset({"assertion", "applied_digest", "admin_user_id"}),
        frozenset({"step", "lines", "assertion", "applied_digest", "admin_user_id", "to_digest"}),
    ),
    "failed": (frozenset({"error"}), frozenset({"step", "lines", "error", "to_digest"})),
    "backup": (frozenset({"backup"}), frozenset({"backup"})),
    "removed": (frozenset(), frozenset()),
    "condition": (frozenset({"condition"}), frozenset({"condition"})),
}


class CompanionInstallLogLineReport(BaseModel):
    """One journal line as the runtime read it. Untrusted text, redacted before it is stored."""

    model_config = ConfigDict(extra="forbid")

    stream: CompanionInstallLogStream
    line: StrictStr = Field(max_length=MAX_INSTALL_LOG_LINE_CHARS)


class CompanionReadyAssertion(BaseModel):
    """What the runtime observed Postiz ANSWER with the seeded key -- never an HTTP 200 from /.

    Readiness is a data assertion (the D-16 principle): the companion said it is connected with
    the seeded API key, and exactly one organization exists. After an UPDATE (plan 06-18) the
    runtime also reports ``previous_organizations``, counted just before it swapped the digest, and
    ready means the count is unchanged -- Postiz's start-time schema push lost nothing.
    """

    model_config = ConfigDict(extra="forbid")

    connected: StrictBool
    organizations: StrictInt = Field(ge=0)
    previous_organizations: StrictInt | None = Field(default=None, ge=0)


class CompanionBackupMarkerReport(BaseModel):
    """The sentinel row the runtime wrote just before a backup (plans 06-15, 06-16, 06-18)."""

    model_config = ConfigDict(extra="forbid")

    id: StrictInt = Field(ge=0)
    token: StrictStr = Field(pattern=r"^[a-f0-9]{32}$")
    written_at: datetime


class CompanionBackupReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: StrictBool
    label: StrictStr | None = Field(default=None, max_length=64)
    started_at: datetime
    completed_at: datetime
    db_bytes: StrictInt | None = Field(default=None, ge=0)
    marker: CompanionBackupMarkerReport | None = None


class CompanionHostConditionReport(BaseModel):
    """One host condition raised or cleared by the runtime (plan 06-44, G-06-2).

    ``detail`` is host-composed free text, so it is redacted against the installation's own
    credentials before it is stored and it is never logged. A ``cleared`` condition carries no
    detail: there is nothing left to describe.
    """

    model_config = ConfigDict(extra="forbid")

    code: CompanionHostConditionCode
    state: CompanionHostConditionState
    detail: StrictStr | None = Field(
        default=None, min_length=1, max_length=MAX_INSTALL_REPORT_ERROR_CHARS
    )

    @model_validator(mode="after")
    def _cleared_carries_no_detail(self) -> "CompanionHostConditionReport":
        if self.state == "cleared" and self.detail is not None:
            raise ValueError("a cleared condition cannot carry a detail")
        return self


class CompanionInstallReport(BaseModel):
    """One runtime report about one installation.

    ``progress``, ``ready`` and ``failed`` are JOB phases: they must name the installation's
    current operation and are refused once that operation is terminal. The rest are non-job phases
    accepted in any status that never move the job: ``backup``, because scheduled backups run
    while the install is complete; ``removed``, which carries no payload and is recorded for the
    uninstall pipeline; and ``condition`` (plan 06-44), which raises or clears one host condition
    on the installation (D-26: a condition is host health, not a job state).

    ``to_digest`` (plan 06-50, G-06-4, D-28) is the identity of the update an update job report
    was made for: the app digest that update moves to, 64 lowercase hex, exactly the
    ``toDigest`` the manifest announced. It is permitted on an ``update`` report's ``progress``,
    ``ready`` and ``failed`` phases only. The control plane refuses, writing nothing, a report
    whose ``to_digest`` is not the in-flight update's, so a report the host queued for update N
    across an outage can never fail, move or complete a later update M.

    Two decisions are recorded with it:

    * A report WITHOUT ``to_digest`` is applied to the update in flight exactly as before,
      unchecked. Older CLIs never send it, and refusing their reports would fail every update
      they run at its reporting deadline. The residual lasts until a host runs a CLI that sends
      it (plan 06-52).
    * ``to_digest`` is the WHOLE update identity; no per-acceptance id is added. A stale report
      of a deadline-failed N therefore still matches a later re-acceptance of the same N. The
      impact is low: the host does not retry images it refused while the manifest names them,
      so a re-accepted N ends failed either way, and a stale N ``ready`` over a re-accepted N is
      even the correct outcome.
    """

    model_config = ConfigDict(extra="forbid")

    operation: CompanionInstallationOperation
    phase: CompanionInstallReportPhase
    step: StrictStr | None = Field(
        default=None, min_length=1, max_length=MAX_INSTALL_REPORT_STEP_CHARS
    )
    lines: list[CompanionInstallLogLineReport] = Field(
        default_factory=list, max_length=MAX_INSTALL_REPORT_LINES
    )
    error: StrictStr | None = Field(
        default=None, min_length=1, max_length=MAX_INSTALL_REPORT_ERROR_CHARS
    )
    assertion: CompanionReadyAssertion | None = None
    applied_digest: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    admin_user_id: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    backup: CompanionBackupReport | None = None
    condition: CompanionHostConditionReport | None = None
    to_digest: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def _payload_matches_phase(self) -> "CompanionInstallReport":
        if self.to_digest is not None and self.operation != "update":
            # Only an update has an update identity; install, export and uninstall reports do not.
            raise ValueError(f"an {self.operation} report cannot carry: to_digest")
        required, permitted = _REPORT_PHASE_FIELDS[self.phase]
        present = {
            name
            for name in _REPORT_PAYLOAD_FIELDS
            if (value := getattr(self, name)) is not None and value != []
        }
        missing = sorted(required - present)
        if missing:
            raise ValueError(f"a {self.phase} report requires: {', '.join(missing)}")
        refused = sorted(present - permitted)
        if refused:
            raise ValueError(f"a {self.phase} report cannot carry: {', '.join(refused)}")
        return self


class CompanionInstallReportResponse(BaseModel):
    """What the control plane recorded: the job's status after the report, never a credential."""

    model_config = ConfigDict(extra="forbid")

    status: CompanionInstallationStatus
    step: StrictStr | None
    stored_lines: StrictInt


MAX_INSTALL_LOG_PAGE = 500
DEFAULT_INSTALL_LOG_PAGE = 200


class CompanionInstallLogItemResponse(BaseModel):
    """One stored log line: plain text as the companion wrote it, after storage-time redaction.

    No HTML, no markdown and no transformation beyond the redaction -- a renderer must show it as
    text (UI-SPEC S3), because it is companion-controlled content from inside a tenant network.
    """

    model_config = ConfigDict(extra="forbid")

    seq: StrictInt
    stream: CompanionInstallLogStream
    line: StrictStr
    created_at: datetime


class CompanionInstallLogResponse(BaseModel):
    """A page of the install log after ``after_seq``; pass ``next_after_seq`` to read on."""

    model_config = ConfigDict(extra="forbid")

    items: list[CompanionInstallLogItemResponse]
    next_after_seq: StrictInt
