"""The companion registry, Python half.

A companion is a server-owned program that runs beside the hosted runtime under its own service
identity. This module is the backend's authority on which companion kinds exist and what each one
declares, and it is what the emit-time version gate (D-08/D-15) consults before putting a kind on
the wire.

The entries below are written out verbatim rather than parsed from
``config/companion-registry.txt`` at import time, for the same reason ``app/core/brand_env.py``
embeds its map: the shipped artifact has no repository beside it. ``backend/Dockerfile`` copies
``backend/`` alone into ``/app/backend``, so an import-time read of
``/app/config/companion-registry.txt`` would raise ``FileNotFoundError`` and take down
``python -m app.runtime_entrypoint`` on every container boot. That is not a hypothetical: it is
why the environment-contract map is embedded too.

**Do not "improve" this into a runtime read.** The contract file is the TEST-TIME authority.
``backend/tests/test_companion_registry.py`` asserts this embed equals that file column for
column, and asserts this key set equals the TypeScript embed's in
``packages/cli/src/runtime/companion-registry.ts``, so the three representations cannot drift.

``parse_companion_contract`` lives here rather than in the test module on purpose. It is a
contract-verification helper, never called at import and never called in production, and keeping
one importable definition is the lesson ``backend/tests/doc_contract.py`` records in its own
docstring -- four suites once carried byte-identical private copies of a shared primitive, "the
exact failure mode these suites exist to catch in prose".

Standard library only at import time. ``parse_companion_contract`` resolves the reserved native
tool-name set lazily, inside the call, so the collision rule can be checked against the LIVE
``_NATIVE_TOOL_REGISTRY`` without ``app.core`` importing ``app.routes`` at module scope. The
function is contract-verification only -- never called at import, never called in production -- so
that import is paid by the test suite and by nothing else.
"""

from __future__ import annotations

import re
from collections.abc import Container
from dataclasses import dataclass, field

# The bound every companion key must satisfy, mirrored character for character by
# ``COMPANION_KEY_PATTERN`` in ``packages/cli/src/runtime/companion-registry.ts`` and by the
# provisional local copy in ``app/schemas/runtime_observed.py``. The key becomes a systemd unit
# name, a UID lookup name, a ``StateDirectory=`` value and a path component, and it arrives from
# the provisioning image -- outside this repository -- so it is untrusted input (T-4-01).
# ``sysusers.d(5)`` caps user names at 31 characters and forbids a leading digit or hyphen.
COMPANION_KEY_PATTERN = r"^[a-z][a-z0-9-]{0,30}$"

_COMPANION_KEY_RE = re.compile(COMPANION_KEY_PATTERN)

# The UID range companion service accounts are allocated from, as ``(first, last)``. The first
# numeric allocation from it is ``postiz`` = 10101 (06-03). Its confirmation against the
# provisioning image's ``/etc/passwd`` was explicitly DEFERRED to host UAT at checkpoint CK-B
# (research assumption A4, Phase 4 deferred obligation #2). See the contract file header.
RESERVED_COMPANION_UID_BLOCK = (10100, 10199)

# How a kind's digest pin is located -- CLOSED to exactly these two values. ``binary-sha256`` is a
# native binary; ``oci-digest`` is a container image located by its OCI content digest, the seam
# Phase 4 D-02 left open for a Quadlet/Podman kind and 06-03 uses for ``postiz``. There is
# deliberately no tag-shaped value: D-01 is fail closed, never fetch a floating reference.
_ARTIFACT_KINDS = frozenset({"binary-sha256", "oci-digest"})

# Whether a kind's UID participates in the transparent egress redirect.
_EGRESS_MODES = frozenset({"redirect", "bypass"})

# A kind declaring this minimum is ungated: every deployed CLI can already parse it. It is the
# floor D-15 falls back to when no CLI version has been observed.
_UNGATED_MIN_CLI_VERSION = "0.0.0"

_CONTRACT_COLUMNS = 6

# The verb table (phase 05, D-04/D-05/D-07/D-13). A VERB row is nine columns whose first column
# is the literal marker below, which is therefore a key no companion kind may take. The ninth
# column, ``argsIn``, was appended by 05-13 (WR-06b).
_VERB_ROW_MARKER = "verb"
_VERB_CONTRACT_COLUMNS = 9

