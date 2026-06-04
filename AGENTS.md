# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Validation commands (run locally before pushing)

```bash
# Kustomize — build and validate manifests
kustomize build kustomize/validation/dev  | kubeconform -strict -summary -schema-location default
kustomize build kustomize/validation/prod | kubeconform -strict -summary -schema-location default
kustomize build kubernetes/platform/kafka/overlays/dev  | kubeconform -strict -summary
kustomize build kubernetes/platform/kafka/overlays/prod | kubeconform -strict -summary

# Helm — lint all charts
for chart in helm/*/; do helm lint "$chart"; done

# Helm — render and validate (requires kubeconform)
helm template demo helm/demo-app | kubeconform -strict -summary
helm template vllm helm/vllm     | kubeconform -strict -summary

# Terraform — format check and validate
terraform fmt -check -recursive terraform/
cd terraform/environments/dev  && terraform init -backend=false && terraform validate
cd terraform/environments/prod && terraform init -backend=false && terraform validate

# OPA policy check (requires conftest)
kustomize build kustomize/validation/dev | conftest test --combine --policy policy/ -

# kube-linter
kube-linter lint kubernetes/ --config .kube-linter.yaml

# Kyverno — check policies are well-formed (dry-run apply)
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
- Helm 3.14.0, Strimzi CRDs 0.40.0, KEDA CRDs 2.13.0, Terraform 1.7.0

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
- **`kubernetes/platform/`** — one ArgoCD Application per platform service (cert-manager, ingress-nginx, external-secrets, keda, kafka, crossplane, backstage, monitoring, loki, tempo, opentelemetry-collector, opencost, karpenter, dcgm-exporter, minio, redis, namespaces). Each points to its upstream Helm chart (or, for config-only slices, a repo path). Prod-only slices (crossplane, karpenter, **dcgm-exporter**) are ApplicationSets with a cluster generator scoped to `environment=prod`; **minio** is a **dev-only** ApplicationSet (prod uses the Crossplane S3 bucket instead); **redis** and **namespaces** run in all environments.

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
- **`llm-gateway`** — FastAPI reverse proxy in front of vLLM: per-IP sliding-window rate limiting, upstream error mapping (429/502/504), metrics. Deployed as a plain Deployment (2 replicas, 1 in dev). Its Kubernetes readiness probe is `/healthz` (self-check), **not** the app's `/readyz` — `/readyz` gates on the upstream vLLM `/health`, but vLLM is KEDA scale-to-zero, so gating readiness on it would remove the gateway from Service endpoints whenever vLLM is idle and deadlock scale-from-zero.
- **`vllm-inference`** — vLLM OpenAI-compatible inference server (GPU in prod, CPU TinyLlama in dev). KEDA scale-to-zero on queue depth.

The **AI audio pipeline** (see the dedicated section below) adds four more services that chain over `audio.*` Kafka topics:

- **`audio-api`** — FastAPI ingest: `POST /v1/audio/jobs` stores the upload to S3/MinIO, writes Redis job-state, produces `audio.jobs`. `GET /jobs/{id}` reads Redis.
- **`stt-worker`** / **`llm-worker`** / **`tts-worker`** — the worker stages (speech→text, summarize-via-`llm-gateway`, text→speech). Each reuses the `worker-service` consumer pattern verbatim and is KEDA scale-to-zero on consumer lag.

Each service is a **uv project**: `main.py` (the app entrypoint) plus an **`app/` package** that splits out the cross-cutting pieces (`config.py` typed pydantic-settings, `observability.py`, `metrics.py`, and for the messaging services `kafka_io.py`, plus per-service helpers like `storage.py`/`backends.py`/`job_state.py` in the audio workers). `pyproject.toml` (`[project.dependencies]` runtime + `[dependency-groups].dev` for `pytest`/`httpx`, all pinned; `tool.uv.package = false` — deps are synced, nothing is built as a wheel), a committed `uv.lock`, `test_main.py` + `conftest.py` (sets `OTEL_SDK_DISABLED=true`; run `uv run pytest -q` from the service dir), `Dockerfile` (python:3.12-slim, non-root, deps installed via `uv sync --frozen --no-dev`), `README.md`, and `catalog-info.yaml` (Backstage catalog entry). Common tasks are wrapped in the root `Makefile` (`make deps|test|lint|fmt`). Lint/format with `ruff` (config in the **repo-root** `pyproject.toml`, inherited by each app); CI runs `ruff` (via `uvx`) + `pytest` per service (`app-lint`/`app-test`) and `docker-build.yaml` gates image build/sign/promotion on tests passing. uv.lock files are kept current by Renovate (`pep621` manager + monthly lockfile maintenance). There is also a `services/docker-compose.yml` for running the stack locally without Kubernetes.

### App-of-Apps flow

`kubernetes/platform/argocd/app-of-apps.yaml` → ArgoCD watches `kubernetes/platform/` → each subdirectory is an independent ArgoCD Application → platform services are deployed from upstream Helm charts; apps are deployed from `services/<svc>/k8s/overlays/` (the path each ApplicationSet points at).

### Dev vs prod differences

Each service's dev overlay (`services/<svc>/k8s/overlays/dev/`) does things its prod overlay does not — notably for `vllm-inference`:
1. `patch-vllm-model.yaml` — removes `runtimeClassName`, `nodeSelector`, `tolerations`, and GPU resource requests from the vllm-inference Deployment; switches model arg to `TinyLlama/TinyLlama-1.1B-Chat-v1.0` with `--device cpu`
2. `patch-vllm-pvc.yaml` — changes storage class from `gp3` to `standard` (kind hostPath)
3. `patch-resources.yaml` — reduces CPU/memory requests on all Deployments

The audio workers' dev overlays (`patch-dev.yaml`) likewise switch the **pluggable ML backend** to its stub: `STT_BACKEND=stub` / `TTS_BACKEND=stub` on kind (no GPU), where prod sets `nemo` / `kokoro`. The heavy `nemo`/`kokoro`/`torch` deps are **lazy-imported only when the real backend is selected** and are intentionally **not** in `pyproject.toml`/`uv.lock` — prod images bake the model + add the deps in a build layer, keeping dev images light and the test suites fast.

Kafka dev/prod differences (broker count, partition count, SCRAM auth, storage class) are in `kubernetes/platform/kafka/overlays/dev/` — separate from the app overlay.

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

A multi-stage, event-driven AI workflow that turns an uploaded audio file into a synthesized spoken summary. **The contract between stages is frozen in `docs/audio-pipeline-contract.md` — read it before touching any pipeline service; a change there requires re-freezing and updating every stage.** Sourced from `AI_Audio_Pipeline_Specification.md`.

```
audio-api ─audio.jobs─▶ stt-worker ─audio.transcripts─▶ llm-worker ─audio.summaries─▶ tts-worker ─audio.results─▶ (terminal)
                          └ audio.stt-dlq   └ audio.llm-dlq                              └ audio.tts-dlq
