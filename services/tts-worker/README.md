# tts-worker

The terminal stage of the audio processing pipeline. It consumes meeting summaries and synthesizes spoken audio using Text-to-Speech (TTS) backends.

## Pipeline Role

1. **Consume**: Listens to the `audio.summaries` topic.
2. **Fetch**: Reads summary JSON from S3.
3. **Synthesize**: Converts the executive summary (or action items) into a WAV file.
4. **Store**: Writes the resulting audio to S3 under the `speech/` prefix.
5. **Terminal State**: Sets job status to `done` in Redis.
6. **Produce**: Emits the final `audio.results` event.

## Technology Stack

- **Runtime**: Python 3.12
- **TTS Engine**: [Kokoro](https://github.com/hexgrad/kokoro) (CPU-based) — Production only.
- **Messaging**: `confluent-kafka`
- **Autoscaling**: [KEDA](https://keda.sh/) (scaled by Kafka consumer lag)
- **Storage**: `boto3` (S3), `redis`

## Backends

- **`stub`**: Generates a 0.1s 440Hz tone (WAV). Used for dev/test to keep dependencies light.
- **`kokoro`**: Full Text-to-Speech synthesis using the Kokoro library. Pre-baked into the production CPU image.

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `TTS_BACKEND` | `stub` | `stub` or `kokoro`. |
| `KAFKA_TOPIC_IN` | `audio.summaries` | Input topic from `llm-worker`. |
| `KAFKA_TOPIC_OUT` | `audio.results` | Terminal results topic. |
| `REDIS_URL` | `redis://...` | Connection to job state store. |

## Metrics & SLOs

As the terminal stage, this worker tracks end-to-end pipeline performance:
- `pipeline_jobs_completed_total`: Total jobs finished successfully.
- `pipeline_end_to_end_duration_seconds`: Histogram of total time from initial upload to TTS completion.
- `tts_generation_duration_seconds`: Histogram of synthesis latency.

## Operations

### Autoscaling (KEDA)
- **Min Replicas**: 0 (Scales to zero when idle).
- **Max Replicas**: 3.
- **Resources**: Kokoro requires ~2 CPU cores for timely synthesis.

## Local Development

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest

# Run locally
uv run python main.py
```
