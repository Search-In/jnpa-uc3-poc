"""rfid_ingest — RFID reader emulator, MQTT consumer, and ANPR correlator.

Three console entrypoints share this package (and one Docker image):

  * ``rfid-emulator``   — 25 logical UHF readers publishing reads over MQTT.
  * ``rfid-consumer``   — MQTT subscriber -> Timescale (core.rfid_read) + Kafka.
  * ``rfid-correlator`` — joins rfid.reads with anpr.reads -> vehicle.confirmed.
"""

__version__ = "0.1.0"