```

Invariants that bite if you break them:
- **Message key = `job_id` (UUID) on every topic** — drives partition affinity, ordering, and dedup. **Headers carry W3C `traceparent`/`tracestate`**, re-injected at every hop, so `audio-api → stt → llm → tts` stitches into **one trace** (workers extract context from Kafka headers via the `worker-service` `KafkaHeaderGetter`/`propagate` helpers). `created_at` is **copied forward** through every event so the terminal `tts-worker` can compute `pipeline_end_to_end_duration_seconds`.
- **Workers exchange object-storage keys, never inline payloads.** One bucket, prefixed: `audio/`, `transcripts/`, `summaries/`, `speech/<job_id>`. Writes are idempotent (deterministic keys). MinIO in dev (S3 API, `S3_ENDPOINT_URL` set, path addressing), Crossplane-provisioned S3 in prod (`S3_ENDPOINT_URL` unset, IRSA).
- **Job state in Redis** (`redis` platform service): key `job:<job_id>`, JSON, 7-day TTL, `status ∈ {queued, transcribing, summarizing, synthesizing, done, failed}`; `failed` carries `error.{stage,message,dlq_topic}`. `audio-api` writes `queued` and serves `GET /jobs/{id}` from it; each worker writes its in-progress status then updates `keys`/terminal status.
- **`llm-worker` calls the existing `llm-gateway`** (`POST /v1/chat/completions` via `LLM_GATEWAY_URL`), **never vLLM directly** — it inherits the gateway's rate-limiting and observability.
- Each worker reuses the `worker-service` pattern verbatim: `confluent-kafka`, `enable.auto.commit=false` (commit only **after** the output event is produced), job-id dedup, retry → per-stage DLQ, SIGTERM drain, metrics HTTP server on `:9090`. All three workers are **KEDA scale-to-zero** on consumer lag (generous `cooldownPeriod` for GPU cold start).
- Metric-name contract (dashboard `audio-ai-pipeline` reads these): per-stage `stt_*`/`llm_*`/`tts_*` (incl. `stt_transcript_bytes`, `llm_summary_bytes` for output sizes) plus pipeline-wide `pipeline_jobs_completed_total` / `pipeline_jobs_failed_total{stage}` / `pipeline_end_to_end_duration_seconds` / `pipeline_queue_wait_seconds{stage}` (completed/duration incremented by `tts-worker`; failed by whichever worker dead-letters; queue-wait observed once per message in every worker — `now − created_at`). GPU utilization comes from the **`dcgm-exporter`** platform service (`DCGM_FI_DEV_*`, prod-only) and cost from OpenCost; both are prod-only on the dashboard, so the GPU panel reads "No data" on local kind by design.

Supporting platform services are net-new: **MinIO** (dev object store, `platform/minio`), **Redis** (job state, `platform/redis`), and the **7 `audio.*` Kafka topics** + per-service `KafkaUser`s (`kubernetes/platform/kafka/base/{topics,users}.yaml`).

### Secret ownership split

- **Kafka SASL credentials** — owned by Strimzi UserOperator. One Secret per `KafkaUser` CR (`worker-service`, `audio-api`, `stt-worker`, `llm-worker`, `tts-worker`, …) is created automatically in the `apps` namespace. The prod worker overlays include the KEDA `TriggerAuthentication` that reads each user's `password` key. Do **not** manage these Secrets via External Secrets Operator.
- **MinIO / object-storage credentials** — dev only: a Secret of MinIO access/secret keys consumed by the audio services as `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (with `S3_ENDPOINT_URL` pointed at the MinIO service). In prod these are **unset** — the services use **IRSA** against the Crossplane-provisioned bucket.
- **Everything else** (HuggingFace token, application secrets) — External Secrets Operator pulling from AWS Secrets Manager. Bootstrapped locally by `bootstrap.sh` via `kubectl create secret`.

