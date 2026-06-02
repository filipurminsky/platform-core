"""Structured logging + OpenTelemetry tracing.

Configured at import time. Tracing honours OTEL_SDK_DISABLED=true so tests/CI
and any env without a collector don't block on span export.
"""

import os

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

DEFAULT_SERVICE_NAME = "echo-service"


def add_trace_context(_logger, _method_name, event_dict):
    span = trace.get_current_span()
    context = span.get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = f"{context.trace_id:032x}"
        event_dict["span_id"] = f"{context.span_id:016x}"
    return event_dict


structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        add_trace_context,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()


def configure_tracing() -> None:
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    service_name = os.getenv("OTEL_SERVICE_NAME", DEFAULT_SERVICE_NAME)
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


configure_tracing()
