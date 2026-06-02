# Local development

Two ways to run platform-core locally, depending on what you're working on.

---

## Option A — App dev loop (fastest, no cluster)

For iterating on the three services (`echo-service`, `worker-service`, `llm-gateway`).
Uses Docker Compose: Kafka (KRaft), the apps, and a vLLM mock.

```bash
cd services
docker compose up -d
docker compose logs -f worker-service

# produce a job and watch the worker process it
docker compose exec kafka kafka-console-producer.sh \
  --bootstrap-server localhost:9092 --topic jobs <<< \
  '{"id":"test-1","type":"data-transform","payload":{"input":"hello","operation":"uppercase"}}'

# hit the gateway
curl http://localhost:8080/v1/models
```

No Kubernetes needed. This is the right loop for editing `services/*/main.py`.

---

## Option B — Full platform on kind (the real thing)

Brings up the whole GitOps platform on a local kind cluster: ArgoCD, Argo Rollouts,
KEDA, Kyverno, Strimzi+Kafka, kube-prometheus-stack, ingress-nginx, and the demo apps.

### Prerequisites

```bash
# macOS (brew)
brew install kind helm kubectl
# Argo Rollouts kubectl plugin (for the canary demo)
brew install argoproj/tap/kubectl-argo-rollouts
```

Give Docker Desktop **≥ 8 GB RAM** — the full stack (Kafka + Prometheus + operators)
is heavy. If constrained, see "Trimming the stack" below.

### Bring it up

```bash
./scripts/bootstrap.sh --mode=local
```

This:
1. Creates the kind cluster (`terraform/modules/kind/kind-config.yaml`, with host
   ports 80/443 for ingress).
2. Installs ArgoCD via Helm (`kubernetes/bootstrap/argocd/values.yaml`).
3. Applies the `platform-apps` AppProject and the root **app-of-apps**, which then
   syncs every platform service and the demo apps from Git.

First sync pulls several Helm charts — **give it a few minutes**. Watch it:

```bash
kubectl get applications -n argocd -w
```

### Access

```bash
kubectl port-forward svc/argocd-server -n argocd     8080:80    # http://localhost:8080
kubectl port-forward svc/grafana       -n monitoring 3000:80    # http://localhost:3000 (admin/admin)
# ArgoCD admin password:
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
```

Apps are served through ingress-nginx on `http://localhost`. Add to `/etc/hosts`:

```
127.0.0.1  echo.platform-core.local
127.0.0.1  backstage.platform-core.local
```
```bash
curl -H 'Host: echo.platform-core.local' http://localhost/
open http://backstage.platform-core.local
```

### Demos

```bash
# SLO-gated canary + auto-rollback
kubectl apply -k tests/load                                   # generate traffic
./scripts/canary-demo.sh bad                                  # deploy broken image
kubectl argo rollouts get rollout echo-service -n apps --watch

# Kafka consumer-lag scale-to-zero (worker-service 0 → N → 0)
kubectl -n platform run kafka-producer --rm -it --restart=Never \
  --image=quay.io/strimzi/kafka:0.40.0-kafka-3.7.0 -- \
  bin/kafka-console-producer.sh --bootstrap-server platform-kafka-kafka-bootstrap:9092 \
  --topic jobs
kubectl get scaledobject,deploy/worker-service -n apps -w
```

### Tear down

```bash
./scripts/teardown.sh --mode=local
```

---

## Trimming the stack (low-RAM laptops)

The platform is modular — drop the heavy pieces and use Option A for app dev:

```bash
# after bootstrap, remove what you don't need
kubectl delete application kafka strimzi-operator -n argocd   # ~2-3 GB
kubectl delete application backstage -n argocd                # developer portal
kubectl delete application vllm-inference -n argocd           # model download is large
```

---

## Known caveats (honest)

- **Backstage** (`helm/backstage`) runs the published Backstage container with a
  local ConfigMap-backed catalog. It is enough for browsing the service catalog,
  but deeper plugin integrations should move into a custom Backstage app image.
- **vLLM** scales from zero and pulls a model on first request (TinyLlama on CPU in
  dev) — the first inference is slow and memory-hungry.
- **kafka** runs the dev overlay (1 broker, 1 ZK, no TLS). It still needs the
  Strimzi operator (synced as `strimzi-operator`) to come up first.
- The canary's traffic split needs ingress-nginx healthy on kind; if the host
  ports aren't reachable, you can still watch the rollout progress via the
  `kubectl argo rollouts` plugin (analysis runs against in-cluster Prometheus).
- `dev`/`prod` overlay selection is driven by the `environment` label on the
  `in-cluster` ArgoCD cluster secret (set by `bootstrap.sh`: `dev` for `--mode=local`,
  `prod` for `--mode=aws`). The app/kafka ApplicationSets template the overlay path
  from that label, so flipping environments is a relabel, not a manifest edit.
