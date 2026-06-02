# ADR-010: Enforce policy at admission (Kyverno), not only in CI

**Decision:** Promote the CI `conftest`/OPA checks to **in-cluster Kyverno `ClusterPolicy`** admission control. Kyverno rejects unsigned `platform-core` images (`verifyImages`, keyless), and enforces resources/probes/non-root for tenant workloads; `:latest` is audited cluster-wide.

**Reasoning:**
- CI-only policy is advisory: anyone with `kubectl apply` (or a compromised controller) bypasses every rule. Admission control makes the guardrails non-bypassable at the API server.
- **Graduated enforcement** reflects real org dynamics: `Enforce` strict standards on the governed paved road (`tenant-*` namespaces); rely on operators + CI for platform's own namespaces (e.g. Strimzi broker pods legitimately need root). This mirrors the namespace/PSA split from the multi-tenancy work.
- **verifyImages** is the runtime counterpart to ADR-009 signing: the chain is only as strong as its enforcement point.

**Trade-offs:** A failing/over-strict admission webhook can block deploys cluster-wide (`failurePolicy: Fail` on the signature policy is deliberate but operationally sharp — it needs the controller healthy). Image verification adds admission latency. Accepted: enforcement is the point; the alternative (advisory policy) provides little real assurance.
