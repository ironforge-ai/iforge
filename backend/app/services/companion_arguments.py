"""Per-kind argument contracts for the companion façade (plan 06-07).

Until phase 06 every companion verb was a filebrowser verb, and the façade's input schema,
argument validation, target counting and dry run all assumed one shape: a companion-relative
``path``. A second kind breaks that assumption. Postiz verbs take no path at all, and
``postiz__create_post`` takes a JSON body of posts. So each kind now owns its argument contract,
and the façade asks the kind's contract instead of assuming a path.

**What a contract answers.** For one verb of its kind:

- ``input_schema``: the JSON Schema the agent sees in ``tools/list``.
- ``invalid``: whether these arguments are acceptable, and if not, a message naming what is wrong.
  The façade refuses ``refused_policy`` on a message, BEFORE any signed call is minted.
- ``dispatch_arguments``: the arguments that travel to the companion, with the façade's control
  parameters (``dry_run``, ``approval``) removed.
- ``target_key``: the canonical identity of what this call addresses. The preview ledger is keyed
  on it, and a dry run reports it as the diff's ``target``.
- ``floor_target_count``: the ``records`` a call debits when no preview counted more.
- ``probe_arguments``: the arguments a dry run sends to the verb's declared read probe.
- ``dry_run_state``: the diff's before, after and undetermined parts, computed from the probe's
  JSON payload. The probe-failure branches stay shared in ``companion_dryrun.compute_dry_run``.
  Every string lifted from a payload goes through ``strip_active_markup`` here, because the diff
  leaves through the structured-content channel, outside the untrusted envelope.

**Every contract refuses what it does not declare.** Each schema sets
``additionalProperties: false``, and ``invalid`` rejects an unknown key rather than ignoring it.
An ignored argument is the worst outcome, because the agent believes it was honoured. No contract
accepts a URL or a host: the host and port come from the registry row, never from an argument
(T-5-07, T-06-07-02).

**The filebrowser contract moved here unchanged.** ``_COMPANION_INPUT_SCHEMA``, the three injected
properties, ``_input_schema_for``, ``_dispatch_arguments``, ``_single_path_target_count`` and
``_invalid_arguments`` are the Phase 5 text, moved byte for byte from ``companion_facade.py``, and
``MUTATING_TOOL_CLASSES`` with the before/after diff helpers moved byte for byte from
``companion_dryrun.py``. Both modules re-export the names their Phase 5 tests import, and every
Phase 5 companion suite is the regression guard for the move.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from pydantic import JsonValue

from app.core.companion_registry import CompanionVerbEntry
from app.services.audit import _sanitize_audit_details  # pyright: ignore[reportPrivateUsage]
from app.services.companion_dispatch import canonical_action_arguments
from app.services.companion_sanitize import strip_active_markup

type JsonObject = dict[str, JsonValue]

# The classes a preview applies to, and the classes the `dry_run` property is injected into
# (D-12). `read` is absent on purpose: a read has nothing to preview, and the closed input schema
# turns `dry_run` on a read verb into a validation REJECTION rather than an argument an agent
# could set forever without effect.
MUTATING_TOOL_CLASSES = frozenset({"create", "mutate", "destroy"})


@dataclass(frozen=True)
class DryRunState:
    """What a kind's contract computed from a successful probe payload.

    ``target`` is what the diff says it addressed; ``target_count`` is what the preview counted
    and what the preview ledger carries into the real call's ``records`` debit.
    """

    target: str
    target_count: int
    before: list[dict[str, Any]] = field(default_factory=list)
    after: list[dict[str, Any]] = field(default_factory=list)
    undetermined: list[str] = field(default_factory=list)


class CompanionArgumentContract(Protocol):
    """One companion kind's argument contract. See the module docstring for each method."""

    companion_key: str
    # What `compute_dry_run` reports when `target_key` or `probe_arguments` names nothing to read.
    missing_target_message: str

    def input_schema(self, verb: str, entry: CompanionVerbEntry) -> dict[str, Any]: ...

    def invalid(
        self, verb: str, entry: CompanionVerbEntry, arguments: JsonObject
    ) -> str | None: ...

    def dispatch_arguments(self, arguments: JsonObject) -> JsonObject: ...

    def target_key(self, verb: str, arguments: JsonObject) -> str | None: ...

    def floor_target_count(
        self, verb: str, entry: CompanionVerbEntry, arguments: JsonObject
    ) -> int: ...

    def probe_arguments(
        self, verb: str, entry: CompanionVerbEntry, arguments: JsonObject
    ) -> JsonObject | None: ...

    def dry_run_state(
        self,
        verb: str,
        entry: CompanionVerbEntry,
        arguments: JsonObject,
        payload: dict[str, Any],
    ) -> DryRunState | str: ...


# ---------------------------------------------------------------------------
# filebrowser: the Phase 5 path contract, moved verbatim from companion_facade.py.
# ---------------------------------------------------------------------------

# The argument contract every companion verb shares for the tracer: filebrowser is uniformly
# path-addressed. Per-verb argument schemas are 05-02's work, together with the remaining five
# verb rows; this schema is deliberately narrow rather than permissive in the meantime.
_COMPANION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Companion-relative path, beginning with '/'. Never a URL: the host and port "
                "come from the registry, so this argument cannot redirect the request."
            ),
            "maxLength": 1024,
        }
    },
    "required": ["path"],
    "additionalProperties": False,
}

