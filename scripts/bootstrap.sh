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
  log "Installing Cilium + Hubble"
  helm repo add cilium https://helm.cilium.io --force-update
  helm upgrade --install cilium cilium/cilium \
    --namespace kube-system \
    --version "1.16.5" \
    --set ipam.mode=kubernetes \
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
    --name "$CLUSTER_NAME" \
    --region "$AWS_REGION"
  ok "kubeconfig updated"
fi

# ─── Install ArgoCD ──────────────────────────────────────────────────────────
log "Installing ArgoCD"
kubectl create namespace "$ARGOCD_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

helm repo add argo https://argoproj.github.io/argo-helm --force-update
helm upgrade --install argocd argo/argo-cd \
  --namespace "$ARGOCD_NAMESPACE" \
  --version "9.5.17" \
  --values kubernetes/bootstrap/argocd/values.yaml \
  --wait --timeout 5m

ok "ArgoCD installed"

# ─── Label the destination cluster with its environment ──────────────────────
# The app ApplicationSets use a cluster generator that reads this `environment`
# label to pick the dev vs prod overlay (kustomize/overlays/<env>/...). Declaring
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

# ─── Bootstrap App-of-Apps ───────────────────────────────────────────────────
# The AppProject must exist first — the echo-service Application references it.
log "Applying platform-apps AppProject"
kubectl apply -f kubernetes/platform/argocd/appproject-apps.yaml

log "Applying ArgoCD App-of-Apps (root)"
kubectl apply -f kubernetes/platform/argocd/app-of-apps.yaml
ok "App-of-Apps applied — ArgoCD will now sync platform services + apps"

# ─── Let ArgoCD converge ─────────────────────────────────────────────────────
# Platform components install their own CRDs (Strimzi, KEDA, Rollouts, Kyverno,
# Prometheus) via sync-waves and ArgoCD self-heals until they're ready. Rather
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
  echo "Apps reachable via the ingress on http://localhost (add to /etc/hosts):"
  echo "  127.0.0.1  echo.platform-core.local"
  echo "  127.0.0.1  backstage.platform-core.local"
  echo "  curl -H 'Host: echo.platform-core.local' http://localhost/"
  echo "  open http://backstage.platform-core.local"
  echo ""
  echo "Watch the canary + KEDA scaling demos:"
  echo "  kubectl argo rollouts get rollout echo-service -n apps --watch"
  echo "  kubectl apply -k tests/load        # generate load"
  echo "  ./scripts/canary-demo.sh bad       # SLO-gated auto-rollback"
fi
