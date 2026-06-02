locals {
  cluster_name = "${var.project}-${var.environment}"
  common_tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# ─── EKS Cluster ─────────────────────────────────────────────────────────────
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = local.cluster_name
  cluster_version = var.kubernetes_version

  vpc_id     = var.vpc_id
  subnet_ids = var.public_subnet_ids # Use public subnets for all nodes

  # Allow kubectl from the internet (for a showcase project)
  cluster_endpoint_public_access       = true
  cluster_endpoint_public_access_cidrs = var.allowed_cidrs

  cluster_addons = {
    coredns    = { most_recent = true }
    kube-proxy = { most_recent = true }
    vpc-cni    = { most_recent = true }
  }

  # Default managed node group — general workloads
  eks_managed_node_groups = {
    platform = {
      capacity_type  = "SPOT"
      instance_types = var.node_instance_types
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
  subnet_ids    = var.public_subnet_ids # Use public subnets

  instance_type = var.gpu_instance_type # default: g4dn.xlarge
  min_size      = 0                     # scale-to-zero when idle
  max_size      = 2
  tags          = local.common_tags
}

# ─── Karpenter (prod node autoscaling) ───────────────────────────────────────
# The community submodule provisions everything Karpenter needs on the AWS side:
# the controller IAM role (IRSA), the node IAM role + access entry so nodes can
# join, the SQS interruption queue, and the EventBridge rules feeding it. The
# in-cluster controller + NodePools/EC2NodeClass ship via the prod-only
# kubernetes/platform/karpenter ApplicationSet. This replaces the static GPU
# managed node group for vLLM with an on-demand Karpenter GPU NodePool.
module "karpenter" {
  source  = "terraform-aws-modules/eks/aws//modules/karpenter"
  version = "~> 20.0"
  count   = var.enable_karpenter ? 1 : 0

  cluster_name = module.eks.cluster_name

  enable_v1_permissions = true # Karpenter v1.0+ controller policy

  # Use IRSA (cluster OIDC) like the rest of the platform, not Pod Identity.
  # The Helm chart's ServiceAccount is kube-system:karpenter.
  enable_pod_identity             = false
  create_pod_identity_association = false
  enable_irsa                     = true
  irsa_oidc_provider_arn          = data.aws_iam_openid_connect_provider.eks.arn
  irsa_namespace_service_accounts = ["kube-system:karpenter"]

  # SSM on nodes for break-glass debugging.
  node_iam_role_additional_policies = {
    AmazonSSMManagedInstanceCore = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
  }

  tags = local.common_tags
}

# ─── IRSA — shared OIDC provider ─────────────────────────────────────────────
data "aws_iam_openid_connect_provider" "eks" {
  url = module.eks.cluster_oidc_issuer_url
}
