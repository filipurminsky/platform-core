# Platform tool upgrades and compatibility

Checked on 2026-11-09. These changes update repository pins, bootstrap defaults,
CI tools, scaffold dependencies, and Python lockfiles. They do not upgrade a
running cluster or the workstation's globally installed tools.

## Updated versions

| Component | Previous | Selected |
|---|---|---|
| Kubernetes fleet target | 1.35 | 1.36 |
| kind node image | 1.35.8 | 1.36.4, with the digest published for kind 0.33.0 |
| Cilium and Hubble chart | 1.16.5 | 1.20.4 |
| Argo CD chart | 10.14.0 | 10.17.1, Argo CD 3.6.1 |
| Envoy Gateway controller and CRD charts | 1.9.3 | 1.10.0 |
| kube-prometheus-stack chart | 92.20.0 | 93.1.1, Prometheus Operator 0.95.1 |
| Argo Rollouts chart | 2.43.9 | 2.43.11, controller remains 1.10.2 |
| DCGM Exporter chart | 4.8.4 | 4.9.0 |
| Reflector chart | 10.0.73 | 10.0.74 |
| SeaweedFS chart and Compose image | 4.52.0 / 4.52 | 4.53.0 / 4.53 |
| Trivy | 0.76.1 | 0.76.2, CI checksum and pre-commit image digest updated together |
| Ruff | 0.17.3 | 0.17.4, pinned consistently in hooks, Makefile and CI |
| helm/kind-action | 1.15.1 | 1.15.2, commit SHA pinned |
| cosign-installer / cosign | 4.1.2 / 3.0.6 | 4.1.3 / 3.1.4, action commit SHA pinned |
| k6 | 0.49.0 | 2.3.0, image digest pinned |
| BusyBox init containers | 1.36 | 1.38.0, image digest pinned |
| FastAPI | 0.162.0 | 0.162.2 |
| boto3 | 1.43.124 | 1.43.126 |
| Service Python runtime | 3.13 | 3.14.10, slim and Alpine image digests pinned |

All eight service/library lockfiles were refreshed. Python 3.14.10 runtime images
use the current published digest; the mock and microservice scaffold now use
that same base. The scaffold also uses uv 0.13.7 and the service versions of
FastAPI, Uvicorn, Prometheus client and pytest. Its test client dependency is
`httpx2`, as required by the current Starlette TestClient.

## Compatibility decisions

