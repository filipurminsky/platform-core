# Code review findings — platform-core

Scope: all service source (`services/*`), Kubernetes manifests (bases + overlays), ArgoCD ApplicationSets,
Kafka/KEDA/Kyverno/OPA platform config, observability rules, CI workflows, scripts, and Terraform.
Sorted by severity; within a severity, by importance. File references are clickable (`path:line`).

Legend: **Critical** = a headline feature of the project does not work as claimed, or prod is broken.
**High** = correctness/data-loss/security defect. **Medium** = real defect with bounded blast radius or
operational risk. **Low** = hygiene, drift, polish.

> **Status update (2026-06-11):** C1–C3 and H1–H7 are fixed (see the working tree / git history).
> C2b was resolved by aligning config with the shipped image — all environments now explicitly run
> the `stub` backends until a GPU image build pipeline exists (TODO.md §B); the nemo/kokoro
> re-enable path is documented in the manifests and Dockerfile. H7 trades the shared model-cache
> PVC for per-pod ephemeral volumes (cold-start download cost accepted; documented in the
> manifest). Medium and Low findings are still open.

---

## Critical

### C1. The canary SLO gate is inert — analysis can never fail, bad images promote

`services/echo-service/k8s/base/analysistemplate.yaml:26` scopes every Prometheus query to
`rollouts_pod_template_hash="{{args.canary-hash}}"`. That label only exists on the metrics if the
ServiceMonitor copies the pod label onto scraped series via
`podTargetLabels: [rollouts-pod-template-hash]` — and
`services/echo-service/k8s/base/servicemonitor.yaml` **does not set `podTargetLabels` at all** (no
ServiceMonitor in the repo does; `grep -r podTargetLabels` only matches comments).

Consequence: every analysis query returns an **empty result**, and the success conditions are written as
`len(result) == 0 || result[0] >= 0.99` — empty result is treated as success. The SLO gate therefore
passes unconditionally:

- A broken image promotes 10% → 50% → 100% with green analysis runs.
- `./scripts/canary-demo.sh bad` should also fail its assertion (the rollout reaches `Healthy`, not
  `Degraded`), so the flagship demo doesn't demonstrate what it claims.

This is doubly painful because `AGENTS.md` explicitly documents the `podTargetLabels` wiring as a
load-bearing invariant ("If you change the ServiceMonitor, keep that") — the docs describe configuration
that isn't in the repo.

**Fix:** add `podTargetLabels: ["rollouts-pod-template-hash"]` to the echo-service ServiceMonitor, and
consider tightening the success conditions so that *sustained* empty results fail (empty is only
acceptable during the no-traffic warm-up window; `count: 4` with all four empty should not pass).
Then re-run `canary-demo.sh bad` as the regression proof.

### C2. The prod audio pipeline is broken end-to-end — three independent blockers

Each of these alone makes every prod job fail; together they show the prod path was never exercised
(TODO.md §B acknowledges "unverified", but these are concrete, findable-by-inspection defects, not just
missing verification):

1. **No S3 credentials in prod.** The workers/audio-api are documented to use IRSA against the
   Crossplane bucket, but `terraform/modules/iam/main.tf` has no IAM role granting
   `s3:GetObject/PutObject` to any app service account, and none of the prod overlays annotate the
   ServiceAccounts with `eks.amazonaws.com/role-arn` (`grep -r eks.amazonaws.com services/` → no hits).
   The Crossplane role only manages *bucket* lifecycle, not object access. In prod, every
   `put_object/get_object` will fail with `NoCredentialsError`/`AccessDenied`.

2. **Prod images don't contain the ML backends.** `services/stt-worker/k8s/base/deployment.yaml:68`
   sets `STT_BACKEND=nemo` (and tts-worker sets `kokoro`), but the `nemo` Docker build stage is
   entirely commented out (`services/stt-worker/Dockerfile:51-75`) and `docker-build.yaml` has no
   `--target` selection — the comment "The prod CI pipeline selects this target when STT_BACKEND=nemo"
   is false. The promoted prod image is the stub-only image; the lazy `import torch` raises on the
   first message and every job DLQs after 3 retries.

