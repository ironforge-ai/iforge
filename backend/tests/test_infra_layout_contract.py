import re

from tests.doc_contract import REPO_ROOT as _REPO_ROOT
from tests.doc_contract import normalized_text as _normalized_text

_LAYOUT_DOC = _REPO_ROOT / "docs" / "infrastructure-layout.md"

# Value-shaped only: a vendor value prefix that never appears in a variable name, an assignment
# of a value to a *declared* credential name, or a literal whose shape alone identifies it as
# secret material. Matching shape rather than a fixed substring is what lets this guard coexist
# with test_infra_layout_credential_columns_are_disjoint, which requires the same document to
# *declare* credential names such as `HCLOUD_TOKEN`.
#
# The assignment patterns allow whitespace around the separator. The previous bare-`=` literals
# caught `HCLOUD_TOKEN=abc` but not `HCLOUD_TOKEN = abc`, which is the more idiomatic way to
# write an assignment in prose or in a table cell -- so the guard missed the likelier leak shape.
_STRIPE_LIVE_KEY = re.compile(r"sk_live_|rk_live_")

# Card and account numbers: 13-19 digits, optionally split by single spaces or hyphens. Written
# out because the document tells its readers this file rejects them.
_ACCOUNT_OR_CARD_NUMBER = re.compile(r"\b(?:\d[ -]?){13,19}\b")

# Undelimited hex of key length. No prose in this document produces a run this long; a token,
# an API key or a hash does. `ENCRYPTION_KEY` and `VAULT_ENCRYPTION_KEY` are generated in this
# repository as `secrets.token_hex(32)`, which is exactly this shape.
_HEX_KEY_MATERIAL = re.compile(r"\b[0-9a-f]{32,}\b")

_FORBIDDEN_VALUE_PATTERNS = (
    _STRIPE_LIVE_KEY,
    _ACCOUNT_OR_CARD_NUMBER,
    _HEX_KEY_MATERIAL,
)

_VENDOR_TABLE_COLUMNS = (
    "vendor",
    "account",
    "purpose",
    "payment method",
    "resources held",
    "credential names",
)

_TENANT_WORKLOAD_MARKER = "tenant workload"
_CONTROL_PLANE_MARKER = "control plane"

# The 06.4 close-out of 2026-11-19 (06.4 D-28, D-29, D-34) kept the control plane on the existing
# Hostinger VPS and the tenant runtimes in the Hetzner tenant-workload project. Separation is by
# vendor and account again, as Phase 1 D-14 intended, so the Vendor cell carries the property the
# suspension walkthrough rests on, and the project is a detail of the Hetzner row.
_TENANT_VENDOR = "hetzner cloud"
_CONTROL_PLANE_VENDOR = "hostinger"
_TENANT_PROJECT_MARKER = "tenant-workload project"

# A credential name carrying one of these fragments belongs to the tenant vendor. The control-plane
# row must declare none: a Hetzner token on the control plane would put the tenants' vendor back
# in the control plane's path, which is the overlap the vendor split removes.
_TENANT_VENDOR_CREDENTIAL_FRAGMENTS = ("hcloud", "hetzner")

# No iforge service holds a Hostinger API token (06.4 D-24, D-40): Hostinger tokens carry the
# owning user's full rights with no project scope, so one in any row reaches the whole account.
_CONTROL_PLANE_VENDOR_CREDENTIAL_FRAGMENT = "hostinger"

# Phrases the layout must carry, normalized (lowercase, single-spaced).
_REQUIRED_SEPARATION_TEXT = (
    "an account-scoped hetzner suspension",
    "an account-scoped hostinger suspension",
    "the enforcement unit is the account, not the project",
    "separation is by vendor and account",
)

# 06.4 has no control-plane backup to place (06.4 D-39). The layout must say so rather than leave
# an operator to infer it from a missing row.
_REQUIRED_NO_BACKUP_TEXT = (
    "06.4 delivers no control-plane backup",
    "06.4 d-39",
    "staging data is disposable",
)

# The Hetzner control-plane move is planned, not running: backlog 999.5, with D-22 and D-23 as its
# starting design (06.4 D-38). The decisions that kept the layout as it is are cited by number.
_REQUIRED_PLANNED_MOVE_TEXT = (
    "backlog 999.5",
    "06.4 d-28",
    "06.4 d-29",
    "no iforge service holds a hostinger api token",
)

