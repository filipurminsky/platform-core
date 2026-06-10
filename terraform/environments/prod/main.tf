terraform {
  required_version = ">= 1.6.0"

  backend "s3" {
    bucket         = "platform-core-tfstate"
    key            = "prod/terraform.tfstate"
    region         = "eu-west-1"
    encrypt        = true
    dynamodb_table = "platform-core-tflock"
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project     = "platform-core"
      Environment = "prod"
      ManagedBy   = "terraform"
    }
  }
}

module "networking" {
  source      = "../../modules/networking"
  project     = "platform-core"
  environment = "prod"
  vpc_cidr    = "10.1.0.0/16"
}

module "eks" {
  source      = "../../modules/eks"
  project     = "platform-core"
  environment = "prod"

  vpc_id            = module.networking.vpc_id
  public_subnet_ids = module.networking.public_subnet_ids

  kubernetes_version = "1.31"

  # Small static managed node group for baseline/system capacity (CoreDNS,
  # Karpenter controller, platform operators). Karpenter provisions everything
  # else — app burst capacity and GPU nodes — on demand.
  node_instance_types = ["m5.large"]
  node_min            = 2
  node_max            = 3
  node_desired        = 2

  # GPU is handled by the Karpenter GPU NodePool (kubernetes/platform/karpenter),
  # not a static managed node group — provision-on-demand, scale-to-zero.
  enable_gpu_nodegroup = false
  enable_karpenter     = true
}

module "iam" {
  source            = "../../modules/iam"
  project           = "platform-core"
  environment       = "prod"
  aws_region        = var.aws_region
  aws_account_id    = data.aws_caller_identity.current.account_id
  cluster_name      = module.eks.cluster_name
  oidc_provider_arn = module.eks.oidc_provider_arn
}

data "aws_caller_identity" "current" {}

variable "aws_region" {
  type    = string
  default = "eu-west-1"
}

output "cluster_name" { value = module.eks.cluster_name }
output "cluster_endpoint" { value = module.eks.cluster_endpoint }

# Annotate the Crossplane provider-aws-s3 ServiceAccount with this (IRSA):
# kubernetes/platform/crossplane/config/provider.yaml
output "crossplane_s3_role_arn" { value = module.iam.crossplane_s3_role_arn }

# Karpenter wiring — substitute into kubernetes/platform/karpenter:
#   karpenter_controller_role_arn → applicationset.yaml serviceAccount IRSA annotation
#   karpenter_interruption_queue  → applicationset.yaml settings.interruptionQueue
#   karpenter_node_iam_role_name  → config/ec2nodeclass.yaml .spec.role
output "karpenter_controller_role_arn" { value = module.eks.karpenter_controller_role_arn }
output "karpenter_node_iam_role_name" { value = module.eks.karpenter_node_iam_role_name }
output "karpenter_interruption_queue" { value = module.eks.karpenter_interruption_queue }
