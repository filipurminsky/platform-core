# GitHub Actions OIDC → AWS (keyless ECR push). Replaces static AWS keys in CI.
# aws_region must match AWS_REGION in .github/workflows/docker-build.yaml.
module "github_oidc" {
  source      = "../../modules/github-oidc"
  project     = "platform-core"
  github_repo = "filipurminsky/platform-core"
  aws_region  = var.aws_region
  # Hardened: only the main branch can assume this role (prevents PR/branch pushes)
  subject_filter = "ref:refs/heads/main"
}

output "github_actions_role_arn" {
  description = "Set as repo variable AWS_OIDC_ROLE_ARN (gh variable set ...)."
  value       = module.github_oidc.role_arn
}
