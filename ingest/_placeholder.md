# Ingest services

Edge/cloud collectors that pull ANPR reads, RFID scans, GPS telemetry, FASTag pings, weather, and live traffic, normalise them to the `jnpa_shared.schemas` models, and publish to Kafka / MQTT.

> Placeholder for a later stage of the JNPA UC-III PoC. The infrastructure
> skeleton, shared library, and bootstrap self-test in the repo root must be
> green (`make bootstrap-check` → `BOOTSTRAP OK`) before work starts here.
