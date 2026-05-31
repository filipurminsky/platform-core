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
  subnet_ids = var.private_subnet_ids

  # Allow kubectl from the VPC (adjust for bastion / VPN CIDR)
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
      instance_types = var.node_instance_types
      min_size       = var.node_min
      max_size       = var.node_max
      desired_size   = var.node_desired

      labels = {
        role = "platform"
      }
    }
  }

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

# ─── IRSA — shared OIDC provider ─────────────────────────────────────────────
data "aws_iam_openid_connect_provider" "eks" {
  url = module.eks.cluster_oidc_issuer_url
}
