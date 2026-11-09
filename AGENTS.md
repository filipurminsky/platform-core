# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Validation commands (run locally before pushing)

```bash
# Kustomize — build and validate manifests
kustomize build kustomize/validation/dev  | kubeconform -strict -summary -kubernetes-version 1.36.0 -schema-location default
kustomize build kustomize/validation/prod | kubeconform -strict -summary -kubernetes-version 1.36.0 -schema-location default
kustomize build kubernetes/platform/kafka/overlays/dev  | kubeconform -strict -summary -kubernetes-version 1.36.0
kustomize build kubernetes/platform/kafka/overlays/prod | kubeconform -strict -summary -kubernetes-version 1.36.0
# CI resolves CRD kinds (Strimzi, KEDA, Gateway API, Envoy Gateway, ESO, Argo Rollouts)
# from schemas it converts from the pinned CRDs — see .github/actions/setup-kubeconform.

# Helm — lint all charts
for chart in helm/*/; do helm lint "$chart"; done

# Helm — render and validate (requires kubeconform)
helm template demo helm/demo-app --set image.repository=registry.example.com/demo-app | kubeconform -strict -summary
helm template vllm helm/vllm     | kubeconform -strict -summary

# Terraform — format check and validate
terraform fmt -check -recursive terraform/
cd terraform/environments/dev  && terraform init -backend=false && terraform validate
cd terraform/environments/prod && terraform init -backend=false && terraform validate

# OPA policy check (requires conftest)
kustomize build kustomize/validation/dev | conftest test --combine --policy policy/ -

# kube-linter
kube-linter lint kubernetes/ --config .kube-linter.yaml

# Kyverno — check policies are well-formed (ValidatingPolicy / ImageValidatingPolicy, kyverno-cli ≥ 1.19)
kyverno apply kubernetes/platform/kyverno-policies/base/

# Argo Rollouts — watch / drive a canary (needs the kubectl plugin)
kubectl -n apps argo rollouts get rollout echo-service --watch
./scripts/canary-demo.sh bad     # deploy a broken image → SLO analysis auto-rolls-back

# k6 load test (drives the canary analysis + KEDA)
kubectl apply -k tests/load

# Bootstrap local cluster
./scripts/bootstrap.sh --mode=local

# Tear down
./scripts/teardown.sh --mode=local
```

**Pinned tool versions** (match what CI uses):
- Helm 4.3.1, Terraform 1.17.1 (config requires ≥ 1.10), kubeconform v0.8.0, Kubernetes target 1.36 (EKS + kindest/node v1.36.4, kind ≥ v0.33.0)
- Strimzi 1.3.1 (Kafka 4.3.2, `kafka.strimzi.io/v1`), KEDA 2.21.0, Gateway API v1.6.1 (standard), Envoy Gateway v1.10.0, External Secrets 2.14.0 (`external-secrets.io/v1`), Argo Rollouts v1.10.2
- Cilium 1.20.4, Argo CD chart 10.17.1, Trivy 0.76.2, Ruff 0.17.4. Compatibility holds and existing-cluster upgrade steps: `docs/tool-upgrades.md`.

---

## Architecture

### Where deployable manifests live

Application manifests are **co-located per service** under `services/<svc>/k8s/`
(`base/` + `overlays/{dev,prod}/`) — everything for a service lives in the one
folder it owns. Three independent kustomize hierarchies feed ArgoCD:

| Tree | Purpose | ArgoCD points here? |
|------|---------|---------------------|
| `services/<svc>/k8s/base/` + `k8s/overlays/{dev,prod}/` | App Deployments/Rollouts, Services, PDBs, NetworkPolicies, ServiceMonitors, ScaledObjects | Yes — each `kubernetes/apps/<svc>/applicationset.yaml` points at `services/<svc>/k8s/overlays/{{env}}` |
| `kubernetes/platform/kafka/base/` + `overlays/{dev,prod}/` | Strimzi Kafka, KafkaTopic, KafkaUser CRs | Yes — via the `kafka` ApplicationSet |
| `kubernetes/tenants/team-*/` | Per-team Namespace, ResourceQuota, LimitRange, NetworkPolicies, RBAC | Yes — via `tenants` ApplicationSet |

`kustomize/validation/{dev,prod}/` is a **CI-only** aggregate that composes every
service's overlay so kubeconform and the OPA `--combine` relationship checks see
the whole `apps` namespace at once. ArgoCD never points at it.

### Environment (dev vs prod) is cluster-labelled, not hardcoded

The app/kafka manifests are **ApplicationSets** with a **cluster generator** (`kubernetes/apps/*/applicationset.yaml`, `kubernetes/platform/kafka/applicationset.yaml`). Each templates the overlay path from the destination cluster's `environment` label:

