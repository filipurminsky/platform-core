#!/usr/bin/env bash
# Read-only prerequisites and cluster diagnostics; never prints Secret contents.
set -uo pipefail
MODE=local
PREFLIGHT=false
for arg in "$@"; do
  case "$arg" in
    --mode=local|--mode=aws) MODE=${arg#*=} ;;
    --preflight) PREFLIGHT=true ;;
    *) echo "Usage: $0 [--mode=local|--mode=aws] [--preflight]" >&2; exit 2 ;;
  esac
done
failed=0
check_tool() {
  if command -v "$1" >/dev/null 2>&1; then
    printf '[OK] %s: %s\n' "$1" "$(command -v "$1")"
  else
    printf '[FAIL] Missing %s; install the version in docs/tool-upgrades.md\n' "$1" >&2
    failed=1
  fi
}
for tool in kubectl helm python3; do check_tool "$tool"; done
if [[ "$MODE" == local ]]; then
  for tool in docker kind openssl; do check_tool "$tool"; done
  if command -v docker >/dev/null; then
    if docker info --format 'Docker: {{.ServerVersion}}, memory: {{.MemTotal}} bytes' 2>/dev/null; then
      echo 'Allocate at least 8 GiB to Docker for the full local platform; see docs/local-dev.md.'
    else
      echo '[FAIL] Docker is unavailable. Start Docker Desktop and check docker context show.' >&2
      failed=1
    fi
  fi
  if command -v kind >/dev/null; then
    version=$(kind version 2>/dev/null | awk '{print $2}')
    python3 - "$version" <<'PY' || failed=1
import re
import sys
match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", sys.argv[1])
if not match or tuple(map(int, match.groups())) < (0, 33, 0):
    sys.exit("[FAIL] kind >= 0.33.0 required for the pinned Kubernetes node image.")
PY
  fi
else
  check_tool aws
  echo 'AWS prerequisite: applied prod Terraform, output wiring, and network access to the private EKS API.'
fi
if command -v helm >/dev/null; then helm version --short; fi
if [[ "$PREFLIGHT" == false ]] && command -v kubectl >/dev/null; then
  context=$(kubectl config current-context 2>/dev/null || true)
  if [[ -z "$context" ]]; then
    echo '[FAIL] No Kubernetes context selected. List contexts with: kubectl config get-contexts' >&2
    failed=1
  else
    echo "Inspecting context: $context"
    if kubectl --request-timeout=10s get nodes -o wide; then
      kubectl --request-timeout=10s -n argocd get applications \
        -o 'custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status' || failed=1
      kubectl --request-timeout=10s get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded || failed=1
    else
      echo '[FAIL] API unreachable. Check the selected context, Docker, or private EKS network access.' >&2
      failed=1
    fi
  fi
fi
echo 'Troubleshooting: docs/local-dev.md; application events: kubectl -n argocd describe application <name>'
exit "$failed"