# Claims of the D-22/D-23 layout that the 06.4 close-out made false (06.4 D-38). Each one, left in
# the document, tells an operator walking a suspension something about the control plane or its
# backups that is not true of what runs.
_SUPERSEDED_PLACEMENT_CLAIMS = (
    "separation is by project, not by vendor",
    "backups are hetzner cloud backups",
    "no off-vendor copy exists, by decision",
    "every row in the table above reads hetzner cloud",
    "neither hetzner nor the backup vendor",
)


def _split_row(line: str) -> list[str]:
    """Split one raw markdown table line into stripped cells, or [] if it is not a table row."""
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _is_separator(cells: list[str]) -> bool:
    return all(set(cell) <= {"-", ":", " "} and cell for cell in cells)


def _vendor_table_rows() -> list[dict[str, str]]:
    """Parse the vendor table from raw lines.

    Reads raw text rather than normalized text on purpose: whitespace collapsing destroys
    column boundaries. Raises loudly if fewer than two data rows parse (the tenant workloads and
    the control plane), so a table that silently loses a row fails rather than passing vacuously
    against an empty set.
    """
    lines = _LAYOUT_DOC.read_text(encoding="utf-8").splitlines()

    header_index = -1
    for index, line in enumerate(lines):
        cells = _split_row(line)
        if [cell.casefold() for cell in cells] == list(_VENDOR_TABLE_COLUMNS):
            header_index = index
            break
    assert header_index >= 0, (
        f"vendor table header {list(_VENDOR_TABLE_COLUMNS)} not found in {_LAYOUT_DOC}"
    )

    rows: list[dict[str, str]] = []
    for line in lines[header_index + 1 :]:
        cells = _split_row(line)
        if not cells:
            break
        if _is_separator(cells):
            continue
        assert len(cells) == len(_VENDOR_TABLE_COLUMNS), (
            f"vendor table row has {len(cells)} cells, expected "
            f"{len(_VENDOR_TABLE_COLUMNS)}: {line!r}"
        )
        rows.append(dict(zip(_VENDOR_TABLE_COLUMNS, cells, strict=True)))

    assert len(rows) >= 2, (
        f"vendor table parsed {len(rows)} data rows, expected at least 2 "
        "(tenant workloads, control plane)"
    )
    for row in rows:
        for column, value in row.items():
            assert value, f"vendor table row has an empty {column!r} cell: {row}"
    return rows


def _credential_names(row: dict[str, str]) -> set[str]:
    """Credential variable names declared in one row, taken from its backticked tokens."""
    names = {token.casefold() for token in re.findall(r"`([^`]+)`", row["credential names"])}
    assert names, f"row declares no backticked credential name: {row}"
    return names


def _declared_credential_names() -> set[str]:
    """Every credential name the vendor table declares, casefolded."""
    declared: set[str] = set()
    for row in _vendor_table_rows():
        declared |= _credential_names(row)
    return declared


def _credential_assignment_pattern() -> re.Pattern[str]:
    """A value assigned to any credential name this document itself declares.

    Built from the table rather than from a hand-kept literal list, so a credential added to the
    vendor table is guarded against value leakage on the same edit that declares it. `\\s*[=:]\\s*`
    accepts the spaced forms an author is most likely to write.
    """
    names = "|".join(re.escape(name) for name in sorted(_declared_credential_names()))
    return re.compile(rf"\b({names})\s*[=:]\s*\S")


def _row_for(rows: list[dict[str, str]], marker: str) -> dict[str, str]:
    matches = [row for row in rows if marker in row["purpose"].casefold()]
    assert len(matches) == 1, (
        f"expected exactly one vendor row whose purpose mentions {marker!r}, found {len(matches)}"
    )
    return matches[0]


