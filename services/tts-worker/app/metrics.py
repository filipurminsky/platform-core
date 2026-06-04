"""Prometheus metric definitions (contract §7 — exact names).

tts-worker is the terminal pipeline stage, so it owns the end-to-end
completion + duration metrics in addition to its per-stage ones.
"""

from prometheus_client import Counter, Histogram

TTS_JOBS = Counter(
    "tts_jobs_total",
    "TTS jobs processed by outcome",
    ["status"],  # success | error | dlq
)
TTS_GENERATION_DURATION = Histogram(
    "tts_generation_duration_seconds",
    "Time spent synthesizing speech",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)
PIPELINE_COMPLETED = Counter(
    "pipeline_jobs_completed_total",
    "Total pipeline jobs completed end-to-end (terminal stage)",
)
PIPELINE_E2E_DURATION = Histogram(
    "pipeline_end_to_end_duration_seconds",
    "End-to-end pipeline duration from original job created_at to TTS completion",
    buckets=[1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0],
)
PIPELINE_FAILED = Counter(
    "pipeline_jobs_failed_total",
    "Pipeline jobs dead-lettered by stage",
    ["stage"],
)
# Shared queue-wait metric — seconds a job waited between creation (audio-api) and
# this stage starting to process it. Same name/labels in every worker (§7).
PIPELINE_QUEUE_WAIT = Histogram(
    "pipeline_queue_wait_seconds",
    "Seconds a job waited between creation and this stage starting",
    ["stage"],
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
)