Kubernetes **1.36** is the newest common tested minor for
[KEDA 2.21](https://keda.sh/docs/2.21/operate/cluster/) and
[Cilium 1.20](https://docs.cilium.io/en/v1.20/network/kubernetes/requirements/).
Although Kubernetes 1.37 is available, those two components do not yet list it
in their tested ranges. Strimzi 1.3.1 supports Kubernetes 1.32–1.37 and Kafka
4.3.2; both pins remain current. The kind image comes from the
[kind 0.33.0 release](https://github.com/kubernetes-sigs/kind/releases/tag/v0.33.0),
whose newest published 1.36 image is 1.36.4.

Gateway API stays at **1.6.1**, including the CI schema bundle. This is the
version bundled and tested with Envoy Gateway 1.10, despite Gateway API 1.7.0
being available. The controller and CRD charts move together.
[Envoy compatibility matrix](https://gateway.envoyproxy.io/news/releases/matrix/).

Prometheus selectors now use `matchLabels: {}` instead of `null`. The new CRD
schema rejects null maps; empty maps still discover every monitor and rule.
Keep `ServerSideApply=true` so Argo CD updates the chart's CRDs before the
operator. The chart upgrade also updates operator RBAC.
[Monitoring upgrade notes](https://github.com/prometheus-community/helm-charts/blob/kube-prometheus-stack-93.1.1/charts/kube-prometheus-stack/UPGRADE.md).

DCGM 4.9 changes utilization from a percentage to a ratio. The audio dashboard
uses `100 * avg(DCGM_FI_DEV_GPU_UTIL_RATIO) by (pod)` to preserve its percent
axis. Existing scheduling and ServiceMonitor values render with the new chart;
the chart supplies its new configuration format. GPU collection still needs
verification on an NVIDIA node.
[DCGM release notes](https://github.com/NVIDIA/dcgm-exporter/releases/tag/4.9.0).

The cosign 3.1.4 CLI still accepts `--new-bundle-format=false` and
`--use-signing-config=false`. Keep these together while Kyverno consumes
classic signature tags. They are deprecated upstream, so removing them needs
a separate signing/admission interoperability check.
[Signing options](https://github.com/sigstore/cosign/blob/v3.1.4/cmd/cosign/cli/options/sign.go).

The following versions are deliberately retained:

- **vLLM 0.32.0-cu129:** the newer `0.33.0rc3` is a release candidate, even
  though GitHub marks it as latest. Preserve CUDA 12.9 compatibility with the
  existing GPU deployment. [Releases](https://github.com/vllm-project/vllm/releases).
- **Backstage image 1.55.2:** the configured GHCR repository serves this tag;
  `1.56.3` and `1.56.0` return `not found`. A newer source release alone is not
  a deployable image upgrade.
- **Redis 7.4.12:** latest published 7.4 patch. Preserve the repository's
  explicit persistence-format decision; moving to Redis 8.10.3 needs a tested
  data migration and rollback plan.

The seven application services and shared library now require **Python 3.14**.
Local `.python-version` files and the reusable CI test workflow select 3.14;
the container images pin [Python 3.14.10](https://www.python.org/downloads/release/python-31410/).
The microservice scaffold and both vLLM mocks use the same runtime minor.
Ruff targets Python 3.14, including deferred annotations and its exception
syntax. Every locked runtime and test dependency resolves from wheels on Linux
AMD64 and ARM64. These checks cover the standard worker images with stub ML
backends. Optional NeMo/Kokoro image stages remain disabled and require separate
ML dependency validation; vLLM retains the interpreter in its upstream image.
Checkov 3.4.3 also passes the Terraform gate on Python 3.14, so both CI scanner
commands now use that minor.

Helm 4.3.1, Terraform 1.17.1, kustomize 5.8.3, kubeconform 0.8.0, conftest
0.71.1, kube-linter 0.8.3, Checkov 3.4.3, uv 0.13.7, gitleaks 8.30.1, KEDA
2.21.0, Strimzi 1.3.1, ESO 2.14.0, Kyverno 1.19.2, Karpenter 1.15.1 and the
remaining pinned chart releases were already current. Terraform registry
versions remain AWS 6.72.0, EKS module 21.29.0, VPC module 6.7.3 and IAM module
6.8.2, within the existing constraints.

## Existing local clusters

Bootstrap installs Cilium 1.20.4 on new clusters and reconciles existing 1.20
installations. It refuses other installed minor versions. For a cluster still
on 1.16, follow **1.16 → 1.17 → 1.18 → 1.19 → 1.20**, first reaching the latest
patch of each minor and performing the required preflight checks at every hop.
Preserve the original values and `upgradeCompatibility` as described in the
[Cilium upgrade guide](https://docs.cilium.io/en/v1.20/operations/upgrade/).
The kind networking settings retain kube-proxy and portmap hostPort chaining.

Changing kind's node image affects new clusters only. To replace an existing
disposable local cluster, back up any data you need before teardown and
bootstrap; this removes the old cluster's local storage. For EKS, upgrade the
control plane one minor at a time and check add-on and node readiness before
continuing. No cluster changes were applied during this update.

## Validation results

| Check | Result |
|---|---|
| Service unit tests | 195 passed, 1 existing skip, on Python 3.14.10 |
| Ruff 0.17.4 | Lint and format checks passed for 82 source files |
| Local Helm charts | All three linted with `--strict` and rendered |
| Updated upstream charts | Rendered with repository values for Kubernetes 1.36 |
| Kubernetes and CRD schemas | 452 resources valid; no invalid resources or schema lookup errors |
| OPA | Dev and prod aggregate relationship checks passed |
| kube-linter | Both repository configurations passed |
| Terraform 1.17.1 | Formatting, registry initialization and dev/prod validation passed |
| Trivy 0.76.2 | IaC scan passed with the repository's HIGH/CRITICAL gate and exclusions |
| Checkov 3.4.3 on Python 3.14 | Terraform gate: 110 checks passed, 0 failed, 4 skipped |
| Python 3.14 wheels | All eight projects' locked runtime/test dependencies resolve without source builds for Linux AMD64 and ARM64 |
| Python 3.14 container builds | All seven services passed ARM64 builds/import checks; audio-api and llm-worker also passed on AMD64 |
| Python 3.14 Compose mock | Alpine image import smoke check and Compose configuration validation passed |
| k6 2.3.0 | Existing load script imported and its scenarios/thresholds validated with `k6 inspect` |
| Bootstrap upgrade guard | Mocked fresh, same-minor, older-minor and Helm lookup-error cases passed |
| Lockfiles | All eight passed `uv lock --check` |
| Integration target | Existing harness fails; details below |

CRD definitions themselves were excluded from kubeconform; their served
schemas were converted and used to validate the custom resources. No live
admission dry-run, GPU execution, AWS apply or multi-architecture image scan was
performed. YAML parsing used the renderer and schema validator. Optional local
chart improvements remain: values schemas and `.helmignore` files, plus NOTES
for demo-app and vllm; these are not required for the version update.

`make integration-test` currently fails to import `platform_common` because
audio-api's test environment does not install the worker library. Supplying
the missing dependencies in an isolated Python 3.13 environment exposes four
existing failures: `FakeProducer` never calls the delivery callback, so ingest
times out instead of returning a job. The harness predates the workers' shared
processing and delivery-confirmation behavior and needs its own repair. The
service unit suites cover those behaviors individually; the cross-service
integration suite cannot yet establish end-to-end compatibility.
