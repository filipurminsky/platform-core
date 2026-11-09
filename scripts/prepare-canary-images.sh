#!/usr/bin/env bash
# Build local-only demo variants; the bad variant keeps probes/metrics healthy
# while returning HTTP 500 at /, so the failure exercises the SLO gate.
set -euo pipefail
cd "$(dirname "$0")/.."
CLUSTER_NAME=${CLUSTER_NAME:-platform-core}
GOOD_IMAGE=${GOOD_IMAGE:-echo-service:v2}
BAD_IMAGE=${BAD_IMAGE:-echo-service:broken}
[[ "$GOOD_IMAGE" != "$BAD_IMAGE" ]] || { echo 'Use distinct good/bad image tags.' >&2; exit 1; }
for tool in docker kind python3; do command -v "$tool" >/dev/null || { echo "Missing $tool" >&2; exit 1; }; done
docker info >/dev/null
kind get clusters | grep -Fx "$CLUSTER_NAME" >/dev/null || { echo "kind cluster $CLUSTER_NAME not found; bootstrap it first." >&2; exit 1; }
context=$(mktemp -d)
trap 'rm -rf "$context"' EXIT
DOCKER_BUILDKIT=1 docker build -f services/echo-service/Dockerfile -t "$GOOD_IMAGE" services/
python3 - "$context/main.py" <<'PY'
import pathlib
import sys
source = pathlib.Path("services/echo-service/main.py").read_text()
needle = "\n    body = None\n"
if source.count(needle) != 1:
    sys.exit("echo handler changed; update the demo fault injection before building")
source = source.replace(needle, '\n    return Response(status_code=500, content="intentional canary demo failure")\n' + needle)
pathlib.Path(sys.argv[1]).write_text(source)
PY
# Docker expands BASE inside the generated Dockerfile.
# shellcheck disable=SC2016
printf 'ARG BASE\nFROM ${BASE}\nCOPY main.py /app/main.py\n' > "$context/Dockerfile"
docker build --build-arg "BASE=$GOOD_IMAGE" -t "$BAD_IMAGE" "$context"
kind load docker-image --name "$CLUSTER_NAME" "$GOOD_IMAGE" "$BAD_IMAGE"
echo "Loaded $GOOD_IMAGE and $BAD_IMAGE into kind cluster $CLUSTER_NAME."
echo "Run GOOD_IMAGE=$GOOD_IMAGE BAD_IMAGE=$BAD_IMAGE KUBE_CONTEXT=kind-$CLUSTER_NAME ./scripts/canary-demo.sh bad"
