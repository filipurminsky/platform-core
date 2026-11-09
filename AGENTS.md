# AGENTS.md

## Project and navigation

platform-core is a reference internal developer platform for local kind and AWS
EKS: Terraform provisions infrastructure, Argo CD reconciles Kubernetes resources,
and Python services demonstrate HTTP, Kafka, LLM inference, and an audio pipeline.
The AWS path is authored but has not been verified on a live account.

This guide applies repository-wide; follow more specific `AGENTS.md` instructions
where present. `CLAUDE.md` imports this file. Keep operational detail in the linked
docs and use this file for commands, ownership boundaries, and change constraints.

| Location | What belongs here |
| --- | --- |
| `services/<service>/` | Python source, tests, `pyproject.toml`, `uv.lock`, Dockerfile, README, Backstage catalog entry |
| `services/<service>/k8s/base/` and `k8s/overlays/{dev,prod}/` | Deployable service resources and environment patches |
| `services/platform-common/src/platform_common/` | Shared Kafka worker settings, processing, retries, dedup, storage, state, metrics, tracing |
| `kubernetes/apps/<service>/applicationset.yaml` | GitOps registration only; points at the service's overlay |
| `kubernetes/platform/` | Platform Applications/ApplicationSets, upstream chart configuration, platform CRs |
| `kubernetes/platform/kafka/overlays/{dev,prod}/` | Kafka cluster, topic, and user overlays, separate from app overlays |
| `kubernetes/tenants/` | Team namespaces, shared Components, quota tiers, per-team RBAC bindings |
| `kustomize/validation/{dev,prod}/` | CI-only aggregate of app overlays; never an Argo CD deployment target |
| `helm/`, `terraform/`, `policy/` | Local Helm charts, foundation infrastructure, OPA policies |
| `observability/`, `backstage/` | Dashboards/alerts/SLOs, catalog/scaffolding |

Read [architecture decisions](docs/architecture.md) for design rationale,
[local development](docs/local-dev.md) for cluster operations, and
[tool upgrades](docs/tool-upgrades.md) before changing versions. Treat executable
configuration as authoritative when older prose disagrees with it.

## Setup and development

Run commands from the repository root unless a command explicitly changes directory.
Use Python 3.14 and uv; each Python project has its own environment and lockfile.
The seven app projects are `echo-service`, `worker-service`, `llm-gateway`,
`audio-api`, `stt-worker`, `llm-worker`, and `tts-worker`. `platform-common` is an
installable shared library; `vllm-inference` uses upstream images and has no Python
project to install or test.

```bash
make help
make deps                  # uv sync --frozen for all eight Python projects
make hooks                 # install pre-commit and pre-push hooks

# Single-service loop (example: echo-service)
(cd services/echo-service && uv sync --frozen)
(cd services/echo-service && OTEL_SDK_DISABLED=true uv run --frozen uvicorn main:app --reload --port 8080)

# Local stack, including Kafka, Redis, S3-compatible storage, and mock inference
# Requires Docker; starts containers and builds service images.
docker compose -f services/docker-compose.yml up -d --build
docker compose -f services/docker-compose.yml logs -f worker-service
```

For infrastructure work, install the versions pinned in
[CI](.github/workflows/ci.yaml), the [kubeconform setup action](.github/actions/setup-kubeconform/action.yaml),
and [Trivy setup action](.github/actions/setup-trivy/action.yaml). Current validation
targets include Kubernetes 1.36.0, Helm 4.3.1, Terraform 1.17.1, Kustomize 5.8.3,
kubeconform 0.8.0, conftest 0.71.1, kube-linter 0.8.3, and Ruff 0.17.4.
Local clusters use kind ≥ 0.33.0 with the pinned `kindest/node:v1.36.4` image.
Keep tool pins, checksums, CRD schema versions, and deployed charts aligned.

## Tests and validation

Run checks relevant to the changed files before pushing. Report checks run and
any failures or unavailable prerequisites. Documentation-only changes need path,
command, and diff checks; they do not require starting a cluster or applying IaC.
CI definitions are in [.github/workflows/ci.yaml](.github/workflows/ci.yaml) and
[test-apps.yaml](.github/workflows/test-apps.yaml).

### Python

```bash
make lint                  # Ruff lint and format check, apps + shared library
make test                  # pytest separately in each Python project
make integration-test      # cross-service audio contract tests using the audio-api venv
make infrastructure-test   # isolated real Kafka/Redis/S3 recovery tests; requires Docker
make script-test           # operational-script regressions and shell syntax checks
make scaffold-test         # npm ci + generated-service tests; Node/npm, uv, kustomize, conftest

# Targeted checks (replace echo-service with the affected project)
(cd services/echo-service && uv run --frozen pytest -q)
(cd services/echo-service && uv run --frozen pytest test_main.py -k health -q)
uvx ruff@0.17.4 check services/echo-service
uvx ruff@0.17.4 format --check services/echo-service
```

