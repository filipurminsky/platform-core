"""
llm-gateway — thin reverse proxy that forwards OpenAI-compatible requests to vLLM.

Responsibilities:
  - Forward /v1/* to the upstream vLLM service; supports streaming (SSE) responses
  - Add rate-limit headers (X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset)
  - Structured JSON logging of every request + upstream latency
  - Expose Prometheus metrics on GET /metrics
  - Liveness / readiness probes

Rate limiting: sliding-window per client IP, 60 req/min by default (env: RATE_LIMIT_RPM).
Rate-limit identity uses the direct peer address. Forwarding headers are not trusted:
internal callers can set them too, so using them would make the limit trivially spoofable.
Upstream: env VLLM_BASE_URL (default http://vllm-inference:8000).

Config, logging/tracing, metrics, and the rate-limiter class live in the `app`
package; this module holds the FastAPI app, shared client + limiter state, and
the routes.
"""

import time
from contextlib import asynccontextmanager

import httpx
from app.config import RATE_LIMIT_RPM, REQUEST_TIMEOUT, VLLM_BASE_URL
from app.metrics import PROXY_LATENCY, PROXY_REQUESTS, RATE_LIMITED, UPSTREAM_ERRORS
from app.observability import log
from app.rate_limiter import SlidingWindowRateLimiter
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

# ---------------------------------------------------------------------------
# Shared, mutable state (overridable in tests via monkeypatch)
# ---------------------------------------------------------------------------
limiter = SlidingWindowRateLimiter(RATE_LIMIT_RPM)
http_client: httpx.AsyncClient | None = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        base_url=VLLM_BASE_URL,
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    limiter.start_cleanup()
    log.info("gateway_started", upstream=VLLM_BASE_URL, rate_limit_rpm=RATE_LIMIT_RPM)
    yield
    if http_client:
        await http_client.aclose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="llm-gateway", version="0.1.0", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)


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
# Proxy helpers
# ---------------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    """Return the non-spoofable direct peer address.

    Kubernetes NetworkPolicy also permits llm-worker to call this service
    directly, so X-Real-IP/X-Forwarded-For cannot be trusted as ingress-only.
    """
    if request.client and request.client.host:
        return request.client.host

    return "unknown"


def _normalize_path(path: str) -> str:
    """Return a fixed route label; never expose user-controlled path segments."""
    if path == "/":
        return "/"
    if path.startswith("/v1/"):
        return "/v1/*"
    return "unmatched"


async def _stream_body(upstream_resp):
    """Yield upstream bytes and always release the pooled connection."""
    try:
        async for chunk in upstream_resp.aiter_bytes():
            yield chunk
    finally:
        await upstream_resp.aclose()


# ---------------------------------------------------------------------------
# Proxy — catch-all for /v1/*
# ---------------------------------------------------------------------------
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "DELETE", "OPTIONS"])
async def proxy(path: str, request: Request):
    client_ip = _client_ip(request)

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
    norm_path = _normalize_path(f"/v1/{path}")

    start = time.perf_counter()
    try:
        # Use streaming so SSE (server-sent events) responses from vLLM flow through
        # to the client without buffering the full completion in memory.
        req = http_client.build_request(
            method=request.method,
            url=upstream_path,
            headers=upstream_headers,
            content=body,
        )
        upstream_resp = await http_client.send(req, stream=True)
    except httpx.TimeoutException as exc:
        elapsed = time.perf_counter() - start
        UPSTREAM_ERRORS.inc()
        PROXY_REQUESTS.labels(method=request.method, path=norm_path, status=504).inc()
        PROXY_LATENCY.labels(path=norm_path).observe(elapsed)
        log.error("upstream_timeout", path=upstream_path, timeout=REQUEST_TIMEOUT)
        raise HTTPException(status_code=504, detail="upstream timeout") from exc
    except httpx.RequestError as exc:
        elapsed = time.perf_counter() - start
        UPSTREAM_ERRORS.inc()
        PROXY_REQUESTS.labels(method=request.method, path=norm_path, status=502).inc()
        PROXY_LATENCY.labels(path=norm_path).observe(elapsed)
        log.error("upstream_error", path=upstream_path, error=str(exc))
        raise HTTPException(status_code=502, detail="upstream error") from exc

    elapsed = time.perf_counter() - start
    PROXY_REQUESTS.labels(
        method=request.method, path=norm_path, status=upstream_resp.status_code
    ).inc()
    PROXY_LATENCY.labels(path=norm_path).observe(elapsed)

    log.info(
        "proxied",
        method=request.method,
        path=upstream_path,
        status=upstream_resp.status_code,
        client_ip=client_ip,
        duration_ms=round(elapsed * 1000, 2),
    )

    # Pass through upstream headers plus our rate-limit headers.
    # Strip hop-by-hop and encoding headers that httpx has already handled so
    # the client does not receive stale/incorrect framing metadata.
    response_headers = dict(upstream_resp.headers)
    response_headers.update(rate_headers)
    for hop in ("transfer-encoding", "content-encoding", "content-length"):
        response_headers.pop(hop, None)

    return StreamingResponse(
        _stream_body(upstream_resp),
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )
