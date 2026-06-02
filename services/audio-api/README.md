# audio-api

FastAPI service that accepts audio uploads, stores them in S3 (or MinIO), and produces Kafka events to initiate the AI audio processing pipeline.

## Responsibilities

- **Audio Ingestion**: Accepts multipart audio uploads via `POST /v1/audio/jobs`.
- **Validation**: Enforces file size limits (default 25MB) and allowed content types (WAV, MP3, MP4, etc.).
- **Object Storage**: Persists raw audio files to S3/MinIO under the `audio/` prefix.
- **Persistence**: Writes initial job state to Redis with a configurable TTL (default 7 days).
- **Event Orchestration**: Produces `audio.jobs` Kafka events to trigger the `stt-worker`.
- **Observability**: Exposes Prometheus metrics and propagates W3C trace context via Kafka headers.

## Technology Stack

- **Framework**: FastAPI (Python 3.12)
- **Package Manager**: [uv](https://github.com/astral-sh/uv)
- **Storage**: `boto3` (S3/MinIO), `redis`
- **Messaging**: `confluent-kafka`
- **Observability**: `prometheus-client`, OpenTelemetry (OTel), `structlog`

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/audio/jobs` | Upload audio file and start a processing job |
| `GET`  | `/jobs/{job_id}` | Retrieve the current status and artifact keys from Redis |
| `GET`  | `/healthz` | Liveness probe |
| `GET`  | `/readyz` | Readiness probe (checks Redis connectivity) |
| `GET`  | `/metrics` | Prometheus metrics |

## Kafka Interaction

- **Topic Out**: `audio.jobs` (configured via `KAFKA_TOPIC_JOBS`)
- **Key**: `job_id` (UUIDv4)
- **Headers**: Injects `traceparent` for distributed tracing.

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `API_TOKEN` | `""` | Bearer token for auth. If empty, auth is skipped. |
| `MAX_UPLOAD_BYTES` | `26214400` | Max upload size (25 MB). |
| `ALLOWED_CONTENT_TYPES` | `audio/wav,...` | CSV of allowed MIME types. |
| `S3_ENDPOINT_URL` | `""` | MinIO URL for local dev; unset for AWS S3. |
| `S3_BUCKET` | `audio-pipeline` | Target S3 bucket. |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string. |
| `KAFKA_BOOTSTRAP_SERVERS`| `localhost:9092` | Kafka broker list. |

## Deployment

Managed via Kustomize in `k8s/`.
- **Replicas**: 2 (Production), 1 (Development).
- **Resources**: Configured with requests/limits for CPU and Memory.
- **NetworkPolicy**: Restricts traffic to Ingress-Nginx, Prometheus, Kafka, Redis, and S3.

## Local Development

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest

# Run locally
uv run uvicorn main:app --host 0.0.0.0 --port 8080
```
