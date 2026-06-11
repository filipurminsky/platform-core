# Runbook: Kafka Consumer Lag

**Alerts:** `KafkaConsumerLagHigh`, `KafkaDLQNonEmpty`
**Severity:** Warning
**Team:** Platform Engineering

---

## Symptoms

- `kafka_consumergroup_lag_sum{group="worker-service"}` > 100 sustained for > 5 minutes
- Messages accumulating in `jobs-dlq` topic
- KEDA has scaled `worker-service` to `maxReplicaCount` but lag continues to grow

---

## Immediate Checks

```bash
# 1. Check current consumer group lag
kubectl exec -n platform platform-kafka-kafka-0 -- \
  bin/kafka-consumer-groups.sh \
    --bootstrap-server localhost:9092 \
    --describe --group worker-service

# 2. Check worker-service pod health and replica count
kubectl get pods -n apps -l app=worker-service
kubectl describe scaledobject worker-service -n apps

# 3. Check for processing errors in worker-service logs
kubectl logs -n apps -l app=worker-service --tail=100 | grep -i error

# 4. Check DLQ depth
kubectl exec -n platform platform-kafka-kafka-0 -- \
  bin/kafka-consumer-groups.sh \
    --bootstrap-server localhost:9092 \
    --describe --group worker-service-dlq 2>/dev/null || \
  bin/kafka-run-class.sh kafka.tools.GetOffsetShell \
    --broker-list localhost:9092 --topic jobs-dlq
```

---

## Root Causes and Remediation

### Cause 1: worker-service processing is slow / blocked

**Signals:** Pods running and consuming, but processing latency > normal

```bash
# Check job_processing_duration_seconds metric
kubectl port-forward -n apps svc/worker-service 8080:8080 &
curl http://localhost:8080/metrics | grep job_processing_duration
```

**Fix:** Check for slow downstream dependencies (database, external API). Scale replicas manually if KEDA is at max:

```bash
kubectl scale deployment worker-service -n apps --replicas=10  # temp override
```

### Cause 2: KEDA not scaling due to TriggerAuthentication failure

**Signals:** KEDA operator logs show auth errors; ScaledObject shows `READY: False`

```bash
kubectl describe scaledobject worker-service -n apps
kubectl logs -n keda -l app=keda-operator --tail=50 | grep worker-service
```

**Fix:** Check that the Strimzi-generated secret exists and is populated:

```bash
kubectl get secret worker-service -n platform -o yaml
```

If missing, the Strimzi UserOperator may have failed. Restart it:

```bash
kubectl rollout restart deployment strimzi-cluster-operator -n platform
```

### Cause 3: Kafka partition imbalance

**Signals:** One partition has very high lag while others are near-zero

**Fix:** Trigger a partition rebalance:

```bash
kubectl exec -n platform platform-kafka-kafka-0 -- \
  bin/kafka-preferred-replica-election.sh \
    --bootstrap-server localhost:9092
```

### Cause 4: Messages in DLQ due to processing errors

**Signals:** `KafkaDLQNonEmpty` alert firing; `jobs_failed_total` metric climbing

**Fix:**
1. Identify failing message pattern from worker-service logs
2. Fix the root cause (bad message schema, dependency outage)
3. Replay DLQ messages after fix is deployed:

```bash
# Replay DLQ → jobs topic (use with caution)
kubectl exec -n platform platform-kafka-kafka-0 -- \
  bin/kafka-console-consumer.sh \
    --bootstrap-server localhost:9092 \
    --topic jobs-dlq --from-beginning \
  | kubectl exec -i -n platform platform-kafka-kafka-0 -- \
    bin/kafka-console-producer.sh \
      --bootstrap-server localhost:9092 \
      --topic jobs
```

### Cause 5: Audio pipeline DLQ messages

**Alerts:** `AudioSTTDLQNonEmpty`, `AudioLLMDLQNonEmpty`, `AudioTTSDLQNonEmpty`

Each audio DLQ maps back to one upstream topic. Replay pattern (replace `<dlq-topic>` and `<upstream-topic>` as appropriate):

| DLQ topic          | Upstream topic      | Worker to check |
|--------------------|---------------------|-----------------|
| `audio.stt-dlq`    | `audio.jobs`        | `stt-worker`    |
| `audio.llm-dlq`    | `audio.transcripts` | `llm-worker`    |
| `audio.tts-dlq`    | `audio.summaries`   | `tts-worker`    |

```bash
# Replay audio DLQ (example: stt-dlq → audio.jobs)
DLQ_TOPIC=audio.stt-dlq
UPSTREAM_TOPIC=audio.jobs
kubectl exec -n platform platform-kafka-kafka-0 -- \
  bin/kafka-console-consumer.sh \
    --bootstrap-server localhost:9092 \
    --topic "$DLQ_TOPIC" --from-beginning \
  | kubectl exec -i -n platform platform-kafka-kafka-0 -- \
    bin/kafka-console-producer.sh \
      --bootstrap-server localhost:9092 \
      --topic "$UPSTREAM_TOPIC"
```

Note: audio workers are idempotent by job_id (S3 writes use deterministic keys; Redis state is overwritten). Replay is safe to run after the root cause is fixed; messages that already completed will be deduplicated by the `seen_ids` set or overwrite S3/Redis with identical data.

---

## Escalation

If lag does not decrease within 30 minutes after remediation:
1. Page on-call platform engineer
2. Consider disabling the consumer (set `minReplicaCount: 0`, remove `activationLagThreshold`) to stop the DLQ from growing
3. Capture a heap dump from a worker-service pod for debugging
