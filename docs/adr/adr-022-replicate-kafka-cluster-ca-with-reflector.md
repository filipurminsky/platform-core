# ADR-022: Replicate the Kafka cluster CA into `apps` with kubernetes-reflector

**Decision:** Strimzi creates `platform-kafka-cluster-ca-cert` only in the Kafka namespace (`platform`). The prod Kafka clients in `apps` mount a Secret of that name (audio-api, worker-service, stt/llm/tts-worker), and their KEDA `TriggerAuthentication`s read it (`parameter: ca`). We mirror the Secret into `apps` **under the same name** with emberstack **kubernetes-reflector** (platform app `kubernetes/platform/reflector`, chart pinned).

The Kafka CR opts the Secret in through the Strimzi template field `spec.kafka.template.clusterCaCert.metadata.annotations`, with these annotations:
- `reflection-allowed: "true"`
- `reflection-allowed-namespaces: "apps"`
- `reflection-auto-enabled: "true"`
- `reflection-auto-namespaces: "apps"`

**Reasoning:**
- **No app manifest changes.** The mirror keeps the source name, so the existing volume mounts and TriggerAuthentications work unchanged.
- **Strimzi owns the opt-in.** The annotations come from the Kafka CR template, so Strimzi re-applies them on every reconcile and when it re-creates the Secret. A hand-annotated Secret would lose them.
- **Rotation propagates.** Reflector watches the source and updates the mirror within seconds when Strimzi renews the CA. The pods mount the Secret with `items` (no `subPath`), so the kubelet refreshes the file in place. KEDA re-reads auth parameters when it rebuilds a scaler.
- **Narrow blast radius.** Only this one Secret is annotated, and it is allowed only into `apps`. It contains only the public CA certificate (`ca.crt`, plus `ca.p12`/`ca.password`, a truststore with no private key).
- **Works in every environment.** The External Secrets Operator is prod-only (`external-secrets-config`), while reflector runs in dev and prod alike, so the wiring can be tested on kind.

**Alternatives considered:**
- **ESO `ClusterExternalSecret` with the Kubernetes provider.** RBAC is narrower (a Role scoped to one Secret in `platform`), but it polls on a refresh interval instead of watching, it is prod-only, and it needs a SecretStore, a ServiceAccount, and a token wired across two ArgoCD apps.
- **Running the Kafka clients in `platform`, or making the CA public (`clusterCa.generateCertificateAuthority: false` with a cert-manager CA).** Both are much bigger changes; cert-manager integration is still Early Access in Strimzi 1.3.
- **Copying the Secret by hand or with a CronJob.** Rotation would drift.

**Trade-offs:**
- Reflector runs with a cluster-wide ClusterRole on `secrets`/`configmaps` (get/list/watch/create/update/delete). We exclude the system namespaces, and it acts only on Secrets that carry the opt-in annotations. Review any new reflector annotations in code review.
- Reflector runs as a single replica. If it is down, the existing mirror stays in place, but changes stop propagating until it restarts.
- **CA key replacement** (as opposed to renewal) is different. After Strimzi re-signs the broker certificates with a new key, long-lived librdkafka clients still hold the old CA in memory. After a key replacement, run `kubectl -n apps rollout restart deploy` on the Kafka clients. Plain renewal keeps the same key and needs nothing.
- If a non-reflected Secret named `platform-kafka-cluster-ca-cert` already exists in `apps`, reflector skips the namespace. Delete the stale copy first.
