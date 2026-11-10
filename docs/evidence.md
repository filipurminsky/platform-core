# Validation and evidence

This matrix separates source intent, automated checks, and live operation.
A configured workflow is not evidence that its latest run passed. Record the
commit and actual command/run URL with each result; never infer a successful
AWS deployment from Terraform validation or local rendering.

The [reconciled historical backlog](../issues-to-be-addressed.md) records which
June review findings are fixed in source, which retain work, and which still
need operational evidence. A source-level resolution is not a live acceptance result.

## Coverage matrix

| Capability | Reproduce / inspect | What it establishes | Boundary |
|---|---|---|---|
| Python service behavior | `make test`; [test workflow](../.github/workflows/test-apps.yaml) | Unit-level behavior in separate service environments | External dependencies commonly use fakes |
| Audio stage contracts | `make integration-test`; [tests](../tests/integration/) | Cross-service event/state/object-key compatibility | In-process infrastructure; not broker or container operation |
| Real dependency recovery | `make infrastructure-test`; [suite](../tests/infrastructure/) | Real Kafka, Redis, object-store behavior and controlled worker recovery | Isolated local test; does not establish Kubernetes or AWS behavior |
| Redis persistence recovery | [Recovery drill](runbooks/redis-recovery.md) | Forced replacement, independent RDB restore and restored-AOF replacement preserve fixture state/TTLs | Container volumes; no Kubernetes PVC, node-loss or HA proof |
| Workflow and IaC guards | `make workflow-lint`; `make iac-security`; [scope](../policy/terraform/README.md) | GitHub expression validation, negative fixtures, reviewed source posture and authored expanded-plan gate | Real AWS plan execution remains unverified |
| Service generation | `make scaffold-test`; [live portal QA](qa-dev-2026-11-09.md) | Generated artifacts tested; actual Backstage local template dry-run produced a tested service | GitHub publishing, registration, and promotion were not executed |
| Kubernetes shape and policies | `make schemas` then `make validate` | Rendering, pinned schema checks, combined OPA relationships, local checks in Makefile | Rendering is not admission, scheduling, or traffic verification |
| Echo startup on kind | [CI `e2e-smoke`](../.github/workflows/ci.yaml) | Actual overlay deploys and serves basic requests | Does not install the full platform or prove canary recovery |
| Release supply chain | [Release workflow](../.github/workflows/docker-build.yaml) | Intended test → scan → build → sign → verify → promote sequence | Needs successful run artifacts and real registry/admission verification |
| Canary recovery | `./scripts/canary-demo.sh bad`; [live report](qa-dev-2026-11-09.md) | Bad-canary rejection, good-canary promotion, serving checks and original-image restoration passed on fresh kind | Local prepared images; no AWS delivery proof |
| Full local bootstrap | [Fresh DEV report](qa-dev-2026-11-09.md); [local guide](local-dev.md) | Fresh 1.36.4/Cilium environment exercised through portal, audio, telemetry, and tenancy | Local unpublished Git snapshot; old cluster preserved, not repaired; known limits recorded |
| Tenant isolation and gateway access | [Tenant manifests](../kubernetes/tenants/); [access runner](../tests/network/README.md); [earlier live report](qa-dev-2026-11-09.md) | Eight additional access assertions passed on 2026-11-10, including authenticated gateway access and symmetric isolation | Local Cilium evidence; no AWS networking claim |
| AWS foundation and storage | Terraform fmt/init/validate; [prod config](../terraform/environments/prod/), [Crossplane](crossplane.md) | Static configuration and provider schema validation | No live AWS apply, bucket lifecycle, IAM, restore, or teardown verification |
| ML inference | Mock/stub service tests | Orchestration contracts with deterministic responses | No real STT/TTS accuracy or GPU cold-start/performance claim |
| SLOs and cost | [Rules/objectives](slo-definitions.md), [cost decision](adr/adr-011-infrastructure-cost-minimization.md) | Configured measurements and design tradeoffs | No measured 30-day availability, savings, or current AWS cost estimate |

`make validate` covers local non-cluster checks defined in the Makefile, including
`make scaffold-test` (locked npm install followed by generated-service tests).
`make infrastructure-test` is a separate Docker gate. Hosted
CI additionally runs security scans, container builds, and a kind smoke job.
The schema setup downloads pinned CRDs and converts their served schemas; a
missing schema is not silently treated as valid. Some kinds are explicitly
excluded in CI and remain outside its schema coverage.

