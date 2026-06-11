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
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        # Starlette attaches the matched route after routing. Use its template,
        # never the raw URL, so arbitrary path scans cannot create new series.
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        elapsed = time.perf_counter() - start
        REQUEST_COUNT.labels(method=request.method, path=path, status=status).inc()
        REQUEST_LATENCY.labels(method=request.method, path=path).observe(elapsed)
        log.info(
            "request",
            method=request.method,
            path=path,
            status=status,
            duration_ms=round(elapsed * 1000, 2),
        )


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

    # Strip sensitive headers before echoing — Authorization and Cookie would expose
    # credentials to whoever reads the response (logs, browser, other services).
    safe_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("authorization", "cookie", "set-cookie")
    }
    return {
        "service": "echo-service",
        "version": APP_VERSION,
        "hostname": HOSTNAME,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "request": {
            "method": request.method,
            "path": str(request.url.path),
            "query": str(request.url.query),
            "headers": safe_headers,
            "body": body,
        },
    }