```
path: services/echo-service/k8s/overlays/{{.metadata.labels.environment}}
```

`bootstrap.sh` sets that label by declaring an `in-cluster` cluster Secret: `--mode=local` → `environment=dev`, `--mode=aws` → `environment=prod`. **Switching environments never requires editing a manifest** — and the generator's `environment Exists` selector means no apps sync until the cluster is labelled. To flip a running cluster, relabel the secret:

```bash
kubectl label secret in-cluster -n argocd environment=prod --overwrite
```

### Where Kubernetes manifests actually live

- **`kubernetes/apps/<svc>/`** — the service's ArgoCD `ApplicationSet` only (GitOps registration: project, namespace, sync policy). **No Deployments or Services here.**
- **`services/<svc>/k8s/base/`** — the real Kubernetes manifests (Deployment/Rollout, Service, ServiceMonitor, NetworkPolicy, PDB, ScaledObject). Per-env patches live in `services/<svc>/k8s/overlays/{dev,prod}/`.
- **`kubernetes/platform/`** — one ArgoCD Application per platform service (cert-manager, envoy-gateway, argo-rollouts, external-secrets, keda, strimzi, kafka, reflector, crossplane, backstage, monitoring, loki, alloy, tempo, opentelemetry-collector, opencost, karpenter, dcgm-exporter, seaweedfs, redis, namespaces). Each points to its upstream Helm chart (or, for config-only slices, a repo path). Prod-only slices (crossplane, karpenter, **dcgm-exporter**) are ApplicationSets with a cluster generator scoped to `environment=prod`; **seaweedfs** is a **dev-only** ApplicationSet providing the S3 API behind a compatibility `Service minio` (`platform`, :9000) — MinIO community images are no longer published; prod uses the Crossplane S3 bucket instead; **redis** and **namespaces** run in all environments.

### Application source code & manifests (`services/<svc>/`)

Each service is **co-located in one folder it owns**: `services/<svc>/` holds the
*source code and container build* (top level) and its *Kubernetes manifests*
(`k8s/base/` + `k8s/overlays/{dev,prod}/`). The only piece kept outside is the
*ArgoCD ApplicationSet* (`kubernetes/apps/<svc>/applicationset.yaml`) — GitOps
registration stays platform-reviewed; its `path:` points back at the service's
overlay.

Each `services/<service>/` source is a self-contained Python project. The **demo apps**:

- **`echo-service`** — FastAPI HTTP demo (probes, `/metrics`, request echo). Deployed as an Argo Rollout (canary).
- **`worker-service`** — Kafka consumer (confluent-kafka, no web framework). Manual-commit processing with dedup, retry → DLQ, graceful SIGTERM drain; exposes metrics on `:9090`. Scaled by KEDA on consumer lag.
- **`llm-gateway`** — FastAPI reverse proxy in front of vLLM: bearer API-key auth (`GATEWAY_API_KEYS` from Secret `llm-gateway-api-keys`; fail-closed when `ENVIRONMENT=prod`), sliding-window rate limiting keyed on the API key (or client IP; `X-Forwarded-For` trusted only from `TRUSTED_PROXY_CIDRS`), request-body cap, upstream error mapping (connect failure → **503 + `Retry-After`** while vLLM is cold, 504 timeout, 502 other), metrics. Deployed as a plain Deployment (2 replicas, 1 in dev); **internal-only in prod** (no HTTPRoute). Readiness is a self-check — it must never gate on vLLM, which is KEDA scale-to-zero (gating on it would remove the gateway from Service endpoints whenever vLLM is idle and deadlock scale-from-zero).
- **`vllm-inference`** — vLLM OpenAI-compatible inference server (`vllm/vllm-openai:v0.32.0-cu129` on GPU in prod; **not running in dev** — the dev overlay switches to the `vllm-openai-cpu` image but pauses its ScaledObject at 0 via `autoscaling.keda.sh/paused-replicas`, and the dev gateway points at `vllm-mock` via `patch-upstream.yaml`). KEDA scale-to-zero with two Prometheus triggers: vLLM queue depth for 1→N, plus llm-gateway request/upstream-error rates for 0→1 activation (the queue-depth metric is exposed by the vLLM pods themselves, so it doesn't exist at zero replicas and can't wake the service on its own).

The **AI audio pipeline** (see the dedicated section below) adds four more services that chain over `audio.*` Kafka topics:

