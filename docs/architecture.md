# Architecture Decision Records

This document captures the key design decisions made for platform-core, the reasoning behind each choice, and the trade-offs accepted.

## Records

- [ADR-001: ArgoCD over Flux](adr/adr-001-argocd-over-flux.md)
- [ADR-002: Strimzi (self-hosted Kafka) over MSK / SQS](adr/adr-002-strimzi-kafka-over-msk-sqs.md)
- [ADR-003: Kustomize overlays over Helm values files per environment](adr/adr-003-kustomize-overlays-over-helm-values.md)
- [ADR-004: External Secrets Operator over sealed-secrets or SOPS](adr/adr-004-external-secrets-operator-over-sops.md)
- [ADR-005: vLLM over TGI (Text Generation Inference) or Triton](adr/adr-005-vllm-over-tgi-triton.md)
- [ADR-006: Scale-to-zero for both vLLM and worker-service](adr/adr-006-scale-to-zero-vllm-worker.md)
- [ADR-007: App-of-Apps pattern for GitOps](adr/adr-007-app-of-apps-pattern-gitops.md)
- [ADR-008: SLO-gated progressive delivery (Argo Rollouts) over ArgoCD auto-sync](adr/adr-008-slo-gated-progressive-delivery.md)
- [ADR-009: Sign and verify the software supply chain end to end](adr/adr-009-sign-verify-supply-chain.md)
- [ADR-010: Enforce policy at admission (Kyverno), not only in CI](adr/adr-010-enforce-policy-at-admission-kyverno.md)
- [ADR-011: Infrastructure cost minimization (Spot + Public Subnets)](adr/adr-011-infrastructure-cost-minimization.md)
- [ADR-012: Co-location of Application Source and Kubernetes Manifests](adr/adr-012-colocation-app-source-k8s-manifests.md)
- [ADR-013: Split IaC Responsibility: Terraform vs. Crossplane](adr/adr-013-split-iac-terraform-crossplane.md)
- [ADR-014: Dynamic Environment Selection via Cluster Labels](adr/adr-014-dynamic-environment-selection-cluster-labels.md)
- [ADR-015: Immutable Image Promotion via Digest Pinning](adr/adr-015-immutable-image-promotion-digest-pinning.md)
- [ADR-016: Component-based Multi-tenancy](adr/adr-016-component-based-multi-tenancy.md)
- [ADR-017: Local-first eBPF Networking with Cilium](adr/adr-017-local-first-ebpf-networking-cilium.md)
- [ADR-018: Just-in-Time Node Provisioning with Karpenter](adr/adr-018-just-in-time-node-provisioning-karpenter.md)
