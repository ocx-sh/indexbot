"""GHCR (`ghcr.io`) `RegistryPort` implementation (CONTRACTS.md §9).

The only place `httpx` is imported for registry reads (ADR-4 BD-1, functional
core / imperative shell). Owns three things `core/` never sees the mechanics
of:

- The anonymous bearer-token dance: an unauthenticated request gets a `401`,
  a pull token is fetched from `GET /token?service=ghcr.io&scope=repository:
  <path>:pull` (no credentials required for a public repository), and the
  original request is retried once with `Authorization: Bearer <token>`. The
  token is cached per repository path for this instance's lifetime and
  refreshed (not counted against `BackoffPolicy.max_attempts`) on exactly one
  fresh `401` — a second consecutive `401` for the same logical request is a
  persistent auth failure, raised as `TransientError`.
- A `403` from either the token endpoint or a `/v2/` API call — GHCR's
  `DENIED` response for a repository that is missing or private, body
  present or not — is a permanent condition, never a bug and never worth
  retrying: raised as `ValidationError`, distinct from the `401` dance above
  (which *can* succeed once a token is attached) and from `TransientError`
  (which implies retrying later might help).
- `tags/list` pagination via the RFC 8288 `Link` response header, bounded by
  `max_pages` so a misbehaving/malicious next-link chain can't loop forever.
- The 429/5xx retry loop — the imperative-shell half of `core/backoff.py`'s
  pure timing decisions (CONTRACTS.md §7): this module calls `time.sleep`
  directly, `core/backoff.py` only computes *how long*.

`repository` arguments are always the full `oci://<host>/<path>` URI stored
in `PackageRoot.repository` (see `core/validate_entry.py`'s
`check_repository_allowlisted`/`check_repository_shape`, which already ran
against this same string before any `RegistryPort` call reaches here per BD-1's
SSRF ordering) — this adapter only ever parses out the `<path>` portion, it
never re-validates the host.
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

import httpx

from indexbot.core.backoff import BackoffPolicy, delay_seconds, is_retryable_status
from indexbot.errors import AnomalyError, TransientError, ValidationError
from indexbot.model import ManifestFetch

if TYPE_CHECKING:
    from collections.abc import Mapping

    from indexbot.model import OwnershipProbeResult

_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_MAX_PAGES = 10_000
_TAGS_PAGE_SIZE = 100

_DESC_TAG = "__ocx.desc"

_OWNERSHIP_ANNOTATION_KEY = "sh.ocx.name"
"""Manifest-level annotation `probe_ownership` reads for the embedded
canonical identifier (e.g. `ocx.sh/kitware/cmake`).

The identifier-embedding convention itself is **unconfirmed** against
`ocx-mirror`'s actual publish behavior (ADR-4 Risk 2; `ports.py`'s
`probe_ownership` docstring calls this "a pluggable seam, not a fixed
annotation-key lookup"). Reusing the `__ocx.desc` manifest (already fetched
by `core/desc.py` for title/description/keywords) as the identifier's home is
this stage's best-effort default, not a locked contract — confirm against
real `ocx-mirror` output before Phase 3.
"""

_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)
"""Accept both OCI and legacy Docker media types for a manifest or an image
index — GHCR has historically served the "wrong" one of the pair for a given
request (`research_ghcr_constraints.md` §4), so a strict single-type Accept
header is a known footgun here, not defensive over-engineering."""


def _repo_path(repository: str) -> str:
    """`oci://ghcr.io/<path>` -> `<path>`, mirroring
    `core/validate_entry.py`'s `check_repository_shape` parse exactly."""
    return urlsplit(repository).path.lstrip("/")