# D-12's per-call preview parameter, injected into the input schema of `create`, `mutate` and
# `destroy` verbs and NOTHING else. A per-call parameter rather than a parallel `preview_` tool per
# verb or a session-level mode: it matches the plan-and-apply convention agents already recognise,
# keeps the tool list half the size, and gives the class and cap logic one entry point instead of
# two that could drift.
#
# `additionalProperties: False` above is left exactly as it was, and that is what makes `dry_run`
# on a READ verb a validation rejection rather than an argument quietly ignored -- an ignored
# preview flag is the worst of the three outcomes, because the agent believes it previewed.
_DRY_RUN_PROPERTY: dict[str, Any] = {
    "type": "boolean",
    "description": (
        "Preview this call instead of running it. The façade reads the target through this "
        "verb's declared read probe and returns the diff it computed. Nothing is written, and "
        "the diff is a point-in-time read rather than a guarantee of the eventual outcome."
    ),
    "default": False,
}

# D-08's approval continuation, injected into EVERY companion verb's schema rather than only into
# the mutating ones. `dry_run` is scoped by class because only a write has something to preview;
# `ask` is not, because a tenant can write an explicit `ask` policy row against any class INCLUDING
# `read` (`app/core/companion_policy.py` resolves a corrupt row to `ask` for exactly that reason).
# A verb that could be put behind approval but could not carry a continuation would be a verb no
# decision could ever unblock -- fail-closed, but a dead end rather than a gate.
#
# It is a CONTROL parameter, never forwarded to the companion: see `_dispatch_arguments`.
_APPROVAL_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "The approval continuation a previous refusal of this exact call returned. Retry the "
        "call unchanged with this value once the account owner has approved it. It authorizes "
        "THIS action only: different arguments, or a different number of resolved targets, are "
        "refused."
    ),
    "maxLength": 4096,
}

# WR-06a's second address, injected into the input schema of `mutate` verbs and NOTHING else. A
# `mutate` verb RELOCATES its target, so it must be able to say where to; a `create` verb names one
# target and a `destroy` verb removes one, and neither has a second address.
#
# Before this argument existed, `filebrowser__move_path` admitted only `path`, so a move could not
# state what it would do -- `companion_dryrun._after_state` said exactly that in its `mutate`
# branch -- and TOOL-05's "see exactly what would change" was INEXPRESSIBLE for the one verb that
# most needed it. D-13 makes a probe-less mutating row a parse error precisely so preview would be
# structurally true rather than aspirational; a verb that carries a read probe and still cannot
# name its effect defeats that.
#
# Bounded exactly as `path` is, and for the same reason: it is a new agent-supplied string that
# reaches the companion (T-05-13-01). It is NOT url-shaped and cannot become so -- the host and
# port come from the registry row, never from an argument (T-5-07).
_DESTINATION_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "Where to move the target. Companion-relative, beginning with '/'. Never a URL: the host "
        "and port come from the registry, so this argument cannot redirect the request. It must "
        "differ from 'path' -- a move to its own location changes nothing."
    ),
    "maxLength": 1024,
}

_MAX_ARGUMENT_VALUE_LENGTH = 1024
_MAX_CONTINUATION_LENGTH = 4096


def _input_schema_for(entry: CompanionVerbEntry) -> dict[str, Any]:
    """The shared argument contract, plus the control and class parameters this verb accepts.

    Derived from ``_COMPANION_INPUT_SCHEMA`` by class rather than hand-authored per verb, for the
    same reason ``derive_annotations`` is a lookup: a per-verb copy is a place for the preview
    parameter to be forgotten on the one verb that most needed it.

    ``destination`` is derived the same way, from ``mutate`` and from no other class. A ``mutate``
    verb RELOCATES its target and so must be able to say where to; a ``create`` verb names one
    target and a ``destroy`` verb removes one, and neither has a second address.

    ``required`` is rebuilt as a FRESH list in the mutate branch. The copy above is shallow, so its
    ``required`` value is the module schema's own list object -- appending to it would corrupt
    ``_COMPANION_INPUT_SCHEMA`` for every other verb on the first call, and the corruption would be
    invisible until a read verb started demanding a destination.
    """
    schema = dict(_COMPANION_INPUT_SCHEMA)
    properties = {**_COMPANION_INPUT_SCHEMA["properties"], "approval": dict(_APPROVAL_PROPERTY)}
    if entry.tool_class in MUTATING_TOOL_CLASSES:
        properties["dry_run"] = dict(_DRY_RUN_PROPERTY)
    if entry.tool_class == "mutate":
        properties["destination"] = dict(_DESTINATION_PROPERTY)
        schema["required"] = ["path", "destination"]
    schema["properties"] = properties
    return schema


def _dispatch_arguments(arguments: JsonObject) -> JsonObject:
    """The arguments that actually travel to the companion, with control parameters removed.

    ``dry_run`` and ``approval`` are instructions to THIS façade, not to a filebrowser. Forwarding
    either would put a query parameter on the wire that the registry's path template never
    declared -- and for the continuation it would additionally sign a redeemable bearer token into
    the dispatch envelope and persist it in the dispatch row the activity log renders (T-5-44).

    One helper rather than a filter at each call site: the mint, the refusal record and the
    approval row all need the same answer, and three copies is where the fourth one forgets.
    """
    return canonical_action_arguments(arguments)


def _single_path_target_count(entry: CompanionVerbEntry, arguments: JsonObject) -> int:
    """One path argument resolves to one record: the FLOOR, never the whole answer.

    Deliberately NOT a guess at how many files live under a directory. Inventing a number here
    would make the debit a fiction; returning the honest one that today's narrow argument schema
    admits keeps the accounting truthful.

    Plan 05-05 layered the read probe's resolved count ON TOP of this rather than replacing it --
    see ``_previewed_target_count_resolver`` for why that composition, and only that composition,
    is safe.
    """
    del entry
    return 1 if arguments.get("path") else 0


