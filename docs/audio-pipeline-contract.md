# AI Audio Pipeline — Frozen Contracts (Chunk 0)

> **Status: FROZEN.** This is the seam between the parallel chunks. No chunk may
> change anything here unilaterally — a change requires re-freezing and notifying
> every chunk. Implements `AI_Audio_Pipeline_Specification.md` § "Parallel
> Implementation Plan". Cross-references the existing platform in
> `AGENTS.md` — reuse, don't reinvent.

## Pipeline shape

```
audio-api ──audio.jobs──▶ stt-worker ──audio.transcripts──▶ llm-worker
   ──audio.summaries──▶ tts-worker ──audio.results──▶ (terminal)
```

Each worker: consume → read input blob from object storage by key → process →
write output blob to object storage → `SET` Redis job-state → produce next event.
Reuse the shared worker pattern — implemented once in `services/platform-common`
(`platform_common.stage` / `consumer_loop`), used by all three stages: confluent-kafka consumer,
`enable.auto.commit=false` (commit only after the output event is produced),
job-id dedup, retry → per-stage DLQ, SIGTERM drain, metrics HTTP server on
`:9090` (`/metrics`, `/healthz`, `/readyz`), trace context extracted from Kafka
headers via the `KafkaHeaderGetter`/`KafkaHeaderSetter` + `propagate` helpers.

## 1. Kafka envelope

- **Message key** = `job_id` (UUID string, utf-8 bytes) on every topic — drives
  partition affinity, ordering, and dedup.
- **Headers** carry W3C `traceparent` (and `tracestate`) — injected/extracted
  with the existing `opentelemetry.propagate` + Kafka header getter/setter
  pattern from `worker-service`. Every hop re-injects so the chain stitches into
  one trace: `audio-api → stt-worker → llm-worker → tts-worker`.
- **Value** = UTF-8 JSON, schemas below.

## 2. Topics (all `audio.`-prefixed — owned by Chunk 1)

| Topic | Producer | Consumer | Consumer group |
|-------|----------|----------|----------------|
| `audio.jobs` | audio-api | stt-worker | `stt-worker` |
| `audio.transcripts` | stt-worker | llm-worker | `llm-worker` |
| `audio.summaries` | llm-worker | tts-worker | `tts-worker` |
| `audio.results` | tts-worker | (audio-api/UI, optional) | — |
| `audio.stt-dlq` | stt-worker | replay/alert | — |
| `audio.llm-dlq` | llm-worker | replay/alert | — |
| `audio.tts-dlq` | tts-worker | replay/alert | — |

## 3. Event values (JSON)

```jsonc
// audio.jobs   (audio-api → stt-worker)
{ "job_id": "<uuid>", "audio_key": "audio/<job_id>", "content_type": "audio/wav", "bytes": 12345, "created_at": "<iso8601>" }

// audio.transcripts   (stt-worker → llm-worker)
{ "job_id": "<uuid>", "transcript_key": "transcripts/<job_id>", "language": "en", "duration_s": 42.0 }

// audio.summaries   (llm-worker → tts-worker)
{ "job_id": "<uuid>", "summary_key": "summaries/<job_id>", "action_items": ["...", "..."] }

// audio.results   (tts-worker → terminal)
{ "job_id": "<uuid>", "speech_key": "speech/<job_id>", "status": "done" }

// DLQ value (any *-dlq topic): original message value + envelope
{ ...original_event..., "error": "<str>", "stage": "stt|llm|tts", "failed_at": "<iso8601>", "attempts": 3,
  "error_type": "invalid|permanent|retries_exhausted" }          // error_type: additive (2026-11)
// DLQ value when the original value is not a JSON object (bad JSON, array, …):
{ "raw_b64": "<base64 of the exact Kafka value>", "error": "...", "stage": "...", "failed_at": "...",
  "attempts": 0, "error_type": "invalid" }                        // additive (2026-11)
```

