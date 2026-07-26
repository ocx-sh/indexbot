"""Socket-level fake `ghcr.io` registry driving the REAL registry adapter.

Serves exactly the HTTP surface `indexbot.adapters.ghcr.GhcrRegistry` calls,
so an integration test exercises that adapter's anonymous bearer-token dance,
content-digest recomputation, and media-type handling end to end rather than
re-mocking them:

- `GET /token?service=ghcr.io&scope=repository:<repo>:pull` — the anonymous
  pull-token endpoint. By default every `/v2/*` request without an
  `Authorization: Bearer` header is answered `401`, so the adapter's
  fetch-token-and-retry-once dance runs for real (`set_require_bearer(False)`
  disables the gate when a test wants to script a `401`/`200` sequence on a
  specific ref instead).
- `GET /v2/<repo>/tags/list` — RFC 8288 `Link`-header pagination lives in the
  adapter; this fake serves one page by default.
- `GET|HEAD /v2/<repo>/manifests/<ref>` — `<ref>` is a tag or a
  `sha256:<hex>` digest. `add_manifest` registers both a tag ref and the
  content-addressed digest ref for one manifest, since the adapter fetches a
  manifest by tag (`observe`) and re-fetches each platform digest by digest
  (`check_digest_in_scope`).
- `GET /v2/<repo>/blobs/<digest>` — desc/readme/logo blob reads.

Every route is scriptable with arbitrary `ScriptedResponse`s (200 / 404 /
429+`Retry-After` / 5xx / malformed-JSON) so callers can synthesize the edges
the adapter's retry and error paths depend on.

`<repo>` throughout is the registry **path** (`ocx-contrib/cmake`), i.e. the
`<path>` portion of the `oci://ghcr.io/<path>` URI the adapter parses out —
never the full URI.

Stdlib only (`http.server.ThreadingHTTPServer`). This is the socket tier,
distinct from `tests/fakes`' in-memory `FakeRegistry`.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Self, cast
from urllib.parse import unquote, urlsplit

from tests.integration.harness._http import ScriptedResponse, json_response, write_response

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

_UNAUTHORIZED = ScriptedResponse(
    status=401,
    headers={"WWW-Authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io"'},
)


def manifest_wire_bytes(manifest: Mapping[str, object]) -> bytes:
    """The exact wire bytes this fake serves for `manifest`.

    **Deliberately not any encoder's canonical output.** A registry serves the
    bytes its publisher pushed, and the index stores those bytes verbatim
    (`core/observe.py` — `Observation.raw`), so this fake serves a body that no
    re-serialization could reproduce: 2-space indent, the mapping's own
    insertion key order, no trailing newline.

    That is the whole load-bearing property of this function. Were it to emit
    `json.dumps(..., sort_keys=True, separators=(",", ":"))`, a pipeline that
    re-encoded a parsed manifest would be byte-indistinguishable from one that
    copied the registry's bytes, and every "stored verbatim" assertion in
    `tests/integration/` would pass against both. Encoded this way, a
    re-encoding pipeline produces a different digest and the flows fail loudly.

    `git_tree` seeds through this same function, so a CAS content digest
    written into a seeded root equals the one `core/observe.py` computes when
    the real adapter serves these bytes back at validate time.
    """
    return json.dumps(dict(manifest), indent=2, ensure_ascii=True).encode("utf-8")


def manifest_digest(body: bytes) -> str:
    """`sha256:<hex>` over `body` — the adapter's own digest-over-wire-bytes rule."""
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def _take(queue: list[ScriptedResponse]) -> ScriptedResponse:
    """Next scripted response: consume the head while more remain, then repeat
    the last (so one response serves forever, `[429, 200]` serves `429` once
    then `200` forever — the shape a retry-then-succeed test needs)."""
    return queue.pop(0) if len(queue) > 1 else queue[0]


def _split_v2(path: str) -> tuple[str, str, str] | None:
    """`/v2/<repo>/{tags/list|manifests/<ref>|blobs/<digest>}` -> `(kind, repo,
    ref)`, or `None` if `path` is not a `/v2/` API path. `repo` may contain
    slashes; `rfind` on the suffix separator keeps a multi-segment repo intact.
    """
    if not path.startswith("/v2/"):
        return None
    rest = path[len("/v2/") :]
    if rest.endswith("/tags/list"):
        return ("tags", rest[: -len("/tags/list")], "")
    for kind, separator in (("manifests", "/manifests/"), ("blobs", "/blobs/")):
        index = rest.rfind(separator)
        if index != -1:
            return (kind, rest[:index], rest[index + len(separator) :])
    return None


@dataclass(slots=True)
class _GhcrState:
    """Mutable server state, guarded by `lock` (ThreadingHTTPServer serves each
    connection on its own thread)."""

    token: list[ScriptedResponse]
    tags: dict[str, list[ScriptedResponse]] = field(
        default_factory=dict[str, list[ScriptedResponse]]
    )
    manifests: dict[tuple[str, str], list[ScriptedResponse]] = field(
        default_factory=dict[tuple[str, str], list[ScriptedResponse]]
    )
    blobs: dict[tuple[str, str], list[ScriptedResponse]] = field(
        default_factory=dict[tuple[str, str], list[ScriptedResponse]]
    )
    require_bearer: bool = True
    lock: threading.Lock = field(default_factory=threading.Lock)
    requests: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])