def _invalid_arguments(arguments: JsonObject, entry: CompanionVerbEntry) -> str | None:
    """Bound the agent-supplied arguments, or name what is wrong with them.

    The agent supplies NO url-shaped argument at all -- host, port, method and path template all
    come from the registry and the signed envelope -- so there is no SSRF parameter here to
    filter (T-5-07). What is left to bound is size and type.

    ``dry_run`` is accepted only where it was injected. The check is by CLASS rather than by a
    list of previewable verbs, so it cannot drift from ``_input_schema_for``. ``approval`` is
    accepted everywhere, for the reason recorded on ``_APPROVAL_PROPERTY``.

    ``destination`` is accepted by a ``mutate`` verb and by no other class, the same polarity
    ``dry_run`` has: a destination on a read or a delete is a REJECTION rather than an argument
    quietly ignored, because an ignored argument is the worst of the three outcomes -- the agent
    believes it was honoured. It then gets the identical bound set ``path`` gets, in the same order
    and with the same message shapes (T-05-13-01): a weaker bound on the newer argument would make
    the older one's hardening decorative. One bound ``path`` does not have is added: a destination
    equal to the path is refused, because a move to its own location changes nothing and previewing
    it as a clean diff would show an approver a change that is not one.
    """
    previewable = entry.tool_class in MUTATING_TOOL_CLASSES
    relocating = entry.tool_class == "mutate"
    accepted = {"path", "approval"}
    if previewable:
        accepted.add("dry_run")
    if relocating:
        accepted.add("destination")
    if set(arguments) - accepted:
        if "dry_run" in arguments and not previewable:
            return (
                "'dry_run' is only accepted by a create, mutate or destroy verb: a read changes "
                "nothing, so it has nothing to preview"
            )
        if "destination" in arguments and not relocating:
            return (
                "'destination' is only accepted by a mutate verb: it is the second address a "
                "relocation needs, and no other class has one"
            )
        if relocating:
            return "only 'path', 'approval', 'dry_run' and 'destination' are accepted"
        return (
            "only 'path' and 'approval' are accepted"
            if not previewable
            else "only 'path', 'approval' and 'dry_run' are accepted"
        )
    if not isinstance(arguments.get("dry_run", False), bool):
        return "'dry_run' must be true or false"
    continuation = arguments.get("approval")
    if continuation is not None:
        if not isinstance(continuation, str) or not continuation:
            return "'approval' must be a non-empty string"
        if len(continuation) > _MAX_CONTINUATION_LENGTH:
            return f"'approval' exceeds {_MAX_CONTINUATION_LENGTH} characters"
    path = arguments.get("path")
    if not isinstance(path, str) or not path:
        return "'path' must be a non-empty string"
    if len(path) > _MAX_ARGUMENT_VALUE_LENGTH:
        return f"'path' exceeds {_MAX_ARGUMENT_VALUE_LENGTH} characters"
    if not path.startswith("/"):
        return "'path' must be companion-relative and begin with '/'"
    if "://" in path or path.startswith("//"):
        return "'path' must not carry a scheme or authority"
    if relocating:
        destination = arguments.get("destination")
        if not isinstance(destination, str) or not destination:
            return "'destination' must be a non-empty string"
        if len(destination) > _MAX_ARGUMENT_VALUE_LENGTH:
            return f"'destination' exceeds {_MAX_ARGUMENT_VALUE_LENGTH} characters"
        if not destination.startswith("/"):
            return "'destination' must be companion-relative and begin with '/'"
        if "://" in destination or destination.startswith("//"):
            return "'destination' must not carry a scheme or authority"
        if destination == path:
            return (
                "'destination' must differ from 'path': a move to its own location changes "
                "nothing, so it is refused rather than previewed as a clean change"
            )
    return None


# The filebrowser diff helpers, moved verbatim from companion_dryrun.py with 06-07's refactor of
# `compute_dry_run`: the before and after state of a preview is the kind's to compute.


def _before_state(target: str, payload: JsonObject) -> list[JsonObject]:
    """The entries the probe actually described, as the diff's before-state.

    A directory resolves to its listed children, because the endpoints these verbs sit on are
    bulk-capable and the thing an approver needs to see is what would be TOUCHED, not the one path
    that was typed. A file resolves to itself.
    """
    items = payload.get("items")
    if payload.get("type") == "directory" and isinstance(items, list):
        return [_entry(target, item) for item in items if isinstance(item, dict)]
    return [_entry(_parent(target), payload)]


def _after_state(
    verb_entry: CompanionVerbEntry,
    target: str,
    before: list[JsonObject],
    *,
    destination: str | None = None,
) -> tuple[list[JsonObject], list[str]]:
    """Apply the verb's declared effect symbolically, and say what could not be applied.

    Each branch below states the limit of what the probe's ONE level of listing can support. A
    branch that guessed past that limit would produce a confident diff about state nobody read.

    ``destination`` is the ``mutate`` branch's second address (05-13, WR-06a). With one, the branch
    performs the same symbolic application ``destroy`` and ``create`` already do: each entry the
    probe listed under ``target`` appears under ``destination`` with its basename preserved and the
    ``size``/``modified`` it was READ with carried through, and the original location is emptied.
    Without one -- reachable only from a caller outside the tool path now, since the input schema
    makes the argument required -- the branch keeps its old undetermined sentence rather than
    inventing a destination.
    """
    undetermined: list[str] = []
    unlisted = [
        f"the contents of {entry['path']} were not listed, so whatever is inside it is not in "
        "this preview"
        for entry in before
        if entry["kind"] == "directory"
    ]

    if verb_entry.tool_class == "destroy":
        return [], unlisted
    if verb_entry.tool_class == "mutate":
        if destination is None:
            undetermined.append(
                "the destination of this move is not one of this call's arguments, so where these "
                "entries would appear afterwards could not be determined"
            )
            return [], undetermined + unlisted
        relocated = [{**entry, "path": _join(destination, str(entry["name"]))} for entry in before]
        return relocated, undetermined + unlisted
    # `create`: the target gains an entry whose size this façade cannot know, because the input
    # schema admits no content argument to measure.
    created = {
        "name": _basename(target),
        "path": target,
        "kind": "unknown",
        "size": None,
        "modified": None,
    }
    undetermined.append(
        f"the size and type of the entry this call would create at {target} are not in its "
        "arguments, so they could not be determined"
    )
    return [*before, created], undetermined + unlisted


