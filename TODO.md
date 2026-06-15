# TODO

Outstanding work for platform-core, ordered by leverage. Reflects the repo after
the supply-chain/policy/KRaft work (commit `89a3641`), the platform extension in
section E, the **`services/` co-location refactor** (ADR-012), and the **AI audio
pipeline** in section F (commit `c8334e0`). Legend: ⬜ not started · 🟡 partial ·
✅ done.

## A. Correctness & honesty (cheap, high-impact — do first)

- ✅ **Deploy `llm-gateway` to Kubernetes.** Added `kustomize/base/llm-gateway/`
  (SA, Deployment, Service, Ingress, NetworkPolicy, PDB, ServiceMonitor),
  `overlays/{dev,prod}/llm-gateway/`, and `kubernetes/apps/llm-gateway/applicationset.yaml`
  (cluster-generator, env-selected — same pattern as the other apps). Readiness uses
  `/healthz` (not `/readyz`) to avoid a scale-from-zero deadlock against vLLM. All
  overlays build clean and satisfy the OPA deny rules. **Still to do:** prove it end
  to end on a live cluster (part of the local smoke test below).
- ✅ **Fix the worker idempotency claim.** Reworded "short-lived in-memory set"
  claims to clarify they handle redelivery "within a pod lifetime" rather than
  surviving restarts.
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
- ✅ **Resolve `GEMINI.md`.** Verified that `GEMINI.md` is removed and not present
  in the repo, adhering to the single-source convention.
- ✅ **Fix the "Full LGTM" / kube-prometheus-stack claims in docs.** Renamed
  overclaiming "LGTM" references to "Prometheus + Loki + Tempo + Grafana" in
  manifests and capability tables.
- ✅ **Reconcile the SLO numbers in one place.** Consolidated HTTP, vLLM, and
  Worker SLOs into a unified table in `docs/slo-definitions.md`. Aligned the
  canary gate to use **p95 ≤ 500ms**, matching the platform latency SLO.
  Updated `analysistemplate.yaml`, runbooks, and load tests to reflect p95.
- ⬜ **Make the worker Kafka path part of a real smoke test.** The manifests now align
  on port 9090 and SCRAM auth, but this still needs a cluster-level proof: create a
  job, verify KEDA scales worker from zero, confirm the worker commits offsets, and
  prove failed jobs land in `jobs-dlq`.

## B. End-to-end verification (does it actually run?)

- ⬜ **Full local smoke test.** `bootstrap.sh --mode=local`, confirm Cilium comes up and
  nodes go Ready, every ArgoCD Application reaches Healthy/Synced, then exercise: canary
  auto-rollback (`canary-demo.sh bad`), worker KEDA scale-from-zero on Kafka lag, vLLM CPU
  inference via llm-gateway, Kyverno rejecting an unsigned image, a Loki log query, **a
  request trace appearing in Tempo/Grafana**, **Hubble showing app flows**, **OpenCost
  reporting namespace cost**, and **the audio pipeline end-to-end** (`POST /v1/audio/jobs`
  → STT→LLM→TTS with `stub` backends → a `speech/<job_id>` object in MinIO and Redis
  job-state `done`). Capture output for the README. (The CI `e2e-smoke` job and
  `scripts/smoke-test.sh` are a start — extend them.)
- ⬜ **Validate the AWS path.** At minimum `terraform plan` against a real account for
  `environments/{dev,prod}`; ideally a throwaway EKS apply + `--mode=aws` bootstrap,
  including **Karpenter** (controller healthy, default NodePool provisions general nodes,
  GPU NodePool provisions for vLLM, interruption queue wired), the `github-oidc` role
  assumption from CI, and the Crossplane S3 claim provisioning a real bucket. Also prove
  the **audio pipeline prod path**: the `nemo`/`kokoro` GPU model images build + load their
  lazy-imported deps, stt/tts-worker scale-from-zero onto the GPU NodePool, and the
  pipeline writes to the Crossplane bucket via IRSA (no MinIO). Substitute the
  `terraform output` values into the Karpenter Helm values + EC2NodeClass placeholders.
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
- ✅ **AKHQ Kafka UI.** Dev-only platform service for topic / consumer-group
  inspection (`kubernetes/platform/akhq/applicationset.yaml`, cluster generator
  scoped to `environment=dev`, like MinIO). Plaintext connection to the Strimzi
  bootstrap; prod is intentionally excluded (would need SCRAM/SASL wiring).
  Exposed at `http://akhq.platform-core.local`.