# The classification vocabulary (D-05). The parser refuses anything outside it, which is what
# makes "a verb cannot ship unclassified" structural rather than a review convention.
_TOOL_CLASSES = frozenset({"read", "create", "mutate", "destroy"})

# The classes for which a read probe is MANDATORY (D-13). A `mutate` or `destroy` row without one
# is malformed, which is what makes TOOL-05's "any mutating tool has a preview path" true by
# construction instead of by intention.
_PROBE_REQUIRED_CLASSES = frozenset({"mutate", "destroy"})

# The blast-radius units a cap may be counted in (TOOL-04).
_CAP_UNITS = frozenset({"calls", "records", "bytes"})

# The HTTP methods a verb row may declare. The executor issues exactly what the signed envelope
# carries, so an unbounded method column would be an unbounded request verb into tenant loopback.
_VERB_METHODS = frozenset({"GET", "POST", "PATCH", "PUT", "DELETE"})

# Where a verb's arguments travel (05-13, WR-06b). ``query`` puts each argument in the query
# string; ``body`` sends them as one JSON object with an empty query string. Closed, and with NO
# DEFAULT: placement used to be derived from the HTTP method inside the runtime-side executor, so
# three verbs sent their target path somewhere their siblings did not and no reader could see the
# decision in a diff. A default here would reintroduce exactly that invisible rule.
_VERB_ARGS_IN = frozenset({"query", "body"})

# The sentinel meaning "this verb has no read probe". Spelled rather than left blank so an empty
# column is a visible mistake instead of an accidental opt-out of D-13.
_NO_READ_PROBE = "-"

_VERB_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,60}$"
_VERB_NAME_RE = re.compile(_VERB_NAME_PATTERN)

# The assembled tool name (D-04): a bounded kind key, the double-underscore separator, a bounded
# verb. Mirrored by `COMPANION_TOOL_NAME_PATTERN` in the TypeScript half.
COMPANION_TOOL_NAME_PATTERN = r"^[a-z][a-z0-9-]{0,30}__[a-z][a-z0-9_]{0,60}$"

_COMPANION_TOOL_NAME_RE = re.compile(COMPANION_TOOL_NAME_PATTERN)

_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@dataclass(frozen=True)
class CompanionRegistryEntry:
    """One row of ``config/companion-registry.txt``, minus its key."""

    artifact_kind: str
    min_cli_version: str
    uid: int | str
    port: int
    egress: str


COMPANION_REGISTRY: dict[str, CompanionRegistryEntry] = {
    "filebrowser": CompanionRegistryEntry(
        artifact_kind="binary-sha256",
        min_cli_version="0.0.0",
        uid="preallocated",
        port=9120,
        egress="bypass",
    ),
    # 06-03 (D-01). The first container kind, the first gated kind and the first numeric UID. Each
    # is one-way under D-13; the exclusions for its egress and minimum are in the contract file.
    "postiz": CompanionRegistryEntry(
        artifact_kind="oci-digest",
        min_cli_version="0.14.0",
        uid=10101,
        port=9130,
        egress="bypass",
    ),
}

KNOWN_COMPANION_KINDS: tuple[str, ...] = tuple(sorted(COMPANION_REGISTRY))


@dataclass(frozen=True)
class CompanionVerbEntry:
    """One VERB row of ``config/companion-registry.txt``, minus its ``kind`` and ``verb``.

    ``tool_class`` is the server-side gate's input; the MCP annotations derived from it are
    advisory client-side UX and are NEVER the gate (D-07). ``read_probe`` is ``None`` only when
    the row's class does not require one -- D-13 makes a probe-less ``mutate`` or ``destroy`` row
    malformed at the parser, not merely discouraged downstream.
    """

    tool_class: str
    method: str
    path_template: str
    read_probe: str | None
    cap_unit: str
    # Where this verb's arguments travel: ``query`` or ``body``. Declared per row rather than
    # derived from ``method``, so a reader can see the placement in a diff and change it by
    # editing one column (05-13, WR-06b).
    args_in: str


@dataclass(frozen=True)
class CompanionContract:
    """Both tables of one parse of the contract file.

    Returned as one object rather than as two calls so a caller cannot check the kind rows and
    skip the verb rows: the cross-table rules (a verb's ``kind`` must own a row, a read probe must
    name a verb of the same kind) are only checkable when both are parsed together.
    """

    kinds: dict[str, CompanionRegistryEntry] = field(default_factory=dict)
    verbs: dict[str, CompanionVerbEntry] = field(default_factory=dict)