- **`audio-api`** — FastAPI ingest: `POST /v1/audio/jobs` stores the upload to S3 (SeaweedFS in dev), writes Redis job-state, produces `audio.jobs`. `GET /jobs/{id}` reads Redis.
- **`stt-worker`** / **`llm-worker`** / **`tts-worker`** — the worker stages (speech→text, summarize-via-`llm-gateway`, text→speech). Each is built on the shared `platform_common.stage` processor (validate → atomic dedup claim → process → produce-confirmed → commit, with transient/permanent error classification and DLQ) and is KEDA scale-to-zero on consumer lag.

Each service is a **uv project**: `main.py` (the app entrypoint) plus an **`app/` package** (`config.py` typed pydantic-settings, `metrics.py`, per-service helpers like `backends.py`/`gateway.py`). The four Kafka workers share **`services/platform-common/`** (package `platform_common`: settings, observability, health/metrics server, Kafka I/O + consumer loop, retry/error classification, pydantic event models, job state, dedup, storage, pipeline metrics), depended on via `[tool.uv.sources] platform-common = { path = "../platform-common" }` — fix shared behaviour there, not per worker. `pyproject.toml` (`[project.dependencies]` runtime + `[dependency-groups].dev` for `pytest`/`httpx`, all pinned; `tool.uv.package = false` — deps are synced, nothing is built as a wheel), a committed `uv.lock`, `test_main.py` + `conftest.py` (sets `OTEL_SDK_DISABLED=true`; run `uv run pytest -q` from the service dir), `Dockerfile` (multi-stage: uv builder → digest-pinned `python:3.14.10-slim` runtime without uv, non-root; **build context is `services/`**: `docker build -f services/<app>/Dockerfile services/`, with a per-Dockerfile `<app>/Dockerfile.dockerignore`, BuildKit required), `README.md`, and `catalog-info.yaml` (Backstage catalog entry). Common tasks are wrapped in the root `Makefile` (`make deps|test|lint|fmt`). Lint/format with `ruff` (config in the **repo-root** `pyproject.toml`, inherited by each app); CI runs `ruff` (via `uvx`) + `pytest` per service (`app-lint`/`app-test`) and `docker-build.yaml` gates image build/sign/promotion on tests passing. uv.lock files are kept current by Renovate (`pep621` manager + monthly lockfile maintenance). There is also a `services/docker-compose.yml` for running the stack locally without Kubernetes.

### App-of-Apps flow

`kubernetes/platform/argocd/app-of-apps.yaml` → ArgoCD watches `kubernetes/platform/` → each subdirectory is an independent ArgoCD Application → platform services are deployed from upstream Helm charts; apps are deployed from `services/<svc>/k8s/overlays/` (the path each ApplicationSet points at).

### Dev vs prod differences

Each service's dev overlay (`services/<svc>/k8s/overlays/dev/`) does things its prod overlay does not — notably for `vllm-inference`:
1. `patch-vllm-model.yaml` — removes `runtimeClassName`, `nodeSelector`, `tolerations`, and GPU resource requests from the vllm-inference Deployment; switches to the `vllm/vllm-openai-cpu` image and `TinyLlama/TinyLlama-1.1B-Chat-v1.0` in `bfloat16` (vLLM removed `--device`; the platform is auto-detected). `patch-scaledobject-paused.yaml` keeps it at 0 replicas in dev
2. `patch-vllm-pvc.yaml` — changes the model-cache ephemeral volume's storage class from `gp3` to `standard` (kind hostPath) and shrinks it (the cache is a per-pod generic ephemeral volume on the Deployment, not a shared PVC — RWO EBS cannot be multi-attached across replicas)
3. `patch-resources.yaml` — reduces CPU/memory requests on all Deployments

The audio workers run the **stub ML backend in every environment** (`STT_BACKEND=stub` / `TTS_BACKEND=stub` in the base manifests). The real backends (`nemo` / `kokoro`) have lazy-imported deps that are intentionally **not** in `pyproject.toml`/`uv.lock`, and the Docker build stages that would bake them are authored but **not built by CI** — so selecting a real backend against the standard image makes every job fail its lazy import and dead-letter. Switching prod to `nemo`/`kokoro` requires (1) a build pipeline for the GPU image target, (2) a prod patch setting the backend env plus GPU scheduling (`runtimeClassName: nvidia`, `nodeSelector role=gpu`, GPU resources) — see the comments in `services/stt-worker/k8s/base/deployment.yaml` and the Dockerfile `nemo` stage.

Kafka dev/prod differences are in `kubernetes/platform/kafka/overlays/{dev,prod}/` — separate from the app overlay. Dev: 1 KRaft node, plaintext unauthenticated :9092. Prod: 3 brokers spread one per zone (rack-aware, `topology.kubernetes.io/zone`), SCRAM over **TLS only on :9093** (the plain listener is removed by a guarded JSON6902 patch).

