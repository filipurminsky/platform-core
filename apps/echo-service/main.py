"""
echo-service — simple FastAPI demo that echoes requests and exposes Prometheus metrics.

Endpoints:
  GET /          → echo payload with request metadata
  GET /healthz   → liveness probe (always 200)
  GET /readyz    → readiness probe (always 200)
  GET /metrics   → Prometheus exposition format
"""

import os
import socket
import time

import structlog
from fastapi import FastAPI, Request, Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
def configure_tracing() -> None:
    # Honour the standard kill switch so tests/CI (and any env without a
    # collector) don't block on span export. Set OTEL_SDK_DISABLED=true there.
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    service_name = os.getenv("OTEL_SERVICE_NAME", "echo-service")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


configure_tracing()

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
REQUEST_COUNT = Counter(
    "echo_requests_total",
    "Total HTTP requests received",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "echo_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="echo-service", version="0.1.0")
FastAPIInstrumentor.instrument_app(app)
START_TIME = time.time()
HOSTNAME = socket.gethostname()


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start

    path = request.url.path
    REQUEST_COUNT.labels(method=request.method, path=path, status=response.status_code).inc()
    REQUEST_LATENCY.labels(method=request.method, path=path).observe(elapsed)

    log.info(
        "request",
        method=request.method,
        path=path,
        status=response.status_code,
        duration_ms=round(elapsed * 1000, 2),
    )
    return response


@app.get("/healthz")
async def healthz():
    """Liveness probe — always returns 200 while the process is alive."""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Readiness probe — always returns 200 (no external dependencies)."""
    return {"status": "ready"}


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/")
async def echo(request: Request):
    """Echo the request back with service metadata."""
    body = None
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = None

    return {
        "service": "echo-service",
        "version": os.getenv("APP_VERSION", "dev"),
        "hostname": HOSTNAME,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "request": {
            "method": request.method,
            "path": str(request.url.path),
            "query": str(request.url.query),
            "headers": dict(request.headers),
            "body": body,
        },
    }
