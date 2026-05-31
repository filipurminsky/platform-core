"""
worker-service — Kafka consumer that processes jobs from the `jobs` topic.

Processing contract:
  - Deserialises JSON from `jobs` topic
  - Runs the registered handler for job["type"]
  - On success: commits offset manually (enable.auto.commit=false)
  - On failure: retries up to MAX_RETRIES; after that publishes to `jobs-dlq` then commits
  - UUID-based deduplication via an in-memory set (survives restarts via Kafka offset)
  - Drains gracefully on SIGTERM: finishes the in-flight message, commits, exits

Prometheus metrics exposed on :9090/metrics:
  worker_jobs_processed_total{status="success|error|dlq"}
  worker_job_duration_seconds (histogram)
  worker_consumer_lag (gauge, polled from assignment watermarks)
"""

import json
import os
import signal
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer

import structlog
from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# ---------------------------------------------------------------------------
# Config (environment-driven, safe defaults for local dev)
# ---------------------------------------------------------------------------
BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_JOBS = os.getenv("KAFKA_TOPIC", "jobs")
TOPIC_DLQ = os.getenv("KAFKA_DLQ_TOPIC", "jobs-dlq")
CONSUMER_GROUP = os.getenv("KAFKA_CONSUMER_GROUP", "worker-service")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "9090"))

