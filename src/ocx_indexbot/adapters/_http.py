"""The two HTTP behaviours both forge adapters must agree on.

`adapters/github_api.py` and `adapters/gitlab_api.py` are otherwise wholly
different clients — different auth headers, different resource names,
different request bodies — and deliberately stay that way. These two pieces
are the exception, because they are not spelling: they are *policy*, and a
policy that drifts between the two forges is a bug nobody would notice.

- **Which responses mean "give up and retry later".** ADR-4 BD-2 maps that to
  exit 75, and `core/backoff.py` never sees a forge call — so this
  classification is the only thing standing between a rate-limited governance
  run and a false verdict. A GitHub adapter that retried on 429 while a
  GitLab adapter raised on it would make the same PR merge on one forge and
  stall on the other.
- **The hard pagination cap.** An unbounded `Link: rel="next"` walk is an
  infinite loop against a misbehaving (or hostile) API. The bound is what
  makes the failure a clear error instead of a hung job. The walk also never
  leaves the host it started on: the client carries a write-scoped token in
  its default headers, so following a `next` link to another host would hand
  that credential to whoever wrote the header.
- **Turning a failed response into an `IndexBotError`.** No `httpx` type may
  escape the adapter layer, because the callers above it catch
  `IndexBotError` — and something that is not one walks through those guards.

Both forges emit RFC 5988 `Link` headers, so `httpx`'s own `response.links`
serves both without a per-forge parser.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import httpx

from ocx_indexbot.errors import ForgeError, TransientError

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_PAGES = 100
"""Hard bound on a `Link: rel="next"` walk. Not `Final` — the test suite
patches it to prove the cap, rather than mocking 100 pages."""


def check_transient(response: httpx.Response, *, forge: str) -> None:
    """Raise `TransientError` for the response classes that mean retry later.

    Four cases, identical on both forges:

    - **401** — a forge adapter's token is fixed for its process lifetime
      (no mid-run refresh, unlike `adapters/registry_v2.py`'s anonymous pull
      token), so a 401 is not retryable *within* this run. It maps straight
      to the exit-75 "retry later" contract (BD-2).
    - **429** — rate limited, always.
    - **403 with `Retry-After`** — GitHub's secondary rate limit answers 403,
      not 429. A *bare* 403 is left alone deliberately: that is
      permission-denied, a config bug, and retrying it for an hour would
      hide it. The header is the whole discriminator.
    - **5xx** — forge weather.

    `forge` is only ever a display label (`"GitHub"`, `"GitLab"`). No message
    built here contains the credential — the caller's token never reaches
    this function.
    """
    status = response.status_code
    if status == 401:
        raise TransientError(f"{forge} API rejected the request: invalid or expired token")
    if status == 429 or (status == 403 and "Retry-After" in response.headers):
        retry_after = response.headers.get("Retry-After")
        suffix = f" (retry after {retry_after}s)" if retry_after else ""
        raise TransientError(f"{forge} API rate limit exceeded{suffix}")
    if status >= 500:
        raise TransientError(f"{forge} API server error: {status}")


def raise_for_status(response: httpx.Response, *, forge: str, action: str = "") -> None:
    """Turn a failed response into a `ForgeError` naming what was attempted.

    Call `check_transient` first — it claims the retryable classes, and what
    reaches here is a permanent refusal. `action` defaults to the request's
    own method and path, which is what every adapter call site wants: naming
    each one by hand would be forty strings that drift from the calls beside
    them.

    The response body is included, truncated: a forge's own explanation
    ("Cannot transition status via :enqueue from :pending") is usually the
    entire diagnosis, and losing it costs an hour. It is not a credential —
    the token travels in a request header and is never echoed back.
    """
    if response.is_success:
        return
    action = action or f"{response.request.method} {response.request.url.path}"
    detail = response.text[:400].strip().replace("\n", " ")
    suffix = f": {detail}" if detail else ""
    raise ForgeError(f"{forge} API refused to {action} ({response.status_code}){suffix}")


def as_object_list(response: httpx.Response, *, forge: str, url: str) -> list[dict[str, Any]]:
    """Decode a list endpoint's body, refusing anything that is not a JSON array
    of objects.

    `list.extend` accepts any iterable: handed a JSON object it appends that
    object's *keys*, and handed a string it appends that string's characters —
    both silently, both producing a `list[dict[str, Any]]` by annotation only.
    Every caller then reads `item["id"]`, so the failure surfaces far from the
    response that caused it, as a `TypeError` about string indices. A forge
    that answers a list endpoint with an error object (some proxies do, at 200)
    would take that path.

    Exported (not `_`-prefixed) because `paginate` below is not this
    function's only caller: `github_api.find_pull_request_by_head_sha`,
    `github_api._find_open_pull_request` and `gitlab_api.find_pull_request_by_head_sha`
    each read a single, unpaginated list response and want the same guard —
    duplicating it per adapter is exactly the drift `adapters/_http.py`'s
    module docstring exists to rule out.
    """
    payload: object = response.json()
    if not isinstance(payload, list):
        raise ForgeError(f"{forge} API returned a non-list body for {url}")
    entries = cast("list[object]", payload)
    if not all(isinstance(entry, dict) for entry in entries):
        raise ForgeError(f"{forge} API returned a non-list body for {url}")
    return cast("list[dict[str, Any]]", entries)


def as_object(response: httpx.Response, *, forge: str, url: str) -> dict[str, Any]:
    """Decode a single-resource endpoint's body, refusing anything that is not
    a JSON object.

    The singular counterpart to `as_object_list`, for the same reason:
    `github_api._pull_request` and `gitlab_api._merge_request` used to
    `cast("dict[str, Any]", response.json())` with nothing proving the shape,
    so a forge answering a single-resource GET with a list or a bare scalar
    (an error body some proxies return unwrapped) would flow silently into
    the first `payload["key"]` far from here.
    """
    payload: object = response.json()
    if not isinstance(payload, dict):
        raise ForgeError(f"{forge} API returned a non-object body for {url}")
    return cast("dict[str, Any]", payload)


def _same_host(url: str, other: str) -> bool:
    left, right = urlsplit(url), urlsplit(other)
    return (left.scheme, left.netloc) == (right.scheme, right.netloc)


def paginate(
    client: httpx.Client, url: str, params: dict[str, str], *, forge: str
) -> list[dict[str, Any]]:
    """Follow `Link: rel="next"` at `per_page=100`, up to `MAX_PAGES`.

    `params` is sent on the first request only — every `next` link already
    carries the full query string, and re-appending `params` to it would
    reset the cursor and loop forever on the second page.

    A `next` link pointing anywhere but the scheme and host the walk started
    on is refused rather than followed. `client` carries the forge token in
    its default headers, so following such a link would send a write-scoped
    credential to a host named by a response header — the same
    SSRF-via-pagination-link that `adapters/registry_v2._parse_next_link`
    already refuses on the registry side.
    """
    items: list[dict[str, Any]] = []
    current_url = url
    current_params: dict[str, str] | None = {**params, "per_page": "100"}
    for _ in range(MAX_PAGES):
        response = client.get(current_url, params=current_params)
        check_transient(response, forge=forge)
        raise_for_status(response, forge=forge, action=f"list {url}")
        items.extend(as_object_list(response, forge=forge, url=url))
        next_link = response.links.get("next", {}).get("url")
        if next_link is None:
            return items
        if not _same_host(url, next_link):
            raise ForgeError(
                f"{forge} API pagination link left the API host and was refused: {url}"
            )
        current_url = next_link
        current_params = None
    raise TransientError(f"{forge} API pagination exceeded {MAX_PAGES} pages: {url}")


class _TransientTransport(httpx.BaseTransport):
    """Turns a transport-level failure into `TransientError`, so it carries an
    exit code instead of a traceback.

    A timeout, connection reset or protocol error is an *exception*, never a
    status, so `check_transient` never sees it. Without this, a read timeout
    talking to the forge escapes every handler in the adapter, escapes
    `cli/main.py`'s `IndexBotError` branch, and ends the run on a bare
    traceback with exit `1` — the code that means "this pull request is
    invalid" — for what is a network blip. `governance-poll` made it worse
    still: its per-MR `except Exception` caught the timeout and scored that
    merge request `VALIDATION_FAILURE`, so a green announce read as a failed
    one and nothing retried it. Measured in production, not inferred:
    `governance-poll: #10: ReadTimeout: The read operation timed out`, exit 1.

    `adapters/registry_v2.py` has caught `httpx.TransportError` for exactly
    this reason since it was written; the two forge adapters never did. It
    lives at transport level rather than around each call because both
    adapters make ~37 of them between them, and a rule that has to be
    remembered at every call site is a rule that will be missed at one.

    No retry here on purpose. `TransientError` is exit `75`, which is the
    contract for "run me again": the poller's next tick and CI's own re-run
    are the retry. `registry_v2` retries in-process because it is spending a
    token whose acquisition it also owns; a forge call has no such budget to
    manage.
    """

    def __init__(self, forge: str, inner: httpx.BaseTransport | None = None) -> None:
        self._forge = forge
        self._inner = httpx.HTTPTransport() if inner is None else inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return self._inner.handle_request(request)
        except httpx.TransportError as exc:
            raise TransientError(
                f"{self._forge} API {type(exc).__name__} for {request.method} "
                f"{request.url.path}: {exc}"
            ) from exc

    def close(self) -> None:
        self._inner.close()


def client(*, headers: Mapping[str, str], timeout: float, forge: str) -> httpx.Client:
    """The one `httpx.Client` both forge adapters build.

    Shared for the transport above, not to save the two lines: the mapping is
    a policy every forge call must obey, which is the same reason
    `check_transient` and `paginate` live here rather than being spelled out
    per adapter.
    """
    return httpx.Client(headers=headers, timeout=timeout, transport=_TransientTransport(forge))
