# TODO

Outstanding work for platform-core, ordered by leverage. Reflects the repo at
commit `cd7d3cb` (2026-06-01). Legend: ⬜ not started · 🟡 partial.

## A. Correctness & honesty (cheap, high-impact — do first)

- ✅ **Deploy `llm-gateway` to Kubernetes.** Added `kustomize/base/llm-gateway/`
  (SA, Deployment, Service, Ingress, NetworkPolicy, PDB, ServiceMonitor),
  `overlays/{dev,prod}/llm-gateway/`, and `kubernetes/apps/llm-gateway/applicationset.yaml`
  (cluster-generator, env-selected — same pattern as the other apps). Readiness uses
  `/healthz` (not `/readyz`) to avoid a scale-from-zero deadlock against vLLM. All
  overlays build clean and satisfy the OPA deny rules. **Still to do:** prove it end
  to end on a live cluster (part of the local smoke test below).
- ⬜ **Fix the worker idempotency claim.** Docs say a "short-lived **in-memory** set"
  handles "redelivery **after pod restart**" — in-memory state can't survive a
  restart. Either back it with a persistent store (Redis / compacted topic) or
  reword to "within a pod lifetime."
- ✅ **Reorder the `docker-build.yaml` image gate.** Now build (local `load`, no
  push) → Trivy gate → push w/ SBOM+provenance → cosign sign → promote. The scan
  runs before anything reaches ECR; the two builds share the GHA cache. Promotion now
  pins the **immutable digest** into the **per-app** prod overlay
  (`overlays/prod/<app>`) — fixing a latent bug where it edited the aggregate
  `overlays/prod/` that ArgoCD never deploys — with a rebase-and-retry around the
  parallel-matrix push. Dropped the mutable `:latest` registry tag.
- ✅ **Stop shipping prod `:latest` defaults.** Prod per-app + aggregate overlays now
  render `:unpromoted` (un-pullable placeholder → fails closed until a digest is
  promoted), never `:latest`. `kyverno-policies` became an **ApplicationSet**
  (cluster-generator, `overlays/{{env}}`); `disallow-latest-tag` is **Audit in dev,
  Enforce in prod**. Builds verified: dev=Audit, prod=Enforce.
- ⬜ **Resolve `GEMINI.md`.** It's untracked and duplicates `AGENTS.md` instead of
  importing it — at odds with the single-source convention (`CLAUDE.md` is a
  one-line `@AGENTS.md` import). Make `GEMINI.md` import `AGENTS.md` too, or remove it.
- ⬜ **Fix the "Full LGTM" / kube-prometheus-stack claims in docs.** kube-prometheus-stack
  does **not** bundle Loki (it's a separate chart — which is why `platform/loki`
  exists). And there's no Tempo (tracing) or Mimir, so "Full LGTM" overclaims —
  rename to "Prometheus + Loki + Grafana" or add tracing.
- ⬜ **Reconcile the SLO numbers in one place.** Canary gate uses p90 ≤ 0.5s while the
  platform latency SLO is p95 < 500ms; vLLM availability is 99.0% vs platform 99.5%.
  Put one table in `docs/slo-definitions.md` and reference it from §7/§9/§11.
- ⬜ **Make the worker Kafka path part of a real smoke test.** The manifests now align
  on port 9090 and SCRAM auth, but this still needs a cluster-level proof: create a
  job, verify KEDA scales worker from zero, confirm the worker commits offsets, and
  prove failed jobs land in `jobs-dlq`.

## B. End-to-end verification (does it actually run?)

- ⬜ **Full local smoke test.** `bootstrap.sh --mode=local`, confirm every ArgoCD
  Application reaches Healthy/Synced, then exercise: canary auto-rollback
  (`canary-demo.sh bad`), worker KEDA scale-from-zero on Kafka lag, vLLM CPU inference
  via llm-gateway, Kyverno rejecting an unsigned image, and a Loki log query. Capture
  output for the README. (The CI `e2e-smoke` job is a start — extend it.)
