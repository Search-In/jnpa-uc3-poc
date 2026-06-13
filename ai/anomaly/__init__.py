"""ai.anomaly — behavioural anomaly detector (UC-III Sub-Criterion 2C).

Hybrid detector = ByteTrack (vehicle tracking) + a rule engine (wrong-way,
abandoned, illegal parking, route deviation) + a 1D-conv autoencoder over
per-track trajectory features (catches behaviours the rules can't enumerate).
Alerts are written to ``jnpa.alerts`` and published to the Kafka ``alerts``
topic, with the offending frame saved to MinIO as evidence.
"""

__version__ = "0.1.0"