### Image promotion path

`docker-build.yaml` runs on every push to `main` that touches `services/**`: **test → build (local, no push) → Trivy scan (HIGH/CRITICAL gate) → push with SBOM+provenance → cosign sign → promote**. The scan runs on the locally-loaded image *before* anything reaches ECR, so a vulnerable image is never pushed/signed/promoted (the two build steps share the GHA layer cache, so the push build is not a full rebuild). Promotion pins the **immutable digest** (`kustomize edit set image <app>=<repo>@sha256:<digest>`) into the **per-app** overlay the ApplicationSet actually deploys — `services/<app>/k8s/overlays/prod/`, NOT the CI validation aggregate `kustomize/validation/prod/`. The matrix runs per-app in parallel, each editing its own overlay dir, with a rebase-and-retry around the push. ArgoCD picks up the commit and syncs. No manual image tag editing, and no mutable `:latest` in prod.

### Terraform module structure

`terraform/environments/{dev,prod}/main.tf` composes three modules: `networking` → `eks` → `iam`. The `gpu-nodegroup` module is conditionally invoked inside `eks` (`enable_gpu_nodegroup = false` in dev, `true` in prod). All three modules share the same `project`/`environment` naming convention which becomes the cluster name (`platform-core-dev`, `platform-core-prod`).

