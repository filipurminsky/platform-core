# Platform follow-up acceptance — 2026-11-10

This record covers the reliability, developer setup and security follow-ups to
the [November 9 DEV assessment](qa-dev-2026-11-09.md). The implementation commits
include `041d6b444e47e00ae566c596e61db295566a102b`,
`b51b50a9989195527a3d6362530a03c8c16ada82`,
`3707bdfe159f7fcfe41fd7eac1f1d50913b6617c`, and
`c7f98b8529239de72b9ebd6681f280fa4134fc1f`. Results below are scoped to the
commands and environments actually exercised; they do not establish AWS operation.
The [portable evidence summary](evidence/followups-2026-11-10.json) preserves
nonsecret outcomes beyond the hosted artifact retention window.

## Local and hosted validation

| Check | Result |
|---|---|
| Python suites | 208 passed, 1 skipped, across eight projects |
| Audio contract suite | 4 passed |
| Operational scripts | 43 passed after adding pod-persistence and cold-install deadline checks |
| Workflow contexts | All workflows linted; 2 positive/negative regression tests passed |
| Infrastructure security guards | 31 source/plan fixture tests passed, including rejection of unsafe configurations |
| Generated service/portal contracts | 16 passed, including route timeouts and retry policies for app/platform registrations |
| Manifests and charts | Strict schemas, both combined OPA aggregates, Helm checks and kube-linter passed |
| Terraform | Formatting and dev/prod validation passed; no AWS plan/apply was performed |
| Security scans | Clean-source Trivy HIGH/CRITICAL gate passed; local Checkov reported 103 passed, 0 failed, 2 skipped |
| App images | All seven exact dev image tags built and loaded into kind; hosted CI also built all seven amd64 images |

The first `make validate` invocation reached Kubernetes validation but failed
on a remote schema download timeout. Retrying the remaining targets succeeded.
Later portal/Redis-runner changes received scoped regression checks. Commit and
push hooks passed; the download failure is not reported as an uninterrupted
successful aggregate run.

