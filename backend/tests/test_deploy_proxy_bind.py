"""Text pins of kamal-proxy's `run` block in config/deploy.yml.

Docker's published ports bypass the host firewall, so the addresses kamal-proxy publishes 80 and
443 on decide what is reachable. With a `proxy.run` block Kamal ignores `kamal proxy boot_config`,
so the bind must live in that block. These cases read the config as text (it is ERB, not plain
YAML until Kamal renders it), so an edit that drops or widens the bind fails without Docker.
scripts/test-kamal-proxy-bind-render.sh proves the same contract with Kamal's own render.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_CONFIG = REPO_ROOT / "config" / "deploy.yml"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_comment(line: str) -> str:
    stripped = line.lstrip(" ")
    if stripped.startswith("#"):
        return ""
    if " #" in line:
        line = line[: line.index(" #")]
    return line.rstrip()


def proxy_run_block(text: str | None = None) -> list[str]:
    """The lines under the `run:` key of the top-level `proxy:` key, comments dropped.

    Lines keep their indentation so the caller can read nesting. Blank and comment lines are
    removed; a trailing ` # ...` comment is cut off.
    """
    if text is None:
        text = DEPLOY_CONFIG.read_text(encoding="utf-8")
    lines = [_strip_comment(line) for line in text.splitlines()]
    lines = [line for line in lines if line.strip()]

    start = lines.index("proxy:")
    proxy: list[str] = []
    for line in lines[start + 1 :]:
        if _indent(line) == 0:
            break
        proxy.append(line)

    child_indent = min(_indent(line) for line in proxy)
    runs = [i for i, line in enumerate(proxy) if line == " " * child_indent + "run:"]
    assert len(runs) == 1, f"proxy: holds {len(runs)} `run:` keys"

    block: list[str] = []
    for line in proxy[runs[0] + 1 :]:
        if _indent(line) <= child_indent:
            break
        block.append(line)
    assert block, "proxy.run is empty"
    return block


def _key_children(block: list[str], key: str) -> list[list[str]]:
    """For every direct `key:` in the block, the lines nested under it."""
    key_indent = min(_indent(line) for line in block)
    found: list[list[str]] = []
    for i, line in enumerate(block):
        if line == " " * key_indent + f"{key}:":
            children: list[str] = []
            for child in block[i + 1 :]:
                nested = _indent(child) > key_indent
                same_level_item = _indent(child) == key_indent and child.lstrip().startswith("- ")
                if not (nested or same_level_item):
                    break
                children.append(child.strip())
            found.append(children)
    return found


def test_kamal_proxy_is_pinned_to_loopback() -> None:
    block = proxy_run_block()

    binds = _key_children(block, "bind_ips")
    assert len(binds) == 1, f"proxy.run holds {len(binds)} `bind_ips:` keys, expected 1"
    assert all(item.startswith("- ") for item in binds[0]), f"bind_ips is not a list: {binds[0]}"
    items = [item.removeprefix("- ").strip().strip("\"'") for item in binds[0]]
    assert items == ["127.0.0.1"], f"proxy.run.bind_ips is {items}"


def test_the_proxy_run_block_keeps_its_log_hardening() -> None:
    block = proxy_run_block()
    key_indent = min(_indent(line) for line in block)
    direct = [line.strip() for line in block if _indent(line) == key_indent]

    assert 'log_max_size: ""' in direct, direct
    options = _key_children(block, "options")
    assert len(options) == 1, f"proxy.run holds {len(options)} `options:` keys"
    assert "log-driver: none" in options[0], options[0]
