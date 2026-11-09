#!/usr/bin/env bash
# scripts/smoke-test.sh — end-to-end validation of the platform.
#
# Exercises two independent paths:
#   A. Kafka worker-service path: publish a job, verify readiness and
#      message processing, then verify a poison message lands in jobs-dlq.
#   B. Audio AI pipeline: upload a file, wait for stt-worker readiness,
#      poll to completion.
#
# Requires: a dev-labelled local cluster. AWS SCRAM/TLS is not supported by this script.
#
# Usage:
#   ./scripts/smoke-test.sh

set -euo pipefail
cd "$(dirname "$0")/.."

# ─── Config ──────────────────────────────────────────────────────────────────
NS="apps"
KAFKA_NS="platform"
KAFKA_BOOTSTRAP="localhost:9092"
API_SERVICE="audio-api"
LOCAL_PORT="${LOCAL_PORT:-18080}"
AUDIO_PIPELINE_TIMEOUT="${AUDIO_PIPELINE_TIMEOUT:-600}"
WORKER_TIMEOUT="${WORKER_TIMEOUT:-120}"
DLQ_TIMEOUT="${DLQ_TIMEOUT:-120}"
EVIDENCE_DIR="${EVIDENCE_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/platform-smoke.XXXXXX")}"
mkdir -p "$EVIDENCE_DIR"
DUMMY_FILE="$EVIDENCE_DIR/audio.wav"

