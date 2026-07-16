"""`indexbot` CLI layer — argparse entrypoint plus per-subcommand modules.

Thin by design: subcommand modules parse args and call into `core/`/
`adapters/`, they never contain the logic itself (ADR-4 BD-1, functional
core / imperative shell).
"""
