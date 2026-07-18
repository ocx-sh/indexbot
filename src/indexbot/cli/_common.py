"""CLI-layer plumbing: the `GITHUB_OUTPUT` writer.

Pure I/O-boundary helper — no subcommand business logic here (that is
`cli/<subcommand>.py`). `read_validated_env` (the `repository_dispatch`
`PACKAGE_ID` env-var-indirection reader, ADR-4 BD-4) retired with
`cli/announce.py`'s doorbell pipeline in the fork-PR announce revamp — every
remaining subcommand takes its inputs as CLI args or already-trusted
GitHub-Actions-runner env vars (`cli/_wiring.py`'s `_require_env`), neither
of which needs the untrusted-payload length-cap-then-fullmatch discipline
this module used to also carry.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

_MAX_DELIMITER_ATTEMPTS = 5


def _random_delimiter() -> str:
    """One random, unguessable multiline-output delimiter."""
    return f"ghadelim_{secrets.token_hex(16)}"


def write_github_output(name: str, value: str) -> None:
    """Append `name=value` to `$GITHUB_OUTPUT`.

    Always uses GitHub's multiline delimiter/heredoc form
    (`name<<DELIM\\nvalue\\nDELIM\\n`) so callers never need to special-case
    a value that turns out to contain a newline.

    A fresh random delimiter is generated and rejected if it happens to
    appear verbatim in `value`, retrying up to `_MAX_DELIMITER_ATTEMPTS`
    times — a bound so a pathological value cannot spin forever. With 128
    bits of entropy per attempt a real collision is not expected to ever
    happen; the bound exists so the failure mode is a clear error instead of
    an infinite loop.
    """
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise RuntimeError("GITHUB_OUTPUT is not set")

    delimiter = _random_delimiter()
    attempts = 1
    while delimiter in value:
        if attempts >= _MAX_DELIMITER_ATTEMPTS:
            raise RuntimeError(
                f"could not find a collision-free delimiter for output {name!r} "
                f"after {_MAX_DELIMITER_ATTEMPTS} attempts"
            )
        delimiter = _random_delimiter()
        attempts += 1

    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
