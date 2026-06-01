variable "project" {
  description = "Project name, used in resource naming"
  type        = string
  default     = "platform-core"
}

variable "environment" {
  description = "Environment name: dev | prod"
  type        = string
}

variable "kubernetes_version" {
  description = "Kubernetes version for the EKS cluster"
  type        = string
  default     = "1.29"
}

variable "vpc_id" {
  description = "VPC ID in which the cluster is created"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for worker nodes"
  type        = list(string)
}

variable "allowed_cidrs" {
  description = "CIDRs allowed to reach the public API endpoint"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "node_instance_types" {
  description = "EC2 instance types for the default node group"
  type        = list(string)
  default     = ["m5.large"]
}

variable "node_min" {
  type    = number
  default = 2
}

variable "node_max" {
  type    = number
  default = 5
}

variable "node_desired" {
  type    = number
  default = 2
}

variable "enable_gpu_nodegroup" {
  description = "Whether to provision the static GPU managed node group for vLLM. Leave false when Karpenter manages GPU capacity instead."
  type        = bool
  default     = false
}

variable "enable_karpenter" {
  description = "Whether to provision Karpenter (controller IAM role, node role, SQS interruption queue). Prod only; stays false on local kind."
  type        = bool
  default     = false
}

variable "gpu_instance_type" {
  description = "EC2 GPU instance type for vLLM (e.g. g4dn.xlarge)"
  type        = string
  default     = "g4dn.xlarge"
}
