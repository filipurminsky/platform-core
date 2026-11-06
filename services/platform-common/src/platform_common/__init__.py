"""Shared building blocks for the platform-core Kafka workers.

Modules (import what you need — heavy optional deps are imported lazily):

- ``settings``        pydantic-settings base classes (Kafka / retry / S3 / Redis)
- ``observability``   structlog JSON logging + OpenTelemetry tracing setup
- ``health``          stdlib /metrics + /healthz + /readyz server with poll heartbeat
- ``kafka_io``        client construction, trace-context header propagation,
                      delivery-confirmed produce, consumer-lag polling
- ``consumer_loop``   the poll → process → commit loop + SIGTERM drain
- ``retry``           error classification (permanent vs transient) + backoff
- ``events``          audio-pipeline event models (contract §3) + DLQ envelopes
- ``job_state``       atomic Redis job-state merge (contract §5)        [audio extra]
- ``dedup``           atomic Redis dedup claim/lease                      [audio extra]
- ``storage``         boto3 S3 client (MinIO-compatible in dev)          [audio extra]
- ``pipeline_metrics`` shared pipeline_* Prometheus metrics (contract §7)
- ``stage``           the generic audio-pipeline stage processor
"""

__version__ = "0.2.0"
