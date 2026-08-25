"""OCI Distribution (Registry v2) `RegistryPort` implementation
(CONTRACTS.md §9).

One client class, configured per host: `ghcr.io` and `ocx.sh` speak the same
`/v2/` API with the same anonymous-pull token dance, and differ only in which
URL issues the token (GHCR: `https://ghcr.io/token`; `ocx.sh` is an
Artifactory-backed registry whose `WWW-Authenticate` realm sits under
`/artifactory/...`). `RoutedRegistry` below picks the configured client for a
`repository` URI's host, so `core/` still sees a single `RegistryPort`.

The only place `httpx` is imported for registry reads (ADR-4 BD-1, functional
core / imperative shell). Owns three things `core/` never sees the mechanics
of:

- The anonymous bearer-token dance: an unauthenticated request gets a `401`,
  a pull token is fetched from `GET <realm>?service=<host>&scope=
  repository:<path>:pull` (no credentials required for a publicly readable
  repository), and the original request is retried once with
  `Authorization: Bearer <token>`. The token is cached per repository path
  for this instance's lifetime and refreshed (not counted against
  `BackoffPolicy.max_attempts`) on exactly one fresh `401` — a second
  consecutive `401` for the same logical request is a persistent auth
  failure, raised as `TransientError`.
- A `403` from either the token endpoint or a `/v2/` API call — the `DENIED`
  response for a repository that is missing or private, body present or not
  — is a permanent condition, never a bug and never worth retrying: raised
  as `ValidationError`, distinct from the `401` dance above (which *can*
  succeed once a token is attached) and from `TransientError` (which implies
  retrying later might help).
- `tags/list` pagination via the RFC 8288 `Link` response header, bounded by
  `max_pages` so a misbehaving/malicious next-link chain can't loop forever.
- The retry loop — the imperative-shell half of `core/backoff.py`'s pure
  timing decisions (CONTRACTS.md §7): this module calls `time.sleep`
  directly, `core/backoff.py` only computes *how long*. It spends one
  `BackoffPolicy` budget on two failure kinds: retryable *statuses*
  (429/5xx, via `is_retryable_status`) and transport *failures* (timeout,
  connection reset, protocol error), which are exceptions and so never reach
  a status test at all. Both exhaust into `TransientError`; a bare `httpx`
  exception must never escape this module, because `cli/main.py` only maps
  `IndexBotError` onto an exit code and a step summary.

`repository` arguments are always the full `oci://<host>/<path>` URI stored
in `PackageRoot.repository` (see `core/validate_entry.py`'s
`check_repository_allowlisted`/`check_repository_shape`, which already ran
against this same string before any `RegistryPort` call reaches here per BD-1's
SSRF ordering) — `RegistryV2` only ever parses out the `<path>` portion; the
host is re-read exactly once, by `RoutedRegistry`, to choose a client.

`reference` (a tag name or digest passed to `get_manifest`/`get_blob`) carries
no equivalent upstream gate against the registry-URL grammar specifically —
it is a tag KEY straight from a PR-authored root, validated only against the
wire tag grammar (`core/validate_entry.py`'s `parse_package_root`), never
against "safe to drop unescaped into a URL path segment". `_url_repo_path`/
`_url_reference` below percent-encode both interpolated components at every
URL builder in this module, so a hostile `reference` can never retarget a
request outside the path segment it was built for (A-5).
"""

from __future__ import annotations

import base64
import hashlib
import random
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import quote, urlsplit

import httpx

from ocx_indexbot.core.backoff import BackoffPolicy, delay_seconds, is_retryable_status
from ocx_indexbot.errors import AnomalyError, TransientError, ValidationError
from ocx_indexbot.model import ManifestFetch

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ocx_indexbot.core.policy import RegistryAuth
    from ocx_indexbot.model import OwnershipProbeResult

GHCR_HOST: Final[str] = "ghcr.io"
"""GitHub Container Registry — the host every third-party mirror lives on.

`cli/_wiring.py` builds its servable-host set from this and `OCX_SH_HOST`: a
deployment policy (`.github/index-policy.json`) that allowlists a host no
adapter implements is refused at wiring time rather than producing roots that
validate and then cannot be fetched. Keep these the single source of the
literals — `base_url` and the token endpoint's `service` parameter both
derive from them."""

