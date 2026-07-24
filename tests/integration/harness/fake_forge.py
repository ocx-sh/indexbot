"""Socket-level fake GitHub forge driving the REAL `github_api.py` adapter.

A scriptable request/response server (respx-shaped, but over a real TCP socket)
covering the exact GitHub REST + GraphQL surface
`indexbot.adapters.github_api.GitHubApi` calls. Point
`GitHubApi(base_url=fake.base_url, graphql_url=fake.base_url + "/graphql")` at
it to drive that adapter's URL construction, `Authorization`/`Accept` headers,
`Link`-header pagination, base64 blob encode/decode, and GraphQL auto-merge
mutation end to end.

Routes are matched first-registered-wins on `(method, path)` plus an optional
query-parameter subset — so, exactly as the existing respx suite does, a more
specific route (with `params=`) must be stubbed *before* a broader one.
Unstubbed routes answer `404 {"message": "Not Found"}`.

The exact payload shapes each `GitHubApi` method expects are the ones its
respx suite (`tests/test_github_api.py`) documents; script them with `stub_json`
/ `stub`. This scriptability is what lets a fork-flow WP synthesize the fork/PR
state branches (a `201`/`202`/`409` on a fork-create route, a `422` PR-reuse, a
renamed-fork or parent-mismatch body) without this base harness hard-coding any
fork state machine it cannot yet verify against a caller.

Stdlib only (`http.server.ThreadingHTTPServer`). Socket tier, distinct from
`tests/fakes`' in-memory `FakeGitHub`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Self, cast
from urllib.parse import parse_qs, unquote, urlsplit

from tests.integration.harness._http import ScriptedResponse, json_response, write_response

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

_NOT_FOUND = json_response(404, {"message": "Not Found"})


@dataclass(slots=True)
class _StubbedRoute:
    method: str
    path: str
    params: Mapping[str, str] | None
    responses: list[ScriptedResponse]

    def matches(self, method: str, path: str, query: Mapping[str, str]) -> bool:
        if method != self.method or path != self.path:
            return False
        if self.params is None:
            return True
        return all(query.get(key) == value for key, value in self.params.items())

    def take(self) -> ScriptedResponse:
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


@dataclass(slots=True)
class _ForgeState:
    routes: list[_StubbedRoute] = field(default_factory=list[_StubbedRoute])
    lock: threading.Lock = field(default_factory=threading.Lock)
    requests: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    received_headers: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    """Headers of every request the forge received — the X6 leak seam that lets
    a flow prove no registry credential ever reaches a forge request."""

    def resolve(self, method: str, path: str, query: Mapping[str, str]) -> ScriptedResponse:
        for route in self.routes:
            if route.matches(method, path, query):
                return route.take()
        return _NOT_FOUND


class _ForgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def log_message(self, format: str, *args: object) -> None:  # stdlib override signature
        """Silence the default stderr access log under test — `format`/`args`
        deliberately unused; the name `format` matches the base signature so
        the override stays type-compatible."""

    def _handle(self, method: str) -> None:
        self._drain_body()
        server = cast("_ForgeHTTPServer", self.server)
        state = server.state
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        with state.lock:
            state.requests.append((method, path))
            state.received_headers.append(dict(self.headers.items()))
            response = state.resolve(method, path, query)
        write_response(self, response, include_body=(method != "HEAD"))

    def _drain_body(self) -> None:
        """Read and discard any request body so a keep-alive connection stays
        framed for the next request (canned responses ignore request content)."""
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 0:
            self.rfile.read(length)


class _ForgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], state: _ForgeState) -> None:
        super().__init__(server_address, _ForgeHandler)
        self.state = state


class FakeForgeServer:
    """Context-managed fake GitHub forge scoped to one `owner/repo`."""

    def __init__(self, owner: str = "ocx-sh", repo: str = "index", host: str = "127.0.0.1") -> None:
        self._owner = owner
        self._repo = repo
        self._state = _ForgeState()
        self._server = _ForgeHTTPServer((host, 0), self._state)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-forge", daemon=True
        )

    @property
    def base_url(self) -> str:
        address = cast("tuple[str, int]", self._server.server_address)
        return f"http://{address[0]}:{address[1]}"

    @property
    def requests(self) -> list[tuple[str, str]]:
        with self._state.lock:
            return list(self._state.requests)

    @property
    def received_headers(self) -> list[dict[str, str]]:
        with self._state.lock:
            return [dict(headers) for headers in self._state.received_headers]

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)

    # --- path builders -------------------------------------------------------

    def repo_path(self, *segments: str) -> str:
        """`/repos/<owner>/<repo>/<segments...>` — the path shape
        `GitHubApi._repo_url` builds for this server's owner/repo."""
        return "/".join(("/repos", self._owner, self._repo, *segments))

    def graphql_path(self) -> str:
        """The `/graphql` path (`GitHubApi` posts the auto-merge mutation here)."""
        return "/graphql"

    # --- scripting -----------------------------------------------------------

    def stub(
        self,
        method: str,
        path: str,
        first: ScriptedResponse,
        *rest: ScriptedResponse,
        params: Mapping[str, str] | None = None,
    ) -> None:
        """Register a route. Multiple responses are consumed in order then the
        last repeats (retry-then-succeed). Register `params`-qualified routes
        before broader ones — first match wins."""
        with self._state.lock:
            self._state.routes.append(
                _StubbedRoute(method=method, path=path, params=params, responses=[first, *rest])
            )

    def stub_json(
        self,
        method: str,
        path: str,
        payload: object,
        *,
        status: int = 200,
        params: Mapping[str, str] | None = None,
    ) -> None:
        """Register a single JSON response — the common case for the GitHub REST
        payloads `github_api.py` decodes."""
        self.stub(method, path, json_response(status, payload), params=params)