### 3.1 Validation, error classes and retries (amendment 2026-11 — backward compatible)

Each worker validates its inbound event with the pydantic models in
`services/platform-common/src/platform_common/events.py` (the executable form
of the schemas above). Unknown fields are ignored (forward compatible).

- `job_id` **must be a UUID** (as §1 always required). It becomes part of every
  object-storage key, so a missing/invalid `job_id` is never "guessed".
- `*_key` fields may be omitted (default `<prefix>/<job_id>`, as before) but if
  present must start with their stage prefix (`audio/`, `transcripts/`,
  `summaries/`, `speech/`).
- **Invalid messages** (bad JSON, not an object, schema violation) are
  dead-lettered with `error_type="invalid"`, `attempts=0` and then committed —
  never silently dropped. If the message carried a valid `job_id`, the job state
  is set to `failed`.
- **Permanent errors** (S3 `NoSuchKey`/`AccessDenied`, gateway 400/401/403/404/
  413/422, undecodable data) → DLQ immediately, `error_type="permanent"`.
- **Transient errors** (connection errors, timeouts, S3 5xx/throttling, Kafka
  delivery failures, gateway 5xx) → exponential back-off with jitter
  (`RETRY_BASE_DELAY_SECONDS`=2, `RETRY_MAX_DELAY_SECONDS`=30), bounded by
  `MAX_RETRIES` **and** a per-message wall-clock budget, then DLQ with
  `error_type="retries_exhausted"`. `attempts` is the real number of attempts.
- Each worker's `KAFKA_MAX_POLL_INTERVAL_MS` is validated at startup to exceed
  retry budget + slowest attempt + 60 s, and retry waits heartbeat the
  `/healthz` probe. SIGTERM aborts a retry wait without committing (the message
  is redelivered).

Consumers of the DLQ topics that only read the previously documented fields are
unaffected.

## 4. Object storage (S3 API; SeaweedFS in dev, S3 in prod — one bucket, prefixed)

- `audio/<job_id>` — uploaded audio (written by audio-api)
- `transcripts/<job_id>` — **full transcript text** (written by stt-worker; the
  full transcript is durably stored and independently retrievable, *not* just
  the summary). llm-worker reads it back by key.
- `summaries/<job_id>` — structured summary JSON (written by llm-worker)
- `speech/<job_id>` — synthesized audio (written by tts-worker)

Writes are idempotent (deterministic keys → re-processing overwrites). Workers
exchange **keys**, never inline payloads.

**S3 client config (env, identical across all four services):**
- `S3_ENDPOINT_URL` — set to the dev object store URL (`minio` compat Service in front of SeaweedFS); **unset** in prod (use AWS default).
- `S3_BUCKET` — bucket name (dev: `audio-pipeline`; prod: from the Crossplane bucket connection secret).
- `S3_REGION` — default `eu-west-1`.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — dev: credentials from the `minio-credentials` Secret; prod: IRSA (unset).
- Client built with boto3; in dev pass `endpoint_url=S3_ENDPOINT_URL` and `config=Config(s3={"addressing_style":"path"})` for the dev store.

## 5. Job-state store (Redis — owned by Chunk 1)

- Key: `job:<job_id>` (Redis string holding JSON), TTL ≈ 7 days (`604800` s).
- Value:
  ```jsonc
  { "status": "queued|transcribing|summarizing|synthesizing|done|failed",
    "stage": "audio-api|stt|llm|tts",
    "updated_at": "<iso8601>",
    "keys": { "audio": "...", "transcript": "...", "summary": "...", "speech": "..." },
    "error": { "stage": "...", "message": "...", "dlq_topic": "..." }   // only when failed
  }
  ```
- **Status transitions:** audio-api writes `queued` on upload; stt-worker writes
  `transcribing` then leaves it for llm; convention: each worker writes the
  *in-progress* status when it starts and updates `keys`/terminal status as it
  completes. Terminal `done` written by tts-worker; any worker writes `failed`
  with `error.{stage,message,dlq_topic}` when it dead-letters.
