"""The shared ``filebrowser`` HTTP stub every companion integration test in phase 05 reuses.

ONE double, reused. A second one invented in a later plan is how two suites end up asserting
against two different ideas of what the companion does, and the one that matters is always the
one nobody reads. If a later plan needs a new response shape, it adds a route HERE.

A REAL local HTTP server rather than a patched client: the point of an integration double is to
exercise the bytes on the wire -- the method, the path, the query string and the headers the
executor actually sent. A mock of the HTTP client would assert that the executor called the mock
the way the test expected, which is a tautology, and it would not catch a header the executor
forgot to set. This stub RECORDS every request it received and hands the list to the test.

Bound to ``127.0.0.1`` on an ephemeral port. Never ``0.0.0.0``: a test fixture that listens on
every interface is a test fixture that answers a stranger.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

# What `GET /api/resources` answers with. A directory listing shaped like the one
# `05-RESEARCH.md` records for `filebrowser v1.5.0-stable`, trimmed to the fields a listing
# consumer actually reads.
COMPANION_STUB_LISTING: dict[str, object] = {
    "path": "/",
    "name": "",
    "type": "directory",
    "items": [
        {
            "name": "notes.md",
            "size": 128,
            "type": "text",
            "modified": "2026-03-01T10:15:00Z",
        },
        {
            "name": "projects",
            "size": 0,
            "type": "directory",
            "modified": "2026-02-14T08:00:00Z",
        },
    ],
}

# The path this stub reports as ABSENT. Spelled as a constant rather than open-coded in a test,
# because the dry-run suite's load-bearing assertion is the negative one: a preview of an
# impossible mutation must not render as a clean diff. A stub that answered 200 for every path
# could not tell the two apart, so this route is what makes that assertion writable at all.
COMPANION_STUB_MISSING_PATH = "/absent"

# The path this stub refuses with a 403. A probe refused for permission is not the same failure as
# a probe that found nothing, and a preview must not flatten the two into one rendered diff.
COMPANION_STUB_FORBIDDEN_PATH = "/forbidden"


@dataclass
class RecordedRequest:
    """One request the stub received, as it arrived."""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup -- HTTP header names are not case-sensitive."""
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return None


@dataclass
class CompanionStub:
    """A running stub: where to reach it, and everything it has been asked."""

    host: str
    port: int
    requests: list[RecordedRequest] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _handler_for(stub: CompanionStub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _record(self) -> RecordedRequest:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            parts = urlsplit(self.path)
            recorded = RecordedRequest(
                method=self.command,
                path=parts.path,
                query=parse_qs(parts.query),
                headers=dict(self.headers.items()),
                body=body,
            )
            stub.requests.append(recorded)
            return recorded

        def _respond(self, status: int, payload: object) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's dispatch contract
            recorded = self._record()
            if recorded.path == "/health":
                self._respond(200, {"status": "ok"})
                return
            if recorded.path == "/api/resources":
                # An unauthenticated read is a 401, not a listing. The whole claim under test is
                # that the executor MINTS a credential; a stub that answered without one would
                # let a broken executor pass.
                if not recorded.header("X-JWT-Assertion"):
                    self._respond(401, {"error": "missing assertion"})
                    return
                requested = (recorded.query.get("path") or [""])[0]
                if requested == COMPANION_STUB_MISSING_PATH:
                    self._respond(404, {"error": "not found"})
                    return
                if requested == COMPANION_STUB_FORBIDDEN_PATH:
                    self._respond(403, {"error": "forbidden"})
                    return
                self._respond(200, COMPANION_STUB_LISTING)
                return
            self._respond(404, {"error": "not found"})

        def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's dispatch contract
            """``DELETE /api/resources`` -- the one mutating route this stub answers.

            Added by plan 05-06, which has to COUNT executions: "one human decision authorizes one
            action once" is only assertable against requests that actually reached the companion,
            and a handler-less method never reaches ``_record`` at all, so an approval redeemed
            twice would have looked identical to one redeemed once.

            Same 401-without-an-assertion rule as the read route, for the same reason: a stub that
            answered an unauthenticated delete would let a broken executor pass.
            """
            recorded = self._record()
            if recorded.path != "/api/resources":
                self._respond(404, {"error": "not found"})
                return
            if not recorded.header("X-JWT-Assertion"):
                self._respond(401, {"error": "missing assertion"})
                return
            requested = (recorded.query.get("path") or [""])[0]
            if requested == COMPANION_STUB_MISSING_PATH:
                self._respond(404, {"error": "not found"})
                return
            if requested == COMPANION_STUB_FORBIDDEN_PATH:
                self._respond(403, {"error": "forbidden"})
                return
            self._respond(200, {"deleted": requested})

        def _write_route(self, action: str) -> None:
            """The shape ``do_POST`` and ``do_PATCH`` share with ``do_DELETE``.

            **This stub proves the SHAPE the executor sends** -- the method, the path, the query
            string and the minted assertion -- and nothing more. It is NOT a filebrowser and it is
            NOT evidence about what a live filebrowser accepts for a write: research assumptions A2
            and A3 stay open, no provisioned host exists (WINDOWS #133), and
            ``scripts/probe-companion-assertion.ts`` is what settles them. A stub answers whatever
            the executor sends, so it can only show that the executor sends what the registry
            declares.
            """
            recorded = self._record()
            if recorded.path != "/api/resources":
                self._respond(404, {"error": "not found"})
                return
            # Same 401-without-an-assertion rule the read and delete routes carry, for the same
            # reason: a stub that answered an unauthenticated write would let a broken executor --
            # one that forgot to mint -- pass every case that drives this route.
            if not recorded.header("X-JWT-Assertion"):
                self._respond(401, {"error": "missing assertion"})
                return
            requested = (recorded.query.get("path") or [""])[0]
            if requested == COMPANION_STUB_MISSING_PATH:
                self._respond(404, {"error": "not found"})
                return
            if requested == COMPANION_STUB_FORBIDDEN_PATH:
                self._respond(403, {"error": "forbidden"})
                return
            answer: dict[str, object] = {action: requested}
            if action == "moved":
                # Echoed so a test can assert the VALUE arrived, not merely that the request did.
                answer["destination"] = (recorded.query.get("destination") or [""])[0]
            self._respond(200, answer)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's dispatch contract
            """``POST /api/resources`` -- what `create_dir` and `write_file` issue.

            Added by plan 05-13. Until it existed, those two verbs had registry rows since 05-02
            and had never been dispatched by anything in this repository, so the request shape they
            put on the wire was asserted nowhere. See ``_write_route`` for what this does and does
            not prove.
            """
            self._write_route("created")

        def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's dispatch contract
            """``PATCH /api/resources`` -- what `move_path` issues.

            Added by plan 05-13 alongside ``do_POST``, and it additionally reads ``destination``
            from the query string and includes it in its answer so a test can assert the value
            arrived. See ``_write_route`` for what this does and does not prove.
            """
            self._write_route("moved")

        def log_message(self, *args: object) -> None:
            """Silence the default stderr access log; the test reads `stub.requests`."""

    return Handler


@pytest.fixture
def companion_stub() -> Iterator[CompanionStub]:
    """A running ``filebrowser`` stub on an ephemeral loopback port, torn down with the test."""
    stub = CompanionStub(host="127.0.0.1", port=0)
    server = ThreadingHTTPServer((stub.host, 0), _handler_for(stub))
    stub.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, name="companion-stub", daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
