"""Prometheus metric definitions (contract §7), exposed on :9090/metrics."""

from prometheus_client import Counter, Histogram

STT_JOBS_TOTAL = Counter(
    "stt_jobs_total",
    "STT jobs processed by outcome",
    ["status"],  # success | error | dlq
)
STT_JOB_DURATION = Histogram(
    "stt_job_duration_seconds",
    "Time spent transcribing a single job",
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
)
STT_ERRORS_TOTAL = Counter(
    "stt_errors_total",
    "Total STT processing errors",
)
# Shared pipeline metric — incremented by whichever worker dead-letters (§7)
PIPELINE_JOBS_FAILED = Counter(
    "pipeline_jobs_failed_total",
    "Pipeline jobs that were dead-lettered",
    ["stage"],
)
