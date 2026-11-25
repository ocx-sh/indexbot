"""Fetch one document from an `https://` URL or a local path.

Used by `indexbot check-expiry`, which reads a published catalog and its
signature bundle. The two inputs are public, unauthenticated documents, so no
credential is ever attached. Plain `http://` is refused: a catalog read over
cleartext proves nothing about the served bytes.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from ocx_indexbot.errors import TransientError, ValidationError

MAX_BYTES = 64 * 1024 * 1024
"""Upper bound on a fetched document. Not `Final` — tests patch it."""

_TIMEOUT_SECONDS = 30.0


def fetch_source(source: str, *, client: httpx.Client | None = None) -> bytes:
    """Bytes of `source`: an `https://` URL, or a local file path.

    A 4xx answer or a missing file is a `ValidationError` (the document is not
    there); a connection failure or a 5xx is a `TransientError` (try later).
    """
    if source.startswith("http://"):
        raise ValidationError(f"refusing cleartext URL {source!r}: use https://")
    if not source.startswith("https://"):
        try:
            data = Path(source).read_bytes()
        except OSError as exc:
            raise ValidationError(f"cannot read {source!r}: {exc.strerror}") from exc
        return _bounded(source, data)
    http = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
    try:
        response = http.get(source)
    except httpx.HTTPError as exc:
        raise TransientError(f"cannot fetch {source!r}: {type(exc).__name__}") from exc
    if response.status_code >= 500:
        raise TransientError(f"{source!r} answered {response.status_code}")
    if response.status_code >= 400:
        raise ValidationError(f"{source!r} answered {response.status_code}")
    return _bounded(source, response.content)


def _bounded(source: str, data: bytes) -> bytes:
    if len(data) > MAX_BYTES:
        raise ValidationError(f"{source!r} exceeds {MAX_BYTES} bytes")
    return data
