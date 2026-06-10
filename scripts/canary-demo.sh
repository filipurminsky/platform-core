#!/usr/bin/env bash
# canary-demo.sh — SLO-gated progressive delivery: scripted and verified.
#
# Deploys a new image, polls until Argo Rollouts reaches a terminal phase
# (Healthy or Degraded), then asserts the expected outcome. Works as a
# live demo or as a scriptable smoke test.
#
# Prereqs (local kind): bootstrap.sh has run; argo-rollouts, ingress-nginx,
# kube-prometheus-stack and echo-service are synced; `kubectl argo rollouts`
# plugin installed.
#
# Usage:
#   ./scripts/canary-demo.sh good   # healthy image → promotes to Healthy
#   ./scripts/canary-demo.sh bad    # broken image → SLO abort → Degraded
set -euo pipefail

NS="${NS:-apps}"
ROLLOUT="${ROLLOUT:-echo-service}"
MODE="${1:-good}"

# ── Timeouts ──────────────────────────────────────────────────────────────────
# bad:  abort fires after first failing analysis interval (~50s); 180s is generous
# good: two analysis rounds × (30s pause + 4×20s) ≈ 4 min; 480s covers slow clusters
TIMEOUT_BAD=180
TIMEOUT_GOOD=480

# ── Helpers ───────────────────────────────────────────────────────────────────
log()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()   { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
err()  { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }

cleanup() {
  log "Removing k6 load job..."
  kubectl delete -k tests/load --ignore-not-found 2>/dev/null || true
}
trap cleanup EXIT

# ── Start background load (analysis needs traffic to measure) ─────────────────
log "Applying k6 load job..."
kubectl apply -k tests/load

# ── Select image and expected outcome ─────────────────────────────────────────
case "$MODE" in
  good)
    NEW_IMAGE="${GOOD_IMAGE:-echo-service:v2}"
    EXPECTED_PHASE="Healthy"
    TIMEOUT=$TIMEOUT_GOOD
    log "Deploying healthy image: $NEW_IMAGE"
    log "  → expect canary to promote through 10%→50%→100% and reach Healthy"
    ;;
  bad)
    NEW_IMAGE="${BAD_IMAGE:-echo-service:broken}"
    EXPECTED_PHASE="Degraded"
    TIMEOUT=$TIMEOUT_BAD
    log "Deploying deliberately broken image: $NEW_IMAGE"
    log "  → expect SLO analysis to detect failures, abort, and revert to stable"
    ;;
  *)
    echo "usage: $0 [good|bad]" >&2; exit 1 ;;
esac

# ── Capture stable image before the rollout starts ────────────────────────────
STABLE_IMAGE=$(kubectl -n "$NS" get rollout "$ROLLOUT" \
  -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo "unknown")

kubectl -n "$NS" argo rollouts set image "$ROLLOUT" "echo-service=$NEW_IMAGE"
log "Image set to $NEW_IMAGE — rollout in progress"

# ── Poll until terminal phase ─────────────────────────────────────────────────
log "Polling rollout phase every 5s (timeout: ${TIMEOUT}s)..."
START=$SECONDS
LAST_PHASE=""

while [ $((SECONDS - START)) -lt $TIMEOUT ]; do
  PHASE=$(kubectl -n "$NS" get rollout "$ROLLOUT" \
    -o jsonpath='{.status.phase}' 2>/dev/null || echo "")

  if [[ "$PHASE" != "$LAST_PHASE" ]]; then
    log "Phase: $LAST_PHASE → $PHASE  (+$((SECONDS - START))s)"
    LAST_PHASE="$PHASE"
  fi

  if [[ "$PHASE" == "Healthy" || "$PHASE" == "Degraded" ]]; then
    break
  fi
  sleep 5
done

# ── Print current rollout state ────────────────────────────────────────────────
echo ""
kubectl -n "$NS" argo rollouts get rollout "$ROLLOUT" 2>/dev/null || true
echo ""

# ── Assert expected outcome ────────────────────────────────────────────────────
FINAL_PHASE=$(kubectl -n "$NS" get rollout "$ROLLOUT" \
  -o jsonpath='{.status.phase}' 2>/dev/null || echo "")

if [[ "$FINAL_PHASE" != "$EXPECTED_PHASE" ]]; then
  # Dump the most recent AnalysisRun for diagnostics
  LATEST_RUN=$(kubectl -n "$NS" get analysisrun \
    -l rollouts.argoproj.io/rollout="$ROLLOUT" \
    --sort-by='.metadata.creationTimestamp' \
    -o name 2>/dev/null | tail -1 || true)
  if [[ -n "$LATEST_RUN" ]]; then
    log "Most recent AnalysisRun ($LATEST_RUN):"
    kubectl -n "$NS" get "$LATEST_RUN" \
      -o jsonpath='  phase={.status.phase}  message={.status.message}' 2>/dev/null || true
    echo ""
  fi
  err "Expected phase '$EXPECTED_PHASE' but rollout is '$FINAL_PHASE' after ${TIMEOUT}s."
fi

ok "Rollout reached phase '$FINAL_PHASE' as expected (+$((SECONDS - START))s)"

if [[ "$MODE" == "bad" ]]; then
  # Show which AnalysisRun triggered the abort and why
  LATEST_RUN=$(kubectl -n "$NS" get analysisrun \
    -l rollouts.argoproj.io/rollout="$ROLLOUT" \
    --sort-by='.metadata.creationTimestamp' \
    -o name 2>/dev/null | tail -1 || true)
  if [[ -n "$LATEST_RUN" ]]; then
    ABORT_MSG=$(kubectl -n "$NS" get "$LATEST_RUN" \
      -o jsonpath='{.status.message}' 2>/dev/null || echo "n/a")
    ok "AnalysisRun: $LATEST_RUN — $ABORT_MSG"
  fi
  ok "Auto-rollback confirmed: pods reverted to stable image ($STABLE_IMAGE)"
fi

if [[ "$MODE" == "good" ]]; then
  ok "Canary promoted to 100% — new stable image: $NEW_IMAGE"
fi
