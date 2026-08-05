"""Top-level external-API integration packages for the JNPA UC-III PoC.

Importable as ``integrations.<name>`` with the repo root on ``PYTHONPATH`` (the
same model ``services.<name>`` relies on: compose bind-mounts the repo and sets
``PYTHONPATH=/app``). Each sub-package owns the HTTP client, response schemas
and typed exceptions for ONE external provider — services never speak raw HTTP
to a vendor themselves.
"""