[CI at 041d6b4](https://github.com/filipurminsky/platform-core/actions/runs/42819076566)
[CI at b51b50a](https://github.com/filipurminsky/platform-core/actions/runs/42822089970),
and [CI at 3707bdf](https://github.com/filipurminsky/platform-core/actions/runs/42825103170)
passed, including the real kind echo smoke job. The separate generated-service
contract also passed at [b51b50a](https://github.com/filipurminsky/platform-core/actions/runs/42822090123).
These runs are not evidence of release signing or production promotion.

The retry and connection-regression revision `c7f98b8` also passed
[main CI](https://github.com/filipurminsky/platform-core/actions/runs/42830429342)
and the [15-test generated-service contract](https://github.com/filipurminsky/platform-core/actions/runs/42830429286).

## Real dependency and Redis backup recovery

The corrected [hosted infrastructure workflow](https://github.com/filipurminsky/platform-core/actions/runs/42819126097)
ran commit `041d6b4` successfully and uploaded
`infrastructure-recovery-42819126097`. The downloaded artifact's commit record,
JUnit and Redis reports were inspected. Its retention is seven days; the
portable summary retains the nonsecret measured outcomes.

JUnit recorded **4 passed, 0 failed, 0 skipped in 38.581 seconds**: worker
interruption after object write, interruption before input commit, SIGTERM
during retry, and invalid-event DLQ delivery. Redis assertions then verified:

| Recovery operation | Local Docker | Hosted `041d6b4` | Hosted `c7f98b8` |
|---|---:|---:|---:|
| Forced container replacement, same volume | 4.550 s | 4.944 s | 4.633 s |
| RDB restore onto an independent volume | 5.861 s | 5.931 s | 5.754 s |
| Forced replacement using the restored AOF | 4.689 s | 4.941 s | 4.575 s |

The [repeat hosted run at c7f98b8](https://github.com/filipurminsky/platform-core/actions/runs/42830620428)
passed all four worker tests in **35.589 seconds**, repeated the Redis assertions,
and passed the connection-close regression described below. Its downloaded
artifact `infrastructure-recovery-42830620428` records the exact commit in both
test runners; JUnit reports zero failures, errors or skips.

The checks compared exact job/dedup records and remaining TTLs, excluded a write
made after the backup, and retained a write made after restoration. The test
found a real procedure error: an RDB-only restore must initially load with AOF
disabled, then enable AOF and verify its rewrite and subsequent startup. See
the [recovery runbook](runbooks/redis-recovery.md).

These measurements include orchestration and assertions. They cover small,
synthetic, fsync-confirmed data and are not production RPO/RTO commitments.
Redis remains a single instance with service interruption during replacement.

The opt-in Kubernetes drill also passed on the retained QA cluster. A graceful
deletion produced a different Ready `redis-0` pod UID after **11.810 seconds**;
exact synthetic job/dedup values and their elapsed TTLs were verified after
**12.026 seconds**. StatefulSet, PVC and PV UIDs were unchanged, and both owned
test keys were removed. The runner recorded source `3707bdf` with unrelated
working-tree edits and the script's SHA256. This proves same-volume pod
replacement on local kind, not node loss, lost-volume restoration or continuous
availability. Readiness timing is not a continuous client outage measurement.

## Published GitOps and portal behavior

The retained `kind-platform-core-qa` cluster was migrated from the local Git
snapshot to public GitHub sources. Bootstrap convergence completed in 212.9
seconds, including the normal echo image promotion. All 37 Applications and
22 ApplicationSets were checked for temporary-server dependencies. First-party
Git sources observed `041d6b4`; the authenticated catalog contained 28 entities
and three templates with no catalog errors.

The two task-owned servers on ports 19418/19419 were stopped after source
verification. With those servers stopped, Application/Gateway/HTTP acceptance
and catalog checks passed. No local Git/catalog server is required by the
published configuration. Child sources still track `main`; this is not an
immutable fleet pin.

Public template generation exposed another actual defect: Envoy returned a
`504`, flag `UT`, `response_timeout`, after 15.007 seconds while the backend
continued fetching GitHub templates. An equivalent request directly to
Backstage generated all 25 files in 18.912 seconds. After `b51b50a` reconciled,
the Backstage route's bounded 120-second budget was accepted and the same
fetch-only dry run succeeded **through the Gateway in 36.625 seconds**.

This exercised real `fetch:template` actions against public GitHub with no
uploaded replacement directory. Publish/registration actions were omitted;
no external repository or promotion PR was created. It does not prove complete
service onboarding or internet-independent operation.

## Tenant access and echo connection investigation

Eight local Cilium assertions passed: same-tenant traffic, symmetric cross-tenant
denial, authenticated gateway HTTP 200, missing/wrong-key HTTP 401, an allowed
gateway-to-inference control, and denied direct inference from a tenant even
with a spoofed gateway pod label. Probe cleanup succeeded. The repeatable
[runner](../tests/network/README.md) makes a blocked authenticated request fail
rather than interpreting every timeout as successful isolation.

Echo comparison results through the QA Gateway:

| Profile | Baseline requests/errors | Candidate requests/errors |
|---|---:|---:|
| Configured seven-minute sustained profile | 44,682 / 0 | 45,319 / 0 |
| Configured five-minute idle-boundary profile | 6,000 / 0 | 6,000 / 0 |

All four commands exited successfully. Envoy accepted the route policy and its
effective upstream configuration contained `idle_timeout: 2s`, below Uvicorn's
explicit five-second keepalive close. The change adds no HTTP retry policy.
Corresponding queried Envoy/echo logs contained no HTTP errors.

The historical 503s were **not reproduced by these load profiles**, so their root cause and elimination
remain unproven. This is timeout hardening and finite regression evidence.
Candidate wall-clock summaries exceeded the configured profiles, and its
sustained run recorded a 25.27-second maximum request duration; the sustained
p95 values were 184.69/186.34 ms. Do not present these runs as a controlled
performance improvement, production capacity measurement, or zero-error guarantee.
See the [diagnostic procedure](runbooks/echo-upstream-connections.md).

A separate controlled experiment delayed notice of the backend's connection
close by 0.3 seconds and attempted reuse after 5.1 seconds. With the old pool
timeout, all three measured requests returned **503/UC**, with Envoy detail
`upstream_reset_before_response_started{connection_termination}`. With the
repository's two-second timeout, all three returned **200** with the echo
payload. The fixture correlates every request with its access log, checks the
image command against the Dockerfile and reads the candidate timeout from the
actual BackendTrafficPolicy. Run `make connection-test` to repeat it.
Both the local run and hosted run `42830620428` passed all six requests and their
exact access-log assertions.

This supports the mitigation under an injected fault. It does not establish
that delayed close notification caused the historical errors. The Compose
fixture uses one Envoy worker and HTTP/1.1; live Gateway policy translation was
verified separately above.

## Fresh bootstrap: preserved failed attempt

A separate clean checkout of published `b51b50a` passed source preflight. To fit
within Docker's 16 GiB allocation, the rehearsal stopped only the three retained
QA node containers, preserving their data, then created a new three-node kind
cluster with a separate kubeconfig and host ports.

The first full bootstrap failed at the five-minute Cilium installation deadline:
one agent was available while the others were still initializing. A successful
240,966,361-byte image pull took 295.572 seconds, nearly the entire old budget;
there were no failed-image events in the captured diagnostics. This is a failed
fresh installation, despite passing static checks and a successful repeat
bootstrap on the retained environment.

The rehearsal removed its new cluster, verified repeated teardown, restarted
the exact retained QA containers, and passed full QA acceptance after 153.324
seconds. The selected context was preserved. Bootstrap now has a separately
configurable `--install-timeout` for Cilium/Argo Helm readiness, defaulting to
600 seconds, with namespace/context-specific diagnostics. The application
convergence `--timeout` remains separate.

## Fresh bootstrap: dependency retry failure

The second clean-checkout attempt used published `3707bdf`. The longer Helm
budget allowed Cilium/Hubble installation to complete, all seven app images
were built and loaded, and Argo CD began reconciling public sources.

It exposed a separate unattended-startup defect. KEDA's `ScaledObject` CRD
became `Established=True` at 13:47:24 UTC, just as `llm-worker`, `stt-worker`
and `vllm-inference` exhausted their five synchronization attempts. They
remained `OutOfSync/Missing`; at 13:50:36 the controller explicitly logged that
it would not retry the failed revision. This was not an image or CNI failure:
the preloaded audio API and gateway pods were ready. Echo was separately
waiting for the Rollouts controller's cold image initialization.

No manual resync was used to turn this into a clean-bootstrap pass. The
bootstrap process was stopped after confirming the blocker, its cluster was
deleted, repeated teardown passed, and the original QA containers were restored.
Full retained-QA acceptance passed after 76.406 seconds with the selected
context preserved. The failed attempt's diagnostics are retained separately.

The repair gives all eight app ApplicationSets and generated service
registrations automatic retries with a 10-second initial backoff, factor two,
and 60-second cap. Retries can pick up a corrected source revision. The
bootstrap observer retains its finite timeout and fails if convergence does
not complete. Another fresh published-source acceptance run is required.

## Fresh bootstrap: platform dependency retries

The third clean checkout used `c7f98b8`. Its eight app registrations continued
through at least **ten retries** while external CRDs were absent, exceeding the
previous five-attempt limit. The observer recorded their operations as
`Running`; this establishes that extended retries were exercised, not that
those apps had already completed deployment.

The same failure mechanism then affected platform registrations. Alloy,
Rollouts, KEDA, Loki, the OpenTelemetry collector, Tempo, OpenCost and
`loki-config` exhausted default retries while monitoring CRDs were still
unavailable. The controller explicitly logged that it would not retry their
failed revision. The monitoring admission hook was still waiting for its cold
image pull. No manual resync was performed, and this attempt is also a failure.

The repair now covers all 16 identified platform registrations that consume
CRDs supplied by another Application, including monitoring, Gateway, Strimzi,
Kyverno and external-secret resources. It preserves the existing explicit
ten-retry budgets for monitoring, monitoring rules, Crossplane configuration
and Karpenter configuration. Strict schemas passed for all 16 registrations;
the expanded scaffolding suite passed 16 tests. Fresh acceptance remains
required for this broader repair.

## Remaining boundaries

No AWS deployment, EBS provisioning, IAM operation, live ECR signature
admission, dependency-outage admission test, real GPU inference, or production
backup/HA guarantee was verified. The signature runbook now matches the CEL
policy and retains fail-closed behavior; external verification dependencies
remain. Expanded-plan security is implemented and fixture-tested, but a real
AWS plan still requires configured credentials and infrastructure.
