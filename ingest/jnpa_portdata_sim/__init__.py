"""Contract-faithful simulator of the JNPA Simulated Port-Data API v2.0.

Serves the SAME surface as https://dt.jnpa.in/poc-api-data-access (token,
groups, records, files) seeded from the real sample-data-pack folders, and —
deliberately — reproduces the catalogued defects of the live service
(docs/JNPA_API_DEFECTS.md) so the client and sync layer are tested against
reality, not against an idealised spec. The live endpoint sits behind a port
filter as of 04-Aug-2026; this sim is what makes the whole integration
verifiable offline and plug-and-play when the filter lifts.
"""