3. **llm-worker requests a model prod vLLM doesn't serve.** The base sets
   `LLM_MODEL=TinyLlama/TinyLlama-1.1B-Chat-v1.0` (`services/llm-worker/k8s/base/deployment.yaml:59`)
   and the prod overlay never patches it, while prod vLLM serves
   `mistralai/Mistral-7B-Instruct-v0.2`. vLLM's OpenAI endpoint 404s on unknown model names → every
   summarization job fails → DLQ.

**Fix:** add an IRSA module for the four audio SAs (one role, scoped to the pipeline bucket prefix) +
SA annotations in the prod overlays; implement the `--target nemo`/`kokoro` build path (or change the
prod env to `stub` until it exists, so prod is honest); patch `LLM_MODEL` in the prod overlay.

### C3. vLLM scale-from-zero deadlock — the KEDA trigger metric only exists while vLLM is running

`services/vllm-inference/k8s/base/scaledobject.yaml:20` activates 0→1 on the Prometheus query
`vllm:num_requests_waiting`. That is a **vLLM server metric** — it is only exposed by a running vLLM
pod. At `minReplicaCount: 0` there are no pods, the series is absent, KEDA reads 0, and activation can
never fire. Requests through llm-gateway meanwhile fail with 502 (no endpoints), which also never
increments the metric. The gateway's `/readyz` design comment ("the very traffic that would trigger
KEDA to scale the upstream back up") describes a mechanism that doesn't exist.

**Fix:** activate on a metric that exists when vLLM is at zero — e.g. a gateway-side gauge/counter of
requests to `/v1/*` (llm-gateway already counts `PROXY_REQUESTS`; `rate()` over it works), or
`UPSTREAM_ERRORS` as a proxy, or an HTTP add-on/scaler. Keep `vllm:num_requests_waiting` for the 1→N
scaling, use the gateway metric for 0→1 activation.

---

## High

### H1. Consumers commit offsets without confirming the output event was delivered — message loss

All four Kafka consumers produce their output (or DLQ) event with `producer.produce(...)` followed by
`producer.flush(timeout=5)` and **ignore the flush return value and use no delivery callback**
(e.g. `services/stt-worker/main.py:183-189`, same in llm-worker, tts-worker, worker-service DLQ path).
`flush()` returns the number of messages *still undelivered*; on broker unavailability or
`MSG_SIZE_TOO_LARGE` the produce silently fails, `process_message` returns "success", and the caller
commits the offset (`main.py:316`). The input message is consumed, the output never exists — the job
vanishes mid-pipeline. This directly violates the documented invariant "commit only **after** the
output event is produced" — the code commits after *attempting* to produce.

audio-api does this correctly (`services/audio-api/main.py:256-278` — delivery callback + remaining
check + 503). **Fix:** apply the same pattern in the workers: register an `on_delivery` callback,
check `flush()`'s return, and raise (so the retry/DLQ path engages and the offset is *not* committed)
when delivery is unconfirmed.

### H2. Several alerts can never fire (and one will false-fire) — silent monitoring gaps

`observability/prometheus/rules/alerts.yaml`:

- **`KafkaConsumerLagHigh` (line 78) and the recording rules** filter on `{group="worker-service"}`,
  but the Strimzi Kafka Exporter labels consumer groups `consumergroup=`, not `group=`. The query
  matches nothing → the alert is permanently silent. Same bug in
  `observability/prometheus/slo/recording-rules.yaml:69-73`.
- **All four DLQ alerts** (`KafkaDLQNonEmpty`, `AudioSTTDLQNonEmpty`, …) use
  `kafka_consumergroup_lag{topic="<dlq>"}`. Consumer-group lag only exists for topics **some group
  consumes** — nothing consumes the DLQs, so the series never exists and the alerts never fire. Use a
  produced-offset signal instead, e.g.
  `sum(delta(kafka_topic_partition_current_offset{topic="audio.stt-dlq"}[5m])) > 0`.
