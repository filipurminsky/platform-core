# Architecture Decision Records

This document captures the key design decisions made for platform-core, the reasoning behind each choice, and the trade-offs accepted.

---

## ADR-001: ArgoCD over Flux

**Decision:** Use ArgoCD as the GitOps controller.

**Reasoning:**
- ArgoCD's UI gives hiring managers and teammates a visual overview of sync state across all Applications — important for a showcase project and for real-world incident response
- RBAC model maps naturally to platform-team-vs-app-team permissions (project-scoped Applications)
- ArgoCD ApplicationSets enable templating multiple environments from a single definition
- Broader industry adoption in 2024–2025

**Trade-offs:** Flux has a lighter footprint and tighter Helm OCI support; either would work. ArgoCD chosen for visibility.

---

## ADR-002: Strimzi (self-hosted Kafka) over MSK / SQS

**Decision:** Run Kafka on Kubernetes via the Strimzi operator rather than AWS MSK or SQS.

**Reasoning:**
1. **Dev/prod parity** — the same Strimzi `Kafka` CR runs on a local kind cluster and EKS. MSK does not run locally, which would create a gap between development and production.
2. **KEDA precision** — Kafka's per-partition consumer lag is a better autoscaling signal than a coarse SQS `ApproximateNumberOfMessages`. KEDA's `kafka` trigger scales one replica per `lagThreshold` messages per partition, enabling proportional scale-out.
3. **Metric depth** — Strimzi exposes JMX metrics and a Kafka Exporter sidecar, giving full broker, topic, and consumer group observability in Grafana.
4. **No AWS lock-in** — the platform can run on GKE or AKS without changing application code.

**Trade-offs:** Strimzi adds operational burden (ZooKeeper or KRaft quorum, storage management). For a startup this might not be worth it vs. MSK; for a multi-cloud or cost-sensitive org, the trade-off is justified.

---

## ADR-003: Kustomize overlays over Helm values files per environment

**Decision:** Use Kustomize to patch manifests across environments; Helm only for packaging reusable charts.

**Reasoning:**
- Kustomize patches are surgical and diff-friendly — a PR that only changes a resource limit is one line, not a buried values file change
- Helm's `values.yaml` inheritance across environments is workable but requires templating complexity (`{{ if eq .Values.env "prod" }}`) that obscures intent
- Kustomize `overlays/dev` and `overlays/prod` make it immediately clear what differs between environments

**Trade-offs:** Kustomize doesn't support secrets generation or Helm hooks natively; External Secrets Operator handles secret rotation, and init containers handle lifecycle hooks.

---

## ADR-004: External Secrets Operator over sealed-secrets or SOPS

**Decision:** Use External Secrets Operator (ESO) pulling from AWS Secrets Manager.

**Reasoning:**
- Secrets never touch Git — not even encrypted. ESO syncs directly from Secrets Manager to Kubernetes Secrets at runtime.
- Rotation is automatic: when a secret rotates in Secrets Manager, ESO re-syncs the Kubernetes Secret within the configured `refreshInterval`
- IRSA ensures ESO's service account has least-privilege access to only the secrets in its environment prefix

**Trade-offs:** Requires AWS Secrets Manager (cost). For local dev, secrets are mounted from `kubectl create secret` commands run by `bootstrap.sh`.

---

## ADR-005: vLLM over TGI (Text Generation Inference) or Triton

**Decision:** Use vLLM as the inference engine.

**Reasoning:**
- **OpenAI-compatible API** — zero application code changes to swap from OpenAI to self-hosted; the llm-gateway proxies the same `/v1/chat/completions` endpoint
- **PagedAttention** — vLLM's KV cache management enables higher throughput on the same GPU vs. TGI at comparable model sizes
- **Built-in Prometheus metrics** — `vllm:num_requests_waiting`, `vllm:e2e_request_latency_seconds`, and GPU cache metrics are available out of the box with no exporter sidecar needed
- **KEDA integration** — `vllm:num_requests_waiting` feeds directly into a KEDA Prometheus trigger for scale-to-zero

**Trade-offs:** TGI has better support for speculative decoding and some quantisation modes. Triton is more flexible for custom model serving. vLLM chosen for its developer experience and observability story.

---

## ADR-006: Scale-to-zero for both vLLM and worker-service

**Decision:** Both vLLM and worker-service have `minReplicaCount: 0` in their KEDA ScaledObjects.

**Reasoning:**
- GPU nodes are expensive ($0.50–$1.50/hr for g4dn.xlarge). Scaling to zero and letting Cluster Autoscaler terminate the GPU node when idle eliminates idle cost entirely.
- worker-service has no traffic at night/weekends — running 2 replicas 24/7 wastes resources and makes SLO math misleading (high availability of an idle service).
- KEDA's `activationThreshold` and `cooldownPeriod` control the cold-start behaviour: vLLM has a 300 s cooldown (model loading is slow); worker-service has 60 s.

