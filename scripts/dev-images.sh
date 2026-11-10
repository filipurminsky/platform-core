#!/usr/bin/env bash
# Build/load precisely the image references used by the seven dev overlays.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
CLUSTER_NAME=platform-core
for arg in "$@"; do
  case "$arg" in
    --cluster-name=*) CLUSTER_NAME=${arg#*=} ;;
    --help) echo "Usage: $0 [--cluster-name=NAME]"; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done
[[ "$CLUSTER_NAME" =~ ^[a-z0-9][a-z0-9.-]*$ ]] || { echo 'Invalid kind cluster name.' >&2; exit 2; }
for tool in docker kind; do
  command -v "$tool" >/dev/null || { echo "Missing $tool; see docs/local-dev.md." >&2; exit 1; }
done
docker info >/dev/null
kind get clusters | grep -Fx "$CLUSTER_NAME" >/dev/null || {
  echo "kind cluster $CLUSTER_NAME not found; run bootstrap first." >&2; exit 1;
}
apps=(echo-service worker-service llm-gateway audio-api stt-worker llm-worker tts-worker)
images=()
for app in "${apps[@]}"; do
  echo "Building $app:latest (dev overlay image; services/ context)"
  DOCKER_BUILDKIT=1 docker build --file "services/$app/Dockerfile" --tag "$app:latest" services/
  images+=("$app:latest")
done
kind load docker-image --name "$CLUSTER_NAME" "${images[@]}"
echo "Loaded all seven dev images into kind-$CLUSTER_NAME. Existing pods are not restarted."