# The mechanical class -> MCP annotation map (D-07). Hand-authoring an annotation anywhere is the
# drift this table exists to make impossible; the two vocabularies have exactly one join.
_CLASS_ANNOTATIONS: dict[str, dict[str, bool]] = {
    "read": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
    "create": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
    "mutate": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
    "destroy": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
}


# The curated toolsets (TOOL-08), in the contract file's ascending `(kind, verb)` order. The
# `filebrowser` rows are one entry per INTEGRATE capability in
# `.planning/phases/05-companion-tool-contract-mcp-fa-ade/COVERAGE.md`; the `postiz` rows follow.
# `filebrowser__delete_path` is `destroy` class: its presence here is the CLASSIFICATION, never the
# permission -- D-06 keeps it out of `tools/list` until the tenant enables it and refuses it by
# name at call time.
COMPANION_VERBS: dict[str, CompanionVerbEntry] = {
    "filebrowser__create_dir": CompanionVerbEntry(
        tool_class="create",
        method="POST",
        path_template="/api/resources",
        read_probe="list_dir",
        cap_unit="calls",
        args_in="query",
    ),
    "filebrowser__delete_path": CompanionVerbEntry(
        tool_class="destroy",
        method="DELETE",
        path_template="/api/resources",
        read_probe="list_dir",
        cap_unit="records",
        args_in="query",
    ),
    "filebrowser__list_dir": CompanionVerbEntry(
        tool_class="read",
        method="GET",
        path_template="/api/resources",
        read_probe=None,
        cap_unit="calls",
        args_in="query",
    ),
    "filebrowser__move_path": CompanionVerbEntry(
        tool_class="mutate",
        method="PATCH",
        path_template="/api/resources",
        read_probe="list_dir",
        cap_unit="records",
        args_in="query",
    ),
    "filebrowser__read_file": CompanionVerbEntry(
        tool_class="read",
        method="GET",
        path_template="/api/raw",
        read_probe=None,
        cap_unit="bytes",
        args_in="query",
    ),
    "filebrowser__write_file": CompanionVerbEntry(
        tool_class="create",
        method="POST",
        path_template="/api/resources",
        read_probe="list_dir",
        cap_unit="bytes",
        args_in="query",
    ),
    # The curated `postiz` toolset (06-06), one entry per INTEGRATE capability in
    # `.planning/phases/06-first-companion-end-to-end/COVERAGE.md`, decided against the Postiz
    # v2.24.0 public controller. `create_post` counts `records` -- one call can create one post per
    # listed channel -- and previews with `list_posts`. No postiz verb is `destroy` class; every
    # excluded capability and its reason is in the contract file's POSTIZ VERB TABLE EXCLUSIONS.
    "postiz__create_post": CompanionVerbEntry(
        tool_class="create",
        method="POST",
        path_template="/api/public/v1/posts",
        read_probe="list_posts",
        cap_unit="records",
        args_in="body",
    ),
    "postiz__is_connected": CompanionVerbEntry(
        tool_class="read",
        method="GET",
        path_template="/api/public/v1/is-connected",
        read_probe=None,
        cap_unit="calls",
        args_in="query",
    ),
    "postiz__list_groups": CompanionVerbEntry(
        tool_class="read",
        method="GET",
        path_template="/api/public/v1/groups",
        read_probe=None,
        cap_unit="calls",
        args_in="query",
    ),
    "postiz__list_integrations": CompanionVerbEntry(
        tool_class="read",
        method="GET",
        path_template="/api/public/v1/integrations",
        read_probe=None,
        cap_unit="calls",
        args_in="query",
    ),
    "postiz__list_notifications": CompanionVerbEntry(
        tool_class="read",
        method="GET",
        path_template="/api/public/v1/notifications",
        read_probe=None,
        cap_unit="calls",
        args_in="query",
    ),
    "postiz__list_posts": CompanionVerbEntry(
        tool_class="read",
        method="GET",
        path_template="/api/public/v1/posts",
        read_probe=None,
        cap_unit="calls",
        args_in="query",
    ),
}


