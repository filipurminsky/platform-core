"""
llm-gateway — thin reverse proxy that forwards OpenAI-compatible requests to vLLM.

Responsibilities:
  - Forward /v1/* to the upstream vLLM service; supports streaming (SSE) responses
  - Add rate-limit headers (X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset)
  - Structured JSON logging of every request + upstream latency
  - Expose Prometheus metrics on GET /metrics
  - Liveness / readiness probes

Auth: bearer API keys on /v1/* (env GATEWAY_API_KEYS, CSV). Empty disables auth
(dev only — startup is refused when ENVIRONMENT=prod/production). Probes and
/metrics stay unauthenticated.

Rate limiting: sliding window, 60 req/min by default (env RATE_LIMIT_RPM), keyed
by the authenticated key's hashed id when auth is on, else by client IP.
X-Forwarded-For is only honoured when the direct peer is inside
TRUSTED_PROXY_CIDRS (right-most untrusted hop wins), so callers can't spoof it.

Upstream: env VLLM_BASE_URL (default http://vllm-inference:8000). Connect
failures (vLLM scaled to zero) return 503 + Retry-After; timeouts return 504.

Config, logging/tracing, metrics, and the rate-limiter class live in the `app`
package; this module holds the FastAPI app, shared client + limiter state, and
the routes.
"""

import json
import time
from contextlib import asynccontextmanager

import httpx
from app.config import (
    API_KEYS,
    MAX_REQUEST_BYTES,
    RATE_LIMIT_RPM,
    REDIS_PASSWORD,
    REDIS_URL,
    REQUEST_TIMEOUT,
    TRUSTED_PROXY_CIDRS,
    UPSTREAM_UNAVAILABLE_RETRY_AFTER,
    VLLM_BASE_URL,
)
from app.identity import bearer_token, client_ip, key_id, match_api_key
from app.metrics import (
    AUTH_FAILURES,
    PROXY_LATENCY,
    PROXY_REQUESTS,
    RATE_LIMITED,
    REQUESTS_TOO_LARGE,
    UPSTREAM_ERRORS,
)
from app.observability import log
from app.rate_limiter import RedisSlidingWindowRateLimiter, SlidingWindowRateLimiter
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis as AsyncRedis

# ---------------------------------------------------------------------------
# Shared, mutable state (overridable in tests via monkeypatch)
# ---------------------------------------------------------------------------
# Default to the in-process limiter (correct for single-replica dev and what the
# unit tests construct). When REDIS_URL is set (prod, 2 replicas), lifespan swaps
# in the Redis-backed limiter so the per-client limit is enforced across all pods.
limiter = SlidingWindowRateLimiter(RATE_LIMIT_RPM)
http_client: httpx.AsyncClient | None = None
redis_client: AsyncRedis | None = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    global http_client, limiter, redis_client
    http_client = httpx.AsyncClient(
        base_url=VLLM_BASE_URL,
        timeout=httpx.Timeout(REQUEST_TIMEOUT),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    if REDIS_URL:
        redis_client = AsyncRedis.from_url(
            REDIS_URL, password=REDIS_PASSWORD or None, decode_responses=True
        )
        limiter = RedisSlidingWindowRateLimiter(RATE_LIMIT_RPM, redis_client)
        backend = "redis"
    else:
        limiter.start_cleanup()
        backend = "in-memory"
    log.info(
        "gateway_started",
        upstream=VLLM_BASE_URL,
        rate_limit_rpm=RATE_LIMIT_RPM,
        rate_limiter_backend=backend,
        auth_enabled=bool(API_KEYS),
        api_key_count=len(API_KEYS),
        trusted_proxy_cidrs=[str(c) for c in TRUSTED_PROXY_CIDRS],
        max_request_bytes=MAX_REQUEST_BYTES,
    )
    yield
    if http_client:
        await http_client.aclose()
    if redis_client is not None:
        await redis_client.aclose()


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
# Hop-by-hop headers (RFC 9110 §7.6.1) are connection-scoped and must not be
# forwarded; host/content-length are recomputed by httpx. The client's
# credentials (authorization = the gateway API key, cookie) are for the gateway
# only and are never passed to vLLM.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_STRIP_REQUEST = _HOP_BY_HOP | {"host", "content-length", "authorization", "cookie"}
# httpx has already de-chunked and decoded the upstream body, so its framing /
# encoding headers would be stale.
_STRIP_RESPONSE = _HOP_BY_HOP | {"content-encoding", "content-length", "set-cookie"}


def _connection_tokens(headers) -> set[str]:
    """Header names listed in Connection are hop-by-hop too (RFC 9110 §7.6.1)."""
    getlist = getattr(headers, "getlist", None) or getattr(headers, "get_list", None)
    values = getlist("connection") if getlist else [headers.get("connection", "")]
    return {token.strip().lower() for value in values for token in value.split(",")}


def _filter_headers(headers, strip: frozenset[str] | set[str]) -> dict[str, str]:
    drop = strip | _connection_tokens(headers)
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in drop and not k.lower().startswith("proxy-")
    }


def _client_ip(request: Request) -> str:
    """Proxy-aware client IP (X-Forwarded-For honoured only from trusted proxies)."""
    return client_ip(request, TRUSTED_PROXY_CIDRS)


def _json_error(status: int, message: str, headers: dict[str, str] | None = None) -> Response:
    return Response(
        content=json.dumps({"error": message}),
        status_code=status,
        media_type="application/json",
        headers=headers,
    )