- **`GpuNodeUnavailable` (line 129)** joins on `kube_node_labels{label_role="gpu"}` — kube-state-metrics
  v2 does not expose arbitrary node labels unless `--metric-labels-allowlist` is configured (it isn't in
  the monitoring values). The right-hand side of `unless` is therefore empty, so the alert **fires
  whenever vLLM has > 0 replicas**, even when GPU nodes are healthy — a guaranteed false positive that
  trains operators to ignore criticals.

The lag alerts being dev-dead (exporter disabled) is already noted in TODO.md; the label bugs above
make them prod-dead too.

### H3. NetworkPolicies cut off the tracing pipeline (and are inconsistently strict)

Every audio service and worker-service exports OTLP to `otel-collector.monitoring:4317`, but their
egress NetworkPolicies only allow: platform ns (9092/9093/6379/9000), DNS, and 443
(`services/stt-worker/k8s/base/networkpolicy.yaml:17-36`, same for llm/tts/audio-api/worker-service).
There is **no egress rule for monitoring:4317**, so on dev (where Cilium actually enforces these) every
span export is dropped and the "one trace across four Kafka hops" showcase silently doesn't work.
Meanwhile `echo-service` and `llm-gateway` use `egress: - {}` (allow-all) — so the posture is both
broken and inconsistent. **Fix:** add a `to: monitoring` / `port: 4317` egress rule to the five
restricted policies (or standardize on one egress posture per tier and document it).

### H4. ArgoCD will fight KEDA over `spec.replicas` — `ignoreDifferences` without `RespectIgnoreDifferences`