- audio-api `GET`s `job:<job_id>` for the status endpoint.
- **Redis client config (env):** `REDIS_URL` (e.g. `redis://redis.platform.svc.cluster.local:6379/0`)
  plus `REDIS_PASSWORD` from the `redis-auth` Secret. Use the `redis` Python client.
  `JOB_STATE_TTL_SECONDS=604800`.
- **Stage dedup (amendment 2026-11):** before processing, each worker claims
  `dedup:<stage>:<job_id>` atomically with `SET … NX EX <lease>` (lease =
  `DEDUP_LEASE_SECONDS`, default max.poll.interval + 60 s). On success or
  dead-letter the key becomes `done` with TTL `JOB_STATE_TTL_SECONDS`; on a
  crash the lease simply expires. A message whose job is claimed by another live
  consumer is not committed but re-polled later. Legacy value `"1"` is read as
  `done`.

## 6. LLM contract

- llm-worker calls the **existing `llm-gateway`** (OpenAI-compatible
  `POST /v1/chat/completions`) via `LLM_GATEWAY_URL`
  (`http://llm-gateway.apps.svc.cluster.local:80`). **Never vLLM directly.**
- Prompt produces a structured meeting summary; llm-worker parses an executive
  summary + `action_items[]`, writes the full summary JSON to `summaries/<job_id>`,
  and puts `action_items` on the event.
- *(amendment 2026-11)* **Auth:** requests carry `Authorization: Bearer
  <LLM_GATEWAY_API_KEY>` (Secret `llm-gateway-api-keys`, key `llm-worker`).
- *(amendment 2026-11)* **Scale-from-zero:** gateway 429/502/503/504 and
  connection errors are retried for up to `LLM_COLD_START_BUDGET_SECONDS`
  (default 600), honouring `Retry-After`; other 4xx are permanent.
- *(amendment 2026-11)* **Context window / prompt injection:** transcripts
  longer than `LLM_MAX_INPUT_CHARS` (default 4000 ≈ TinyLlama's 2k context minus
  output) are summarised map-reduce in at most `LLM_MAX_CHUNKS` chunks (beyond
  that: truncated + warning + `llm_transcript_truncated_total`). The transcript
  is wrapped in `<transcript>` delimiters and the system prompt tells the model
  to treat it strictly as data.

## 7. Metric names (the contract; producers emit, dashboard reads)

| Service | Metrics |
|---------|---------|
| stt-worker | `stt_jobs_total{status}`, `stt_job_duration_seconds`, `stt_errors_total`, `stt_transcript_bytes` (size of the written transcript) |
| llm-worker | `llm_jobs_total{status}`, `llm_tokens_generated_total`, `llm_request_duration_seconds`, `llm_summary_bytes` (size of the written summary JSON); additive: `llm_transcript_chunks`, `llm_transcript_truncated_total` |
| tts-worker | `tts_jobs_total{status}`, `tts_generation_duration_seconds` |
| pipeline (all workers contribute) | `pipeline_jobs_completed_total`, `pipeline_jobs_failed_total{stage}`, `pipeline_end_to_end_duration_seconds`, `pipeline_queue_wait_seconds{stage}` (now − event `created_at` at receipt) |

> `pipeline_queue_wait_seconds{stage=stt\|llm\|tts}` is the **shared queue-wait
> histogram** — defined once in `platform_common.pipeline_metrics` (like
> `pipeline_jobs_failed_total`) and observed once per message, right after dedup,
> using the same `parse_created_at` helper that the e2e metric uses. These are
> **additive** producer-side metrics: no Kafka envelope or event-value change, so
> the frozen seam in §1–§3 is untouched.

