#!/usr/bin/env bash
# Dev-only fault exercise. Proves new analysis + routing + healthy stable responses.
set -euo pipefail
cd "$(dirname "$0")/.."
MODE=${1:-good}
case "$MODE" in good|bad) ;; *) echo "Usage: $0 good|bad" >&2; exit 2 ;; esac
for tool in kubectl python3 curl; do command -v "$tool" >/dev/null || { echo "Missing $tool" >&2; exit 1; }; done
NS=apps
ROLLOUT=echo-service
KUBE_CONTEXT="${KUBE_CONTEXT:-$(kubectl config current-context)}"
kubectl() { command kubectl --context "$KUBE_CONTEXT" --request-timeout=20s "$@"; }
rollouts() { command kubectl argo rollouts --context "$KUBE_CONTEXT" --request-timeout=20s -n "$NS" "$@"; }
[[ $(kubectl -n argocd get secret in-cluster -o jsonpath='{.metadata.labels.environment}') == dev ]] || { echo 'Canary demo requires a dev-labelled cluster.' >&2; exit 1; }
NEW_IMAGE=${GOOD_IMAGE:-echo-service:v2}
[[ "$MODE" == good ]] || NEW_IMAGE=${BAD_IMAGE:-echo-service:broken}
TIMEOUT=${CANARY_TIMEOUT:-600}
EVIDENCE_DIR=${EVIDENCE_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/platform-canary.XXXXXX")}
mkdir -p "$EVIDENCE_DIR"
# Let kubectl allocate a free port unless the caller needs a specific one.
PF_PORT=${CANARY_PORT:-0}
PF_PID=""
PAUSED=false
CHANGED=false
LOAD_CREATED=false
kubectl -n "$NS" get rollout "$ROLLOUT" -o json > "$EVIDENCE_DIR/baseline.json"
kubectl -n "$NS" get analysisruns -o json > "$EVIDENCE_DIR/previous-analysis.json"
kubectl -n argocd get application "$ROLLOUT" -o json > "$EVIDENCE_DIR/application.json"
STABLE_IMAGE=$(python3 scripts/canary-evidence.py baseline "$EVIDENCE_DIR" "$MODE" "$NEW_IMAGE")
OLD_SKIP=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("metadata", {}).get("annotations", {}).get("argocd.argoproj.io/skip-reconcile", ""))' "$EVIDENCE_DIR/application.json")
# Never take over a user's existing load exercise.
if [[ -n $(kubectl -n "$NS" get job k6-echo-load --ignore-not-found -o name) || -n $(kubectl -n "$NS" get configmap k6-echo-script --ignore-not-found -o name) ]]; then
  echo 'Existing tests/load resources found. Finish that exercise before starting this demo.' >&2
  exit 1
fi
cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "$PF_PID" ]]; then kill "$PF_PID" 2>/dev/null || true; wait "$PF_PID" 2>/dev/null || true; fi
  if [[ "$CHANGED" == true ]]; then
    current=$(kubectl -n "$NS" get rollout "$ROLLOUT" -o jsonpath='{.spec.template.spec.containers[0].image}') || current=''
    if [[ "$current" == "$NEW_IMAGE" ]]; then
      rollouts set image "$ROLLOUT" "echo-service=$STABLE_IMAGE" >/dev/null || status=1
      restored=false
      restore_start=$SECONDS
      while (( SECONDS - restore_start < 600 )); do
        if kubectl -n "$NS" get rollout "$ROLLOUT" -o json > "$EVIDENCE_DIR/restored.json" && \
          python3 scripts/canary-evidence.py restored "$EVIDENCE_DIR" "$MODE" "$NEW_IMAGE" 2> "$EVIDENCE_DIR/restore-pending.txt"; then
          restored=true
          break
        fi
        sleep 5
      done
      if [[ "$restored" != true ]]; then
        echo 'Original rollout did not recover within 600s; inspect restore-pending.txt and the cluster.' >&2
        status=1
      fi
    elif [[ "$current" != "$STABLE_IMAGE" ]]; then
      echo 'Rollout changed externally; refusing to overwrite it. Inspect its current image.' >&2
      status=1
    fi
  fi
  # Restoration after a good promotion can itself run analysis; retain traffic.
  if [[ "$LOAD_CREATED" == true ]]; then kubectl delete -k tests/canary-load --ignore-not-found >/dev/null || status=1; fi
  if [[ "$PAUSED" == true ]]; then
    if [[ -n "$OLD_SKIP" ]]; then
      kubectl -n argocd annotate application "$ROLLOUT" "argocd.argoproj.io/skip-reconcile=$OLD_SKIP" --overwrite >/dev/null || status=1
    else
      kubectl -n argocd annotate application "$ROLLOUT" argocd.argoproj.io/skip-reconcile- >/dev/null || status=1
    fi
  fi
  echo "Evidence: $EVIDENCE_DIR (original image and reconciliation setting restored if cleanup succeeded)"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
