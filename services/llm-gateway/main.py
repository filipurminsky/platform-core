"""
llm-gateway — thin reverse proxy that forwards OpenAI-compatible requests to vLLM.

Responsibilities:
  - Forward POST /v1/* to the upstream vLLM service
  - Add rate-limit headers (X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset)
  - Structured JSON logging of every request + upstream latency
  - Expose Prometheus metrics on GET /metrics
  - Liveness / readiness probes

Rate limiting: token-bucket per client IP, 60 req/min by default (env: RATE_LIMIT_RPM).
Upstream: env VLLM_BASE_URL (default http://vllm-inference:8000).

Config, logging/tracing, metrics, and the rate-limiter class live in the `app`
package; this module holds the FastAPI app, shared client + limiter state, and
the routes.
"""

import time

import httpx
from app.config import RATE_LIMIT_RPM, REQUEST_TIMEOUT, VLLM_BASE_URL
from app.metrics import PROXY_LATENCY, PROXY_REQUESTS, RATE_LIMITED, UPSTREAM_ERRORS
from app.observability import log
from app.rate_limiter import SlidingWindowRateLimiter
from fastapi import FastAPI, HTTPException, Request, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

# ---------------------------------------------------------------------------
# Shared, mutable state (overridable in tests via monkeypatch)
# ---------------------------------------------------------------------------
limiter = SlidingWindowRateLimiter(RATE_LIMIT_RPM)
http_client: httpx.AsyncClient | None = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="llm-gateway", version="0.1.0")
FastAPIInstrumentor.instrument_app(app)


@app.on_event("startup")
async def startup():
    global http_client
    http_client = httpx.AsyncClient(
        base_url=VLLM_BASE_URL,
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    log.info("gateway_started", upstream=VLLM_BASE_URL, rate_limit_rpm=RATE_LIMIT_RPM)


@app.on_event("shutdown")
async def shutdown():
    if http_client:
        await http_client.aclose()


# ---------------------------------------------------------------------------
# Probes and metrics
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Readiness: self-check only.
    We avoid probing upstream (vLLM) here because it may be scaled to zero;
    probing it would fail, keeping the gateway 'not ready' and preventing
    the very traffic that would trigger KEDA to scale the upstream back up.
    """
    if http_client is None:
        raise HTTPException(status_code=503, detail="http_client not initialized")
    return {"status": "ready"}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Proxy — catch-all for /v1/*
# ---------------------------------------------------------------------------
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "DELETE", "OPTIONS"])
async def proxy(path: str, request: Request):
    client_ip = request.client.host if request.client else "unknown"

    # Rate limiting
    allowed, remaining = await limiter.is_allowed(client_ip)
    reset_at = await limiter.reset_at(client_ip)
    rate_headers = {
        "X-RateLimit-Limit": str(RATE_LIMIT_RPM),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset_at),
    }

    if not allowed:
        RATE_LIMITED.inc()
        log.warning("rate_limited", client_ip=client_ip)
        return Response(
            content='{"error":"rate limit exceeded"}',
            status_code=429,
            media_type="application/json",
            headers=rate_headers,
        )

    # Forward the request
    upstream_path = f"/v1/{path}"
    if request.url.query:
        upstream_path += f"?{request.url.query}"

    body = await request.body()
    upstream_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")
    }

    start = time.perf_counter()
    try:
        upstream_resp = await http_client.request(
            method=request.method,
            url=upstream_path,
            headers=upstream_headers,
            content=body,
        )
    except httpx.TimeoutException as exc:
        UPSTREAM_ERRORS.inc()
        log.error("upstream_timeout", path=upstream_path, timeout=REQUEST_TIMEOUT)
        raise HTTPException(status_code=504, detail="upstream timeout") from exc
    except httpx.RequestError as exc:
        UPSTREAM_ERRORS.inc()
        log.error("upstream_error", path=upstream_path, error=str(exc))
        raise HTTPException(status_code=502, detail="upstream error") from exc

    elapsed = time.perf_counter() - start
    PROXY_REQUESTS.labels(
        method=request.method, path=f"/v1/{path}", status=upstream_resp.status_code
    ).inc()
    PROXY_LATENCY.labels(path=f"/v1/{path}").observe(elapsed)

    log.info(
        "proxied",
        method=request.method,
        path=upstream_path,
        status=upstream_resp.status_code,
        client_ip=client_ip,
        duration_ms=round(elapsed * 1000, 2),
    )

    # Pass through headers from vLLM (content-type, etc.) plus our rate-limit headers
    response_headers = dict(upstream_resp.headers)
    response_headers.update(rate_headers)
    # Remove transfer-encoding — httpx already decoded it
    response_headers.pop("transfer-encoding", None)

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )
