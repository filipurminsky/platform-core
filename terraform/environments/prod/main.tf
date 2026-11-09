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
    key          = "prod/terraform.tfstate"
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

  # 3 AZs: Kafka runs 3 brokers with min.insync.replicas=2 and must survive an
  # AZ loss. One NAT gateway per AZ + interface endpoints for ECR/STS/EC2/SQS/Logs.
  az_count                   = 3
  single_nat_gateway         = false
  enable_interface_endpoints = true
}

module "eks" {
  source      = "../../modules/eks"
  project     = "platform-core"
  environment = "prod"

  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids

  # Fleet target. EKS upgrades one minor at a time — an existing 1.31 cluster
  # must be walked 1.32 → 1.33 → 1.34 → 1.35 → 1.36 (see modules/eks/main.tf).
  kubernetes_version = "1.36"

  # Private API endpoint only by default — reach it from inside the VPC (SSM
  # Session Manager port-forward / VPN). Setting api_public_access = true
  # additionally requires explicit api_allowed_cidrs; 0.0.0.0/0 is rejected.
  endpoint_public_access = var.api_public_access
  allowed_cidrs          = var.api_allowed_cidrs

  # Small static ON_DEMAND managed node group for baseline/system capacity
  # (CoreDNS, Karpenter controller, ArgoCD, Kafka brokers) — one node per AZ.
  # Karpenter provisions everything else — Spot app burst capacity and GPU
  # nodes — on demand.
  node_capacity_type  = "ON_DEMAND"
  node_instance_types = ["m5.large"]
  node_min            = 3
  node_max            = 6
  node_desired        = 3

  # GPU is handled by the Karpenter GPU NodePool (kubernetes/platform/karpenter),
  # not a static managed node group — provision-on-demand, scale-to-zero.
  enable_gpu_nodegroup = false
  enable_karpenter     = true
}

module "ecr" {
  source  = "../../modules/ecr"
  project = "platform-core"
  apps    = ["echo-service", "worker-service", "llm-gateway", "audio-api", "stt-worker", "tts-worker", "llm-worker"]
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

variable "api_public_access" {
  description = "Expose the prod EKS API publicly (restricted to api_allowed_cidrs). Default false: private endpoint only."
  type        = bool
  default     = false
}

variable "api_allowed_cidrs" {
  description = "CIDRs allowed to reach the prod public API endpoint when api_public_access = true. 0.0.0.0/0 is rejected."
  type        = list(string)
  default     = []
}

output "cluster_name" { value = module.eks.cluster_name }
output "cluster_endpoint" { value = module.eks.cluster_endpoint }

# Annotate the Crossplane provider-aws-s3 ServiceAccount with this (IRSA):
# kubernetes/platform/crossplane/config/provider.yaml
output "crossplane_s3_role_arn" { value = module.iam.crossplane_s3_role_arn }

# Audio pipeline S3 access (IRSA) — substitute the account id into each
# services/<svc>/k8s/overlays/prod/patch-serviceaccount-irsa.yaml annotation
# (audio-api, stt-worker, llm-worker, tts-worker).
output "audio_pipeline_role_arn" { value = module.iam.audio_pipeline_role_arn }

# Karpenter wiring — substitute into kubernetes/platform/karpenter:
#   karpenter_controller_role_arn → applicationset.yaml serviceAccount IRSA annotation
#   karpenter_interruption_queue  → applicationset.yaml settings.interruptionQueue
#   karpenter_node_iam_role_name  → config/ec2nodeclass.yaml .spec.role
output "karpenter_controller_role_arn" { value = module.eks.karpenter_controller_role_arn }
output "karpenter_node_iam_role_name" { value = module.eks.karpenter_node_iam_role_name }
output "karpenter_interruption_queue" { value = module.eks.karpenter_interruption_queue }

output "ecr_repository_urls" {
  description = "Application ECR repository URLs."
  value       = module.ecr.repository_urls
}
