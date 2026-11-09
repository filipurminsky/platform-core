# llm-worker

Kafka consumer that processes meeting transcripts, generates executive summaries and action items via the `llm-gateway`, and persists the results.

## Pipeline Flow

1. **Consume**: Listens to the `audio.transcripts` Kafka topic.
2. **Read**: Fetches the full transcript text from S3/MinIO.
3. **Summarize**: Calls the internal `llm-gateway` (OpenAI-compatible API).
   The transcript is wrapped in `<transcript>` delimiters and the model is told to treat it strictly as data (prompt-injection mitigation).
4. **Defensive Parsing**: Parses the LLM response into a structured format; falls back to raw text on failure.
5. **Write**: Persists the summary JSON to S3 under the `summaries/` prefix.
6. **State**: Updates the job status in Redis.
7. **Produce**: Emits an `audio.summaries` event for the `tts-worker`.

## Technology Stack

- **Runtime**: Python 3.14 (multi-stage image, digest-pinned base, non-root, no uv at runtime)
- **Shared library**: `platform-common` (Kafka I/O, retries, dedup, events)
- **Messaging**: `confluent-kafka`
- **Autoscaling**: [KEDA](https://keda.sh/) (scaled by Kafka consumer lag)
- **Storage**: `boto3` (S3), `redis`
- **LLM Client**: `httpx` (targets `llm-gateway`)

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `LLM_GATEWAY_URL` | `http://llm-gateway...` | Internal URL for the LLM Gateway. |
| `LLM_MODEL` | `TinyLlama/...` | Model identifier to pass to the gateway. |
| `LLM_GATEWAY_API_KEY` | unset | Bearer key for llm-gateway (Secret `llm-gateway-api-keys`, key `llm-worker`). |
| `LLM_MAX_INPUT_CHARS` | `4000` | Per-call transcript size (≈ TinyLlama's 2k context minus output). Longer transcripts are summarised map-reduce. |
| `LLM_MAX_CHUNKS` | `4` | Map-phase cap; beyond it the transcript is truncated (warning + `llm_transcript_truncated_total`). |
| `LLM_TIMEOUT_SECONDS` | `120` | Per gateway call. |
| `KAFKA_TOPIC_IN` | `audio.transcripts` | Input topic. |
| `KAFKA_TOPIC_OUT` | `audio.summaries` | Output topic. |
| `MAX_RETRIES` | `3` | Retries per job before sending to DLQ. |

## Operations

### Autoscaling (KEDA)
Scales based on the lag of the `llm-worker` consumer group on the `audio.transcripts` topic.
- **Min Replicas**: 0 (Scales to zero when idle).
- **Max Replicas**: 3.
- **Lag Threshold**: 5 messages per partition.

### Dead Letter Queue (DLQ)
Jobs that fail after all retries are moved to `audio.llm-dlq`. The job state in Redis is updated to `failed` with the corresponding error message.

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
| `LLM_COLD_START_BUDGET_SECONDS` | `600` | Max time retrying gateway 429/502/503/504 / connection refused (vLLM scale-from-zero), honouring `Retry-After`. Other 4xx are permanent. |
| `LLM_ATTEMPT_BUDGET_SECONDS` | `240` | One full attempt (all map-reduce calls). |
| `KAFKA_MAX_POLL_INTERVAL_MS` | `1200000` | Must exceed 600 + 240 + 60 s. |
| `DEDUP_LEASE_SECONDS` | poll interval + 60 s | Claim lease if a pod crashes mid-job. |

## Local Development

```bash
# Install dependencies
uv sync

# Run tests (mocks S3/Kafka/Gateway)
uv run pytest

# Run worker (requires Kafka/Redis/S3)
uv run python main.py
```