def default_reserved_tool_names() -> frozenset[str]:
    """The tool names a ``kind__verb`` may never take: the live native MCP tool registry.

    Imported HERE rather than at module scope so ``app.core`` never depends on ``app.routes`` at
    import time. Only ``parse_companion_contract`` calls this, and only tests call that.
    """
    from app.routes.mcp_bridge import _NATIVE_TOOL_REGISTRY

    return frozenset(_NATIVE_TOOL_REGISTRY)


def companion_verb(name: str) -> CompanionVerbEntry:
    """The verb row for a ``kind__verb`` tool name.

    Raises ``KeyError`` naming the name rather than returning a default, for the same reason
    ``companion_min_cli_version`` does: a default here would be a silent decision about whether an
    unregistered tool name is callable, and the façade's whole contract is that it is not.
    """
    entry = COMPANION_VERBS.get(name)
    if entry is None:
        raise KeyError(f"unknown companion tool: {name}")
    return entry


def derive_annotations(tool_class: str) -> dict[str, bool]:
    """The MCP annotation triple for a tool class (D-07).

    Pure, total over the class vocabulary, and never hand-authored per verb. These are advisory
    client-side UX layered on top of the server-side class gate -- they are never the gate. The
    MCP spec's own default for an unannotated tool is "destructive, non-idempotent, open-world",
    which is already fail-safe, so emitting nothing would be safe; emitting something WRONG would
    not be, which is why this is a lookup rather than a heuristic.
    """
    annotations = _CLASS_ANNOTATIONS.get(tool_class)
    if annotations is None:
        raise ValueError(f"unknown tool class: {tool_class!r}")
    return dict(annotations)


def parse_companion_contract(
    source: str, *, reserved_tool_names: Container[str] | None = None
) -> CompanionContract:
    """Parse the contract file's text into its kind table and its verb table.

    Every failure RAISES, naming the offending line, key or path. A malformed row is never
    skipped and an empty result is never returned: either would let a parity assertion pass by
    comparing nothing to nothing -- a green test that reads as evidence while proving nothing,
    which is strictly worse than having no test.

    ``reserved_tool_names`` defaults to the live native MCP tool registry. It is injectable
    because the collision rule is otherwise unprovable: every native tool name today carries a
    single underscore and every companion tool name carries a double one, so no natural collision
    exists to write a test against. A rule that cannot be exercised is a rule that is not there.
    """
    reserved = default_reserved_tool_names() if reserved_tool_names is None else reserved_tool_names
    kinds: dict[str, CompanionRegistryEntry] = {}
    verb_rows: list[tuple[str, list[str]]] = []
    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        columns = line.split("|")
        if columns[0] == _VERB_ROW_MARKER:
            # A KIND row's column count under the verb marker is the reserved-key mistake, not a
            # miscounted verb row. The two have different fixes, so the message has to say which.
            if len(columns) == _CONTRACT_COLUMNS:
                raise ValueError(
                    f"contract key {_VERB_ROW_MARKER!r} is reserved as the verb-row marker and "
                    f"cannot be a companion kind: {line!r}"
                )
            if len(columns) != _VERB_CONTRACT_COLUMNS:
                raise ValueError(
                    f"malformed verb line: expected {_VERB_CONTRACT_COLUMNS} columns, "
                    f"got {len(columns)}: {line!r}"
                )
            verb_rows.append((line, columns))
            continue
        if len(columns) != _CONTRACT_COLUMNS:
            raise ValueError(
                f"malformed contract line: expected {_CONTRACT_COLUMNS} columns, "
                f"got {len(columns)}: {line!r}"
            )
        key, artifact_kind, min_cli_version, uid, port, egress = columns
        if _COMPANION_KEY_RE.fullmatch(key) is None:
            raise ValueError(f"contract key {key!r} does not match {COMPANION_KEY_PATTERN}")
        if artifact_kind not in _ARTIFACT_KINDS:
            # Names the value AND the line: the vocabulary is closed, and a reader fixing a row
            # needs to find it without re-deriving which one carried the value.
            raise ValueError(f"unknown artifactKind for {key!r}: {artifact_kind!r}: {line!r}")
        if egress not in _EGRESS_MODES:
            raise ValueError(f"unknown egress for {key!r}: {egress!r}")
        if not port.isdigit():
            raise ValueError(f"port for {key!r} is not a decimal: {port!r}")
        if uid != "preallocated" and not uid.isdigit():
            raise ValueError(f'uid for {key!r} must be "preallocated" or a decimal: {uid!r}')
        if key in kinds:
            raise ValueError(f"duplicate contract key: {key!r}")
        kinds[key] = CompanionRegistryEntry(
            artifact_kind=artifact_kind,
            min_cli_version=min_cli_version,
            uid="preallocated" if uid == "preallocated" else int(uid),
            port=int(port),
            egress=egress,
        )
    verbs = _parse_verb_rows(verb_rows, kinds=kinds, reserved_tool_names=reserved)
    if not kinds and not verbs:
        raise ValueError("companion contract has no data rows; the registry cannot be empty")
    return CompanionContract(kinds=kinds, verbs=verbs)