OCX_SH_HOST: Final[str] = "ocx.sh"
"""The index operator's own registry, home of the first-party `ocx/cli`,
`ocx/mirror` and `regclient/regsync` repositories. Anonymously readable, same
Registry v2 API as GHCR."""

OCX_SH_REALM: Final[str] = "https://ocx.sh/artifactory/api/docker/sh-ocx-oci-prod/v2/token"
"""`ocx.sh`'s pull-token endpoint — the `realm` its `/v2/` `401` advertises
(`WWW-Authenticate: Bearer realm="…",service="ocx.sh"`), which is NOT
`https://ocx.sh/token` (that path 404s: the registry is Artifactory-backed
and issues tokens under its own repository path).

Pinned as a constant rather than followed from the response header on the
fly: a `realm` is a server-supplied URL, and this adapter already refuses to
follow a server-supplied cross-host pagination link (`_parse_next_link`) for
the same SSRF reason. If JFrog ever moves it, the token fetch 404s loudly at
`raise_for_status` — a visible break, not a silent widening of where this bot
sends requests."""

GITLAB_HOST: Final[str] = "registry.gitlab.com"
"""GitLab's shared container registry. Every GitLab project gets one, which
makes it the natural physical layer for a GitLab-hosted index — and a host no
OCX deployment allowlists, so it only ever arrives through a copy's own
committed policy."""

GITLAB_REALM: Final[str] = "https://gitlab.com/jwt/auth"
"""`registry.gitlab.com`'s pull-token endpoint, from the `realm` its `/v2/`
`401` advertises. Pinned rather than followed from the response header for
the same SSRF reason as `OCX_SH_REALM`."""

GITLAB_SERVICE: Final[str] = "container_registry"
"""GitLab's token `service`, which is a fixed literal and NOT its host.

GHCR and `ocx.sh` both name themselves here, so the adapter passed `host` for
this parameter and the coupling went unnoticed. Sending
`service=registry.gitlab.com` yields a token that is not valid for the
requested scope — an index whose downloads 401 rather than one that works."""

BUILTIN_REGISTRIES: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        GHCR_HOST: MappingProxyType({}),
        OCX_SH_HOST: MappingProxyType({"realm": OCX_SH_REALM}),
        GITLAB_HOST: MappingProxyType({"realm": GITLAB_REALM, "service": GITLAB_SERVICE}),
    }
)
"""What a bare-string `registry_hosts` entry means, for the hosts whose
non-default token endpoints this package already knows.

An index that writes `"ghcr.io"` gets the Registry v2 conventions;
`"ocx.sh"` and `"registry.gitlab.com"` additionally get the two facts no
convention supplies (Artifactory's token path, GitLab's fixed `service`
literal). Every other host either follows the conventions — `https://<host>`,
`<base_url>/token`, `service = host` — or states its own in an object entry.
This is a convenience table, not an allowlist: `cli/_wiring.py` builds a
client for whatever the policy names, and a host absent from here is not
refused, only undefaulted."""

_DEFAULT_TIMEOUT_SECONDS = 30.0
"""Per-request deadline handed to every `httpx` call.

Raised from 10s after a GHCR manifest GET stalled past it and failed a
REQUIRED announce check. A manifest is a few KiB, so a request that has not
answered inside 30s is a stall, not a slow transfer — and `_send` now spends
its `BackoffPolicy` budget on the retry rather than surfacing the timeout,
so the generous per-attempt deadline costs nothing on the happy path.
"""

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


def _url_repo_path(repo_path: str) -> str:
    """Percent-encode `repo_path` for interpolation into a `/v2/` URL path.
    `/` stays literal — a multi-component repository path is legitimate —
    everything else percent-encodes per RFC 3986.

    Defence in depth (A-5), not the primary control: `repo_path` is always
    the parsed-out path of a `repository` string `check_repository_allowlisted`/
    `check_repository_shape` already ran against (module docstring, BD-1 SSRF
    ordering), so this is a no-op for every value that reaches here in
    practice. It exists so an adapter bug upstream of that gate fails safe
    rather than open.
    """
    return quote(repo_path, safe="/")