App tests live in `services/<service>/test_main.py`; shared-library tests live in
`services/platform-common/tests/`, and integration tests in `tests/integration/`.
Keep `OTEL_SDK_DISABLED=true` in test setup so tracing cannot block on export.
Run service suites separately: their `main.py` and `app/` module names overlap.
For shared worker changes, test `platform-common` and all four consumers
(`worker-service`, `stt-worker`, `llm-worker`, `tts-worker`); run integration tests
when audio behavior changes. Add regression coverage for changed behavior.

### Kubernetes, Helm, and policies

```bash
# Render both environments and evaluate cross-resource OPA relationships.
# pipefail prevents a failed render being hidden by the next command.
set -o pipefail
for environment in dev prod; do
  kustomize build "kustomize/validation/$environment" \
    | conftest test --combine --policy policy/ - || exit 1
  kustomize build "kubernetes/platform/kafka/overlays/$environment" > /dev/null || exit 1
done

make helm-lint
kube-linter lint kubernetes/ --config .kube-linter.yaml
kube-linter lint . --config .kube-linter-pdb.yaml
# Requires Kyverno CLI >= 1.19 for the CEL policy types.
kyverno apply kubernetes/platform/kyverno-policies/base/
```

Also build each affected service overlay and tenant directory independently.
Rendering alone does not validate Kubernetes schemas. `make schemas` downloads
and converts the pinned CRDs using the same setup script as
[CI](.github/actions/setup-kubeconform/action.yaml). Locally, schemas live under
`.cache/platform-validation/crd-schemas` and `scripts/kubeconform-k8s` checks the
cache against CI's version pins before strict validation. CI installs its wrapper
on PATH and places schemas in `/tmp/crd-schemas`.

```bash
make schemas               # requires network access, curl, yq, Python 3
make kustomize             # aggregate, service, Kafka, tenant and canary-load renders
make helm-validate         # lint + render + schema validation for all three local charts
make policy                # --combine cross-resource checks for both dev and prod
make kube-lint             # workload and repository-wide PDB configurations
```

For targeted schema checks after `make schemas`, use the repository wrapper:

```bash
set -o pipefail
for environment in dev prod; do
  kustomize build "kustomize/validation/$environment" | ./scripts/kubeconform-k8s || exit 1
  kustomize build "kubernetes/platform/kafka/overlays/$environment" | ./scripts/kubeconform-k8s || exit 1
  for overlay in services/*/k8s/overlays/"$environment"/; do
    kustomize build "$overlay" | ./scripts/kubeconform-k8s || exit 1
  done
done
helm template demo helm/demo-app --set image.repository=registry.example.com/demo-app | ./scripts/kubeconform-k8s
helm template vllm helm/vllm | ./scripts/kubeconform-k8s
helm template backstage helm/backstage -f helm/backstage/values.yaml | ./scripts/kubeconform-k8s
```

Plain kubeconform does not install the repository wrapper or CRD cache. Use the
explicit CI skip list only for exempted kinds; never mask missing schemas with
a blanket ignore flag.

`make validate` runs the non-cluster aggregate: Python lint/tests, in-process
integration, operational-script and generated-service tests, manifest/chart
schema validation, combined OPA policy, both kube-linter configurations, and
Terraform formatting/validation. Run `make schemas` first. Security scans,
container builds, the Docker infrastructure recovery suite, and live-cluster
checks remain separate gates. Hosted CI also runs an echo-service kind smoke test.

### Terraform

```bash
terraform fmt -check -recursive terraform/
terraform -chdir=terraform/environments/dev init -backend=false
terraform -chdir=terraform/environments/dev validate
terraform -chdir=terraform/environments/prod init -backend=false
terraform -chdir=terraform/environments/prod validate
```

Initialization downloads providers/modules; validation does not apply resources.
Use [.github/workflows/terraform-plan.yaml](.github/workflows/terraform-plan.yaml)
for the plan workflow and required environment inputs.

## Code and dependency conventions

- Inherit Ruff configuration from the root [pyproject.toml](pyproject.toml): Python
  3.14, 100-column formatting, double quotes, space indentation, sorted imports.
  `make fmt` formats all Python projects.
- Keep service entrypoints in `main.py`, typed pydantic-settings in `app/config.py`,
  and helpers in `app/`. Fix shared worker behavior in `platform_common` rather
  than copying fixes into individual workers.
