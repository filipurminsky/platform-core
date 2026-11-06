terraform {
  # >= 1.10 for S3-native state locking (use_lockfile).
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.72"
    }
  }

  # State locking uses an S3 lock object (<key>.tflock) instead of the
  # deprecated DynamoDB table. Migration from dynamodb_table: see the note in
  # terraform/backend.tf (run `terraform init -reconfigure` once).
  backend "s3" {
    bucket       = "platform-core-tfstate"
    key          = "dev/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    use_lockfile = true
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

  # Dev trades resilience for cost: 2 AZs, one shared NAT gateway, and only the
  # free S3 gateway endpoint (no ~$7/AZ/month interface endpoints). Kafka in
  # dev is a single broker, so it doesn't need the third AZ prod requires.
  az_count                   = 2
  single_nat_gateway         = true
  enable_interface_endpoints = false
}

module "eks" {
  source      = "../../modules/eks"
  project     = "platform-core"
  environment = "dev"

  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids

  # Fleet target. EKS upgrades one minor at a time — see modules/eks/main.tf.
  kubernetes_version = "1.35"

  # Dev keeps a public API endpoint for laptop kubectl, but only from the
  # explicitly supplied CIDRs (no default — set api_allowed_cidrs in a tfvars
  # file or TF_VAR_api_allowed_cidrs, e.g. ["203.0.113.10/32"]).
  endpoint_public_access = true
  allowed_cidrs          = var.api_allowed_cidrs

  # Spot is acceptable for the dev baseline group (prod is ON_DEMAND).
  node_capacity_type  = "SPOT"
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

variable "api_allowed_cidrs" {
  description = "CIDRs allowed to reach the dev EKS public API endpoint (e.g. your egress IP /32). No default on purpose."
  type        = list(string)
}

output "cluster_name" { value = module.eks.cluster_name }
output "cluster_endpoint" { value = module.eks.cluster_endpoint }

# Annotate the Crossplane provider-aws-s3 ServiceAccount with this (IRSA):
# kubernetes/platform/crossplane/config/provider.yaml
output "crossplane_s3_role_arn" { value = module.iam.crossplane_s3_role_arn }
