"""Trucking-app GPS telemetry simulator — JNPA UC-III Sub-Criterion 1D.

A 20,000-device (scalable to 30,000+) fleet simulator that drives realistic
truck telemetry along the NH-348 corridor into the four JNPA gates and back,
publishing each ping to MQTT and Kafka and batching writes to Timescale.

The reusable pieces live in this package; the FastAPI control plane that owns
the fleet is the top-level ``app`` module (entrypoint ``truck-sim``).
"""

__version__ = "0.1.0"
