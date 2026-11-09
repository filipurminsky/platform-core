# Local development

Run commands from the repository root. Choose Compose for application changes;
use kind to exercise GitOps, Gateway API, admission, and autoscaling.

## Application loop with Docker Compose

The stack includes all seven Python apps, Kafka in KRaft mode, Redis,
SeaweedFS behind the compatibility name `minio`, and a vLLM mock. STT/TTS are
stubs. No GPU or Kubernetes cluster is required.

```bash
docker compose -f services/docker-compose.yml up -d --build
docker compose -f services/docker-compose.yml ps
docker compose -f services/docker-compose.yml logs -f worker-service
```

In another terminal:

```bash
docker compose -f services/docker-compose.yml exec -T kafka kafka-console-producer \
  --bootstrap-server localhost:9092 --topic jobs <<'MESSAGE'
{"id":"test-1","type":"data-transform","payload":{"input":"hello","operation":"uppercase"}}
MESSAGE
curl --fail http://localhost:8081/healthz
LLM_KEY=$(docker compose -f services/docker-compose.yml exec -T llm-worker printenv LLM_GATEWAY_API_KEY)
curl --fail -H "Authorization: Bearer $LLM_KEY" http://localhost:8080/v1/models
unset LLM_KEY
```

Compose publishes echo-service on `8081`, llm-gateway on `8080`, audio-api on
`8082`, and the mock directly on `8000`. The gateway requires the configured
bearer key. Published Kafka port `9092` is not a general host-client endpoint:
the broker advertises the Compose hostname `kafka`, so run clients on its network.
The Compose Redis/object-store credentials are disposable local defaults.

For Python-only work, install uv and Python 3.14, then `make deps`. Each project
has a separate environment and frozen lockfile. Run suites separately because
service module names overlap:

```bash
(cd services/echo-service && OTEL_SDK_DISABLED=true uv run --frozen pytest -q)
make integration-test
```

The integration target exercises cross-service contracts in process. The
[showcase guide](showcase.md) also covers `make infrastructure-test`, the
separate Docker-based real-infrastructure gate.

```bash
docker compose -f services/docker-compose.yml down
```

## Kubernetes loop with kind

### Prerequisites

- Running Docker daemon with Compose; host ports 80/443 available for kind.
- kind ≥ 0.33.0; the checked-in configuration pins `kindest/node:v1.36.4`.
- kubectl compatible with Kubernetes 1.36; Helm 4.3.1.
- `openssl`, `base64`, and the standard shell tools used by bootstrap.
- `kubectl argo rollouts` for canary exercises, matching the controller version
  pinned in CI.
- uv/Python 3.14 and the validation tools in [CI](../.github/workflows/ci.yaml)
  when developing or running repository checks; Terraform is not used by local bootstrap.

On macOS, Homebrew can install the tools, but verify the installed versions
against the repository pins rather than assuming its current packages match.
Start with at least 8 GB allocated to Docker; this is guidance, not a measured
minimum for every platform component. Record host resources and timings with
any published demo result.

### Bootstrap and observe

```bash
./scripts/bootstrap.sh --mode=local --check
./scripts/bootstrap.sh --mode=local
kubectl get applications -n argocd -w
```

Bootstrap creates or reuses `kind-platform-core`, installs Cilium with portmap
chaining before waiting for node readiness, installs Argo CD, creates local
Secrets, labels the destination cluster `environment=dev`, and applies the root
App-of-Apps. Applications then reconcile asynchronously from the configured
remote repository/revision. To demonstrate a fork, update the repository URLs
in GitOps configuration and publish the intended revision first.

Bootstrap refuses to reuse nodes outside Kubernetes 1.36 or a cluster still
using kindnet. Updating the checked-in node image does not upgrade an existing
cluster. Preserve an older environment and use a separate name/config instead:

```bash
cp terraform/modules/kind/kind-config.yaml /tmp/platform-core-qa-kind.yaml
# Edit the copied config's hostPort mappings if 80/443 are occupied.
./scripts/bootstrap.sh --mode=local --cluster-name=platform-core-qa \
  --kind-config=/tmp/platform-core-qa-kind.yaml
```

The cluster name changes the kubectl context to `kind-platform-core-qa`.
Allow enough Docker memory for every running cluster; the dated
[dev QA report](qa-dev-2026-11-09.md) records the resources and limitations of
one verified setup. Argo CD still reads the configured Git revision, so local
uncommitted edits are not deployed by these commands alone.