def _url_reference(reference: str) -> str:
    """Percent-encode `reference` — a tag name or digest — for interpolation
    into a `/manifests/<reference>` or `/blobs/<digest>` URL path segment.
    `:` stays literal (a digest reference `sha256:<hex>` legitimately
    contains one); everything else, including `/`, percent-encodes.

    This is the fix for A-5: `reference` is a tag KEY straight from a
    PR-authored root, and nothing upstream of this adapter validates its
    shape against the registry-URL grammar (only
    `core/validate_entry.py`'s `parse_package_root` enforces the wire tag
    grammar, which is a different, narrower property). Before this encoding,
    a tag named `../blobs/sha256:<hex>` retargeted the request at a sibling
    URL segment in the same repository — httpx collapses a literal `../` in
    a path client-side. Percent-encoding `/` here means there is no longer a
    literal path separator in `reference` for that collapse to act on.
    """
    return quote(reference, safe=":")


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


_MAX_REDIRECTS: Final[int] = 5
"""Redirect hops a single registry request may take.

One is what a CDN-fronted blob costs. A chain longer than this is either a
loop or a redirector being used as one, and neither is worth a request.
"""


def _same_origin(current: str, target: str) -> bool:
    left, right = urlsplit(current), urlsplit(target)
    return (left.scheme, left.netloc) == (right.scheme, right.netloc)