- ✅ **`helm/platform-services` umbrella.** Deleted. It was unused (no ArgoCD app or
  script referenced it), duplicated the platform chart version pins as a second source
  of truth, and had already drifted (it pinned external-secrets `0.9.0` while the live
  app is on `0.9.18`). Removed the chart and its references in `README.md`/`helm/README.md`.
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
- ✅ **Extract ADRs.** Created `docs/adr/` and split the 18 decisions from
  `architecture.md` into individual numbered files with descriptive names
  (e.g. `adr-011-infrastructure-cost-minimization.md`). `architecture.md` now
  serves as a linked index.
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

## E. Platform extension — tracing, networking, cost, autoscaling

- ✅ **Distributed tracing (OpenTelemetry + Tempo).** Added `platform/tempo` (single-binary
  Tempo, OTLP receivers) and `platform/opentelemetry-collector` (OTLP→Tempo pipeline) in
  `monitoring`; wired a Grafana Tempo datasource with tracesToLogs/Metrics correlation and a
  `trace_id`→Tempo derived field on Loki. Apps already had OTel code — wired `OTEL_*` env into
  their base manifests (export to `otel-collector.monitoring:4317`). **Fixed two latent bugs
  the instrumentation had:** stale `uv.lock`s (OTel was in `pyproject` but never locked → the
  `--frozen` Docker build would fail) and `opentelemetry-instrumentation 0.46b0` importing
  `pkg_resources` (gone from setuptools ≥81) → echo/llm-gateway crashed on import. Bumped the
  OTel stack to 1.29.0 / 0.50b0, added an `OTEL_SDK_DISABLED` guard + `conftest.py` so tests
  don't block on export. All three app test suites pass.
- ✅ **Cilium + Hubble (local).** `kind-config.yaml` disables the default CNI; `bootstrap.sh
  --mode=local` installs Cilium (eBPF, `ipam.mode=kubernetes`) + Hubble before the App-of-Apps
  and waits for node readiness. EKS keeps the AWS VPC CNI (documented, deliberate). Hubble UI
  port-forward documented.
- ✅ **Karpenter (prod/EKS).** Terraform `karpenter` submodule (controller IRSA role, node role
  + access entry, SQS interruption queue) gated by `enable_karpenter`; `karpenter.sh/discovery`
  tags on subnets/SG; prod-only `platform/karpenter` ApplicationSet with one `EC2NodeClass` +
  default/GPU `NodePool`s. Replaced the static prod GPU managed node group
  (`enable_gpu_nodegroup=false`). Terraform validates for dev+prod. **Unverified on a live
  account** (see section B) — Helm/EC2NodeClass carry `terraform output` placeholders.
- ✅ **OpenCost (FinOps).** `platform/opencost` wired to the kube-prometheus-stack Prometheus,
  ServiceMonitor scraping its metrics, and a `cost-finops` Grafana dashboard (cost by node /
  namespace). All three charts (Tempo, OTel Collector, OpenCost) `helm template` cleanly with
  the embedded values.

## F. AI audio pipeline (STT → LLM → TTS over Kafka)

A multi-stage event-driven AI workflow (upload → speech-to-text → summarize → text-to-speech),
built in parallel "chunks" against a frozen seam contract. Contract: `docs/audio-pipeline-contract.md`.

- ✅ **Chunk 0 — freeze the inter-chunk contract.** `docs/audio-pipeline-contract.md`: Kafka
  envelope (key=`job_id`, W3C trace headers, `created_at` carried forward), the 7 `audio.*`
  topics + per-stage DLQs, object-storage key scheme (`audio/transcripts/summaries/speech/`),
  Redis job-state schema, the `llm-gateway` LLM contract, metric names, and the locked
  decisions (Redis job-state, MinIO-dev/Crossplane-S3-prod, pluggable `stub`/GPU backends).
- ✅ **Chunk 1 — platform infra.** 7 `audio.*` KafkaTopics + DLQs and per-service KafkaUsers;
  **MinIO** (dev-only ApplicationSet) object store; **Redis** (all-env, in-repo upstream
  manifest after dropping the Bitnami chart) job-state store; prod Crossplane S3 claim. Fixed
  duplicate `S3_BUCKET`/`REDIS_URL`/`S3_REGION` env keys and stopped re-generating the shared
  env-config ConfigMap.
- ✅ **Chunk 2 — `audio-api`.** FastAPI upload (`POST /v1/audio/jobs`, size/MIME validation),
  S3/MinIO write, Redis `queued` state, `audio.jobs` produce, `GET /jobs/{id}` status.
