"""
llm-worker — Kafka consumer that summarises meeting transcripts via the LLM gateway.

Processing contract (per audio-pipeline-contract.md §1-9, especially §6):
  - Consumes from `audio.transcripts` (consumer group `llm-worker`)
  - Reads full transcript text from S3 transcripts/<job_id>
  - Calls the EXISTING llm-gateway at LLM_GATEWAY_URL (OpenAI-compatible
    POST /v1/chat/completions).  NEVER calls vLLM directly.
    In dev the gateway proxies to TinyLlama — low quality by design; the pipeline
    is a wiring demo, not a production-quality summariser.
  - Parses the assistant reply defensively: extracts executive summary + action_items[];
    if parsing fails falls back to full text as summary + empty action_items (no crash).
  - Writes full summary JSON to S3 summaries/<job_id> (idempotent).
  - SETs Redis job-state: status=summarizing on start, keys.summary on success, failed on DLQ.
  - Produces `audio.summaries` (carries created_at forward for end-to-end metric).
  - On repeated failure: produces to `audio.llm-dlq` + sets Redis failed.
  - Manual commit only after output event/DLQ produced (enable.auto.commit=false).
  - SIGTERM: finishes in-flight message, commits, exits (terminationGracePeriodSeconds=60).

Prometheus metrics on :9090/metrics (contract §7):
  llm_jobs_total{status}          Counter   success|error|dlq
  llm_tokens_generated_total      Counter   completion_tokens from gateway response
  llm_request_duration_seconds    Histogram latency of the /v1/chat/completions call
  pipeline_jobs_failed_total{stage="llm"}  Counter  on DLQ
"""

import json
import os
import signal
import socket
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import boto3
import httpx
import redis as redis_lib
import structlog
from botocore.config import Config
from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagators.textmap import Getter, Setter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

# ---------------------------------------------------------------------------
# Config (environment-driven, safe defaults for local dev)
# ---------------------------------------------------------------------------
BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC_IN = os.getenv("KAFKA_TOPIC_IN", "audio.transcripts")
TOPIC_OUT = os.getenv("KAFKA_TOPIC_OUT", "audio.summaries")
TOPIC_DLQ = os.getenv("KAFKA_DLQ_TOPIC", "audio.llm-dlq")
CONSUMER_GROUP = os.getenv("KAFKA_CONSUMER_GROUP", "llm-worker")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "9090"))

# SASL/SCRAM — injected by Strimzi KafkaUser secret in prod; empty → plaintext for local dev
SASL_USERNAME = os.getenv("KAFKA_SASL_USERNAME", "")
SASL_PASSWORD = os.getenv("KAFKA_SASL_PASSWORD", "")
SECURITY_PROTOCOL = os.getenv(
    "KAFKA_SECURITY_PROTOCOL", "SASL_SSL" if SASL_USERNAME else "PLAINTEXT"
)

# LLM gateway — never talk to vLLM directly (§6)
LLM_GATEWAY_URL = os.getenv("LLM_GATEWAY_URL", "http://llm-gateway.apps.svc.cluster.local:80")
# In dev the gateway proxies to TinyLlama (low quality by design — pipeline wiring demo).
LLM_MODEL = os.getenv("LLM_MODEL", "TinyLlama/TinyLlama-1.1B-Chat-v1.0")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))

# S3 / MinIO (§4)
S3_BUCKET = os.getenv("S3_BUCKET", "audio-pipeline")
S3_REGION = os.getenv("S3_REGION", "eu-west-1")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "")  # set for MinIO dev; unset in prod

# Redis job-state (§5)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
JOB_STATE_TTL = int(os.getenv("JOB_STATE_TTL_SECONDS", "604800"))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def add_trace_context(_logger, _method_name, event_dict):
    span = trace.get_current_span()
    context = span.get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = f"{context.trace_id:032x}"
        event_dict["span_id"] = f"{context.span_id:016x}"
    return event_dict


structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        add_trace_context,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------
def configure_tracing() -> None:
    # Honour the standard kill switch so tests/CI (and any env without a
    # collector) don't block on span export. Set OTEL_SDK_DISABLED=true there.
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    service_name = os.getenv("OTEL_SERVICE_NAME", "llm-worker")
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


configure_tracing()
tracer = trace.get_tracer(__name__)


