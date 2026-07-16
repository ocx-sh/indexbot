# indexbot

Write path for the OCX public index. Subcommands (`announce`, `reconcile`,
`validate`, `render`, `seed-import`) land in Phase 2 — today this is scaffold
only: exit codes, error hierarchy, data model, port protocols, CLI plumbing.

## Layout

- `src/indexbot/core/` — pure logic (I/O-free; Phase 2)
- `src/indexbot/adapters/` — the only place `httpx` is imported (Phase 2)
- `src/indexbot/cli/` — argparse entrypoint + subcommands
- `tests/fakes/` — in-memory `Protocol` implementations for Phase 2 tests

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
