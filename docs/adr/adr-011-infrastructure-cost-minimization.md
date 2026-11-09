# ADR-011: Cost and network posture

**Status:** The original public-node/all-Spot proposal is superseded by the
current Terraform configuration and [ADR-018](adr-018-just-in-time-node-provisioning-karpenter.md).
The AWS design has not been verified on a live account.

## Current decision

- Nodes run in **private subnets**. Prod defaults to a private-only EKS API;
  optional public access requires explicit restricted CIDRs.
- Prod keeps an **on-demand platform node group** across three AZs. Karpenter
  handles application burst and GPU capacity; its controller must not depend
  on capacity that it needs to provision itself.
- Prod uses a NAT gateway per AZ and interface endpoints. The authored dev
  Terraform configuration uses two AZs, a shared NAT gateway, and the S3 gateway
  endpoint. These are deliberate cost/resilience differences.
- GPU workloads scale down when idle, but the EKS control plane, baseline
  nodes, networking, storage, and other resources continue to incur charges.

The authoritative implementation is
[`terraform/environments/prod/main.tf`](../../terraform/environments/prod/main.tf),
[`terraform/environments/dev/main.tf`](../../terraform/environments/dev/main.tf),
and [`kubernetes/platform/karpenter/`](../../kubernetes/platform/karpenter/).

## Historical proposal and why it changed

The original showcase proposal put all nodes on Spot in public subnets to avoid
NAT costs. It also included rough monthly totals and cold-start estimates that
were not measured. Those estimates are withdrawn; they are not a budget for the
current topology. Public node placement and all-Spot baseline capacity no
longer describe the implementation.

Private placement and stable controller capacity add baseline cost, but make
network boundaries and recovery dependencies explicit. Local kind is the
zero-AWS-cost demonstration path. A future AWS exercise should publish its
region, instance mix, elapsed runtime, actual bill, and residual-resource audit.
