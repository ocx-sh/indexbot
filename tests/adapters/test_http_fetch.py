"""`adapters/http_fetch.fetch_source` — https URL or local path, bounded."""

from __future__ import annotations

import httpx
import pytest

from ocx_indexbot.adapters import http_fetch
from ocx_indexbot.errors import TransientError, ValidationError


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def test_https_url_returns_body() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, content=b"hi")))
    assert http_fetch.fetch_source("https://x.example/c", client=client) == b"hi"


def test_404_is_a_validation_failure() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(404)))
    with pytest.raises(ValidationError, match="404"):
        http_fetch.fetch_source("https://x.example/c", client=client)


def test_5xx_is_transient() -> None:
    client = _client(httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(TransientError):
        http_fetch.fetch_source("https://x.example/c", client=client)


def test_connection_error_is_transient() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(TransientError):
        http_fetch.fetch_source("https://x.example/c", client=_client(httpx.MockTransport(boom)))


def test_oversize_body_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_fetch, "MAX_BYTES", 3)
    client = _client(httpx.MockTransport(lambda r: httpx.Response(200, content=b"abcd")))
    with pytest.raises(ValidationError, match="exceeds"):
        http_fetch.fetch_source("https://x.example/c", client=client)


def test_default_client_is_built_when_none_given(monkeypatch: pytest.MonkeyPatch) -> None:
    real = httpx.Client

    def mocked(**_kw: float) -> httpx.Client:
        return real(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"ok")))

    monkeypatch.setattr(http_fetch.httpx, "Client", mocked)
    assert http_fetch.fetch_source("https://x.example/c") == b"ok"


def test_local_path_is_read_and_missing_or_http_refused(tmp_path: object) -> None:
    from pathlib import Path

    f = Path(str(tmp_path)) / "c.json"
    f.write_bytes(b"{}")
    assert http_fetch.fetch_source(str(f)) == b"{}"
    with pytest.raises(ValidationError):
        http_fetch.fetch_source(str(f) + ".missing")
    with pytest.raises(ValidationError):
        http_fetch.fetch_source("http://insecure.example/c.json")