- App projects use `tool.uv.package = false`; `platform-common` uses `uv_build`
  and is consumed through relative `[tool.uv.sources]` entries.
- Keep runtime dependencies and dev dependency groups pinned. After changing a
  project's dependencies, run `uv lock` in that project and commit its `uv.lock`.
  Refresh affected consumer locks when shared-library dependency metadata changes;
  `uv lock --check` verifies consistency. Normal setup/tests use `--frozen`.
- Preserve service ownership: source and deployable resources stay together;
  platform-reviewed ApplicationSets stay under `kubernetes/apps/`. Update catalog
  and scaffolding entries when adding services.

## Architecture constraints

### GitOps, environments, and delivery

- App and Kafka ApplicationSets select overlays using the Argo CD cluster Secret's
  `environment` label. Bootstrap sets `dev` for local and `prod` for AWS. Preserve
  the selector that prevents app sync until that label exists.
- The App-of-Apps entrypoint is `kubernetes/platform/argocd/app-of-apps.yaml`.
  Preserve environment selectors: Crossplane, Karpenter, and DCGM exporter are
  prod-only; SeaweedFS and AKHQ are dev-only; Redis and namespaces run in both.
- `echo-service` is an Argo `Rollout`. Use its JSON6902 replica/resource patches;
  Kustomize's normal `replicas:` transformer does not handle it. Preserve the
  ServiceMonitor's `podTargetLabels: [rollouts-pod-template-hash]`, canary-scoped
  analysis, and ApplicationSet exclusions for controller-managed HTTPRoute weights.
- Release images flow through tests → amd64 scan → multi-arch push with SBOM and
  provenance → arm64 digest scan → keyless signing → promotion. The `production`
  GitHub Environment gates promotion, which verifies signatures and updates all
  digests together in `services/<app>/k8s/overlays/prod/`. Never promote into the CI
  aggregate or replace production digests with mutable tags.

### Inference and audio pipeline

Read [the frozen audio contract](docs/audio-pipeline-contract.md) **before touching
any pipeline service**. Contract changes require re-freezing and updating every stage.

- Flow: `audio-api` → `audio.jobs` → `stt-worker` → `audio.transcripts` →
  `llm-worker` → `audio.summaries` → `tts-worker` → `audio.results`; each worker has
  a stage DLQ. Kafka keys are UUID `job_id`s. Forward `created_at` unchanged and
  extract/re-inject W3C `traceparent`/`tracestate` at every hop.
- Exchange object-storage keys, never inline payloads. Use deterministic writes
  under `audio/`, `transcripts/`, `summaries/`, and `speech/<job_id>`. Preserve Redis
  job-state statuses, seven-day TTL, error fields, and metric names in the contract.
- Preserve `platform_common.stage` processing: validate → atomic Redis dedup claim
  → process → delivery-confirm output → commit. Permanent errors go directly to
  DLQ; transient retries must fit `max.poll.interval.ms`. Preserve SIGTERM drain.
  `worker-service` dedup is only in-memory/per-pod; handlers must be idempotent.
- `llm-worker` calls `llm-gateway`, never vLLM directly. Preserve API-key auth,
  `Retry-After` handling, the cold-start budget, and long-transcript chunking.
- Gateway readiness must remain a self-check: gating on vLLM deadlocks activation
  from zero. Keep queue depth for 1→N scaling and gateway traffic/error metrics for
  0→1 activation. Production gateway access is internal-only, auth fails closed,
  and forwarded client IPs are trusted only from `TRUSTED_PROXY_CIDRS`.
- In dev, vLLM is paused at zero and the gateway uses `vllm-mock`. Keep dev patches
  removing GPU scheduling/resources and using `standard` storage. Model caches are
  per-pod ephemeral volumes; a shared RWO EBS PVC cannot serve multiple replicas.
- STT/TTS use stub backends in **both** environments. Real `nemo`/`kokoro` imports
  are absent from standard images. Enabling them requires building GPU image
  targets and adding prod backend settings plus GPU scheduling/resources.

### Networking, tenancy, and secrets

- Use Gateway API `HTTPRoute`, with parent Gateway `platform` in namespace
  `envoy-gateway-system` (GatewayClass `envoy`, HTTP listener, `*.platform-core.local`).
  Allow that namespace in ingress NetworkPolicies; do not introduce legacy Ingress.
- Local kind uses Cilium with `cni.chainingMode=portmap` for Envoy hostPort access.
  Bootstrap installs Cilium before waiting for node readiness. EKS retains AWS VPC
  CNI; prod Envoy uses an NLB LoadBalancer.
