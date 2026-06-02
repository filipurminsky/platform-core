# ADR-011: Infrastructure cost minimization (Spot + Public Subnets)

**Decision:** Optimize for minimum AWS burn rate by using **EC2 Spot instances** for all node groups and running all nodes in **public subnets** to avoid NAT Gateway charges ($32/month/AZ).

**Reasoning:**
- **Showcase economics** — as a non-revenue-generating reference implementation, reducing the idle monthly bill from ~$150 to ~$40 (EKS control plane + minimal spot nodes) is a priority.
- **Spot utility** — Karpenter and EKS Managed Node Groups handle Spot interruptions gracefully. The Kafka-based architecture (worker-service) is idempotent, and the vLLM inference service is backed by an Argo Rollouts canary that can handle individual pod terminations.
- **NAT Gateway elimination** — NAT Gateways are one of the highest idle costs in a small EKS cluster. Moving nodes to public subnets and using `map_public_ip_on_launch` allows nodes to reach ECR and the internet for free.

**Trade-offs:**
- **Interruption risk** — a GPU Spot interruption will cause a 2–5 minute cold start as the model reloads on a new node. Accepted: for a demo, a 70% cost saving justifies the rare interruption.
- **Security surface** — nodes having public IPs increases the theoretical attack surface. Mitigated by strict Security Groups (EKS defaults + Karpenter) and the fact that no services are exposed via NodePort; all traffic enters through the LoadBalancer.
- **Complexity** — requires ensuring Karpenter and EKS are explicitly configured for Spot and public subnet discovery.
