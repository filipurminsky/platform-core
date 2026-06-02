# ADR-013: Split IaC Responsibility: Terraform vs. Crossplane

**Decision:** Use Terraform for "Day-0" foundation (VPC, EKS, IAM OIDC) and Crossplane for "Day-2" app-facing infrastructure (S3, RDS).

**Reasoning:**
- **Bootstrap safety** — Terraform is better for provisioning the environment that Crossplane itself depends on (avoiding circular dependencies).
- **GitOps for Cloud** — Crossplane allows app teams to self-serve cloud resources using standard Kubernetes YAML, reconciled by ArgoCD.
- **Infrastructure as Data** — reduces the need for app teams to learn HCL or manage Terraform state; they interact only with Kubernetes CRDs.

**Trade-offs:** Requires managing two different IaC tools and state stores.
