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

- **Runtime**: Python 3.14 (multi-stage image, digest-pinned base, non-root, no uv at runtime)
- **Shared library**: `platform-common` (Kafka I/O, retries, dedup, events)
- **TTS Engine**: [Kokoro](https://github.com/hexgrad/kokoro) (CPU-based) — Production only.
- **Messaging**: `confluent-kafka`
- **Autoscaling**: [KEDA](https://keda.sh/) (scaled by Kafka consumer lag)
- **Storage**: `boto3` (S3), `redis`

## Backends

- **`stub`**: Generates a 0.1s 440Hz tone (WAV). Used for dev/test to keep dependencies light.
- **`kokoro`**: Full Text-to-Speech synthesis using the Kokoro library (not built by CI; see the Dockerfile notes). The `KPipeline` is built once per process and cached.

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `TTS_BACKEND` | `stub` | `stub` or `kokoro` — any other value fails at startup. |
| `KOKORO_LANG_CODE` / `KOKORO_VOICE` | `a` / `af_heart` | Kokoro pipeline language + voice. |
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
# Install dependencies
uv sync

# Run tests
uv run pytest

# Run locally
uv run python main.py
```
