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

  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids

  kubernetes_version   = "1.29"
  node_instance_types  = ["m5.xlarge"]
  node_min             = 3
  node_max             = 8
  node_desired         = 3

  enable_gpu_nodegroup = true           # GPU node group for Mistral-7B vLLM
  gpu_instance_type    = "g4dn.xlarge"  # NVIDIA T4
}

module "iam" {
  source         = "../../modules/iam"
  project        = "platform-core"
  environment    = "prod"
  aws_region     = var.aws_region
  aws_account_id = data.aws_caller_identity.current.account_id
  cluster_name   = module.eks.cluster_name
  oidc_provider_arn = module.eks.oidc_provider_arn
}

data "aws_caller_identity" "current" {}

variable "aws_region" {
  type    = string
  default = "eu-west-1"
}

output "cluster_name"     { value = module.eks.cluster_name }
output "cluster_endpoint" { value = module.eks.cluster_endpoint }
