#!/usr/bin/env bash
# teardown.sh — destroy local kind cluster or AWS EKS resources

set -euo pipefail

MODE="local"
CLUSTER_NAME="platform-core"

for arg in "$@"; do
  case $arg in
    --mode=*) MODE="${arg#*=}" ;;
    *) echo "Unknown argument: $arg"; exit 1 ;;
  esac
done

log() { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
err() { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }

if [[ "$MODE" == "local" ]]; then
  log "Deleting kind cluster: $CLUSTER_NAME"
  kind delete cluster --name "$CLUSTER_NAME"
  log "Killing port-forwards"
  pkill -f "kubectl port-forward" || true
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
fi
