variable "project" {
  description = "Project prefix for ECR repository names."
  type        = string
  default     = "platform-core"
}

variable "apps" {
  description = "List of application names to create ECR repositories for."
  type        = list(string)
}

variable "tags" {
  description = "Tags applied to all resources."
  type        = map(string)
  default     = {}
}