### Crossplane vs Terraform (two IaC tools, split by lifecycle)

**Terraform owns the day-0 foundation; Crossplane owns day-2 app-facing infra.** Terraform provisions everything that must exist before the cluster is useful (VPC, EKS, IRSA/OIDC, GPU nodes) — including the IAM role Crossplane assumes. Crossplane (running *in* the cluster) then exposes cloud resources as Kubernetes CRDs that app teams self-serve via claims, GitOps-reconciled by ArgoCD. Don't migrate the foundation to Crossplane — it can't bootstrap its own cluster, and you don't want VPC/EKS lifecycle in a reconcile loop.

Current slice = **S3** (`kubernetes/platform/crossplane/`, full details in `docs/crossplane.md`):
- Two ArgoCD apps from one `applicationset.yaml`: `crossplane` (Helm core, wave -2) + `crossplane-config` (the `config/` dir of Crossplane CRs, wave -1).
- A team applies a namespaced `ObjectStorageBucket` claim (`platform.io/v1alpha1`); the platform-owned **XRD** (`config/definition.yaml`) + **Composition** (`config/composition.yaml`, pipeline mode + function-patch-and-transform) render a secure bucket (AES256, public access blocked, versioned).
- `provider-aws-s3` authenticates via **IRSA** (`module.iam.crossplane_s3_role_arn`, SA `crossplane-system:provider-aws-s3`) — no static keys. The role ARN is wired into the SA annotation in `config/provider.yaml`.
- **AWS-only**: both ApplicationSets use a cluster generator scoped to `environment: prod`, so the slice is skipped on local kind clusters. New Crossplane CRD kinds are added to the kubeconform `-skip` list in `ci.yaml`.

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

Flow: nginx traffic routing splits 10→50→100%; each pause runs `echo-service-slo` (success-rate ≥ 99%, p95 ≤ 0.5s); `failureLimit: 1` aborts + auto-reverts.
 The `argo-rollouts` controller is a platform service (sync-wave -1). The echo-service ArgoCD Application uses the `platform-apps` AppProject (change-freeze sync window).

### Supply chain & admission (cosign keyless + Kyverno)

`docker-build.yaml` authenticates to AWS via **GitHub OIDC** (`role-to-assume: ${{ vars.AWS_OIDC_ROLE_ARN }}`, from `terraform/modules/github-oidc`) — there are **no static AWS keys**. It scans (Trivy), attaches SBOM + SLSA provenance, and **cosign-signs each image keyless**.

Kyverno (`kubernetes/platform/kyverno/`, sync-wave -1) enforces this at admission via `kyverno-policies` (sync-wave 1):
- `verify-image-signatures` (**Enforce**) — rejects unsigned `*.dkr.ecr.*/platform-core/*` images. Scoped to our registry, so upstream images are unaffected. **`failurePolicy: Fail`** — if the Kyverno controller is down, admission of our images is blocked by design.
- `require-pod-standards` (**Enforce, `tenant-*` only**) — resources/probes/non-root. Graduated enforcement: strict on the tenant paved road, advisory elsewhere.
- `disallow-latest-tag` — **Audit in dev** (local `:latest` still runs, just reported) but **Enforce in prod** (patched by `kyverno-policies/overlays/prod`). This is env-selected: `kyverno-policies` is an **ApplicationSet** (cluster generator) pointing at `overlays/{{environment}}`, same pattern as the apps. Prod images are digest-pinned, so `:latest` there is a hard admission failure.

### Observability wiring