def _parse_verb_rows(
    verb_rows: list[tuple[str, list[str]]],
    *,
    kinds: dict[str, CompanionRegistryEntry],
    reserved_tool_names: Container[str],
) -> dict[str, CompanionVerbEntry]:
    """Resolve verb rows in a SECOND pass, once every kind and every verb name is known.

    Order-independence is the point: a row may reference a kind declared anywhere in the file, and
    a read probe is checked against the whole verb table rather than against whatever happened to
    have been parsed already. A one-pass reader would make a dangling probe depend on row order,
    which is exactly the kind of position-sensitive rule a contract file should not have.
    """
    verbs: dict[str, CompanionVerbEntry] = {}
    for line, columns in verb_rows:
        (
            _marker,
            kind,
            verb,
            tool_class,
            method,
            path_template,
            read_probe,
            cap_unit,
            args_in,
        ) = columns
        name = f"{kind}__{verb}"
        if _COMPANION_KEY_RE.fullmatch(kind) is None:
            raise ValueError(f"verb row kind {kind!r} does not match {COMPANION_KEY_PATTERN}")
        if _VERB_NAME_RE.fullmatch(verb) is None:
            raise ValueError(f"verb {verb!r} does not match {_VERB_NAME_PATTERN}")
        if _COMPANION_TOOL_NAME_RE.fullmatch(name) is None:
            raise ValueError(f"tool name {name!r} does not match {COMPANION_TOOL_NAME_PATTERN}")
        if kind not in kinds:
            raise ValueError(f"unknown companion kind for {name}: {kind!r}")
        if tool_class not in _TOOL_CLASSES:
            # Names the LINE as well as the class: in a file whose rows all share one shape, the
            # class alone does not tell a reader which row to fix.
            raise ValueError(f"unknown tool class for {name}: {tool_class!r}: {line!r}")
        if method not in _VERB_METHODS:
            raise ValueError(f"unknown method for {name}: {method!r}")
        if not path_template.startswith("/"):
            raise ValueError(f"pathTemplate for {name} must be companion-relative: {line!r}")
        if cap_unit not in _CAP_UNITS:
            raise ValueError(f"unknown capUnit for {name}: {cap_unit!r}")
        if args_in not in _VERB_ARGS_IN:
            # Names the offending value AND the closed vocabulary. There is no default: an empty
            # column is this branch too, because an absent placement is a mistake and not an
            # opt-in to whatever the executor would otherwise have guessed.
            permitted = ", ".join(repr(value) for value in sorted(_VERB_ARGS_IN))
            raise ValueError(f"unknown argsIn for {name}: {args_in!r}: must be one of {permitted}")
        if read_probe == _NO_READ_PROBE and tool_class in _PROBE_REQUIRED_CLASSES:
            raise ValueError(f"{name} is class {tool_class!r} and must declare a read probe (D-13)")
        if name in reserved_tool_names:
            raise ValueError(f"tool name {name} collides with a native MCP tool")
        if name in verbs:
            raise ValueError(f"duplicate verb row: {name}")
        verbs[name] = CompanionVerbEntry(
            tool_class=tool_class,
            method=method,
            path_template=path_template,
            read_probe=None if read_probe == _NO_READ_PROBE else read_probe,
            cap_unit=cap_unit,
            args_in=args_in,
        )
    _assert_verb_rows_sorted(verb_rows)
    _resolve_read_probes(verbs)
    return verbs


