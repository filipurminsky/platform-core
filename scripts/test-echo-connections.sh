#!/usr/bin/env bash
# Controlled delayed-FIN regression. Only this invocation's Compose project is removed.
set -euo pipefail
cd "$(dirname "$0")/.."
for tool in docker python3; do
  command -v "$tool" >/dev/null || { echo "Required command missing: $tool" >&2; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "Docker is unavailable. Start Docker and retry." >&2; exit 1; }
docker compose version >/dev/null
export ECHO_IMAGE="${ECHO_IMAGE:-echo-service:latest}"
PROJECT="platform-core-connections-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
export CONNECTION_EVIDENCE_DIR="${EVIDENCE_DIR:-${TMPDIR:-/tmp}/$PROJECT}"
mkdir -p "$CONNECTION_EVIDENCE_DIR"
CONNECTION_EVIDENCE_DIR=$(cd "$CONNECTION_EVIDENCE_DIR" && pwd)
export CONNECTION_EVIDENCE_DIR
python3 - "$CONNECTION_EVIDENCE_DIR" <<'PY'
import sys
from pathlib import Path

if any(Path(sys.argv[1]).iterdir()):
    raise SystemExit("EVIDENCE_DIR must be empty; choose a new directory for each run.")
PY
TEST_UID=$(id -u)
TEST_GID=$(id -g)
export TEST_UID TEST_GID
# Require the real built image and explicit backend timeout; never silently pull
# a similarly named image from a registry or override the application command.
docker image inspect "$ECHO_IMAGE" --format '{"id":{{json .Id}},"cmd":{{json .Config.Cmd}}}' > "$CONNECTION_EVIDENCE_DIR/echo-image.json" || {
  echo "Build first: docker build -f services/echo-service/Dockerfile -t echo-service:latest services/" >&2
  exit 1
}
python3 tests/connection-reuse/evidence.py prepare "$CONNECTION_EVIDENCE_DIR"
compose=(docker compose -p "$PROJECT" -f tests/connection-reuse/compose.yaml)
cleanup() {
  result=$?
  trap - EXIT
  "${compose[@]}" logs --no-color > "$CONNECTION_EVIDENCE_DIR/containers.log" 2>&1 || true
  "${compose[@]}" down --volumes --remove-orphans --timeout 5 || result=1
  echo "Connection regression evidence: $CONNECTION_EVIDENCE_DIR"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf 'commit=%s\nproject=%s\nstarted=%s\n' "$(git rev-parse HEAD)" "$PROJECT" "$(date -u +%FT%TZ)" > "$CONNECTION_EVIDENCE_DIR/run.txt"
"${compose[@]}" up -d echo proxy baseline candidate
"${compose[@]}" run --rm --no-deps tests 2>&1 | tee "$CONNECTION_EVIDENCE_DIR/tests.log"
# Stop Envoy to flush access logs before checking exact per-cycle attribution.
"${compose[@]}" stop --timeout 5 baseline candidate
for target in baseline candidate; do
  "${compose[@]}" logs --no-color "$target" > "$CONNECTION_EVIDENCE_DIR/$target.log"
done
python3 tests/connection-reuse/evidence.py verify "$CONNECTION_EVIDENCE_DIR"