def _resolve_route(state: _GhcrState, kind: str, repo: str, ref: str) -> ScriptedResponse:
    if kind == "tags":
        queue = state.tags.get(repo)
    elif kind == "manifests":
        queue = state.manifests.get((repo, ref))
    else:
        queue = state.blobs.get((repo, ref))
    if not queue:
        return ScriptedResponse(status=404)
    return _take(queue)


class _GhcrHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def log_message(self, format: str, *args: object) -> None:  # stdlib override signature
        """Silence the default stderr access log under test — `format`/`args`
        deliberately unused; the name `format` matches the base signature so
        the override stays type-compatible."""

    def _handle(self, method: str) -> None:
        server = cast("_GhcrHTTPServer", self.server)
        state = server.state
        path = unquote(urlsplit(self.path).path)
        include_body = method != "HEAD"
        with state.lock:
            state.requests.append((method, path))
            if path == "/token":
                write_response(self, _take(state.token), include_body=include_body)
                return
            route = _split_v2(path)
            if route is None:
                write_response(self, ScriptedResponse(status=404), include_body=include_body)
                return
            if state.require_bearer and not _has_bearer(self):
                write_response(self, _UNAUTHORIZED, include_body=include_body)
                return
            kind, repo, ref = route
            response = _resolve_route(state, kind, repo, ref)
        write_response(self, response, include_body=include_body)


def _has_bearer(handler: BaseHTTPRequestHandler) -> bool:
    authorization = handler.headers.get("Authorization")
    return authorization is not None and authorization.startswith("Bearer ")


class _GhcrHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], state: _GhcrState) -> None:
        super().__init__(server_address, _GhcrHandler)
        self.state = state


class FakeGhcrServer:
    """Context-managed fake `ghcr.io`. Point `GhcrRegistry(base_url=...)` at
    `base_url` to drive the real adapter against it."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._state = _GhcrState(token=[json_response(200, {"token": "fake-pull-token"})])
        self._server = _GhcrHTTPServer((host, 0), self._state)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-ghcr", daemon=True
        )

    @property
    def base_url(self) -> str:
        address = cast("tuple[str, int]", self._server.server_address)
        return f"http://{address[0]}:{address[1]}"

    @property
    def requests(self) -> list[tuple[str, str]]:
        """Every `(method, path)` served so far — lets a test assert the token
        dance ran (`("GET", "/token")` present)."""
        with self._state.lock:
            return list(self._state.requests)

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

    # --- scripting -----------------------------------------------------------

    def add_manifest(self, repository: str, reference: str, manifest: Mapping[str, object]) -> str:
        """Serve `manifest` (as `manifest_wire_bytes`) at both its tag
        `reference` and its content-addressed `sha256:<hex>` digest ref, with a
        matching `Docker-Content-Digest` header and the document's own
        `mediaType` as `Content-Type`. Returns that digest."""
        body = manifest_wire_bytes(manifest)
        digest = manifest_digest(body)
        media_type = str(manifest.get("mediaType", "application/vnd.oci.image.manifest.v1+json"))
        response = ScriptedResponse(
            status=200,
            body=body,
            headers={"Content-Type": media_type, "Docker-Content-Digest": digest},
        )
        with self._state.lock:
            self._state.manifests[(repository, reference)] = [response]
            self._state.manifests[(repository, digest)] = [response]
        return digest

    def set_manifest(
        self, repository: str, reference: str, first: ScriptedResponse, *rest: ScriptedResponse
    ) -> None:
        """Script the `manifests/<reference>` route (the retry/error/malformed
        edge hook — pass a sequence like `429`, then `200`)."""
        with self._state.lock:
            self._state.manifests[(repository, reference)] = [first, *rest]

    def set_tags(self, repository: str, tags: list[str]) -> None:
        """Serve `tags` at `tags/list` as `{"name": repo, "tags": [...]}`."""
        with self._state.lock:
            self._state.tags[repository] = [json_response(200, {"name": repository, "tags": tags})]

    def set_blob(self, repository: str, digest: str, data: bytes) -> None:
        """Serve raw `data` at `blobs/<digest>`."""
        with self._state.lock:
            self._state.blobs[(repository, digest)] = [ScriptedResponse(status=200, body=data)]

    def set_token(self, first: ScriptedResponse, *rest: ScriptedResponse) -> None:
        """Script the `/token` endpoint (e.g. a `403` DENIED for a missing/
        private repository)."""
        with self._state.lock:
            self._state.token = [first, *rest]

    def set_require_bearer(self, *, required: bool) -> None:
        """Toggle the anonymous-`401`-then-token gate on `/v2/*` requests."""
        with self._state.lock:
            self._state.require_bearer = required