def _assert_verb_rows_sorted(verb_rows: list[tuple[str, list[str]]]) -> None:
    """Enforce ascending ``(kind, verb)`` order, naming the FIRST offending pair.

    Set equality alone leaves the exposed order unspecified: two rows that compare equal on kind
    would have no defined position, and ``tools/list`` is asserted as a SEQUENCE against this
    order. A client that renders the catalog positionally must not be able to watch it reshuffle
    between two identical reads because somebody pasted a row in the wrong place.
    """
    names = [f"{columns[1]}__{columns[2]}" for _line, columns in verb_rows]
    for previous, current in zip(names, names[1:], strict=False):
        if current < previous:
            raise ValueError(
                f"verb rows must be sorted by kind then verb: {current} follows {previous}"
            )


def _resolve_read_probes(verbs: dict[str, CompanionVerbEntry]) -> None:
    """Resolve every declared read probe against the FULL verb table.

    Run after every row is parsed, so a probe may name a verb declared later in the file and the
    contract has no position-sensitive rule. Two ways to fail, and the second is the one that
    matters: a probe that names nothing is a preview path that does not exist, and a probe whose
    own class is not ``read`` is not a preview at all -- it is a SECOND mutation (T-5-13). Without
    the class check, D-13's "a mutating verb must declare a probe" would be satisfiable by naming
    another ``create`` verb, and TOOL-05's guarantee would be a spelling convention.
    """
    for name, entry in verbs.items():
        if entry.read_probe is None:
            continue
        kind = name.partition("__")[0]
        probe_name = f"{kind}__{entry.read_probe}"
        probe = verbs.get(probe_name)
        if probe is None:
            raise ValueError(
                f"{name} names a read probe that is not a verb of {kind!r}: {entry.read_probe!r}"
            )
        if probe.tool_class != "read":
            raise ValueError(
                f"{name} names a read probe that is not a read-class verb: "
                f"{entry.read_probe!r} is class {probe.tool_class!r}"
            )


def companion_min_cli_version(kind: str) -> str:
    """The lowest CLI semver that can parse ``kind``.

    Raises ``KeyError`` naming the kind rather than returning a default. A default would be a
    silent decision about whether an unknown kind is emittable, which is exactly the decision
    D-15 requires to be made explicitly and fail closed.
    """
    entry = COMPANION_REGISTRY.get(kind)
    if entry is None:
        raise KeyError(f"unknown companion kind: {kind}")
    return entry.min_cli_version


def companion_kinds_at_or_below(cli_version: str | None) -> tuple[str, ...]:
    """The companion kinds a runtime reporting ``cli_version`` is allowed to be sent.

    Consumed by the emit-time version gate (D-08). Sorted by registry key, so the output is
    stable across calls and across processes.

    ``cli_version=None`` means no observation has been recorded, and D-15 makes that FAIL CLOSED:
    no observed version is no proof the CLI can parse anything, so only the ungated floor
    (``min_cli_version`` of ``0.0.0``, the kinds every deployed CLI already parses) is returned.
    "Absent means current" was rejected precisely because a stale client that never reports would
    otherwise receive kinds it cannot parse. The gate's wiring into ``render_runtime_source`` is
    ``04-05``'s work; this function is the decision it consults.
    """
    if cli_version is None:
        return tuple(
            kind
            for kind, entry in sorted(COMPANION_REGISTRY.items())
            if entry.min_cli_version == _UNGATED_MIN_CLI_VERSION
        )
    reported = _semver_sort_key(cli_version)
    return tuple(
        kind
        for kind, entry in sorted(COMPANION_REGISTRY.items())
        if _semver_sort_key(entry.min_cli_version) <= reported
    )


def _semver_sort_key(version: str) -> tuple[object, ...]:
    """A total order over semver strings, per the semver 2.0.0 precedence rules.

    Build metadata is ignored, and a prerelease sorts BELOW the normal release it precedes --
    so a runtime reporting ``1.2.3-rc.1`` is not sent a kind requiring ``1.2.3``.
    """
    match = _SEMVER_RE.match(version)
    if match is None:
        raise ValueError(f"not an exact semver version: {version!r}")
    core = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    prerelease = match.group(4)
    if prerelease is None:
        return (core, 1, ())
    identifiers: list[tuple[int, int, str]] = []
    for identifier in prerelease.split("."):
        if identifier.isdigit():
            identifiers.append((0, int(identifier), ""))
        else:
            identifiers.append((1, 0, identifier))
    return (core, 0, tuple(identifiers))
