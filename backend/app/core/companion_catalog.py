"""The companions a tenant can install, and exactly which images each one runs (APP-01, D-28).

One entry per installable companion kind. Every image is a repository plus a sha256 **index**
digest, resolved from its registry when it was pinned (the tag rides along for humans; nothing
pulls by tag). An installation copies the app digest at request time (``pinned_digest``), so what
it runs cannot drift from what the tenant accepted; a newer digest here becomes an OFFER to
existing installations, never a silent upgrade (D-28).

**Where each pin came from.** Recorded with the command that resolved it in plan 06-09's
SUMMARY. The Postgres pin is ``config/postgres-image.txt``'s value, EMBEDDED rather than read at
import because the backend image ships without ``config/``; a test asserts the two stay equal.

**Tiers (D-06).** The contract admits standard, dedicated and shared; this build offers standard
only. Under CK-A (plan 06-05) standard is a per-installation PostgreSQL AND Redis, with Temporal
shared through a per-installation namespace -- so Redis is pinned here and Temporal is not.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.companion_installation import COMPANION_ISOLATION_TIERS


@dataclass(frozen=True)
class CompanionImagePin:
    image: str
    tag: str
    sha256: str


@dataclass(frozen=True)
class CompanionCatalogEntry:
    key: str
    display_name: str
    version: str
    app: CompanionImagePin
    postgres: CompanionImagePin
    redis: CompanionImagePin
    cloudflared: CompanionImagePin
    offered_tiers: tuple[str, ...]

    def images(self) -> dict[str, CompanionImagePin]:
        return {
            "app": self.app,
            "postgres": self.postgres,
            "redis": self.redis,
            "cloudflared": self.cloudflared,
        }

    def tier_offered(self, tier: str) -> bool:
        return tier in self.offered_tiers


COMPANION_CATALOG: dict[str, CompanionCatalogEntry] = {
    "postiz": CompanionCatalogEntry(
        key="postiz",
        display_name="Postiz",
        version="v2.24.0",
        app=CompanionImagePin(
            image="ghcr.io/gitroomhq/postiz-app",
            tag="v2.24.0",
            sha256="01b24a4fc1055f6833122c99614805bea702fdc0ee30c9b5d09330b521fa1efa",
        ),
        # ``config/postgres-image.txt`` (the house pgBackRest image), embedded; parity-tested.
        postgres=CompanionImagePin(
            image="ghcr.io/clawdi-ai/postgres-pgbackrest",
            tag="pg18",
            sha256="db8d1af12ebac7fe10c57730502a31e9b34586ac1ed32852639007dd77eda449",
        ),
        # Per installation under CK-A: Postiz keeps OAuth state under bare Redis keys (F3).
        redis=CompanionImagePin(
            image="docker.io/library/redis",
            tag="7.2",
            sha256="0637954999d01b7c9ce9167db2da50656e2590d3b884f1c600c5f63bb6e6773c",
        ),
        # The tunnel connector plan 06-14 runs; the latest cloudflared release when pinned.
        cloudflared=CompanionImagePin(
            image="docker.io/cloudflare/cloudflared",
            tag="2026.9.1",
            sha256="b269e8abd07a5bf6f3f4be65d5050b2174eca89c56a0241a8ff32a16aec454e4",
        ),
        offered_tiers=("standard",),
    ),
}

# Every tier the contract admits, in display order; ``offered_tiers`` is always a subset.
CATALOG_TIERS: tuple[str, ...] = COMPANION_ISOLATION_TIERS


def catalog_entry(key: str) -> CompanionCatalogEntry:
    """The catalog entry for ``key``; ``KeyError`` for a companion this build cannot install."""
    return COMPANION_CATALOG[key]
