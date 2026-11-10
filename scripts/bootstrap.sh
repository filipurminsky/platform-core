#!/usr/bin/env bash
# bootstrap.sh — one-command local or AWS cluster setup
# Usage:
#   ./scripts/bootstrap.sh --mode=local   # kind cluster, zero AWS cost
#   ./scripts/bootstrap.sh --mode=aws     # AWS EKS (terraform must already be applied)

set -Eeuo pipefail
cd "$(dirname "$0")/.."

# ─── Defaults ────────────────────────────────────────────────────────────────
MODE="local"
ARGOCD_NAMESPACE="argocd"
CLUSTER_NAME="platform-core"
KIND_CONFIG="terraform/modules/kind/kind-config.yaml"
ENVIRONMENT=""   # derived from MODE below: local→dev, aws→prod
CHECK_ONLY=false
BUILD_IMAGES=true
WAIT_FOR_DEV=false
REQUIRE_PUBLISHED_SOURCE=false
GATEWAY_URL=http://localhost
WAIT_TIMEOUT=2400
INSTALL_TIMEOUT=600
IMAGE_CACHE=""
IMAGE_CACHE_REPORT=""
PHASE=preflight

# ─── Parse args ──────────────────────────────────────────────────────────────
for arg in "$@"; do
  case $arg in
    --mode=*) MODE="${arg#*=}" ;;
    --cluster-name=*) CLUSTER_NAME="${arg#*=}" ;;
    --kind-config=*) KIND_CONFIG="${arg#*=}" ;;
    --check) CHECK_ONLY=true ;;
    --skip-images) BUILD_IMAGES=false ;;
    --require-published-source) REQUIRE_PUBLISHED_SOURCE=true ;;
    --wait) WAIT_FOR_DEV=true ;;
    --gateway-url=*) GATEWAY_URL="${arg#*=}" ;;
    --timeout=*) WAIT_TIMEOUT="${arg#*=}" ;;
    --install-timeout=*) INSTALL_TIMEOUT="${arg#*=}" ;;
    --image-cache=*) IMAGE_CACHE="${arg#*=}" ;;
    --image-cache-report=*) IMAGE_CACHE_REPORT="${arg#*=}" ;;
    --help) echo "Usage: $0 [--mode=local|--mode=aws] [--check] [--cluster-name=NAME] [--kind-config=PATH] [--skip-images] [--require-published-source] [--wait] [--gateway-url=http://localhost] [--timeout=2400] [--install-timeout=600] [--image-cache=DIR] [--image-cache-report=PATH] (timeouts in seconds)"; exit 0 ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

# Map deployment mode → ArgoCD overlay environment.
case "$MODE" in
  local) ENVIRONMENT="dev"  ;;
  aws)   ENVIRONMENT="prod" ;;
  *)     echo "Unknown mode: $MODE (expected local|aws)"; exit 1 ;;
esac
if [[ "$WAIT_FOR_DEV" == true && "$MODE" != local ]]; then
  echo '--wait is a local DEV acceptance check; do not use it for AWS.' >&2
  exit 2
