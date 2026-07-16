"""CLI-layer plumbing: untrusted env-var reads and the `GITHUB_OUTPUT` writer.

Both are pure I/O-boundary helpers — no subcommand business logic here (that
is `cli/<subcommand>.py`, Phase 2). `read_validated_env` is the shape every
subcommand uses to pull a `repository_dispatch`-derived value out of the
environment (ADR-4 BD-4's env-var-indirection discipline); the actual
regexes (`PACKAGE_ID_RE`, `OCI_REPOSITORY_RE`) are `core/validate_payload.py`
and `core/validate_entry.py`'s contract, not this module's — callers pass
`pattern` in.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from indexbot.errors import ValidationError

_MAX_DELIMITER_ATTEMPTS = 5


def read_validated_env(name: str, *, pattern: re.Pattern[str], max_length: int) -> str:
    """Read and validate `name` from the environment.

    Enforces ADR-4 BD-4's length-cap-then-fullmatch discipline: reject on
    length *before* any regex evaluation, so worst-case regex work is
    bounded regardless of what the value contains, then match with
    `fullmatch` only — never `match`/`search`, which would silently accept a
    valid prefix followed by injected garbage.

    Raises `ValidationError` if the variable is unset, empty, over
    `max_length`, or does not fullmatch `pattern`.
    """
    value = os.environ.get(name)
    if not value:
        raise ValidationError(f"{name} is not set")
    if len(value) > max_length:
        raise ValidationError(f"{name} exceeds max length {max_length} characters")
    if pattern.fullmatch(value) is None:
        raise ValidationError(f"{name} does not match the expected format")
    return value


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
