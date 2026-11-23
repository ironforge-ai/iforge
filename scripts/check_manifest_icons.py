#!/usr/bin/env python3
"""Fail closed when ``site.webmanifest`` disagrees with the shipped asset set.

The web manifest is the one place in this repository that declares brand artwork and cannot
check itself. It carries two duplicated colour literals, because a manifest cannot read a
stylesheet token; it names icon files by path, so a rename anywhere leaves a declaration
pointing at nothing; and it can claim a ``maskable`` purpose over a raster that has no
safe-zone padding, which is a claim no file property contradicts. All three failures are
silent: the manifest stays valid JSON and the app still boots.

``apps/web/src/routes/__root.icons.test.ts`` asserts all of this and more, but it only runs
under the JavaScript test runner. The canonical runner for this phase's guards is shell and
Python host-side, and this file is the manifest's only signal there. It duplicates the
manifest half of that suite deliberately, so a host-side sweep can catch the same regressions.

P33-23 resolved this file's orphan status by WIRING it into ``scripts/test.sh`` rather than
retiring it, and the reason is PROFILE coverage, not property coverage. Property coverage
argued the other way: every one of the eleven properties below is also asserted by the vitest
contract, which is a strict superset. But ``run_on_host`` runs the four host-side guards for
EVERY profile including ``ci``, while the vitest contract only executes under an unfiltered
``web_tests`` -- and ``run_ci`` filters web tests down to ``src/hosted/oss-clean.test.ts``.
``.github/workflows/clean-test-runner-ci.yml`` runs ``scripts/test.sh ci`` on every push and
pull request. Retiring this file would therefore have left the manifest unguarded in CI, which
is the one place it most needs guarding.

Structure copied from the sibling ``check_rebrand_*.py`` guards: no arguments, no argparse,
standard library only (so it runs under bare ``python3``, not only under ``uv``), paths
resolved from a module-level ``ROOT``, a domain error subclass internally, ``SystemExit`` with
a readable message from ``main()``, and exactly one line of stdout on success.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PUBLIC_DIR = ROOT / "apps" / "web" / "public"
MANIFEST_FILE = PUBLIC_DIR / "site.webmanifest"
THEME_FILE = ROOT / "packages" / "shared" / "src" / "style" / "theme.css"

# The manifest's two colours are the sRGB of these light-mode tokens. The manifest cannot read
# them, so this is the only place the duplication can be checked rather than merely written.
THEME_COLOUR_TOKEN = "primary"
BACKGROUND_COLOUR_TOKEN = "background"

# P33-19: the maskable entry is DROPPED, not re-declared over an unpadded raster. Shipping a
# separately padded maskable asset is the sanctioned alternative, but it makes a seventh asset
# path and requires MIGR-04's six-path count re-amended first. It is not a judgement call.
EXPECTED_ICON_COUNT = 2


class ManifestIconsError(RuntimeError):
    """A manifest declaration that does not match what actually ships."""


def _read_manifest() -> dict:
    if not MANIFEST_FILE.is_file():
        raise ManifestIconsError(f"{MANIFEST_FILE.relative_to(ROOT)} is missing")
    try:
        return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ManifestIconsError(
            f"{MANIFEST_FILE.relative_to(ROOT)} is not valid JSON: {error}"
        ) from error


def _light_mode_token(name: str) -> tuple[float, float, float]:
    """Read an ``oklch()`` token from the ``:root`` block only.

    Searching the whole stylesheet would pick up the ``.dark`` override of the same token name,
    which is a different colour and would make this check assert the wrong thing while passing.
    """
    css = THEME_FILE.read_text(encoding="utf-8")
    start = css.find(":root {")
    if start < 0:
        raise ManifestIconsError(f"no :root block in {THEME_FILE.relative_to(ROOT)}")
    end = css.find("\n}", start)
    if end < 0:
        raise ManifestIconsError(f"unterminated :root block in {THEME_FILE.relative_to(ROOT)}")
    match = re.search(
        rf"--{name}:\s*oklch\(([\d.]+)\s+([\d.]+)\s+([\d.]+)\)", css[start:end]
    )
    if match is None:
        raise ManifestIconsError(
            f"--{name} is not an oklch() value in the :root block of "
            f"{THEME_FILE.relative_to(ROOT)}"
        )
    return float(match.group(1)), float(match.group(2)), float(match.group(3))


def _oklch_to_hex(lightness: float, chroma: float, hue: float) -> str:
    radians = math.radians(hue)
    a = chroma * math.cos(radians)
    b = chroma * math.sin(radians)
    long_ = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    medium = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    short = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3
    linear = (
        4.0767416621 * long_ - 3.3077115913 * medium + 0.2309699292 * short,
        -1.2684380046 * long_ + 2.6097574011 * medium - 0.3413193965 * short,
        -0.0041960863 * long_ - 0.7034186147 * medium + 1.7076147010 * short,
    )
    channels = []
    for channel in linear:
        encoded = (
            12.92 * channel
            if channel <= 0.0031308
            else 1.055 * channel ** (1 / 2.4) - 0.055
        )
        channels.append(round(min(1.0, max(0.0, encoded)) * 255))
    return "#{:02x}{:02x}{:02x}".format(*channels)


def validate_icons(manifest: dict) -> int:
    icons = manifest.get("icons")
    if not isinstance(icons, list):
        raise ManifestIconsError("the manifest declares no icons array")
    if len(icons) != EXPECTED_ICON_COUNT:
        raise ManifestIconsError(
            f"the manifest declares {len(icons)} icons, expected {EXPECTED_ICON_COUNT}"
        )

    widths: list[int] = []
    declared_sizes: list[str] = []
    for icon in icons:
        src = icon.get("src", "")
        if not src.startswith("/"):
            raise ManifestIconsError(f"icon src {src!r} is not an absolute public path")
        target = PUBLIC_DIR / src.lstrip("/")
        if not target.is_file():
            raise ManifestIconsError(
                f"icon src {src!r} resolves to nothing under apps/web/public"
            )
        if "purpose" in icon:
            raise ManifestIconsError(
                f"icon src {src!r} declares purpose {icon['purpose']!r}; the maskable entry is "
                "dropped rather than re-declared over an unpadded raster (P33-19)"
            )
        sizes = str(icon.get("sizes", "0x0"))
        declared_sizes.append(sizes)
        widths.append(int(sizes.split("x")[0]))

    # MIGR-04 adjacency edge, added by P33-23. The ordering check below compares the widths
    # against their own sort, and a sorted list is still sorted when two entries are equal --
    # so two icons both declaring "192x192" passed silently, and a launcher picking one of
    # them by size would pick arbitrarily. Adjacency is asserted here rather than left to the
    # ordering check, which cannot see it by construction.
    collisions = sorted({size for size in declared_sizes if declared_sizes.count(size) > 1})
    if collisions:
        detail = ", ".join(
            f"{size!r} declared by {declared_sizes.count(size)} entries" for size in collisions
        )
        raise ManifestIconsError(
            f"two or more icons declare the same sizes value: {detail}; adjacent entries must "
            "be distinct rather than silently deduplicated (MIGR-04 adjacency)"
        )

    if widths != sorted(widths):
        raise ManifestIconsError(f"icons are not listed smallest first: {widths}")

    if "maskable" in MANIFEST_FILE.read_text(encoding="utf-8"):
        raise ManifestIconsError("the manifest still mentions a maskable purpose (P33-19)")

    return len(icons)


def validate_colours(manifest: dict) -> int:
    checks = (
        ("theme_color", THEME_COLOUR_TOKEN),
        ("background_color", BACKGROUND_COLOUR_TOKEN),
    )
    for key, token in checks:
        declared = str(manifest.get(key, "")).lower()
        expected = _oklch_to_hex(*_light_mode_token(token))
        if declared != expected:
            raise ManifestIconsError(
                f"{key} is {declared!r} but light-mode --{token} converts to {expected!r}; "
                "the manifest duplicates the token and the two have drifted"
            )
    return len(checks)


def main() -> None:
    try:
        manifest = _read_manifest()
        icon_count = validate_icons(manifest)
        colour_count = validate_colours(manifest)
    except ManifestIconsError as error:
        raise SystemExit(f"manifest icons failed: {error}") from error
    print(
        f"manifest icons passed: {icon_count} icons resolve, "
        f"{colour_count} colours match their tokens, no maskable purpose"
    )


if __name__ == "__main__":
    main()
