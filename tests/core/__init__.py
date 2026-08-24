"""Marker package — disambiguates `tests/core/test_render.py` from
`tests/cli/test_render.py` under pytest's default (non-`--import-mode=importlib`)
collection, which requires unique dotted module names, not just unique paths.
"""
