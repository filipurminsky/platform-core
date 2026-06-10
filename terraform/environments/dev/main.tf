terraform {
  required_version = ">= 1.6.0"

  backend "s3" {
    bucket         = "platform-core-tfstate"
    key            = "dev/terraform.tfstate"
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
      Environment = "dev"
      ManagedBy   = "terraform"
    }
  }
}

module "networking" {
  source      = "../../modules/networking"
  project     = "platform-core"
  environment = "dev"
  vpc_cidr    = "10.0.0.0/16"
}

module "eks" {
  source      = "../../modules/eks"
  project     = "platform-core"
  environment = "dev"

  vpc_id            = module.networking.vpc_id
  public_subnet_ids = module.networking.public_subnet_ids

  kubernetes_version  = "1.31"
  node_instance_types = ["m5.large"]
  node_min            = 2
  node_max            = 4
  node_desired        = 2

  enable_gpu_nodegroup = false # GPU disabled in dev — use CPU vLLM model
}

module "iam" {
  source            = "../../modules/iam"
  project           = "platform-core"
  environment       = "dev"
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
