# worker-service

A generic Kafka consumer template used to demonstrate job processing, deduplication, and dead-letter queue (DLQ) patterns.

## Features

- **Handler Registry**: Pluggable architecture for adding new job types via decorators.
- **Idempotency**: UUID-based deduplication (within a pod's lifetime) to prevent double-processing.
- **Resilience**: Configurable exponential back-off retries and DLQ fallback.
- **Graceful Shutdown**: Handles `SIGTERM` to finish in-flight jobs and commit offsets before exiting.
- **Autoscaling**: Fully compatible with KEDA scale-to-zero.

## Job Types

| Type | Description |
|------|-------------|
| `data-transform` | Performs string operations (uppercase, reverse, etc.) on a payload. |
| `ping` | Simple health check job that returns a `pong`. |

## Technology Stack

- **Runtime**: Python 3.12
- **Messaging**: `confluent-kafka`
- **Observability**: OpenTelemetry (trace extraction/injection), `prometheus-client`.
- **Package Manager**: [uv](https://github.com/astral-sh/uv)

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `KAFKA_TOPIC` | `jobs` | Input topic. |
| `KAFKA_DLQ_TOPIC` | `jobs-dlq` | Dead letter topic for failed jobs. |
| `MAX_RETRIES` | `3` | Retries before moving to DLQ. |
| `KAFKA_CONSUMER_GROUP`| `worker-service` | Kafka consumer group ID. |

## Operations

### Consumer Lag Monitoring
Exposes a Prometheus gauge `worker_consumer_lag` per partition, calculated by comparing watermark offsets with committed offsets.

### KEDA Scaling
- **Min Replicas**: 0.
- **Max Replicas**: 5.
- **Threshold**: 10 messages of lag per partition.

## Local Development

```bash
# Produce a test job via Docker Compose
docker compose exec kafka kafka-console-producer.sh \
  --bootstrap-server localhost:9092 --topic jobs <<< \
  '{"id":"test-001","type":"data-transform","payload":{"input":"hello","operation":"uppercase"}}'

# Run tests
uv run pytest
```
