# Historical review — reconciled backlog

**Reconciled:** 2026-11-09 · **Audited source:** `9e6c659dd35337e9a92691ffd430140134d940db`

**Follow-up:** 2026-11-10 — implementation and local recovery/access evidence updated below.

This is the current disposition of the 18 findings from the 2026-06-15 review
of `harden-eks-netpol-cleanup`. The
[original review is preserved at the audited commit](https://github.com/filipurminsky/platform-core/blob/9e6c659dd35337e9a92691ffd430140134d940db/issues-to-be-addressed.md).
Historical excerpts and severities describe that earlier review, not defects
all still present today.

**Result: 14 findings resolved in implementation; four partially addressed.**
A separate tenant-to-gateway access mismatch found during reconciliation is
tracked as **F1** below.

- **Resolved:** the reported implementation defect is absent in the audited
  source. This does not imply a live AWS deployment or exhaustive failure testing.
- **Partial:** a correction exists, but part of the concern or its operational
  guidance still needs work.
- **Accepted tradeoff:** an existing documented decision, not new risk acceptance
  by this review. Mitigation or verification may still be outstanding.

The initial reconciliation used source inspection and read-only rendering. It did not
apply infrastructure, change policies, rerun service suites, or execute cloud or
signing operations. Previously recorded tests and local acceptance are linked
where relevant; see the [evidence matrix](docs/evidence.md) and
[dev QA report](docs/qa-dev-2026-11-09.md) for their boundaries. November QA
findings remain in that report rather than being folded into this historical
closure count.

## Disposition summary

Severities below preserve the original assessment, not a new severity for the
remaining work.

| ID | Historical severity | Original concern | Status | Current disposition |
|---|---|---|---|---|
| 1 | P0 | KEDA Kafka TLS lacks cluster CA | Resolved | All four prod authentication resources reference the CA; live TLS/rotation proof remains |
| 2 | P0 | Production EKS API public by default | Resolved | Private endpoint default; public access requires explicit CIDRs |
| 3 | P1 | Worker nodes in public subnets | Resolved | Private subnet wiring, NAT, and private Karpenter discovery tags |
| 4 | P1 | Registry modules invisible to IaC scanners | Resolved | Blocking source guards, negative fixtures and expanded-plan gate added; AWS execution remains unverified |
| 5 | P1 | Redis loses state on reschedule; single point of failure | Partial | Container/Pod replacement and backup/AOF restore verified; node/volume-loss recovery and availability remain |
| 6 | P1 | Audio delivery depends on global flush count | Resolved | Request-specific delivery acknowledgement controls acceptance |
| 7 | P1 | Signature admission depends on public services | Partial | CEL runbook corrected; external dependency retained; live outage/admission proof remains |
| 8 | P1 | Helm HPA/replica conflict and invalid image defaults | Resolved | Conditional replicas, required repository, version default, seccomp |
| 9 | P2 | Rate limit multiplies across gateway replicas | Resolved | Production uses shared atomic Redis limiter; documented fallback tradeoffs remain |
| 10 | P2 | Broad application egress | Partial | Gateway allow-all egress and unrestricted DNS fixed; unrestricted HTTPS destinations remain |
| 11 | P2 | Tenant direct vLLM bypass | Resolved | Tenant port 8000 removed and vLLM ingress narrowed; separate gateway-access issue F1 |
| 12 | P2 | Readiness ping blocks audio event loop | Resolved | Redis ping offloaded to executor |
| 13 | P2 | Unused IAM roles | Resolved | Unused autoscaler/ECR-pull roles removed from configuration |
| 14 | P2 | Unverified CI tool downloads | Resolved | Reported binaries use pinned SHA256 checks before installation |
| 15 | P2 | Promotion can recursively trigger builds | Resolved | Manifest-only change guard plus `[skip ci]` promotion commit |
| 16 | P2 | Versioned S3 objects grow without lifecycle | Partial | Audio/speech and old-version expiry added; current derived objects remain indefinite |
| 17 | P2 | Redis job-state merge is non-atomic | Resolved | Shared atomic Lua merge preserves accumulated keys and TTL |
| 18 | P2 | Versions, dedup TTL, producer retries, stale lag labels | Resolved | All four sub-findings corrected; targeted proof gaps remain |

## Findings with follow-up work

### 4. Selected infrastructure controls now have blocking gates

The [CI workflow](.github/workflows/ci.yaml#L330) has a blocking Checkov pass
over local Terraform and a separate pass with external-module download enabled.
The [expanded pass](.github/workflows/ci.yaml#L354) uses `--soft-fail`.
The original claim that scanners never see registry modules is obsolete;
expanded findings still do not fail CI. Trivy gates findings subject to its
configured exclusions.

**2026-11-10:** `make iac-security` parses source wiring and runs positive and
negative fixtures for endpoint exposure, node placement, public IP assignment,
and IMDS settings. The [plan workflow](.github/workflows/terraform-plan.yaml)
blocks unsafe evaluated resources, including nested registry modules, when AWS
is configured. Public-IP/IMDS exemptions were removed and the node-egress
exception narrowed. See the [guard definitions and limits](policy/terraform/README.md).
The local source/fixture checks passed; a real AWS plan remains unverified.

### 5. Redis persistence recovery is verified; availability remains

[Redis](kubernetes/platform/redis/manifests/redis.yaml#L59) enables AOF with
`appendfsync everysec` and periodic snapshots. Its
[volume claim](kubernetes/platform/redis/manifests/redis.yaml#L95) is 1 GiB RWO.
The old `emptyDir`, disabled-persistence, and missing-PDB assertions are obsolete.
It still runs one replica; its
[PDB](kubernetes/platform/redis/manifests/redis.yaml#L123) permits one unavailable
pod, including complete voluntary downtime.

**2026-11-10:** the isolated [recovery drill](docs/runbooks/redis-recovery.md)
verified forced container replacement on the same volume, RDB restore onto a
separate volume, and another forced replacement using the restored AOF. Exact
job/dedup values and remaining TTLs survived; the snapshot recovery point was
checked. A subsequent [local Kubernetes drill](docs/qa-followups-2026-11-10.md)
replaced `redis-0` with a different Ready pod UID, preserved the StatefulSet/PVC/PV
identities, and verified exact job/dedup values and elapsed TTLs in 12.026 seconds.
Only the two owned synthetic keys were removed. This is graceful pod replacement
on local storage, not node/volume loss or high availability.

**Remaining work:** establish recovery objectives, then exercise node/volume
loss, disk exhaustion, and offsite backups. Seven-day key TTL is
a retention setting, not an availability or recovery guarantee. Keep the
single-instance design's limit explicit; replication or a managed service is a
separate requirement if high availability is needed.

**AWS prerequisite:** Redis relies on a default StorageClass and Kafka requests
`gp3`. The authored [EKS add-ons](terraform/modules/eks/main.tf#L64) do not
include EBS CSI, and this review found no repository StorageClass definition.
Confirm the provisioner, class, and permissions before claiming production PVC
readiness. This is a provisioning gap to resolve or document, not an observed
failure on a deployed AWS cluster.

### 7. Fail-closed signature admission is intentional; outage proof remains

The current [ImageValidatingPolicy](kubernetes/platform/kyverno-policies/base/verify-image-signatures.yaml#L34)
uses `Deny`, `failurePolicy: Fail`, a 30-second timeout, exact signing identity,
and a public Rekor endpoint. It matches platform ECR images; generated GHCR
services are outside that selector. The external dependency is accepted in
[ADR-009](docs/adr/adr-009-sign-verify-supply-chain.md). Preserve fail-closed
admission; this reconciliation does not authorize weakening it.

**2026-11-10:** the [signature-admission runbook](docs/runbooks/image-signature-admission.md)
now uses CEL fields, removes unsupported cache-TTL claims, distinguishes explicit
denials from webhook failures, and refreshes the owning Argo Application.
It preserves fail-closed admission and does not claim an offline recovery path.

Test signed/unsigned images, signer mismatch, dependency unavailability, and
cold/warm paths before claiming cache or outage recovery. Private/offline
verification infrastructure remains a possible architecture decision, not an
implemented control or an automatic requirement for this showcase.

### 10. DNS and gateway egress narrowed; broad HTTPS remains

The [gateway policy](services/llm-gateway/k8s/base/networkpolicy.yaml#L28)
restricts egress to namespace/port combinations and CoreDNS.
[vLLM](services/vllm-inference/k8s/base/networkpolicy.yaml#L29) also scopes DNS,
but retains destination-unrestricted HTTPS. That permission also remains in
[STT](services/stt-worker/k8s/base/networkpolicy.yaml#L37),
[TTS](services/tts-worker/k8s/base/networkpolicy.yaml#L52),
[LLM worker](services/llm-worker/k8s/base/networkpolicy.yaml#L62), and
[audio API](services/audio-api/k8s/base/networkpolicy.yaml#L61), including their
rendered dev overlays.

**Remaining work:** identify required S3/model-download destinations and implement
or explicitly accept the appropriate egress boundary per environment. Test both
required access and denied arbitrary destinations. Current port restrictions
do not establish destination-restricted HTTPS or complete exfiltration
prevention. Local Cilium evidence does not establish AWS enforcement.

### 16. Lifecycle rules exist; current derived objects remain indefinite

The [Crossplane composition](kubernetes/platform/crossplane/config/composition.yaml#L147)
creates lifecycle rules for audio/speech expiry, noncurrent-version expiry
across prefixes, and abandoned multipart uploads. The
[bucket settings](kubernetes/platform/crossplane/config/audio-bucket-claim.yaml#L31)
select 30-day audio and seven-day noncurrent-version retention;
[IAM](terraform/modules/iam/main.tf#L109) includes lifecycle permissions.
The original claim that lifecycle is only a TODO is obsolete.

**Remaining work:** decide retention/deletion requirements for current
`transcripts/` and `summaries/` objects, which have no expiration rule and can
still grow indefinitely. Verify live bucket provisioning, lifecycle behavior,
version/multipart cleanup, and teardown. The
[provider IRSA annotation](kubernetes/platform/crossplane/config/provider.yaml#L17)
still needs environment wiring. Rendering does not prove cloud data deletion.

## Resolved findings and verification boundaries

### 1. Kafka scaler CA configuration

Prod authentication resources for
[worker-service](services/worker-service/k8s/overlays/prod/trigger-auth.yaml#L9),
[STT](services/stt-worker/k8s/overlays/prod/trigger-auth.yaml#L9),
[LLM](services/llm-worker/k8s/overlays/prod/trigger-auth.yaml#L9), and
[TTS](services/tts-worker/k8s/overlays/prod/trigger-auth.yaml#L9) supply the CA and
password, with username from the workload environment.
[Kafka CA annotations](kubernetes/platform/kafka/base/kafka.yaml#L55) arrange
reflection into `apps`. All four prod overlays render with port 9093, TLS,
SCRAM-SHA-512, and their authentication reference.

**Proof still needed:** live TLS scaler readiness, activation from zero, and CA
rotation. Successful local plaintext Kafka tests do not prove these paths.

### 2. Production API endpoint defaults

[Production](terraform/environments/prod/main.tf#L105) defaults public access
off. The [EKS module](terraform/modules/eks/main.tf#L37) keeps private access on,
and [input validation](terraform/modules/eks/variables.tf#L43) requires explicit
CIDRs for public access and rejects literal `0.0.0.0/0` in prod. Actual
plan/applied endpoint and private operator/CI access remain unverified in AWS.

### 3. Private node placement

[Production wiring](terraform/environments/prod/main.tf#L53) and the
[EKS module](terraform/modules/eks/main.tf#L34) use private subnets for
control-plane ENIs and node groups. [Networking](terraform/modules/networking/main.tf#L36)
enables NAT, disables automatic public IPs, and tags only private subnets for
[Karpenter discovery](kubernetes/platform/karpenter/config/ec2nodeclass.yaml#L21).
Applied routes, node IPs, and Karpenter provisioning remain unverified; its
environment-specific role placeholder must also be wired.

### 6. Per-request Kafka delivery acknowledgement

[Audio API](services/audio-api/main.py#L166) now waits for its own delivery
callback/event, polls the shared producer, and offloads this work to an executor.
Global `flush()` remains at shutdown, not as the request acceptance condition.
[Delivery rejection coverage](services/audio-api/test_main.py#L276) and recorded
live audio/smoke runs support the changed path. A concurrent-upload regression
with another message still pending would prove the exact historical contention
case more directly.

### 8. Helm replica ownership and defaults

The [Deployment template](helm/demo-app/templates/deployment.yaml#L8) omits
replicas when HPA is enabled, requires an image repository, defaults its tag to
the chart version, and sets `RuntimeDefault` seccomp. This reconciliation's
render checks confirmed missing-repository rejection, no Deployment replicas
with HPA enabled, two replicas/no HPA with it disabled, and tag `1.0.0`.
This does not establish live HPA scaling behavior.

### 9. Shared production rate limiting

The [Redis limiter](services/llm-gateway/app/rate_limiter.py#L87) uses an atomic
sliding window and is selected when Redis is configured. The
[production overlay](services/llm-gateway/k8s/overlays/prod/patch-redis.yaml#L23)
wires Redis for two gateway replicas. Existing
[tests](services/llm-gateway/test_main.py#L78) cover script results, Lua behavior,
and Redis-unavailable fallback.

**Accepted tradeoffs:** dev's in-memory limiter is per-pod; the Redis limiter
intentionally fails open on Redis errors. It does not enforce a global cap
during that outage. A real multi-replica/shared-key load test and outage exercise
remain verification work.

### 11. Direct tenant-to-vLLM bypass

[Tenant egress](kubernetes/tenants/_template/network-policies.yaml#L116) permits
gateway ports 80/8080 instead of vLLM port 8000.
[vLLM ingress](services/vllm-inference/k8s/base/networkpolicy.yaml#L15) additionally
requires both `apps` namespace and gateway pod label. The reported bypass is
removed in configuration. Positive tenant-to-gateway access is a separate issue,
tracked as F1 below.

### 12. Nonblocking readiness probe

[Audio readiness](services/audio-api/main.py#L139) offloads Redis `ping()` with
`run_in_executor`; a [basic test](services/audio-api/test_main.py#L260) exists.
A slow-Redis concurrency regression would strengthen evidence. The client does
not set explicit socket timeouts, so executor offload is not itself a bounded
readiness-time guarantee.

### 13. Unused IAM declarations removed

The `irsa_cluster_autoscaler` and `irsa_ecr_pull` declarations are gone.
[IAM source](terraform/modules/iam/main.tf#L180) explains their omission;
[outputs](terraform/modules/iam/outputs.tf) expose retained roles. If an older
AWS installation exists, inspect its plan and IAM inventory: source removal
does not prove deployed roles were destroyed.

### 14. Reported CI binary downloads verified

Pinned SHA256 checks precede installation for
[Kustomize](.github/workflows/ci.yaml#L148),
[conftest](.github/workflows/ci.yaml#L206),
[kubeconform](.github/actions/setup-kubeconform/action.yaml#L49), and
[Trivy](.github/actions/setup-trivy/action.yaml#L31). The
[release workflow](.github/workflows/docker-build.yaml#L280) also checks its
Kustomize download; Helm and kube-linter have corresponding checks. This closes
the reported binary-integrity defects, not every possible supply-chain risk.

### 15. Promotion recursion guarded explicitly

The [change detector](.github/workflows/docker-build.yaml#L94) excludes service
`k8s/` changes from the build matrix; its output gates tests/build/promotion.
The [promotion commit](.github/workflows/docker-build.yaml#L331) also includes
`[skip ci]`. Normal promotion-only pushes no longer rely solely on the default
token's workflow suppression. Actual signing/promotion still needs configured
infrastructure and successful execution evidence.

### 17. Atomic job-state merge

The [shared helper](services/platform-common/src/platform_common/job_state.py#L22)
merges accumulated keys and writes TTL in one Redis Lua call. Shared stages use
it; [unit coverage](services/platform-common/tests/test_state_and_dedup.py#L5) and
[real dependency tests](tests/infrastructure/test_recovery.py#L175) check retained
keys/state. A concurrent-client merge test would strengthen the contention proof.
Atomic merge does not enforce monotonic status transitions, and Redis errors
remain best-effort; these are separate limits from the lost-key race.

### 18. Four smaller findings corrected

| Sub-finding | Current implementation | Remaining boundary |
|---|---|---|
| Version drift | [EKS input](terraform/modules/eks/variables.tf#L17) has no stale default; [dev](terraform/environments/dev/main.tf#L58) and [prod](terraform/environments/prod/main.tf#L58) select 1.36; CI schemas target 1.36.0 and kind uses 1.36.4 | Patch differences are intentional; pins still need coordination and EKS upgrades were not exercised |
| Dedup TTL shorter than job TTL | [Shared stage](services/platform-common/src/platform_common/stage.py#L136) uses the same 604800-second setting for completed dedup and state; [tests](services/platform-common/tests/test_state_and_dedup.py#L31) assert it | Processing leases are shorter by design; TTL inspection is not a seven-day soak; ordinary worker-service dedup stays in-memory |
| Non-idempotent producer retries | [Audio producer](services/audio-api/main.py#L95) and [shared worker producer](services/platform-common/src/platform_common/kafka_io.py#L90) enable idempotence | This is not transactional exactly-once processing and does not eliminate duplicates after restart |
| Stale revoked-partition lag series | [Shared lag refresh](services/platform-common/src/platform_common/kafka_io.py#L131) clears the gauge before rebuilding current assignments | No direct rebalance regression found; stale labels can persist until the next approximately 15-second refresh |

## F1. Tenant gateway access — resolved in source and local acceptance

This is separate from resolved historical finding 11. Gateway ingress now allows
platform-enrolled tenant namespaces on port 8080, while tenant egress selects
only gateway pods in `apps`. Application authentication remains required.

The [repeatable acceptance runner](tests/network/README.md) passed eight local
Cilium checks on 2026-11-10: authenticated HTTP 200, missing/wrong-key HTTP 401,
same-tenant connectivity, symmetric cross-tenant denial, a live inference
positive control and denial of direct inference even with a spoofed gateway pod
label. Probe cleanup passed. This does not establish EKS network enforcement.

## Evidence collected for the initial 2026-11-09 reconciliation

- Inspected source and existing test definitions at the audited commit;
  earlier runtime results remain in the linked QA report.
- Rendered all four prod Kafka-worker overlays and inspected TLS/auth references.
- Rendered demo-app with/without HPA and verified missing-repository rejection,
  replica ownership, image tag, and seccomp.
- Rendered dev/prod overlays for gateway, vLLM, STT, TTS, LLM worker, and audio API;
  inspected both aggregate NetworkPolicy selector sets for F1.
- No new AWS, signed-image admission, broker-fault, Redis-restore, or live tenant
  connectivity verification is implied by these read-only checks.
