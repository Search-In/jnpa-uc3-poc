"""FASTag Step-5 live-validation kit (vendor-independent tooling).

Nothing here is imported by the running gateway. These are operator tools you run
ONCE the authorised FASTag provider is configured (FASTAG_ULIP_URL + credentials):

* ``live_validation.py``      — runnable harness exercising the REAL ULIP client
                                against the real provider (no mocks); captures
                                correlation-id / status / latency / retry count.
* ``CONTRACT_VERIFICATION.md``— field-by-field checklist to diff our DTO/DB
                                against the provider's API documentation.
* ``DEPLOYMENT_RUNBOOK.md``   — docker/EC2 build-deploy-verify-restart steps.
"""
