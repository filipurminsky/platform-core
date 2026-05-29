# Module: github-oidc

Creates a GitHub Actions OIDC provider and a least-privilege IAM role that CI
assumes to push images to ECR — **no long-lived AWS keys in GitHub**.

## Usage

```hcl
module "github_oidc" {
  source      = "../../modules/github-oidc"
  project     = "platform-core"
  github_repo = "filipurminsky/platform-core"
  aws_region  = "eu-west-1"
  # Optional hardening: only trust the main branch
  # subject_filter = "ref:refs/heads/main"
}

output "github_actions_role_arn" {
  value = module.github_oidc.role_arn
}
```

After `terraform apply`, set the output as a **repository variable** (not secret):

```bash
gh variable set AWS_OIDC_ROLE_ARN \
  --body "$(terraform output -raw github_actions_role_arn)"
```

`.github/workflows/docker-build.yaml` consumes it via
`role-to-assume: ${{ vars.AWS_OIDC_ROLE_ARN }}` with `permissions: id-token: write`.

## Notes

- The IAM policy is scoped to `arn:aws:ecr:<region>:<account>:repository/<project>/*`.
- `GetAuthorizationToken` cannot be resource-scoped (AWS limitation) and is the
  only `*`-resource action.
- Set `create_oidc_provider = false` + `oidc_provider_arn` if the account already
  has the GitHub provider.
