#!/usr/bin/env bash
# scripts/smoke-test.sh — end-to-end validation of the AI audio pipeline.
#
# This script:
# 1. Verifies cluster connectivity and required components.
# 2. Submits a dummy audio job to audio-api.
# 3. Watches for KEDA scale-up of the worker pipeline.
# 4. Polls the job status until it reaches 'done'.
#
# Usage:
#   ./scripts/smoke-test.sh

set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────────────
NS="apps"
API_SERVICE="audio-api"
LOCAL_PORT=8080
TIMEOUT_SECONDS=300
DUMMY_FILE="/tmp/smoke-test-audio.wav"

# ─── Helpers ─────────────────────────────────────────────────────────────────
log()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()   { echo -e "\033[1;32m[ OK ]\033[0m  $*"; }
warn() { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
err()  { echo -e "\033[1;31m[ERR ]\033[0m  $*" >&2; exit 1; }

cleanup() {
  log "Cleaning up..."
  [[ -f "$DUMMY_FILE" ]] && rm "$DUMMY_FILE"
  if [[ -n "${PF_PID:-}" ]]; then
    kill "$PF_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# ─── Pre-flight checks ───────────────────────────────────────────────────────
log "Checking cluster connectivity..."
kubectl cluster-info >/dev/null 2>&1 || err "kubectl not connected to a cluster"

log "Verifying required components in namespace: $NS"
for deploy in audio-api stt-worker llm-worker tts-worker; do
  kubectl -n "$NS" get deployment "$deploy" >/dev/null 2>&1 || err "Deployment $deploy not found in $NS"
done

# ─── Setup ───────────────────────────────────────────────────────────────────
log "Creating dummy audio file..."
echo "This is a smoke test audio file." > "$DUMMY_FILE"

log "Port-forwarding $API_SERVICE..."
# Use a random port if 8080 is busy or just stick to 8080 but check it
kubectl -n "$NS" port-forward svc/"$API_SERVICE" "$LOCAL_PORT":80 > /tmp/pf.log 2>&1 &
PF_PID=$!

# Wait for port-forward
MAX_RETRIES=10
COUNT=0
while ! curl -s http://localhost:"$LOCAL_PORT"/healthz >/dev/null; do
  if [[ $COUNT -ge $MAX_RETRIES ]]; then
    warn "Port-forward log:"
    cat /tmp/pf.log
    err "Port-forward failed to become ready after $MAX_RETRIES attempts"
  fi
  sleep 2
  COUNT=$((COUNT + 1))
done

# ─── Job Submission ──────────────────────────────────────────────────────────
log "Submitting job..."
RESP=$(curl -s -F "file=@$DUMMY_FILE;type=audio/wav" http://localhost:"$LOCAL_PORT"/v1/audio/jobs)
JOB_ID=$(echo "$RESP" | grep -o '"job_id":"[^"]*' | cut -d'"' -f4)

if [[ -z "$JOB_ID" ]]; then
  err "Failed to submit job. Response: $RESP"
fi
ok "Job submitted successfully! ID: $JOB_ID"

# ─── Pipeline Monitoring ─────────────────────────────────────────────────────
log "Monitoring worker scale-up (KEDA)..."
# We check stt-worker as it's the first to scale
END=$((SECONDS + 60))
SCALED=false
while [ $SECONDS -lt $END ]; do
  REPLICAS=$(kubectl -n "$NS" get deployment stt-worker -o jsonpath='{.status.replicas}')
  if [[ "$REPLICAS" -gt 0 ]]; then
    ok "stt-worker scaled up to $REPLICAS replicas"
    SCALED=true
    break
  fi
  log "Waiting for KEDA scale-up... (current: $REPLICAS)"
  sleep 5
done

if [[ "$SCALED" == "false" ]]; then
  warn "stt-worker did not scale up within 60s. Checking KEDA ScaledObject..."
  kubectl -n "$NS" describe scaledobject stt-worker
fi

# ─── Status Polling ──────────────────────────────────────────────────────────
log "Polling for job completion (timeout: ${TIMEOUT_SECONDS}s)..."
START_TIME=$SECONDS
LAST_STAGE=""

while [ $((SECONDS - START_TIME)) -lt $TIMEOUT_SECONDS ]; do
  STATUS_RESP=$(curl -s http://localhost:"$LOCAL_PORT"/jobs/"$JOB_ID")
  STATUS=$(echo "$STATUS_RESP" | grep -o '"status":"[^"]*' | cut -d'"' -f4)
  STAGE=$(echo "$STATUS_RESP" | grep -o '"stage":"[^"]*' | cut -d'"' -f4)

  if [[ "$STAGE" != "$LAST_STAGE" ]]; then
    log "Job advanced to stage: $STAGE (Status: $STATUS)"
    LAST_STAGE="$STAGE"
  fi

  if [[ "$STATUS" == "done" ]]; then
    ok "Job completed successfully end-to-end!"
    echo "$STATUS_RESP" | python3 -m json.tool
    exit 0
  fi

  if [[ "$STATUS" == "failed" ]]; then
    err "Job failed! Status: $STATUS_RESP"
  fi

  sleep 5
done

err "Job timed out after ${TIMEOUT_SECONDS}s. Last state: $STATUS_RESP"