## Current verification notes

The [tool-upgrade record](tool-upgrades.md) contains results from that specific
upgrade session. It is historical evidence, not a pass result for every later
working-tree change.

During the showcase improvement work, a read-only inspection of the existing
`kind-platform-core` cluster found multiple degraded Applications, including
Rollouts, monitoring, Kafka, and audio components. That cluster was not treated
as a valid baseline for a live canary proof. It was preserved while a fresh
`platform-core-qa` cluster was created from a local working-tree Git snapshot.
The checks below apply to that fresh cluster; they are not evidence that the
original cluster was repaired. The [dated DEV report](qa-dev-2026-11-09.md)
records both canary acceptance passes, source corrections, retests, and
remaining limits.

The implementation session also repaired the existing in-process integration
harness and found/fixed UUID normalization splitting API and worker job-state
keys. The repaired suite passed **4 tests**. A real isolated Kafka/Redis/SeaweedFS
run on Docker 29.4.3 / Python 3.14.10 passed **4 tests in 33.77 seconds** (test execution, excluding image build
and dependency startup). The runner retained logs and JUnit in a temporary
evidence directory and removed its isolated Compose resources. These are
working-tree results from 2026-11-09, not hosted CI or a committed release.
Reproduce them before presenting a later revision.

The operational-script regression suite passed **22 tests**. The shared schema
setup downloaded and converted **77 CRD schemas**. These verify the script and
schema setup paths; they do not establish live controller reconciliation or
admission behavior.

`make validate` passed during live QA; subsequent process-ownership and Tempo
corrections received scoped regression/schema and live retests. Checks during
the same working-tree session:

| Check | Result |
|---|---|
| `make validate` | Passed the full non-cluster aggregate; 208 Python tests passed, 1 skipped, plus 4 audio contract tests and the checks below |
| Generated service tests | 13 passed, including endpoint/metrics checks, frozen lock consistency, and dev/prod policy checks |
| Generated service container | Image built; UID 10001, read-only filesystem, dropped capabilities, no uv in runtime, `/healthz` HTTP 200 |
| Kubernetes schemas | Both environment aggregates, every service overlay, Kafka overlays, and tenant renders passed |
| Local Helm charts | All three charts rendered and passed schema validation |
| OPA relationship checks | 10/10 passed for each environment |
| Terraform | Dev and prod validation passed |
| Bootstrap preflight | `--check` passed without cluster changes; Docker reported 16 GiB available |

These results do not include live Backstage publishing, registry signing,
or promotion PR execution. The subsequent DEV report records actual platform
onboarding and local template generation. The expanded script suite includes
22 operational regressions, including stale analysis rejection, cache-refresh
failure handling, restoration-generation checks, and pipeline failure
propagation on macOS GNU Make 3.81.

## Live DEV acceptance (2026-11-09)

See the [dated QA report](qa-dev-2026-11-09.md) for environment details, observed
defects, fixes/retests, artifacts, cleanup, and final results. Verified
behavior includes actual Backstage generation, audio worker activation and cooldown,
real Kafka/Redis/object-store processing, full smoke checks, canary promotion/recovery,
correlated logs/traces, and tenant authorization/network/admission boundaries.

All nine platform dashboards loaded; 98 static PromQL expressions executed
without errors. Six kind control-plane/proxy endpoints remain unavailable;
Kafka Exporter and real GPU telemetry are absent by design in dev. These limits
are documented rather than reported as healthy. No 30-day SLO or AWS claim follows
from the local evidence.

## Recording a run

Use the following fields in a PR, CI artifact, or dated evidence note:

```text
Revision: <git SHA; include dirty diff if relevant>
Date/time and environment: <UTC; OS/architecture; Docker resources or cluster>
Command/workflow: <exact invocation and CI URL if available>
Dependencies: <image digests, tool versions, mock/stub boundaries>
Result: <exit code, passed/failed counts, duration>
Assertions: <specific behaviors checked>
Artifacts: <logs, JUnit, analysis/routing snapshot, screenshots if actually captured>
Limitations: <unrun paths and reasons>
Cleanup: <isolated resources removed; any retained artifacts>
```

Keep evidence free of API keys, kubeconfigs, raw Secrets, cloud credentials, and
user audio. Screenshots supplement assertions; they do not replace test results.
Measure onboarding with actual users separately: time to first deployment,
manual interventions, failed-task recovery, and feedback with sample size.
