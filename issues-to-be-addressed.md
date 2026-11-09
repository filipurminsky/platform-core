# Issues To Be Addressed — Independent Code Review

**Reviewer perspective:** staff-level platform engineer, adversarial read.
**Scope:** Python services, Kubernetes/kustomize, Helm, Terraform, CI/CD, OPA/Kyverno.
**Date:** 2026-06-15 · **Branch:** `harden-eks-netpol-cleanup`

> This is a *fresh* pass, not a re-run of `findings.md`/`TODO.md`. Where it overlaps
> with those, it is noted. Each item cites `file:line` and states whether it was
> **confirmed by code reading** or **would require a live cluster to confirm**
> (much of the AWS/prod path is authored-but-unverified, per `AGENTS.md`).

Severity key: **P0** = breaks a core advertised capability / serious security exposure ·
**P1** = real correctness/operational risk · **P2** = hardening, consistency, cost, polish.

---

## Summary table

| # | Severity | Area | One-liner |
|---|----------|------|-----------|
| 1 | P0 | KEDA / Kafka | Prod Kafka scalers have no broker CA → TLS auth fails → scale-from-zero never fires |
| 2 | P0 | Terraform/EKS | API server open to `0.0.0.0/0` in prod (default never overridden) |
| 3 | P1 | Terraform/VPC | Worker nodes in public subnets with public IPs in prod |
| 4 | P1 | CI security | IaC scanners never see the real cluster config (registry-module blind spot) |
| 5 | P1 | Redis | Job-state store is non-durable (emptyDir, no persistence, SPOF) vs. advertised 7-day TTL |
| 6 | P1 | audio-api | Per-request Kafka `flush()` uses a global counter → false "delivery failed" under load |
| 7 | P1 | Kyverno | Pod admission coupled to public sigstore (`failurePolicy: Fail`) |
| 8 | P1 | Helm | demo-app HPA + hardcoded `replicas` fight; `:latest` default, no `required` repo |
| 9 | P2 | llm-gateway | Rate limiter is per-pod → effective limit scales with replica count |
| 10 | P2 | NetworkPolicy | Several `egress: - {}` / unscoped `:443`/`:53` rules undermine zero-trust |
| 11 | P2 | Tenancy | Tenant egress to `apps:8000` lets tenants bypass the llm-gateway |
| 12 | P2 | audio-api | `/readyz` does a blocking `redis.ping()` on the event loop |
| 13 | P2 | Terraform/IAM | Dead IAM roles (cluster-autoscaler, ecr-pull→`apps:default`) |
| 14 | P2 | CI supply chain | CI tool binaries fetched via `curl | tar` with no checksum verification |
| 15 | P2 | CI | Promotion job only avoided looping by GitHub's `GITHUB_TOKEN` no-recursion rule |
| 16 | P2 | Storage | No S3 lifecycle policy + versioning on → unbounded object/version growth |
| 17 | P2 | Workers | Redis job-state read-modify-write is non-atomic (lost-update window) |
| 18 | P2 | Misc | k8s version drift in TF defaults; dedup-TTL < job-TTL; producer not idempotent |

---

## P0 — Breaks a core capability or serious exposure

### 1. Prod KEDA Kafka scalers cannot authenticate → scale-from-zero is dead
**Confirmed by code reading** (live-cluster impact, prod path).
Files:
- `services/stt-worker/k8s/overlays/prod/trigger-auth.yaml`
- `services/stt-worker/k8s/overlays/prod/patch-scaledobject-kafka-auth.yaml`
- same pair under `services/{worker-service,llm-worker,tts-worker}/k8s/overlays/prod/`

