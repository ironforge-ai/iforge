"""Prove the origin gate's verdict logic in both directions, without waiting for DNS.

``scripts/check_production_origins.py`` is red today and no commit in this repository can make it
green: the records it probes live in a Cloudflare zone and on a VPS. That is precisely why its
logic has to be proven here. A gate nobody has seen exit 0 is not known to fail in the right
direction -- it is only known to fail.

Every assertion below drives the guard's pure functions with a *recorded* observation: a small
frozen dataclass carrying a status code, an error name or an MX answer. No socket is opened, no
resolver is called, and no test depends on the state of the zone at the moment it runs. The two
tests that exercise ``main`` monkeypatch the two network adapters, which are the guard's only
egress, so even the end-to-end exit status is established offline.

The measured baseline is recorded as a named fixture rather than as prose, so this suite documents
the state the gate was written against and fails if the verdict mapping for that state ever moves.

Hostnames are read from the destinations record, never typed here, for the same reason the guard
does not type them: a name written in two places drifts in one of them.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys

import pytest

from tests.doc_contract import REPO_ROOT

_GATE_PATH = REPO_ROOT / "scripts" / "check_production_origins.py"
_TEST_SH = REPO_ROOT / "scripts" / "test.sh"


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_production_origins", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: ``@dataclass`` resolves its own defining module through
    # ``sys.modules``, and an unregistered module makes that lookup return ``None`` and raise.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

DESTINATIONS = gate.parse_destinations(gate.DESTINATIONS_FILE.read_text(encoding="utf-8"))
DECLARED_HOSTS = gate.declared_hosts(DESTINATIONS)
SUPPORT_DOMAIN = gate.support_domain(DESTINATIONS)

# ------------------------------------------------------------------------------------------
# The recorded baseline: what this host measured on 2026-09-11, in the declared origin order
# (webOrigin, wwwOrigin, dashboardOrigin, cloudApiOrigin, deployApiOrigin, assetsOrigin).
#
# Four hosts had no address record. Two returned HTTP 525 -- the edge reached the origin and the
# origin did not complete a handshake the edge would accept.
#
# The apex MX answer moved DURING the execution of plan 03.3-22: at 00:59:51Z the apex answered
# NOERROR with zero MX records, and at 01:02:39Z it answered with three. Both observations are
# recorded because both are real, and because the pair is the evidence that the mail half of this
# gate can change under a run while the origin half does not.
# ------------------------------------------------------------------------------------------

BASELINE_DATE = "2026-09-11"
BASELINE_TLS_HANDSHAKE_FAILURE_STATUS = 525
BASELINE_NO_ADDRESS_RECORD = "no address record"
BASELINE_ORIGIN_VERDICTS = (
    gate.UNRESOLVED,
    gate.UNRESOLVED,
    gate.UNRESOLVED,
    gate.NO_TLS,
    gate.NO_TLS,
    gate.UNRESOLVED,
)
BASELINE_MX_BEFORE = ()
BASELINE_MX_AFTER = (
    "21 route1.mx.cloudflare.net.",
    "49 route2.mx.cloudflare.net.",
    "54 route3.mx.cloudflare.net.",
)


def _baseline_observations() -> list[gate.OriginObservation]:
    observations = []
    for host, verdict in zip(DECLARED_HOSTS, BASELINE_ORIGIN_VERDICTS, strict=True):
        if verdict == gate.NO_TLS:
            observations.append(
                gate.OriginObservation(host=host, status=BASELINE_TLS_HANDSHAKE_FAILURE_STATUS)
            )
        else:
            observations.append(gate.OriginObservation(host=host, error=BASELINE_NO_ADDRESS_RECORD))
    return observations


def _served(hosts: list[str], status: int = 200) -> list[gate.OriginObservation]:
    return [gate.OriginObservation(host=host, status=status) for host in hosts]


def _mail(records: tuple[str, ...]) -> gate.MailObservation:
    return gate.MailObservation(host=SUPPORT_DOMAIN, mx_records=records)


# ------------------------------------------------------------------------------------------
# Behaviour 1 -- a recorded 525 is reachable-but-no-TLS and contributes a failure.
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(gate.TLS_HANDSHAKE_FAILURE_STATUSES))
def test_edge_reported_origin_handshake_failure_is_no_tls(status: int) -> None:
    verdict, detail = gate.origin_verdict(gate.OriginObservation(host="h", status=status))
    assert verdict == gate.NO_TLS
    assert str(status) in detail


def test_a_recorded_525_contributes_a_failure() -> None:
    hosts = DECLARED_HOSTS[:1]
    report = gate.build_report(
        hosts,
        [gate.OriginObservation(host=hosts[0], status=525)],
        _mail(BASELINE_MX_AFTER),
    )
    assert report.origins[0].verdict == gate.NO_TLS
    assert report.ok is False
    assert "1 no-tls" in gate.summary_line(report)


def test_no_tls_is_not_reported_as_served() -> None:
    """525 must not fall through to SERVED just because a status came back.

    The request reached the edge and never reached the thing holding the data. A build whose
    credential destination answers only this is not shippable, so the distinction is the whole
    reason the verdict vocabulary has three values rather than two.
    """
    verdict, _ = gate.origin_verdict(gate.OriginObservation(host="h", status=525))
    assert verdict != gate.SERVED


# ------------------------------------------------------------------------------------------
# Behaviour 2 -- a connection failure with no address record is UNRESOLVED and fails.
# ------------------------------------------------------------------------------------------


def test_no_address_record_is_unresolved() -> None:
    verdict, detail = gate.origin_verdict(
        gate.OriginObservation(host="h", error=BASELINE_NO_ADDRESS_RECORD)
    )
    assert verdict == gate.UNRESOLVED
    assert BASELINE_NO_ADDRESS_RECORD in detail


@pytest.mark.parametrize("error", ["no address record", "ssl SSLCertVerificationError", "timeout"])
def test_any_observation_without_a_status_is_unresolved(error: str) -> None:
    verdict, _ = gate.origin_verdict(gate.OriginObservation(host="h", error=error))
    assert verdict == gate.UNRESOLVED


def test_unresolved_contributes_a_failure() -> None:
    hosts = DECLARED_HOSTS[:1]
    report = gate.build_report(
        hosts,
        [gate.OriginObservation(host=hosts[0], error=BASELINE_NO_ADDRESS_RECORD)],
        _mail(BASELINE_MX_AFTER),
    )
    assert report.ok is False
    assert "1 unresolved" in gate.summary_line(report)


# ------------------------------------------------------------------------------------------
# Behaviour 3 -- 200, 301 and 404 are all SERVED. The gate asks whether the host answers over
# TLS, not what it answers.
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 204, 301, 302, 401, 403, 404, 500, 502, 503])
def test_any_status_outside_the_handshake_failure_set_is_served(status: int) -> None:
    assert status not in gate.TLS_HANDSHAKE_FAILURE_STATUSES
    assert status not in gate.ORIGIN_UNREACHABLE_STATUSES
    verdict, detail = gate.origin_verdict(gate.OriginObservation(host="h", status=status))
    assert verdict == gate.SERVED
    assert detail == f"HTTP {status}"


@pytest.mark.parametrize("status", [200, 301, 404])
def test_a_served_response_contributes_no_failure(status: int) -> None:
    report = gate.build_report(
        DECLARED_HOSTS, _served(DECLARED_HOSTS, status), _mail(BASELINE_MX_AFTER)
    )
    assert report.counts[gate.SERVED] == len(DECLARED_HOSTS)
    assert report.ok is True


# ------------------------------------------------------------------------------------------
# Behaviour 3a -- the edge's own "origin unreachable" statuses are NOT served.
#
# A proxied name with nothing deployed behind it (a stopped service, a closed port, a tunnel
# with no connector) answers 520-524 or 530 from the edge. Counting those as SERVED would let
# the gate pass on an empty host.
# ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(gate.ORIGIN_UNREACHABLE_STATUSES))
def test_edge_reported_unreachable_origin_is_no_origin(status: int) -> None:
    verdict, detail = gate.origin_verdict(gate.OriginObservation(host="h", status=status))
    assert verdict == gate.NO_ORIGIN
    assert str(status) in detail


def test_an_unreachable_origin_contributes_a_failure() -> None:
    hosts = DECLARED_HOSTS[:1]
    report = gate.build_report(
        hosts,
        [gate.OriginObservation(host=hosts[0], status=521)],
        _mail(BASELINE_MX_AFTER),
    )
    assert report.origins[0].verdict == gate.NO_ORIGIN
    assert report.ok is False
    assert "1 no-origin" in gate.summary_line(report)


def test_the_two_edge_failure_sets_are_disjoint() -> None:
    assert not gate.ORIGIN_UNREACHABLE_STATUSES & gate.TLS_HANDSHAKE_FAILURE_STATUSES


# ------------------------------------------------------------------------------------------
# Behaviour 3b -- a redirect that never settles is NOT served.
#
# Measured 2026-10-26: four origins answered 301 with a Location naming the exact URL that was
# requested. No service was deployed behind them; whatever the edge reached only redirected to
# https, forever. A browser shows "too many redirects"; the gate read HTTP 301 and said
# SERVED. The probe now follows redirects, and the observation records where it went.
# ------------------------------------------------------------------------------------------


def test_a_redirect_back_to_the_requested_url_is_a_bad_redirect() -> None:
    observation = gate.OriginObservation(
        host="h",
        status=301,
        redirects=("https://h/",),
        redirect_problem="redirect loop at https://h/",
    )
    verdict, detail = gate.origin_verdict(observation)
    assert verdict == gate.BAD_REDIRECT
    assert "loop" in detail and "301" in detail


def test_a_bad_redirect_contributes_a_failure() -> None:
    hosts = DECLARED_HOSTS[:1]
    report = gate.build_report(
        hosts,
        [
            gate.OriginObservation(
                host=hosts[0],
                status=301,
                redirects=(f"https://{hosts[0]}/",),
                redirect_problem=f"redirect loop at https://{hosts[0]}/",
            )
        ],
        _mail(BASELINE_MX_AFTER),
    )
    assert report.origins[0].verdict == gate.BAD_REDIRECT
    assert report.ok is False
    assert "1 bad-redirect" in gate.summary_line(report)


def test_a_redirect_that_settles_on_another_origin_is_served() -> None:
    """The www alias redirecting to the apex, which answers 200, is a working origin."""
    verdict, detail = gate.origin_verdict(
        gate.OriginObservation(host="www.h", status=200, redirects=("https://h/",))
    )
    assert verdict == gate.SERVED
    assert detail == "HTTP 200 after 1 redirect(s), ending at https://h/"


@pytest.mark.parametrize(
    ("chain", "problem"),
    [
        ({"https://h/": "https://h/"}, "redirect loop at https://h/"),
        (
            {"https://h/": "https://h/a", "https://h/a": "https://h/"},
            "redirect loop at https://h/",
        ),
        ({"https://h/": "http://h/"}, "redirect leaves https for http://h/"),
        ({"https://h/": None}, "redirect with no Location header"),
    ],
)
def test_the_probe_records_why_a_redirect_did_not_settle(monkeypatch, chain, problem) -> None:
    def fetch(url: str, timeout: float) -> tuple[int, str | None]:
        return 301, chain[url]

    monkeypatch.setattr(gate, "_fetch_once", fetch)
    observation = gate.probe_origin("h")
    assert observation.status == 301
    assert observation.redirect_problem == problem


def test_the_probe_gives_up_after_the_redirect_limit(monkeypatch) -> None:
    def fetch(url: str, timeout: float) -> tuple[int, str | None]:
        return 302, f"{url}x"

    monkeypatch.setattr(gate, "_fetch_once", fetch)
    observation = gate.probe_origin("h")
    assert len(observation.redirects) == gate.MAX_REDIRECTS
    assert observation.redirect_problem == f"still redirecting after {gate.MAX_REDIRECTS} hops"


def test_the_probe_follows_a_relative_location_to_a_final_status(monkeypatch) -> None:
    answers = {"https://h/": (308, "/home"), "https://h/home": (200, None)}
    monkeypatch.setattr(gate, "_fetch_once", lambda url, timeout: answers[url])
    observation = gate.probe_origin("h")
    assert observation.status == 200
    assert observation.redirects == ("https://h/home",)
    assert observation.redirect_problem is None


# ------------------------------------------------------------------------------------------
# Behaviour 4 -- the apex MX record, in both directions.
# ------------------------------------------------------------------------------------------


def test_absent_mx_is_no_mail() -> None:
    verdict, detail = gate.mail_verdict(_mail(BASELINE_MX_BEFORE))
    assert verdict == gate.NO_MAIL
    assert "discarded" in detail


def test_present_mx_is_mail() -> None:
    verdict, detail = gate.mail_verdict(_mail(BASELINE_MX_AFTER))
    assert verdict == gate.MAIL
    assert str(len(BASELINE_MX_AFTER)) in detail


def test_absent_mx_fails_the_gate_even_when_every_origin_is_served() -> None:
    report = gate.build_report(DECLARED_HOSTS, _served(DECLARED_HOSTS), _mail(BASELINE_MX_BEFORE))
    assert report.counts[gate.SERVED] == len(DECLARED_HOSTS)
    assert report.ok is False
    assert gate.summary_line(report).endswith(f"mail {gate.NO_MAIL}")


def test_present_mx_contributes_no_failure() -> None:
    report = gate.build_report(DECLARED_HOSTS, _served(DECLARED_HOSTS), _mail(BASELINE_MX_AFTER))
    assert report.ok is True


def test_a_negative_from_the_system_resolver_is_escalated_to_the_zones_nameservers(
    monkeypatch,
) -> None:
    """A cached NXRRSET is not a fact about the zone, and must not be recorded as one.

    Measured while writing this guard: after the apex MX records were added, one host's resolver
    kept answering NOERROR-with-no-MX from a negative cache entry carrying the *pre-change* SOA
    serial, while ``1.1.1.1`` and the zone's own nameservers returned three records. The gate
    flapped between MAIL and NO_MAIL across runs minutes apart against an unchanging zone.
    """
    asked: list[list[str]] = []

    def _run(command, timeout):
        asked.append(command)
        if command[2] == "NS":
            return "ns-two.example.\nns-one.example.\n"
        if command[-1].startswith("@"):
            return "21 route1.mx.example.\n"
        return ""  # the system resolver's stale negative

    monkeypatch.setattr(gate, "_run_resolver", _run)
    observation = gate.probe_mail("zone.example")
    assert observation.mx_records == ("21 route1.mx.example.",)
    assert gate.mail_verdict(observation)[0] == gate.MAIL
    assert observation.source == "ns-one.example."
    assert any(command[-1].startswith("@") for command in asked)


def test_a_positive_from_the_system_resolver_is_not_escalated(monkeypatch) -> None:
    """The escalation runs on the negative path only. A positive answer needs no second opinion."""
    asked: list[list[str]] = []

    def _run(command, timeout):
        asked.append(command)
        return "21 route1.mx.example.\n"

    monkeypatch.setattr(gate, "_run_resolver", _run)
    observation = gate.probe_mail("zone.example")
    assert observation.source == "the system resolver"
    assert [command[2] for command in asked] == ["MX"]


def test_a_negative_from_the_zones_own_nameservers_is_no_mail(monkeypatch) -> None:
    """Escalation cannot invent records. Only a real absence still reads NO_MAIL."""
    monkeypatch.setattr(
        gate,
        "_run_resolver",
        lambda command, timeout: "ns-one.example.\n" if command[2] == "NS" else "",
    )
    observation = gate.probe_mail("zone.example")
    assert observation.mx_records == ()
    assert gate.mail_verdict(observation)[0] == gate.NO_MAIL


def test_asking_nobody_reads_as_no_mail(monkeypatch) -> None:
    """No resolver client at all is the failing direction: unestablished is not established."""
    monkeypatch.setattr(gate, "_run_resolver", lambda command, timeout: None)
    observation = gate.probe_mail("zone.example")
    assert observation.mx_records == ()
    assert gate.mail_verdict(observation)[0] == gate.NO_MAIL


def test_the_mail_detail_names_which_resolver_answered() -> None:
    """A report that cannot say who it asked cannot be argued with."""
    verdict, detail = gate.mail_verdict(
        gate.MailObservation(host="zone.example", mx_records=("21 mx.example.",), source="ns.")
    )
    assert verdict == gate.MAIL
    assert "ns." in detail
    verdict, detail = gate.mail_verdict(
        gate.MailObservation(host="zone.example", source="the system resolver")
    )
    assert verdict == gate.NO_MAIL
    assert "the system resolver" in detail


def test_the_mail_row_names_the_domain_of_the_rendered_support_address() -> None:
    """The mail question is asked of the address the product actually renders.

    ``supportEmail`` is what appears at the billing dunning banner and the deactivated-account
    page, so its domain -- not the apex key, which could drift from it -- is what must accept
    mail. Today they are the same string; the gate derives it from the address so that they
    staying the same is checked rather than assumed.
    """
    assert SUPPORT_DOMAIN == gate.support_domain(DESTINATIONS)
    assert DESTINATIONS[gate.SUPPORT_EMAIL_KEY].endswith(f"@{SUPPORT_DOMAIN}")


# ------------------------------------------------------------------------------------------
# Behaviour 5 -- everything served and mail present: exit 0, one row per origin.
# ------------------------------------------------------------------------------------------


def test_all_served_with_mail_exits_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        gate, "probe_origin", lambda host, **_: gate.OriginObservation(host=host, status=200)
    )
    monkeypatch.setattr(gate, "probe_mail", lambda domain, **_: _mail(BASELINE_MX_AFTER))
    assert gate.main([]) == 0
    out = capsys.readouterr().out
    assert "production origins passed" in out
    assert out.strip().splitlines()[-1] == (
        f"production origins passed: {len(DECLARED_HOSTS)} origins probed, "
        f"{len(DECLARED_HOSTS)} served, 0 no-tls, 0 no-origin, 0 unresolved, 0 bad-redirect; "
        f"mail {gate.MAIL}"
    )


def test_all_served_prints_one_row_per_origin_plus_the_mail_row() -> None:
    report = gate.build_report(DECLARED_HOSTS, _served(DECLARED_HOSTS), _mail(BASELINE_MX_AFTER))
    lines = gate.render_rows(report).splitlines()
    assert len(lines) == len(DECLARED_HOSTS) + 1
    assert sum(1 for line in lines if line.startswith("origin ")) == len(DECLARED_HOSTS)
    assert sum(1 for line in lines if line.startswith("mail ")) == 1


def test_the_measured_baseline_maps_to_the_recorded_verdicts() -> None:
    """The 2026-09-11 measurement, replayed. This is the state the gate was written against."""
    report = gate.build_report(DECLARED_HOSTS, _baseline_observations(), _mail(BASELINE_MX_BEFORE))
    assert [row.verdict for row in report.origins] == list(BASELINE_ORIGIN_VERDICTS)
    assert report.mail.verdict == gate.NO_MAIL
    assert report.ok is False
    assert gate.summary_line(report) == (
        f"production origins failed: {len(DECLARED_HOSTS)} origins probed, 0 served, "
        f"2 no-tls, 0 no-origin, 4 unresolved, 0 bad-redirect; mail {gate.NO_MAIL}"
    )


def test_the_baseline_with_mail_added_still_fails_on_the_origins() -> None:
    """Adding the MX record does not make the gate green; four hosts still have no address."""
    report = gate.build_report(DECLARED_HOSTS, _baseline_observations(), _mail(BASELINE_MX_AFTER))
    assert report.mail.verdict == gate.MAIL
    assert report.ok is False
    assert gate.summary_line(report) == (
        f"production origins failed: {len(DECLARED_HOSTS)} origins probed, 0 served, "
        f"2 no-tls, 0 no-origin, 4 unresolved, 0 bad-redirect; mail {gate.MAIL}"
    )


# ------------------------------------------------------------------------------------------
# Behaviour 6 -- an empty origin list says so and NEVER exits 0.
# ------------------------------------------------------------------------------------------


def test_empty_origin_list_is_not_ok() -> None:
    report = gate.build_report([], [], _mail(BASELINE_MX_AFTER))
    assert report.origins == ()
    assert report.ok is False
    assert "0 origins probed" in gate.summary_line(report)


def test_empty_origin_list_exits_non_zero_even_with_mail_present(monkeypatch, capsys) -> None:
    """A gate that passes because it iterated nothing is the defect this whole set closes."""
    monkeypatch.setattr(gate, "probe_mail", lambda domain, **_: _mail(BASELINE_MX_AFTER))
    monkeypatch.setattr(
        gate, "probe_origin", lambda host, **_: gate.OriginObservation(host=host, status=200)
    )
    assert gate.main(["--hosts", ","]) == 1
    out = capsys.readouterr().out
    assert "0 origins probed" in out
    assert "production origins failed" in out


def test_a_short_parse_fails_rather_than_narrowing_the_gate() -> None:
    with pytest.raises(gate.ProductionOriginsError) as raised:
        gate.parse_destinations('export const DESTINATIONS = Object.freeze({\n\tfoo: "bar",\n})')
    assert "required keys" in str(raised.value)


def test_the_declared_record_parses_the_full_required_key_set() -> None:
    assert len(DESTINATIONS) == gate._MINIMUM_PARSED_KEYS
    assert set(DESTINATIONS) == {*gate.ORIGIN_KEYS, gate.APEX_KEY, gate.SUPPORT_EMAIL_KEY}


# ------------------------------------------------------------------------------------------
# Behaviour 7 -- byte-exact ASCII host comparison. No IDNA fold, no case fold, no inferred
# scheme.
# ------------------------------------------------------------------------------------------


def test_host_matches_is_byte_exact() -> None:
    host = DECLARED_HOSTS[0]
    assert gate.host_matches(host, host) is True
    assert gate.host_matches(host.upper(), host) is False
    assert gate.host_matches(host + ".", host) is False
    assert gate.host_matches(" " + host, host) is False


def test_a_punycode_form_is_not_accepted_as_a_match() -> None:
    """``xn--`` labels are a different sequence of bytes and therefore a different host.

    The gate's claim is about the exact name a shipped binary dials. Folding a punycode
    lookalike into a match would let a host nobody in this fork owns satisfy the check.
    """
    declared = DECLARED_HOSTS[0]
    punycode = "xn--" + declared.replace(".", "-", 1)
    assert punycode != declared
    assert gate.host_matches(punycode, declared) is False
    with pytest.raises(gate.ProductionOriginsError) as raised:
        gate.build_report(
            [declared],
            [gate.OriginObservation(host=punycode, status=200)],
            _mail(BASELINE_MX_AFTER),
        )
    assert "byte-exact" in str(raised.value)


def test_a_case_varied_form_is_not_accepted_as_a_match() -> None:
    declared = DECLARED_HOSTS[0]
    with pytest.raises(gate.ProductionOriginsError):
        gate.build_report(
            [declared],
            [gate.OriginObservation(host=declared.upper(), status=200)],
            _mail(BASELINE_MX_AFTER),
        )


def test_no_scheme_is_ever_inferred() -> None:
    with pytest.raises(gate.ProductionOriginsError) as raised:
        gate.host_from_origin(DECLARED_HOSTS[0])
    assert gate._REQUIRED_SCHEME in str(raised.value)
    with pytest.raises(gate.ProductionOriginsError):
        gate.host_from_origin("http://" + DECLARED_HOSTS[0])


def test_a_non_ascii_origin_is_rejected_rather_than_folded() -> None:
    with pytest.raises(gate.ProductionOriginsError) as raised:
        gate.host_from_origin(f"{gate._REQUIRED_SCHEME}exämple.test")
    assert "ASCII" in str(raised.value)


def test_the_guard_hardcodes_no_hostname_from_the_destinations_record() -> None:
    """Equivalent to ``grep -c '<apex>' scripts/check_production_origins.py`` returning 0."""
    source = _GATE_PATH.read_text(encoding="utf-8")
    for value in DESTINATIONS.values():
        assert value not in source
    for host in DECLARED_HOSTS:
        assert host not in source
    assert SUPPORT_DOMAIN not in source


# ------------------------------------------------------------------------------------------
# Behaviour 8 -- the apex and its ``www`` alias are two rows. Nothing merges.
# ------------------------------------------------------------------------------------------


def test_the_row_count_equals_the_declared_origin_count() -> None:
    report = gate.build_report(DECLARED_HOSTS, _served(DECLARED_HOSTS), _mail(BASELINE_MX_AFTER))
    assert len(report.origins) == len(gate.ORIGIN_KEYS) == len(DECLARED_HOSTS)


def test_the_apex_and_its_www_alias_are_two_distinct_rows() -> None:
    apex_host = gate.host_from_origin(DESTINATIONS["webOrigin"])
    www_host = gate.host_from_origin(DESTINATIONS["wwwOrigin"])
    assert apex_host != www_host
    assert www_host.endswith(apex_host)
    report = gate.build_report(DECLARED_HOSTS, _served(DECLARED_HOSTS), _mail(BASELINE_MX_AFTER))
    hosts = [row.host for row in report.origins]
    assert apex_host in hosts
    assert www_host in hosts


def test_two_declared_entries_naming_the_same_host_stay_two_rows() -> None:
    """Adjacency: identical neighbours are probed twice and reported twice, never deduplicated.

    A gate that collapsed them would report one verdict for two declared entries, so deleting
    one entry's record would read as covered by the other's.
    """
    host = DECLARED_HOSTS[0]
    report = gate.build_report([host, host], _served([host, host]), _mail(BASELINE_MX_AFTER))
    assert len(report.origins) == 2
    assert [row.host for row in report.origins] == [host, host]
    assert len(gate.render_rows(report).splitlines()) == 3


def test_a_length_mismatch_between_declared_and_observed_is_an_error() -> None:
    with pytest.raises(gate.ProductionOriginsError):
        gate.build_report(DECLARED_HOSTS, _served(DECLARED_HOSTS[:-1]), _mail(BASELINE_MX_AFTER))


# ------------------------------------------------------------------------------------------
# Behaviour 9 -- determinism. Rows come out in declared order, byte-identically, every run.
# ------------------------------------------------------------------------------------------


def test_two_runs_over_the_same_fixtures_are_byte_identical() -> None:
    observations = _baseline_observations()
    first = gate.build_report(DECLARED_HOSTS, observations, _mail(BASELINE_MX_BEFORE))
    second = gate.build_report(DECLARED_HOSTS, observations, _mail(BASELINE_MX_BEFORE))
    assert gate.render_rows(first) == gate.render_rows(second)
    assert gate.render_json(first) == gate.render_json(second)
    assert gate.summary_line(first) == gate.summary_line(second)


def test_rows_are_emitted_in_the_declared_origin_order() -> None:
    report = gate.build_report(DECLARED_HOSTS, _baseline_observations(), _mail(BASELINE_MX_BEFORE))
    assert [row.host for row in report.origins] == DECLARED_HOSTS
    rendered = gate.render_rows(report).splitlines()
    for line, host in zip(rendered, DECLARED_HOSTS, strict=False):
        assert host in line


# ------------------------------------------------------------------------------------------
# The ``--json`` contract. The publish gate reads ``origins[*].host``, so the shape is a
# contract rather than an illustration.
# ------------------------------------------------------------------------------------------


def test_json_carries_origins_mail_and_summary() -> None:
    report = gate.build_report(DECLARED_HOSTS, _baseline_observations(), _mail(BASELINE_MX_BEFORE))
    payload = json.loads(gate.render_json(report))
    assert set(payload) == {"origins", "mail", "summary"}
    assert [entry["host"] for entry in payload["origins"]] == DECLARED_HOSTS
    assert [entry["verdict"] for entry in payload["origins"]] == list(BASELINE_ORIGIN_VERDICTS)
    assert payload["mail"]["verdict"] == gate.NO_MAIL
    assert payload["summary"] == gate.summary_line(report)


def test_json_is_exactly_one_object_on_stdout(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        gate, "probe_origin", lambda host, **_: gate.OriginObservation(host=host, status=200)
    )
    monkeypatch.setattr(gate, "probe_mail", lambda domain, **_: _mail(BASELINE_MX_AFTER))
    assert gate.main(["--json"]) == 0
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1
    assert json.loads(out)["summary"].startswith("production origins passed")


def test_the_exit_status_is_the_same_with_and_without_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        gate, "probe_origin", lambda host, **_: gate.OriginObservation(host=host, status=525)
    )
    monkeypatch.setattr(gate, "probe_mail", lambda domain, **_: _mail(BASELINE_MX_AFTER))
    assert gate.main([]) == 1
    assert gate.main(["--json"]) == 1
    capsys.readouterr()


def test_hosts_overrides_the_origin_list_only(monkeypatch, capsys) -> None:
    """``--hosts`` never moves the mail row.

    Mail acceptance is a property of the domain the support address names, not of whichever host
    was passed in. An override that also moved the mail row would let a caller make the mail
    question disappear by naming a different host.
    """
    monkeypatch.setattr(
        gate, "probe_origin", lambda host, **_: gate.OriginObservation(host=host, status=200)
    )
    seen: list[str] = []

    def _probe_mail(domain, **_):
        seen.append(domain)
        return _mail(BASELINE_MX_AFTER)

    monkeypatch.setattr(gate, "probe_mail", _probe_mail)
    assert gate.main(["--hosts", "example.test,other.test", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [entry["host"] for entry in payload["origins"]] == ["example.test", "other.test"]
    assert payload["mail"]["host"] == SUPPORT_DOMAIN
    assert seen == [SUPPORT_DOMAIN]


# ------------------------------------------------------------------------------------------
# Where this gate is, and is not, wired.
# ------------------------------------------------------------------------------------------


def test_the_gate_is_not_wired_into_the_canonical_suite() -> None:
    """Deliberate, and asserted so a later edit has to argue with a test rather than a comment.

    The gate is red until a Cloudflare zone changes. No commit here can make it green, so wiring
    it into ``scripts/test.sh`` would turn the canonical suite permanently red for a condition
    nobody working in this repository can fix -- which trains people to ignore a red suite. Its
    enforcement point is the publish path instead.
    """
    assert "check_production_origins" not in _TEST_SH.read_text(encoding="utf-8")


def test_the_native_release_path_consults_the_gate() -> None:
    native_release = REPO_ROOT / "packages" / "cli" / "scripts" / "check-native-release.mjs"
    assert "check_production_origins" in native_release.read_text(encoding="utf-8")


def test_the_npm_publish_path_consults_the_gate() -> None:
    """``prepublishOnly`` runs the origin check, and runs it BEFORE ``build``.

    Human-approved extension beyond plan 03.3-22's declared ``files_modified``. Without it the
    npm path was ungated: ``prepublishOnly`` called only ``check:publish-manifest`` and ``build``,
    and ``build`` is what bakes both API origins into the bundle with ``--define``. So
    ``npm publish`` would have shipped a binary hard-pointed at two hosts returning 525 while
    ``check:native-release`` -- the path that WAS gated -- was never on that route.

    ``check:native-release`` is deliberately NOT what runs here. It needs a ``dist-release/``
    that a plain publish never builds, so calling it would fail for a missing artifact rather
    than for an unserved origin, and break publishing for the wrong reason.
    """
    package_json = json.loads(
        (REPO_ROOT / "packages" / "cli" / "package.json").read_text(encoding="utf-8")
    )
    prepublish = package_json["scripts"]["prepublishOnly"]
    assert "check_production_origins" in prepublish
    assert "check:native-release" not in prepublish
    assert prepublish.index("check_production_origins") < prepublish.index("bun run build")


def test_the_publish_path_has_no_way_to_turn_the_gate_off() -> None:
    """An escape hatch would be used exactly once, in the situation the gate exists to prevent."""
    native_release = (
        REPO_ROOT / "packages" / "cli" / "scripts" / "check-native-release.mjs"
    ).read_text(encoding="utf-8")
    hatch = re.compile(r"(env|process\.env)[^)]*(SKIP|ALLOW|FORCE|OVERRIDE)", re.IGNORECASE)
    assert hatch.search(native_release) is None


def test_the_guard_names_no_private_address_or_vendor_account_detail() -> None:
    """AGENTS.md OSS boundary. Only public hostnames the shipped binary already contains."""
    source = _GATE_PATH.read_text(encoding="utf-8")
    private_ipv4 = re.compile(r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b|\b192\.168\.\d")
    assert private_ipv4.search(source) is None
    for forbidden in ("root@", "ssh -", "kamal", "DEPLOY_HOST", ".kamal/secrets"):
        assert forbidden not in source