The dev overlay pauses real vLLM at zero and points llm-gateway at `vllm-mock`.
Kafka uses one combined KRaft broker/controller with plaintext port 9092; there
is no ZooKeeper. Crossplane, Karpenter, and DCGM are prod-only. Kafka Exporter
is disabled in dev, so its dashboard series can be absent.

Argo CD exposes controller metrics during bootstrap; the `monitoring-rules`
Application installs its ServiceMonitor and custom SLO rules after their CRDs
exist. Kind's controller-manager, scheduler, etcd, and kube-proxy metrics are
not exposed to these chart scrape endpoints: six down targets are a known
local limitation. Application scrape failures are not covered by that exception.

### Access

Run port-forwards in separate terminals:

```bash
kubectl port-forward svc/argocd-server -n argocd 8080:80
kubectl port-forward svc/grafana -n monitoring 3000:80
kubectl port-forward svc/hubble-ui -n kube-system 12000:80
make argocd-password
```

Check the Grafana credential configuration in
[`kubernetes/platform/monitoring/application.yaml`](../kubernetes/platform/monitoring/application.yaml)
before logging in. Do not expose local administrative endpoints publicly.

Envoy accepts requests on host port 80. A Host header avoids changing `/etc/hosts`:

```bash
kubectl get gateway platform -n envoy-gateway-system
kubectl get httproute -A
curl --fail -H 'Host: echo.platform-core.local' http://localhost/
KEY=$(kubectl get secret llm-gateway-api-keys -n apps -o jsonpath='{.data.llm-worker}' | base64 -d)
curl --fail -H 'Host: llm-gateway.platform-core.local' -H "Authorization: Bearer $KEY" http://localhost/v1/models
unset KEY
```

For browser access, add the desired names to `/etc/hosts`:

```text
127.0.0.1 echo.platform-core.local llm-gateway.platform-core.local audio-api.platform-core.local backstage.platform-core.local akhq.platform-core.local
```

Backstage integration depends on its packaged backend modules and configured
GitHub credentials. Catalog visibility alone does not prove scaffolder actions
or repository publishing work. Follow the generated service README and
[showcase steps](showcase.md) for the publishing and registration boundary.

### Troubleshooting

| Symptom | Inspect | Likely next action |
|---|---|---|
| Docker connection or port binding failure | `docker info`, Docker resource settings, listeners on 80/443 | Start Docker or free the conflicting port before retrying |
| Nodes remain NotReady | `kubectl -n kube-system get pods`; Cilium logs | Resolve CNI startup; do not wait for nodes before installing Cilium |
| Existing Cilium minor rejected | `helm list -n kube-system` | Follow consecutive-minor upgrades in [tool upgrades](tool-upgrades.md) |
| No application generated | `kubectl -n argocd get secret in-cluster --show-labels` | Confirm environment label and ApplicationSet generator status |
| Argo sync fails | `kubectl -n argocd get applications`; describe the failed Application | Check remote revision, repository credentials, missing CRDs or Secrets |
| Host request fails | Gateway `Programmed`, HTTPRoute `Accepted`/`ResolvedRefs`, Envoy pods | Check hostPort placement and Cilium `cni.chainingMode=portmap` |
| ImagePullBackOff | `kubectl -n apps describe pod <pod>` | Build/load the exact dev image into kind or configure an accessible registry |
| Pod Pending | `kubectl -n apps describe pod <pod>` | Check available CPU/memory, quota, and storage class |
| Gateway returns 401 | `llm-gateway-api-keys` Secret keys and request Authorization header | Use an accepted bearer key; never print Secrets in diagnostic bundles |
| Missing GPU/Kafka-exporter panels | Dev/prod component selectors | These components are intentionally absent in dev |

Dev application manifests can reference local image tags. When needed, build
with `services/` as context and load the exact referenced name, for example:

```bash
docker build -f services/echo-service/Dockerfile -t echo-service:latest services/
kind load docker-image echo-service:latest --name platform-core
```

Loading an image does not restart an existing pod. Apply an intentional dev
revision and confirm rollout health; do not use the CI validation aggregate as
a deployment target.

For constrained machines, use Compose or a dedicated branch that omits unwanted
Application registrations. Deleting an Argo-managed Application alone is not a
persistent way to trim the stack: the root Application can recreate it, and
finalizers may delete its workloads and data.

### Demos and teardown

Follow [the showcase walkthrough](showcase.md) for prerequisites, expected
outcomes, and evidence capture before running scripts that change workload state.

```bash
./scripts/teardown.sh --mode=local
```

Teardown removes the disposable cluster and its local storage. Save anything
you need first. Relabeling an existing cluster from dev to prod is not an AWS
migration: infrastructure, secrets, storage, network, and image prerequisites
must be prepared separately.
