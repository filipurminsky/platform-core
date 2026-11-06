"""Prometheus metric definitions (contract §7), exposed on :9090/metrics."""

from platform_common.pipeline_metrics import PIPELINE_JOBS_FAILED, PIPELINE_QUEUE_WAIT
from prometheus_client import Counter, Histogram

__all__ = [
    "PIPELINE_JOBS_FAILED",
    "PIPELINE_QUEUE_WAIT",
    "STT_ERRORS_TOTAL",
    "STT_JOBS_TOTAL",
    "STT_JOB_DURATION",
    "STT_TRANSCRIPT_BYTES",
]

STT_JOBS_TOTAL = Counter(
    "stt_jobs_total",
    "STT jobs processed by outcome",
    ["status"],  # success | dlq
)
STT_JOB_DURATION = Histogram(
    "stt_job_duration_seconds",
    "Time spent transcribing a single job",
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
)
STT_ERRORS_TOTAL = Counter(
    "stt_errors_total",
    "Total STT processing errors (failed attempts)",
)
STT_TRANSCRIPT_BYTES = Histogram(
    "stt_transcript_bytes",
    "Size of the transcript written to object storage",
    buckets=[256, 1024, 4096, 16384, 65536, 262144, 1048576],
)