def _entry(parent: str, item: JsonObject) -> JsonObject:
    """One before/after entry, with every companion-supplied string stripped of active markup.

    ``size`` and ``modified`` are the fields that make this a READ rather than an echo: neither is
    an argument any companion verb accepts, so a preview carrying them could only have got them
    from the companion.
    """
    name = strip_active_markup(str(item.get("name") or ""))
    size = item.get("size")
    modified = item.get("modified")
    return {
        "name": name,
        "path": _join(parent, name),
        "kind": strip_active_markup(str(item.get("type") or "unknown")),
        "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
        "modified": strip_active_markup(modified) if isinstance(modified, str) else None,
    }


def _join(parent: str, name: str) -> str:
    if not name:
        return parent or "/"
    return f"{parent.rstrip('/')}/{name}"


def _parent(target: str) -> str:
    trimmed = target.rstrip("/")
    head = trimmed.rpartition("/")[0]
    return head or "/"


def _basename(target: str) -> str:
    return target.rstrip("/").rpartition("/")[2] or "/"


class FilebrowserArgumentContract:
    """Phase 5's path contract behind the per-kind seam.

    Every method delegates to the moved Phase 5 text above, so the seam adds no behaviour of its
    own for filebrowser: the target is the path, the probe reads the path, the floor is one record
    per path, and the diff is the one ``compute_dry_run`` computed before the contract existed.
    """

    companion_key = "filebrowser"
    missing_target_message = "the preview needs a 'path' to read"

    def input_schema(self, verb: str, entry: CompanionVerbEntry) -> dict[str, Any]:
        del verb
        return _input_schema_for(entry)

    def invalid(self, verb: str, entry: CompanionVerbEntry, arguments: JsonObject) -> str | None:
        del verb
        return _invalid_arguments(arguments, entry)

    def dispatch_arguments(self, arguments: JsonObject) -> JsonObject:
        return _dispatch_arguments(arguments)

    def target_key(self, verb: str, arguments: JsonObject) -> str | None:
        del verb
        path = arguments.get("path")
        return path if isinstance(path, str) and path else None

    def floor_target_count(
        self, verb: str, entry: CompanionVerbEntry, arguments: JsonObject
    ) -> int:
        del verb
        return _single_path_target_count(entry, arguments)

    def probe_arguments(
        self, verb: str, entry: CompanionVerbEntry, arguments: JsonObject
    ) -> JsonObject | None:
        del entry
        target = self.target_key(verb, arguments)
        return None if target is None else {"path": target}

    def dry_run_state(
        self,
        verb: str,
        entry: CompanionVerbEntry,
        arguments: JsonObject,
        payload: dict[str, Any],
    ) -> DryRunState | str:
        target = self.target_key(verb, arguments) or ""
        before = _before_state(target, payload)
        if not before:
            return f"the read probe described nothing at {target}, so there is no change to preview"
        destination = arguments.get("destination")
        after, undetermined = _after_state(
            entry,
            target,
            before,
            destination=destination if isinstance(destination, str) and destination else None,
        )
        return DryRunState(
            target=target,
            target_count=len(before),
            before=before,
            after=after,
            undetermined=undetermined,
        )


# ---------------------------------------------------------------------------
# postiz: the curated Postiz toolset's contract (06-06 rows, 06-07 contract).
#
# Field names and the `type` vocabulary are Postiz v2.24.0's own, read from the pinned DTOs:
# `libraries/nestjs-libraries/src/dtos/posts/create.post.dto.ts` (CreatePostDto, Post,
# PostContent, Integration, Tags), `dtos/media/media.dto.ts` (MediaDto) and
# `dtos/posts/get.posts.dto.ts` (GetPostsDto). The BOUNDS are this façade's, not Postiz's: Postiz
# declares no size limits, and an agent-supplied body with none is a tampering surface
# (T-06-07-03).
# ---------------------------------------------------------------------------

POSTIZ_COMPANION_KIND = "postiz"

# The read verbs that take no argument at all. Postiz's controller accepts an optional `group`
# query on `/integrations` and a `page` on `/notifications`; the curated toolset exposes neither,
# so an agent cannot page or filter through them.
POSTIZ_NO_ARGUMENT_VERBS = frozenset(
    {"list_integrations", "is_connected", "list_groups", "list_notifications"}
)

# The `type` values that CREATE. CreatePostDto also admits `update`, which edits an existing post
# group. That is a mutation, and `create_post` is a `create`-class verb: admitting `update` here
# would let an edit ride through the create class's policy and preview, so it is refused.
POSTIZ_CREATE_TYPES = ("draft", "schedule", "now")

