variable "project" {
  description = "Project name; used for role naming and ECR repo path scoping (platform-core/*)."
  type        = string
  default     = "platform-core"
}

variable "github_repo" {
  description = "GitHub repo in 'owner/name' form, used in the OIDC subject trust condition."
  type        = string
}

variable "subject_filter" {
  description = "Suffix of the OIDC 'sub' claim to trust. Default '*' = any ref/workflow in the repo. Tighten to e.g. 'ref:refs/heads/main' or 'environment:prod'."
  type        = string
  default     = "*"
}

variable "aws_region" {
  description = "Region used to build the ECR resource ARN."
  type        = string
}

variable "create_oidc_provider" {
  description = "Create the GitHub OIDC provider. Set false if one already exists in the account."
  type        = bool
  default     = true
}

variable "oidc_provider_arn" {
  description = "Existing GitHub OIDC provider ARN (used only when create_oidc_provider = false)."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to the IAM role."
  type        = map(string)
  default     = {}
}
