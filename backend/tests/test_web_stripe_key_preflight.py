"""Prove the hosted web Stripe-key preflight in both directions (G-06.3-3).

The 06.3 UAT walk started the web dev server with no ``VITE_STRIPE_PUBLISHABLE_KEY``. That key is
a documented prerequisite for a hosted walk -- wallet top-ups are Stripe Elements only, so without
it the payment form cannot load -- yet nothing noticed: ``scripts/uat-stack.sh`` neither set nor
checked it, and the web env schema treats it as optional. ``scripts/check-web-stripe-key.sh``
closes that gap, and ``uat-stack.sh`` calls it in its preflight. It also runs standalone, because
the failing walk started the web server directly.

The check is PRESENCE ONLY. A publishable key is not a secret in the browser, but an operator's env
files are not a place a script should be echoing values from, and a preflight has no reason to
know the value -- only whether it resolved. Every case below therefore asserts that the fixture key
string appears in neither stdout nor stderr.

Every case builds its own web directory under ``tmp_path`` and runs the helper with an environment
holding only ``PATH`` and ``HOME`` plus the case's own variables. Nothing here reads or writes the
real ``apps/web`` env files: they belong to the operator, and a test whose verdict depends on what
happens to be in them on this machine proves nothing about the helper.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.doc_contract import REPO_ROOT

HELPER = REPO_ROOT / "scripts" / "check-web-stripe-key.sh"
UAT_STACK = REPO_ROOT / "scripts" / "uat-stack.sh"

FIXTURE_KEY = "pk_test_FIXTUREVALUE123"
KEY_NAME = "VITE_STRIPE_PUBLISHABLE_KEY"


def _web_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    web = tmp_path / "web"
    web.mkdir()
    for name, body in files.items():
        (web / name).write_text(body, encoding="utf-8")
    return web


def _run(
    web: Path, env: dict[str, str] | None = None, *extra: str
) -> subprocess.CompletedProcess[str]:
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(web.parent)}
    base.update(env or {})
    result = subprocess.run(
        ["bash", str(HELPER), str(web), *extra],
        env=base,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    # T-06.3-17-01: presence only. Asserted on every run, not in one dedicated case.
    assert FIXTURE_KEY not in result.stdout, result.stdout
    assert FIXTURE_KEY not in result.stderr, result.stderr
    return result


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


# ------------------------------------------------------------------ not a hosted build


def test_not_hosted_when_flag_absent_everywhere(tmp_path: Path) -> None:
    web = _web_dir(tmp_path, {".env": "VITE_IFORGE_API_URL=http://localhost:8000\n"})
    result = _run(web)
    assert result.returncode == 0, _output(result)
    assert "not required" in _output(result)


def test_not_hosted_when_flag_false(tmp_path: Path) -> None:
    web = _web_dir(tmp_path, {".env": "VITE_IFORGE_HOSTED=false\n"})
    result = _run(web)
    assert result.returncode == 0, _output(result)
    assert "not required" in _output(result)


# ------------------------------------------------------------------ hosted, key missing


def test_hosted_without_key_exits_1_and_says_where_it_goes(tmp_path: Path) -> None:
    web = _web_dir(tmp_path, {".env": "VITE_IFORGE_HOSTED=true\n"})
    result = _run(web)
    assert result.returncode == 1, _output(result)
    assert KEY_NAME in result.stderr
    assert "apps/web/.env.local" in result.stderr


def test_empty_value_at_higher_precedence_hides_lower_value(tmp_path: Path) -> None:
    web = _web_dir(
        tmp_path,
        {
            ".env": f"VITE_IFORGE_HOSTED=true\n{KEY_NAME}={FIXTURE_KEY}\n",
            ".env.local": f"{KEY_NAME}=\n",
        },
    )
    result = _run(web)
    assert result.returncode == 1, _output(result)


def test_hosted_flag_in_process_environment_outranks_files(tmp_path: Path) -> None:
    web = _web_dir(tmp_path, {".env": "VITE_IFORGE_HOSTED=false\n"})
    result = _run(web, {"VITE_IFORGE_HOSTED": "true"})
    assert result.returncode == 1, _output(result)


# ------------------------------------------------------------------ hosted, key present


def test_key_in_env_local_passes_and_names_source(tmp_path: Path) -> None:
    web = _web_dir(
        tmp_path,
        {".env": "VITE_IFORGE_HOSTED=true\n", ".env.local": f"{KEY_NAME}={FIXTURE_KEY}\n"},
    )
    result = _run(web)
    assert result.returncode == 0, _output(result)
    assert "source: .env.local" in result.stdout


def test_key_in_env_development_local_passes(tmp_path: Path) -> None:
    web = _web_dir(
        tmp_path,
        {
            ".env": "VITE_IFORGE_HOSTED=true\n",
            ".env.development.local": f"{KEY_NAME}={FIXTURE_KEY}\n",
        },
    )
    result = _run(web)
    assert result.returncode == 0, _output(result)
    assert "source: .env.development.local" in result.stdout


def test_key_only_in_process_environment_passes(tmp_path: Path) -> None:
    web = _web_dir(tmp_path, {".env": "VITE_IFORGE_HOSTED=true\n"})
    result = _run(web, {KEY_NAME: FIXTURE_KEY})
    assert result.returncode == 0, _output(result)
    assert "source: process environment" in result.stdout


def test_empty_process_environment_value_hides_file_value(tmp_path: Path) -> None:
    web = _web_dir(
        tmp_path,
        {".env": "VITE_IFORGE_HOSTED=true\n", ".env.local": f"{KEY_NAME}={FIXTURE_KEY}\n"},
    )
    result = _run(web, {KEY_NAME: ""})
    assert result.returncode == 1, _output(result)


def test_mode_argument_selects_mode_files(tmp_path: Path) -> None:
    web = _web_dir(
        tmp_path,
        {
            ".env": "VITE_IFORGE_HOSTED=true\n",
            ".env.production": f"{KEY_NAME}={FIXTURE_KEY}\n",
        },
    )
    development = _run(web)
    assert development.returncode == 1, _output(development)
    production = _run(web, None, "production")
    assert production.returncode == 0, _output(production)
    assert "source: .env.production" in production.stdout


# ------------------------------------------------------------------ quoting and syntax


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param(f'{KEY_NAME}="{FIXTURE_KEY}"', 0, id="double-quoted value counts"),
        pytest.param(f"{KEY_NAME}='{FIXTURE_KEY}'", 0, id="single-quoted value counts"),
        pytest.param(f'{KEY_NAME}=""', 1, id="empty double-quoted value does not"),
        pytest.param(f"{KEY_NAME}=''", 1, id="empty single-quoted value does not"),
        pytest.param(f"export {KEY_NAME}={FIXTURE_KEY}", 0, id="export prefix accepted"),
        pytest.param(f"# {KEY_NAME}={FIXTURE_KEY}", 1, id="commented line ignored"),
        pytest.param(f"#{KEY_NAME}={FIXTURE_KEY}", 1, id="tight comment ignored"),
        pytest.param(f"{KEY_NAME}=   ", 1, id="whitespace-only value does not"),
        # WR-03: dotenv (as Vite loads it) lets a quoted value carry a trailing comment, and the
        # value is the text inside the quotes. An unquoted value ends at the first #.
        pytest.param(f'{KEY_NAME}="" # fill in', 1, id="empty double-quoted value with comment"),
        pytest.param(f"{KEY_NAME}='' # c", 1, id="empty single-quoted value with comment"),
        pytest.param(f'{KEY_NAME}=""#c', 1, id="empty double-quoted value with tight comment"),
        pytest.param(f'{KEY_NAME}="{FIXTURE_KEY}" # c', 0, id="double-quoted value with comment"),
        pytest.param(f"{KEY_NAME}='{FIXTURE_KEY}' # c", 0, id="single-quoted value with comment"),
        pytest.param(f"{KEY_NAME}={FIXTURE_KEY} # c", 0, id="unquoted value with comment"),
        pytest.param(f"{KEY_NAME}=#c", 1, id="unquoted value is only a comment"),
        pytest.param(f"{KEY_NAME}=`{FIXTURE_KEY}`", 0, id="backtick-quoted value counts"),
        pytest.param(f"{KEY_NAME}=``", 1, id="empty backtick-quoted value does not"),
    ],
)
def test_env_file_syntax(tmp_path: Path, line: str, expected: int) -> None:
    web = _web_dir(tmp_path, {".env": "VITE_IFORGE_HOSTED=true\n", ".env.local": f"{line}\n"})
    result = _run(web)
    assert result.returncode == expected, _output(result)


@pytest.mark.parametrize(
    "line",
    [
        pytest.param('VITE_IFORGE_HOSTED="true" # hosted', id="double-quoted flag with comment"),
        pytest.param("VITE_IFORGE_HOSTED=true # hosted", id="unquoted flag with comment"),
        pytest.param("VITE_IFORGE_HOSTED=true#c", id="unquoted flag with tight comment"),
    ],
)
def test_hosted_flag_with_comment_is_still_hosted(tmp_path: Path, line: str) -> None:
    # WR-03: a hosted flag Vite reads as true must not be skipped as "not required".
    web = _web_dir(tmp_path, {".env": f"{line}\n"})
    result = _run(web)
    assert result.returncode == 1, _output(result)
    assert "not required" not in _output(result)


def test_last_definition_within_a_file_wins(tmp_path: Path) -> None:
    web = _web_dir(
        tmp_path,
        {".env": f"VITE_IFORGE_HOSTED=true\n{KEY_NAME}={FIXTURE_KEY}\n{KEY_NAME}=\n"},
    )
    result = _run(web)
    assert result.returncode == 1, _output(result)


# ------------------------------------------------------------------ usage


def test_missing_web_dir_exits_2(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(HELPER), str(tmp_path / "does-not-exist")],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stderr.strip(), "usage goes to stderr"


def test_helper_is_executable() -> None:
    assert os.access(HELPER, os.X_OK)


# ------------------------------------------------------------------ uat-stack.sh call site


def test_uat_stack_parses() -> None:
    result = subprocess.run(
        ["bash", "-n", str(UAT_STACK)], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_uat_stack_preflight_calls_helper_and_stops_on_failure() -> None:
    lines = UAT_STACK.read_text(encoding="utf-8").splitlines()
    ready = next(i for i, line in enumerate(lines) if "tooling and config present" in line)
    block_open = max(
        i
        for i, line in enumerate(lines[:ready])
        if re.match(r'\s*if \[\[ "\$START_WEB" == true \]\]; then\s*$', line)
    )
    calls = [
        i
        for i, line in enumerate(lines)
        if "check-web-stripe-key.sh" in line and not line.lstrip().startswith("#")
    ]
    assert calls, "uat-stack.sh never calls scripts/check-web-stripe-key.sh"
    assert all(block_open < i < ready for i in calls), (
        "the helper call must sit inside the START_WEB preflight block, before the "
        f"'tooling and config present' line (block {block_open + 1}-{ready + 1}, calls "
        f"{[i + 1 for i in calls]})"
    )
    call = calls[0]
    assert '"$REPO_ROOT/apps/web"' in lines[call]
    window = "\n".join(lines[call : call + 4])
    assert "exit 1" in window, f"a failing helper must stop the run:\n{window}"