POSTIZ_ID_MAX_LENGTH = 64
POSTIZ_MAX_TAGS = 20
POSTIZ_TAG_FIELD_MAX_LENGTH = 64
POSTIZ_MAX_POST_ITEMS = 20
POSTIZ_MAX_VALUE_ENTRIES = 20
POSTIZ_MAX_IMAGES = 4
POSTIZ_IMAGE_PATH_MAX_LENGTH = 500
POSTIZ_SETTINGS_MAX_BYTES = 16 * 1024
POSTIZ_LIST_WINDOW_MAX = timedelta(days=31)
POSTIZ_PREVIEW_CONTENT_CHARACTERS = 200

# A post item's content bound. Postiz accepts far longer content, and the plan asked for 10 000
# characters. But the dispatch row stores every argument string through the audit redactor, which
# clips strings at 500 characters, and `enqueue_call` refuses a call whose stored arguments differ
# from the validated ones (CR-01). A bound above 500 would advertise content the pipeline refuses
# after the gates, and a preview would show a post the real call then refuses. So the contract
# advertises the bound the pipeline can actually carry. Raising it needs the dispatch row to store
# long arguments unchanged, which is a change to `companion_dispatch.enqueue_call`, not here.
POSTIZ_CONTENT_MAX_LENGTH = 500

# One UTC instant in the ISO-8601 form Postiz's `@IsDateString` accepts, restricted to `Z`. An
# offset form would be a second spelling of the same instant, and the preview's UTC day and the
# approval digest both need one spelling.
_UTC_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")

# An id is an opaque Postiz identifier. It never carries a path separator, a scheme or authority
# separator, or whitespace, so no id can be read as an address (T-06-07-02).
_POSTIZ_ID = re.compile(r"^[^/:\s]+$")

_POSTIZ_UTC_INSTANT_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": "A UTC instant in ISO-8601 form ending in 'Z', e.g. 2026-10-01T09:00:00Z.",
    "pattern": _UTC_INSTANT.pattern,
    "maxLength": 32,
}


def _postiz_id_property(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "description": description,
        "minLength": 1,
        "maxLength": POSTIZ_ID_MAX_LENGTH,
        "pattern": _POSTIZ_ID.pattern,
    }


_LIST_POSTS_PROPERTIES: dict[str, Any] = {
    "startDate": {
        **_POSTIZ_UTC_INSTANT_PROPERTY,
        "description": "Start of the window, a UTC instant ending in 'Z'.",
    },
    "endDate": {
        **_POSTIZ_UTC_INSTANT_PROPERTY,
        "description": "End of the window, after startDate and at most 31 days later.",
    },
    "customer": _postiz_id_property("Optional customer (group) id to filter by."),
}

_CREATE_POST_PROPERTIES: dict[str, Any] = {
    "type": {
        "type": "string",
        "enum": list(POSTIZ_CREATE_TYPES),
        "description": "'draft' saves without publishing, 'schedule' publishes at 'date', "
        "'now' publishes immediately.",
    },
    "date": {**_POSTIZ_UTC_INSTANT_PROPERTY, "description": "When to publish, UTC, ending in 'Z'."},
    "shortLink": {"type": "boolean", "description": "Whether Postiz shortens links."},
    "tags": {
        "type": "array",
        "maxItems": POSTIZ_MAX_TAGS,
        "items": {
            "type": "object",
            "properties": {
                "value": {"type": "string", "maxLength": POSTIZ_TAG_FIELD_MAX_LENGTH},
                "label": {"type": "string", "maxLength": POSTIZ_TAG_FIELD_MAX_LENGTH},
            },
            "required": ["value", "label"],
            "additionalProperties": False,
        },
    },
    "posts": {
        "type": "array",
        "minItems": 1,
        "maxItems": POSTIZ_MAX_POST_ITEMS,
        "description": "One item per channel. Each item debits one record.",
        "items": {
            "type": "object",
            "properties": {
                "integration": {
                    "type": "object",
                    "properties": {
                        "id": _postiz_id_property("The channel id, from postiz__list_integrations.")
                    },
                    "required": ["id"],
                    "additionalProperties": False,
                },
                "value": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": POSTIZ_MAX_VALUE_ENTRIES,
                    "description": "The post, then any thread entries after it.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "maxLength": POSTIZ_CONTENT_MAX_LENGTH},
                            "image": {
                                "type": "array",
                                "maxItems": POSTIZ_MAX_IMAGES,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": _postiz_id_property("An uploaded media id."),
                                        "path": {
                                            "type": "string",
                                            "description": "The uploaded media's path on this "
                                            "Postiz, beginning with '/'. Never a URL.",
                                            "maxLength": POSTIZ_IMAGE_PATH_MAX_LENGTH,
                                        },
                                    },
                                    "required": ["id", "path"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["content"],
                        "additionalProperties": False,
                    },
                },
                "settings": {
                    "type": "object",
                    "description": "Provider settings, at most 16 KiB of JSON.",
                },
            },
            "required": ["integration", "value"],
            "additionalProperties": False,
        },
    },
}


def _parse_utc_instant(value: object) -> datetime | None:
    """The instant ``value`` names, or ``None`` when it is not one ``Z``-suffixed UTC instant."""
    if not isinstance(value, str) or _UTC_INSTANT.fullmatch(value) is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _invalid_postiz_id(value: object, where: str) -> str | None:
    if not isinstance(value, str) or not value:
        return f"'{where}' must be a non-empty string"
    if len(value) > POSTIZ_ID_MAX_LENGTH:
        return f"'{where}' exceeds {POSTIZ_ID_MAX_LENGTH} characters"
    if _POSTIZ_ID.fullmatch(value) is None:
        return f"'{where}' must not contain '/', ':' or whitespace: it is an id, never an address"
    return None


