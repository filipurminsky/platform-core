variable "project" { type = string }
variable "environment" { type = string }

variable "vpc_cidr" {
  description = "VPC CIDR. Subnet CIDRs are derived from it (see locals in main.tf) unless overridden."
  type        = string
  default     = "10.0.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones to spread subnets (and nodes) across. Prod needs 3: Kafka runs 3 brokers with min.insync.replicas=2, so it must survive the loss of one AZ."
  type        = number
  default     = 3

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}

variable "private_subnet_cidrs" {
  description = "Private (node/pod) subnet CIDRs, one per AZ. Empty = derive /19s from vpc_cidr (VPC CNI assigns pod IPs from these, so they must be large)."
  type        = list(string)
  default     = []
}

variable "public_subnet_cidrs" {
  description = "Public (load balancer + NAT only) subnet CIDRs, one per AZ. Empty = derive x.x.101+i.0/24 from vpc_cidr."
  type        = list(string)
  default     = []
}

variable "single_nat_gateway" {
  description = "true = one shared NAT gateway (cheap; dev). false = one NAT gateway per AZ (prod: no cross-AZ egress dependency, survives an AZ outage)."
  type        = bool
  default     = false
}

variable "enable_interface_endpoints" {
  description = "Create interface VPC endpoints (ECR api/dkr, STS, EC2, SQS, CloudWatch Logs). Each costs ~$7/month per AZ; the S3 gateway endpoint is free and always created."
  type        = bool
  default     = true
}
