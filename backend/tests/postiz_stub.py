"""The shared ``postiz`` HTTP stub every Postiz façade test reuses (plan 06-07).

The same rule as ``tests/companion_stub.py``: there is ONE double of Postiz's public API. A second
one written later is how two suites end up asserting against two different ideas of what Postiz
does. If a later plan needs a new response shape, it adds a route HERE.

It is a real local HTTP server, not a patched client, so a test sees the bytes on the wire: the
method, the path, the query string, the JSON body and the ``Authorization`` header the executor
actually sent. The stub records every request it receives.

**Authentication is the one part of Postiz this stub copies exactly.** Postiz's public API reads a
static API key from a bare ``Authorization`` header with no ``Bearer`` prefix
(``public.auth.middleware.ts`` at v2.24.0). The stub answers 401 unless the header equals its
configured key byte for byte, so an executor that forgot the header, added a ``Bearer`` prefix or
sent a filebrowser JWT assertion instead fails every case that uses the stub.

What the stub does NOT prove: anything about how a live Postiz validates a post body, or which
social provider a post reaches. It answers whatever a correctly authenticated request asks for.
Every response shape here is trimmed from the v2.24.0 controller
(``apps/backend/src/public-api/routes/v1/public.integrations.controller.ts``) and the posts
repository's ``getPosts`` select.

Bound to ``127.0.0.1`` on an ephemeral port, never ``0.0.0.0``.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

POSTIZ_API_PREFIX = "/api/public/v1"

# What `GET /public/v1/integrations` answers with: the connected channels of one organisation, in
# the controller's own field names.
POSTIZ_STUB_INTEGRATIONS: list[dict[str, object]] = [
    {
        "id": "int-bluesky-1",
        "name": "Brand Bluesky",
        "identifier": "bluesky",
        "picture": "/uploads/bluesky.png",
        "disabled": False,
        "profile": "brand.bsky.social",
    },
    {
        "id": "int-linkedin-1",
        "name": "Brand LinkedIn",
        "identifier": "linkedin",
        "picture": "/uploads/linkedin.png",
        "disabled": False,
        "profile": "brand",
    },
]

POSTIZ_STUB_GROUPS: list[dict[str, object]] = [{"id": "grp-1", "name": "Main customer"}]

POSTIZ_STUB_NOTIFICATIONS: dict[str, object] = {
    "notifications": [
        {"id": "note-1", "content": "Your post was published", "createdAt": "2026-09-30T08:00:00Z"}
    ],
    "total": 1,
}

# The posts already scheduled, in `getPosts`'s select shape. `list_posts` filters these by the
# requested window, so a listing for 2026-10-01 returns the first three and not the last.
POSTIZ_STUB_POSTS: list[dict[str, object]] = [
    {
        "id": "post-existing-1",
        "content": "Launch week starts tomorrow",
        "publishDate": "2026-10-01T09:00:00.000Z",
        "state": "QUEUE",
        "group": "g-existing-1",
        "integration": {
            "id": "int-bluesky-1",
            "providerIdentifier": "bluesky",
            "name": "Brand Bluesky",
            "picture": "/uploads/bluesky.png",
        },
    },
    {
        "id": "post-existing-2",
        "content": "Hiring: two engineers",
        "publishDate": "2026-10-01T15:30:00.000Z",
        "state": "QUEUE",
        "group": "g-existing-2",
        "integration": {
            "id": "int-linkedin-1",
            "providerIdentifier": "linkedin",
            "name": "Brand LinkedIn",
            "picture": "/uploads/linkedin.png",
        },
    },
    {
        # Same day, a channel no test's create_post names: a preview for the two named channels
        # must leave this one out of its before-state.
        "id": "post-existing-other-channel",
        "content": "Only on the other channel",
        "publishDate": "2026-10-01T11:00:00.000Z",
        "state": "QUEUE",
        "group": "g-existing-other",
        "integration": {
            "id": "int-mastodon-1",
            "providerIdentifier": "mastodon",
            "name": "Brand Mastodon",
            "picture": "/uploads/mastodon.png",
        },
    },
    {
        "id": "post-existing-3",
        "content": "Next month",
        "publishDate": "2026-11-05T09:00:00.000Z",
        "state": "QUEUE",
        "group": "g-existing-3",
        "integration": {
            "id": "int-bluesky-1",
            "providerIdentifier": "bluesky",
            "name": "Brand Bluesky",
            "picture": "/uploads/bluesky.png",
        },
    },
]


@dataclass
class RecordedPostizRequest:
    """One request the stub received, as it arrived."""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup, because HTTP header names are case-insensitive."""
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return None

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