def test_infra_layout_names_every_vendor_account() -> None:
    """The walkthrough's premises and the separation unit are stated, and each row exists.

    The 06.4 close-out of 2026-11-19 (06.4 D-28, D-29) kept the control plane on the Hostinger
    VPS and the tenants on Hetzner, so separation is by vendor and account again (Phase 1 D-14's
    intent), and the walkthrough reads both account-scoped suspensions. 06.4 places no
    control-plane backup (06.4 D-39), so the table has two rows the walkthrough names.
    """
    text = _normalized_text(_LAYOUT_DOC)

    for required in _REQUIRED_SEPARATION_TEXT:
        assert required in text, f"the layout does not state {required!r}"

    rows = _vendor_table_rows()
    for marker in (_TENANT_WORKLOAD_MARKER, _CONTROL_PLANE_MARKER):
        _row_for(rows, marker)


def test_infra_layout_credential_columns_are_disjoint() -> None:
    """Every parsed row is compared, so the document's stated guarantee is the one enforced.

    ``docs/infrastructure-layout.md`` tells its readers this test "fails if a credential appears
    against two rows". Selecting rows by purpose-cell marker and comparing only those made that
    false: the HashiCorp Terraform Cloud row was parsed and discarded, so `TFC_TOKEN` could be
    declared against the tenant-workload vendor as well and nothing fired. Disjointness is a
    property of the whole table, so it is checked over the whole table. The failure message names
    a row by its Vendor and Account cells.
    """
    rows = _vendor_table_rows()

    # No emptiness assert here: `_credential_names` already ends with `assert names, ...`, so it
    # cannot return an empty set, and its message names the offending row.
    seen: dict[str, str] = {}
    for row in rows:
        label = f"{row['vendor']} / {row['account']}"
        for name in _credential_names(row):
            assert name not in seen, (
                f"credential {name!r} is declared against two rows: "
                f"{seen[name]!r} and {label!r}. The separation property this layout "
                "rests on is that the name sets do not overlap."
            )
            seen[name] = label

    # The two rows the suspension walkthrough names must still exist and still declare
    # credentials; the pairwise scan above would pass vacuously over a table that lost one.
    for marker in (_TENANT_WORKLOAD_MARKER, _CONTROL_PLANE_MARKER):
        assert _credential_names(_row_for(rows, marker))


def test_infra_layout_control_plane_is_off_the_tenant_vendor() -> None:
    """The control plane is on Hostinger and the tenants are on Hetzner, in the tenant project.

    The 06.4 close-out (06.4 D-28, D-29, D-34) kept the control plane on the existing Hostinger
    VPS, so an account-scoped Hetzner lock reaches the tenants and not the control plane, and the
    reverse holds for a Hostinger lock. That only stays true while the two rows name different
    vendors and the control plane holds no Hetzner credential. A Hostinger credential in any row
    is refused too: Hostinger API tokens carry the owning user's full rights with no project
    scope (06.4 D-24, D-40), so no iforge service holds one.
    """
    rows = _vendor_table_rows()
    tenant = _row_for(rows, _TENANT_WORKLOAD_MARKER)
    control_plane = _row_for(rows, _CONTROL_PLANE_MARKER)

    assert tenant["vendor"].casefold() == _TENANT_VENDOR, (
        f"the tenant-workload row names {tenant['vendor']!r}; tenant runtimes stay on Hetzner "
        "Cloud (06.4 D-29)"
    )
    assert _TENANT_PROJECT_MARKER in tenant["account"].casefold(), (
        f"the tenant-workload row's account cell does not name the {_TENANT_PROJECT_MARKER!r}"
    )
    assert control_plane["vendor"].casefold() == _CONTROL_PLANE_VENDOR, (
        f"the control-plane row names {control_plane['vendor']!r}; the control plane stays on the "
        "existing Hostinger VPS (06.4 D-28). Moving it is backlog 999.5, a new decision that "
        "re-decides the table, not an edit to one cell."
    )
    assert tenant["vendor"].casefold() != control_plane["vendor"].casefold(), (
        "the tenant-workload and control-plane rows name the same vendor: separation is by "
        "vendor and account"
    )

    for name in _credential_names(control_plane):
        for fragment in _TENANT_VENDOR_CREDENTIAL_FRAGMENTS:
            assert fragment not in name, (
                f"the control-plane row declares {name!r}, a tenant-vendor credential; the "
                "control plane holds no Hetzner token"
            )

    for row in rows:
        for name in _credential_names(row):
            assert _CONTROL_PLANE_VENDOR_CREDENTIAL_FRAGMENT not in name, (
                f"the {row['vendor']!r} row declares {name!r}: no iforge service holds a "
                "Hostinger API token, because one carries the owning user's full rights"
            )


def test_infra_layout_declares_no_control_plane_backup() -> None:
    """No row claims a backup, and the document says why and where the move went.

    06.4 delivers no control-plane backup (06.4 D-39): staging data is disposable, and the
    production backup target is chosen when production's control plane is built. A backup row
    would claim a target that does not exist. The Hetzner control-plane move, with D-22 and D-23 as
    its starting design, is backlog 999.5 (06.4 D-38), and the layout says so in a planned note.
    """
    rows = _vendor_table_rows()
    for row in rows:
        assert "backup" not in row["purpose"].casefold(), (
            f"the {row['vendor']!r} row's purpose claims a backup; 06.4 delivers no control-plane "
            "backup (06.4 D-39)"
        )

    text = _normalized_text(_LAYOUT_DOC)
    for required in (*_REQUIRED_NO_BACKUP_TEXT, *_REQUIRED_PLANNED_MOVE_TEXT):
        assert required in text, f"the layout does not state {required!r}"


def test_infra_layout_drops_the_superseded_placement_claims() -> None:
    """The D-22/D-23 placement sentences the 06.4 close-out made false are gone.

    D-22 and D-23 are superseded for 06.4 (06.4 D-38) and survive only as backlog 999.5's starting
    design. Their claims -- separation by project, Hetzner Cloud Backups as the control plane's
    backups, no off-vendor copy by decision, every row on Hetzner -- must not survive in the
    layout of what runs.
    """
    text = _normalized_text(_LAYOUT_DOC)

    for superseded in _SUPERSEDED_PLACEMENT_CLAIMS:
        assert superseded not in text, (
            f"the layout still claims {superseded!r}, which 06.4 D-38 superseded"
        )


def test_required_and_superseded_layout_phrases_do_not_collide() -> None:
    """A superseded claim inside a required phrase would make the two cases unsatisfiable."""
    for superseded in _SUPERSEDED_PLACEMENT_CLAIMS:
        for required in (
            *_REQUIRED_SEPARATION_TEXT,
            *_REQUIRED_NO_BACKUP_TEXT,
            *_REQUIRED_PLANNED_MOVE_TEXT,
        ):
            assert superseded not in required


def test_infra_layout_records_residual_risks() -> None:
    text = _normalized_text(_LAYOUT_DOC)

    for required in (
        "one instance per tenant, no shared private network, per-tenant firewalls",
        "the provisioner's token reaches every tenant's instance",
        "the per-account resource-count ceiling is unknown",
        "console limits tab",
        "no project-creation endpoint is documented in the cloud api reference",
        "no live revocation drill has been run",
    ):
        assert required in text


def test_infra_layout_rejects_credential_values() -> None:
    text = _normalized_text(_LAYOUT_DOC)

    for pattern in (*_FORBIDDEN_VALUE_PATTERNS, _credential_assignment_pattern()):
        match = pattern.search(text)
        assert match is None, (
            f"value-shaped literal in the layout document: {match.group(0)!r}. Credentials are "
            "named by variable name and vendor only; a value committed here is the one mistake "
            "on this page that an edit cannot undo."
        )


def test_forbidden_values_do_not_collide_with_declared_credential_names() -> None:
    """The two guards read the same file with opposite intent; prove they stay satisfiable.

    A name-shaped forbidden pattern (say a bare `hcloud_`) would make the disjointness test and
    the value guard mutually unsatisfiable, because the vendor table is *required* to declare
    `HCLOUD_TOKEN`. This asserts the failure mode rather than leaving it to a manual check. The
    assignment pattern is included: it names every declared credential, so it is the pattern most
    at risk of matching a bare declaration, and it must match only a name followed by a value.
    """
    for pattern in (*_FORBIDDEN_VALUE_PATTERNS, _credential_assignment_pattern()):
        for name in _declared_credential_names():
            match = pattern.search(name)
            assert match is None, (
                f"forbidden value pattern {pattern.pattern!r} matches declared credential name "
                f"{name!r} at {match.group(0)!r} — the value guard and the disjointness test "
                "cannot both pass"
            )