def _normalize_path(path: str) -> str:
    """Return a fixed route label; never expose user-controlled path segments."""
    if path == "/":
        return "/"
    if path.startswith("/v1/"):
        return "/v1/*"
    return "unmatched"


class _BodyTooLarge(Exception):
    pass


async def _read_body_capped(request: Request, limit: int) -> bytes:
    """Read the request body, aborting as soon as it exceeds `limit` bytes.

    Content-Length is checked up front, but it can be absent (chunked) or lie,
    so the cap is also enforced on the bytes actually received.
    """
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise _BodyTooLarge
    return bytes(buf)


async def _stream_body(upstream_resp):
    """Yield upstream bytes and always release the pooled connection."""
    try:
        async for chunk in upstream_resp.aiter_bytes():
            yield chunk
    finally:
        await upstream_resp.aclose()


def _record(method: str, norm_path: str, status: int, elapsed: float) -> None:
    PROXY_REQUESTS.labels(method=method, path=norm_path, status=status).inc()
    PROXY_LATENCY.labels(path=norm_path).observe(elapsed)


# ---------------------------------------------------------------------------
# Proxy — catch-all for /v1/*
# ---------------------------------------------------------------------------
@app.api_route("/v1/{path:path}", methods=["GET", "POST", "DELETE", "OPTIONS"])
async def proxy(path: str, request: Request):
    ip = _client_ip(request)

    # --- Authentication (before anything that costs resources) -------------
    if API_KEYS:
        token = bearer_token(request.headers.get("authorization"))
        matched = match_api_key(token, API_KEYS) if token is not None else None
        if matched is None:
            reason = "missing" if token is None else "invalid"
            AUTH_FAILURES.labels(reason=reason).inc()
            log.warning("auth_failed", reason=reason, client_ip=ip)
            return _json_error(
                401,
                "missing bearer token" if token is None else "invalid api key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        caller = key_id(matched)
        identity = f"key:{caller}"
    else:
        caller = None
        identity = f"ip:{ip}"

    # --- Cheap size check before spending a rate-limit slot ----------------
    raw_cl = request.headers.get("content-length")
    if raw_cl is not None:
        try:
            declared = int(raw_cl)
        except ValueError:
            return _json_error(400, "invalid content-length")
        if declared > MAX_REQUEST_BYTES:
            REQUESTS_TOO_LARGE.inc()
            return _json_error(413, f"request body exceeds {MAX_REQUEST_BYTES} bytes")

    # --- Rate limiting -----------------------------------------------------
    allowed, remaining, reset_at = await limiter.is_allowed(identity)
    rate_headers = {
        "X-RateLimit-Limit": str(RATE_LIMIT_RPM),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset_at),
    }

    if not allowed:
        RATE_LIMITED.inc()
        log.warning("rate_limited", client_ip=ip, key_id=caller)
        retry_after = max(1, reset_at - int(time.time()))
        return _json_error(
            429, "rate limit exceeded", headers={**rate_headers, "Retry-After": str(retry_after)}
        )

    try:
        body = await _read_body_capped(request, MAX_REQUEST_BYTES)
    except _BodyTooLarge:
        REQUESTS_TOO_LARGE.inc()
        return _json_error(413, f"request body exceeds {MAX_REQUEST_BYTES} bytes")

    # --- Forward -----------------------------------------------------------
    upstream_path = f"/v1/{path}"
    if request.url.query:
        upstream_path += f"?{request.url.query}"
    upstream_headers = _filter_headers(request.headers, _STRIP_REQUEST)
    log_path = f"/v1/{path}"  # no query string: it may carry user data
    norm_path = _normalize_path(log_path)

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
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # No connection could be established: vLLM is scaled to zero (Service has
        # no endpoints → refused) or a pod is still starting. Transient by nature,
        # so 503 + Retry-After tells callers to come back once KEDA has scaled up.
        # Still counted as an upstream error: KEDA's 0→1 activation query sums
        # rate(gateway_requests_total) + rate(gateway_upstream_errors_total).
        UPSTREAM_ERRORS.inc()
        _record(request.method, norm_path, 503, time.perf_counter() - start)
        log.warning("upstream_unavailable", path=log_path, error=type(exc).__name__)
        return _json_error(
            503,
            "upstream unavailable",
            headers={**rate_headers, "Retry-After": str(UPSTREAM_UNAVAILABLE_RETRY_AFTER)},
        )
    except httpx.TimeoutException:
        UPSTREAM_ERRORS.inc()
        _record(request.method, norm_path, 504, time.perf_counter() - start)
        log.error("upstream_timeout", path=log_path, timeout=REQUEST_TIMEOUT)
        return _json_error(504, "upstream timeout", headers=rate_headers)
    except httpx.RequestError as exc:
        UPSTREAM_ERRORS.inc()
        _record(request.method, norm_path, 502, time.perf_counter() - start)
        log.error("upstream_error", path=log_path, error=str(exc))
        return _json_error(502, "upstream error", headers=rate_headers)

    elapsed = time.perf_counter() - start
    _record(request.method, norm_path, upstream_resp.status_code, elapsed)

    log.info(
        "proxied",
        method=request.method,
        path=log_path,
        status=upstream_resp.status_code,
        client_ip=ip,
        key_id=caller,
        duration_ms=round(elapsed * 1000, 2),
    )

    # Pass through upstream headers (minus hop-by-hop / stale framing) plus our
    # rate-limit headers.
    response_headers = _filter_headers(upstream_resp.headers, _STRIP_RESPONSE)
    response_headers.update(rate_headers)

    return StreamingResponse(
        _stream_body(upstream_resp),
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )
