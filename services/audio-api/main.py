"""
audio-api — receives audio uploads, stores to S3, produces audio.jobs Kafka events.

Responsibilities:
  - POST /v1/audio/jobs: accept multipart audio upload, validate size + content-type,
    store to S3 (audio/<job_id>), write job state to Redis, produce audio.jobs event.
  - GET /jobs/{job_id}: return Redis job state or 404.
  - GET /healthz, GET /readyz, GET /metrics: standard probes + Prometheus metrics.
  - Structured JSON logging, W3C trace context propagation via Kafka headers.

Config, logging/tracing, metrics, and Kafka helpers live in the `app` package;
this module holds the FastAPI app, the external-client singletons (overridable
in tests), lifecycle hooks, auth check, and the routes.

Env:
  API_TOKEN           — bearer token; if unset, auth is skipped (dev convenience).
  MAX_UPLOAD_BYTES    — default 26214400 (25 MB).
  ALLOWED_CONTENT_TYPES — CSV; default audio/wav,audio/mpeg,audio/mp4,audio/x-m4a,audio/ogg,audio/flac.
  S3_ENDPOINT_URL     — MinIO in dev; unset in prod (AWS default).
  S3_BUCKET           — bucket name.
  S3_REGION           — default eu-west-1.
  REDIS_URL           — e.g. redis://redis.platform.svc.cluster.local:6379/0.
  JOB_STATE_TTL_SECONDS — default 604800 (7 days).
  KAFKA_BOOTSTRAP_SERVERS — default localhost:9092.
  KAFKA_TOPIC_JOBS    — default audio.jobs.
  KAFKA_SASL_USERNAME / KAFKA_SASL_PASSWORD / KAFKA_SECURITY_PROTOCOL — SASL/SCRAM in prod.
  OTEL_SERVICE_NAME, OTEL_EXPORTER_OTLP_ENDPOINT — OTel wiring.
  OTEL_SDK_DISABLED   — set to "true" to suppress span export (tests / no-collector envs).
"""

import asyncio
import datetime
import json
import secrets
import uuid
from contextlib import asynccontextmanager

import boto3
import redis as redis_lib
from app.config import (
    ALLOWED_CONTENT_TYPES,
    API_TOKEN,
    JOB_STATE_TTL_SECONDS,
    KAFKA_TOPIC_JOBS,
    MAX_UPLOAD_BYTES,
    REDIS_URL,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_REGION,
)
from app.kafka_io import _kafka_config, kafka_header_setter
from app.metrics import UPLOAD_BYTES, UPLOADS_TOTAL
from app.observability import log
from botocore.config import Config
from confluent_kafka import Producer
from fastapi import FastAPI, Header, HTTPException, Request, Response, UploadFile
from opentelemetry import propagate
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

# ---------------------------------------------------------------------------
# Module-level singletons (overridable in tests via monkeypatch)
# ---------------------------------------------------------------------------
s3_client = None
redis_client = None
kafka_producer = None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    global s3_client, redis_client, kafka_producer

    # S3 client — MinIO in dev (endpoint_url + path addressing), real S3 in prod.
    s3_kwargs: dict = {"region_name": S3_REGION}
    if S3_ENDPOINT_URL:
        s3_kwargs["endpoint_url"] = S3_ENDPOINT_URL
        s3_kwargs["config"] = Config(s3={"addressing_style": "path"})
    if s3_client is None:
        s3_client = boto3.client("s3", **s3_kwargs)

    if redis_client is None:
        redis_client = redis_lib.from_url(REDIS_URL, decode_responses=True)

    if kafka_producer is None:
        kafka_producer = Producer(_kafka_config())

    log.info(
        "audio_api_started",
        bucket=S3_BUCKET,
        topic=KAFKA_TOPIC_JOBS,
        max_upload_bytes=MAX_UPLOAD_BYTES,
    )

    yield

    if kafka_producer is not None:
        kafka_producer.flush(timeout=10)
    log.info("audio_api_stopped")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="audio-api", version="0.1.0", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)


# ---------------------------------------------------------------------------
# Probes and metrics
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    """Readiness: Redis ping (lenient — just checks connectivity)."""
    checks: dict = {"redis": "unknown", "kafka": "unknown"}
    try:
        if redis_client is not None:
            redis_client.ping()
            checks["redis"] = "ok"
        else:
            checks["redis"] = "not_initialized"
    except Exception as exc:
        log.warning("readyz_redis_fail", error=str(exc))
        checks["redis"] = "error"

    # Kafka: just verify the producer object exists — producer.list_topics()
    # can block/timeout; we keep this lenient per the spec.
    checks["kafka"] = "ok" if kafka_producer is not None else "not_initialized"

    # Return ready even if kafka producer is absent so the pod can come up.
    if checks["redis"] == "error":
        raise HTTPException(status_code=503, detail=checks)

    return {"status": "ready", "checks": checks}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------
def _check_auth(authorization: str | None) -> None:
    """Validate bearer token if API_TOKEN is configured. Skipped when unset."""
    if not API_TOKEN:
        return
    if authorization is None or not authorization.startswith("Bearer "):
        UPLOADS_TOTAL.labels(status="auth_error").inc()
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len("Bearer ") :]
    # Constant-time comparison prevents timing-based token enumeration.
    if not secrets.compare_digest(token, API_TOKEN):
        UPLOADS_TOTAL.labels(status="auth_error").inc()
        raise HTTPException(status_code=401, detail="invalid token")


# ---------------------------------------------------------------------------
# Upload endpoint
# ---------------------------------------------------------------------------
@app.post("/v1/audio/jobs")
async def create_job(
    request: Request,
    file: UploadFile,
    authorization: str | None = Header(default=None),
):
    """Accept a multipart audio upload, store it, and enqueue an audio.jobs event."""
    _check_auth(authorization)

    # Validate content-type
    content_type = (file.content_type or "").split(";")[0].strip()
    if content_type not in ALLOWED_CONTENT_TYPES:
        UPLOADS_TOTAL.labels(status="rejected_type").inc()
        raise HTTPException(
            status_code=415,
            detail=f"unsupported content type: {content_type!r}. Allowed: {sorted(ALLOWED_CONTENT_TYPES)}",
        )

    # Reject obviously oversized uploads before buffering using Content-Length.
    # The hard limit below catches everything else (missing or wrong Content-Length).
    raw_cl = request.headers.get("content-length")
    if raw_cl:
        try:
            if int(raw_cl) > MAX_UPLOAD_BYTES:
                UPLOADS_TOTAL.labels(status="rejected_size").inc()
                raise HTTPException(
                    status_code=413,
                    detail=f"content-length {raw_cl} exceeds maximum {MAX_UPLOAD_BYTES}",
                )
        except ValueError:
            pass  # malformed Content-Length — enforce hard limit after reading

    # Read the file and enforce hard size limit
    audio_bytes = await file.read()
    byte_count = len(audio_bytes)
    if byte_count > MAX_UPLOAD_BYTES:
        UPLOADS_TOTAL.labels(status="rejected_size").inc()
        raise HTTPException(
            status_code=413,
            detail=f"upload size {byte_count} exceeds maximum {MAX_UPLOAD_BYTES}",
        )

    job_id = uuid.uuid4().hex
    audio_key = f"audio/{job_id}"
    created_at = datetime.datetime.now(datetime.UTC).isoformat()

    # Write audio to S3
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=audio_key,
        Body=audio_bytes,
        ContentType=content_type,
    )

    # Write job state to Redis
    job_state = {
        "status": "queued",
        "stage": "audio-api",
        "updated_at": created_at,
        "keys": {
            "audio": audio_key,
            "transcript": None,
            "summary": None,
            "speech": None,
        },
    }
    redis_client.setex(
        f"job:{job_id}",
        JOB_STATE_TTL_SECONDS,
        json.dumps(job_state),
    )

    # Build Kafka event (contract §3)
    event = {
        "job_id": job_id,
        "audio_key": audio_key,
        "content_type": content_type,
        "bytes": byte_count,
        "created_at": created_at,
    }

    # Inject W3C traceparent into Kafka headers
    headers: list[tuple[str, bytes]] = []
    propagate.inject(headers, setter=kafka_header_setter)

    # Produce with delivery confirmation — fail the request if the broker rejects
    # the event so the client knows the job was not enqueued.
    delivery_errors: list[str] = []

    def _on_delivery(err, _msg):
        if err:
            delivery_errors.append(str(err))

    kafka_producer.produce(
        KAFKA_TOPIC_JOBS,
        value=json.dumps(event).encode(),
        key=job_id.encode(),
        headers=headers,
        on_delivery=_on_delivery,
    )
    # Flush in a thread pool so the async event loop is not blocked.
    remaining = await asyncio.get_running_loop().run_in_executor(
        None, lambda: kafka_producer.flush(timeout=5)
    )
    if delivery_errors:
        log.error("kafka_delivery_failed", error=delivery_errors[0], job_id=job_id)
        raise HTTPException(status_code=503, detail=f"event delivery failed: {delivery_errors[0]}")
    if remaining:
        log.error("kafka_flush_timeout", job_id=job_id)
        raise HTTPException(status_code=503, detail="kafka delivery timeout")

    UPLOADS_TOTAL.labels(status="success").inc()
    UPLOAD_BYTES.observe(byte_count)

    log.info(
        "job_created",
        job_id=job_id,
        content_type=content_type,
        bytes=byte_count,
        audio_key=audio_key,
    )

    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# Status endpoint
# ---------------------------------------------------------------------------
@app.get("/jobs/{job_id}")
async def get_job(job_id: str, authorization: str | None = Header(default=None)):
    """Return the current job state from Redis, or 404 if not found."""
    _check_auth(authorization)
    raw = redis_client.get(f"job:{job_id}")
    if raw is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    return json.loads(raw)
