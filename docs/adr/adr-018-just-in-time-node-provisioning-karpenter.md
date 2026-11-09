# ADR-018: Just-in-time node provisioning with Karpenter

**Decision:** In prod EKS, retain a small on-demand managed node group for
platform capacity and use Karpenter for application burst and GPU nodes.
The static GPU node group is disabled. Karpenter is not installed on local kind.

This supersedes the original statement that all managed node groups were
replaced. Karpenter's controller runs on the `role=platform` baseline group so
it can provision nodes even when all dynamic capacity is absent.

**Reasoning:** NodePools express workload requirements and purchase options
without maintaining a separate static group for every shape. GPU workloads
retain matching selectors, taints, and tolerations. Terraform owns the
controller/node IAM, interruption queue, discovery tags, and baseline EKS
capacity; GitOps owns Karpenter's NodePool and EC2NodeClass configuration.

**Tradeoffs:** Another controller, IAM and discovery wiring, interruption
handling, and cold-start latency. Baseline capacity still costs money. Faster
provisioning and lower spend are hypotheses to measure in an AWS exercise,
not results established by this reference implementation.

See [prod Terraform](../../terraform/environments/prod/main.tf) and
[Karpenter configuration](../../kubernetes/platform/karpenter/). Complete the
Terraform-output wiring before bootstrap; the AWS path remains unverified live.
