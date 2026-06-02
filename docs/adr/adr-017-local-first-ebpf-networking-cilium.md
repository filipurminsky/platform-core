# ADR-017: Local-first eBPF Networking with Cilium

**Decision:** Use Cilium as the CNI for local development (kind clusters) to provide eBPF-based observability and policy enforcement.

**Reasoning:**
- **Hubble visibility** — provides deep, live flow visibility and NetworkPolicy debugging via the Hubble UI.
- **Security parity** — allows developers to test and verify NetworkPolicies locally with the same eBPF-backed logic used in advanced production environments.
- **Performance** — eBPF-based networking is more efficient than standard iptables-based routing.

**Trade-offs:** Increases local bootstrap complexity; EKS continues to use AWS VPC CNI for simplicity and stability in this reference implementation.