- ⬜ **Validate the AWS path.** At minimum `terraform plan` against a real account for
  `environments/{dev,prod}`; ideally a throwaway EKS apply + `--mode=aws` bootstrap,
  including the GPU node group, `github-oidc` role assumption from CI, and the
  Crossplane S3 claim provisioning a real bucket.
- ⬜ **Exercise Terraform plan OIDC in CI.** `terraform-plan.yaml` no longer uses static
  AWS keys, but the workflow should be proven from a PR with `AWS_OIDC_ROLE_ARN`
  configured. Add a clear skip/failure mode when the repo variable is absent.

## C. Finish partial surfaces

- 🟡 **Backstage frontend plugins.** ArgoCD + Grafana plugins are configured (backend
  proxy, values-gated) but need a **custom Backstage image** — they're not in the
  stock image. Build the image or document that those panels are inert until then.
- ⬜ **Kafka Exporter on the validated path.** It's disabled in the dev overlay, so the
  `KafkaConsumerLagHigh` / `KafkaDLQNonEmpty` alerts and the consumer-lag dashboard
  panels have no data on local kind. Enable it in dev (cheap), or scope those
  alerts/panels to prod and say so.
- ⬜ **AKHQ Kafka UI.** Optional platform service for topic / consumer-group inspection.
- ⬜ **`helm/platform-services` umbrella.** Currently a `Chart.yaml` stub — either flesh
  it out or delete it and remove the references.
- ✅ **Turn the OPA NetworkPolicy check into a real relationship check.** CI now runs
  `conftest --combine`; `policy/network-policy.rego` **denies** (hard fail) when a
  workload in `apps`/`tenant-*` has no NetworkPolicy whose `podSelector` actually
  matches its pod labels — replacing the old per-Deployment "ensure one exists" warn.
  Verified with a negative test (rogue workload fails; real apps pass).
- ✅ **Extend policy coverage to Rollouts.** Policies now operate on a `workloads` set
  (Deployment, Rollout, StatefulSet, DaemonSet) via `policy/lib.rego`; the resources/
  probes/non-root denies and the PDB/NetworkPolicy checks all cover the echo-service
  Rollout. **This immediately caught a real gap:** echo-service's `pdb.yaml` existed
  but was never wired into its kustomization, so the canary workload had no disruption
  protection — now fixed (added to `kustomize/base/echo-service/kustomization.yaml`).

## D. Polish / governance

- ⬜ **ArgoCD RBAC + Slack notifications.** Platform-vs-app-team RBAC and sync-failure
  alerts (the `ArgoCDSyncFailed` alert has nowhere to route today — Alertmanager is
  disabled for local).
- ⬜ **Harden EKS defaults.** `allowed_cidrs` defaults to `0.0.0.0/0`, control-plane
  logs are not explicitly enabled, and secrets encryption/KMS posture is not visible.
  Make secure defaults the module default and require explicit opt-outs.
- ⬜ **Pin CI bootstrap binaries.** Several jobs download `latest` release tarballs or
  install scripts at runtime. Pin versions and, where practical, verify checksums so
  CI itself is reproducible and less exposed to upstream drift.
- ⬜ **Extract ADRs.** The spec references ADR-008/009/010; create `docs/adr/` and split
  the decisions currently inline in `architecture.md` into numbered ADRs.
- ✅ **Modernize Kafka to KRaft.** Dropped `spec.zookeeper`, added the
  `strimzi.io/kraft` + `strimzi.io/node-pools` annotations, and moved replicas/storage
  into a new combined controller+broker `KafkaNodePool` (`base/nodepool.yaml`). Dev
  overlay patches the pool to 1 node / `standard` storage; prod stays 3 nodes / gp3.
  Added `KafkaNodePool` to the CI kubeconform skip list; both overlays build clean
  with zero ZooKeeper references.
- ⬜ **Clarify the vLLM namespace.** The tenant `allow-to-platform` NetworkPolicy opens
  egress to the **platform** namespace for vLLM:8000, but vLLM is deployed under
  `apps`. Align the policy with where vLLM actually runs.
- ⬜ **Automate the spec's Implementation Status table.** It drifted from the repo within
  a day. Stamp it with the commit SHA it describes and add a CI check (or generate it).