class KafkaHeaderGetter(Getter):
    def get(self, carrier, key):
        values = []
        for header_key, header_value in carrier or []:
            if header_key.lower() == key.lower() and header_value is not None:
                values.append(
                    header_value.decode() if isinstance(header_value, bytes) else str(header_value)
                )
        return values or None

    def keys(self, carrier):
        return [key for key, _value in carrier or []]


class KafkaHeaderSetter(Setter):
    def set(self, carrier, key, value):
        carrier.append((key, value.encode()))


kafka_header_getter = KafkaHeaderGetter()
kafka_header_setter = KafkaHeaderSetter()


# ---------------------------------------------------------------------------
# Metrics (contract §7)
# ---------------------------------------------------------------------------
LLM_JOBS = Counter(
    "llm_jobs_total",
    "LLM summarisation jobs by outcome",
    ["status"],  # success | error | dlq
)
LLM_TOKENS = Counter(
    "llm_tokens_generated_total",
    "Completion tokens generated by the LLM gateway",
)
LLM_REQUEST_DURATION = Histogram(
    "llm_request_duration_seconds",
    "Latency of POST /v1/chat/completions to the LLM gateway",
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
)
PIPELINE_FAILED = Counter(
    "pipeline_jobs_failed_total",
    "Jobs dead-lettered by stage",
    ["stage"],
)


# ---------------------------------------------------------------------------
# Metrics HTTP server (separate thread — no FastAPI to keep deps minimal)
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
        "client.id": f"llm-worker-{socket.gethostname()}",
    }
    if SASL_USERNAME:
        cfg.update(
            {
                "security.protocol": SECURITY_PROTOCOL,
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
            "enable.auto.commit": False,  # manual commit only after output produced
            "max.poll.interval.ms": 300_000,
        }
    )
    consumer = Consumer(cfg)
    consumer.subscribe([TOPIC_IN])
    return consumer


def make_producer() -> Producer:
    return Producer(_kafka_config())


# ---------------------------------------------------------------------------
# S3 client (§4)
# ---------------------------------------------------------------------------
def make_s3_client():
    kwargs: dict = {
        "region_name": S3_REGION,
    }
    if S3_ENDPOINT_URL:
        # MinIO (dev): path-style addressing required
        kwargs["endpoint_url"] = S3_ENDPOINT_URL
        kwargs["config"] = Config(s3={"addressing_style": "path"})
    return boto3.client("s3", **kwargs)


# ---------------------------------------------------------------------------
# Redis client (§5)
# ---------------------------------------------------------------------------
def make_redis_client():
    return redis_lib.from_url(REDIS_URL, decode_responses=True)


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are an executive assistant. Given a meeting transcript, produce:\n"
    "1. SUMMARY: A concise executive summary (2-5 sentences).\n"
    "2. ACTION_ITEMS: A bullet list of concrete action items.\n\n"
    "Format your response exactly as:\n"
    "SUMMARY:\n<your summary here>\n\n"
    "ACTION_ITEMS:\n- <item 1>\n- <item 2>\n..."
)


def call_llm_gateway(
    http_client: httpx.Client,
    transcript: str,
) -> tuple[str, list[str], int]:
    """
    POST to LLM gateway and parse the response.

    Returns (summary, action_items, completion_tokens).
    Defensive: if parsing fails, returns (full_text, [], tokens).
    Raises httpx.HTTPStatusError / httpx.RequestError on gateway errors.
    """
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Transcript:\n{transcript}"},
        ],
        "temperature": 0.3,
        "max_tokens": 512,
    }

    start = time.perf_counter()
    resp = http_client.post(
        "/v1/chat/completions",
        json=payload,
        timeout=LLM_TIMEOUT,
    )
    elapsed = time.perf_counter() - start
    LLM_REQUEST_DURATION.observe(elapsed)

    resp.raise_for_status()
    data = resp.json()

    # Extract completion_tokens for metric
    completion_tokens = 0
    try:
        completion_tokens = data.get("usage", {}).get("completion_tokens", 0) or 0
    except Exception:
        pass

    # Extract assistant message text
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        text = str(data)

    # Defensive parse: extract SUMMARY and ACTION_ITEMS sections
    summary, action_items = _parse_llm_response(text)
    return summary, action_items, completion_tokens


