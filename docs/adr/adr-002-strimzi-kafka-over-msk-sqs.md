# ADR-002: Strimzi (self-hosted Kafka) over MSK / SQS

**Decision:** Run Kafka on Kubernetes via the Strimzi operator rather than AWS MSK or SQS.

**Reasoning:**
1. **Dev/prod parity** — the same Strimzi `Kafka` CR runs on a local kind cluster and EKS. MSK does not run locally, which would create a gap between development and production.
2. **KEDA precision** — Kafka's per-partition consumer lag is a better autoscaling signal than a coarse SQS `ApproximateNumberOfMessages`. KEDA's `kafka` trigger scales one replica per `lagThreshold` messages per partition, enabling proportional scale-out.
3. **Metric depth** — Strimzi exposes JMX metrics and a Kafka Exporter sidecar, giving full broker, topic, and consumer group observability in Grafana.
4. **No AWS lock-in** — the platform can run on GKE or AKS without changing application code.

**Trade-offs:** Strimzi adds operational burden (managing the KRaft metadata quorum and storage). For a startup this might not be worth it vs. MSK; for a multi-cloud or cost-sensitive org, the trade-off is justified.
