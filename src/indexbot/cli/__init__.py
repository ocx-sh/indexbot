"""`indexbot` CLI layer — argparse entrypoint plus per-subcommand modules.

Thin by design: subcommand modules parse args and call into `core/`, never
`adapters/`/`httpx` directly (ADR-4 BD-1, functional core / imperative
shell) — each subcommand module's `run(args, *, <ports>) -> ExitCode` takes
its ports as explicit keyword arguments. `cli/main.py` is the CLI's
argparse-building entrypoint; `cli/_wiring.py` (WP2-M) is the only module
under this package that constructs real `adapters/*` instances and binds
them into `main.py`'s subcommand dispatch table.
"""