`pipeline_jobs_completed_total` / `pipeline_end_to_end_duration_seconds` are
incremented by **tts-worker** (the terminal stage; end-to-end measured from the
job `created_at` in the original envelope, propagated through the events — each
event carries `created_at` forward so the terminal stage can compute it).
`pipeline_jobs_failed_total{stage}` incremented by whichever worker dead-letters.

> To carry `created_at` to the terminal stage, **every event value also includes
> the original `created_at`** (added to the transcript/summary/results schemas in
> addition to the fields shown in §3). Workers copy it forward.

## 8. Naming / placement conventions (from AGENTS.md)

- Source: `services/<svc>/` (uv project: `main.py`, `pyproject.toml`, `uv.lock`,
  `test_main.py`, `conftest.py` setting `OTEL_SDK_DISABLED=true`, `Dockerfile`,
  `catalog-info.yaml`).
- Manifests: `services/<svc>/k8s/base/` (Deployment/Svc/ServiceMonitor/NetworkPolicy/PDB;
  for workers also `scaledobject.yaml`, `serviceaccount.yaml`).
- Per-env overlays: `services/<svc>/k8s/overlays/{dev,prod}/` (own kustomization +
  patches; prod pins image to ECR digest placeholder `unpromoted`, dev `:latest`).
- ArgoCD: `kubernetes/apps/<svc>/applicationset.yaml` (cluster generator,
  `path: services/<svc>/k8s/overlays/{{.metadata.labels.environment}}`, project
  `platform-apps`, namespace `apps`).
- Env wiring in base manifests: `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT`
  (`http://otel-collector.monitoring.svc.cluster.local:4317`),
  `OTEL_RESOURCE_ATTRIBUTES=service.namespace=apps`,
  `KAFKA_BOOTSTRAP_SERVERS=platform-kafka-kafka-bootstrap.platform.svc.cluster.local:9092`.

## 9. Decisions locked for this build

> **Deployment status clarification (2026-11-09; not a wire-contract amendment):**
> The backend and GPU statements below record the original intended build.
> Executable [STT configuration](../services/stt-worker/k8s/base/deployment.yaml)
> and [TTS configuration](../services/tts-worker/k8s/base/deployment.yaml) currently
> select `stub` in **both dev and prod**; the prod overlays do not enable NeMo or
> Kokoro. Standard images omit those optional ML dependencies. Real backends
> require purpose-built images and explicit backend/scheduling/resource settings,
> and have not been verified here. GPU sizing below is an intended scenario, not
> evidence of deployed STT GPU capacity. This note changes no event schema, Kafka
> key, state transition, metric name, or other frozen contract requirement.

- **Job state:** Redis (per §5).
- **Object storage:** SeaweedFS dev (net-new platform service), Crossplane v2 namespaced `ObjectStorageBucket` prod.
- **STT dev model:** no GPU on kind → stt-worker has a pluggable backend
  `STT_BACKEND=stub|nemo`; **dev overlay sets `stub`** (returns placeholder
  transcript — pipeline/wiring demo, not quality), prod sets `nemo` (Parakeet on GPU).
- **TTS dev model:** pluggable `TTS_BACKEND=stub|kokoro`; dev `stub`, prod `kokoro` (CPU).
  Heavy ML deps (nemo/kokoro/torch) are **lazy-imported only when the backend is
  selected** and are NOT in `pyproject.toml`/`uv.lock` (kept light + testable);
  prod images bake the model + add the deps via a build arg / extra layer. Document this.
- **GPU NodePool sizing:** default = **one GPU pod per node**; Karpenter scales the
  `gpu` pool to **2 nodes** when both STT and vLLM are active. `nvidia.com/gpu: 1`
  per GPU pod. (Co-scheduling/MPS rejected for v1.)
- **Prod scaling posture:** stt-worker + llm-worker `minReplicaCount: 0` (scale-to-zero,
  generous KEDA `cooldownPeriod: 300`), accepting documented GPU cold start;
  tts-worker CPU scale-to-zero.