rollouts version --short >/dev/null
# Establish the Gateway connection before making any cluster changes. A port
# conflict must fail preflight, not interrupt verification after fault injection.
# Bypass the shell function: $! must identify kubectl itself, otherwise killing
# its background wrapper can orphan the forwarding process during cleanup.
command kubectl --context "$KUBE_CONTEXT" --request-timeout=20s -n envoy-gateway-system \
  port-forward svc/envoy-platform "$PF_PORT":80 > "$EVIDENCE_DIR/port-forward.log" 2>&1 &
PF_PID=$!
forward_ready=false
for attempt in {1..50}; do
  kill -0 "$PF_PID" 2>/dev/null || { cat "$EVIDENCE_DIR/port-forward.log" >&2; exit 1; }
  allocated_port=$(python3 -c 'import re,sys; match=re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", open(sys.argv[1]).read()); print(match[1] if match else "")' "$EVIDENCE_DIR/port-forward.log")
  if [[ -n "$allocated_port" ]]; then
    PF_PORT=$allocated_port
    forward_ready=true
    break
  fi
  sleep 0.2
done
[[ "$forward_ready" == true ]] || { cat "$EVIDENCE_DIR/port-forward.log" >&2; echo 'Gateway port-forward did not become ready.' >&2; exit 1; }
# Pausing only this dev Application prevents self-heal from undoing fault injection.
kubectl -n argocd annotate application "$ROLLOUT" argocd.argoproj.io/skip-reconcile=true --overwrite >/dev/null
PAUSED=true
LOAD_CREATED=true
kubectl apply -k tests/canary-load
CHANGED=true
rollouts set image "$ROLLOUT" "echo-service=$NEW_IMAGE"
START=$SECONDS
verified=false
while (( SECONDS - START < TIMEOUT )); do
  kubectl -n "$NS" get rollout "$ROLLOUT" -o json > "$EVIDENCE_DIR/rollout.json"
  kubectl -n "$NS" get analysisruns -o json > "$EVIDENCE_DIR/analysis.json"
  kubectl -n "$NS" get httproute "$ROLLOUT" -o json > "$EVIDENCE_DIR/route.json"
  kubectl -n "$NS" get service "$ROLLOUT" -o json > "$EVIDENCE_DIR/service.json"
  kubectl -n "$NS" get replicasets -l app=echo-service -o json > "$EVIDENCE_DIR/replicasets.json"
  kubectl -n "$NS" get pods -l app=echo-service -o json > "$EVIDENCE_DIR/pods.json"
  [[ $(kubectl -n argocd get application "$ROLLOUT" -o jsonpath='{.metadata.annotations.argocd\.argoproj\.io/skip-reconcile}') == true ]] || { echo 'Reconciliation resumed externally; stopping exercise.' >&2; exit 1; }
  if python3 scripts/canary-evidence.py verify "$EVIDENCE_DIR" "$MODE" "$NEW_IMAGE" 2> "$EVIDENCE_DIR/pending.txt"; then
    verified=true
    break
  fi
  sleep 5
done
if [[ "$verified" != true ]]; then
  cat "$EVIDENCE_DIR/pending.txt" >&2
  echo "Canary evidence incomplete after ${TIMEOUT}s; inspect retained JSON and controller logs." >&2
  exit 1
fi
# Verify actual successful requests THROUGH the Gateway, not port-forwarded stable Service.
for attempt in 1 2 3 4 5; do
  kill -0 "$PF_PID" 2>/dev/null || { cat "$EVIDENCE_DIR/port-forward.log" >&2; exit 1; }
  curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
    -H 'Host: echo.platform-core.local' "http://localhost:$PF_PORT/" > "$EVIDENCE_DIR/response-$attempt.json"
  python3 scripts/canary-evidence.py response "$EVIDENCE_DIR" "$MODE" "$NEW_IMAGE" "$attempt"
done
printf '{"scenario":"%s","elapsed_seconds":%s,"verified":true}\n' "$MODE" "$((SECONDS - START))" > "$EVIDENCE_DIR/result.json"
echo "Verified $MODE canary: new analysis, stable routing, ready ReplicaSet and five Gateway responses."