The prod patch turns on broker TLS for the KEDA trigger:
```yaml
- op: add
  path: /spec/triggers/0/metadata/tls
  value: enable
- op: add
  path: /spec/triggers/0/metadata/sasl
  value: scram_sha512
```
but the `TriggerAuthentication` only supplies `password`:
```yaml
spec:
  secretTargetRef:
    - parameter: password
      name: stt-worker
      key: password
```
There is **no `ca` parameter**. Strimzi's `:9093` listener presents a cert signed by the
**self-signed cluster CA**. The KEDA operator pod (in the `keda` namespace) has no copy of
that CA, so the TLS handshake to the broker fails certificate verification. The worker pods
themselves mount the CA (`KAFKA_SSL_CA_LOCATION=/mnt/kafka-certs/ca.crt`) — but **KEDA does
not**, and KEDA is what reads consumer lag.

**Consequence:** every Kafka-triggered `ScaledObject` (worker-service + all three audio
workers) goes into an error state in prod. With `minReplicaCount: 0`, the deployments stay at
**0 replicas forever** → the entire audio pipeline and worker-service never process anything in
prod. This is the single most impactful defect: the headline "KEDA scale-to-zero" capability is
non-functional on the SCRAM/TLS path.

**Fix:** add the cluster CA to each `TriggerAuthentication`, e.g. mount the
`platform-kafka-cluster-ca-cert` secret’s `ca.crt` and reference it via a `ca` parameter
(`secretTargetRef`/`configMapTargetRef`), matching the worker’s own CA mount. Verify with
`kubectl describe scaledobject` showing `READY=True` against the TLS listener.

---

### 2. Prod EKS API server is reachable from the entire internet
**Confirmed by code reading** (intentional for "showcase", but prod inherits it).
Files: `terraform/modules/eks/variables.tf:28-32`, `terraform/modules/eks/main.tf:22-23`,
`terraform/environments/prod/main.tf:31-53`.

```hcl
variable "allowed_cidrs" {
  default = ["0.0.0.0/0"]
}
...
cluster_endpoint_public_access       = true
cluster_endpoint_public_access_cidrs = var.allowed_cidrs
```
Prod’s `module "eks"` block never overrides `allowed_cidrs`, so the production control plane is
exposed to `0.0.0.0/0`. Authentication still applies, but for a project that markets supply-chain
and zero-trust rigor, an internet-open prod API endpoint is the kind of thing a hiring manager
will circle immediately. The comment "Allow kubectl from the internet (for a showcase project)"
lives in the shared module, so it silently applies to prod too.

**Fix:** in prod, set `allowed_cidrs` to the office/CI egress ranges (or front with a bastion /
private endpoint + VPN). At minimum, make the default `[]` and force every environment to opt in.

---

## P1 — Real correctness / operational risk

### 3. Prod worker nodes run in public subnets with public IPs
**Confirmed by code reading.**
Files: `terraform/modules/networking/main.tf:16-21`, `terraform/modules/eks/main.tf:19,91`.

```hcl
enable_nat_gateway      = false
map_public_ip_on_launch = true
...
subnet_ids = var.public_subnet_ids   # eks + gpu nodegroup
```
Private subnets are created (and tagged `internal-elb`) but **nodes never land in them** — every
node, including future GPU nodes running the model, gets a public IP and sits directly on the
internet. This is a cost optimization (no NAT gateway) traded against a materially larger attack
surface. For a "production" posture this is a finding; combined with #2 it’s a weak security
story overall.

**Fix:** run nodes in private subnets with a NAT gateway (or VPC endpoints for S3/ECR/etc. to
keep NAT cost low). If cost is the constraint, document it as an explicit *dev-only* shortcut and
keep prod private.

### 4. IaC security scanners have a registry-module blind spot
**Confirmed by code reading.**
Files: `.github/workflows/ci.yaml` (`checkov`, `trivy-iac` jobs).