def _invalid_postiz_approval(arguments: JsonObject) -> str | None:
    """The same bound filebrowser places on ``approval``, applied to a postiz call."""
    continuation = arguments.get("approval")
    if continuation is None:
        return None
    if not isinstance(continuation, str) or not continuation:
        return "'approval' must be a non-empty string"
    if len(continuation) > _MAX_CONTINUATION_LENGTH:
        return f"'approval' exceeds {_MAX_CONTINUATION_LENGTH} characters"
    return None


def _unexpected_keys(value: dict[str, Any], accepted: set[str], where: str) -> str | None:
    unexpected = sorted(set(value) - accepted)
    if not unexpected:
        return None
    return (
        f"'{where}' accepts only {', '.join(repr(key) for key in sorted(accepted))}; "
        f"unexpected: {', '.join(repr(key) for key in unexpected)}"
    )


def _invalid_list_posts(arguments: JsonObject) -> str | None:
    for name in ("startDate", "endDate"):
        if name not in arguments:
            return f"'{name}' is required"
        if _parse_utc_instant(arguments[name]) is None:
            return f"'{name}' must be a UTC instant in ISO-8601 form ending in 'Z'"
    start = _parse_utc_instant(arguments["startDate"])
    end = _parse_utc_instant(arguments["endDate"])
    assert start is not None and end is not None
    if end <= start:
        return "'endDate' must be after 'startDate'"
    if end - start > POSTIZ_LIST_WINDOW_MAX:
        return "the window from 'startDate' to 'endDate' must not exceed 31 days"
    if "customer" in arguments:
        return _invalid_postiz_id(arguments["customer"], "customer")
    return None


def _invalid_image(image: object, where: str) -> str | None:
    if not isinstance(image, dict):
        return f"'{where}' must be an object"
    unexpected = _unexpected_keys(image, {"id", "path"}, where)
    if unexpected is not None:
        return unexpected
    problem = _invalid_postiz_id(image.get("id"), f"{where}.id")
    if problem is not None:
        return problem
    path = image.get("path")
    if not isinstance(path, str) or not path:
        return f"'{where}.path' must be a non-empty string"
    if len(path) > POSTIZ_IMAGE_PATH_MAX_LENGTH:
        return f"'{where}.path' exceeds {POSTIZ_IMAGE_PATH_MAX_LENGTH} characters"
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        return (
            f"'{where}.path' must be a path on this Postiz beginning with '/', never a URL or "
            "an authority"
        )
    return None


def _invalid_value_entry(entry: object, where: str) -> str | None:
    if not isinstance(entry, dict):
        return f"'{where}' must be an object"
    unexpected = _unexpected_keys(entry, {"content", "image"}, where)
    if unexpected is not None:
        return unexpected
    content = entry.get("content")
    if not isinstance(content, str):
        return f"'{where}.content' must be a string"
    if len(content) > POSTIZ_CONTENT_MAX_LENGTH:
        return f"'{where}.content' exceeds {POSTIZ_CONTENT_MAX_LENGTH} characters"
    images = entry.get("image", [])
    if not isinstance(images, list):
        return f"'{where}.image' must be an array"
    if len(images) > POSTIZ_MAX_IMAGES:
        return f"'{where}.image' carries more than {POSTIZ_MAX_IMAGES} images"
    # PostContent's own `ValidContent` rule: without an image, the content must say something.
    if not images and not content.strip():
        return f"'{where}.content' must be non-empty when the entry carries no image"
    for index, image in enumerate(images):
        problem = _invalid_image(image, f"{where}.image[{index}]")
        if problem is not None:
            return problem
    return None


