"""OpenTelemetry tracing for the JNPA UC-III PoC (cross-service propagation).

One helper every service calls at startup so the evaluator can open Jaeger and
see the full causal chain a what-if scenario fires:

    ingest (truck.telemetry / cargo.dpd_release)
      -> AI (congestion /predict, anomaly engine)
        -> alert (core.alert + Kafka "alerts")
          -> action (gate close, reroute, e-Challan, TAS reschedule)

Design
------
* ``init_tracing(service_name)`` configures a global TracerProvider exporting
  OTLP/gRPC to the collector at ``OTEL_EXPORTER_OTLP_ENDPOINT`` (Jaeger 1.59 has
  a native OTLP receiver on :4317). Idempotent; safe to call once per process.
* ``instrument_fastapi(app)`` adds the ASGI middleware so every HTTP request is
  a span and incoming ``traceparent`` headers continue the caller's trace.
* Kafka has no auto-instrumentation that fits our thin confluent-kafka helpers,
  so we propagate context *manually*: the producer injects ``traceparent`` into
  the message headers (``inject_context``) and the consumer extracts it
  (``extract_context``) to parent the handling span. ``kafka_io`` calls these.
* EVERYTHING is best-effort. If the otel packages are not installed (host test
  venv, or a trimmed image) every function degrades to a no-op and returns a
  dummy context manager, so importing this module never breaks a service.

Enable/disable with ``OTEL_SDK_DISABLED=true`` (the SDK's own switch) or simply
by not setting ``OTEL_EXPORTER_OTLP_ENDPOINT`` (then we use a no-op exporter).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from .logging import get_logger

log = get_logger("jnpa_shared.tracing")

_INITIALISED = False
_ENABLED = False


def _otel_available() -> bool:
    try:
        import opentelemetry.trace  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def init_tracing(service_name: str) -> bool:
    """Configure the global tracer provider for this service.

    Returns True if tracing is live (SDK present + endpoint configured), else
    False (the service still runs; spans become no-ops). Idempotent.
    """
    global _INITIALISED, _ENABLED
    if _INITIALISED:
        return _ENABLED
    _INITIALISED = True

    if os.environ.get("OTEL_SDK_DISABLED", "").lower() in {"1", "true", "yes"}:
        log.info("tracing_disabled", reason="OTEL_SDK_DISABLED")
        return False
    if not _otel_available():
        log.info("tracing_inactive", reason="opentelemetry_not_installed")
        return False

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        log.info("tracing_inactive", reason="no_OTEL_EXPORTER_OTLP_ENDPOINT")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        resource = Resource.create(
            {
                "service.name": service_name,
                "service.namespace": "jnpa-uc3",
                "deployment.environment": os.environ.get("JNPA_ENV", "poc"),
            }
        )
        provider = TracerProvider(resource=resource)
        # endpoint may include scheme (http://jaeger:4317); gRPC exporter wants
        # host:port with insecure for the PoC.
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _ENABLED = True
        log.info("tracing_initialised", service=service_name, endpoint=endpoint)
    except Exception as exc:  # noqa: BLE001
        log.warning("tracing_init_failed", error=str(exc))
        _ENABLED = False
    return _ENABLED


def instrument_fastapi(app: Any) -> None:
    """Add the FastAPI ASGI instrumentation (incoming-context propagation)."""
    if not _ENABLED:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception as exc:  # noqa: BLE001
        log.debug("fastapi_instrument_failed", error=str(exc))


def instrument_httpx() -> None:
    """Auto-instrument outbound httpx so child HTTP calls continue the trace."""
    if not _ENABLED:
        return
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        log.debug("httpx_instrument_failed", error=str(exc))


def get_tracer(name: str = "jnpa"):
    """Return a tracer, or a no-op tracer if tracing is inactive."""
    if not _ENABLED:
        return _NoopTracer()
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except Exception:  # noqa: BLE001
        return _NoopTracer()


@contextmanager
def span(name: str, attributes: Optional[Dict[str, Any]] = None) -> Iterator[Any]:
    """Start a span as a context manager (no-op when tracing is inactive)."""
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as sp:
        if attributes and hasattr(sp, "set_attribute"):
            for k, v in attributes.items():
                try:
                    sp.set_attribute(k, v)
                except Exception:  # noqa: BLE001
                    pass
        yield sp


# --------------------------------------------------------------------------- Kafka propagation
def inject_context(headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Return Kafka message headers carrying the current trace context.

    Merges into ``headers`` (W3C ``traceparent`` / ``tracestate``). When tracing
    is inactive this returns ``headers`` unchanged.
    """
    headers = dict(headers or {})
    if not _ENABLED:
        return headers
    try:
        from opentelemetry.propagate import inject

        inject(headers)
    except Exception:  # noqa: BLE001
        pass
    return headers


@contextmanager
def extract_context(headers: Optional[Dict[str, str]], name: str,
                    attributes: Optional[Dict[str, Any]] = None) -> Iterator[Any]:
    """Start a span parented to the trace context carried in ``headers``.

    Used by Kafka consumers so the handling span continues the producer's trace.
    No-op span when tracing is inactive.
    """
    if not _ENABLED:
        with span(name, attributes) as sp:
            yield sp
        return
    try:
        from opentelemetry import trace
        from opentelemetry.propagate import extract

        ctx = extract(headers or {})
        tracer = trace.get_tracer("jnpa")
        with tracer.start_as_current_span(name, context=ctx) as sp:
            if attributes:
                for k, v in attributes.items():
                    try:
                        sp.set_attribute(k, v)
                    except Exception:  # noqa: BLE001
                        pass
            yield sp
    except Exception:  # noqa: BLE001
        with span(name, attributes) as sp:
            yield sp


def current_traceparent() -> Optional[str]:
    """The W3C ``traceparent`` of the active span, or None when inactive.

    Lets non-OTel surfaces (a Scenario step row, a WS frame) record the trace id
    so the dashboard / DB can deep-link to Jaeger.
    """
    if not _ENABLED:
        return None
    try:
        carrier: Dict[str, str] = {}
        from opentelemetry.propagate import inject

        inject(carrier)
        return carrier.get("traceparent")
    except Exception:  # noqa: BLE001
        return None


class _NoopSpan:
    def set_attribute(self, *_a, **_k):
        return None

    def record_exception(self, *_a, **_k):
        return None

    def set_status(self, *_a, **_k):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _NoopTracer:
    @contextmanager
    def start_as_current_span(self, _name, *_a, **_k):
        yield _NoopSpan()


__all__ = [
    "init_tracing",
    "instrument_fastapi",
    "instrument_httpx",
    "get_tracer",
    "span",
    "inject_context",
    "extract_context",
    "current_traceparent",
]
