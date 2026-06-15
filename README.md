# platform-core

> A production-grade Internal Developer Platform with GitOps delivery and AI inference capability.

[![CI](https://github.com/filipurminsky/platform-core/actions/workflows/ci.yaml/badge.svg)](https://github.com/filipurminsky/platform-core/actions/workflows/ci.yaml)
[![Terraform](https://github.com/filipurminsky/platform-core/actions/workflows/terraform-plan.yaml/badge.svg)](https://github.com/filipurminsky/platform-core/actions/workflows/terraform-plan.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## What This Is

A fully working reference implementation of a modern Internal Developer Platform (IDP) built on Kubernetes. It demonstrates senior platform engineering patterns: GitOps delivery via ArgoCD, event-driven autoscaling with KEDA + Kafka, self-hosted LLM inference with vLLM, and a Backstage developer portal wired to real infrastructure.

Designed to run **locally** (kind cluster, zero AWS cost) or on **AWS EKS** (production-realistic).

---

## Architecture

A developer pushes to Git; ArgoCD syncs it onto a Kubernetes cluster that
Terraform provisioned (EKS in the cloud, kind locally).

```mermaid
flowchart LR
    Dev([Developer]) -->|git push| Git[(Git)]
    Git -->|GitOps sync| Argo[ArgoCD]
    Argo --> Cluster[Kubernetes Cluster]
    Terraform -.provisions.-> Cluster
```

Inside the cluster:

- **Apps** — echo-service, worker-service, the llm-gateway + vLLM inference, and the AI audio pipeline (`audio-api → stt → llm → tts`).
- **Platform services** — Kafka (Strimzi), KEDA autoscaling, Redis, MinIO, cert-manager, External Secrets.
- **Observability** — Prometheus, Grafana, Loki (logs), Tempo (traces).

---

## What This Demonstrates

| Capability | Implementation |
|---|---|
| Kubernetes cluster operations | EKS + Kustomize overlays + Argo Rollouts canary + PDB + Network Policies |
| Terraform IaC | Modular AWS + kind modules + GPU node group + remote state |
| Cloud infra self-service | Crossplane S3 API — teams claim a secure bucket via `kubectl`; XRD + Composition + IRSA (AWS-only slice) |
| Internal developer platform | Backstage with scaffolding templates, catalog, and plugins |
| Multi-tenancy | Namespace-per-team via ApplicationSet + ResourceQuota/LimitRange tiers + zero-trust NetworkPolicies + per-tenant RBAC |
| GitOps / SDLC automation | ArgoCD App-of-Apps + GitHub Actions (lint / validate / plan) |
| Event-driven architecture | Kafka (Strimzi) + consumer groups + DLQ + idempotent processing ([SLOs](docs/slo-definitions.md)) |
| Scale-to-zero autoscaling | KEDA Kafka consumer-lag trigger (worker) + Prometheus trigger (vLLM) |
| SLO / error budgets | Prometheus recording rules + Grafana burn-rate dashboards + runbooks ([Definitions](docs/slo-definitions.md)) |
| AI workload infrastructure | vLLM + GPU scheduling + inference SLOs + llm-gateway |
| Observability | Prometheus + Grafana + Loki + Tempo + structured alerting ([Reference](docs/slo-definitions.md)) |
| Progressive delivery | Argo Rollouts canary + Prometheus SLO analysis + automatic rollback on error-budget burn |
| Supply chain security | cosign keyless signing + SBOM + SLSA provenance + Trivy scan + GitHub OIDC (no static keys) |
| Dependency hygiene | Renovate keeps Actions, Python deps, base images, Helm charts, Crossplane providers & `kindest/node` current via CI-gated PRs |
| Policy as code | Kyverno admission control (verify image signatures, pod standards) — graduated enforcement |
| Security | RBAC + Network Policies + IRSA + Kafka SCRAM-SHA-512 + External Secrets + image signing |

---

## Prerequisites

**Local mode (kind):**
- Docker Desktop
- `kind` ≥ 0.20
- `kubectl` ≥ 1.28
- `helm` ≥ 3.12
- `terraform` ≥ 1.6

**AWS mode (EKS):**
- All of the above
- AWS CLI configured (`aws configure`)
- IAM permissions: EKS, EC2, VPC, IAM, S3, DynamoDB, ECR

---

## Quick Start

> Common tasks are wrapped in a `Makefile` — run `make help` to list them
> (`make validate` runs the full local check suite, `make up` bootstraps a local
> kind cluster, `make test` runs the app unit tests). Contributors: run
> `make hooks` once to install the pre-commit/pre-push git hooks
> (`.pre-commit-config.yaml`) so lint/format/test issues surface before CI.

### Local — no AWS cost

```bash
# Clone and bootstrap a local kind cluster
git clone https://github.com/filipurminsky/platform-core.git
cd platform-core
./scripts/bootstrap.sh --mode=local

# Services available at:
#   Backstage    →  http://backstage.platform-core.local
#   ArgoCD       →  http://localhost:8080
#   Grafana      →  http://localhost:3000
#   vLLM API     →  http://localhost:8000/v1
#   AKHQ (Kafka) →  http://akhq.platform-core.local  (dev only)

# Trigger worker-service autoscaling by producing test jobs
# (spins up a throwaway Strimzi producer pod and pipes in the sample job):
kubectl -n platform run kafka-producer --rm -i --restart=Never \
  --image=quay.io/strimzi/kafka:0.40.0-kafka-3.7.0 -- \
  bin/kafka-console-producer.sh \
    --bootstrap-server platform-kafka-kafka-bootstrap.platform.svc:9092 \
    --topic jobs < scripts/sample-job.json
```

### AWS — production-realistic

```bash
cd terraform/environments/dev
terraform init
terraform apply          # provisions VPC + EKS + ECR
cd ../../..
./scripts/bootstrap.sh --mode=aws
```

---

## Repository Structure

```
platform-core/
├── docs/                    # Architecture decisions, SLOs, runbooks, diagrams
├── terraform/               # Infrastructure as Code (kind + EKS + GPU node group)
│   ├── modules/             # Reusable modules: eks, networking, iam, gpu-nodegroup, kind
│   └── environments/        # dev + prod root modules
├── kubernetes/
│   ├── bootstrap/argocd/    # One-time ArgoCD installation
│   ├── platform/            # ArgoCD Applications for platform services
│   │   ├── kafka/           # Strimzi Kafka cluster + topics + users
│   │   ├── keda/            # KEDA operator
│   │   └── crossplane/      # Crossplane + AWS S3 self-service API (XRD/Composition)
│   └── apps/                # ArgoCD ApplicationSets (GitOps registration, one per service)
├── services/                # One folder per service: source + co-located k8s manifests
│   ├── echo-service/        #   main.py, Dockerfile, catalog-info.yaml, …
│   │   └── k8s/             #   base/ + overlays/{dev,prod}/ (what ArgoCD deploys)
│   └── <svc>/               #   worker-service, llm-gateway, audio-api, stt/tts/llm-worker, vllm-inference
├── helm/                    # Custom Helm charts (demo-app, vllm, backstage)
├── kustomize/validation/    # CI-only aggregate overlay (kubeconform + OPA --combine)
├── backstage/               # Backstage config, catalog, scaffolding templates
├── observability/           # Prometheus rules, Grafana dashboards, Loki config
├── .github/workflows/       # CI, Terraform plan, Docker build pipelines
└── scripts/                 # bootstrap.sh, teardown.sh
```

---

## Key Design Decisions

See [`docs/architecture.md`](docs/architecture.md) for full ADRs. Summary:

- **ArgoCD over Flux** — richer UI, RBAC model, and broader ecosystem adoption
- **Strimzi over MSK/SQS** — runs identically on kind and EKS; partition-level lag metrics for precise KEDA autoscaling
- **Kustomize overlays over Helm values files** — cleaner separation of env-specific patches without templating complexity
- **External Secrets Operator** — secrets live in AWS Secrets Manager; never committed to Git
- **vLLM over TGI/Triton** — OpenAI-compatible API, PagedAttention for KV cache efficiency, built-in Prometheus metrics
- **Terraform + Crossplane, split by lifecycle** — Terraform for the day-0 foundation (VPC/EKS/IAM, the seed Crossplane can't bootstrap itself); Crossplane for day-2 app-facing infra exposed as self-service Kubernetes APIs. See [`docs/crossplane.md`](docs/crossplane.md)

---

## Runbooks

| Alert | Runbook |
|---|---|
| `PodCrashLooping` | [pod-crash-loop.md](docs/runbooks/pod-crash-loop.md) |
| `HighErrorRate` | [high-error-rate.md](docs/runbooks/high-error-rate.md) |
| `KafkaConsumerLagHigh` | [kafka-consumer-lag.md](docs/runbooks/kafka-consumer-lag.md) |
| `VllmLatencyBreach` | [vllm-inference-latency.md](docs/runbooks/vllm-inference-latency.md) |

---

## License

MIT