# ─── Helpers ─────────────────────────────────────────────────────────────────
log()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()   { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
err()  { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }
section() { echo -e "\n\033[1;35m══ $* ══\033[0m"; }

cleanup() {
  log "Cleaning up..."
  echo "Evidence retained in $EVIDENCE_DIR"
  if [[ -n "${PF_PID:-}" ]]; then
    kill "$PF_PID" 2>/dev/null || true
    wait "$PF_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# ─── Kafka helper: exec console-producer in the Kafka pod ────────────────────
kafka_produce() {
  local topic="$1"
  local message="$2"
  kubectl -n "$KAFKA_NS" exec -i "$KAFKA_POD" -- \
    /opt/kafka/bin/kafka-console-producer.sh \
    --bootstrap-server "$KAFKA_BOOTSTRAP" \
    --topic "$topic" <<< "$message"
}

# ─── Pre-flight checks ───────────────────────────────────────────────────────
section "Pre-flight"
for tool in kubectl curl python3; do command -v "$tool" >/dev/null || err "Missing $tool"; done
KUBE_CONTEXT="${KUBE_CONTEXT:-$(kubectl config current-context)}"
kubectl() { command kubectl --context "$KUBE_CONTEXT" --request-timeout=20s "$@"; }
ENVIRONMENT=$(kubectl -n argocd get secret in-cluster -o jsonpath='{.metadata.labels.environment}')
[[ "$ENVIRONMENT" == dev ]] || err "This smoke test requires a dev-labelled cluster; AWS TLS/SCRAM needs a separate client configuration."
log "Context: $KUBE_CONTEXT; evidence: $EVIDENCE_DIR"

log "Checking cluster connectivity..."
kubectl cluster-info >/dev/null 2>&1 || err "kubectl not connected to a cluster"
ok "Cluster reachable"

log "Verifying required deployments in namespace: $NS"
for deploy in audio-api worker-service stt-worker llm-worker tts-worker; do
  kubectl -n "$NS" get deployment "$deploy" >/dev/null 2>&1 \
    || err "Deployment '$deploy' not found in namespace '$NS'"
done
ok "All required deployments present"

log "Finding Kafka pod in namespace: $KAFKA_NS"
KAFKA_POD=$(kubectl -n "$KAFKA_NS" get pods \
  -l strimzi.io/component-type=kafka \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
[[ -z "$KAFKA_POD" ]] && err "No Kafka pod found in namespace '$KAFKA_NS' (label strimzi.io/component-type=kafka)"
ok "Kafka pod: $KAFKA_POD"

# ─── Section A: Kafka worker-service path ────────────────────────────────────
section "A — Kafka worker-service path"

SMOKE_JOB_ID="smoke-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
SMOKE_MSG="{\"id\":\"$SMOKE_JOB_ID\",\"type\":\"ping\"}"

log "Publishing ping job to 'jobs' topic (id: $SMOKE_JOB_ID)..."
kafka_produce "jobs" "$SMOKE_MSG"
ok "Message published to jobs topic"

log "Waiting for worker-service readiness (up to ${WORKER_TIMEOUT}s)..."
END=$((SECONDS + WORKER_TIMEOUT))
SCALED=false
while [ $SECONDS -lt $END ]; do
  REPLICAS=$(kubectl -n "$NS" get deployment worker-service \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
  if [[ "${REPLICAS:-0}" -gt 0 ]]; then
    ok "worker-service has $REPLICAS ready replica(s)"
    SCALED=true
    break
  fi
  log "Waiting for worker readiness... (readyReplicas: ${REPLICAS:-0})"
  sleep 5
done

if [[ "$SCALED" == "false" ]]; then
  warn "worker-service did not become ready within ${WORKER_TIMEOUT}s — checking KEDA ScaledObject..."
  kubectl -n "$NS" describe scaledobject worker-service 2>/dev/null || \
    warn "No ScaledObject found for worker-service"
  err "worker-service did not become ready within ${WORKER_TIMEOUT}s"
fi

log "Polling consumer group offset for 'worker-service' (up to ${WORKER_TIMEOUT}s)..."
END=$((SECONDS + WORKER_TIMEOUT))
PROCESSED=false
while [ $SECONDS -lt $END ]; do
  OFFSETS_OK=false
  if kubectl -n "$KAFKA_NS" exec "$KAFKA_POD" -- \
    /opt/kafka/bin/kafka-consumer-groups.sh \
    --bootstrap-server "$KAFKA_BOOTSTRAP" --describe --group worker-service \
    > "$EVIDENCE_DIR/offsets.txt" 2>/dev/null; then OFFSETS_OK=true; fi
  kubectl -n "$NS" logs -l app=worker-service --all-containers=true --prefix=false --tail=1000 \
    > "$EVIDENCE_DIR/worker.log" 2>/dev/null || true
  if [[ "$OFFSETS_OK" == true ]] && python3 scripts/smoke-evidence.py processed "$EVIDENCE_DIR" "$SMOKE_JOB_ID"; then
    ok "Specific ping job processed and every jobs partition caught up"
    PROCESSED=true
    break
  fi
  log "Waiting for the specific job and committed offsets..."
  sleep 5
done

if [[ "$PROCESSED" == "false" ]]; then
  err "Could not confirm this job and committed offsets within ${WORKER_TIMEOUT}s"
fi

# ─── Section A2: DLQ — poison message ────────────────────────────────────────
section "A2 — DLQ poison path"

log "Publishing valid job with unknown operation to trigger handler failure..."
POISON_ID="poison-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
POISON_MSG="{\"id\":\"$POISON_ID\",\"type\":\"data-transform\",\"payload\":{\"input\":\"fail\",\"operation\":\"kaboom\"}}"
kafka_produce "jobs" "$POISON_MSG"
ok "Poison message published"

log "Polling 'jobs-dlq' for dead-letter with id $POISON_ID (up to ${DLQ_TIMEOUT}s)..."
DLQ_FOUND=false
END=$((SECONDS + DLQ_TIMEOUT))
while [ $SECONDS -lt $END ]; do
  # Scan all accumulated DLQ messages and look for the specific poison job ID.
  # --timeout-ms 10000: wait up to 10s for new messages after draining existing
  # ones so we don't miss the DLQ entry arriving right after the consumer starts.
  # Console consumer returns nonzero on timeout even after receiving records.
  # Capture first, then parse exact JSON identity (no grep/SIGPIPE false positives).
  kubectl -n "$KAFKA_NS" exec "$KAFKA_POD" -- \
    /opt/kafka/bin/kafka-console-consumer.sh \
    --bootstrap-server "$KAFKA_BOOTSTRAP" --topic jobs-dlq --from-beginning \
    --timeout-ms 10000 > "$EVIDENCE_DIR/dlq.jsonl" 2>/dev/null || true
  if python3 scripts/smoke-evidence.py dlq "$EVIDENCE_DIR" "$POISON_ID"; then
    ok "Dead-letter message for $POISON_ID found in jobs-dlq"
    DLQ_FOUND=true
    break
  fi
  log "Waiting for DLQ message... (retries may still be in progress)"
  sleep 5
done

if [[ "$DLQ_FOUND" == "false" ]]; then
  err "Poison message $POISON_ID not found in jobs-dlq within ${DLQ_TIMEOUT}s"
fi

# ─── Section B: Audio AI pipeline ────────────────────────────────────────────
section "B — Audio AI pipeline"

log "Creating dummy audio file..."
python3 - "$DUMMY_FILE" <<'WAV'
import sys
import wave
with wave.open(sys.argv[1], "wb") as audio:
    audio.setnchannels(1)
    audio.setsampwidth(2)
    audio.setframerate(16000)
    audio.writeframes(b"\x00\x00" * 16000)
WAV

log "Port-forwarding $API_SERVICE on port $LOCAL_PORT..."
# Track the actual kubectl PID rather than a background shell-function wrapper.
command kubectl --context "$KUBE_CONTEXT" --request-timeout=20s -n "$NS" \
  port-forward svc/"$API_SERVICE" "$LOCAL_PORT":80 > "$EVIDENCE_DIR/port-forward.log" 2>&1 &
PF_PID=$!

MAX_RETRIES=10
COUNT=0
while ! grep -q "Forwarding from" "$EVIDENCE_DIR/port-forward.log" || ! curl --fail --silent --show-error --connect-timeout 3 --max-time 15 "http://localhost:$LOCAL_PORT/healthz" >/dev/null; do
  if [[ $COUNT -ge $MAX_RETRIES ]]; then
    warn "Port-forward log:"
    cat "$EVIDENCE_DIR/port-forward.log"
    err "Port-forward failed to become ready after $MAX_RETRIES attempts"
  fi
  kill -0 "$PF_PID" 2>/dev/null || err "Port-forward exited; inspect $EVIDENCE_DIR/port-forward.log"
  sleep 2
  COUNT=$((COUNT + 1))
done
ok "Port-forward ready"

kill -0 "$PF_PID" 2>/dev/null || err "Owned port-forward is not running"
log "Submitting audio job..."
RESP=$(curl --fail --silent --show-error --connect-timeout 3 --max-time 15 -F "file=@$DUMMY_FILE;type=audio/wav" "http://localhost:$LOCAL_PORT/v1/audio/jobs")
JOB_ID=$(printf '%s' "$RESP" | python3 -c 'import json,sys,uuid; value=json.load(sys.stdin)["job_id"]; uuid.UUID(value); print(value)')

if [[ -z "$JOB_ID" ]]; then
  err "Failed to submit job. Response: $RESP"
fi
ok "Job submitted: $JOB_ID"

log "Waiting for stt-worker readiness (up to ${WORKER_TIMEOUT}s)..."
END=$((SECONDS + WORKER_TIMEOUT))
SCALED=false
while [ $SECONDS -lt $END ]; do
  REPLICAS=$(kubectl -n "$NS" get deployment stt-worker \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
  if [[ "${REPLICAS:-0}" -gt 0 ]]; then
    ok "stt-worker has $REPLICAS replica(s)"
    SCALED=true
    break
  fi
  log "Waiting for stt-worker readiness... (replicas: ${REPLICAS:-0})"
  sleep 5
done

if [[ "$SCALED" == "false" ]]; then
  warn "stt-worker did not become ready within ${WORKER_TIMEOUT}s — checking ScaledObject..."
  kubectl -n "$NS" describe scaledobject stt-worker 2>/dev/null || true
  err "stt-worker did not become ready within ${WORKER_TIMEOUT}s"
fi

log "Polling for job completion (timeout: ${AUDIO_PIPELINE_TIMEOUT}s)..."
START_TIME=$SECONDS
LAST_STAGE=""
STATUS=""
STATUS_RESP=""

while [ $((SECONDS - START_TIME)) -lt "$AUDIO_PIPELINE_TIMEOUT" ]; do
  STATUS_RESP=$(curl --fail --silent --show-error --connect-timeout 3 --max-time 15 "http://localhost:$LOCAL_PORT/jobs/$JOB_ID")
  STATUS=$(printf '%s' "$STATUS_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  STAGE=$(printf '%s' "$STATUS_RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("stage", ""))')

  if [[ "$STAGE" != "$LAST_STAGE" ]]; then
    log "Job advanced to stage: $STAGE (status: $STATUS)"
    LAST_STAGE="$STAGE"
  fi

  if [[ "$STATUS" == "done" ]]; then
    ok "Audio pipeline job completed end-to-end!"
    printf '%s' "$STATUS_RESP" > "$EVIDENCE_DIR/job.json"
    python3 scripts/smoke-evidence.py completed "$EVIDENCE_DIR" "$JOB_ID" || err 'Completed job has missing output keys'
    break
  fi

  if [[ "$STATUS" == "failed" ]]; then
    err "Job failed: $STATUS_RESP"
  fi

  sleep 5
done

if [[ "$STATUS" != "done" ]]; then
  err "Audio pipeline timed out after ${AUDIO_PIPELINE_TIMEOUT}s. Last response: $STATUS_RESP"
fi

# ─── Summary ─────────────────────────────────────────────────────────────────
section "Smoke test complete"
ok "A: Kafka worker-service path — ping job published and consumed"
ok "A2: DLQ path — poison message delivery verified"
ok "B: Audio pipeline — job $JOB_ID completed end-to-end"
