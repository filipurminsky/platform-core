# llm-worker

Kafka consumer that processes meeting transcripts, generates executive summaries and action items via the `llm-gateway`, and persists the results.

## Pipeline Flow

1. **Consume**: Listens to the `audio.transcripts` Kafka topic.
2. **Read**: Fetches the full transcript text from S3/MinIO.
3. **Summarize**: Calls the internal `llm-gateway` (OpenAI-compatible API).
4. **Defensive Parsing**: Parses the LLM response into a structured format; falls back to raw text on failure.
5. **Write**: Persists the summary JSON to S3 under the `summaries/` prefix.
6. **State**: Updates the job status in Redis.
7. **Produce**: Emits an `audio.summaries` event for the `tts-worker`.

## Technology Stack

- **Runtime**: Python 3.12
- **Messaging**: `confluent-kafka`
- **Autoscaling**: [KEDA](https://keda.sh/) (scaled by Kafka consumer lag)
- **Storage**: `boto3` (S3), `redis`
- **LLM Client**: `httpx` (targets `llm-gateway`)

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `LLM_GATEWAY_URL` | `http://llm-gateway...` | Internal URL for the LLM Gateway. |
| `LLM_MODEL` | `TinyLlama/...` | Model identifier to pass to the gateway. |
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

## Local Development

```bash
# Install dependencies
uv sync

# Run tests (mocks S3/Kafka/Gateway)
uv run pytest

# Run worker (requires Kafka/Redis/S3)
uv run python main.py
```