- Tenant `_template/` and `tiers/` are Kustomize **Components**. Roles are shared;
  RoleBindings and OIDC groups are per tenant. `tenant-admin` must remain read-only
  on quotas, limits, and NetworkPolicies; `tenant-viewer` must not read secrets.
- OPA checks relationships across the combined manifest set. Preserve checks for
  Deployments, Rollouts, StatefulSets, and DaemonSets, including NetworkPolicy
  selectors that actually match workloads in `apps` and `tenant-*` namespaces.
- Strimzi owns Kafka SASL Secrets; do not recreate them through ESO. Reflector
  mirrors `platform-kafka-cluster-ca-cert` from `platform` into `apps`. Prod uses
  SCRAM over TLS on :9093 only; dev uses plaintext :9092. KEDA reads the password
  from the user Secret and username via `KAFKA_SASL_USERNAME`, not trigger metadata.
- Dev S3 is SeaweedFS behind compatibility Service `minio:9000`; retain
  `minio-credentials` names/keys. Prod uses IRSA and S3 with no static credentials
  or `S3_ENDPOINT_URL`. Other prod application secrets come from ESO.
- Local bootstrap creates `redis-auth`, `minio-credentials`, and
  `llm-gateway-api-keys`. The latter's `llm-worker` key must be included in its
  accepted `api-keys` CSV. Never commit real credentials.
- Preserve Kyverno CEL `ValidatingPolicy`/`ImageValidatingPolicy` types and
  fail-closed image signature verification. Tenant pod standards are enforced;
  `latest` tags are audited in dev and denied in prod.

### Infrastructure and observability

- Terraform owns foundation infrastructure (VPC, EKS, IAM/IRSA); Crossplane owns
  app-facing S3. Keep private nodes, private-only prod API access by default,
  restricted public CIDRs, and S3-native Terraform state locking (Terraform ≥ 1.10).
- Crossplane v2 uses namespaced `ObjectStorageBucket` composites, namespaced
  `*.aws.m.upbound.io` resources, and `ClusterProviderConfig`; no v1 claims.
  Preserve bucket naming `platform-core-<env>-<ns>-<name>` to match IAM policies.
  Read [Crossplane guidance](docs/crossplane.md) before edits or live upgrades.
- Prod Karpenter runs on the on-demand `role=platform` group. Its GPU NodePool
  replaces the static GPU group and must match workload taints/tolerations and
  `role=gpu` selectors. Preserve discovery tags and Terraform-output wiring.
- Keep app OTLP export through `otel-collector.monitoring:4317` to Tempo, HTTP/Kafka
  context propagation, and structured-log `trace_id`/`span_id` fields. Grafana
  datasource UIDs are `prometheus`, `loki`, and `tempo`; Alloy collects logs.
- Register new dashboard JSON in the `observability/grafana/dashboards/`
  kustomization's `configMapGenerator`. Keep alert queries aligned with recording
  rules and [SLO definitions](docs/slo-definitions.md). GPU/cost pipeline panels
  are prod-only; Kafka Exporter is disabled in dev, so missing data there is expected.

## Build, operations, and review

Container builds require BuildKit and **`services/` as the build context**, so the
shared library is available. Preserve per-Dockerfile `.dockerignore` files and the
non-root, digest-pinned runtime stage without uv.

```bash
DOCKER_BUILDKIT=1 docker build -f services/echo-service/Dockerfile -t platform-core/echo-service:dev services/
make images                # build all seven app images locally
```

Read-only diagnostics before cluster changes:

```bash
./scripts/bootstrap.sh --mode=local --check
make doctor
```

For cluster work, see [local-dev.md](docs/local-dev.md) and the focused
[runbooks](docs/runbooks/). These commands change the selected environment; use
them when the task calls for cluster lifecycle or demos:

```bash
./scripts/bootstrap.sh --mode=local
# AWS: apply the Terraform foundation and wire outputs before bootstrap --mode=aws.
kubectl argo rollouts get rollout echo-service -n apps --watch
./scripts/prepare-canary-images.sh  # build/load runnable good and faulty dev images
./scripts/canary-demo.sh bad   # verified abort/recovery; restores original image
kubectl apply -k tests/load   # starts k6 load
./scripts/teardown.sh --mode=local
```

Before submitting, run `git diff --check`, relevant checks above, and review the
rendered dev/prod behavior for manifest changes. Describe the problem, resulting
behavior, validation, and any deployment/migration implications in the PR. Keep
GitHub Actions pinned by commit SHA, downloaded tools checksum-verified, and job
permissions minimal. Update related docs and version pins with behavior changes.
