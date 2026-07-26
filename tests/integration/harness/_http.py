"""Shared HTTP-response primitives for the socket-level fakes.

`ScriptedResponse` is the one canned-response shape both `fake_ghcr` and
`fake_forge` script their routes with; `write_response` is the single place
either handler serializes one onto the wire (so HEAD-vs-body handling and the
`Content-Length` header live in exactly one function).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from http.server import BaseHTTPRequestHandler


@dataclass(frozen=True, slots=True)
class ScriptedResponse:
    """One canned HTTP response: a status plus optional body and headers.

    `body` is the exact bytes written on the wire (never re-encoded), so a
    malformed-JSON edge (`ScriptedResponse(status=200, body=b"not-json{")`)
    reaches the adapter's parser verbatim.
    """

    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict[str, str])


def json_response(
    status: int, payload: object, *, headers: Mapping[str, str] | None = None
) -> ScriptedResponse:
    """A `ScriptedResponse` whose body is `payload` JSON-encoded, with
    `Content-Type: application/json` merged ahead of any `headers`."""
    merged: dict[str, str] = {"Content-Type": "application/json"}
    if headers is not None:
        merged.update(headers)
    return ScriptedResponse(status=status, body=json.dumps(payload).encode("utf-8"), headers=merged)


def write_response(
    handler: BaseHTTPRequestHandler, response: ScriptedResponse, *, include_body: bool
) -> None:
    """Serialize `response` onto `handler`'s socket. `include_body=False` for a
    HEAD request (headers and `Content-Length` still sent, body suppressed —
    the shape `adapters/registry_v2.py::get_desc_tag_digest` reads)."""
    handler.send_response(response.status)
    for key, value in response.headers.items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(response.body)))
    handler.end_headers()
    if include_body:
        handler.wfile.write(response.body)