def _denied_message(host: str, repo_path: str, credentialed: bool = False) -> str:
    """`403`/`DENIED` from the registry (token endpoint or a `/v2/` call),
    body present or empty — the repository is missing, or the identity this
    bot presented does not grant `:pull` on it. Permanent, not retryable:
    distinct from a `401`, which the token dance above can still recover
    from.

    Which identity that was decides what the operator has to fix, so the
    message says: with no credential configured the answer is "grant
    anonymous pull", and with one it is "grant this account pull" — never the
    first sentence when a credential was in fact sent, which would send an
    operator looking for a permission they already have.
    """
    identity = (
        "the configured credentials do not grant :pull on it"
        if credentialed
        else "it does not allow anonymous pull"
    )
    remedy = "grant that account" if credentialed else "grant anonymous"
    return (
        f"{host}/{repo_path} is missing or {identity} "
        "(the registry denied the request with 403); the repository must exist and "
        f"{remedy} :pull access before this bot can observe it"
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
class RegistryV2:
    """`RegistryPort` over one Registry v2 host (defaults to `ghcr.io`). One
    instance per host per process run — `_tokens` caches one anonymous pull
    token per repository path for this instance's lifetime (CONTRACTS.md §9).

    `host` is the *logical* host (the one in `oci://<host>/<path>`, used for
    the token request's `service` parameter and for error messages); it is
    deliberately separate from `base_url`, which the integration harness
    repoints at a loopback fake while the roots under test still name
    `ghcr.io`.
    """

    base_url: str = f"https://{GHCR_HOST}"
    host: str = GHCR_HOST
    realm: str = ""
    service: str = ""
    auth: RegistryAuth = "token"
    credentials: str = field(default="", repr=False)
    timeout: float = _DEFAULT_TIMEOUT_SECONDS
    policy: BackoffPolicy = field(default_factory=BackoffPolicy)
    max_pages: int = _DEFAULT_MAX_PAGES
    client: httpx.Client = field(default_factory=httpx.Client)
    _tokens: dict[str, str] = field(default_factory=dict[str, str], init=False, repr=False)
    _basic: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        """Default the token endpoint to `<base_url>/token` — the Registry v2
        convention GHCR follows, and the shape the integration harness's fake
        serves, so repointing `base_url` at a loopback fake moves the token
        endpoint with it. `ocx.sh` passes its own `realm` explicitly
        because Artifactory issues tokens from a different path.

        `service` defaults to `host` — the convention GHCR and `ocx.sh` both
        follow. GitLab does not, and passes its own literal.

        `credentials` is `user:password` (Docker's own convention) and is
        base64'd here, once, into the header every authenticated request
        reuses — never split into parts, because RFC 7617 encodes the pair
        verbatim and a password may legitimately contain a colon. A value
        with no colon at all is refused loudly: it is a misconfigured secret,
        and the alternative is a registry that answers 401 forever for a
        reason nobody can see. The message names the host, never the value.
        """
        if not self.service:
            self.service = self.host
        if not self.realm:
            self.realm = f"{self.base_url}/token"
        if self.credentials:
            if ":" not in self.credentials:
                raise ValidationError(
                    f"{self.host}: malformed registry credentials — expected 'user:password' "
                    "(Docker's convention), got a value with no ':' separator"
                )
            encoded = base64.b64encode(self.credentials.encode()).decode()
            self._basic = f"Basic {encoded}"

    def list_tags(self, repository: str) -> list[str]:
        repo_path = _repo_path(repository)
        url: str | None = f"{self.base_url}/v2/{_url_repo_path(repo_path)}/tags/list"
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
        url = (
            f"{self.base_url}/v2/{_url_repo_path(repo_path)}/manifests/{_url_reference(reference)}"
        )
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
        # A digest reference is a *demand*, not a lookup key: whatever comes
        # back must be the content that was asked for. A tag reference makes
        # no such claim — the computed digest is the answer there.
        if reference.startswith("sha256:") and reference != computed_digest:
            raise AnomalyError(
                f"manifest digest mismatch for {repository}@{reference}: "
                f"served bytes hash to {computed_digest!r}"
            )
        return ManifestFetch(raw=raw, digest=computed_digest, parsed=response.json())

    def get_desc_tag_digest(self, repository: str) -> str | None:
        repo_path = _repo_path(repository)
        url = f"{self.base_url}/v2/{_url_repo_path(repo_path)}/manifests/{_DESC_TAG}"
        response = self._send(
            "HEAD", url, repo_path=repo_path, headers={"Accept": _MANIFEST_ACCEPT}
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.headers["Docker-Content-Digest"]

    def get_blob(self, repository: str, digest: str) -> bytes:
        repo_path = _repo_path(repository)
        url = f"{self.base_url}/v2/{_url_repo_path(repo_path)}/blobs/{_url_reference(digest)}"
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
        wrapped in the backoff loop, which covers both retryable *statuses*
        (429/5xx) and transport *failures* (timeout, reset, protocol error)."""
        auth_retried = False
        attempt = 1
        while True:
            request_headers = dict(headers or {})
            authorization = self._authorization(repo_path)
            if authorization is not None:
                request_headers["Authorization"] = authorization

            try:
                response = self.client.request(
                    method, url, headers=request_headers, params=params, timeout=self.timeout
                )

                if response.status_code == 401:
                    # `basic` mode has nothing to refresh: the credential it
                    # already sent is the whole of what it can offer, so a
                    # second identical attempt is only a slower failure.
                    if auth_retried or self.auth == "basic":
                        raise TransientError(f"persistent 401 for {method} {url}")
                    self._tokens[repo_path] = self._fetch_token(repo_path)
                    # Set only after the fetch lands: a token fetch that dies
                    # on a transport failure must leave the 401 lane re-armed,
                    # or the retry below sends unauthenticated again and trips
                    # "persistent 401" instead of spending its budget.
                    auth_retried = True
                    continue
            except httpx.TransportError as exc:
                # A timeout / connection reset / protocol error is an
                # exception, not a status, so `is_retryable_status` never sees
                # it. Without this it escapes the loop, past `main()`'s
                # `IndexBotError` handler, and fails the run with a bare
                # traceback — no `ExitCode.TRANSIENT`, no step summary. Same
                # attempt budget as a 429/5xx; `TransientError` when spent.
                if attempt >= self.policy.max_attempts:
                    raise TransientError(
                        f"transport failure for {method} {url} after {attempt} attempts: {exc!r}"
                    ) from exc
                jitter = random.random()  # noqa: S311 # nosec B311 - retry-jitter, not crypto
                time.sleep(delay_seconds(attempt, self.policy, jitter=jitter))
                attempt += 1
                continue

            if response.status_code == 403:
                raise ValidationError(_denied_message(self.host, repo_path, bool(self.credentials)))

            if response.is_redirect:
                return self._follow(response, repo_path=repo_path)

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

    def _follow(self, response: httpx.Response, *, repo_path: str) -> httpx.Response:
        """Follow a registry redirect, **without carrying the token across a
        host boundary**.

        Blob GETs are redirected by every registry that fronts storage with a
        CDN. Measured on ghcr.io: `GET /v2/<repo>/blobs/<digest>` answers
        `307` to `pkg-containers.githubusercontent.com` with a pre-signed URL.
        httpx does not follow redirects by default, so before this the 307
        itself was returned — `raise_for_status()` does not consider a 3xx an
        error — and `get_blob` handed back the redirect body as if it were the
        blob. Every desc blob (README, logo) read from ghcr.io was wrong bytes.

        The redirect target is pre-signed and needs no `Authorization` of its
        own; sending one anyway hands a registry pull token to whatever host
        the redirect names, which is the leak the OCI distribution spec warns
        about. So the header is dropped whenever the hop leaves the current
        host, and kept only on a same-origin hop.
        """
        seen = 0
        while response.is_redirect and seen < _MAX_REDIRECTS:
            if response.next_request is None:
                # A 3xx with no usable `Location`. Returning it would put the
                # caller back where this method started: a non-error status
                # whose body is not the resource.
                raise TransientError(f"registry redirected {response.url} without a location")
            target = str(response.next_request.url)
            headers: dict[str, str] = {}
            if _same_origin(str(response.url), target):
                authorization = self._authorization(repo_path)
                if authorization is not None:
                    headers["Authorization"] = authorization
            response = self.client.request("GET", target, headers=headers, timeout=self.timeout)
            seen += 1
        if response.is_redirect:
            raise TransientError(f"registry redirected more than {_MAX_REDIRECTS} times")
        return response

    def _authorization(self, repo_path: str) -> str | None:
        """The `Authorization` header this instance sends for `repo_path`, or
        `None` when it has nothing to send yet.

        One chokepoint for both credential shapes, so every call site — the
        request loop and the same-origin redirect hop — carries exactly the
        same rule, and a future third shape has one place to be wrong in.
        """
        if self.auth == "basic":
            return self._basic or None
        token = self._tokens.get(repo_path)
        return None if token is None else f"Bearer {token}"

    def _fetch_token(self, repo_path: str) -> str:
        """Exchange `realm` for a pull token, authenticating the exchange
        itself when this registry has credentials.

        Basic-on-the-token-request is Docker's own flow: the realm is the
        only endpoint that ever sees the operator's credential, and what
        reaches `/v2/` afterwards is a scoped, short-lived Bearer.
        """
        response = self.client.get(
            self.realm,
            params={"service": self.service, "scope": f"repository:{repo_path}:pull"},
            headers={"Authorization": self._basic} if self._basic else None,
            timeout=self.timeout,
        )
        if response.status_code == 403:
            raise ValidationError(_denied_message(self.host, repo_path, bool(self.credentials)))
        response.raise_for_status()
        return str(response.json()["token"])


@dataclass(slots=True)
class RoutedRegistry:
    """`RegistryPort` that picks a per-host `RegistryV2` from the
    `oci://<host>/<path>` URI it is handed.

    The index serves more than one registry host (`.github/index-policy.json`),
    and one `validate`/`reconcile` run can touch roots on both — so the choice
    has to be per call, not per run. `cli/_wiring.py` builds the mapping, one
    client per `registry_hosts` entry, so its keys are exactly this
    deployment's allowlist.

    A host with no client is a `ValidationError`, not a `KeyError`: it is
    unreachable in production (`check_repository_allowlisted` refuses any host
    outside the policy, and every policy host gets a client by construction,
    both before this class ever sees the URI), so this is the
    defence-in-depth backstop for a future wiring bug — and it must fail the
    same closed way an out-of-policy host does.
    """

    by_host: dict[str, RegistryV2]

    def list_tags(self, repository: str) -> list[str]:
        return self._client(repository).list_tags(repository)

    def get_manifest(self, repository: str, reference: str) -> ManifestFetch:
        return self._client(repository).get_manifest(repository, reference)

    def get_desc_tag_digest(self, repository: str) -> str | None:
        return self._client(repository).get_desc_tag_digest(repository)

    def get_blob(self, repository: str, digest: str) -> bytes:
        return self._client(repository).get_blob(repository, digest)

    def probe_ownership(self, repository: str, expected_name: str) -> OwnershipProbeResult:
        return self._client(repository).probe_ownership(repository, expected_name)

    def _client(self, repository: str) -> RegistryV2:
        # `hostname`, never `netloc`: G-03 matches a policy entry against the
        # port-stripped hostname (`core/validate_entry.check_repository_allowlisted`),
        # so keying this lookup on `netloc` made `oci://harbor.corp:5000/team/tool`
        # pass validation and then find no client at all. Where the requests
        # actually go is the entry's own `base_url`, port included.
        host = urlsplit(repository).hostname
        client = None if host is None else self.by_host.get(host)
        if client is None:
            raise ValidationError(
                f"no registry client for host {host!r} (serving {sorted(self.by_host)})"
            )
        return client
