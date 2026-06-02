"""Prometheus metric definitions, exposed on GET /metrics."""

from prometheus_client import Counter, Histogram

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
