"""`indexbot` I/O adapters — the imperative shell.

Each module implements exactly one `ports.py` `Protocol`. `httpx`, filesystem
calls, and wall-clock reads live only here — `core/` never imports from this
package (ADR-4 BD-1, functional core / imperative shell).
"""