- ✅ **Chunk 3 — `stt-worker`.** Parakeet/NeMo speech-to-text (pluggable `STT_BACKEND=stub|nemo`),
  writes the full transcript blob, produces `audio.transcripts`.
- ✅ **Chunk 4 — `llm-worker`.** Summarization via the existing `llm-gateway` (never vLLM
  directly); writes structured summary + `action_items[]`, produces `audio.summaries`.
- ✅ **Chunk 5 — `tts-worker`.** Kokoro text-to-speech terminal stage (`TTS_BACKEND=stub|kokoro`),
  writes `speech/<job_id>`, produces `audio.results`, sets Redis `done`, increments the
  pipeline end-to-end metrics.
- ✅ **Chunk 6 — observability.** `audio-ai-pipeline` Grafana dashboard + per-stage DLQ /
  failure alerts.
- ✅ **Chunk 7 — Backstage.** All four services + their topic APIs registered in the catalog.
- ✅ **Dashboard deep-dive (perf/size/cost/GPU/queue).** Added worker metrics
  `pipeline_queue_wait_seconds{stage}` (now − `created_at` at receipt, all 3 workers),
  `stt_transcript_bytes`, `llm_summary_bytes`; deployed a prod-only **`dcgm-exporter`**
  ApplicationSet (GPU `DCGM_FI_DEV_*` via ServiceMonitor); expanded `audio-ai-pipeline.json`
  to 17 panels (queue wait, transcript/summary size, audio-services cost via OpenCost, GPU
  utilization). GPU + cost panels are prod-only ("No data" on kind by design).
- ✅ **Wiring fixes.** Aggregate CI overlay covers the new apps; KEDA scale-from-zero unblocked
  (activation lag threshold 0, ArgoCD selfHeal no longer reverts the scale); NetworkPolicies
  for llm-worker↔gateway; seccomp/PSA/topology-spread/label hygiene hardening across workloads.
- ⬜ **End-to-end proof.** Folded into the §B full local smoke test (and the §B AWS GPU-path
  item for the real `nemo`/`kokoro` models).

## G. Repository structure

- ✅ **Co-locate app source with its k8s manifests (ADR-012).** Moved each service from
  `apps/<svc>` + central `kustomize/base+overlays` into a single `services/<svc>/` folder it
  owns (`k8s/base` + `k8s/overlays/{dev,prod}`); `kustomize/` is now only the CI `validation/`
  aggregate; the per-service ApplicationSets point at `services/<svc>/k8s/overlays/{{env}}`.
- ✅ **Split `main.py` into an `app/` package + typed config.** Each service keeps a thin
  `main.py` entrypoint plus `app/` (`config.py` pydantic-settings, `observability.py`,
  `metrics.py`, `kafka_io.py`, per-service helpers); `tool.uv.package = false` retained.
- ✅ **Generate per-service READMEs** for all pipeline + demo services.

## H. Road to 10/10 — proof, security rigor, drift-prevention

The repo is strong on design and self-honesty but caps out around 7.5–8 because it
optimizes for *surface* (breadth of features, all gates green) over *proof* (run on
real infra, under load, with failures injected). Closing that gap is what separates a
polished showcase from production-grade staff work. None of this is about adding more
features — it's about earning the claims already made. Each item has an explicit
**Done when** so it can't be marked ✅ on vibes.

### H1. Prove it, don't just render it (the single biggest gap)

- ⬜ **Stand up the AWS path for real and capture evidence.** Supersedes the §B
  "Validate the AWS path" item with a hard bar. A throwaway account, `terraform apply`
  for `environments/{dev,prod}`, `--mode=aws` bootstrap, then drive one real audio job
  end-to-end on GPU nodes.
  **Done when:** a `docs/proof/aws-runbook.md` exists with timestamped `kubectl`/AWS
  CLI output (or asciinema) showing: Karpenter provisioning a GPU node from zero, the
  `nemo`/`kokoro` images loading their lazy deps, IRSA writing to the Crossplane bucket
  (no static keys), cosign **rejecting** an unsigned image at admission, and the
  Crossplane S3 claim going `Ready`. Tear-down cost noted.
- ⬜ **Make the local smoke test a CI gate, not a script.** Promote §B's `e2e-smoke`
  from "a start" to a required check that fails the build on regression.
  **Done when:** a kind-based GitHub Actions job runs bootstrap → audio pipeline
  end-to-end → asserts a `speech/<job_id>` object in MinIO + Redis `done`, **and** drives
  `canary-demo.sh bad` asserting the Rollout reaches `Degraded` (proves the SLO gate
  actually aborts, not just that it renders). Green required to merge to `main`.