### Multi-tenancy (namespace per team)

`kubernetes/tenants/` provisions one isolated namespace per team via an ArgoCD
**ApplicationSet** (`kubernetes/platform/tenants/applicationset.yaml`, git directory
generator over `team-*`). A team folder named `team-foo` becomes Application
`tenant-foo` syncing namespace `tenant-foo`.

Composition uses **kustomize Components**, not bases:
- `tenants/_template/` (Component) — shared, identical-for-all NetworkPolicies (zero-trust default-deny + explicit allows) and the `tenant-admin`/`tenant-viewer` **Role** definitions.
- `tenants/tiers/{small,medium,large}/` (Components) — `ResourceQuota` + `LimitRange` presets; `large` includes `nvidia.com/gpu` quota.
- `tenants/team-*/` — each sets `namespace:`, declares its own `Namespace` (the kustomize namespace transformer renames the Namespace object to match) + PSA labels, and binds the shared Roles to its OIDC groups in `rolebindings.yaml`.

Key invariants when editing: **Roles are shared, RoleBindings are per-tenant**; `tenant-admin` gets read-only on ResourceQuota/LimitRange/NetworkPolicy so teams can't widen their own guardrails; `tenant-viewer` deliberately omits `secrets`. Validate a tenant with `kustomize build kubernetes/tenants/team-<name>`. See `kubernetes/tenants/README.md` for the onboarding steps.

### AI audio pipeline (`audio-api` → `stt` → `llm` → `tts`)

A multi-stage, event-driven AI workflow that turns an uploaded audio file into a synthesized spoken summary. **The contract between stages is frozen in `docs/audio-pipeline-contract.md` — read it before touching any pipeline service; a change there requires re-freezing and updating every stage.**

```
audio-api ─audio.jobs─▶ stt-worker ─audio.transcripts─▶ llm-worker ─audio.summaries─▶ tts-worker ─audio.results─▶ (terminal)
                          └ audio.stt-dlq   └ audio.llm-dlq                              └ audio.tts-dlq
```

Invariants that bite if you break them:
- **Message key = `job_id` (UUID) on every topic** — drives partition affinity, ordering, and dedup. **Headers carry W3C `traceparent`/`tracestate`**, re-injected at every hop, so `audio-api → stt → llm → tts` stitches into **one trace** (workers extract context from Kafka headers via the `platform_common` Kafka header getter/setter). `created_at` is **copied forward** through every event so the terminal `tts-worker` can compute `pipeline_end_to_end_duration_seconds`.
- **Workers exchange object-storage keys, never inline payloads.** One bucket, prefixed: `audio/`, `transcripts/`, `summaries/`, `speech/<job_id>`. Writes are idempotent (deterministic keys). SeaweedFS in dev (S3 API behind the `minio` compat Service, `S3_ENDPOINT_URL` set, path addressing), Crossplane-provisioned S3 in prod (`S3_ENDPOINT_URL` unset, IRSA).
- **Job state in Redis** (`redis` platform service): key `job:<job_id>`, JSON, 7-day TTL, `status ∈ {queued, transcribing, summarizing, synthesizing, done, failed}`; `failed` carries `error.{stage,message,dlq_topic}`. `audio-api` writes `queued` and serves `GET /jobs/{id}` from it; each worker writes its in-progress status then updates `keys`/terminal status.
- **`llm-worker` calls the existing `llm-gateway`** (`POST /v1/chat/completions` via `LLM_GATEWAY_URL`), **never vLLM directly** — it inherits the gateway's rate-limiting and observability.
- Each worker uses the shared `platform_common.stage` / `consumer_loop`: `confluent-kafka`, `enable.auto.commit=false` (commit only **after** the output event is delivery-confirmed), pydantic event validation (`job_id` must be a UUID; invalid messages go to the stage DLQ with `error_type=invalid` + `raw_b64`), an **atomic Redis dedup claim** (`SET dedup:<stage>:<id> NX EX <lease>`, rewritten `done` on success/DLQ), error classification (permanent → DLQ at once; transient → jittered exponential backoff inside a time budget that settings validate against `max.poll.interval.ms`), SIGTERM drain, metrics/health HTTP server on `:9090`. `llm-worker` treats gateway 429/502/503/504 and refused connections as cold start (honours `Retry-After`, `LLM_COLD_START_BUDGET_SECONDS`), authenticates with `LLM_GATEWAY_API_KEY` (Secret `llm-gateway-api-keys`, key `llm-worker`), and map-reduces long transcripts (`LLM_MAX_INPUT_CHARS` × `LLM_MAX_CHUNKS`). worker-service's dedup is in-memory and per-pod (best effort; handlers must be idempotent). All three workers are **KEDA scale-to-zero** on consumer lag (generous `cooldownPeriod` for GPU cold start).
- Metric-name contract (dashboard `audio-ai-pipeline` reads these): per-stage `stt_*`/`llm_*`/`tts_*` (incl. `stt_transcript_bytes`, `llm_summary_bytes` for output sizes) plus pipeline-wide `pipeline_jobs_completed_total` / `pipeline_jobs_failed_total{stage}` / `pipeline_end_to_end_duration_seconds` / `pipeline_queue_wait_seconds{stage}` (completed/duration incremented by `tts-worker`; failed by whichever worker dead-letters; queue-wait observed once per message in every worker — `now − created_at`). GPU utilization comes from the **`dcgm-exporter`** platform service (`DCGM_FI_DEV_*`, prod-only) and cost from OpenCost; both are prod-only on the dashboard, so the GPU panel reads "No data" on local kind by design.

