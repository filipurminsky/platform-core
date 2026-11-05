# stt-worker

Kafka consumer responsible for transcribing audio files into text. It supports pluggable backends, including a lightweight stub for development and an NVIDIA NeMo-based backend for production GPU inference.

## Responsibilities

- **Consume**: Listens to the `audio.jobs` Kafka topic.
- **Fetch**: Retrieves audio blobs from S3/MinIO.
- **Transcribe**: Converts audio to text using the configured backend.
- **Store**: Writes transcript text back to S3 under the `transcripts/` prefix.
- **State**: Updates Redis job state (`status: transcribing`).
- **Produce**: Emits `audio.transcripts` events for the `llm-worker`.

## Technology Stack

- **Runtime**: Python 3.13 (multi-stage image, digest-pinned base, non-root, no uv at runtime)
- **Shared library**: `platform-common` (Kafka I/O, retries, dedup, events)
- **ML Framework**: NVIDIA NeMo (Parakeet RNNT) — Production only.
- **Messaging**: `confluent-kafka`
- **Autoscaling**: [KEDA](https://keda.sh/) (scaled by Kafka consumer lag)
- **Storage**: `boto3` (S3), `redis`

## Pluggable Backends

| Backend | Environment | Description |
|---------|-------------|-------------|
| `stub`  | Dev / CI | Returns placeholder text; duration exact for WAV (header), estimated otherwise. |
| `nemo`  | Prod | NVIDIA Parakeet on GPU. Any input (wav/mp3/ogg/flac/m4a) is decoded with **ffmpeg** to 16 kHz mono PCM first; duration comes from the decoded samples. Handles NeMo 2.x `Hypothesis` results. Image requirements: see the Dockerfile `nemo` stage. |

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `STT_BACKEND` | `stub` | `stub` or `nemo` — any other value fails at startup. |
| `NEMO_MODEL_PATH` | `/model-cache/parakeet-rnnt-0.6b` | Baked model (nemo only). |
| `FFMPEG_BINARY` | `ffmpeg` | Decoder used by the nemo backend. |
| `KAFKA_TOPIC_IN` | `audio.jobs` | Input topic from `audio-api`. |
| `KAFKA_TOPIC_OUT` | `audio.transcripts` | Output topic for `llm-worker`. |
| `S3_BUCKET` | `audio-pipeline` | S3 bucket for audio and transcripts. |

## Operations

### GPU Scheduling
In production, the `nemo` backend requires NVIDIA GPUs. The Kubernetes deployment uses:
- `runtimeClassName: nvidia`
- `nodeSelector: role: gpu`
- Tolerations for `nvidia.com/gpu`

### Autoscaling (KEDA)
- **Min Replicas**: 0 (Scales to zero to save GPU costs).
- **Max Replicas**: 3 (Bounded by GPU node pool capacity).
- **Cooldown**: 300s (Prevents thrashing during model load).

## Reliability (shared via `services/platform-common`)

- **Validation**: inbound events are validated against the contract models
  (`platform_common.events`); invalid messages (bad JSON, missing/non-UUID
  `job_id`, wrong types) go to the stage DLQ with `error_type=invalid` and the
  reason — never silently dropped.
- **Errors are classified**: permanent (missing S3 object, 4xx, undecodable
  data) → DLQ immediately; transient (network, timeouts, S3 5xx/throttling,
  Kafka delivery) → exponential back-off with jitter, bounded by `MAX_RETRIES`
  and a per-message time budget, then DLQ.
- **Dedup** is an atomic Redis claim (`SET dedup:<stage>:<job_id> … NX EX
  <lease>`), set to `done` for `JOB_STATE_TTL_SECONDS` on success/DLQ.
- `KAFKA_MAX_POLL_INTERVAL_MS` is validated at startup against the retry budget;
  retry waits heartbeat `/healthz`; SIGTERM aborts a wait without committing.

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `MAX_RETRIES` | `3` | Attempts for transient errors. |
| `RETRY_BASE_DELAY_SECONDS` / `RETRY_MAX_DELAY_SECONDS` | `2` / `30` | Back-off (equal jitter). |
| `RETRY_BUDGET_SECONDS` | `300` | Max time spent retrying one message. |
| `KAFKA_MAX_POLL_INTERVAL_MS` | `900000` | Must exceed budget + slowest attempt + 60 s. |
| `DEDUP_LEASE_SECONDS` | poll interval + 60 s | Claim lease if a pod crashes mid-job. |

## Local Development

```bash
# Install dependencies (stub mode only)
uv sync

# Run tests
uv run pytest

# Run locally (defaults to stub)
uv run python main.py
```