The worker ApplicationSets set `ignoreDifferences: [/spec/replicas]` with `selfHeal: true` and
`ServerSideApply=true` (`kubernetes/apps/stt-worker/applicationset.yaml:26-44`) — but
`ignoreDifferences` only affects **diffing**, not **syncing**. Without
`syncOptions: [RespectIgnoreDifferences=true]`, every actual sync (any commit touching the app, or
self-heal on another field) re-applies the manifest including `replicas: 0` from git
(`overlays/prod/kustomization.yaml` pins `replicas: count: 0`), scaling running workers to zero
mid-backlog until KEDA re-activates. **Fix:** add `RespectIgnoreDifferences=true` to all
KEDA-managed apps — or better, omit `replicas` from the manifests entirely (SSA then leaves the field
to the HPA's field manager), which is the documented Argo+KEDA pattern.

### H5. Prod `audio-api` runs with authentication disabled behind a public Ingress

`API_TOKEN` defaults to empty → `_check_auth` is skipped entirely
(`services/audio-api/main.py:152-154`). The base deployment sets no `API_TOKEN`, and
`services/audio-api/k8s/overlays/prod/patch-prod-env.yaml` adds Kafka/S3 env but **never wires
`API_TOKEN`** — so prod accepts unauthenticated 25 MB uploads from anyone who can reach the Ingress
(`k8s/base/ingress.yaml`, no TLS, no auth annotations). Worse, the patch's own documentation comment
(line 34) claims the base env order starts with `API_TOKEN=0, MAX_UPLOAD_BYTES=1, …` — none of those
exist in the base (`deployment.yaml:42-72` starts `ENVIRONMENT, LOG_LEVEL, …`); the comment is stale
fiction that happens to land on the right indices 7/8 by coincidence. **Fix:** add `API_TOKEN` from a
Secret (ESO) in the prod overlay, fail-closed in prod (refuse to start without it, keyed off
`ENVIRONMENT`), add TLS to the Ingress, and fix the comment (see M7 on positional patches generally).

### H6. NetworkPolicies are not enforced at all in prod

Prod EKS uses the stock `vpc-cni` addon (`terraform/modules/eks/main.tf:25-29`) with no
`enableNetworkPolicy` configuration and no other policy engine — so every NetworkPolicy in the repo
(zero-trust tenants, per-service segmentation, the OPA relationship checks gating CI) is a **no-op in
prod**. Enforcement only exists on local kind via Cilium. The README/AGENTS zero-trust narrative is
therefore dev-only. **Fix:** enable VPC CNI network policy support
(`configuration_values = jsonencode({enableNetworkPolicy = "true"})`) or deploy Cilium on EKS, and say
which one the design intends.

### H7. vLLM `maxReplicaCount: 4` with a single ReadWriteOnce PVC

`services/vllm-inference/k8s/base/pvc.yaml` is `accessModes: [ReadWriteOnce]` (gp3/EBS), and the
Deployment mounts it in every replica, but the ScaledObject allows up to 4 replicas
(`scaledobject.yaml:12`). Any scale-out beyond one node deadlocks in `Multi-Attach error` —
replicas 2–4 stay `ContainerCreating` forever (and on a GPU NodePool that means provisioned,
billed, idle GPU nodes). **Fix:** per-pod model cache (emptyDir/ephemeral volume + image-baked or
S3-pulled weights), RWX storage, or `maxReplicaCount: 1` until solved.

---

## Medium

### M1. Kafka SASL credentials and data go over plaintext in prod

All prod overlays set `KAFKA_SECURITY_PROTOCOL=SASL_PLAINTEXT` against the un-TLS'd 9092 listener
(`services/stt-worker/k8s/overlays/prod/patch-kafka-auth.yaml:12-13`), while the header comment claims
"Prod uses Strimzi's TLS/SCRAM listener". SCRAM doesn't reveal the password, but all message payloads
traverse the cluster unencrypted, and SCRAM-over-plaintext is MITM-able. The TLS listener (9093) exists
and is unused. KEDA's trigger likewise polls 9092. **Fix:** point bootstrap at 9093 with
`SASL_SSL` + the Strimzi cluster CA (mounted from the `<cluster>-cluster-ca-cert` Secret), and set
`tls: enable` + CA on the KEDA trigger; or delete the misleading comments and document the tradeoff.

### M2. llm-gateway rate limiter: spoofable key and unbounded memory

- `_client_ip` trusts `X-Real-IP`/`X-Forwarded-For` from the request (`services/llm-gateway/main.py:93-101`).
  Any in-cluster caller (the netpol allows whole-namespace ingress, see M5) — or any client whose
  ingress doesn't overwrite these headers — can rotate fake IPs to bypass the limit entirely.
- Each new key creates a deque in `self._windows` that is **never evicted**
  (`app/rate_limiter.py:22`); rotating spoofed IPs grows the dict without bound → OOM (the gateway has a
  256 Mi-class limit).

**Fix:** only honor forwarding headers from the trusted ingress hop, and evict empty/idle windows
(drop the entry when the deque empties, or sweep periodically).

### M3. llm-gateway leaks upstream connections on client disconnect

`_stream_body` only closes the upstream response after full iteration
(`services/llm-gateway/main.py:190-193`). If the client disconnects mid-SSE or `aiter_bytes()` raises,
`aclose()` never runs and the pooled connection leaks (pool max 100, then the gateway wedges). **Fix:**
wrap in `try/finally`, or pass `background=BackgroundTask(upstream_resp.aclose)` to the
`StreamingResponse`. Also: 502/504 outcomes are not recorded in `PROXY_REQUESTS`/`PROXY_LATENCY`
(only `UPSTREAM_ERRORS.inc()`), so error rate and latency SLIs under-count exactly when it matters.

### M4. PDB `minAvailable: 1` on scale-to-zero / single-replica workers blocks node drains

All four audio workers ship a PDB with `minAvailable: 1` (`services/stt-worker/k8s/base/pdb.yaml`).
When KEDA runs them at 1 replica, the PDB makes that pod **unevictable** — `kubectl drain`, managed
node-group upgrades, and Karpenter consolidation all wedge (on GPU nodes this pins the most expensive
node in the fleet). The in-file comment ("harmless when KEDA scales to zero") covers scaling but not
eviction. Note the OPA exemption (`policy/network-policy.rego:52-54`) only exempts names starting
`vllm`/`worker` — the audio workers were instead "fixed" by adding the harmful PDB to satisfy the
warn. **Fix:** either exempt scale-to-zero consumers from PDBs entirely (Kafka redelivery already
covers eviction mid-job) or use `maxUnavailable: 1`.

### M5. vllm-inference NetworkPolicy allows the whole `apps` namespace, voiding the gateway-only contract

`services/vllm-inference/k8s/base/networkpolicy.yaml:11-19` lists `podSelector: app=llm-gateway` and
`namespaceSelector: apps` as **two separate `from` peers (OR)** — so every pod in `apps` can reach
vLLM:8000 directly, contradicting contract §6 ("workers never call vLLM directly") and the comment in
the llm-gateway policy that claims gateway-only ingress. If the intent was "llm-gateway pods in apps",
both selectors must be in **one** `from` element. (Related: the tenant template's `allow-to-platform`
opens vLLM:8000 toward the `platform` namespace, but vLLM lives in `apps` — already tracked in
TODO.md §D.)

### M6. Worst-case message handling exceeds `max.poll.interval.ms` → rebalance storm

llm-worker: 3 attempts × (`LLM_TIMEOUT` 120 s + S3 I/O) + backoff ≈ 360 s+, against
`max.poll.interval.ms = 300_000` (`app/kafka_io.py:59`). A slow/unresponsive gateway gets the consumer
evicted from the group mid-processing; the message is redelivered to another pod which hits the same
slow upstream — a rebalance/duplicate-work loop, and the eventual `consumer.commit` raises on the
revoked partition, crashing the pod. Same math threatens stt/tts with real GPU models. **Fix:** bound
total retry time below the poll interval (budget-based retries), or raise `max.poll.interval.ms`
to cover the worst case, and treat `commit` failures as expected after rebalance.

### M7. Positional JSON6902 env patches are a footgun (and one comment is already wrong)

Prod overlays patch env vars **by array index** (`patch-s3-prod.yaml: /env/11`,
`patch-prod-env.yaml: /env/7, /env/8`, dev `patch-dev.yaml: /env/7`). Inserting one env var in a base
deployment silently rewires the wrong variable in prod — the worst failure mode (no error, wrong
config). The audio-api patch already carries a stale order comment (see H5). **Fix:** replace with
strategic-merge patches on `env` (merge key is `name`, no indices needed) — the Trivy-scan concern that
motivated op-lists applies to whole-container patches, not env-only SMPs.

### M8. Redis job-state store is unauthenticated, unrestricted, and volatile

`kubernetes/platform/redis/manifests/redis.yaml`: no AUTH, **no NetworkPolicy anywhere in
`kubernetes/platform/`** (any pod in any namespace can read/rewrite all job state — including marking
jobs done/failed), single replica with `emptyDir` and persistence off — a restart 404s every in-flight
job's status while processing continues (confusing for API consumers). Volatility is a documented demo
tradeoff; the missing ingress restriction + auth is not. **Fix:** at minimum a platform-ns
NetworkPolicy allowing 6379 only from the five client apps; `requirepass` from a Secret is cheap.

### M9. ECR repositories are not provisioned anywhere

`docker-build.yaml` pushes to `platform-core/<app>` ECR repos, but no tracked Terraform creates
`aws_ecr_repository` (only `.terraform/` module-cache hits). First prod push fails until someone
hand-creates seven repos — and hand-created repos have no immutability, lifecycle policy, or
scan-on-push config. **Fix:** an `ecr` module (for_each over the app list) with
`image_tag_mutability = IMMUTABLE` (you promote by digest anyway) + lifecycle policy.

### M10. CI OIDC role is assumable from any ref; signature policy accepts any workflow

`subject_filter` defaults to `*` (`terraform/modules/github-oidc/variables.tf:12`) and prod leaves it
commented (`environments/prod/github-oidc.tf:9`) — any branch, tag, or PR workflow in the repo can
assume the ECR-push role and produce a **validly signed** image (the Kyverno attestor subject is
`.github/workflows/*`, `kubernetes/platform/kyverno-policies/base/verify-image-signatures.yaml:38`).
The supply-chain story is only as strong as this subject. **Fix:** `subject_filter =
"ref:refs/heads/main"` and pin the Kyverno subject to the specific workflow file.

### M11. `docker-compose.yml` is broken: Confluent images have no `*.sh` tools

`services/docker-compose.yml:43-46` runs `kafka-topics.sh` inside `confluentinc/cp-kafka` — Confluent
images ship `kafka-topics` (no suffix; the healthcheck on line 31 gets it right). `kafka-init` exits
non-zero → `worker-service` (gated on `service_completed_successfully`) never starts. The usage header
(`kafka-console-producer.sh`) has the same bug. Also, none of the four audio services are in the
compose file despite AGENTS.md selling it as "running the stack locally".

### M12. `smoke-test.sh` looks for Kafka in the wrong namespace and asserts a path it didn't test

- `KAFKA_NS="kafka"` (`scripts/smoke-test.sh:19`) but the Strimzi cluster lives in `platform` — the
  pre-flight fails on every cluster this repo builds.
- The "DLQ poison path" publishes malformed JSON, which `process_message` deliberately logs-and-skips
  (never DLQs — `services/worker-service/main.py:53-57`). The script even warns about this, then the
  summary unconditionally prints "A2: DLQ path — poison message handled". To exercise the DLQ, publish
  a *valid* job whose handler raises (e.g. `data-transform` with a bad payload).

### M13. tts-worker treats a Redis blip as a job failure (inconsistent with its siblings)

stt/llm `job_state` helpers swallow Redis errors by design ("never raise"), but tts-worker's
`_get_state/_save_state` (`services/tts-worker/app/job_state.py:25-31`) propagate — and
`set_synthesizing` is called *inside* the retry loop (`main.py:137`), so a transient Redis outage burns
all 3 attempts and DLQs a job whose actual processing would have succeeded. Align on the
log-and-continue policy (state writes are best-effort by contract).

### M14. `seen_ids` dedup sets grow without bound

Every consumer accumulates `seen_ids` forever (`services/stt-worker/main.py:285` et al.). Long-lived
pods (audio-api aside, workers can run for days under steady load) leak memory and the "dedup" gets
slower-growing-set semantics nobody chose. **Fix:** a bounded structure (e.g. `OrderedDict` LRU of the
last N job ids) — or move dedup to Redis `SETNX job:<id>:done`, which also survives restarts/rebalances
and actually delivers the idempotency the README implies.

### M15. echo-service metrics use the raw URL path as a label — unbounded cardinality

`services/echo-service/main.py:39-41` labels `REQUEST_COUNT`/`REQUEST_LATENCY` with `request.url.path`,
so any path-scanning traffic mints unlimited series (llm-gateway solved this with `_normalize_path` —
reuse it; or label with the route template). Exceptions from `call_next` also bypass metrics, so 500s
from crashes are uncounted.

### M16. Hidden cross-app dependency: `env-config` is generated by the worker-service Application

Every audio service hard-depends on the `env-config` ConfigMap via non-optional `configMapKeyRef`, but
only worker-service's overlay generates it (`services/worker-service/k8s/overlays/dev/kustomization.yaml:25`).
Sync ordering on a fresh cluster (or pruning/renaming the worker-service app) leaves four other
Applications with unstartable pods, and nothing in the manifests reveals why. **Fix:** make the
references `optional: true` with in-code defaults (the apps already default `ENVIRONMENT`/`LOG_LEVEL`),
or move env-config into its own tiny config Application owned by the platform.

### M17. audio-api does blocking I/O on the event loop

`create_job` runs `s3_client.put_object`, `redis_client.setex`, and `redis_client.get` (in `get_job`)
synchronously inside `async def` handlers (`services/audio-api/main.py:216-239,301`); only the Kafka
flush was moved to an executor. A slow S3/MinIO or Redis call stalls the **entire** event loop —
including `/healthz`/`/readyz` — so one slow dependency turns into failed probes and a restart loop
under load. **Fix:** `run_in_executor` for the boto3/redis calls (matching the flush), or use aioboto3 /
redis.asyncio. Related smaller gap: if the Kafka produce fails after S3+Redis writes, the request 503s
but the orphaned S3 object and a never-progressing `queued` Redis record linger for 7 days — consider
deleting/marking failed in the error path.

---

## Low

- **L1. Alert polish:** `ArgoCDSyncFailed` actually matches `health_status="Degraded"` (sync ≠ health;
  use `argocd_app_info{sync_status="OutOfSync"}` or rename); `NodeMemoryPressure` templates
  `{{ $labels.node }}` but node-exporter series carry `instance`; `HighErrorRate` is a 14× burn-rate
  alert with `for: 1h` on a 1 h window — a fast burn should page in minutes (use the standard 5m+1h
  multiwindow pair).
- **L2. `.gitignore` excludes `.terraform.lock.hcl`** — provider lock files should be committed for
  reproducible `terraform init` (the repo pins everything else religiously; this is the one gap).
- **L3. Pre-commit local hooks cover only 3 of 7 services** (`.pre-commit-config.yaml` `uv-lock-check`
  and `pytest` loops stop at echo/worker/llm-gateway — the four audio services were never added).
  Derive the list from `services/*/` or reuse the Makefile `APPS` var.
- **L4. Doc drift (AGENTS.md/README vs reality):** dev vLLM is described as "CPU TinyLlama in dev", but
  the dev gateway actually points at the `vllm-mock` Deployment because the vLLM image can't run on
  CPU (`services/llm-gateway/k8s/overlays/dev/mock-vllm.yaml` says so explicitly) — the dev
  `vllm-inference` Deployment is dead weight that can never become Ready on kind; AGENTS.md claims
  `bootstrap.sh` creates the HF-token secret via `kubectl create secret` — it doesn't;
  `rollout.yaml`'s header says p90, the template gates p95.
- **L5. `LOG_LEVEL` is wired into every Deployment but read by nothing** — structlog is configured with
  no level filtering (`app/observability.py`), so the env var is dead config. Either honor it
  (`structlog.make_filtering_bound_logger`) or drop it (and then M16 mostly evaporates).
- **L6. llm-worker `mark_done()` doesn't mark done** (`app/job_state.py:46` — it sets
  `status="summarizing"` by design). Correct behavior, misleading name; rename to
  `mark_summarized`/`set_summary_key`.