def _parse_llm_response(text: str) -> tuple[str, list[str]]:
    """
    Parse the structured LLM response.

    Expects:
      SUMMARY:
      <summary text>

      ACTION_ITEMS:
      - <item>
      - <item>

    Falls back to (full_text, []) on any parse failure.
    """
    try:
        summary = ""
        action_items: list[str] = []

        lines = text.strip().splitlines()
        section = None
        summary_lines: list[str] = []
        item_lines: list[str] = []

        for line in lines:
            stripped = line.strip()
            if stripped.upper().startswith("SUMMARY:"):
                section = "summary"
                # inline content after the header
                rest = stripped[len("SUMMARY:") :].strip()
                if rest:
                    summary_lines.append(rest)
            elif stripped.upper().startswith("ACTION_ITEMS:"):
                section = "action_items"
                rest = stripped[len("ACTION_ITEMS:") :].strip()
                if rest.startswith("-"):
                    item_lines.append(rest.lstrip("- ").strip())
            else:
                if section == "summary":
                    summary_lines.append(stripped)
                elif section == "action_items":
                    if stripped.startswith("-"):
                        item_lines.append(stripped.lstrip("- ").strip())
                    elif stripped:
                        item_lines.append(stripped)

        summary = " ".join(s for s in summary_lines if s).strip()
        action_items = [i for i in item_lines if i]

        if not summary:
            # Nothing parseable — fall back
            return text.strip(), []

        return summary, action_items

    except Exception:
        # Defensive: never crash on parse failure
        return text.strip(), []


# ---------------------------------------------------------------------------
# Redis job-state helpers (§5)
# ---------------------------------------------------------------------------
def _redis_get_state(r, job_id: str) -> dict:
    try:
        raw = r.get(f"job:{job_id}")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _redis_set_state(r, job_id: str, state: dict) -> None:
    try:
        r.set(f"job:{job_id}", json.dumps(state), ex=JOB_STATE_TTL)
    except Exception as exc:
        log.warning("redis_set_failed", job_id=job_id, error=str(exc))


def redis_mark_summarizing(r, job_id: str) -> None:
    state = _redis_get_state(r, job_id)
    state["status"] = "summarizing"
    state["stage"] = "llm"
    state["updated_at"] = datetime.now(UTC).isoformat()
    _redis_set_state(r, job_id, state)


def redis_mark_done(r, job_id: str, summary_key: str) -> None:
    state = _redis_get_state(r, job_id)
    state["status"] = "summarizing"  # tts-worker will set synthesizing/done
    state["stage"] = "llm"
    state["updated_at"] = datetime.now(UTC).isoformat()
    keys = state.get("keys", {})
    keys["summary"] = summary_key
    state["keys"] = keys
    _redis_set_state(r, job_id, state)


def redis_mark_failed(r, job_id: str, error_msg: str) -> None:
    state = _redis_get_state(r, job_id)
    state["status"] = "failed"
    state["stage"] = "llm"
    state["updated_at"] = datetime.now(UTC).isoformat()
    state["error"] = {
        "stage": "llm",
        "message": error_msg,
        "dlq_topic": TOPIC_DLQ,
    }
    _redis_set_state(r, job_id, state)


