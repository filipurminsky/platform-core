# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Validation commands (run locally before pushing)

```bash
# Kustomize — build and validate manifests
kustomize build kustomize/overlays/dev  | kubeconform -strict -summary -schema-location default
kustomize build kustomize/overlays/prod | kubeconform -strict -summary -schema-location default
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
kustomize build kustomize/overlays/dev | conftest test --policy policy/ -

# kube-linter
kube-linter lint kubernetes/ --config .kube-linter.yaml

# Kyverno — check policies are well-formed (dry-run apply)
kyverno apply kubernetes/platform/kyverno-policies/policies/

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

### Two separate kustomize trees

There are **two independent kustomize hierarchies** — confusing them is the most common mistake:

| Tree | Purpose | ArgoCD points here? |
|------|---------|---------------------|
| `kustomize/base/` + `kustomize/overlays/{dev,prod}/` | App Deployments, Services, PDBs, NetworkPolicies, ServiceMonitors | Yes — via the `echo-service`/`worker-service`/`vllm-inference` ApplicationSets |
| `kubernetes/platform/kafka/base/` + `overlays/{dev,prod}/` | Strimzi Kafka, KafkaTopic, KafkaUser CRs | Yes — via the `kafka` ApplicationSet |
| `kubernetes/tenants/team-*/` | Per-team Namespace, ResourceQuota, LimitRange, NetworkPolicies, RBAC | Yes — via `tenants` ApplicationSet |

### Environment (dev vs prod) is cluster-labelled, not hardcoded

The app/kafka manifests are **ApplicationSets** with a **cluster generator** (`kubernetes/apps/*/applicationset.yaml`, `kubernetes/platform/kafka/applicationset.yaml`). Each templates the overlay path from the destination cluster's `environment` label:

```
path: kustomize/overlays/{{.metadata.labels.environment}}/echo-service
```

`bootstrap.sh` sets that label by declaring an `in-cluster` cluster Secret: `--mode=local` → `environment=dev`, `--mode=aws` → `environment=prod`. **Switching environments never requires editing a manifest** — and the generator's `environment Exists` selector means no apps sync until the cluster is labelled. To flip a running cluster, relabel the secret:

```bash
kubectl label secret in-cluster -n argocd environment=prod --overwrite
```

### Where Kubernetes manifests actually live

- **`kubernetes/apps/`** — ArgoCD ApplicationSets and KEDA ScaledObjects/TriggerAuthentications only. **No Deployments or Services here.**
- **`kustomize/base/{echo-service,worker-service,vllm-inference}/`** — the real Kubernetes manifests for the three demo apps.
- **`kubernetes/platform/`** — one ArgoCD Application per platform service (cert-manager, ingress-nginx, external-secrets, keda, kafka, crossplane, backstage). Each points to its upstream Helm chart.

### Application source code (`apps/`)

**Do not confuse `apps/` (root) with `kubernetes/apps/`.** `apps/` holds the *source code and container builds* for the three demo services; `kubernetes/apps/` holds their *ArgoCD ApplicationSets*. The deployable manifests live in neither — they're in `kustomize/base/<svc>/`.

Each `apps/<service>/` is a self-contained Python project:

- **`echo-service`** — FastAPI HTTP demo (probes, `/metrics`, request echo). Deployed as an Argo Rollout (canary).
- **`worker-service`** — Kafka consumer (confluent-kafka, no web framework). Manual-commit processing with dedup, retry → DLQ, graceful SIGTERM drain; exposes metrics on `:9090`. Scaled by KEDA on consumer lag.
- **`llm-gateway`** — FastAPI reverse proxy in front of vLLM: per-IP sliding-window rate limiting, upstream error mapping (429/502/504), metrics.

Per service: `main.py` (single-module app), `requirements.txt` (runtime, pinned), `requirements-dev.txt` (adds `pytest`), `test_main.py` (unit tests — run `pytest -q` from the service dir), `Dockerfile` (python:3.12-slim, non-root), and `catalog-info.yaml` (Backstage catalog entry). Lint/format with `ruff` (config in `pyproject.toml`); CI runs `ruff` + `pytest` per service (`app-lint`/`app-test`) and `docker-build.yaml` gates image build/sign/promotion on tests passing. There is also an `apps/docker-compose.yml` for running the stack locally without Kubernetes.

### App-of-Apps flow

`kubernetes/platform/argocd/app-of-apps.yaml` → ArgoCD watches `kubernetes/platform/` → each subdirectory is an independent ArgoCD Application → platform services are deployed from upstream Helm charts; apps are deployed from `kustomize/overlays/`.

### Dev vs prod differences

The dev overlay (`kustomize/overlays/dev/`) does three things the prod overlay does not:
1. `patch-vllm-model.yaml` — removes `runtimeClassName`, `nodeSelector`, `tolerations`, and GPU resource requests from the vllm-inference Deployment; switches model arg to `TinyLlama/TinyLlama-1.1B-Chat-v1.0` with `--device cpu`
2. `patch-vllm-pvc.yaml` — changes storage class from `gp3` to `standard` (kind hostPath)
3. `patch-resources.yaml` — reduces CPU/memory requests on all Deployments

Kafka dev/prod differences (broker count, partition count, TLS/SCRAM auth, storage class) are in `kubernetes/platform/kafka/overlays/dev/` — separate from the app overlay.

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

### Secret ownership split

- **Kafka SASL credentials** — owned by Strimzi UserOperator. The Secret named `worker-service` is created automatically from the `KafkaUser` CR. KEDA's `TriggerAuthentication` (`kubernetes/apps/worker-service/trigger-auth.yaml`) mounts it directly. Do **not** manage this Secret via External Secrets Operator.
- **Everything else** (HuggingFace token, application secrets) — External Secrets Operator pulling from AWS Secrets Manager. Bootstrapped locally by `bootstrap.sh` via `kubectl create secret`.

### Image promotion path

`docker-build.yaml` builds on every push to `main`, pushes to ECR, then runs `kustomize edit set image` to update the tag in `kustomize/overlays/prod/` and commits back to the repo. ArgoCD picks up the commit and syncs. No manual image tag editing.

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

`policy/deployments.rego` — `deny` rules (hard failures) for missing resource requests/limits, missing probes, and `runAsUser: 0`. `policy/network-policy.rego` — `warn` rules only (won't fail CI). `.kube-linter.yaml` exempts resources in the `platform` namespace from root-check and privilege-escalation checks because Strimzi broker pods need those permissions.

### Progressive delivery (echo-service is a Rollout, not a Deployment)

`echo-service` is an **Argo Rollouts `Rollout`** (`kustomize/base/echo-service/rollout.yaml`), not a Deployment — this trips up two things:
- The kustomize **`replicas:` transformer does not support `Rollout`**. Per-env replicas/resources for echo-service are set with **JSON6902 patches** (`overlays/{dev,prod}/patch-echo-resources.yaml`, targeting `kind: Rollout`). worker-service/vllm-inference are still Deployments and use the normal `replicas:` transformer + strategic-merge patches.
- Canary analysis (`analysistemplate.yaml`) scopes its Prometheus queries to canary pods via the `rollouts-pod-template-hash` label, which only reaches the metrics because the ServiceMonitor sets `podTargetLabels: [rollouts-pod-template-hash]`. If you change the ServiceMonitor, keep that.

Flow: nginx traffic routing splits 10→50→100%; each pause runs `echo-service-slo` (success-rate ≥ 99%, p90 ≤ 0.5s); `failureLimit: 1` aborts + auto-reverts. The `argo-rollouts` controller is a platform service (sync-wave -1). The echo-service ArgoCD Application uses the `platform-apps` AppProject (change-freeze sync window).

### Supply chain & admission (cosign keyless + Kyverno)

`docker-build.yaml` authenticates to AWS via **GitHub OIDC** (`role-to-assume: ${{ vars.AWS_OIDC_ROLE_ARN }}`, from `terraform/modules/github-oidc`) — there are **no static AWS keys**. It scans (Trivy), attaches SBOM + SLSA provenance, and **cosign-signs each image keyless**.

Kyverno (`kubernetes/platform/kyverno/`, sync-wave -1) enforces this at admission via `kyverno-policies` (sync-wave 1):
- `verify-image-signatures` (**Enforce**) — rejects unsigned `*.dkr.ecr.*/platform-core/*` images. Scoped to our registry, so upstream images are unaffected. **`failurePolicy: Fail`** — if the Kyverno controller is down, admission of our images is blocked by design.
- `require-pod-standards` (**Enforce, `tenant-*` only**) — resources/probes/non-root. Graduated enforcement: strict on the tenant paved road, advisory elsewhere.
- `disallow-latest-tag` (**Audit**) — so local `:latest` still runs but is reported.

### Observability wiring

`observability/prometheus/rules/alerts.yaml` is a `PrometheusRule` CR deployed by ArgoCD alongside `kube-prometheus-stack`. Alert PromQL queries reference recording rules defined in `observability/prometheus/slo/recording-rules.yaml`. The Kafka consumer lag alert uses `kafka_consumergroup_lag_sum` which comes from the Strimzi Kafka Exporter sidecar — it is disabled in the dev Kafka overlay to save resources.
