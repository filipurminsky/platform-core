"""Prometheus metric definitions, exposed on :9090/metrics."""

from prometheus_client import Counter, Gauge, Histogram

JOBS_PROCESSED = Counter(
    "worker_jobs_processed_total",
    "Jobs processed by outcome",
    ["status"],  # success | error | dlq
)
JOB_DURATION = Histogram(
    "worker_job_duration_seconds",
    "Time spent processing a single job",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)
CONSUMER_LAG = Gauge(
    "worker_consumer_lag",
    "Estimated consumer lag (messages behind)",
    ["partition"],
)
