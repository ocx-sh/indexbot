"""`.github/maintainers.yml` parsing (fork-PR announce revamp, G-20).

Reviewer identity has the exact same two-field shape (`github`, `github_id`)
`model.Owner` already carries (`PackageRoot.owners`) — reused rather than a
second near-identical dataclass (DRY).

`maintainers.yml`'s shape is fixed and entirely under this repo's own
control (never PR-submitted, never attacker-influenced): a top-level
`maintainers:` key followed by a flat list of two-field mappings, e.g.

```yaml
maintainers:
  - github: michael-herwig
    github_id: 3511590
```

`bot/pyproject.toml` declares no YAML dependency (`httpx` is the only
runtime dep, ADR-4 BD-1's minimal-footprint driver — every dependency is
audit surface for a credential-holding process, `quality-python.md`'s "CI
Bots" guidance) and this fixed, narrow shape doesn't justify adding one
(`cli/seed_import.py`'s `_parse_mirror_yml` sets the same precedent for
`mirror.yml`). A deliberately tiny, strict line-based parser instead —
upgrade to `pyyaml`/`ruamel.yaml` (a separate reviewed `pyproject.toml` PR)
if this file's shape ever needs to grow beyond a flat list of two-field
mappings.
"""

from __future__ import annotations

import re
from typing import Final

from indexbot.errors import ValidationError
from indexbot.model import Owner

_TOP_KEY: Final[str] = "maintainers:"
_ITEM_RE: Final[re.Pattern[str]] = re.compile(r"^-\s+github:\s*(\S+)$")
_ID_RE: Final[re.Pattern[str]] = re.compile(r"^github_id:\s*(\d+)$")


def parse_maintainers(raw: bytes) -> tuple[Owner, ...]:
    """Parse `maintainers.yml` bytes into `(Owner, ...)`.

    Raises `ValidationError` on any structurally malformed input: no
    top-level `maintainers:` key, an odd (unpaired) entry, or a
    `- github: <login>` line not immediately followed by its
    `github_id: <int>` line. Blank lines and `#`-prefixed comment lines are
    skipped; every other line's leading/trailing whitespace is stripped
    before matching (this parser only ever sees this repo's own committed
    file, never PR-submitted content).
    """
    lines = [
        line.strip()
        for line in raw.decode("utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines or lines[0] != _TOP_KEY:
        raise ValidationError("maintainers.yml must start with a top-level 'maintainers:' key")

    body = lines[1:]
    if len(body) % 2 != 0:
        raise ValidationError("maintainers.yml has a malformed maintainer entry")

    maintainers: list[Owner] = []
    for index in range(0, len(body), 2):
        github_line, id_line = body[index], body[index + 1]
        github_match = _ITEM_RE.fullmatch(github_line)
        id_match = _ID_RE.fullmatch(id_line)
        if github_match is None or id_match is None:
            raise ValidationError(f"maintainers.yml entry {index // 2} is malformed")
        maintainers.append(Owner(github=github_match.group(1), github_id=int(id_match.group(1))))
    return tuple(maintainers)
