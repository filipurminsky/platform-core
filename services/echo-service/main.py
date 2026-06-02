"""
echo-service — simple FastAPI demo that echoes requests and exposes Prometheus metrics.

Endpoints:
  GET /          → echo payload with request metadata
  GET /healthz   → liveness probe (always 200)
  GET /readyz    → readiness probe (always 200)
  GET /metrics   → Prometheus exposition format

Logging/tracing setup and metric definitions live in the `app` package; this
module holds the FastAPI app, request-metrics middleware, and the routes.
"""

import socket
import time

from app.config import APP_VERSION
from app.metrics import REQUEST_COUNT, REQUEST_LATENCY
from app.observability import log
from fastapi import FastAPI, Request, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

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
        "version": APP_VERSION,
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
