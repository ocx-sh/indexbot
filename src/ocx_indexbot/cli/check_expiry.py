"""`indexbot check-expiry` — alert before a published catalog's signature lapses
(plan C-503; signing-dogfood index signing).

An index signs `c/index.json` and stamps an `expires` instant into the signed
bytes. A client under `index_signature = "enforce"` refuses a catalog past
that instant, so a quiet re-sign job that stops working turns into an outage
a few weeks later. This command is the early warning: run on a schedule, a
red run is the alert.

Usage::

    indexbot check-expiry --catalog <https-URL | path> [--bundle <https-URL | path>]
                          [--min-days 21]

Checks, in order; every failure is reported, then the run exits non-zero:

1. `--catalog` is a JSON object carrying `expires`, an RFC 3339 instant with an
   explicit zone, at least `--min-days` away. The remaining days print to
   stdout on success and to stderr on failure.
2. With `--bundle`: the Sigstore bundle's DSSE statement has exactly one
   subject named `c/index.json`, whose sha256 equals the fetched catalog bytes,
   under predicate type `PREDICATE_TYPE`. A fresh catalog beside a missing or
   stale signature would pass check 1 while `enforce` clients fail.

This is a *binding* check, not a cryptographic one: the certificate chain and
signer identity are verified by `cosign verify-blob-attestation` in
`.github/workflows/catalog-expiry.yml`, so cosign stays a workflow tool and
this package gains no crypto dependency.

Exit codes: `0` all checks pass; `1` (`VALIDATION_FAILURE`) a check failed or an
input is absent (HTTP 4xx, missing file) or malformed; `75` (`TRANSIENT`) a
fetch hit a connection failure or HTTP 5xx; `2` argparse usage error.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sys
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast

from ocx_indexbot.errors import ValidationError
from ocx_indexbot.exit_codes import ExitCode

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from ocx_indexbot.ports import ClockPort

PREDICATE_TYPE: Final[str] = "https://ocx.sh/attestation/index-catalog/v1"
SUBJECT_NAME: Final[str] = "c/index.json"
DEFAULT_MIN_DAYS: Final[float] = 21.0
_SECONDS_PER_DAY: Final[float] = 86400.0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--catalog", required=True, help="catalog to check: an https:// URL or a file path"
    )
    parser.add_argument(
        "--bundle",
        default=None,
        help="its Sigstore bundle (https:// URL or file path); its statement must bind the catalog",
    )
    parser.add_argument(
        "--min-days",
        type=float,
        default=DEFAULT_MIN_DAYS,
        help=f"fail when fewer days remain (default: {DEFAULT_MIN_DAYS:g})",
    )


def _remaining_days(catalog: bytes, now: str) -> float:
    try:
        doc = json.loads(catalog)
    except ValueError as exc:
        raise ValidationError("catalog is not valid JSON") from exc
    if not isinstance(doc, dict):
        raise ValidationError("catalog is not a JSON object")
    expires = cast("dict[str, Any]", doc).get("expires")
    if expires is None:
        raise ValidationError("catalog has no `expires`")
    try:
        instant = datetime.fromisoformat(cast("str", expires))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"`expires` is not an RFC 3339 instant: {expires!r}") from exc
    if instant.tzinfo is None:
        raise ValidationError(f"`expires` has no zone: {expires!r}")
    return (instant - datetime.fromisoformat(now)).total_seconds() / _SECONDS_PER_DAY


def _check_bundle(bundle: bytes, catalog: bytes) -> None:
    try:
        envelope = json.loads(bundle)["dsseEnvelope"]["payload"]
        statement = json.loads(base64.b64decode(envelope, validate=True))
        subjects = statement["subject"]
        predicate_type = statement["predicateType"]
        name = subjects[0]["name"]
        digest = subjects[0]["digest"]["sha256"]
    except (ValueError, KeyError, IndexError, TypeError, binascii.Error) as exc:
        raise ValidationError("bundle carries no readable DSSE statement") from exc
    if predicate_type != PREDICATE_TYPE:
        raise ValidationError(f"bundle predicate type is {predicate_type!r}")
    if len(subjects) != 1 or name != SUBJECT_NAME:
        raise ValidationError(f"bundle must have exactly one subject named {SUBJECT_NAME!r}")
    if digest != hashlib.sha256(catalog).hexdigest():
        raise ValidationError("bundle subject digest does not match the catalog bytes")


def run(args: argparse.Namespace, *, fetch: Callable[[str], bytes], clock: ClockPort) -> ExitCode:
    """`indexbot check-expiry` entry point. Fetch errors (`IndexBotError`) are
    left to `cli/main.py`'s single exit-code mapping."""
    catalog = fetch(cast("str", args.catalog))
    bundle_source = cast("str | None", args.bundle)
    bundle = fetch(bundle_source) if bundle_source is not None else None
    min_days = cast("float", args.min_days)
    failed = False

    try:
        days = _remaining_days(catalog, clock.now_iso8601())
    except ValidationError as exc:
        print(f"check-expiry: {exc}", file=sys.stderr)
        failed = True
    else:
        if days < min_days:
            print(
                f"check-expiry: {days:.1f} day(s) remaining, below the {min_days:g}-day floor",
                file=sys.stderr,
            )
            failed = True
        else:
            print(f"check-expiry: {days:.1f} day(s) remaining")

    if bundle is not None:
        try:
            _check_bundle(bundle, catalog)
        except ValidationError as exc:
            print(f"check-expiry: {exc}", file=sys.stderr)
            failed = True

    return ExitCode.VALIDATION_FAILURE if failed else ExitCode.OK
