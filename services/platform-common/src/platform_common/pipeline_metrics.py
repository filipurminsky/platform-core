"""Pipeline-wide Prometheus metrics shared by every audio worker (contract §7).

Importing this module registers the series, so only audio-pipeline workers
import it. Names and labels are the dashboard contract — do not change them.
``pipeline_jobs_completed_total`` / ``pipeline_end_to_end_duration_seconds``
are terminal-stage only and stay in tts-worker.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from prometheus_client import Counter, Histogram

PIPELINE_JOBS_FAILED = Counter(
    "pipeline_jobs_failed_total",
    "Pipeline jobs that were dead-lettered, by stage",
    ["stage"],
)
PIPELINE_QUEUE_WAIT = Histogram(
    "pipeline_queue_wait_seconds",
    "Seconds a job waited between creation and this stage starting",
    ["stage"],
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
)


def parse_created_at(created_at: str | None) -> float | None:
    """UTC epoch seconds from an ISO-8601 string, or None if absent/unparsable."""
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(created_at)
    except ValueError, TypeError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def observe_queue_wait(stage: str, created_at: str | None) -> None:
    ts = parse_created_at(created_at)
    if ts is not None:
        PIPELINE_QUEUE_WAIT.labels(stage=stage).observe(max(0.0, time.time() - ts))
