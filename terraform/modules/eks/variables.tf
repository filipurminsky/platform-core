variable "project" {
  description = "Project name, used in resource naming"
  type        = string
  default     = "platform-core"
}

variable "environment" {
  description = "Environment name: dev | prod"
  type        = string

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be dev or prod."
  }
}

variable "kubernetes_version" {
  description = "Kubernetes minor version for the EKS control plane + node groups. EKS upgrades one minor at a time — see the upgrade notes in main.tf."
  type        = string
  # No default: every environment pins it explicitly so dev/prod can't drift
  # silently onto whatever the module default happens to be.
}

variable "vpc_id" {
  description = "VPC ID in which the cluster is created"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs (one per AZ) for the control-plane ENIs, managed node groups and Karpenter nodes. Egress via NAT; nodes never get public IPs."
  type        = list(string)
}

# ─── API endpoint exposure ───────────────────────────────────────────────────
# The private endpoint is always on (nodes + in-VPC tooling talk to the API over
# the VPC). The public endpoint is opt-in and must be fenced to explicit CIDRs.
variable "endpoint_public_access" {
  description = "Expose the Kubernetes API on a public endpoint (restricted to allowed_cidrs). Prod should keep this false and reach the API from inside the VPC (SSM/VPN)."
  type        = bool
  default     = false
}

variable "allowed_cidrs" {
  description = "CIDRs allowed to reach the PUBLIC API endpoint. Required (non-empty) when endpoint_public_access = true; 0.0.0.0/0 is rejected in prod."
  type        = list(string)
  default     = []

  validation {
    condition     = !var.endpoint_public_access || length(var.allowed_cidrs) > 0
    error_message = "endpoint_public_access = true requires an explicit, non-empty allowed_cidrs list (e.g. your office/VPN egress /32)."
  }

  validation {
    condition     = var.environment != "prod" || !contains(var.allowed_cidrs, "0.0.0.0/0")
    error_message = "The prod EKS API endpoint must not be open to 0.0.0.0/0."
  }

  validation {
    condition     = alltrue([for c in var.allowed_cidrs : can(cidrnetmask(c))])
    error_message = "allowed_cidrs must be valid IPv4 CIDRs."
  }
}

# ─── Platform managed node group ─────────────────────────────────────────────
variable "node_instance_types" {
  description = "EC2 instance types for the platform managed node group"
  type        = list(string)
  default     = ["m5.large"]
}

variable "node_capacity_type" {
  description = "Capacity type of the platform node group. ON_DEMAND in prod: it hosts the Karpenter controller, ArgoCD and the Kafka brokers, which must not be reclaimed by a Spot interruption. Burst capacity is Spot via Karpenter's default NodePool."
  type        = string
  default     = "ON_DEMAND"

  validation {
    condition     = contains(["ON_DEMAND", "SPOT"], var.node_capacity_type)
    error_message = "node_capacity_type must be ON_DEMAND or SPOT."
  }
}

variable "node_min" {
  description = "Minimum size of the platform node group (prod: >= 3, one per AZ)"
  type        = number
  default     = 3
}

variable "node_max" {
  type    = number
  default = 5
}

variable "node_desired" {
  type    = number
  default = 3
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