# SASL/SCRAM — injected by Strimzi KafkaUser secret in prod; empty → plaintext for local dev
SASL_USERNAME = os.getenv("KAFKA_SASL_USERNAME", "")
SASL_PASSWORD = os.getenv("KAFKA_SASL_PASSWORD", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Job handlers — register new types here
# ---------------------------------------------------------------------------
_HANDLERS: dict[str, Callable[[dict], dict]] = {}


def register(job_type: str):
    """Decorator: register a handler for a job type."""
    def decorator(fn: Callable[[dict], dict]):
        _HANDLERS[job_type] = fn
        return fn
    return decorator


@register("data-transform")
def handle_data_transform(job: dict) -> dict:
    """Example handler: transforms payload.input according to payload.operation."""
    payload = job.get("payload", {})
    text = payload.get("input", "")
    operation = payload.get("operation", "uppercase")

    if operation == "uppercase":
        result = text.upper()
    elif operation == "lowercase":
        result = text.lower()
    elif operation == "reverse":
        result = text[::-1]
    else:
        raise ValueError(f"Unknown operation: {operation}")

    return {"input": text, "operation": operation, "result": result}


@register("ping")
def handle_ping(job: dict) -> dict:
    """Health-check job type — always succeeds."""
    return {"pong": True, "hostname": socket.gethostname()}


# ---------------------------------------------------------------------------
# Metrics HTTP server (separate thread, does not use FastAPI to keep deps minimal)
# ---------------------------------------------------------------------------
class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            data = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(data)
        elif self.path in ("/healthz", "/readyz"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # suppress default access log; structlog handles it


def start_metrics_server():
    server = HTTPServer(("0.0.0.0", METRICS_PORT), MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("metrics_server_started", port=METRICS_PORT)


# ---------------------------------------------------------------------------
# Kafka helpers
# ---------------------------------------------------------------------------
def _kafka_config(extra: dict | None = None) -> dict:
    cfg = {
        "bootstrap.servers": BOOTSTRAP_SERVERS,
        "client.id": f"worker-service-{socket.gethostname()}",
    }
    if SASL_USERNAME:
        cfg.update(
            {
                "security.protocol": "SASL_SSL",
                "sasl.mechanism": "SCRAM-SHA-512",
                "sasl.username": SASL_USERNAME,
                "sasl.password": SASL_PASSWORD,
            }
        )
    if extra:
        cfg.update(extra)
    return cfg


def make_consumer() -> Consumer:
    cfg = _kafka_config(
        {
            "group.id": CONSUMER_GROUP,
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,  # manual commit only after processing
            "max.poll.interval.ms": 300_000,
        }
    )
    consumer = Consumer(cfg)
    consumer.subscribe([TOPIC_JOBS])
    return consumer


def make_producer() -> Producer:
    return Producer(_kafka_config())


def update_lag(consumer: Consumer) -> None:
    """Poll watermark offsets and update the lag gauge for each assigned partition."""
    try:
        for tp in consumer.assignment():
            low, high = consumer.get_watermark_offsets(tp, timeout=1.0, cached=True)
            committed = consumer.committed([tp], timeout=1.0)
            committed_offset = committed[0].offset if committed and committed[0].offset >= 0 else low
            lag = max(0, high - committed_offset)
            CONSUMER_LAG.labels(partition=str(tp.partition)).set(lag)
    except Exception as exc:
        log.warning("lag_poll_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Main consume loop
# ---------------------------------------------------------------------------
def process_message(raw_value: bytes, producer: Producer, seen_ids: set) -> None:
    """Deserialise → deduplicate → dispatch → DLQ on repeated failure."""
    try:
        job = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        log.error("bad_json", error=str(exc), raw=raw_value[:200])
        JOBS_PROCESSED.labels(status="error").inc()
        return  # malformed message: commit and move on

    job_id = job.get("id", "")
    job_type = job.get("type", "unknown")

    # Deduplication — in-memory; survives restarts via committed Kafka offset
    if job_id and job_id in seen_ids:
        log.info("duplicate_skipped", job_id=job_id)
        return
    if job_id:
        seen_ids.add(job_id)

    handler = _HANDLERS.get(job_type)
    if handler is None:
        log.warning("unknown_job_type", job_type=job_type, job_id=job_id)
        JOBS_PROCESSED.labels(status="error").inc()
        return  # commit and move on — no handler available

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        start = time.perf_counter()
        try:
            result = handler(job)
            elapsed = time.perf_counter() - start
            JOB_DURATION.observe(elapsed)
            JOBS_PROCESSED.labels(status="success").inc()
            log.info(
                "job_processed",
                job_id=job_id,
                job_type=job_type,
                attempt=attempt,
                duration_ms=round(elapsed * 1000, 2),
                result=result,
            )
            return
        except Exception as exc:
            last_exc = exc
            log.warning(
                "job_attempt_failed",
                job_id=job_id,
                job_type=job_type,
                attempt=attempt,
                error=str(exc),
            )
            if attempt < MAX_RETRIES:
                time.sleep(0.1 * (2 ** (attempt - 1)))  # exponential back-off: 100ms, 200ms

    # All retries exhausted → send to DLQ
    dlq_payload = json.dumps(
        {"original_job": job, "error": str(last_exc), "attempts": MAX_RETRIES}
    ).encode()
    producer.produce(TOPIC_DLQ, value=dlq_payload, key=job_id.encode() if job_id else None)
    producer.flush(timeout=5)
    JOBS_PROCESSED.labels(status="dlq").inc()
    log.error(
        "job_sent_to_dlq",
        job_id=job_id,
        job_type=job_type,
        error=str(last_exc),
    )


def run() -> None:
    log.info("worker_starting", bootstrap=BOOTSTRAP_SERVERS, topic=TOPIC_JOBS, group=CONSUMER_GROUP)

    start_metrics_server()
    consumer = make_consumer()
    producer = make_producer()
    seen_ids: set[str] = set()
    shutdown = threading.Event()
    lag_last_polled = 0.0

    def _handle_signal(sig, frame):
        log.info("shutdown_signal_received", signal=sig)
        shutdown.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while not shutdown.is_set():
            msg = consumer.poll(timeout=1.0)

            # Periodically refresh lag gauge
            now = time.time()
            if now - lag_last_polled > 15:
                update_lag(consumer)
                lag_last_polled = now

            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise KafkaException(msg.error())

            process_message(msg.value(), producer, seen_ids)

            # Manual commit — only after successful processing or DLQ publish
            consumer.commit(message=msg, asynchronous=False)

    finally:
        log.info("worker_shutting_down")
        consumer.close()
        producer.flush(timeout=10)
        log.info("worker_stopped")


if __name__ == "__main__":
    run()