@dataclass
class PostizStub:
    """A running stub: where to reach it, the key it accepts, and everything it has been asked.

    The switches below are what a test flips to drive a failure branch:

    - ``create_rate_limited``: ``POST /posts`` answers 429 with ``Retry-After`` set to
      ``retry_after_seconds``. This is the per-hour ``API_LIMIT`` throttle Postiz applies per
      organisation.
    - ``posts_listing_status``: when set, ``GET /posts`` answers with this status instead of the
      listing. The preview's probe-failure branches are driven with it.
    - ``posts_listing_raw``: when set, ``GET /posts`` answers 200 with these bytes verbatim. The
      preview's non-JSON branch is driven with it.
    """

    host: str
    port: int
    api_key: str = field(default_factory=lambda: f"postiz-test-{secrets.token_hex(12)}")
    requests: list[RecordedPostizRequest] = field(default_factory=list)
    posts: list[dict[str, object]] = field(default_factory=lambda: list(POSTIZ_STUB_POSTS))
    create_rate_limited: bool = False
    retry_after_seconds: int = 7
    posts_listing_status: int | None = None
    posts_listing_raw: bytes | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def mutating_requests(self) -> list[RecordedPostizRequest]:
        return [request for request in self.requests if request.method != "GET"]


def _in_window(publish_date: str, start: str, end: str) -> bool:
    """Whether an ISO-8601 UTC instant lies inside ``[start, end]``.

    All three are the same ``YYYY-MM-DDTHH:MM:SS(.fff)Z`` UTC form, so the comparison is on the
    instants, not on the strings.
    """
    from datetime import datetime

    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    moment = parse(publish_date)
    return parse(start) <= moment <= parse(end)


def _handler_for(stub: PostizStub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _record(self) -> RecordedPostizRequest:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            recorded = RecordedPostizRequest(
                method=self.command,
                path=parts.path,
                query=parse_qs(parts.query),
                headers=dict(self.headers.items()),
                body=body,
            )
            stub.requests.append(recorded)
            return recorded

        def _respond(
            self, status: int, payload: object, *, headers: dict[str, str] | None = None
        ) -> None:
            encoded = json.dumps(payload).encode()
            self._respond_raw(status, encoded, headers=headers)

        def _respond_raw(
            self, status: int, encoded: bytes, *, headers: dict[str, str] | None = None
        ) -> None:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(encoded)

        def _authorized(self, recorded: RecordedPostizRequest) -> bool:
            # Byte-for-byte, as Postiz's middleware looks the key up: a `Bearer ` prefix or any
            # other decoration is a different key and is refused.
            if recorded.header("Authorization") == stub.api_key:
                return True
            self._respond(401, {"msg": "Invalid API key"})
            return False

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's dispatch contract
            recorded = self._record()
            if not self._authorized(recorded):
                return
            route = recorded.path.removeprefix(POSTIZ_API_PREFIX)
            if route == "/integrations":
                self._respond(200, POSTIZ_STUB_INTEGRATIONS)
                return
            if route == "/is-connected":
                self._respond(200, {"connected": True})
                return
            if route == "/groups":
                self._respond(200, POSTIZ_STUB_GROUPS)
                return
            if route == "/notifications":
                self._respond(200, POSTIZ_STUB_NOTIFICATIONS)
                return
            if route == "/posts":
                if stub.posts_listing_status is not None:
                    self._respond(stub.posts_listing_status, {"msg": "listing refused"})
                    return
                if stub.posts_listing_raw is not None:
                    self._respond_raw(200, stub.posts_listing_raw)
                    return
                start = (recorded.query.get("startDate") or [""])[0]
                end = (recorded.query.get("endDate") or [""])[0]
                if not start or not end:
                    # GetPostsDto marks both @IsDateString and required.
                    self._respond(400, {"msg": "startDate and endDate are required"})
                    return
                listed = [
                    post for post in stub.posts if _in_window(str(post["publishDate"]), start, end)
                ]
                self._respond(200, {"posts": listed})
                return
            self._respond(404, {"msg": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's dispatch contract
            recorded = self._record()
            if not self._authorized(recorded):
                return
            route = recorded.path.removeprefix(POSTIZ_API_PREFIX)
            if route != "/posts":
                self._respond(404, {"msg": "not found"})
                return
            if stub.create_rate_limited:
                self._respond(
                    429,
                    {"msg": "ThrottlerException: Too Many Requests"},
                    headers={"Retry-After": str(stub.retry_after_seconds)},
                )
                return
            try:
                body = recorded.json()
            except ValueError:
                self._respond(400, {"msg": "body is not JSON"})
                return
            created = [
                {"postId": f"post-new-{index}", "integration": post["integration"]["id"]}
                for index, post in enumerate(body.get("posts") or [], start=1)
            ]
            self._respond(201, created)

        def log_message(self, *args: object) -> None:
            """Silence the default stderr access log; the test reads `stub.requests`."""

    return Handler


@pytest.fixture
def postiz_stub() -> Iterator[PostizStub]:
    """A running ``postiz`` stub on an ephemeral loopback port, torn down with the test."""
    stub = PostizStub(host="127.0.0.1", port=0)
    server = ThreadingHTTPServer((stub.host, 0), _handler_for(stub))
    stub.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, name="postiz-stub", daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
