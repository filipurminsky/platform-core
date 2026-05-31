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
"""

import asyncio
import collections
import os
import time

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://vllm-inference:8000")
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "60"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "120"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
PROXY_REQUESTS = Counter(
    "gateway_requests_total",
    "Requests proxied to vLLM",
    ["method", "path", "status"],
)
PROXY_LATENCY = Histogram(
    "gateway_request_duration_seconds",
    "End-to-end latency including vLLM processing",
    ["path"],
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
)
UPSTREAM_ERRORS = Counter(
    "gateway_upstream_errors_total",
    "Upstream connection or timeout errors",
)
RATE_LIMITED = Counter(
    "gateway_rate_limited_total",
    "Requests rejected by the rate limiter",
)


# ---------------------------------------------------------------------------
# Rate limiter — sliding-window per client IP (good enough for demo scale)
# ---------------------------------------------------------------------------
class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter (1-minute window)."""

    WINDOW = 60  # seconds

    def __init__(self, limit: int):
        self._limit = limit
        self._windows: dict[str, collections.deque] = {}
        self._lock = asyncio.Lock()

    async def is_allowed(self, client_ip: str) -> tuple[bool, int]:
        """Returns (allowed, remaining)."""
        now = time.time()
        async with self._lock:
            dq = self._windows.setdefault(client_ip, collections.deque())
            # Drop timestamps outside the window
            while dq and dq[0] < now - self.WINDOW:
                dq.popleft()
            if len(dq) >= self._limit:
                return False, 0
            dq.append(now)
            return True, self._limit - len(dq)

    async def reset_at(self, client_ip: str) -> int:
        """Epoch second when the oldest request in the window expires."""
        async with self._lock:
            dq = self._windows.get(client_ip)
            if dq:
                return int(dq[0] + self.WINDOW)
            return int(time.time() + self.WINDOW)


limiter = SlidingWindowRateLimiter(RATE_LIMIT_RPM)

# ---------------------------------------------------------------------------
# HTTP client (shared, connection-pooled)
# ---------------------------------------------------------------------------
http_client: httpx.AsyncClient | None = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="llm-gateway", version="0.1.0")


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
    """Readiness: probe the upstream /health endpoint."""
    try:
        resp = await http_client.get("/health", timeout=5.0)
        if resp.status_code == 200:
            return {"status": "ready", "upstream": "ok"}
        raise HTTPException(status_code=503, detail="upstream not ready")
    except httpx.RequestError as exc:
        log.warning("readyz_upstream_unreachable", error=str(exc))
        raise HTTPException(status_code=503, detail="upstream unreachable") from exc


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
