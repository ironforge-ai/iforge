"""Bind this fork's front page to the destinations and identities it actually ships.

``README.md`` is the most-read file in the repository and the one place a reader forms a model
of what they installed. Through phases 03.1 and 03.3 the *rendered* surfaces moved -- the web
app's Help menu, the CLI's ``--help``, the support-contact sites -- while the front page kept
sending readers to upstream's website, repository, docs and npm package. Nothing caught it,
because ``README.md`` appeared in no plan's ``files_modified`` and no test read it.

That is the gap this module closes. Four properties, in the order they matter:

* every absolute destination in the header (the badge row and the link row) resolves to a host
  ``packages/shared/src/destinations.ts`` names, to the npm package ``packages/cli/package.json``
  publishes, or to the shields.io badge renderer;
* the apex homepage and the signed-in dashboard stay TWO entries. They render different hosts and
  the record's own docstring says sending a device-flow user to the apex sends them to the wrong
  host, so collapsing them is a defect a diff reader would plausibly miss;
* README names no npm package but this fork's own -- an install instruction naming somebody
  else's package is a supply-chain instruction, not a typo;
* README carries no retired identity: not the pre-migration state root, dotenv example filename or
  bundled-skill id, and not an upstream destination anywhere in the file.

Every expected value is READ from the authority rather than restated here. A copy in a test is a
second place to forget, which is the failure mode these suites exist to catch in prose.

Comparison is BYTE-EXACT against the ``DESTINATIONS`` values: no case folding, no trailing-slash
normalisation and no scheme inference. In particular the bare ``apexDomain`` (``iforge.dev``, no
scheme) does not authorise ``https://iforge.dev``; only ``webOrigin`` does. A href differing from
a destination by a trailing slash is a finding, not a match.

Shape follows ``test_compose_identity_contract.py``: repository root from ``tests.doc_contract``,
raw-line parsing wherever line numbers matter, and a loud failure on an enumeration that yields
nothing, so no assertion can pass by comparing an empty set to itself. The collector is fed a
``Path`` rather than a string precisely so the synthetic fixtures below travel the same read,
parse and sort path as the real file -- a failing direction that is proven rather than assumed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.doc_contract import REPO_ROOT as _REPO_ROOT

_README = _REPO_ROOT / "README.md"
_DESTINATIONS_TS = _REPO_ROOT / "packages" / "shared" / "src" / "destinations.ts"
_CLI_MANIFEST = _REPO_ROOT / "packages" / "cli" / "package.json"

# One scalar `key: "value"` pair of the DESTINATIONS record. Anchored to the start of a line so
# prose inside the file's docstrings -- which begins with `*` -- can never be read as a pair, and
# terminated at end-of-line so the `corsOrigins` / `externalHosts` array members, which carry no
# key, are not captured either.
_DESTINATION_PAIR = re.compile(
    r"^[ \t]*([A-Za-z][A-Za-z0-9_]*):[ \t]*\"([^\"]*)\",?[ \t]*$",
    re.MULTILINE,
)

# The record carries 13 scalar keys today. The floor is the point: a regex that stops matching
# after a formatting change would otherwise leave every assertion below comparing a href against
# an EMPTY set of destinations and reporting green over an unread authority.
_MINIMUM_DESTINATION_KEYS = 12

# HTML `href="..."` and `src="..."` attributes, and markdown `](...)` link targets.
_HTML_TARGET = re.compile(r"(?:href|src|srcset)=\"([^\"]+)\"")
_MARKDOWN_TARGET = re.compile(r"\]\(([^)\s]+)\)")

# The badge images are third-party-rendered SVGs the README already trusts; this fork changes
# their query parameters, not their provider.
_BADGE_RENDERER = "https://img.shields.io/"

_NPM_PACKAGE_URL_PREFIX = "https://www.npmjs.com/package/"
_NPM_PACKAGE_REFERENCE = re.compile(r"npmjs\.com/package/([^\"'\s)<>]+)")

# The header is the badge row and the link row: everything above the first level-2 heading.
_FIRST_SECTION_HEADING = re.compile(r"^## ", re.MULTILINE)

# Identities and destinations that must not reappear on the front page, each with the reason it
# is retired. The `clawdi://` reference SCHEME was deliberately absent until upstream sync step 4
# (06.1-11): it was then a persisted wire format the parser accepted. The fork's backend writes
# and only the fork reads it, so under the iforge-only rule the scheme is `iforge://` and the old
# spelling is retired like the rest.
_RETIRED_IDENTITIES: tuple[tuple[str, str], ...] = (
    ("clawdi://", "the pre-06.1-11 vault reference scheme; the parser accepts iforge://"),
    ("~/.clawdi", "the pre-migration CLI state root; lib/state-root.ts resolves ~/.iforge"),
    (".env.clawdi", "the pre-migration dotenv example filename; --help prints .env.iforge"),
    ("bundled `clawdi`", "the pre-migration bundled-skill id; registry.ts installs `iforge`"),
    ("Clawdi-AI/clawdi", "upstream's GitHub repository, which this fork does not own"),
    ("Clawdi-AI%2Fclawdi", "upstream's GitHub repository, URL-encoded for star-history.com"),
    ("https://clawdi.ai", "upstream's apex, which this fork cannot answer on"),
    ("npmjs.com/package/clawdi", "upstream's npm package, which this fork does not publish"),
)

# Occurrences of a retired identity this repository deliberately KEEPS, as `(anchor, reason)`.
#
# EMPTY, and empty on purpose: both README state-root sites were measured to be claims about
# where state lives NOW rather than migration instructions naming the legacy root as legacy, so
# plan 03.3-19 rewrote both instead of exempting either. The machinery is kept anyway and its
# enforcement is exercised in the failing direction by
# `test_a_synthetic_deliberate_reference_without_a_reason_is_refused` -- an empty tuple whose
# enforcement nobody ever ran is a fence nobody has tested.
DELIBERATE_LEGACY_REFERENCES: tuple[tuple[str, str], ...] = ()


class ReadmeContractError(RuntimeError):
    """The contract could not be established, as distinct from being violated."""


@dataclass(frozen=True, order=True)
class Finding:
    """One offending destination or identity, reported with enough detail to fix it."""

    line_number: int
    text: str

    def render(self) -> str:
        return f"line {self.line_number}: {self.text}"


def parse_destinations(
    path: Path = _DESTINATIONS_TS,
    minimum: int = _MINIMUM_DESTINATION_KEYS,
) -> dict[str, str]:
    """Read the scalar string values of the ``DESTINATIONS`` record straight from its source.

    Deliberately a regex over the TypeScript source: importing the module would need a bundler,
    and shelling out to one would make a prose contract depend on a toolchain it does not own.
    """
    pairs = _DESTINATION_PAIR.findall(path.read_text(encoding="utf-8"))
    values = dict(pairs)
    if len(values) < minimum:
        raise ReadmeContractError(
            f"{path.name} yielded {len(values)} destination keys, expected at least {minimum} -- "
            "the parser has drifted from the record and every comparison below would be vacuous"
        )
    return values


def published_package_name(path: Path = _CLI_MANIFEST) -> str:
    """The npm package this fork publishes, read from the manifest that declares it."""
    name = json.loads(path.read_text(encoding="utf-8")).get("name")
    if not name:
        raise ReadmeContractError(f"{path.name} declares no package name")
    return str(name)


def allowed_absolute_destinations(
    destinations: dict[str, str],
    package_name: str,
) -> tuple[str, ...]:
    """The absolute destinations the front page may name, byte-exact and scheme-explicit.

    Only ``https://`` values of the record are anchors. ``apexDomain``, ``githubOrg``,
    ``npmScope`` and ``supportEmail`` are deliberately excluded: authorising a href because it
    happens to contain a bare host would be exactly the scheme inference this module refuses.
    """
    anchors = [value for value in destinations.values() if value.startswith("https://")]
    anchors.append(_NPM_PACKAGE_URL_PREFIX + package_name)
    if not anchors:
        raise ReadmeContractError("no absolute destinations were resolved from the authority")
    return tuple(sorted(anchors))


def _is_anchored(url: str, anchors: tuple[str, ...]) -> bool:
    for anchor in anchors:
        if url == anchor or url.startswith(anchor + "/") or url.startswith(anchor + "?"):
            return True
    return False


def header_lines(path: Path) -> list[tuple[int, str]]:
    """The badge row and the link row: every line above the first level-2 heading."""
    text = path.read_text(encoding="utf-8")
    match = _FIRST_SECTION_HEADING.search(text)
    header = text[: match.start()] if match else text
    return list(enumerate(header.splitlines(), start=1))


def collect_foreign_header_destinations(
    path: Path,
    anchors: tuple[str, ...],
) -> tuple[Finding, ...]:
    """Every absolute header destination that resolves nowhere this fork owns.

    Relative paths and in-page fragments carry no host and are not findings. Findings are sorted
    by ``(line_number, text)`` so two runs over the same content produce identical output.
    """
    findings: list[Finding] = []
    for number, line in header_lines(path):
        targets = _HTML_TARGET.findall(line) + _MARKDOWN_TARGET.findall(line)
        for target in targets:
            url = target.split(" ")[0]
            if "://" not in url:
                continue
            if url.startswith(_BADGE_RENDERER):
                continue
            if _is_anchored(url, anchors):
                continue
            findings.append(Finding(line_number=number, text=url))
    return tuple(sorted(findings))


def resolve_deliberate_anchors(
    entries: tuple[tuple[str, str], ...] = DELIBERATE_LEGACY_REFERENCES,
) -> tuple[str, ...]:
    """The retired identities this repository keeps, refusing any entry that states no reason.

    An exemption without a reason reads as a decision while committing nobody to having made one,
    which is the shape ``config/rebrand-allowlist.txt`` refuses for the same class of fence.
    """
    anchors: list[str] = []
    for anchor, reason in entries:
        if not reason.strip():
            raise ReadmeContractError(
                f"deliberate legacy reference {anchor!r} carries no reason -- an exemption "
                "nobody justified is indistinguishable from an oversight"
            )
        anchors.append(anchor)
    return tuple(anchors)


def collect_retired_identities(
    path: Path,
    exempt: tuple[str, ...] = (),
) -> tuple[Finding, ...]:
    """Every line naming an identity or destination this fork has retired."""
    findings: list[Finding] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for anchor, _reason in _RETIRED_IDENTITIES:
            if anchor in exempt:
                continue
            if anchor in line:
                findings.append(Finding(line_number=number, text=anchor))
    return tuple(sorted(findings))


def test_header_destinations_all_resolve_to_something_this_fork_owns() -> None:
    destinations = parse_destinations()
    anchors = allowed_absolute_destinations(destinations, published_package_name())

    absolute = [
        target
        for _number, line in header_lines(_README)
        for target in _HTML_TARGET.findall(line) + _MARKDOWN_TARGET.findall(line)
        if "://" in target
    ]
    assert absolute, "no absolute destinations were read from the README header"

    findings = collect_foreign_header_destinations(_README, anchors)
    assert not findings, "README header names destinations this fork does not own:\n" + "\n".join(
        finding.render() for finding in findings
    )


def test_the_apex_and_the_dashboard_stay_two_distinct_entries() -> None:
    destinations = parse_destinations()
    apex = destinations["webOrigin"]
    dashboard = destinations["dashboardOrigin"]
    assert apex != dashboard, "the authority itself has collapsed the apex into the dashboard"

    readme = _README.read_text(encoding="utf-8")

    website = re.findall(r"<a href=\"([^\"]+)\">Website</a>", readme)
    assert website == [apex], f"the Website link must name {apex}, found {website}"

    cloud = re.findall(r"\[iforge Cloud dashboard\]\(([^)]+)\)", readme)
    assert cloud == [dashboard], f"the dashboard link must name {dashboard}, found {cloud}"


def test_readme_names_no_npm_package_but_this_forks_own() -> None:
    expected = published_package_name()
    referenced = set(_NPM_PACKAGE_REFERENCE.findall(_README.read_text(encoding="utf-8")))
    assert referenced, "no npm package reference was read from the README"
    assert referenced == {expected}, (
        f"README names npm packages other than {expected}: {sorted(referenced - {expected})}"
    )


def test_readme_carries_no_retired_identity() -> None:
    findings = collect_retired_identities(_README, exempt=resolve_deliberate_anchors())
    assert not findings, "README names identities this fork has retired:\n" + "\n".join(
        finding.render() for finding in findings
    )


def test_the_collector_reports_zero_offending_lines_on_the_real_tree() -> None:
    anchors = allowed_absolute_destinations(parse_destinations(), published_package_name())
    findings = collect_foreign_header_destinations(_README, anchors)
    assert len(findings) == 0, [finding.render() for finding in findings]


def test_a_synthetic_upstream_href_is_reported_with_its_line_named(tmp_path: Path) -> None:
    anchors = allowed_absolute_destinations(parse_destinations(), published_package_name())
    fixture = tmp_path / "README.md"
    fixture.write_text(
        '<h1 align="center">iforge</h1>\n'
        "\n"
        '<p align="center">\n'
        '  <a href="https://clawdi.ai">Website</a>\n'
        "</p>\n"
        "\n"
        "## Quickstart\n",
        encoding="utf-8",
    )

    findings = collect_foreign_header_destinations(fixture, anchors)

    assert len(findings) == 1
    rendered = findings[0].render()
    assert "https://clawdi.ai" in rendered
    assert "line 4" in rendered


def test_a_synthetic_deliberate_reference_without_a_reason_is_refused() -> None:
    with pytest.raises(ReadmeContractError):
        resolve_deliberate_anchors(((".env.clawdi", "   "),))

    assert resolve_deliberate_anchors(((".env.clawdi", "a stated reason"),)) == (".env.clawdi",)


def test_a_synthetic_short_destinations_parse_is_refused(tmp_path: Path) -> None:
    truncated = tmp_path / "destinations.ts"
    truncated.write_text(
        'export const DESTINATIONS = Object.freeze({\n\twebOrigin: "https://iforge.dev",\n});\n',
        encoding="utf-8",
    )

    with pytest.raises(ReadmeContractError):
        parse_destinations(truncated)

    assert len(parse_destinations()) >= _MINIMUM_DESTINATION_KEYS


def test_the_collector_output_is_stable_across_runs(tmp_path: Path) -> None:
    anchors = allowed_absolute_destinations(parse_destinations(), published_package_name())
    fixture = tmp_path / "README.md"
    # Both findings on line 2 are what proves the tie-break: a collector sorted by line number
    # alone would report them in document order, which is the order they are NOT written in here.
    fixture.write_text(
        '<p align="center">\n'
        '  <a href="https://zzz.example/two">Two</a> <a href="https://aaa.example/one">One</a>\n'
        "</p>\n"
        '<a href="https://clawdi.ai">Website</a>\n'
        "\n"
        "## Quickstart\n",
        encoding="utf-8",
    )

    first = [finding.render() for finding in collect_foreign_header_destinations(fixture, anchors)]
    second = [finding.render() for finding in collect_foreign_header_destinations(fixture, anchors)]

    assert first == second
    assert first == [
        "line 2: https://aaa.example/one",
        "line 2: https://zzz.example/two",
        "line 4: https://clawdi.ai",
    ]
