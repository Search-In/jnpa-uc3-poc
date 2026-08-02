# Ingest services

Edge/cloud collectors that pull ANPR reads, RFID scans, GPS telemetry, FASTag
pings, weather, live traffic, and **gate-document OCR**, normalise them to the
`jnpa_shared.schemas` models (or structured OCR fields), and publish to Kafka /
MQTT / HTTP.

| Package | Role |
| --- | --- |
| `anpr/` | Clip replay → YOLO → Kafka `anpr.reads` |
| `rfid/` | Emulator / consumer / correlator |
| `vahan_sim/` · `vahan_live/` | Parivahan / Surepass KYC |
| `trucking_app/` | 20k-device GPS fleet sim |
| `eir_ocr/` | EIR gate-slip OCR (Tesseract) → structured fields (`:8210`) |
