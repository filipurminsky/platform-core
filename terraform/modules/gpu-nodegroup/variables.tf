variable "cluster_name" { type = string }
variable "node_role_arn" { type = string }
variable "subnet_ids" {
  description = "PRIVATE subnet IDs for the GPU nodes (egress via NAT, no public IPs)"
  type        = list(string)
}

variable "instance_type" {
  type    = string
  default = "g4dn.xlarge"
}

variable "min_size" {
  type    = number
  default = 0
}

variable "max_size" {
  type    = number
  default = 2
}

variable "tags" {
  type    = map(string)
  default = {}
}