def _invalid_post_item(item: object, where: str) -> str | None:
    if not isinstance(item, dict):
        return f"'{where}' must be an object"
    unexpected = _unexpected_keys(item, {"integration", "value", "settings"}, where)
    if unexpected is not None:
        return unexpected
    integration = item.get("integration")
    if not isinstance(integration, dict):
        return f"'{where}.integration' must be an object"
    unexpected = _unexpected_keys(integration, {"id"}, f"{where}.integration")
    if unexpected is not None:
        return unexpected
    problem = _invalid_postiz_id(integration.get("id"), f"{where}.integration.id")
    if problem is not None:
        return problem
    value = item.get("value")
    if not isinstance(value, list) or not value:
        return f"'{where}.value' must be a non-empty array"
    if len(value) > POSTIZ_MAX_VALUE_ENTRIES:
        return f"'{where}.value' carries more than {POSTIZ_MAX_VALUE_ENTRIES} entries"
    for index, entry in enumerate(value):
        problem = _invalid_value_entry(entry, f"{where}.value[{index}]")
        if problem is not None:
            return problem
    if "settings" in item:
        settings = item["settings"]
        if not isinstance(settings, dict):
            return f"'{where}.settings' must be an object"
        encoded = json.dumps(settings, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > POSTIZ_SETTINGS_MAX_BYTES:
            return f"'{where}.settings' exceeds {POSTIZ_SETTINGS_MAX_BYTES} bytes of JSON"
    return None


def _invalid_create_post(arguments: JsonObject) -> str | None:
    for name in ("type", "date", "shortLink", "tags", "posts"):
        if name not in arguments:
            return f"'{name}' is required"
    if arguments["type"] not in POSTIZ_CREATE_TYPES:
        return f"'type' must be one of {', '.join(repr(value) for value in POSTIZ_CREATE_TYPES)}"
    if _parse_utc_instant(arguments["date"]) is None:
        return "'date' must be a UTC instant in ISO-8601 form ending in 'Z'"
    if not isinstance(arguments["shortLink"], bool):
        return "'shortLink' must be true or false"
    tags = arguments["tags"]
    if not isinstance(tags, list):
        return "'tags' must be an array"
    if len(tags) > POSTIZ_MAX_TAGS:
        return f"'tags' carries more than {POSTIZ_MAX_TAGS} tags"
    for index, tag in enumerate(tags):
        where = f"tags[{index}]"
        if not isinstance(tag, dict):
            return f"'{where}' must be an object"
        unexpected = _unexpected_keys(tag, {"value", "label"}, where)
        if unexpected is not None:
            return unexpected
        for field_name in ("value", "label"):
            text_value = tag.get(field_name)
            if not isinstance(text_value, str):
                return f"'{where}.{field_name}' must be a string"
            if len(text_value) > POSTIZ_TAG_FIELD_MAX_LENGTH:
                return f"'{where}.{field_name}' exceeds {POSTIZ_TAG_FIELD_MAX_LENGTH} characters"
    posts = arguments["posts"]
    if not isinstance(posts, list) or not posts:
        return "'posts' must be a non-empty array"
    if len(posts) > POSTIZ_MAX_POST_ITEMS:
        return f"'posts' carries more than {POSTIZ_MAX_POST_ITEMS} post items"
    for index, item in enumerate(posts):
        problem = _invalid_post_item(item, f"posts[{index}]")
        if problem is not None:
            return problem
    # Last, over the whole body: the dispatch row stores arguments through the audit redactor, and
    # `enqueue_call` refuses a call whose stored copy differs (CR-01). Checked HERE, before the
    # gates, so a preview never shows a post the real call would then refuse.
    dispatched = canonical_action_arguments(arguments)
    if _sanitize_audit_details(dispatched) != dispatched:
        return (
            "these arguments would not be stored unchanged in the call's dispatch record (a "
            "string over 500 characters, or a key the audit redactor treats as a secret), so the "
            "call is refused rather than run as something else"
        )
    return None


def _utc_day_bounds(day: datetime) -> tuple[str, str]:
    """``list_posts``'s window for one whole UTC day, both ends inclusive to the millisecond."""
    start = day.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    return (
        start.strftime("%Y-%m-%dT%H:%M:%S.") + f"{start.microsecond // 1000:03d}Z",
        end.strftime("%Y-%m-%dT%H:%M:%S.") + f"{end.microsecond // 1000:03d}Z",
    )


def _named_channels(arguments: JsonObject) -> list[str]:
    posts = arguments.get("posts")
    if not isinstance(posts, list):
        return []
    channels: set[str] = set()
    for item in posts:
        integration = item.get("integration") if isinstance(item, dict) else None
        channel = integration.get("id") if isinstance(integration, dict) else None
        if isinstance(channel, str) and channel:
            channels.add(channel)
    return sorted(channels)


def _first_content(item: dict[str, Any]) -> str:
    value = item.get("value")
    first = value[0] if isinstance(value, list) and value else None
    content = first.get("content") if isinstance(first, dict) else None
    return content if isinstance(content, str) else ""


def _listed_post_entry(post: dict[str, Any], channel: str) -> dict[str, Any]:
    """One scheduled post as a diff entry, every Postiz-supplied string stripped of markup."""
    content = strip_active_markup(str(post.get("content") or ""))
    post_id = strip_active_markup(str(post.get("id") or "unknown"))
    state = post.get("state")
    published = post.get("publishDate")
    return {
        "name": content[:POSTIZ_PREVIEW_CONTENT_CHARACTERS],
        "path": f"{strip_active_markup(channel)}/{post_id}",
        "kind": f"post:{strip_active_markup(state).lower()}" if isinstance(state, str) else "post",
        "size": None,
        "modified": strip_active_markup(published) if isinstance(published, str) else None,
    }


class PostizArgumentContract:
    """The postiz verbs' argument contract.

    Every schema is closed (``additionalProperties: false``) and carries the shared ``approval``
    control parameter; ``create_post`` also carries ``dry_run``, by class. No postiz argument is an
    address: ids refuse ``/``, ``:`` and whitespace, an image path must begin with a single ``/``,
    the host and port come from the registry row, and the API key is set by the runtime executor,
    never by an argument.

    ``create_post``'s preview reads ``list_posts`` for the UTC day of ``date`` and returns the
    posts already scheduled that day on the named channels, beside one entry per proposed post
    item. Its target, and its preview-ledger key, is that day plus the sorted channel ids.
    """

    companion_key = POSTIZ_COMPANION_KIND
    missing_target_message = (
        "the preview needs a valid 'date' and at least one post item's integration id to read"
    )

    def input_schema(self, verb: str, entry: CompanionVerbEntry) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        if verb == "list_posts":
            properties.update(copy.deepcopy(_LIST_POSTS_PROPERTIES))
            required = ["startDate", "endDate"]
        elif verb == "create_post":
            properties.update(copy.deepcopy(_CREATE_POST_PROPERTIES))
            required = ["type", "date", "shortLink", "tags", "posts"]
        properties["approval"] = dict(_APPROVAL_PROPERTY)
        # D-12 injects `dry_run` by CLASS, whatever the kind: `create_post` is the only mutating
        # postiz verb, so it is the only postiz schema that carries it.
        if entry.tool_class in MUTATING_TOOL_CLASSES:
            properties["dry_run"] = dict(_DRY_RUN_PROPERTY)
        schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        return schema

    def invalid(self, verb: str, entry: CompanionVerbEntry, arguments: JsonObject) -> str | None:
        previewable = entry.tool_class in MUTATING_TOOL_CLASSES
        if "dry_run" in arguments and not previewable:
            return (
                "'dry_run' is only accepted by a create, mutate or destroy verb: a read changes "
                "nothing, so it has nothing to preview"
            )
        if not isinstance(arguments.get("dry_run", False), bool):
            return "'dry_run' must be true or false"
        problem = _invalid_postiz_approval(arguments)
        if problem is not None:
            return problem
        payload = canonical_action_arguments(arguments)
        if verb in POSTIZ_NO_ARGUMENT_VERBS:
            unexpected = sorted(payload)
            if unexpected:
                return (
                    f"postiz__{verb} takes no arguments beyond 'approval'; unexpected: "
                    f"{', '.join(repr(key) for key in unexpected)}"
                )
            return None
        if verb == "list_posts":
            unexpected_message = _unexpected_keys(
                payload, {"startDate", "endDate", "customer"}, "arguments"
            )
            return unexpected_message or _invalid_list_posts(payload)
        if verb == "create_post":
            unexpected_message = _unexpected_keys(
                payload, {"type", "date", "shortLink", "tags", "posts"}, "arguments"
            )
            return unexpected_message or _invalid_create_post(payload)
        return f"postiz__{verb} has no argument contract in this deployment"

    def dispatch_arguments(self, arguments: JsonObject) -> JsonObject:
        return canonical_action_arguments(arguments)

    def target_key(self, verb: str, arguments: JsonObject) -> str | None:
        if verb != "create_post":
            return None
        moment = _parse_utc_instant(arguments.get("date"))
        channels = _named_channels(arguments)
        if moment is None or not channels:
            return None
        return f"{moment.astimezone(UTC).date().isoformat()}|{','.join(channels)}"

    def floor_target_count(
        self, verb: str, entry: CompanionVerbEntry, arguments: JsonObject
    ) -> int:
        del entry
        if verb != "create_post":
            return 1
        posts = arguments.get("posts")
        return len(posts) if isinstance(posts, list) else 0

    def probe_arguments(
        self, verb: str, entry: CompanionVerbEntry, arguments: JsonObject
    ) -> JsonObject | None:
        del entry
        if verb != "create_post":
            return None
        moment = _parse_utc_instant(arguments.get("date"))
        if moment is None:
            return None
        start, end = _utc_day_bounds(moment)
        return {"startDate": start, "endDate": end}

    def dry_run_state(
        self,
        verb: str,
        entry: CompanionVerbEntry,
        arguments: JsonObject,
        payload: dict[str, Any],
    ) -> DryRunState | str:
        del entry
        target = self.target_key(verb, arguments)
        moment = _parse_utc_instant(arguments.get("date"))
        posts = arguments.get("posts")
        if verb != "create_post" or target is None or moment is None or not isinstance(posts, list):
            return f"postiz__{verb} has no preview in this deployment"
        listed = payload.get("posts")
        if not isinstance(listed, list):
            return (
                "the postiz__list_posts read probe returned a body with no 'posts' listing, so "
                "what is already scheduled could not be read"
            )
        day = moment.astimezone(UTC).date()
        channels = set(_named_channels(arguments))
        before: list[dict[str, Any]] = []
        for post in listed:
            if not isinstance(post, dict):
                continue
            integration = post.get("integration")
            channel = integration.get("id") if isinstance(integration, dict) else None
            published = _parse_listed_instant(post.get("publishDate"))
            if channel not in channels or published is None or published.date() != day:
                continue
            before.append(_listed_post_entry(post, str(channel)))
        proposed: list[dict[str, Any]] = []
        date_text = str(arguments.get("date"))
        undetermined = [
            "whether and when each post is published depends on the social provider behind its "
            "channel, which this preview cannot read"
        ]
        for index, item in enumerate(posts):
            if not isinstance(item, dict):
                continue
            channel = str(cast(JsonObject, item["integration"])["id"])
            content = strip_active_markup(_first_content(item))
            proposed.append(
                {
                    "name": content[:POSTIZ_PREVIEW_CONTENT_CHARACTERS],
                    "path": f"{channel}/new",
                    "kind": "post:proposed",
                    "size": None,
                    "modified": date_text,
                }
            )
            value = item.get("value")
            if isinstance(value, list) and len(value) > 1:
                undetermined.append(
                    f"post item {index} is a thread of {len(value)} entries; only the first is "
                    "shown"
                )
        return DryRunState(
            target=target,
            target_count=len(posts),
            before=before,
            after=[*before, *proposed],
            undetermined=undetermined,
        )


def _parse_listed_instant(value: object) -> datetime | None:
    """A ``publishDate`` as Postiz lists it (``...000Z``), or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


# ---------------------------------------------------------------------------
# The kind -> contract table.
# ---------------------------------------------------------------------------

_CONTRACTS: dict[str, CompanionArgumentContract] = {
    FilebrowserArgumentContract.companion_key: FilebrowserArgumentContract(),
    PostizArgumentContract.companion_key: PostizArgumentContract(),
}


def argument_contract_for(companion_key: str) -> CompanionArgumentContract:
    """The argument contract of one companion kind.

    Raises ``KeyError`` for a kind with no contract. The façade treats that as a refusal, never as
    a fallback to another kind's contract: guessing a contract is how a new kind would inherit an
    argument shape nobody reviewed for it.
    """
    return _CONTRACTS[companion_key]


def argument_contract_kinds() -> frozenset[str]:
    """Every kind that owns an argument contract. A test holds this equal to the verb kinds."""
    return frozenset(_CONTRACTS)
