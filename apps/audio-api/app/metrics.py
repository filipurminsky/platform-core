"""Prometheus metric definitions, exposed on GET /metrics."""

from prometheus_client import Counter, Histogram

UPLOADS_TOTAL = Counter(
    "audio_uploads_total",
    "Audio upload requests by status",
    ["status"],  # success | rejected_size | rejected_type | auth_error
)
UPLOAD_BYTES = Histogram(
    "audio_upload_bytes",
    "Size of accepted audio uploads in bytes",
    buckets=[
        10_000,
        100_000,
        500_000,
        1_000_000,
        5_000_000,
        10_000_000,
        25_000_000,
    ],
)
