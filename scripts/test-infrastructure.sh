#!/usr/bin/env bash
# Real Kafka/Redis/S3 acceptance tests. Only this invocation's project is removed.
set -euo pipefail
cd "$(dirname "$0")/.."
for tool in docker python3; do
  command -v "$tool" >/dev/null || { echo "Required command missing: $tool" >&2; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "Docker is unavailable. Start Docker and retry." >&2; exit 1; }
docker compose version >/dev/null
RUN_ID=$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')
PROJECT="platform-core-infra-$RUN_ID"
export INFRA_EVIDENCE_DIR="${INFRA_EVIDENCE_DIR:-${TMPDIR:-/tmp}/$PROJECT}"
mkdir -p "$INFRA_EVIDENCE_DIR"
INFRA_EVIDENCE_DIR=$(cd "$INFRA_EVIDENCE_DIR" && pwd)
export INFRA_EVIDENCE_DIR
TEST_REDIS_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
TEST_S3_ACCESS_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(12))')
TEST_S3_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
export TEST_REDIS_PASSWORD TEST_S3_ACCESS_KEY TEST_S3_SECRET_KEY
compose=(docker compose -p "$PROJECT" -f tests/infrastructure/compose.yaml)
cleanup() {
  result=$?
  trap - EXIT
  "${compose[@]}" --profile recovery logs --no-color kafka redis redis-restore objectstore > "$INFRA_EVIDENCE_DIR/infrastructure.log" 2>&1 || true
  # Preserve failure if cleanup fails, and fail successful tests on leaked fixtures.
  "${compose[@]}" --profile recovery down --volumes --remove-orphans --rmi local --timeout 10 || result=1
  echo "Infrastructure evidence: $INFRA_EVIDENCE_DIR"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
printf 'commit=%s\nproject=%s\nstarted=%s\n' "$(git rev-parse HEAD)" "$PROJECT" "$(date -u +%FT%TZ)" > "$INFRA_EVIDENCE_DIR/run.txt"
git diff --stat >> "$INFRA_EVIDENCE_DIR/run.txt"
"${compose[@]}" build tests
"${compose[@]}" up -d --wait --wait-timeout 240 kafka redis objectstore
"${compose[@]}" run --rm --no-deps --user "$(id -u):$(id -g)" tests 2>&1 | tee "$INFRA_EVIDENCE_DIR/tests.log"
python3 tests/infrastructure/redis_recovery_runner.py "$PROJECT" 2>&1 | tee "$INFRA_EVIDENCE_DIR/redis-recovery.log"