- **L7. Worker liveness is a no-op:** the metrics-server thread answers `/healthz` even if the consume
  loop is wedged (e.g. stuck commit, deadlocked poll). Track a "last poll heartbeat" timestamp and fail
  the probe when stale.
- **L8. Duplicate deployment paths:** `helm/demo-app` and `helm/vllm` parallel the kustomize trees that
  ArgoCD actually deploys, and `helm/platform-services` is a stub (tracked in TODO §C) — drift bait for
  reviewers and Renovate; delete or clearly mark as packaging examples.
- **L9. MinIO dev credentials (`minio`/`minio123`) are inlined in three places** (minio values, audio
  worker dev patches) — fine for kind, but a single generated Secret would also exercise the same env
  plumbing prod uses, making dev a better rehearsal.
- **L10. CI runs each app's tests twice on main pushes** (ci.yaml `app-test` + docker-build.yaml `test`
  gate). Cheap, but a `workflow_call` reuse would halve the matrix.
- **L11. KafkaTopic `jobs` retention comment vs DLQ:** main topics 7 d, DLQs 30 d is sound, but nothing
  replays DLQs (no tooling, no runbook step) — dead letters currently age out silently after 30 days.
  Worth a runbook + a replay one-liner to complete the story the alerts (H2) start.

---

## Suggested fix order

1. C1 (one-line ServiceMonitor fix; restores the project's headline demo) — then re-run `canary-demo.sh bad`.
2. H1 + H4 (delivery-confirmed produce; `RespectIgnoreDifferences`) — data-integrity of the pipeline.
3. C3 + H7 (vLLM activation metric + PVC) — makes scale-to-zero real.
4. C2 + H5 (prod IRSA, model/backends, API token) — makes the prod story honest; alternatively, scope
   docs to "dev-verified, prod authored-but-unverified" until done.
5. H2 + H3 (alert labels, OTLP egress) — observability that actually observes.
6. The Medium batch — mostly small, high-leverage diffs (M7's strategic-merge conversion removes a
   whole failure class).
