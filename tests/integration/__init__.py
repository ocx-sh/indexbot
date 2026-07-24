"""Socket-level integration harness for `indexbot` (Track B safety net).

Drives the REAL `adapters/ghcr.py` and `adapters/github_api.py` over real TCP
sockets against stdlib `http.server` fakes, rather than substituting the port
objects (`tests/fakes`' in-memory tier). See `harness/` for the servers and
`fixtures/` for canonical seed data.
"""
