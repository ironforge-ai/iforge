"""The read-then-diff dry run (TOOL-05, D-11).

**A preview here is a point-in-time READ of the companion, and never a guarantee of the eventual
outcome.** Companion state can change between the read this preview is built from and the call it
previews. ``DRY_RUN_GUARANTEE`` carries that sentence in the returned payload rather than only in
this docstring, because the surface that renders a diff (plan 05-09) must not be able to present
it as a promise without deleting a field to do it.

**Why a read at all.** D-11 chose read-then-diff over a request-shaped preview to avoid a
documented failure mode: a preview computed from the request alone renders a delete of a
nonexistent path identically to a delete of a real one, so it carries no information and an
approval granted against it means nothing. The probe is executed FIRST and its failure is the
first branch in ``compute_dry_run`` -- a probe that 404s or 403s makes the whole dry run an error
result, because an error result is the channel the MCP specification designates for feedback a
model can use to self-correct, and because a failed preview rendered as a clean diff is worse
than no preview.

**Why the probe is a parameter.** ``compute_dry_run`` takes ``execute_probe`` rather than
reaching for the dispatch queue itself, so this module is exercisable without a network, a
database or a runtime, and the assertion that a dry run issues no mutating request can be written
against a real socket instead of a mock's self-report. The façade supplies the real executor,
which runs the probe through the SAME dispatch path an ordinary read takes.

**Which probe.** ``CompanionVerbEntry.read_probe`` is a parser-enforced column: D-13 makes a
``mutate`` or ``destroy`` row without one malformed at the parser, and ``_resolve_read_probes`` in
``app/core/companion_registry.py`` additionally refuses a probe whose own class is not ``read``.
That is what makes "any mutating tool has a preview path" structurally true rather than a
convention this module would otherwise have to check per verb (T-5-13).

**Companion strings are stripped on the way in.** Every name lifted out of a probe response goes
through ``strip_active_markup`` before it enters the diff. The diff leaves through the
structured-content channel as JSON, so it does not carry the untrusted envelope -- which means the
stripping has to happen at the point the bytes cross from companion to façade, or it does not
happen at all for this path. Since 06-07 that point is the kind's argument contract
(``app/services/companion_arguments.py``): its ``dry_run_state`` computes the before and after
state from the probe payload, and this module keeps the shared failure branches in front of it.

**The preview ledger.** A dry run records the target count it RESOLVED, and the façade's default
``records`` resolver composes that with today's single-path count using ``max``. It only ever
RAISES a debit. See ``record_preview`` for why that direction is the only safe one and for what
the composition does NOT close.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.companion_registry import CompanionVerbEntry, companion_verb
from app.services.companion_arguments import (
    MUTATING_TOOL_CLASSES as MUTATING_TOOL_CLASSES,
)
from app.services.companion_arguments import argument_contract_for, argument_contract_kinds

type JsonObject = dict[str, Any]


# Carried IN the payload, not only in prose. The plan's prohibition is explicit -- a preview must
# not be presented as a guarantee of the eventual outcome -- and a field is harder to drop by
# accident than a paragraph in a docstring no renderer reads.
DRY_RUN_GUARANTEE = (
    "This is a point-in-time read of the companion, not a guarantee of the eventual outcome: "
    "state can change between this read and the call it previews."
)

# How long a resolved target count stays usable by a following real call. Short, because it is a
# claim about companion state that ages, and a stale count is a claim about a directory that may
# have grown since.
PREVIEW_TTL_SECONDS = 900.0

# Per PROCESS, and deliberately not a table. See `record_preview` for what that costs and why the
# `max` composition is what makes the cost survivable.
_preview_ledger: dict[tuple[str, str, str], tuple[float, int]] = {}


@dataclass(frozen=True)
class ProbeResponse:
    """What executing one read probe returned.

    ``error`` is set when the probe never produced an HTTP answer at all -- no executor claimed
    it, the runtime reported a failure, or a gate refused it. That is a DIFFERENT condition from
    an HTTP status the companion chose, and flattening the two would tell an agent to fix the
    wrong thing.
    """

    status: int | None
    body: str
    error: str | None = None


@dataclass(frozen=True)
class DryRunResult:
    """One computed preview, or the error that stopped it being computed.

    ``undetermined`` is never implicit. A diff the façade could only partly compute carries the
    entries it DID compute plus an explicit list of what it could not, because the alternative --
    dropping the unknown part and rendering the rest -- produces a complete-LOOKING diff, which is
    the shape an approver would act on most confidently and should trust least.
    """

    tool: str
    tool_class: str
    target: str
    target_count: int = 0
    before: list[JsonObject] = field(default_factory=list)
    after: list[JsonObject] = field(default_factory=list)
    undetermined: list[str] = field(default_factory=list)
    is_error: bool = False
    message: str | None = None

    def structured(self) -> JsonObject:
        """The payload validated against ``DRY_RUN_OUTPUT_SCHEMA`` and returned to the agent."""
        return {
            "tool": self.tool,
            "toolClass": self.tool_class,
            "target": self.target,
            "targetCount": self.target_count,
            "before": list(self.before),
            "after": list(self.after),
            "undetermined": list(self.undetermined),
            "guarantee": DRY_RUN_GUARANTEE,
        }


def read_probe_tool_name(tool_name: str, entry: CompanionVerbEntry) -> str | None:
    """The full ``kind__verb`` name of this verb's read probe, or ``None`` when it declares none.

    The kind is taken from ``tool_name`` rather than from the probe column, which is what makes a
    probe naming another companion's verb unspellable: the column carries a bare verb and this is
    the only place it is qualified.
    """
    if entry.read_probe is None:
        return None
    kind = tool_name.partition("__")[0]
    return f"{kind}__{entry.read_probe}"


async def compute_dry_run(
    tool_name: str,
    verb_entry: CompanionVerbEntry,
    arguments: JsonObject,
    *,
    execute_probe: Callable[[str, JsonObject], Awaitable[ProbeResponse]],
) -> DryRunResult:
    """Read the target through the declared probe, then diff. Errors before it diffs anything.

    The order below is the design. The probe-failure branch is written first and evaluated first,
    because it is the branch D-11 exists for: a dry run that cannot read its target has nothing to
    preview, and returning an empty diff for that case is precisely the failure mode that makes an
    uninformed preview indistinguishable from an informed one.
    """
    kind, _, verb = tool_name.partition("__")
    if kind not in argument_contract_kinds():
        return _error(
            tool_name,
            verb_entry,
            "",
            f"the {kind} companion declares no argument contract, so there is nothing to preview",
        )
    # The TARGET and the PROBE ARGUMENTS are the kind's to name (06-07): a filebrowser preview reads
    # one path, a postiz create_post preview reads one UTC day of scheduled posts. Everything from
    # the probe call down to the non-object branch below stays shared and in its Phase 5 order.
    contract = argument_contract_for(kind)
    target = contract.target_key(verb, arguments)
    probe_arguments = contract.probe_arguments(verb, verb_entry, arguments)
    if not target or probe_arguments is None:
        return _error(tool_name, verb_entry, "", contract.missing_target_message)

    probe_tool = read_probe_tool_name(tool_name, verb_entry)
    if probe_tool is None:
        return _error(
            tool_name,
            verb_entry,
            target,
            f"{tool_name} declares no read probe, so there is nothing to preview it with",
        )

    probe = await execute_probe(probe_tool, probe_arguments)

    if probe.error is not None:
        return _error(
            tool_name,
            verb_entry,
            target,
            f"the {probe_tool} read probe did not answer: {probe.error}",
        )
    if probe.status is None:
        return _error(
            tool_name, verb_entry, target, f"the {probe_tool} read probe reported no HTTP status"
        )
    if probe.status == 404:
        return _error(
            tool_name,
            verb_entry,
            target,
            f"{target} was not found on the companion (HTTP 404), so there is no change to "
            "preview and nothing this call could have done to it",
        )
    if probe.status in {401, 403}:
        return _error(
            tool_name,
            verb_entry,
            target,
            f"the read probe was refused permission to read {target} (HTTP {probe.status}), so "
            "what this call would change could not be determined",
        )
    if probe.status >= 400:
        return _error(
            tool_name,
            verb_entry,
            target,
            f"the {probe_tool} read probe returned HTTP {probe.status} for {target}",
        )

    try:
        payload = json.loads(probe.body)
    except (ValueError, TypeError):
        return _error(
            tool_name,
            verb_entry,
            target,
            f"the {probe_tool} read probe returned a body this façade could not read as JSON",
        )
    if not isinstance(payload, dict):
        return _error(
            tool_name,
            verb_entry,
            target,
            f"the {probe_tool} read probe returned a body that is not a resource description",
        )

    state = contract.dry_run_state(verb, verb_entry, arguments, payload)
    if isinstance(state, str):
        return _error(tool_name, verb_entry, target, state)
    return DryRunResult(
        tool=tool_name,
        tool_class=verb_entry.tool_class,
        target=state.target,
        target_count=state.target_count,
        before=list(state.before),
        after=list(state.after),
        undetermined=list(state.undetermined),
    )


def _error(
    tool_name: str, verb_entry: CompanionVerbEntry, target: str, message: str
) -> DryRunResult:
    return DryRunResult(
        tool=tool_name,
        tool_class=verb_entry.tool_class,
        target=target,
        is_error=True,
        message=message,
    )


# ---------------------------------------------------------------------------
# The preview ledger (T-5-34).
# ---------------------------------------------------------------------------


def record_preview(
    *,
    user_id: object,
    tool_name: str,
    target_key: str,
    target_count: int,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Record what a dry run RESOLVED, so the real call that follows debits that count.

    **What this closes.** Before this plan the façade's default ``records`` resolver returned 1 for
    every bulk-capable verb unless a test injected a replacement, so an approval granted against a
    preview of a thousand entries could be redeemed for a one-record debit. A dry run is the only
    thing in the system that has actually COUNTED, so its count is the one worth carrying.

    **What this does NOT close.** The ledger lives in this process, not in a table, so a second
    worker will not see it and an expired entry is a miss. That is survivable only because the
    façade composes this with the existing single-path count using ``max`` -- a miss therefore
    leaves the debit exactly where it was before this plan, and can never LOWER one. A resolver
    that replaced the count instead of raising it would be one cache miss away from being a budget
    bypass, which is the opposite of what T-5-34 asks for.

    The remaining gap is real and is recorded rather than papered over: a bulk call made WITHOUT a
    preceding dry run still debits a single record. Closing that needs the probe on the real
    call's own path, which costs every mutating call an extra round trip through the runtime and
    is a decision beyond this plan.
    """
    _preview_ledger[_ledger_key(user_id, tool_name, target_key)] = (
        now() + PREVIEW_TTL_SECONDS,
        max(0, int(target_count)),
    )