**Trade-offs:** Cold starts. vLLM takes 30–120 s to load a 7B model from a PVC-cached checkpoint. Mitigated by the PVC model cache (avoids re-downloading) and a readiness probe that blocks traffic until the model is loaded.

---

## ADR-007: App-of-Apps pattern for GitOps

**Decision:** One root ArgoCD Application points to `kubernetes/platform/`; each subdirectory is an independent Application.

**Reasoning:**
- Platform team controls which Applications exist (by merging to main)
- App teams get autonomy within their Application's source path
- Adding a new platform service = adding a directory + ArgoCD Application manifest, merged via PR with plan review

**Trade-offs:** More ArgoCD objects to manage. ApplicationSets would reduce boilerplate but add abstraction; App-of-Apps chosen for transparency.

---

## ADR-008: SLO-gated progressive delivery (Argo Rollouts) over ArgoCD auto-sync

**Decision:** Deliver user-facing services (starting with `echo-service`) as Argo Rollouts **canary** deployments whose promotion is gated by an `AnalysisTemplate` querying Prometheus, instead of letting ArgoCD apply the new ReplicaSet at 100% immediately.

**Reasoning:**
- A green CI pipeline does not prove a release is healthy *under real traffic*. The canary shifts 10% → 50% → 100% via nginx traffic routing, pausing to measure the canary pods' success-rate and p90 latency against the same thresholds as our SLOs.
- `failureLimit: 1` means a single breaching sample aborts the rollout and Argo Rollouts automatically reverts to the stable ReplicaSet — the error budget *governs* the deploy rather than just being charted.
- Metrics are scoped to canary pods via `rollouts-pod-template-hash` (copied onto series by the ServiceMonitor's `podTargetLabels`), so the analysis measures only the new version.
- An ArgoCD `AppProject` (`platform-apps`) adds a change-freeze sync window, separating app delivery governance from platform services.

**Trade-offs:** Rollouts add a CRD and controller, and require traffic routing (nginx here). The replicas transformer doesn't understand `Rollout`, so per-env replica/resource values are set with explicit JSON6902 patches. For purely internal/stateless jobs (worker-service) a canary adds little value, so those stay Deployments. Accepted: the safety and the demonstrable "bad deploy auto-reverted by its own SLO" loop are worth the moving parts on the request-serving path.

---

## ADR-009: Sign and verify the software supply chain end to end

**Decision:** Every image built in CI is vulnerability-scanned (Trivy), gets an SBOM and max-mode SLSA provenance attestation (BuildKit), and is **signed keyless with cosign** (Fulcio/Rekor, identity bound to the GitHub Actions workflow). CI authenticates to AWS via **GitHub OIDC federation** — no static `AWS_ACCESS_KEY_ID`/`SECRET` anywhere.

**Reasoning:**
- A platform team's core promise is a *trusted* paved road. Unsigned images with no provenance undermine that; signing + attestation make image origin and contents verifiable.
- Keyless signing avoids long-lived signing keys: the signing identity is the ephemeral workflow OIDC token, logged in the Rekor transparency log.
- OIDC federation (`terraform/modules/github-oidc`) removes the single worst credential-handling smell — long-lived cloud keys in a CI secret — and scopes the IAM role to `ecr:...repository/platform-core/*`.

**Trade-offs:** Sigstore introduces an external dependency (Fulcio/Rekor); for an air-gapped environment you'd self-host or switch to key-pair signing. Accepted for a public, cloud-native showcase.

---

## ADR-010: Enforce policy at admission (Kyverno), not only in CI

**Decision:** Promote the CI `conftest`/OPA checks to **in-cluster Kyverno `ClusterPolicy`** admission control. Kyverno rejects unsigned `platform-core` images (`verifyImages`, keyless), and enforces resources/probes/non-root for tenant workloads; `:latest` is audited cluster-wide.

**Reasoning:**
- CI-only policy is advisory: anyone with `kubectl apply` (or a compromised controller) bypasses every rule. Admission control makes the guardrails non-bypassable at the API server.
- **Graduated enforcement** reflects real org dynamics: `Enforce` strict standards on the governed paved road (`tenant-*` namespaces); rely on operators + CI for platform's own namespaces (e.g. Strimzi broker pods legitimately need root). This mirrors the namespace/PSA split from the multi-tenancy work.
- `verifyImages` is the runtime counterpart to ADR-009 signing: the chain is only as strong as its enforcement point.

**Trade-offs:** A failing/over-strict admission webhook can block deploys cluster-wide (`failurePolicy: Fail` on the signature policy is deliberate but operationally sharp — it needs the controller healthy). Image verification adds admission latency. Accepted: enforcement is the point; the alternative (advisory policy) provides little real assurance.
