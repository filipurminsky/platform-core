locals {
  cluster_name = "${var.project}-${var.environment}"
  common_tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# ─── EKS Cluster ─────────────────────────────────────────────────────────────
# terraform-aws-modules/eks v21 (inputs lost their cluster_ prefix; see
# docs/UPGRADE-21.0.md upstream). No state moves are required by the module
# bump itself.
#
# Upgrading an EXISTING cluster's Kubernetes version: EKS only upgrades one
# minor at a time, so a cluster created on 1.31 must be walked
#   1.31 → 1.32 → 1.33 → 1.34 → 1.35 → 1.36
# by setting kubernetes_version to each intermediate minor and applying in turn
# (control plane first, then the managed node groups roll; Karpenter nodes
# drift-replace onto the new AMI on their own). Before each hop: check the EKS
# upgrade insights (`aws eks list-insights`) for removed APIs, and keep add-ons
# (coredns/kube-proxy/vpc-cni, most_recent = true) in step. AL2 AMIs stop at
# 1.32, which is why every node group here is AL2023.
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.29"

  name               = local.cluster_name
  kubernetes_version = var.kubernetes_version

  vpc_id = var.vpc_id
  # Control-plane ENIs and every node live in the private subnets; egress is via
  # NAT (+ VPC endpoints). Public subnets carry only internet-facing LBs.
  subnet_ids               = var.private_subnet_ids
  control_plane_subnet_ids = var.private_subnet_ids

  # API endpoint: private always; public only when explicitly enabled AND fenced
  # to explicit CIDRs (validated in variables.tf — 0.0.0.0/0 is rejected in prod).
  endpoint_private_access      = true
  endpoint_public_access       = var.endpoint_public_access
  endpoint_public_access_cidrs = var.endpoint_public_access ? var.allowed_cidrs : null

  # Full control-plane logging to CloudWatch. The upstream module defaults to
  # only api/audit/authenticator; we pin the list explicitly (so it can't
  # silently regress on a module bump) and add controllerManager/scheduler for
  # complete audit + forensic coverage.
  enabled_log_types = [
    "api",
    "audit",
    "authenticator",
    "controllerManager",
    "scheduler",
  ]

  # Envelope-encrypt Kubernetes Secrets in etcd with a dedicated KMS key. This
  # matches the module's secure default; pinning it explicitly documents the
  # posture and guards against an upstream default change.
  create_kms_key = true
  encryption_config = {
    resources = ["secrets"]
  }

  # v21 defaults: most_recent = true, resolve_conflicts_on_create = "NONE".
  addons = {
    coredns    = { most_recent = true }
    kube-proxy = { most_recent = true }
    vpc-cni = {
      most_recent = true
      # Enable the aws-network-policy-agent: without it the VPC CNI ignores
      # NetworkPolicy objects entirely, so every policy in this repo (tenant
      # zero-trust, per-service segmentation) would be a silent no-op on EKS.
      # Local kind clusters get enforcement from Cilium instead (bootstrap.sh).
      configuration_values = jsonencode({
        enableNetworkPolicy = "true"
      })
    }
  }

  # Baseline managed node group — hosts the Karpenter controller, ArgoCD,
  # Strimzi/Kafka and other platform operators. ON_DEMAND (prod) so a Spot
  # reclaim can't take out the autoscaler that would replace it, or a Kafka
  # broker; min = one node per AZ. Burst/app capacity is Spot via Karpenter.
  eks_managed_node_groups = {
    platform = {
      ami_type       = "AL2023_x86_64_STANDARD" # AL2 is unsupported on EKS >= 1.33
      capacity_type  = var.node_capacity_type
      instance_types = var.node_instance_types
      subnet_ids     = var.private_subnet_ids
      min_size       = var.node_min
      max_size       = var.node_max
      desired_size   = var.node_desired

      labels = {
        role = "platform"
      }
    }
  }

  # Karpenter discovers the node security group via this tag (matches the
  # securityGroupSelectorTerms in the EC2NodeClass).
  node_security_group_tags = merge(local.common_tags, {
    "karpenter.sh/discovery" = local.cluster_name
  })

  tags = local.common_tags
}

# ─── GPU Node Group (optional, for vLLM prod) ────────────────────────────────
module "gpu_nodegroup" {
  source = "../gpu-nodegroup"
  count  = var.enable_gpu_nodegroup ? 1 : 0

  cluster_name  = module.eks.cluster_name
  node_role_arn = module.eks.eks_managed_node_groups["platform"].iam_role_arn
  subnet_ids    = var.private_subnet_ids

  instance_type = var.gpu_instance_type # default: g4dn.xlarge
  min_size      = 0                     # scale-to-zero when idle
  max_size      = 2
  tags          = local.common_tags
}

# ─── Karpenter (prod node autoscaling) ───────────────────────────────────────
# The community submodule provisions everything Karpenter needs on the AWS side:
# the controller IAM role, the node IAM role + access entry so nodes can join,
# the SQS interruption queue, and the EventBridge rules feeding it. The
# in-cluster controller + NodePools/EC2NodeClass ship via the prod-only
# kubernetes/platform/karpenter ApplicationSet. This replaces the static GPU
# managed node group for vLLM with an on-demand Karpenter GPU NodePool.
#
# IRSA, not Pod Identity: v21 of the submodule dropped its built-in IRSA inputs
# (enable_irsa / irsa_*) and defaults to EKS Pod Identity. We keep the platform-
# wide IRSA model by (a) not creating the pod identity association and (b)
# supplying the IRSA web-identity trust statement ourselves. The role name,
# policy and queue are unchanged, so no state moves. Migrating the whole
# platform to Pod Identity is a separate, deliberate change.
data "aws_iam_policy_document" "karpenter_irsa_trust" {
  count = var.enable_karpenter ? 1 : 0

  statement {
    sid     = "IRSA"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:kube-system:karpenter"]
    }

    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

module "karpenter" {
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"
  version = "~> 21.29"
  count   = var.enable_karpenter ? 1 : 0

  cluster_name = module.eks.cluster_name

  # IRSA (see above). The Helm chart's ServiceAccount is kube-system:karpenter.
  create_pod_identity_association         = false
  iam_role_source_assume_policy_documents = [data.aws_iam_policy_document.karpenter_irsa_trust[0].json]

  # SSM on nodes for break-glass debugging.
  node_iam_role_additional_policies = {
    AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  }

  tags = local.common_tags
}
