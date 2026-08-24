"""`indexbot` functional core — pure computation, no I/O.

Every module here is deterministic given its explicit inputs, including any
injected `ports.py` `Protocol` argument (`core/observe.py`'s `registry:
RegistryPort`, for example): no direct `httpx`/filesystem/`time.time()` call
inside a `core/` module's own body, ever — only through an injected port
(ADR-4 BD-1, functional core / imperative shell; see `CONTRACTS.md` §0).
"""
