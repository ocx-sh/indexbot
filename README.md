# indexbot

Write path for the OCX public index. Subcommands `announce`, `reconcile`,
`validate`, `render`, `seed-import` are all implemented and wired
(`cli/_wiring.py`); the CLI ships as the `indexbot` console script
(`uv run indexbot <subcommand>`).

## Layout

- `src/indexbot/core/` — pure logic (I/O-free)
- `src/indexbot/adapters/` — the only place `httpx` is imported
- `src/indexbot/cli/` — argparse entrypoint + subcommands
- `tests/fakes/` — in-memory `Protocol` implementations used across tests

## Deployment policy

`.github/index-policy.json` (repo root, not `bot/`) is this index's
registry-host allowlist — G-03's input, one file per index copy:

```json
{ "registry_hosts": ["ghcr.io"] }
```

A committed file rather than an environment/Actions variable, so widening
registry trust stays a reviewed PR. A host no `RegistryPort` adapter can
serve is refused at wiring time. Details: `CONTRACTS.md` §15.

## Commands

```
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run bandit -c pyproject.toml -r src
uv run pytest --cov --cov-branch --cov-report=term-missing
uv run pip-audit
```

Architecture and security posture:
`.claude/artifacts/adr_index_bot_and_workflow_security.md`.
