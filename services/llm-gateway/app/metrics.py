"""Prometheus metric definitions, exposed on GET /metrics."""

from prometheus_client import Counter, Histogram

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
    "Upstream connection, timeout, or protocol errors (also feeds KEDA 0->1 activation)",
)
RATE_LIMITED = Counter(
    "gateway_rate_limited_total",
    "Requests rejected by the rate limiter",
)
RATE_LIMITER_ERRORS = Counter(
    "gateway_rate_limiter_errors_total",
    "Rate-limiter backend (Redis) errors; the limiter fails open on these",
)
AUTH_FAILURES = Counter(
    "gateway_auth_failures_total",
    "Requests to /v1/* rejected for a missing or invalid API key",
    ["reason"],  # missing | invalid
)
REQUESTS_TOO_LARGE = Counter(
    "gateway_request_too_large_total",
    "Requests rejected with 413 because the body exceeded MAX_REQUEST_BYTES",
)
