"""Enumerated governance-contract + threat-class security suite (spec X7).

Characterization + adversarial tests over the *already-implemented* index
bot. One named test per governance contract G-01..G-20 plus one per threat
class, mapping 1:1 to the contract ids so the suite doubles as the audit
artifact. These tests pass against current `src/indexbot` — they pin the
security posture, they do not drive new behavior.

Coverage source is `src/` only (`pyproject.toml [tool.coverage.run]
source = ["src"]`), so nothing under this package is measured — adding it
cannot move the 100% branch-coverage gate.
"""