`observability/prometheus/rules/alerts.yaml` is a `PrometheusRule` CR deployed by ArgoCD alongside `kube-prometheus-stack`. Alert PromQL queries reference recording rules defined in `observability/prometheus/slo/recording-rules.yaml`. The Kafka consumer lag alert uses `kafka_consumergroup_lag_sum` which comes from the Strimzi Kafka Exporter sidecar — it is disabled in the dev Kafka overlay to save resources.

Grafana is the single pane for **metrics + logs + traces + cost** (datasources wired in the `monitoring` Application's Helm values):
- **Metrics** — Prometheus (default datasource, uid `prometheus`).
- **Logs** — Loki (uid `loki`); a derived field turns the `trace_id` JSON log field into a clickable Tempo link.
- **Traces** — Tempo (uid `tempo`), fed by the OpenTelemetry Collector. `tracesToLogsV2`/`tracesToMetrics` link a span back to its logs and metrics.
- **Cost** — OpenCost reads the Prometheus datasource; the `cost-finops` dashboard (in `observability/grafana/dashboards`) shows $/hr by node and by namespace.

**Tracing pipeline:** app workloads (echo-service, llm-gateway, worker-service, and the audio-pipeline services audio-api/stt-worker/llm-worker/tts-worker) export OTLP spans to the in-cluster collector and back to Tempo:
`app → otel-collector.monitoring:4317 (OTLP) → tempo.monitoring:4317 → Grafana (tempo.monitoring:3200)`.
Each app sets `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT`, and `OTEL_RESOURCE_ATTRIBUTES` in its base manifest (dev/prod are separate clusters, each with its own Tempo, so traces are inherently per-environment). Trace context propagates over HTTP (FastAPI + httpx instrumentation) and across Kafka via message headers (worker-service extracts it and starts a span per job). `add_trace_context` injects `trace_id`/`span_id` into every structlog line. Tracing honours `OTEL_SDK_DISABLED=true` (set in each app's `conftest.py`) so tests don't block on export. New dashboards ship as labelled ConfigMaps via the `grafana-dashboards` Application — add the JSON under `observability/grafana/dashboards` and register it in that kustomization's `configMapGenerator`.

### Networking — Cilium (local) vs AWS VPC CNI (prod)

Local kind clusters run **Cilium** (eBPF dataplane) with **Hubble** for live flow visibility and NetworkPolicy debugging. `terraform/modules/kind/kind-config.yaml` disables the default CNI (kindnet), and `scripts/bootstrap.sh --mode=local` installs Cilium via Helm *before* applying the App-of-Apps — nodes stay NotReady until it's up, so the script creates the cluster without `--wait` and waits for readiness afterward. Hubble UI: `kubectl port-forward svc/hubble-ui -n kube-system 12000:80`. EKS deliberately keeps the **AWS VPC CNI** (the `vpc-cni` cluster addon) — a low-risk choice; Cilium is the local-first networking demo, not yet the EKS dataplane.

### Karpenter — node autoscaling (prod/EKS only)

Prod uses **Karpenter** for just-in-time node provisioning instead of static managed node groups. The Terraform `karpenter` submodule (in `terraform/modules/eks`, gated by `enable_karpenter`) creates the controller IAM role (IRSA, `kube-system:karpenter`), the node IAM role + access entry, and the SQS interruption queue; subnets and the node security group are tagged `karpenter.sh/discovery=<cluster>` (networking + eks modules). The in-cluster controller + NodePools ship via the prod-only `kubernetes/platform/karpenter` ApplicationSet. There are two NodePools sharing one `EC2NodeClass`: a **default** pool (Spot/on-demand, general burst) and a **gpu** pool (on-demand g4dn/g5, tainted `nvidia.com/gpu`, labelled `role=gpu`) that matches the vLLM Deployment's toleration/nodeSelector — this **replaces the static GPU managed node group** (`enable_gpu_nodegroup=false` in prod). The Helm values + EC2NodeClass carry placeholders (`<KARPENTER_CONTROLLER_ROLE_ARN>`, `<KARPENTER_NODE_IAM_ROLE>`) populated from `terraform output`. Like the rest of the AWS path, this is authored but unverified on a live account.