Supporting platform services are net-new: **SeaweedFS** (dev object store, `platform/seaweedfs`, compat `Service minio`), **Redis** (job state, `platform/redis`), and the **7 `audio.*` Kafka topics** + per-service `KafkaUser`s (`kubernetes/platform/kafka/base/{topics,users}.yaml`).

### Secret ownership split

- **Kafka SASL credentials** — owned by Strimzi UserOperator. One Secret per `KafkaUser` CR (`worker-service`, `audio-api`, `stt-worker`, `llm-worker`, `tts-worker`, …) is created automatically in the `apps` namespace. The prod worker overlays include the KEDA `TriggerAuthentication` that reads each user's `password` key from the Secret and `username` from the container's `KAFKA_SASL_USERNAME` env (KEDA ignores `username` in trigger metadata). Do **not** manage these Secrets via External Secrets Operator.
- **Kafka cluster CA** — Strimzi creates `platform-kafka-cluster-ca-cert` only in `platform`; **reflector** (`kubernetes/platform/reflector`) mirrors it into `apps` under the same name, opted in via `spec.kafka.template.clusterCaCert` annotations on the Kafka CR. Prod clients and KEDA triggers mount/read it from `apps`.
- **Object-storage credentials** — dev only: Secret `minio-credentials` (name kept for compatibility; keys `rootUser`/`rootPassword`) consumed by SeaweedFS as its admin identity and by the audio services as `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (with `S3_ENDPOINT_URL` pointed at the `minio` compat Service). In prod these are **unset** — the services use **IRSA** against the Crossplane-provisioned bucket.
- **Everything else** (HuggingFace token, application secrets) — External Secrets Operator pulling from AWS Secrets Manager in prod. The ESO `ClusterSecretStore` and all `ExternalSecret`s are **prod-only** (`external-secrets-config` is a cluster-generator ApplicationSet scoped to `environment=prod`); dev consumes none. The only secrets a local kind cluster needs are `redis-auth` and `minio-credentials` (auto-created in `platform` and `apps`) and `llm-gateway-api-keys` (in `apps`; keys `api-keys` = CSV accepted by the gateway, `llm-worker` = the worker's key, which must be in `api-keys`), all created idempotently by `bootstrap.sh --mode=local` (see `docs/local-dev.md`). In prod `llm-gateway-api-keys` comes from an `ExternalSecret` (`platform-core/prod/llm-gateway-api-keys`) in the llm-gateway prod overlay.

### Image promotion path

`docker-build.yaml` runs on pushes to `main` that touch `services/**`: **changes (only services whose files changed; `platform-common` rebuilds all four Kafka workers; `workflow_dispatch force_all`) → test → build amd64 locally → Trivy scan (HIGH/CRITICAL gate) → multi-arch (amd64+arm64) push with SBOM+provenance → Trivy scan of the pushed arm64 digest → cosign sign → promote**. Nothing is signed unless both scans pass, and Kyverno rejects unsigned images. **Promotion is a separate job gated by the GitHub Environment `production`** (required reviewers), verifies each signature with `cosign verify`, and commits all digests in one `[skip ci]` commit (all-or-nothing). All actions are pinned to commit SHAs and permissions are per-job least-privilege; CLI tools are downloaded with pinned sha256 checks. Promotion pins the **immutable digest** (`kustomize edit set image <app>=<repo>@sha256:<digest>`) into the **per-app** overlay the ApplicationSet actually deploys — `services/<app>/k8s/overlays/prod/`, NOT the CI validation aggregate `kustomize/validation/prod/`. The promote job edits each app's own overlay dir, with a rebase-and-retry around the push. ArgoCD picks up the commit and syncs. No manual image tag editing, and no mutable `:latest` in prod.

### Terraform module structure

`terraform/environments/{dev,prod}/main.tf` composes `networking` → `eks` → `iam` (+ `ecr`, `github-oidc` in prod). The `gpu-nodegroup` module is conditionally invoked inside `eks` (off in prod — Karpenter's GPU NodePool replaces it). Posture: nodes and Karpenter run in **private subnets** behind NAT (one per AZ in prod, single in dev) with VPC endpoints (S3 gateway always; ECR/STS/EC2/SQS/Logs interface endpoints in prod); prod spans **3 AZs** with an **on-demand** `role=platform` node group (min 3); the **prod API endpoint is private-only** by default (public access is opt-in via `api_public_access` + `api_allowed_cidrs`, and `0.0.0.0/0` is rejected); dev's public endpoint requires `api_allowed_cidrs` (CI: repo variable `TF_API_ALLOWED_CIDRS`). Kubernetes 1.36 on AL2023 AMIs. State uses S3-native locking (`use_lockfile = true`, Terraform ≥ 1.10). Providers/modules: aws ~> 6.72, eks ~> 21, vpc ~> 6, iam ~> 6 (IRSA retained; Pod Identity is a future migration). All three modules share the same `project`/`environment` naming convention which becomes the cluster name (`platform-core-dev`, `platform-core-prod`).

### Crossplane vs Terraform (two IaC tools, split by lifecycle)

**Terraform owns the day-0 foundation; Crossplane owns day-2 app-facing infra.** Terraform provisions everything that must exist before the cluster is useful (VPC, EKS, IRSA/OIDC, GPU nodes) — including the IAM role Crossplane assumes. Crossplane (running *in* the cluster) then exposes cloud resources as Kubernetes CRDs that app teams self-serve via namespaced composite resources, GitOps-reconciled by ArgoCD. Don't migrate the foundation to Crossplane — it can't bootstrap its own cluster, and you don't want VPC/EKS lifecycle in a reconcile loop.

Current slice = **S3** (`kubernetes/platform/crossplane/`, full details in `docs/crossplane.md`):
- Two ArgoCD apps from one `applicationset.yaml`: `crossplane` (Helm core, wave -2) + `crossplane-config` (the `config/` dir of Crossplane CRs, wave -1).
- **Crossplane v2** (2.4.x): a team applies a **namespaced composite resource** `ObjectStorageBucket` (`platform.io/v1alpha1`, no claims in v2); the platform-owned **XRD** (`config/definition.yaml`, `apiextensions.crossplane.io/v2`, `scope: Namespaced`) + **Composition** (`config/composition.yaml`, pipeline mode + function-patch-and-transform) render a secure bucket (AES256, public access blocked, versioned) using the namespaced `*.aws.m.upbound.io` managed resources and a `ClusterProviderConfig`. Bucket names are `platform-core-<env>-<ns>-<name>` so they fall inside the IRSA policy prefix.
- `provider-aws-s3` authenticates via **IRSA** (`module.iam.crossplane_s3_role_arn`, SA `crossplane-system:provider-aws-s3`) — no static keys. The role ARN is wired into the SA annotation in `config/provider.yaml`.
- **AWS-only**: both ApplicationSets use a cluster generator scoped to `environment: prod`, so the slice is skipped on local kind clusters. Crossplane CRD kinds are in the kubeconform `-skip` list in `ci.yaml`. Upgrading a live 1.x install requires the 1.20.x hop and orphaning buckets first — see `docs/crossplane.md`.

### OPA policies

CI runs conftest with **`--combine`**, so policies see the whole rendered overlay as one set and can check *relationships*, not just per-document shape. Rego files (all `package main`):
- `policy/lib.rego` — shared helpers: `manifests`, the `workloads` set (**Deployment, Rollout, StatefulSet, DaemonSet** — Rollout included so the echo-service canary isn't exempt), `pod_labels`, `requires_netpol` (apps + `tenant-*`), `selector_matches`.
- `policy/workloads.rego` — `deny` (hard fail) for missing resource requests/limits, missing liveness/readiness probes, and `runAsUser: 0` (pod or container) — applied to **every** workload kind, not just Deployments. `warn` for a missing `app.kubernetes.io/part-of` label.
- `policy/network-policy.rego` — **`deny`**: a real relationship check that fails when a workload in a segmented namespace has **no NetworkPolicy whose `podSelector` matches its pod labels** (empty selector = matches all). `warn`: a non-exempt workload (KEDA scale-to-zero `vllm`/`worker` are exempt) with no PDB selecting it.

`.kube-linter.yaml` exempts resources in the `platform` namespace from root-check and privilege-escalation checks because Strimzi broker pods need those permissions.

### Progressive delivery (echo-service is a Rollout, not a Deployment)

`echo-service` is an **Argo Rollouts `Rollout`** (`services/echo-service/k8s/base/rollout.yaml`), not a Deployment — this trips up two things:
- The kustomize **`replicas:` transformer does not support `Rollout`**. Per-env replicas/resources for echo-service are set with **JSON6902 patches** (`services/echo-service/k8s/overlays/{dev,prod}/patch-echo-resources.yaml`, targeting `kind: Rollout`). worker-service/vllm-inference are still Deployments and use the normal `replicas:` transformer + strategic-merge patches.
- Canary analysis (`analysistemplate.yaml`) scopes its Prometheus queries to canary pods via the `rollouts-pod-template-hash` label, which only reaches the metrics because the ServiceMonitor sets `podTargetLabels: [rollouts-pod-template-hash]`. If you change the ServiceMonitor, keep that.

Flow: the Argo Rollouts **Gateway API traffic-router plugin** (`argoproj-labs/gatewayAPI`) shifts the `echo-service` HTTPRoute backendRef weights 10→50→100% (the ApplicationSet ignores those weights so selfHeal doesn't revert them); each pause runs `echo-service-slo` (success-rate ≥ 99%, p95 ≤ 0.5s); `failureLimit: 1` aborts + auto-reverts.
 The `argo-rollouts` controller is a platform service (sync-wave -1). The echo-service ArgoCD Application uses the `platform-apps` AppProject (change-freeze sync window).

### Supply chain & admission (cosign keyless + Kyverno)

`docker-build.yaml` authenticates to AWS via **GitHub OIDC** (`role-to-assume: ${{ vars.AWS_OIDC_ROLE_ARN }}`, from `terraform/modules/github-oidc`) — there are **no static AWS keys**. It scans (Trivy), attaches SBOM + SLSA provenance, and **cosign-signs each image keyless**.

Kyverno (`kubernetes/platform/kyverno/`, sync-wave -1) enforces this at admission via `kyverno-policies` (sync-wave 1):
Policies use Kyverno's CEL types (Kyverno 1.19; `ClusterPolicy` is deprecated): `ImageValidatingPolicy` / `ValidatingPolicy` with `validationActions` (`[Deny]` ≈ Enforce, `[Audit]`).
- `verify-image-signatures` (`ImageValidatingPolicy`, **Deny**) — rejects unsigned `*.dkr.ecr.*/platform-core/*` images. Scoped to our registry, so upstream images are unaffected. **`failurePolicy: Fail`** — if the Kyverno controller is down, admission of our images is blocked by design.
- `require-pod-standards` (**Enforce, `tenant-*` only**) — resources/probes/non-root. Graduated enforcement: strict on the tenant paved road, advisory elsewhere.
- `disallow-latest-tag` — **Audit in dev** (local `:latest` still runs, just reported) but **Deny in prod** (patched by `kyverno-policies/overlays/prod`). This is env-selected: `kyverno-policies` is an **ApplicationSet** (cluster generator) pointing at `overlays/{{environment}}`, same pattern as the apps. Prod images are digest-pinned, so `:latest` there is a hard admission failure.

### Observability wiring

`observability/prometheus/rules/alerts.yaml` is a `PrometheusRule` CR deployed by ArgoCD alongside `kube-prometheus-stack`. Alert PromQL queries reference recording rules defined in `observability/prometheus/slo/recording-rules.yaml`. The Kafka consumer lag alert uses `kafka_consumergroup_lag_sum` which comes from the Strimzi Kafka Exporter — it is disabled in the dev Kafka overlay to save resources.

Grafana is the single pane for **metrics + logs + traces + cost** (datasources wired in the `monitoring` Application's Helm values):
- **Metrics** — Prometheus (default datasource, uid `prometheus`).
- **Logs** — Loki (uid `loki`; `grafana-community/loki` chart, monolithic mode, Service `loki:3100`), collected by **Grafana Alloy** (DaemonSet; Promtail is EOL) which parses CRI + JSON, labels `level` and puts `trace_id`/`span_id` in structured metadata; a derived field (`"trace_id":\s*"(\w+)"`) turns the `trace_id` into a clickable Tempo link. Grafana is reachable at `grafana.platform-core.local` via an HTTPRoute.
- **Traces** — Tempo (uid `tempo`), fed by the OpenTelemetry Collector. `tracesToLogsV2`/`tracesToMetrics` link a span back to its logs and metrics.
- **Cost** — OpenCost reads the Prometheus datasource; the `cost-finops` dashboard (in `observability/grafana/dashboards`) shows $/hr by node and by namespace.

**Tracing pipeline:** app workloads (echo-service, llm-gateway, worker-service, and the audio-pipeline services audio-api/stt-worker/llm-worker/tts-worker) export OTLP spans to the in-cluster collector and back to Tempo:
`app → otel-collector.monitoring:4317 (OTLP) → tempo.monitoring:4317 → Grafana (tempo.monitoring:3200)`.
Each app sets `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT`, and `OTEL_RESOURCE_ATTRIBUTES` in its base manifest (dev/prod are separate clusters, each with its own Tempo, so traces are inherently per-environment). Trace context propagates over HTTP (FastAPI + httpx instrumentation) and across Kafka via message headers (worker-service extracts it and starts a span per job). `add_trace_context` injects `trace_id`/`span_id` into every structlog line. Tracing honours `OTEL_SDK_DISABLED=true` (set in each app's `conftest.py`) so tests don't block on export. New dashboards ship as labelled ConfigMaps via the `grafana-dashboards` Application — add the JSON under `observability/grafana/dashboards` and register it in that kustomization's `configMapGenerator`.

### Networking — Cilium (local) vs AWS VPC CNI (prod)

Local kind clusters run **Cilium** (eBPF dataplane) with **Hubble** for live flow visibility and NetworkPolicy debugging. `terraform/modules/kind/kind-config.yaml` disables the default CNI (kindnet), and `scripts/bootstrap.sh --mode=local` installs Cilium via Helm *before* applying the App-of-Apps — nodes stay NotReady until it's up, so the script creates the cluster without `--wait` and waits for readiness afterward. Cilium is installed with `cni.chainingMode=portmap` so `hostPort` works — the Envoy Gateway proxy binds hostPort 80/443 on the `ingress-ready=true` node, which kind maps to the host. Hubble UI: `kubectl port-forward svc/hubble-ui -n kube-system 12000:80`. EKS deliberately keeps the **AWS VPC CNI** (the `vpc-cni` cluster addon) — a low-risk choice; Cilium is the local-first networking demo, not yet the EKS dataplane.

### North-south traffic — Gateway API (Envoy Gateway)

ingress-nginx was retired (and 4.10.x was vulnerable to IngressNightmare); there are **no `Ingress` objects**. `kubernetes/platform/envoy-gateway/` installs the Gateway API CRDs (standard channel, wave -3), Envoy Gateway (wave -2) and an env-selected config ApplicationSet (wave -1). **Fixed contract:** GatewayClass `envoy`, Gateway **`platform`** in namespace **`envoy-gateway-system`**, listener `http` :80, hostname `*.platform-core.local`, routes allowed from all namespaces. Every exposed service ships an `HTTPRoute` with `parentRefs: [{name: platform, namespace: envoy-gateway-system}]` (echo-service, audio-api dev, llm-gateway dev, Grafana, AKHQ, Backstage, demo-app chart). NetworkPolicies admit north-south traffic from namespace `envoy-gateway-system`. Dev: the proxy binds hostPort 80/443 on the kind `ingress-ready` node. Prod: NLB `LoadBalancer` (2 replicas + PDB). See ADR-019.

### Karpenter — node autoscaling (prod/EKS only)

Prod uses **Karpenter** for just-in-time node provisioning instead of static managed node groups. The Terraform `karpenter` submodule (in `terraform/modules/eks`, gated by `enable_karpenter`) creates the controller IAM role (IRSA, `kube-system:karpenter`), the node IAM role + access entry, and the SQS interruption queue; subnets and the node security group are tagged `karpenter.sh/discovery=<cluster>` (networking + eks modules). The in-cluster controller + NodePools ship via the prod-only `kubernetes/platform/karpenter` ApplicationSet. There are two NodePools sharing one `EC2NodeClass` (AMI alias `al2023@latest`, IMDSv2): a **default** pool (spot + on-demand, general burst) and a **gpu** pool (on-demand g4dn/g5, tainted `nvidia.com/gpu`, labelled `role=gpu`) that matches the vLLM Deployment's toleration/nodeSelector — this **replaces the static GPU managed node group** (`enable_gpu_nodegroup=false` in prod). The controller (2 replicas, anti-affine) runs on the on-demand `role=platform` node group. The Helm values + EC2NodeClass carry placeholders (`<KARPENTER_CONTROLLER_ROLE_ARN>`, `<KARPENTER_NODE_IAM_ROLE>`) populated from `terraform output`. Like the rest of the AWS path, this is authored but unverified on a live account.
