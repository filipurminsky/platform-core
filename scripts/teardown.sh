#!/usr/bin/env bash
# teardown.sh — destroy local kind cluster or AWS EKS resources

set -euo pipefail
cd "$(dirname "$0")/.."

MODE="local"
CLUSTER_NAME="platform-core"

for arg in "$@"; do
  case $arg in
    --mode=*) MODE="${arg#*=}" ;;
    --cluster-name=*) CLUSTER_NAME="${arg#*=}" ;;
    --help) echo "Usage: $0 [--mode=local|--mode=aws] [--cluster-name=NAME]"; exit 0 ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

log() { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
err() { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }
[[ "$CLUSTER_NAME" =~ ^[a-z0-9][a-z0-9.-]*$ ]] || err 'Invalid kind cluster name.'

if [[ "$MODE" == "local" ]]; then
  log "Deleting kind cluster: $CLUSTER_NAME"
  kind delete cluster --name "$CLUSTER_NAME"
  log "Stop any port-forwards you started for kind-$CLUSTER_NAME in their own terminals."
  log "Done."

elif [[ "$MODE" == "aws" ]]; then
  ENVIRONMENT="prod"  # --mode=aws always targets prod
  echo "⚠️  This will DESTROY the ${ENVIRONMENT} AWS EKS cluster and all resources. Type 'destroy' to confirm:"
  read -r confirm
  [[ "$confirm" != "destroy" ]] && err "Aborted."

  log "Running terraform destroy (${ENVIRONMENT})"
  cd "terraform/environments/${ENVIRONMENT}"
  terraform destroy -auto-approve
  log "Done."
else
  err "Unknown mode: $MODE (expected local|aws)"
fi
