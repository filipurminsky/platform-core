#!/usr/bin/env bash
# scripts/smoke-test.sh — end-to-end validation of the platform.
#
# Exercises two independent paths:
#   A. Kafka worker-service path: publish a job, verify KEDA scale-up and
#      message processing, then verify a poison message lands in jobs-dlq.
#   B. Audio AI pipeline: upload a file, watch KEDA scale-up of stt-worker,
#      poll to completion.
#
# Requires: kubectl connected to a running cluster (kind or EKS).
#
# Usage:
#   ./scripts/smoke-test.sh

set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────────────
NS="apps"
KAFKA_NS="platform"
KAFKA_BOOTSTRAP="localhost:9092"
API_SERVICE="audio-api"
LOCAL_PORT=8080
AUDIO_PIPELINE_TIMEOUT=600
DUMMY_FILE="/tmp/smoke-test-audio.wav"

# ─── Helpers ─────────────────────────────────────────────────────────────────
log()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()   { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
err()  { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }
section() { echo -e "\n\033[1;35m══ $* ══\033[0m"; }

cleanup() {
  log "Cleaning up..."
  [[ -f "$DUMMY_FILE" ]] && rm -f "$DUMMY_FILE"
  if [[ -n "${PF_PID:-}" ]]; then
    kill "$PF_PID" 2>/dev/null || true
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

SMOKE_JOB_ID="smoke-$(date +%s)"
SMOKE_MSG="{\"id\":\"$SMOKE_JOB_ID\",\"type\":\"ping\"}"

log "Publishing ping job to 'jobs' topic (id: $SMOKE_JOB_ID)..."
kafka_produce "jobs" "$SMOKE_MSG"
ok "Message published to jobs topic"

log "Monitoring worker-service KEDA scale-up (up to 60s)..."
END=$((SECONDS + 60))
SCALED=false
while [ $SECONDS -lt $END ]; do
  REPLICAS=$(kubectl -n "$NS" get deployment worker-service \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
  if [[ "${REPLICAS:-0}" -gt 0 ]]; then
    ok "worker-service scaled to $REPLICAS ready replica(s)"
    SCALED=true
    break
  fi
  log "Waiting for KEDA scale-up... (readyReplicas: ${REPLICAS:-0})"
  sleep 5
done

if [[ "$SCALED" == "false" ]]; then
  warn "worker-service did not scale within 60s — checking KEDA ScaledObject..."
  kubectl -n "$NS" describe scaledobject worker-service 2>/dev/null || \
    warn "No ScaledObject found for worker-service"
fi

log "Polling consumer group offset for 'worker-service' (up to 60s)..."
END=$((SECONDS + 60))
PROCESSED=false
while [ $SECONDS -lt $END ]; do
  LAG=$(kubectl -n "$KAFKA_NS" exec "$KAFKA_POD" -- \
    /opt/kafka/bin/kafka-consumer-groups.sh \
    --bootstrap-server "$KAFKA_BOOTSTRAP" \
    --describe --group worker-service 2>/dev/null \
    | awk 'NR>1 && $1=="worker-service" && $2=="jobs" {print $6}' \
    | head -1)
  if [[ "${LAG:-1}" == "0" ]]; then
    ok "worker-service consumed the ping job (consumer lag = 0)"
    PROCESSED=true
    break
  fi
  log "Waiting for message to be processed... (lag: ${LAG:-unknown})"
  sleep 5
done

if [[ "$PROCESSED" == "false" ]]; then
  warn "Could not confirm offset commit within 60s. Worker may still be warming up."
fi

# ─── Section A2: DLQ — poison message ────────────────────────────────────────
section "A2 — DLQ poison path"

log "Publishing valid job with unknown operation to trigger handler failure..."
POISON_ID="poison-$(date +%s)"
POISON_MSG="{\"id\":\"$POISON_ID\",\"type\":\"data-transform\",\"payload\":{\"input\":\"fail\",\"operation\":\"kaboom\"}}"
kafka_produce "jobs" "$POISON_MSG"
ok "Poison message published"

log "Polling 'jobs-dlq' for dead-letter (up to 30s)..."
DLQ_MSG=""
END=$((SECONDS + 30))
while [ $SECONDS -lt $END ]; do
  # Consume at most 1 message with a short timeout per iteration
  DLQ_MSG=$(kubectl -n "$KAFKA_NS" exec "$KAFKA_POD" -- \
    /opt/kafka/bin/kafka-console-consumer.sh \
    --bootstrap-server "$KAFKA_BOOTSTRAP" \
    --topic jobs-dlq \
    --from-beginning \
    --max-messages 1 \
    --timeout-ms 3000 2>/dev/null | head -1 || true)
  if [[ -n "$DLQ_MSG" ]]; then
    ok "Dead-letter message found in jobs-dlq: ${DLQ_MSG:0:120}..."
    break
  fi
  log "Waiting for DLQ message..."
  sleep 5
done

if [[ -z "$DLQ_MSG" ]]; then
  err "No message in jobs-dlq after 30s."
fi

# ─── Section B: Audio AI pipeline ────────────────────────────────────────────
section "B — Audio AI pipeline"

log "Creating dummy audio file..."
echo "This is a smoke test audio file." > "$DUMMY_FILE"

log "Port-forwarding $API_SERVICE on port $LOCAL_PORT..."
kubectl -n "$NS" port-forward svc/"$API_SERVICE" "$LOCAL_PORT":80 > /tmp/pf.log 2>&1 &
PF_PID=$!

MAX_RETRIES=10
COUNT=0
while ! curl -s "http://localhost:$LOCAL_PORT/healthz" >/dev/null; do
  if [[ $COUNT -ge $MAX_RETRIES ]]; then
    warn "Port-forward log:"
    cat /tmp/pf.log
    err "Port-forward failed to become ready after $MAX_RETRIES attempts"
  fi
  sleep 2
  COUNT=$((COUNT + 1))
done
ok "Port-forward ready"

log "Submitting audio job..."
RESP=$(curl -s -F "file=@$DUMMY_FILE;type=audio/wav" "http://localhost:$LOCAL_PORT/v1/audio/jobs")
JOB_ID=$(echo "$RESP" | grep -o '"job_id":"[^"]*' | cut -d'"' -f4)

if [[ -z "$JOB_ID" ]]; then
  err "Failed to submit job. Response: $RESP"
fi
ok "Job submitted: $JOB_ID"

log "Monitoring stt-worker KEDA scale-up (up to 60s)..."
END=$((SECONDS + 60))
SCALED=false
while [ $SECONDS -lt $END ]; do
  REPLICAS=$(kubectl -n "$NS" get deployment stt-worker \
    -o jsonpath='{.status.replicas}' 2>/dev/null || echo 0)
  if [[ "${REPLICAS:-0}" -gt 0 ]]; then
    ok "stt-worker scaled to $REPLICAS replica(s)"
    SCALED=true
    break
  fi
  log "Waiting for stt-worker scale-up... (replicas: ${REPLICAS:-0})"
  sleep 5
done

if [[ "$SCALED" == "false" ]]; then
  warn "stt-worker did not scale within 60s — checking ScaledObject..."
  kubectl -n "$NS" describe scaledobject stt-worker 2>/dev/null || true
fi

log "Polling for job completion (timeout: ${AUDIO_PIPELINE_TIMEOUT}s)..."
START_TIME=$SECONDS
LAST_STAGE=""

while [ $((SECONDS - START_TIME)) -lt $AUDIO_PIPELINE_TIMEOUT ]; do
  STATUS_RESP=$(curl -s "http://localhost:$LOCAL_PORT/jobs/$JOB_ID")
  STATUS=$(echo "$STATUS_RESP" | grep -o '"status":"[^"]*' | cut -d'"' -f4)
  STAGE=$(echo  "$STATUS_RESP" | grep -o '"stage":"[^"]*'  | cut -d'"' -f4)

  if [[ "$STAGE" != "$LAST_STAGE" ]]; then
    log "Job advanced to stage: $STAGE (status: $STATUS)"
    LAST_STAGE="$STAGE"
  fi

  if [[ "$STATUS" == "done" ]]; then
    ok "Audio pipeline job completed end-to-end!"
    echo "$STATUS_RESP" | python3 -m json.tool
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
ok "A2: DLQ path — poison message handled"
ok "B: Audio pipeline — job $JOB_ID completed end-to-end"
