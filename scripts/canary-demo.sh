#!/usr/bin/env bash
# canary-demo.sh — demonstrate SLO-gated progressive delivery end to end.
#
# Prereqs (local kind): bootstrap.sh has run; argo-rollouts, ingress-nginx,
# kube-prometheus-stack and echo-service are synced; `kubectl argo rollouts`
# plugin installed (https://argo-rollouts.readthedocs.io/en/stable/installation/#kubectl-plugin-installation).
#
# Usage:
#   ./scripts/canary-demo.sh good   # deploy a healthy image → canary promotes
#   ./scripts/canary-demo.sh bad    # deploy a broken image → analysis aborts + auto-rollback
set -euo pipefail

NS="${NS:-apps}"
ROLLOUT="${ROLLOUT:-echo-service}"
MODE="${1:-good}"

echo "==> Starting background load so the canary analysis has traffic to measure"
kubectl apply -k tests/load

case "$MODE" in
  good)
    NEW_IMAGE="${GOOD_IMAGE:-echo-service:v2}"
    echo "==> Promoting a healthy image: $NEW_IMAGE"
    ;;
  bad)
    # An image that fails readiness / returns 5xx — the SLO query will drop below
    # 99% and Argo Rollouts will abort and revert to the stable ReplicaSet.
    NEW_IMAGE="${BAD_IMAGE:-echo-service:broken}"
    echo "==> Deploying a deliberately broken image: $NEW_IMAGE"
    ;;
  *)
    echo "usage: $0 [good|bad]" >&2; exit 1 ;;
esac

kubectl -n "$NS" argo rollouts set image "$ROLLOUT" "echo-service=$NEW_IMAGE"

echo "==> Watching the rollout (Ctrl-C to detach; the rollout continues)"
kubectl -n "$NS" argo rollouts get rollout "$ROLLOUT" --watch
