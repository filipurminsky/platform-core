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

- **Runtime**: Python 3.12
- **ML Framework**: NVIDIA NeMo (Parakeet RNNT) — Production only.
- **Messaging**: `confluent-kafka`
- **Autoscaling**: [KEDA](https://keda.sh/) (scaled by Kafka consumer lag)
- **Storage**: `boto3` (S3), `redis`

## Pluggable Backends

| Backend | Environment | Description |
|---------|-------------|-------------|
| `stub`  | Dev / CI | Returns placeholder text; no ML dependencies; fast. |
| `nemo`  | Prod | Uses NVIDIA Parakeet RNNT for high-quality transcription on GPUs. |

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `STT_BACKEND` | `stub` | `stub` or `nemo`. |
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

## Local Development

```bash
# Install dependencies (stub mode only)
uv sync

# Run tests
uv run pytest

# Run locally (defaults to stub)
uv run python main.py
```