# ---------------------------------------------------------------------------
# Core message handler
# ---------------------------------------------------------------------------
def process_message(
    raw_value: bytes,
    producer: Producer,
    seen_ids: set,
    s3,
    r,
    http_client: httpx.Client,
    headers: list[tuple[str, bytes]] | None = None,
) -> None:
    """Deserialise → deduplicate → summarise → write S3 → Redis → produce → DLQ on failure."""
    context = propagate.extract(headers or [], getter=kafka_header_getter)

    try:
        event = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        log.error("bad_json", error=str(exc), raw=raw_value[:200])
        LLM_JOBS.labels(status="error").inc()
        return  # malformed message: commit and move on

    job_id = event.get("job_id", "")
    transcript_key = event.get("transcript_key", f"transcripts/{job_id}")
    created_at = event.get("created_at", datetime.now(UTC).isoformat())

    # Deduplication — in-memory; survives restarts via committed Kafka offset
    if job_id and job_id in seen_ids:
        log.info("duplicate_skipped", job_id=job_id)
        return
    if job_id:
        seen_ids.add(job_id)

    with tracer.start_as_current_span(
        "llm-worker.process",
        context=context,
        attributes={
            "messaging.system": "kafka",
            "messaging.destination.name": TOPIC_IN,
            "job.id": job_id,
        },
    ) as span:
        last_exc: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Mark in-progress in Redis
                redis_mark_summarizing(r, job_id)

                # Read transcript from S3
                obj = s3.get_object(Bucket=S3_BUCKET, Key=transcript_key)
                transcript = obj["Body"].read().decode("utf-8")

                # Call LLM gateway (never vLLM directly — §6)
                summary, action_items, completion_tokens = call_llm_gateway(http_client, transcript)
                LLM_TOKENS.inc(completion_tokens)

                # Build full summary JSON for S3
                summary_key = f"summaries/{job_id}"
                summary_doc = {
                    "job_id": job_id,
                    "summary": summary,
                    "action_items": action_items,
                    "model": LLM_MODEL,
                    "created_at": created_at,
                }
                s3.put_object(
                    Bucket=S3_BUCKET,
                    Key=summary_key,
                    Body=json.dumps(summary_doc).encode("utf-8"),
                    ContentType="application/json",
                )

                # Update Redis with summary key
                redis_mark_done(r, job_id, summary_key)

                # Produce audio.summaries (carry created_at forward for end-to-end metric)
                out_event = {
                    "job_id": job_id,
                    "summary_key": summary_key,
                    "action_items": action_items,
                    "created_at": created_at,
                }
                out_headers: list[tuple[str, bytes]] = []
                propagate.inject(out_headers, setter=kafka_header_setter)
                producer.produce(
                    TOPIC_OUT,
                    value=json.dumps(out_event).encode(),
                    key=job_id.encode() if job_id else None,
                    headers=out_headers,
                )
                producer.flush(timeout=5)

                LLM_JOBS.labels(status="success").inc()
                log.info(
                    "job_summarised",
                    job_id=job_id,
                    attempt=attempt,
                    completion_tokens=completion_tokens,
                    action_items_count=len(action_items),
                )
                return

            except Exception as exc:
                last_exc = exc
                span.record_exception(exc)
                log.warning(
                    "job_attempt_failed",
                    job_id=job_id,
                    attempt=attempt,
                    error=str(exc),
                )
                if attempt < MAX_RETRIES:
                    time.sleep(0.1 * (2 ** (attempt - 1)))  # 100ms, 200ms back-off

        # All retries exhausted → dead-letter
        dlq_payload = json.dumps(
            {
                **event,
                "error": str(last_exc),
                "stage": "llm",
                "failed_at": datetime.now(UTC).isoformat(),
                "attempts": MAX_RETRIES,
            }
        ).encode()
        dlq_headers: list[tuple[str, bytes]] = []
        propagate.inject(dlq_headers, setter=kafka_header_setter)
        producer.produce(
            TOPIC_DLQ,
            value=dlq_payload,
            key=job_id.encode() if job_id else None,
            headers=dlq_headers,
        )
        producer.flush(timeout=5)

        redis_mark_failed(r, job_id, str(last_exc))
        LLM_JOBS.labels(status="dlq").inc()
        PIPELINE_FAILED.labels(stage="llm").inc()
        log.error(
            "job_sent_to_dlq",
            job_id=job_id,
            error=str(last_exc),
        )


# ---------------------------------------------------------------------------
# Main consume loop
# ---------------------------------------------------------------------------
def run() -> None:
    log.info(
        "llm_worker_starting",
        bootstrap=BOOTSTRAP_SERVERS,
        topic_in=TOPIC_IN,
        topic_out=TOPIC_OUT,
        group=CONSUMER_GROUP,
        llm_gateway=LLM_GATEWAY_URL,
    )

    start_metrics_server()
    consumer = make_consumer()
    producer = make_producer()
    s3 = make_s3_client()
    r = make_redis_client()
    http_client = httpx.Client(
        base_url=LLM_GATEWAY_URL,
        timeout=httpx.Timeout(LLM_TIMEOUT),
    )

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

            # Periodically refresh lag gauge (best-effort; metric not in contract §7 for llm)
            now = time.time()
            if now - lag_last_polled > 15:
                lag_last_polled = now

            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise KafkaException(msg.error())

            process_message(
                msg.value(),
                producer,
                seen_ids,
                s3,
                r,
                http_client,
                headers=msg.headers(),
            )

            # Manual commit — only after output event or DLQ publish (§1)
            consumer.commit(message=msg, asynchronous=False)

    finally:
        log.info("llm_worker_shutting_down")
        consumer.close()
        producer.flush(timeout=10)
        http_client.close()
        log.info("llm_worker_stopped")


if __name__ == "__main__":
    run()
