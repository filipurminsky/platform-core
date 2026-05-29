output "role_arn" {
  description = "IAM role ARN for GitHub Actions to assume. Set as repo variable AWS_OIDC_ROLE_ARN."
  value       = aws_iam_role.ci.arn
}

output "oidc_provider_arn" {
  description = "ARN of the GitHub OIDC provider (created or referenced)."
  value       = local.oidc_provider_arn
}