- ⬜ **Load + chaos, with SLOs as the pass/fail.** The k6 test exists but nothing
  asserts the SLOs hold under it or that the system degrades gracefully.
  **Done when:** a documented run shows (a) k6 at target RPS with p95 ≤ 500ms and
  error-rate < 1% sustained, (b) KEDA scaling worker/vLLM up and back to zero under
  the load profile, and (c) a fault-injection pass (kill a broker, kill a worker
  mid-batch, 500 from vLLM) proving no message loss and correct DLQ routing.

### H2. Close the IaC security gap (the weakest layer)

- ⬜ **Stop defaulting the prod EKS API to `0.0.0.0/0`.** (Finding #1 from the
  2026-06-11 review — the one item left unaddressed in the harden-eks branch.) Default
  `allowed_cidrs` to a required, non-wildcard value; make `0.0.0.0/0` an explicit,
  loud opt-in. Consider `cluster_endpoint_public_access = false` + a bastion/SSM path.
  **Done when:** `terraform plan` for prod with no `allowed_cidrs` override errors out
  rather than silently exposing the control plane.
- ⬜ **Move worker nodes off public subnets (or justify it in an ADR).** Today nodes
  run in public subnets with public IPs to dodge NAT cost. Either add a
  `private_nodes` toggle (private subnets + NAT/VPC-endpoint egress) defaulting on for
  prod, or write an ADR that owns the tradeoff explicitly instead of burying it in a
  comment. **Done when:** prod nodes have no public IPs, or `docs/adr/adr-019-*.md`
  documents the decision and its blast radius.
- ⬜ **SHA-pin every GitHub Action.** A repo this invested in supply-chain integrity
  (cosign, SBOM, provenance, Trivy gate) pins its own CI to mutable tags (`@v4`). Pin
  to full commit SHAs with a version comment; let Renovate bump them.
  **Done when:** `grep -rE 'uses: .*@v[0-9]' .github/` returns nothing, and Renovate is
  configured to update the pinned digests.
- ⬜ **Finish §D "Pin CI bootstrap binaries" with checksum verification.** Pinning the
  version isn't enough — verify the downloaded tarball's SHA256 so a compromised
  upstream release is caught. **Done when:** every `curl | ...` install in CI checks a
  pinned checksum.

### H3. Make drift impossible, not just fixed

The review keeps finding the same *class* of bug: a doc/comment/policy that asserts
something the manifests contradict (the "stub" that wasn't, the vLLM netpol namespace,
ESO version skew, the spec's Implementation Status table). Fixing instances is
whack-a-mole; the 10/10 move is to make the assertions machine-checked.

- ⬜ **CI-enforce the cross-references that keep drifting.** Add a lightweight check
  (script or conftest/policy) that fails when, e.g., a NetworkPolicy names a namespace
  no workload lives in, a doc references a chart/path that doesn't exist, or a chart
  version pin disagrees with the ArgoCD app that deploys it.
  **Done when:** deleting a service or moving a namespace breaks CI until the docs/
  policies are updated to match.
- ⬜ **Generate the spec's Implementation Status table** (already §D, restated as a gate).
  **Done when:** the table is generated from the repo (or stamped with the describing
  commit SHA) and a CI check fails if it's hand-edited stale.
- ⬜ **Promote `findings.md` to a tracked, dated changelog.** The self-review is a
  genuine strength — but a 444-line file in the repo root that mixes fixed and open
  items is itself drift bait. Split: open items → this TODO; resolved items → a dated
  `docs/review-log.md`. **Done when:** `findings.md` no longer contains stale
  "Status: verified" claims about code that has since moved.

### H4. Depth over breadth (pick the spine and make it bulletproof)

- ⬜ **Designate one "golden path" and make it provably production-grade**, rather than
  ten surfaces each one-cluster-deep. The audio pipeline is the natural spine. Give it:
  a real DR story (what happens when Redis/MinIO/a broker is lost mid-flight), a
  documented backpressure/poison-pill story, and a runbook proven by actually causing
  the incident. **Done when:** `docs/runbooks/audio-pipeline-*.md` each end with a
  "verified by injecting X on <date>, observed Y" line.
- ⬜ **Add the one test layer that's missing: contract tests on the frozen Kafka seam.**
  `docs/audio-pipeline-contract.md` is asserted in prose; nothing fails when a producer
  drifts from it. **Done when:** a schema/contract test (e.g. per-topic JSON schema
  validated in each service's suite) fails CI if any stage emits an event that violates
  the envelope (`job_id` key, trace headers, `created_at` carried forward).
