# platform-common

Shared library for the platform-core Kafka workers (`worker-service`,
`stt-worker`, `llm-worker`, `tts-worker`). Replaces the copy-pasted
`kafka_io` / `observability` / `metrics_server` / `job_state` / `storage`
modules that used to drift between services.

| Module | What |
|--------|------|
| `settings` | `WorkerSettings` / `AudioWorkerSettings` (pydantic-settings). Validates that `KAFKA_MAX_POLL_INTERVAL_MS` covers the worst-case per-message time. |
| `observability` | structlog JSON logs with `trace_id`/`span_id`, OTLP tracing (honours `OTEL_SDK_DISABLED`). |
| `health` | stdlib `/metrics`, `/healthz`, `/readyz` with a poll heartbeat. |
| `kafka_io` | client config (SASL/SCRAM + TLS), trace-header getter/setter, `produce_confirmed`, `update_lag`. |
| `consumer_loop` | poll → process → sync commit loop; `Outcome.RETRY_LATER` seeks back. |
| `retry` | `classify()` permanent vs transient, `call_with_retry()` exponential back-off + jitter bounded by attempts **and** a wall-clock budget; honours `Retry-After`. |
| `events` | pydantic models for the frozen contract events + DLQ envelope builder. |
| `job_state` | atomic Lua merge into `job:<id>` (contract §5). |
| `dedup` | atomic `SET dedup:<stage>:<id> <token> NX EX <lease>` claim; `done` for the job-state TTL. |
| `storage` | boto3 S3 client (path-style for the dev S3-compatible store). |
| `pipeline_metrics` | `pipeline_jobs_failed_total{stage}`, `pipeline_queue_wait_seconds{stage}`. |
| `stage` | the generic per-message protocol shared by the three audio stages. |

Extras: `platform-common[audio]` adds `boto3` + `redis`.

Consumers depend on it by path (editable for local dev; Docker builds install
it non-editable with `uv sync --no-editable` and a build context of `services/`):

```toml
[tool.uv.sources]
platform-common = { path = "../platform-common", editable = true }
```

```bash
cd services/platform-common && uv run pytest -q
```
