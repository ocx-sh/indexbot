"""`indexbot check-expiry` — the catalog-expiry alert (plan C-503).

`run` is driven with a dict-backed `fetch`, so no test touches the network.
`FixedClock`'s default instant is `2026-07-17T00:00:00Z`; every `expires` is
chosen relative to it.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json

import pytest

from ocx_indexbot.cli import check_expiry
from ocx_indexbot.errors import TransientError
from ocx_indexbot.exit_codes import ExitCode
from tests.fakes import FixedClock

_NOW_PLUS_30 = "2026-08-16T00:00:00Z"
_NOW_PLUS_20 = "2026-08-06T00:00:00Z"
_CATALOG_URL = "https://index.example/c/index.json"
_BUNDLE_URL = "https://index.example/c/index.json.sigstore.json"


def _catalog(**fields: object) -> bytes:
    return json.dumps({"format_version": 1, "packages": {}, **fields}).encode()


def _bundle(
    catalog: bytes,
    *,
    digest: str | None = None,
    name: str = "c/index.json",
    predicate_type: str = check_expiry.PREDICATE_TYPE,
    subjects: int = 1,
) -> bytes:
    sha = digest or hashlib.sha256(catalog).hexdigest()
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": name, "digest": {"sha256": sha}}] * subjects,
        "predicateType": predicate_type,
        "predicate": {"registry": "ocx.sh"},
    }
    payload = base64.b64encode(json.dumps(statement).encode()).decode()
    return json.dumps({"dsseEnvelope": {"payload": payload}}).encode()


def _args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {"catalog": _CATALOG_URL, "bundle": None, "min_days": 21.0}
    base.update(overrides)
    return argparse.Namespace(**base)


def _run(files: dict[str, bytes], **overrides: object) -> ExitCode:
    def fetch(source: str) -> bytes:
        return files[source]

    return check_expiry.run(_args(**overrides), fetch=fetch, clock=FixedClock())


def test_fresh_catalog_exits_ok_and_prints_remaining_days(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run({_CATALOG_URL: _catalog(expires=_NOW_PLUS_30)})
    assert code is ExitCode.OK
    assert "30.0 day(s) remaining" in capsys.readouterr().out


def test_less_than_min_days_remaining_fails(capsys: pytest.CaptureFixture[str]) -> None:
    code = _run({_CATALOG_URL: _catalog(expires=_NOW_PLUS_20)})
    assert code is ExitCode.VALIDATION_FAILURE
    assert "20.0 day(s) remaining" in capsys.readouterr().err


def test_exactly_min_days_remaining_passes() -> None:
    assert _run({_CATALOG_URL: _catalog(expires="2026-08-07T00:00:00Z")}) is ExitCode.OK


def test_min_days_is_configurable() -> None:
    files = {_CATALOG_URL: _catalog(expires=_NOW_PLUS_20)}
    assert _run(files, min_days=10.0) is ExitCode.OK


def test_expired_catalog_fails_with_negative_remaining(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run({_CATALOG_URL: _catalog(expires="2026-07-16T00:00:00Z")})
    assert code is ExitCode.VALIDATION_FAILURE
    assert "-1.0 day(s) remaining" in capsys.readouterr().err


def test_absent_expires_fails() -> None:
    assert _run({_CATALOG_URL: _catalog()}) is ExitCode.VALIDATION_FAILURE


@pytest.mark.parametrize("value", ["tomorrow", 12, None, "2026-08-16T00:00:00", "2026-08-16"])
def test_unparseable_or_zoneless_expires_fails(value: object) -> None:
    assert _run({_CATALOG_URL: _catalog(expires=value)}) is ExitCode.VALIDATION_FAILURE


@pytest.mark.parametrize("raw", [b"not json", b"[]"])
def test_catalog_that_is_not_a_json_object_fails(raw: bytes) -> None:
    assert _run({_CATALOG_URL: raw}) is ExitCode.VALIDATION_FAILURE


def test_matching_bundle_passes() -> None:
    catalog = _catalog(expires=_NOW_PLUS_30)
    files = {_CATALOG_URL: catalog, _BUNDLE_URL: _bundle(catalog)}
    assert _run(files, bundle=_BUNDLE_URL) is ExitCode.OK


def test_bundle_for_other_bytes_fails(capsys: pytest.CaptureFixture[str]) -> None:
    catalog = _catalog(expires=_NOW_PLUS_30)
    files = {_CATALOG_URL: catalog, _BUNDLE_URL: _bundle(catalog, digest="0" * 64)}
    assert _run(files, bundle=_BUNDLE_URL) is ExitCode.VALIDATION_FAILURE
    assert "digest" in capsys.readouterr().err


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "other.json"},
        {"predicate_type": "https://example/other/v1"},
        {"subjects": 2},
    ],
)
def test_bundle_with_wrong_statement_shape_fails(kwargs: dict[str, object]) -> None:
    catalog = _catalog(expires=_NOW_PLUS_30)
    files = {_CATALOG_URL: catalog, _BUNDLE_URL: _bundle(catalog, **kwargs)}  # type: ignore[arg-type]
    assert _run(files, bundle=_BUNDLE_URL) is ExitCode.VALIDATION_FAILURE


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b"{}",
        json.dumps({"dsseEnvelope": {"payload": "!!!"}}).encode(),
        json.dumps({"dsseEnvelope": {"payload": base64.b64encode(b"nope").decode()}}).encode(),
        json.dumps({"dsseEnvelope": {"payload": base64.b64encode(b"[]").decode()}}).encode(),
        json.dumps(
            {
                "dsseEnvelope": {
                    "payload": base64.b64encode(
                        json.dumps(
                            {"predicateType": check_expiry.PREDICATE_TYPE, "subject": [1]}
                        ).encode()
                    ).decode()
                }
            }
        ).encode(),
    ],
)
def test_garbage_bundle_fails(raw: bytes) -> None:
    catalog = _catalog(expires=_NOW_PLUS_30)
    assert _run({_CATALOG_URL: catalog, _BUNDLE_URL: raw}, bundle=_BUNDLE_URL) is (
        ExitCode.VALIDATION_FAILURE
    )


def test_expiry_failure_does_not_hide_a_bad_bundle(capsys: pytest.CaptureFixture[str]) -> None:
    catalog = _catalog(expires=_NOW_PLUS_20)
    files = {_CATALOG_URL: catalog, _BUNDLE_URL: b"garbage"}
    assert _run(files, bundle=_BUNDLE_URL) is ExitCode.VALIDATION_FAILURE
    err = capsys.readouterr().err
    assert "remaining" in err
    assert "bundle" in err


def test_fetch_errors_propagate_as_index_bot_errors() -> None:
    def fetch(source: str) -> bytes:
        raise TransientError(f"cannot fetch {source}")

    with pytest.raises(TransientError):
        check_expiry.run(_args(), fetch=fetch, clock=FixedClock())


def test_add_arguments_requires_catalog_and_defaults_to_21_days() -> None:
    parser = argparse.ArgumentParser()
    check_expiry.add_arguments(parser)
    ns = parser.parse_args(["--catalog", "c.json"])
    assert (ns.catalog, ns.bundle, ns.min_days) == ("c.json", None, 21.0)
    with pytest.raises(SystemExit):
        parser.parse_args([])