Checkov runs `soft_fail: false` and Trivy config-scans the repo, which *looks* like strong IaC
gating. But the EKS cluster and VPC are provisioned through **Terraform Registry modules**
(`terraform-aws-modules/eks/aws`, `.../vpc/aws`). Neither Checkov (without
`download_external_modules: true`) nor Trivy evaluates the resources *inside* those modules. So
the actual cluster config — public endpoint `0.0.0.0/0` (#2), public-subnet nodes (#3) — is
**never assessed**, and CI is green. The scanning is partly theater: it gates the thin local
wrapper, not the resources that matter.

**Fix:** enable external-module download in Checkov (`--download-external-modules true`) or add
explicit `aws_eks_cluster`/subnet assertions; add targeted policy that fails on
`cluster_endpoint_public_access_cidrs` containing `0.0.0.0/0`.

### 5. Redis "job-state store" is non-durable and a SPOF
**Confirmed by code reading.**
File: `kubernetes/platform/redis/manifests/redis.yaml`.

```sh
printf 'save ""\nappendonly no\nrequirepass %s\n' ...   # no RDB, no AOF
```
- Storage is `emptyDir`, persistence is **fully disabled** (`save ""`, `appendonly no`).
- Single replica, **no PDB**.

`AGENTS.md` and the pipeline contract advertise a **7-day TTL** job state and a `GET /jobs/{id}`
lookup. In reality, any Redis pod reschedule (node drain, Karpenter consolidation, OOM, image
update) **wipes all job state**, so previously-"queued"/"done" jobs return 404 and in-flight
dedup keys vanish. The durability claim is contradicted by the deployment. Calling it a "demo
state store" in a comment doesn’t reconcile with the contract language elsewhere.

**Fix:** for a credible story, back Redis with a PVC and enable AOF (`appendonly yes`), or use a
managed ElastiCache in prod; add a PDB. If it’s truly throwaway, downgrade the contract language
so the advertised guarantee matches reality.

### 6. audio-api: per-request Kafka delivery check uses a global flush counter
**Confirmed by code reading.**
File: `services/audio-api/main.py:268-299`.

A single module-global `confluent_kafka.Producer` is shared across all requests. Each request
registers a per-request `on_delivery` closure (correct) but then decides success on:
```python
remaining = await loop.run_in_executor(None, lambda: kafka_producer.flush(timeout=5))
if delivery_errors or remaining:
    ... mark job failed, return 503
```
`flush()` returns the count of **all** messages still in the producer queue — not just this
request’s. Under concurrent uploads, request A’s `flush()` can return `remaining > 0` purely
because request B’s message is still in flight, so A is told **"kafka delivery timeout"**, its job
is marked `failed` in Redis, and the client gets a 503 — even though A’s own event was delivered
fine (and will be consumed downstream). This produces phantom failures and inconsistent Redis
state under load.

**Fix:** gate success on the per-request delivery callback only (`poll()` until this message’s
callback fires, or use a `Future`/threaded producer per request), not on the global `flush()`
remaining count. Consider one producer with `flush()` only at shutdown and per-message
confirmation via the callback.

### 7. Image-signature admission couples pod scheduling to public sigstore
**Confirmed by code reading** (availability risk).
File: `kubernetes/platform/kyverno-policies/base/verify-image-signatures.yaml`.

```yaml
validationFailureAction: Enforce
failurePolicy: Fail
background: false
webhookTimeoutSeconds: 30
... rekor: { url: https://rekor.sigstore.dev }
```
Every admission of a `*/platform-core/*` image performs keyless cosign verification against the
**public Fulcio/Rekor**. With `failurePolicy: Fail` and `background: false`, if sigstore is slow or
unreachable, pod creation **blocks up to 30s and then fails**. That means a sigstore outage (or
just the prod nodes losing egress to it) can prevent *all* app pods from starting — including KEDA
scale-from-zero events and post-incident restarts. You’ve correctly chosen fail-closed for
security, but the operational blast radius (cluster availability tied to a third-party SaaS) isn’t
acknowledged or mitigated.

**Fix:** run a Rekor/Fulcio mirror or use the TUF cached roots + `ctlog`/`tlog` offline
verification options; or scope `failurePolicy: Fail` to a subset and document the dependency in a
runbook with a break-glass (temporarily switch to `Ignore`).

### 8. demo-app Helm chart: HPA vs. replicas fight; `:latest` default
**Confirmed by code reading + `helm lint`.**
Files: `helm/demo-app/templates/deployment.yaml:9`, `helm/demo-app/templates/hpa.yaml`,
`helm/demo-app/values.yaml`.

- `hpa.enabled: true` by default **and** the Deployment hardcodes
  `replicas: {{ .Values.replicaCount }}` (2). With both present, every `helm upgrade` / ArgoCD
  sync rewrites `replicas` back to 2, fighting the HPA and causing scale flapping. The Deployment
  must omit `replicas` when `hpa.enabled`.
- `values.yaml` sets `image.tag: latest` and `image.repository: ""` with a comment "required",
  but there’s no `{{ required ... }}` guard. A `helm install` without overriding repository
  renders `image: ":latest"` — both an invalid ref and a violation of your own no-`:latest`
  policy.
- Minor: pod/container hardening here omits `seccompProfile: RuntimeDefault` that the
  kustomize service manifests set — inconsistent.

**Fix:** wrap `replicas` in `{{- if not .Values.hpa.enabled }}`; use
`required "image.repository must be set"`; drop the `:latest` default or set it to the chart
`appVersion`.

---

## P2 — Hardening, consistency, cost, polish

### 9. llm-gateway rate limiter is per-pod
File: `services/llm-gateway/app/rate_limiter.py`, `services/llm-gateway/main.py:37`.
The sliding window is in-process. Prod runs 2 replicas, so the *effective* per-IP limit is
~2× `RATE_LIMIT_RPM` and is nondeterministic (depends on which pod the LB picks). It’s honestly
labelled "good enough for demo scale", but for a service whose entire reason to exist is rate
limiting in front of an expensive GPU, a shared store (Redis token bucket) is the expected design.

### 10. Several NetworkPolicies quietly defeat the zero-trust narrative
Files: `services/llm-gateway/k8s/base/networkpolicy.yaml` (egress `- {}`),
`services/vllm-inference/k8s/base/networkpolicy.yaml` (egress `- {}`),
`services/stt-worker/k8s/base/networkpolicy.yaml` (unscoped `:443` and `:53`).
- llm-gateway egress is `- {}` (allow all) though it only needs vLLM:8000 + DNS + OTLP.
- Workers allow `:443` to **any** destination and `:53` to **any** destination, whereas the
  tenant template correctly scopes DNS to `kube-system/k8s-app=kube-dns`. A compromised worker
  has unrestricted outbound HTTPS — the usual exfiltration path. The tenant template shows you
  know the right pattern; the service manifests don’t follow it.

### 11. Tenants can bypass the llm-gateway
File: `kubernetes/tenants/_template/network-policies.yaml:90-117`.
`allow-to-platform` opens egress to `apps` namespace port `8000` (vLLM) for every tenant. The
architecture exists to force traffic through llm-gateway (rate limiting, observability), but
tenants can hit vLLM directly on 8000, bypassing it. Either point tenants at the gateway port
(`8080`) or drop the `apps:8000` allowance.

### 12. audio-api `/readyz` blocks the event loop
File: `services/audio-api/main.py:122-144`.
The upload path correctly offloads Redis to `run_in_executor`, but `/readyz` calls the
synchronous `redis_client.ping()` directly inside the coroutine. Under a slow/hung Redis the
readiness probe stalls the event loop for all in-flight requests. Offload it like the rest.

### 13. Dead IAM roles
File: `terraform/modules/iam/main.tf:42-56,166-184`.
`irsa_cluster_autoscaler` is explicitly unused (comment says so) and `irsa_ecr_pull` binds
`apps:default` — but every app uses its own named ServiceAccount and nodes already have ECR pull
via the node role. Both create real IAM roles nobody assumes: needless attack surface and drift.
Remove them.

### 14. CI bootstraps its own toolchain without integrity checks
Files: `.github/workflows/ci.yaml`, `.github/workflows/docker-build.yaml`.
kustomize, kubeconform, conftest, and trivy binaries are installed via
`curl -sSL … | tar -xz` from GitHub releases with **no checksum/signature verification**. The
pipeline that enforces image signing and SBOMs is itself installing unverified executables that
then gate the build. Pin by sha256 (or use the official setup actions that verify).

### 15. Image-promotion job is one config change away from an infinite loop
File: `.github/workflows/docker-build.yaml:148-158` + trigger `paths: services/**`.
The promotion step commits to `services/<app>/k8s/overlays/prod/**` — which is under the
workflow’s own `paths` trigger. It does not loop today **only** because GitHub suppresses workflow
runs for pushes made with the default `GITHUB_TOKEN`. The moment someone switches the checkout to
a PAT/deploy key (a common change to satisfy branch protection), every promotion re-triggers the
build → new SHA → new digest → new promotion → infinite commit storm. Add `[skip ci]`/path
exclusion or an explicit guard so the safety doesn’t rest on an implicit platform behavior.

### 16. No S3 lifecycle policy, versioning on → unbounded growth
Files: `kubernetes/platform/crossplane/config/audio-bucket-claim.yaml:8-19`,
`terraform/modules/iam/main.tf:147-163`.
The Crossplane composition creates **versioned** buckets, the audio services never delete objects
(IAM grants only `GetObject`/`PutObject`, no `DeleteObject`), and the lifecycle policy is a
**TODO**. Raw audio + transcripts + summaries + speech + every version accumulate forever — a cost
and data-retention/compliance issue for a pipeline handling user audio. At minimum add the
documented expiry lifecycle before calling the prod path complete.

### 17. Job-state read-modify-write is non-atomic
Files: `services/*/app/job_state.py` (`set_job_state` / `mark_*` / `set_*`).
Each helper does `GET` → mutate JSON → `SET`. The `job_id` partition key makes intra-stage
concurrency unlikely, but during a rebalance/redelivery two pods can process the same job briefly
and clobber each other’s `keys` accumulation (lost update). Low probability, but for a "single
source of truth" job document it’s worth a `WATCH`/Lua-script CAS or per-field `HSET`.
*(Note: Redis errors themselves are correctly swallowed in all four workers — verified — so this
is purely the concurrency window, not an error-handling gap.)*

### 18. Smaller correctness/consistency nits
- **k8s version drift:** `terraform/modules/eks/variables.tf:14` defaults `1.29`, prod sets
  `1.31`, `AGENTS.md`/comments reference `1.29`. Pick one source of truth.
- **Dedup TTL < job TTL:** workers set `dedup:<stage>:<id>` for 86400s but job state lives
  604800s. A redelivery after 24h (slow DLQ replay, long lag) re-processes and re-emits — at
  least-once is fine, but the dedup guarantee is narrower than it looks.
- **Producer not idempotent:** no `enable.idempotence=true` / explicit `acks` on any
  `confluent_kafka.Producer`. librdkafka defaults to `acks=all` (durable) but non-idempotent, so
  internal retries can duplicate/reorder on the output hop. Downstream dedup covers it; setting
  idempotence makes the intent explicit.
- **Consumer-lag gauge staleness:** `worker-service/app/kafka_io.py:update_lag` labels by
  partition and never clears revoked partitions, so the `worker_consumer_lag` gauge keeps stale
  series after a rebalance.

---

## What’s genuinely strong (so the triage is honest)
- The Kafka consume→produce→commit ordering (`produce_confirmed` before manual commit, crash =
  redeliver) is correct at-least-once and well-reasoned; the inline comments explain *why*, not
  *what*.
- Trace-context propagation across HTTP **and** Kafka headers, with `created_at` carried forward
  for a true end-to-end metric, is a cut above typical demos.
- The OPA `--combine` relationship checks (NetworkPolicy actually selects each workload) and the
  Rollout-inclusive `workloads` set are thoughtful.
- Trivy-before-push gating and digest-pinned promotion is the right supply-chain shape (the gaps
  above are at the edges, not the core).

The recurring theme to fix: **several "production" claims are only true on paper** (KEDA TLS auth,
Redis durability, IaC scanning coverage, S3 lifecycle, public-endpoint posture). For a showcase,
either make them true or scope the claims to match what’s actually wired. That gap is exactly what
a skeptical reviewer will probe.
