locals {
  name = "${var.project}-${var.environment}"
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = local.name
  cidr = var.vpc_cidr

  azs             = slice(data.aws_availability_zones.available.names, 0, 2)
  private_subnets = var.private_subnet_cidrs
  public_subnets  = var.public_subnet_cidrs

  enable_nat_gateway   = true
  single_nat_gateway   = var.environment == "dev" # cost optimisation in dev
  enable_dns_hostnames = true
  enable_dns_support   = true

  # Required tags for EKS to discover subnets
  private_subnet_tags = {
    "kubernetes.io/cluster/${local.name}" = "shared"
    "kubernetes.io/role/internal-elb"     = "1"
    # Karpenter discovers which subnets to launch nodes in via this tag
    # (matches subnetSelectorTerms in the EC2NodeClass). Harmless where
    # Karpenter isn't deployed (e.g. dev).
    "karpenter.sh/discovery" = local.name
  }

  public_subnet_tags = {
    "kubernetes.io/cluster/${local.name}" = "shared"
    "kubernetes.io/role/elb"              = "1"
  }

  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}
