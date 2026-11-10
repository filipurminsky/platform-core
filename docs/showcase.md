# Showcase walkthrough

The [DEV acceptance report](qa-dev-2026-11-09.md) and
[November 10 follow-up](qa-followups-2026-11-10.md) record actual live results,
fixes, hosted recovery evidence, and remaining limits.

The central story is a developer creating a service, seeing it delivered with
platform defaults, and diagnosing a failed change. The asynchronous pipeline
provides a second example of the same platform's recovery and observability
contracts. Allow setup time separately from a short presentation.

Read the [evidence matrix](evidence.md) first. The commands below are a
reproduction procedure, not a record that every live step has passed.

## 1. Establish the tested revision and local prerequisites

```bash
git rev-parse HEAD
git status --short
./scripts/bootstrap.sh --mode=local --check
make doctor
make schemas
make validate
```

Record the revision, any local diff, machine resources, and command output.
Use the configured tool versions from [CI](../.github/workflows/ci.yaml).
`make schemas` downloads validation inputs; it does not install a cluster.

For a presentation, retain a run log and prepare a healthy disposable kind
cluster ahead of time. Use a clean checkout of published GitHub `main` and run
`./scripts/bootstrap.sh --mode=local --check --require-published-source` to
verify that local image inputs match the starting published revision. Ordinary
bootstrap supports development with local edits; those edits need to be
published before Argo CD or the portal catalog can consume them. Child
Applications/catalog templates track `main`; record observed revisions because
the branch may advance during setup. No temporary Git or catalog server is
part of this walkthrough. See [the clean-checkout procedure](local-dev.md#bootstrap-and-observe).

## 2. Show the developer service contract

Open the [microservice template](../backstage/templates/microservice/template.yaml)
and its generated README. Demonstrate the supported inputs, application code,
health/metrics endpoints, service-owned deployment resources, release workflow,
and platform registration review.

Publishing needs a configured Backstage GitHub integration with the necessary
scaffolder actions and repository permissions. The generated service and its
registration PR can be reviewed locally through
[the scaffolding tests](../tests/scaffolding/) before any external write.
Render and check the generated artifacts without publishing:

```bash
make scaffold-test
node tests/scaffolding/render.mjs /tmp/generated-service
```

Use a fresh output directory for each rendered example. The template opens a
platform registration PR with a per-service AppProject and an environment-selected
ApplicationSet. The generated delivery workflow tests and scans images, pushes
multi-architecture images to GHCR, signs them, verifies the signer, and opens a
digest promotion PR. Dev promotion follows source pushes; prod is a manual
workflow path gated by the GitHub `production` Environment.

Configure repository Actions permissions to create PRs, protect the relevant
branches/environment, and make the GHCR package public or provide registry pull
credentials. The initial overlay has zero replicas and a placeholder digest;
merge a successful digest promotion before expecting a running service. The
platform's existing ECR admission identity policy does not cover these generated
GHCR packages: signature verification in the promotion workflow is the current
boundary, not a claim of end-to-end admission enforcement for generated repos.

Catalog registration alone is not a deployed service. Finish the image release,
review the GitOps registration, and check the resulting Argo Application before
claiming a complete onboarding journey.

For an actual onboarding measurement, ask another developer to follow the
instructions unaided. Record elapsed time, manual assistance, and points where
the status or failure message was unclear. Publish the sample size; do not
extrapolate a solo demo to organization-wide productivity.

## 3. Exercise real worker recovery without a Kubernetes cluster

```bash
make integration-test
make infrastructure-test
make connection-test
```

The first command checks contracts in process. The second uses isolated real
Kafka, Redis, and object storage to exercise the shared worker delivery and
recovery behavior. Read [the case study](case-study-worker-recovery.md) alongside
the suite: explain where a crash leaves the input uncommitted, when a dedup
lease expires, and why duplicate execution remains possible.

The third command demonstrates the HTTP connection timeout fix with an injected
delayed-close fault: three baseline 503/UC responses versus three candidate
200 responses. Explain that this is a reproducible failure mode, while the
cause of the historical intermittent errors remains unproven. See the
[connection runbook](runbooks/echo-upstream-connections.md) for boundaries.

The suite interrupts the actual STT stub worker path after an S3 write and
after confirmed output/dedup but before input commit; it also exercises SIGTERM
during a retry and invalid-event DLQ delivery. It asserts recovery using Kafka
offsets, object contents, Redis state, and output events. It uses no host ports
and cleans up its own Compose project.

The runner prints the evidence directory containing worker/infrastructure logs,
JUnit, and run metadata. Set `INFRA_EVIDENCE_DIR` to retain those artifacts in a
chosen location. Capture the actual run result, assertions, and timings. Keep a failed run as a
failed result with diagnostics; rerunning successfully does not erase the
failure or explain its cause.

## 4. Prepare and inspect the platform

```bash
./scripts/bootstrap.sh --mode=local --require-published-source --wait
kubectl --context kind-platform-core get applications -n argocd
kubectl --context kind-platform-core get pods -A
kubectl --context kind-platform-core get gateway platform -n envoy-gateway-system
kubectl --context kind-platform-core argo rollouts -n apps get rollout echo-service
curl --fail -H 'Host: echo.platform-core.local' http://localhost/
```

Bootstrap builds and loads the seven exact dev image tags before registering
GitOps applications. `--wait` checks Application health/sync, Gateway programming,
and four routed HTTP requests, retaining JSON evidence with observed source
revisions. Without that flag, bootstrap completion is not full convergence.
Recheck an existing cluster without reinstalling or rebuilding:

```bash
python3 scripts/wait-for-dev.py --context=kind-platform-core --timeout=1200
```

For alternate kind port mappings, pass `--gateway-url=http://localhost:18000`
to bootstrap or the checker. Resolve degraded Applications before the canary
demonstration. Confirm Prometheus is scraping echo and
the Gateway routes reach the stable workload. See [local troubleshooting](local-dev.md).
The default dev pipeline uses a vLLM mock and stub STT/TTS; label it accordingly
in screenshots and recordings.

## 5. Demonstrate a failed release and serving recovery

Use a healthy stable echo revision and an intentionally faulty **runnable**
revision. An image that cannot be pulled or never becomes ready cannot prove
that SLO analysis caught bad request behavior. Build and load the prepared demo images (`echo-service:v2` and
`echo-service:broken`) into the disposable kind cluster:

```bash
./scripts/prepare-canary-images.sh
```

The canary runner captures its Kubernetes context and operates on dev. It
preserves/restores the original image and Argo reconciliation annotation, and
collects evidence. It uses the dedicated long-running
[`tests/canary-load/`](../tests/canary-load/) overlay and keeps that load running
through restoration, which has a 600-second timeout. Inspect its printed output
directory and any cleanup errors. Script regressions cover evidence validation, false-positive rejection, and
smoke-test failure handling. The dated DEV acceptance report records earlier
live canary results; capture a new result for the revision being presented.

```bash
./scripts/canary-demo.sh bad
kubectl argo rollouts -n apps get rollout echo-service
kubectl -n apps get analysisrun -l rollouts.argoproj.io/rollout=echo-service
kubectl -n apps get httproute echo-service \
  -o jsonpath='{range .spec.rules[0].backendRefs[*]}{.name}={.weight}{"\n"}{end}'
curl --fail -H 'Host: echo.platform-core.local' http://localhost/
```

Capture the failed metric for the new canary, stable/canary traffic weights,
and successful application requests after recovery. A generic `Degraded`
status is insufficient. Explain that aborting a rollout restores serving
traffic; it does not automatically revert the bad Git commit. For a real GitOps release, fix or revert the desired image in Git and
demonstrate the healthy revision next. The local demo restores its original
image as part of cleanup.

## 6. Present evidence and cleanup

A five-minute recording can show:

1. The service request and generated delivery/review contract.
2. The running endpoint and links to deployment/telemetry.
3. A failed canary analysis and stable traffic recovery, if actually executed.
4. The worker interruption test and its asserted recovery.
5. One accepted tradeoff and the unverified AWS/GPU boundaries.

Include a concise evidence note using [the recording fields](evidence.md).
Do not fabricate screenshots, passing CI links, recovery times, or cost savings.
If a live component is unavailable, show its source and mark live verification
pending rather than presenting it as a successful exercise.

```bash
./scripts/teardown.sh --mode=local
```

Run teardown only for the disposable cluster used for the demonstration; its
storage is removed. If bootstrap used a custom name, pass the same
`--cluster-name=NAME` to teardown. Repeating teardown for that absent cluster is
safe; unrelated clusters and port-forwards are left alone. External service repositories, registration PRs, image
packages, and any AWS resources need their own lifecycle cleanup. The automated
infrastructure test manages its own isolated test resources.
