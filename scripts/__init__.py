"""Host-side helper scripts for the JNPA UC-III PoC.

Making this a package lets the integration test + demo driver share one copy of
the preflight sanity checks (``from scripts.preflight import run``) and lets
``python -m scripts.preflight`` work from the repo root.
"""