def _parse_retry_after(value: str | None) -> float | None:
    """Integer-seconds `Retry-After` only.

    # ponytail: HTTP-date form (RFC 9110 §10.2.3) unsupported — GHCR's 429s
    observed in practice send seconds; add date parsing if that changes.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _parse_next_link(link_header: str | None, *, base_url: str) -> str | None:
    """`rel="next"` target URL from an RFC 8288 `Link` header, or `None`.

    An absolute target's host must equal `base_url`'s own host. `list_tags`
    reattaches this instance's cached bearer pull-token to whatever URL this
    function returns (`_send`, keyed only by `repo_path`) — a server-supplied
    `Link` header pointing at a different host is not a condition this
    adapter has a defined recovery for (SSRF-via-pagination-link), so it is
    rejected the same way a malformed-JSON body is (CONTRACTS.md §9): a
    plain `ValueError`, propagating as an unhandled bug, never silently
    followed or silently truncated.
    """
    if not link_header:
        return None
    allowed_host = urlsplit(base_url).netloc
    for part in link_header.split(","):
        segments = part.split(";")
        target = segments[0].strip()
        if not (target.startswith("<") and target.endswith(">")):
            continue
        if any(segment.strip() == 'rel="next"' for segment in segments[1:]):
            raw = target[1:-1]
            if not raw.startswith(("http://", "https://")):
                return f"{base_url}{raw}"
            next_host = urlsplit(raw).netloc
            if next_host != allowed_host:
                raise ValueError(
                    f"next-link host {next_host!r} does not match {allowed_host!r} "
                    "(rejecting cross-host pagination redirect)"
                )
            return raw
    return None


def _denied_message(repo_path: str) -> str:
    """`403`/`DENIED` from GHCR (token endpoint or a `/v2/` call), body
    present or empty — the repository is missing or private and does not
    grant anonymous `:pull`. Permanent, not retryable: distinct from a
    `401`, which the token dance above can still recover from.
    """
    return (
        f"ghcr.io/{repo_path} is missing or does not allow anonymous pull "
        "(GHCR denied the request with 403); the repository must exist and grant "
        "anonymous :pull access before this bot can observe it"
    )


def _embedded_identifier(manifest: dict[str, object]) -> str | None:
    """`_OWNERSHIP_ANNOTATION_KEY`'s value from `manifest["annotations"]`, if
    present and string-shaped — annotation values are always strings per the
    OCI image-spec, so a non-string value (malformed manifest) is treated the
    same as "absent"."""
    annotations = manifest.get("annotations")
    if not isinstance(annotations, dict):
        return None
    typed_annotations = cast("dict[str, object]", annotations)
    value = typed_annotations.get(_OWNERSHIP_ANNOTATION_KEY)
    return value if isinstance(value, str) else None


@dataclass(slots=True)
class GhcrRegistry:
    """`RegistryPort` over `ghcr.io`. One instance per process run — `_tokens`
    caches one anonymous pull token per repository path for this instance's
    lifetime (CONTRACTS.md §9)."""

    base_url: str = "https://ghcr.io"
    timeout: float = _DEFAULT_TIMEOUT_SECONDS
    policy: BackoffPolicy = field(default_factory=BackoffPolicy)
    max_pages: int = _DEFAULT_MAX_PAGES
    client: httpx.Client = field(default_factory=httpx.Client)
    _tokens: dict[str, str] = field(default_factory=dict[str, str], init=False, repr=False)

    def list_tags(self, repository: str) -> list[str]:
        repo_path = _repo_path(repository)
        url: str | None = f"{self.base_url}/v2/{repo_path}/tags/list"
        params: Mapping[str, str] | None = {"n": str(_TAGS_PAGE_SIZE)}
        tags: list[str] = []
        for _page in range(self.max_pages):
            response = self._send("GET", url, repo_path=repo_path, params=params)
            params = None
            if response.status_code == 404:
                return []
            response.raise_for_status()
            tags.extend(response.json().get("tags") or [])
            url = _parse_next_link(response.headers.get("Link"), base_url=self.base_url)
            if url is None:
                return tags
        raise TransientError(
            f"tags/list pagination exceeded {self.max_pages} pages for {repository!r}"
        )

    def get_manifest(self, repository: str, reference: str) -> ManifestFetch:
        repo_path = _repo_path(repository)
        url = f"{self.base_url}/v2/{repo_path}/manifests/{reference}"
        response = self._send("GET", url, repo_path=repo_path, headers={"Accept": _MANIFEST_ACCEPT})
        if response.status_code == 404:
            raise KeyError(f"no manifest for {repository}@{reference}")
        response.raise_for_status()
        raw = response.content
        computed_digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        # Verify-if-present (ports.py's digest doctrine): the header is never
        # trusted in place of the computed digest, only cross-checked against
        # it when the registry happens to send one.
        header_digest = response.headers.get("Docker-Content-Digest")
        if header_digest is not None and header_digest != computed_digest:
            raise AnomalyError(
                f"manifest digest mismatch for {repository}@{reference}: "
                f"Docker-Content-Digest header {header_digest!r} != computed {computed_digest!r}"
            )
        return ManifestFetch(raw=raw, digest=computed_digest, parsed=response.json())

    def get_desc_tag_digest(self, repository: str) -> str | None:
        repo_path = _repo_path(repository)
        url = f"{self.base_url}/v2/{repo_path}/manifests/{_DESC_TAG}"
        response = self._send(
            "HEAD", url, repo_path=repo_path, headers={"Accept": _MANIFEST_ACCEPT}
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.headers["Docker-Content-Digest"]

    def get_blob(self, repository: str, digest: str) -> bytes:
        repo_path = _repo_path(repository)
        url = f"{self.base_url}/v2/{repo_path}/blobs/{digest}"
        response = self._send("GET", url, repo_path=repo_path)
        if response.status_code == 404:
            raise KeyError(f"no blob {digest} for {repository}")
        response.raise_for_status()
        return response.content

    def probe_ownership(self, repository: str, expected_name: str) -> OwnershipProbeResult:
        digest = self.get_desc_tag_digest(repository)
        if digest is None:
            return "unconfirmed"
        fetch = self.get_manifest(repository, digest)
        embedded = _embedded_identifier(fetch.parsed)
        if embedded is None:
            return "unconfirmed"
        return "confirmed" if embedded == expected_name else "mismatch"

    def _send(
        self,
        method: str,
        url: str,
        *,
        repo_path: str,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """One logical request: token dance (401 -> refresh -> retry once)
        wrapped in the 429/5xx backoff loop."""
        auth_retried = False
        attempt = 1
        while True:
            request_headers = dict(headers or {})
            token = self._tokens.get(repo_path)
            if token is not None:
                request_headers["Authorization"] = f"Bearer {token}"
            response = self.client.request(
                method, url, headers=request_headers, params=params, timeout=self.timeout
            )

            if response.status_code == 401:
                if auth_retried:
                    raise TransientError(f"persistent 401 for {method} {url}")
                auth_retried = True
                self._tokens[repo_path] = self._fetch_token(repo_path)
                continue

            if response.status_code == 403:
                raise ValidationError(_denied_message(repo_path))

            if is_retryable_status(response.status_code):
                if attempt >= self.policy.max_attempts:
                    raise TransientError(
                        f"backoff exhausted for {method} {url} (status {response.status_code})"
                    )
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                jitter = random.random()  # noqa: S311 # nosec B311 - retry-jitter, not crypto
                wait = delay_seconds(attempt, self.policy, jitter=jitter, retry_after=retry_after)
                time.sleep(wait)
                attempt += 1
                continue

            return response

    def _fetch_token(self, repo_path: str) -> str:
        response = self.client.get(
            f"{self.base_url}/token",
            params={"service": "ghcr.io", "scope": f"repository:{repo_path}:pull"},
            timeout=self.timeout,
        )
        if response.status_code == 403:
            raise ValidationError(_denied_message(repo_path))
        response.raise_for_status()
        return str(response.json()["token"])