fi
[[ "$CLUSTER_NAME" =~ ^[a-z0-9][a-z0-9.-]*$ ]] || { echo 'Invalid kind cluster name.' >&2; exit 2; }
[[ "$WAIT_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || { echo '--timeout must be positive seconds.' >&2; exit 2; }
[[ "$INSTALL_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || { echo '--install-timeout must be positive seconds.' >&2; exit 2; }
[[ "$GATEWAY_URL" =~ ^https?:// ]] || { echo '--gateway-url must use http:// or https://.' >&2; exit 2; }
if [[ -n "$IMAGE_CACHE" && "$MODE" != local ]]; then
  echo '--image-cache is supported only for local kind clusters.' >&2
  exit 2
fi
if [[ -n "$IMAGE_CACHE_REPORT" && -z "$IMAGE_CACHE" ]]; then
  echo '--image-cache-report requires --image-cache.' >&2
  exit 2
fi

log()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()   { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
err()  { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }

failure() {
  local status=$?
  local installation_namespace="" context_hint=""
  trap - ERR
  echo "Bootstrap failed during: $PHASE (exit $status)." >&2
  case "$PHASE" in
    'Cilium installation') installation_namespace=kube-system ;;
    'Argo CD installation') installation_namespace="$ARGOCD_NAMESPACE" ;;
  esac
  if [[ -n "$installation_namespace" ]]; then
    [[ "$MODE" != local ]] || context_hint="--context kind-$CLUSTER_NAME "
    echo "Helm readiness budget: ${INSTALL_TIMEOUT}s. Inspect installation progress:" >&2
    echo "  kubectl ${context_hint}-n $installation_namespace get pods" >&2
    echo "  kubectl ${context_hint}-n $installation_namespace get events --sort-by=.metadata.creationTimestamp" >&2
    echo 'If images are still downloading successfully, rerun with a larger --install-timeout (seconds). Fix pull errors or scheduling failures before retrying.' >&2
  fi
  echo 'Run ./scripts/doctor.sh, then inspect docs/local-dev.md. Bootstrap can be rerun after fixing the cause.' >&2
  # Do not print BASH_COMMAND: secret creation commands contain credentials.
  exit "$status"
}
trap failure ERR

# ─── Dependency checks ───────────────────────────────────────────────────────
./scripts/doctor.sh --mode="$MODE" --preflight
if [[ -n "$IMAGE_CACHE" ]]; then
  CACHE_SOURCE_REVISION=$(git rev-parse HEAD)
  python3 scripts/dev-image-cache.py validate --cache-dir="$IMAGE_CACHE" \
    --source-revision="$CACHE_SOURCE_REVISION" --kind-config="$KIND_CONFIG"
fi
if [[ "$REQUIRE_PUBLISHED_SOURCE" == true ]]; then
  python3 scripts/verify-dev-source.py
else
  log 'GitOps reads published main, not local edits. Use --require-published-source for a reproducible walkthrough.'
fi
if [[ "$CHECK_ONLY" == true ]]; then
  ok "Preflight passed; no cluster resources changed."
  exit 0
fi

# ─── Local: provision kind cluster ───────────────────────────────────────────
if [[ "$MODE" == "local" ]]; then
  PHASE='kind creation and Cilium installation'
  log "Creating kind cluster: $CLUSTER_NAME"
  if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    log "Cluster already exists, skipping creation"
    # A pinned config does not upgrade existing nodes or replace their CNI.
    # Refuse incompatible reuse before installing anything into that cluster.
    kubectl --context "kind-${CLUSTER_NAME}" get nodes -o json | python3 -c '
import json, sys
nodes = json.load(sys.stdin)["items"]
if not nodes or any(not node["status"]["nodeInfo"]["kubeletVersion"].startswith("v1.36.") for node in nodes):
    sys.exit("Existing cluster is not Kubernetes 1.36. Use a fresh cluster name/config; changing the node pin does not upgrade existing nodes.")
'
    if kubectl --context "kind-${CLUSTER_NAME}" -n kube-system get daemonset kindnet >/dev/null 2>&1; then
      err "Existing cluster uses kindnet. Create a fresh cluster with disableDefaultCNI before installing Cilium."
    fi
  else
    # No --wait: the kind-config disables the default CNI, so nodes stay
    # NotReady until Cilium is installed below.
    kind create cluster \
      --name "$CLUSTER_NAME" \
      --config "$KIND_CONFIG"
  fi
  ok "kind cluster created"
  # A rerun can start with another cluster selected in kubeconfig.
  kubectl config use-context "kind-${CLUSTER_NAME}"

  # Restore only explicitly requested, revision-bound OCI images. Never restore
  # node disks or workload state, and keep local app builds below independent.
  if [[ -n "$IMAGE_CACHE" ]]; then
    PHASE='verified platform image cache import'
    cache_arguments=(import --cluster="$CLUSTER_NAME" --cache-dir="$IMAGE_CACHE"
      --source-revision="$CACHE_SOURCE_REVISION")
    [[ -z "$IMAGE_CACHE_REPORT" ]] || cache_arguments+=(--report="$IMAGE_CACHE_REPORT")
    python3 scripts/dev-image-cache.py "${cache_arguments[@]}"
  fi

  # ─── Cilium + Hubble (eBPF dataplane, local-first) ─────────────────────────
  # Installed here, before ArgoCD, because the cluster has no CNI yet and nothing
  # can schedule until pods get networking. EKS keeps the AWS VPC CNI (a
  # deliberate low-risk choice); Cilium is the local networking + flow-visibility
  # demo. ipam.mode=kubernetes makes Cilium use kind's per-node PodCIDR.
  # cni.chainingMode=portmap chains the portmap plugin (shipped in kindest/node)
  # after Cilium so pod hostPorts work without kube-proxy replacement — the
  # Envoy Gateway proxy binds hostPort 80/443 on the ingress-ready node, which
  # is how kind's host :80/:443 mapping reaches the Gateway.
  PHASE='Cilium installation'
  log "Installing Cilium + Hubble (readiness budget ${INSTALL_TIMEOUT}s)"
  # Cilium only supports consecutive-minor upgrades with preflight checks.
  # Bootstrap installs new clusters or reconciles this minor; it must not jump
  # an existing 1.16 installation directly to 1.20.
  CILIUM_INSTALLED_VERSION=$(helm list --namespace kube-system --filter '^cilium$' --no-headers | awk '{print $NF}')
  if [[ -n "$CILIUM_INSTALLED_VERSION" && "$CILIUM_INSTALLED_VERSION" != 1.20.* ]]; then
    err "Cilium ${CILIUM_INSTALLED_VERSION} needs a staged upgrade before bootstrap can install 1.20.4. See docs/tool-upgrades.md (existing local clusters)."
  fi
  helm repo add cilium https://helm.cilium.io --force-update
  helm upgrade --install cilium cilium/cilium \
    --namespace kube-system \
    --version "1.20.4" \
    --set ipam.mode=kubernetes \
    --set kubeProxyReplacement=false \
    --set cni.chainingMode=portmap \
    --set image.pullPolicy=IfNotPresent \
    --set operator.replicas=1 \
    --set hubble.enabled=true \
    --set hubble.relay.enabled=true \
    --set hubble.ui.enabled=true \
    --wait --timeout "${INSTALL_TIMEOUT}s"
  log "Waiting for nodes to become Ready (Cilium now provides the CNI)"
  kubectl wait --for=condition=Ready nodes --all --timeout=180s
  ok "Cilium + Hubble ready"

  # The dev overlays use these exact local image names. Build/load before Argo
  # creates workloads; make images deliberately uses different development tags.
  PHASE='local application images'
  if [[ "$BUILD_IMAGES" == true ]]; then
    ./scripts/dev-images.sh --cluster-name="$CLUSTER_NAME"
  else
    log 'Skipping local images. All seven dev image tags must already be loaded into this kind cluster.'
  fi

elif [[ "$MODE" == "aws" ]]; then
  PHASE='EKS connectivity'
  log "Updating kubeconfig for EKS cluster"
  AWS_REGION="${AWS_REGION:-eu-west-1}"
  aws eks update-kubeconfig \
    --name "platform-core-${ENVIRONMENT}" \
    --region "$AWS_REGION"
  ok "kubeconfig updated"
fi

# ─── Install ArgoCD ──────────────────────────────────────────────────────────
log "Installing ArgoCD (readiness budget ${INSTALL_TIMEOUT}s)"
PHASE='Argo CD installation'
kubectl create namespace "$ARGOCD_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

helm repo add argo https://argoproj.github.io/argo-helm --force-update
helm upgrade --install argocd argo/argo-cd \
  --namespace "$ARGOCD_NAMESPACE" \
  --version "10.17.1" \
  --values kubernetes/bootstrap/argocd/values.yaml \
  --wait --timeout "${INSTALL_TIMEOUT}s"

ok "ArgoCD installed"

# ─── Label the destination cluster with its environment ──────────────────────
# The app ApplicationSets use a cluster generator that reads this `environment`
# label to pick the dev vs prod overlay (services/<svc>/k8s/overlays/<env>/...). Declaring
# a secret for the in-cluster server lets us attach that label to the local
# cluster. Without it the generator matches nothing and no apps are created.
log "Labelling in-cluster as environment=$ENVIRONMENT"
PHASE='cluster environment and credentials'
kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: in-cluster
  namespace: $ARGOCD_NAMESPACE
  labels:
    argocd.argoproj.io/secret-type: cluster
    environment: $ENVIRONMENT
stringData:
  name: in-cluster
  server: https://kubernetes.default.svc
  config: '{"tlsClientConfig":{"insecure":false}}'
EOF
ok "Cluster labelled environment=$ENVIRONMENT"

# Redis authentication is required in every environment. Local mode has no
# external secret store, so create one random password in both the server and
# client namespaces before ArgoCD starts the workloads. Prod uses ESO instead
# (same for the object-storage credentials and the llm-gateway API keys below).
if [[ "$MODE" == "local" ]]; then
  log "Creating local Redis authentication Secrets"
  if kubectl get secret redis-auth -n platform >/dev/null 2>&1; then
    REDIS_PASSWORD=$(kubectl get secret redis-auth -n platform -o jsonpath='{.data.password}' | base64 -d)
    [[ -n "$REDIS_PASSWORD" ]] || err "Existing redis-auth has no password; repair it before bootstrap."
  else
    REDIS_PASSWORD=$(openssl rand -hex 32)
  fi
  for namespace in platform apps; do
    kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f -
    kubectl create secret generic redis-auth \
      --namespace "$namespace" \
      --from-literal=password="$REDIS_PASSWORD" \
      --dry-run=client -o yaml | kubectl apply -f -
  done
  unset REDIS_PASSWORD
  ok "Local Redis authentication configured"

  # Object-storage credentials — a single generated Secret shared by the dev
  # S3 store (SeaweedFS, kubernetes/platform/seaweedfs — it turns rootUser/
  # rootPassword into its admin identity) and the four audio pipeline services.
  # The Secret keeps its historical name `minio-credentials` and keys; don't rename.
  # This exercises the same secretKeyRef plumbing prod uses (IRSA replaces the
  # secret in prod, but the code path is identical).
  log "Creating local object-storage credentials Secret"
  MINIO_USER="minio"
  if kubectl get secret minio-credentials -n platform >/dev/null 2>&1; then
    MINIO_USER=$(kubectl get secret minio-credentials -n platform -o jsonpath='{.data.rootUser}' | base64 -d)
    MINIO_PASSWORD=$(kubectl get secret minio-credentials -n platform -o jsonpath='{.data.rootPassword}' | base64 -d)
    [[ -n "$MINIO_USER" && -n "$MINIO_PASSWORD" ]] || err 'Existing minio-credentials is incomplete; repair it before bootstrap.'
  else
    MINIO_PASSWORD=$(openssl rand -hex 32)
  fi
  for namespace in platform apps; do
    kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f -
    kubectl create secret generic minio-credentials \
      --namespace "$namespace" \
      --from-literal=rootUser="$MINIO_USER" \
      --from-literal=rootPassword="$MINIO_PASSWORD" \
      --dry-run=client -o yaml | kubectl apply -f -
  done
  unset MINIO_USER MINIO_PASSWORD
  ok "Local object-storage credentials configured"

  # llm-gateway bearer API keys. Same shape as the prod ExternalSecret
  # (services/llm-gateway/k8s/overlays/prod/externalsecret.yaml):
  #   api-keys   — CSV of every key the gateway accepts (GATEWAY_API_KEYS)
  #   llm-worker — the key llm-worker presents (also in api-keys)
  # Idempotent: an existing Secret's keys are reused, so re-running bootstrap
  # never rotates keys out from under running pods.
  log "Creating local llm-gateway API keys Secret"
  kubectl create namespace apps --dry-run=client -o yaml | kubectl apply -f -
  if kubectl get secret llm-gateway-api-keys -n apps >/dev/null 2>&1; then
    LLM_GATEWAY_API_KEYS=$(kubectl get secret llm-gateway-api-keys -n apps -o jsonpath='{.data.api-keys}' | base64 -d)
    LLM_WORKER_API_KEY=$(kubectl get secret llm-gateway-api-keys -n apps -o jsonpath='{.data.llm-worker}' | base64 -d)
    [[ -n "$LLM_GATEWAY_API_KEYS" && -n "$LLM_WORKER_API_KEY" ]] || err "Existing llm-gateway-api-keys is incomplete; repair it before bootstrap."
  else
    LLM_WORKER_API_KEY=$(openssl rand -hex 32)
    LLM_GATEWAY_API_KEYS="$(openssl rand -hex 32),${LLM_WORKER_API_KEY}"
  fi
  case ",${LLM_GATEWAY_API_KEYS}," in
    *",${LLM_WORKER_API_KEY},"*) ;;
    *) err 'Existing llm-gateway-api-keys does not accept its llm-worker key; repair the Secret before bootstrap.' ;;
  esac
  kubectl create secret generic llm-gateway-api-keys \
    --namespace apps \
    --from-literal=api-keys="$LLM_GATEWAY_API_KEYS" \
    --from-literal=llm-worker="$LLM_WORKER_API_KEY" \
    --dry-run=client -o yaml | kubectl apply -f -
  unset LLM_GATEWAY_API_KEYS LLM_WORKER_API_KEY
  ok "Local llm-gateway API keys configured"
fi

# ─── Bootstrap App-of-Apps ───────────────────────────────────────────────────
# The AppProject must exist first — the echo-service Application references it.
log "Applying platform-apps AppProject"
PHASE='GitOps registration'
kubectl apply -f kubernetes/platform/argocd/appproject-apps.yaml
if [[ "$MODE" == local ]]; then
  # This project is bootstrap-owned. The production change-freeze must not
  # prevent an unattended local setup from converging between 00:00–06:00 UTC.
  kubectl -n "$ARGOCD_NAMESPACE" patch appproject platform-apps --type=merge \
    -p '{"spec":{"syncWindows":[]}}'
fi

log "Applying ArgoCD App-of-Apps (root)"
kubectl apply -f kubernetes/platform/argocd/app-of-apps.yaml
ok "App-of-Apps applied — ArgoCD will now sync platform services + apps"

# ─── Let ArgoCD converge ─────────────────────────────────────────────────────
# Platform components install their own CRDs (Gateway API/Envoy Gateway, Strimzi,
# KEDA, Rollouts, Kyverno, Prometheus). Explicit sync retries let dependent apps
# recover when those CRDs arrive late. Cold chart/image downloads can take tens
# of minutes; --wait checks convergence within its separate, bounded budget.
log "ArgoCD is reconciling. Watch progress with:"
echo "    kubectl get applications -n $ARGOCD_NAMESPACE -w"
echo "    kubectl argo rollouts get rollout echo-service -n apps --watch   # canary"
kubectl -n "$ARGOCD_NAMESPACE" rollout status deploy/argocd-server --timeout=180s
if [[ "$WAIT_FOR_DEV" == true ]]; then
  PHASE='DEV convergence and routed endpoints'
  python3 scripts/wait-for-dev.py --context="kind-$CLUSTER_NAME" \
    --gateway-url="$GATEWAY_URL" --timeout="$WAIT_TIMEOUT"
fi

# ─── Access info (local mode only) ───────────────────────────────────────────
if [[ "$MODE" == "local" ]]; then
  echo ""
  if [[ "$WAIT_FOR_DEV" == true ]]; then
    ok "DEV acceptance passed. ArgoCD continues reconciling published main."
  else
    ok "Bootstrap kicked off. ArgoCD will keep syncing in the background; run scripts/wait-for-dev.py to check convergence."
  fi
  echo ""
  echo 'Retrieve the initial Argo CD password when needed: make argocd-password'
  echo ""
  echo "Port-forward what you need (run in separate terminals), e.g.:"
  echo "  kubectl port-forward svc/argocd-server -n argocd     8080:80    # http://localhost:8080"
  echo "  kubectl port-forward svc/grafana       -n monitoring 3000:80    # http://localhost:3000  (admin / admin) — metrics, logs, traces, cost"
  echo "  kubectl port-forward svc/hubble-ui     -n kube-system 12000:80  # http://localhost:12000 — live Cilium network flows"
  echo "  kubectl port-forward svc/opencost      -n monitoring 9090:9090  # http://localhost:9090 — OpenCost UI (cost-model API on :9003)"
  echo ""
  echo "Apps reachable via the Envoy Gateway (Gateway API) on http://localhost (add to /etc/hosts):"
  echo "  127.0.0.1  echo.platform-core.local llm-gateway.platform-core.local audio-api.platform-core.local backstage.platform-core.local"
  echo "  curl -H 'Host: echo.platform-core.local' http://localhost/"
  echo "  open http://backstage.platform-core.local"
  echo "  kubectl get gateway platform -n envoy-gateway-system   # PROGRAMMED=True once Envoy is up"
  echo ""
  echo "llm-gateway requires a bearer key (/v1/*):"
  echo "  KEY=\$(kubectl get secret llm-gateway-api-keys -n apps -o jsonpath='{.data.llm-worker}' | base64 -d)"
  echo "  curl -H 'Host: llm-gateway.platform-core.local' -H \"Authorization: Bearer \$KEY\" http://localhost/v1/models"
  echo ""
  echo "Watch the canary + KEDA scaling demos:"
  echo "  kubectl argo rollouts get rollout echo-service -n apps --watch"
  echo "  # The canary demo creates and removes its own load job."
  echo "  ./scripts/canary-demo.sh bad       # SLO-gated auto-rollback"
fi
