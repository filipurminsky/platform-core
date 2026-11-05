"""Structured JSON logging (structlog) + OpenTelemetry tracing.

``setup(service_name)`` is idempotent and returns ``(log, tracer)``. Tracing
honours ``OTEL_SDK_DISABLED=true`` so tests/CI and any environment without a
collector never block on span export.
"""

from __future__ import annotations

import logging
import os

import structlog
from opentelemetry import trace

_logging_configured = False
_tracing_configured = False

log = structlog.get_logger()


def add_trace_context(_logger, _method_name, event_dict):
    """structlog processor: stamp trace_id/span_id of the current span (Loki → Tempo link)."""
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = f"{context.trace_id:032x}"
        event_dict["span_id"] = f"{context.span_id:016x}"
    return event_dict


def configure_logging() -> None:
    global _logging_configured
    if _logging_configured:
        return
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            add_trace_context,
            structlog.processors.JSONRenderer(),
        ],
    )
    _logging_configured = True


def configure_tracing(service_name: str) -> None:
    global _tracing_configured
    if _tracing_configured or os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    # Imported lazily: the exporter pulls in grpc, which tests never need.
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", service_name)})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _tracing_configured = True


def setup(service_name: str):
    """Configure logging + tracing once; return ``(log, tracer)``."""
    configure_logging()
    configure_tracing(service_name)
    return log, trace.get_tracer(service_name)
