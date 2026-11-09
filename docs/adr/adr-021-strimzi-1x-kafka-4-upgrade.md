# ADR-021: Strimzi 1.x + Kafka 4.x (v1 API, KRaft-only, rack-aware, TLS-only prod)

**Decision:** Run Strimzi **1.3.1** with Apache Kafka **4.3.2** (`metadataVersion: 4.3-IV0`). All Strimzi CRs use `kafka.strimzi.io/v1`. Prod exposes only the SCRAM-SHA-512-over-TLS listener (:9093), spreads the 3 KRaft nodes across 3 AZs, and sets `rack.topologyKey: topology.kubernetes.io/zone`.

**Reasoning:**
- **Support window.** Strimzi 0.40 / Kafka 3.7 are long out of support. Strimzi 1.3.x supports Kubernetes **1.32+**, which covers our 1.36 target (kind + EKS).
- **The v1 API is mandatory.** Strimzi 1.0.0 removed `v1beta2`/`v1beta1`/`v1alpha1`. In v1, broker CPU/memory can't go in `Kafka.spec.kafka.resources` any more, so they moved to the `KafkaNodePool`. The `strimzi.io/kraft` and `strimzi.io/node-pools` annotations are ignored (since 0.48), so we dropped them.
- **No plaintext in prod.** SCRAM over plaintext :9092 exposes the handshake and all payloads on the wire. The prod overlay removes the `plain` listener. A JSON-patch `test` op makes the build fail if the listener order ever changes, instead of silently removing the TLS listener. Dev keeps unauthenticated :9092 for local convenience.
- **Zone resilience.** RF 3 / `min.insync.replicas` 2 with `broker.rack` = AZ keeps every partition's replicas in 3 zones. The cluster survives the loss of a whole AZ for both acks=all writes and the KRaft quorum. Since Strimzi 0.49, Strimzi no longer adds zone anti-affinity by itself, so the node pool sets an explicit `topologySpreadConstraints` (zone: `DoNotSchedule`, host: `ScheduleAnyway`).
- **Cross-namespace User Operator.** Since Strimzi 1.0.1, an Entity Operator `watchedNamespace` that is not the Kafka namespace (ours is `apps`) needs `STRIMZI_ENTITY_OPERATOR_WATCHED_NAMESPACE_ENABLED=true` on the Cluster Operator. It is set in `kubernetes/platform/strimzi/application.yaml`.

**In-place upgrade procedure (existing 0.40 / 3.7.0 cluster).** Each Strimzi release supports only a window of Kafka versions, and the operator must support the *running* Kafka version before you bump it. Do one hop per PR and wait for `kubectl -n platform get kafka platform-kafka` to show `Ready` (all brokers rolled) before the next hop. Within each hop the order is:
1. Upgrade the operator chart. ArgoCD applies the chart's CRDs with ServerSideApply.
2. Change `spec.kafka.version`. This triggers a rolling restart.
3. Once the cluster is healthy, raise `metadataVersion`. This is effectively one-way, so do it last.

| Hop | Operator | Kafka `version` | `metadataVersion` | Notes |
|---|---|---|---|---|
| 1 | 0.44.0 | 3.8.0 | 3.8-IV0 | |
| 2 | 0.45.2 | 3.9.2 | 3.9-IV0 | Last ZooKeeper release (we are already KRaft). |
| 3 | 0.47.0 | 4.0.0 | 4.0-IV3 | Kafka 4: KIP-896 removes pre-2.1 client protocol versions. Log4j2 replaces log4j1 (we have no custom logging). JMX reporter is off unless metrics are configured. |
| 4 | 0.49.1 | 4.1.1 | 4.1-IV1 | `v1` API is introduced. Run the Strimzi **v1 API conversion tool** against the cluster (`convert-resource`), switch the manifests to `kafka.strimzi.io/v1`, and move `spec.kafka.resources` to the node pool. |
| 5 | 0.51.0 | 4.2.0 | 4.2-IV1 | Before leaving 0.51, run the conversion tool's **`crd-upgrade`** so `v1` is the only *stored* version. Otherwise the 1.0 CRDs can't drop `v1beta2`. |
| 6 | 1.0.1 | 4.2.0 | 4.2-IV1 | Set `STRIMZI_ENTITY_OPERATOR_WATCHED_NAMESPACE_ENABLED=true` in this hop, or the `apps` KafkaUser Secrets stop reconciling. |
| 7 | 1.3.1 | 4.3.2 | 4.3-IV0 | Final state in this repo. |

The manifests in `main` describe only the final state. On a live cluster, apply the intermediate hops by pinning the versions above in a PR (or with an ArgoCD parameter override), one hop at a time. A **fresh** cluster (kind, or a new EKS cluster) installs 1.3.1 / 4.3.2 directly.

Before any upgrade, keep a backup of the CRs: `kubectl get kafka,kafkanodepool,kafkatopic,kafkauser -A -o yaml`. Do the operator hops inside a change window, because Kafka rolls once per hop. Adding the rack config and zone spread also triggers one roll. On an existing prod cluster, the new hard zone constraint only holds if the existing gp3 PVCs already sit in 3 different AZs. If they don't, migrate the brokers one at a time (scale out the node pool in the new AZ, reassign partitions, then remove the old node).

**Client compatibility (Kafka 4):**
- **confluent-kafka (librdkafka 2.x):** compatible. librdkafka negotiates API versions, and SCRAM-SHA-512 is unchanged. The services now pin the 2.16.x client line; keep their lockfiles aligned when changing the shared Kafka dependency.
- **KEDA Kafka scaler (sarama):** defaults to protocol `version` 1.0.0. Set `version: "4.0.0"` in the trigger metadata so sarama sends request versions that Kafka 4 still accepts.
- **AKHQ 0.28:** uses a Kafka 4-compatible Java client. It runs in dev only, on :9092.

**Trade-offs:** The prod upgrade needs 7 operator hops and 7 rolling restarts, and a mistaken `metadataVersion` can't be downgraded. The hard zone spread leaves a broker Pending when a zone has no capacity; we choose that over silently putting two replicas in one AZ.
