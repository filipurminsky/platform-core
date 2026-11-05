#!/usr/bin/env bash
# bootstrap.sh — one-command local or AWS cluster setup
# Usage:
#   ./scripts/bootstrap.sh --mode=local   # kind cluster, zero AWS cost
#   ./scripts/bootstrap.sh --mode=aws     # AWS EKS (terraform must already be applied)

set -euo pipefail

# ─── Defaults ────────────────────────────────────────────────────────────────
MODE="local"
ARGOCD_NAMESPACE="argocd"
PLATFORM_NAMESPACE="platform"
CLUSTER_NAME="platform-core"
ENVIRONMENT=""   # derived from MODE below: local→dev, aws→prod

# ─── Parse args ──────────────────────────────────────────────────────────────
for arg in "$@"; do
  case $arg in
    --mode=*) MODE="${arg#*=}" ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

# Map deployment mode → ArgoCD overlay environment.
case "$MODE" in
  local) ENVIRONMENT="dev"  ;;
  aws)   ENVIRONMENT="prod" ;;
  *)     echo "Unknown mode: $MODE (expected local|aws)"; exit 1 ;;
esac

log()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()   { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
err()  { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }

require() {
  command -v "$1" >/dev/null 2>&1 || err "Required tool not found: $1 — install it first"
}

# ─── Dependency checks ───────────────────────────────────────────────────────
require kubectl
require helm
[[ "$MODE" == "local" ]] && require kind
[[ "$MODE" == "aws"   ]] && require aws

# ─── Local: provision kind cluster ───────────────────────────────────────────
if [[ "$MODE" == "local" ]]; then
  log "Creating kind cluster: $CLUSTER_NAME"
  if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    log "Cluster already exists, skipping creation"
  else
    # No --wait: the kind-config disables the default CNI, so nodes stay
    # NotReady until Cilium is installed below.
    kind create cluster \
      --name "$CLUSTER_NAME" \
      --config terraform/modules/kind/kind-config.yaml
  fi
  ok "kind cluster created"

  # ─── Cilium + Hubble (eBPF dataplane, local-first) ─────────────────────────
  # Installed here, before ArgoCD, because the cluster has no CNI yet and nothing
  # can schedule until pods get networking. EKS keeps the AWS VPC CNI (a
  # deliberate low-risk choice); Cilium is the local networking + flow-visibility
  # demo. ipam.mode=kubernetes makes Cilium use kind's per-node PodCIDR.
  # cni.chainingMode=portmap chains the portmap plugin (shipped in kindest/node)
  # after Cilium so pod hostPorts work without kube-proxy replacement — the
  # Envoy Gateway proxy binds hostPort 80/443 on the ingress-ready node, which
  # is how kind's host :80/:443 mapping reaches the Gateway.
  log "Installing Cilium + Hubble"
  helm repo add cilium https://helm.cilium.io --force-update
  helm upgrade --install cilium cilium/cilium \
    --namespace kube-system \
    --version "1.16.5" \
    --set ipam.mode=kubernetes \
    --set cni.chainingMode=portmap \
    --set image.pullPolicy=IfNotPresent \
    --set operator.replicas=1 \
    --set hubble.enabled=true \
    --set hubble.relay.enabled=true \
    --set hubble.ui.enabled=true \
    --wait --timeout 5m
  log "Waiting for nodes to become Ready (Cilium now provides the CNI)"
  kubectl wait --for=condition=Ready nodes --all --timeout=180s
  ok "Cilium + Hubble ready"

elif [[ "$MODE" == "aws" ]]; then
  log "Updating kubeconfig for EKS cluster"
  AWS_REGION="${AWS_REGION:-eu-west-1}"
  aws eks update-kubeconfig \
    --name "platform-core-${ENVIRONMENT}" \
    --region "$AWS_REGION"
  ok "kubeconfig updated"
fi

# ─── Install ArgoCD ──────────────────────────────────────────────────────────
log "Installing ArgoCD"
kubectl create namespace "$ARGOCD_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

helm repo add argo https://argoproj.github.io/argo-helm --force-update
helm upgrade --install argocd argo/argo-cd \
  --namespace "$ARGOCD_NAMESPACE" \
  --version "10.14.0" \
  --values kubernetes/bootstrap/argocd/values.yaml \
  --wait --timeout 5m

ok "ArgoCD installed"

# ─── Label the destination cluster with its environment ──────────────────────
# The app ApplicationSets use a cluster generator that reads this `environment`
# label to pick the dev vs prod overlay (services/<svc>/k8s/overlays/<env>/...). Declaring
# a secret for the in-cluster server lets us attach that label to the local
# cluster. Without it the generator matches nothing and no apps are created.
log "Labelling in-cluster as environment=$ENVIRONMENT"
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
  MINIO_PASSWORD="minio123"  # static for local kind — not a security concern
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
  fi
  if [[ -z "${LLM_GATEWAY_API_KEYS:-}" || -z "${LLM_WORKER_API_KEY:-}" ]]; then
    LLM_WORKER_API_KEY=$(openssl rand -hex 32)
    LLM_GATEWAY_API_KEYS="$(openssl rand -hex 32),${LLM_WORKER_API_KEY}"
  fi
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
kubectl apply -f kubernetes/platform/argocd/appproject-apps.yaml

log "Applying ArgoCD App-of-Apps (root)"
kubectl apply -f kubernetes/platform/argocd/app-of-apps.yaml
ok "App-of-Apps applied — ArgoCD will now sync platform services + apps"

# ─── Let ArgoCD converge ─────────────────────────────────────────────────────
# Platform components install their own CRDs (Gateway API/Envoy Gateway, Strimzi,
# KEDA, Rollouts, Kyverno, Prometheus) via sync-waves and ArgoCD self-heals until they're ready. Rather
# than a brittle label wait, surface progress and let it converge in the
# background — first sync pulls several Helm charts and can take a few minutes.
log "ArgoCD is reconciling. Watch progress with:"
echo "    kubectl get applications -n $ARGOCD_NAMESPACE -w"
echo "    kubectl argo rollouts get rollout echo-service -n apps --watch   # canary"
kubectl -n "$ARGOCD_NAMESPACE" rollout status deploy/argocd-server --timeout=180s 2>/dev/null || true

# ─── Access info (local mode only) ───────────────────────────────────────────
if [[ "$MODE" == "local" ]]; then
  echo ""
  ok "Bootstrap kicked off. ArgoCD will keep syncing in the background."
  echo ""
  echo "ArgoCD admin password:"
  kubectl -n argocd get secret argocd-initial-admin-secret \
    -o jsonpath="{.data.password}" 2>/dev/null | base64 -d && echo
  echo ""
  echo "Port-forward what you need (run in separate terminals), e.g.:"
  echo "  kubectl port-forward svc/argocd-server -n argocd     8080:80    # http://localhost:8080  (admin / above)"
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
  echo "  kubectl apply -k tests/load        # generate load"
  echo "  ./scripts/canary-demo.sh bad       # SLO-gated auto-rollback"
fi
