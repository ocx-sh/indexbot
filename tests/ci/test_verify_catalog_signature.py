# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The OCX Authors

"""`scripts/verify-catalog-signature.sh` with a stub cosign: its argv and exit codes."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify-catalog-signature.sh"
IDENTITY = "https://github.com/ocx-sh/index/.github/workflows/render-deploy.yml@refs/heads/main"
ISSUER = "https://token.actions.githubusercontent.com"
DEFAULT_TYPE = "https://ocx.sh/attestation/index-catalog/v1"


def _run(
    tmp_path: Path,
    *,
    stub_exit: int = 0,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str] | None, Path, Path]:
    argv_log = tmp_path / "argv"
    stub = tmp_path / "cosign"
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{argv_log}'\nexit {stub_exit}\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    catalog, bundle = tmp_path / "index.json", tmp_path / "index.json.sigstore.json"
    catalog.write_text("{}")
    bundle.write_text("{}")
    base = {
        k: v
        for k, v in os.environ.items()
        if k not in {"SIGNER_IDENTITY", "SIGNER_ISSUER", "PREDICATE_TYPE"}
    }
    full = {
        **base,
        "COSIGN": str(stub),
        "SIGNER_IDENTITY": IDENTITY,
        "SIGNER_ISSUER": ISSUER,
        **(env or {}),
    }
    result = subprocess.run(  # noqa: S603 - the script under test, a fixed path
        [str(SCRIPT), *(args if args is not None else [str(catalog), str(bundle)])],
        env=full,
        capture_output=True,
        text=True,
        check=False,
    )
    argv = argv_log.read_text().splitlines() if argv_log.exists() else None
    return result, argv, catalog, bundle


def test_verifies_the_catalog_against_the_bundle_identity_and_type(tmp_path: Path) -> None:
    result, argv, catalog, bundle = _run(tmp_path)

    assert result.returncode == 0
    assert argv == [
        "verify-blob-attestation",
        "--bundle", str(bundle),
        "--type", DEFAULT_TYPE,
        "--certificate-identity", IDENTITY,
        "--certificate-oidc-issuer", ISSUER,
        str(catalog),
    ]  # fmt: skip


def test_cosign_refusal_code_is_passed_through(tmp_path: Path) -> None:
    result, _, _, _ = _run(tmp_path, stub_exit=1)

    assert result.returncode == 1


def test_predicate_type_is_overridable(tmp_path: Path) -> None:
    _, argv, _, _ = _run(tmp_path, env={"PREDICATE_TYPE": "urn:other"})

    assert argv is not None
    assert argv[argv.index("--type") + 1] == "urn:other"


@pytest.mark.parametrize("missing", ["SIGNER_IDENTITY", "SIGNER_ISSUER"])
def test_missing_signer_pin_is_a_usage_error_and_cosign_never_runs(
    tmp_path: Path, missing: str
) -> None:
    result, argv, _, _ = _run(tmp_path, env={missing: ""})

    assert result.returncode == 2
    assert missing in result.stderr
    assert argv is None


def test_wrong_argument_count_is_a_usage_error(tmp_path: Path) -> None:
    result, argv, _, _ = _run(tmp_path, args=["only-one"])

    assert result.returncode == 2
    assert argv is None


def test_missing_file_is_a_usage_error(tmp_path: Path) -> None:
    result, argv, _, _ = _run(tmp_path, args=[str(tmp_path / "nope"), str(tmp_path / "nope2")])

    assert result.returncode == 2
    assert argv is None