def previewed_target_count(
    *,
    user_id: object,
    tool_name: str,
    target_key: str,
    now: Callable[[], float] = time.monotonic,
) -> int:
    """The count a recent dry run resolved for this exact tenant, tool and target, or ``0``.

    ``0`` on a miss rather than a raise or a default: the caller composes with ``max``, so zero is
    the identity and a miss is indistinguishable from "no preview raised this debit".
    """
    entry = _preview_ledger.get(_ledger_key(user_id, tool_name, target_key))
    if entry is None:
        return 0
    expires_at, counted = entry
    if expires_at <= now():
        return 0
    return counted


def reset_preview_ledger() -> None:
    """Drop every recorded preview. Tests only -- production lets the TTL do the work."""
    _preview_ledger.clear()


def _ledger_key(user_id: object, tool_name: str, target_key: str) -> tuple[str, str, str]:
    """Keyed by TENANT as well as tool and the kind's canonical target.

    The target is the argument contract's ``target_key`` (06-07): the path for a filebrowser verb,
    the UTC day plus the sorted channel ids for ``postiz__create_post``. Filebrowser keys are
    therefore exactly what they were before the contract existed.

    A key without the tenant would let one account's preview raise -- or, under a different
    composition, lower -- another account's debit, which is a cross-tenant effect for a cache that
    exists only to make one account's own accounting truthful.
    """
    return (str(user_id), tool_name, target_key)


def dry_run_probe_entry(tool_name: str, entry: CompanionVerbEntry) -> CompanionVerbEntry | None:
    """The probe verb's own registry row, or ``None`` when this verb declares no probe."""
    probe_tool = read_probe_tool_name(tool_name, entry)
    if probe_tool is None:
        return None
    return companion_verb(probe_tool)
