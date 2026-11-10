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
Plan at least 16 GiB allocated to Docker for one full platform. The existing
three-node QA cluster used about 11 GiB on 2026-11-10; builds, tests, and other
containers need additional headroom. This is an observed planning baseline,
not a certified minimum. Use Compose when that budget is unavailable. Record
host resources and timings with any published demo result.

### Bootstrap and observe

```bash
./scripts/bootstrap.sh --mode=local --check
./scripts/bootstrap.sh --mode=local --wait
```

Bootstrap creates or reuses `kind-platform-core`, installs Cilium with portmap
chaining before waiting for node readiness, installs Argo CD, creates local
Secrets, builds all seven application images using the `services/` build context,
loads the exact `<service>:latest` names into every kind node, labels the
destination cluster `environment=dev`, and applies the root App-of-Apps.
The bootstrap-owned application project omits the production change-freeze in
local mode, so overnight setup can converge. Applications reconcile from the configured
remote repository/revision. To demonstrate a fork, update the repository URLs
in GitOps configuration and publish the intended revision first.

`--wait` waits up to 1200 seconds for Applications to be Synced/Healthy, the
Gateway's current generation to be programmed, and routed echo, audio health,
and portal health requests to return HTTP 200. Only the intentionally paused
`vllm-inference` Application may be Suspended. Failure exits nonzero and prints
the evidence directory with observed revisions and actionable diagnostics.
Use `--timeout=1800` for slower application convergence. Cilium and Argo CD
each have a separate, bounded Helm readiness budget of 600 seconds, including
cold image pulls; override it with `--install-timeout=900` when needed. Failed
installation prints the relevant namespace, pod/event commands, and budget.
Check for successful pull progress before increasing it: a larger timeout does
not repair invalid image references, unavailable registries, or unschedulable pods.
Without `--wait`, bootstrap only
starts reconciliation; successful exit does not assert platform health.

App ApplicationSets and generated service registrations retry failed syncs
automatically, with a 10-second initial backoff capped at 60 seconds. This lets
them recover when KEDA, Rollouts or monitoring CRDs arrive during a cold start.
Retries do not suppress errors or bypass admission. The `--wait` observer still
has a finite deadline: inspect its diagnostics if a bad configuration or
unavailable dependency prevents convergence. A root Application retry policy
alone does not configure its children.

For a clean-checkout walkthrough, clone the published repository, then run:

```bash
git clone https://github.com/filipurminsky/platform-core.git platform-core-demo
cd platform-core-demo
./scripts/bootstrap.sh --mode=local --check --require-published-source
./scripts/bootstrap.sh --mode=local --require-published-source --wait
```

The optional source check rejects unpublished HEADs and modified/untracked
deployment inputs before creating a cluster. Ordinary development bootstrap
still permits local changes, but those changes do not become GitOps desired
state until published. The check records the starting GitHub `main` SHA;
child Applications and portal templates **track `main`**, which can advance
during setup. The acceptance evidence records each Application's observed
revision. This is not an immutable fleet revision pin, and no host-local Git
or catalog HTTP server is needed. Use a published revision that includes these
commands; the previous QA snapshot is not proof of a clean bootstrap from a
new revision.

Rerunning bootstrap reuses the cluster and local credentials, reconciles Helm
releases, and reloads the images. It does not delete data or restart existing
application pods. For a routine repeatability check, rerun from the same
checkout. If rebuilding changed code under dev's mutable tags, existing pods
must be deliberately replaced to consume the new image; a successful image
load alone does not demonstrate that new code is serving. `--skip-images` is
only for a cluster where all seven exact image references are already loaded.

Initial verification on 2026-11-10 covered the source checker against a clean checkout
of published `3dc159b`, actual builds/loads of all seven images into the existing
QA cluster, and creation/deletion of a separate empty Kubernetes 1.36.4 cluster
with repeated named teardown. Existing clusters and the selected kubectl
context were preserved. A subsequent fresh full-stack attempt from published
`b51b50a` temporarily stopped the retained QA nodes to fit the 16 GiB Docker
allocation. It exposed a cold-install failure: Cilium's five-minute Helm deadline
expired while successful image pulls were still completing. The fresh cluster
was removed and retained QA restored. The configurable installation budget above
addresses that failure. A second fresh attempt at `3707bdf` passed that phase
but exposed exhausted child-Application retries just before KEDA's CRDs became
available. It is also recorded as a failure, without a manual resync. See the
[follow-up acceptance record](qa-followups-2026-11-10.md) for the diagnosis,
repair and subsequent published-source results.

Bootstrap refuses to reuse nodes outside Kubernetes 1.36 or a cluster still
using kindnet. Updating the checked-in node image does not upgrade an existing
cluster. Preserve an older environment and use a separate name/config instead:

```bash
cp terraform/modules/kind/kind-config.yaml /tmp/platform-core-qa-kind.yaml
# Edit the copied config's hostPort mappings if 80/443 are occupied.
./scripts/bootstrap.sh --mode=local --cluster-name=platform-core-qa \
  --kind-config=/tmp/platform-core-qa-kind.yaml --gateway-url=http://localhost:18000 --wait
```

The example assumes the copied config maps container port 80 to host port 18000
(and 443 to a separate free port). `--gateway-url` configures acceptance probes;
it does not alter the kind port mappings or the portal's browser base URL.
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

Bootstrap now builds/loads all dev application images. To repeat just that step:

```bash
./scripts/dev-images.sh --cluster-name=platform-core
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
# For the custom cluster from the example above:
./scripts/teardown.sh --mode=local --cluster-name=platform-core-qa
```

Choose exactly the teardown command for the disposable cluster you created.
Teardown removes that named cluster and its local storage; rerunning it for an
already absent cluster is safe. It does not kill unrelated port-forwards or
delete other clusters. Stop your own forwards in their terminals. Save anything
you need first. Relabeling an existing cluster from dev to prod is not an AWS
migration: infrastructure, secrets, storage, network, and image prerequisites
must be prepared separately.
