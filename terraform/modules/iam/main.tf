# IRSA roles — pod-level AWS permissions without static credentials
# Pattern: each service account gets its own role with least-privilege policy

# ─── External Secrets Operator ───────────────────────────────────────────────
module "irsa_external_secrets" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "${var.project}-${var.environment}-external-secrets"

  oidc_providers = {
    main = {
      provider_arn               = var.oidc_provider_arn
      namespace_service_accounts = ["external-secrets:external-secrets"]
    }
  }

  role_policy_arns = {
    SecretsManager = aws_iam_policy.external_secrets.arn
  }
}

resource "aws_iam_policy" "external_secrets" {
  name        = "${var.project}-${var.environment}-external-secrets"
  description = "Allow External Secrets Operator to read Secrets Manager"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      Resource = "arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:${var.project}/${var.environment}/*"
    }]
  })
}

# ─── Cluster Autoscaler ───────────────────────────────────────────────────────
module "irsa_cluster_autoscaler" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name                        = "${var.project}-${var.environment}-cluster-autoscaler"
  attach_cluster_autoscaler_policy = true
  cluster_autoscaler_cluster_names = [var.cluster_name]

  oidc_providers = {
    main = {
      provider_arn               = var.oidc_provider_arn
      namespace_service_accounts = ["kube-system:cluster-autoscaler"]
    }
  }
}

# ─── Crossplane provider-aws (S3) ─────────────────────────────────────────────
# IRSA for the Crossplane AWS S3 provider. The provider's controller pod runs
# as ServiceAccount crossplane-system:provider-aws-s3 (pinned via a
# DeploymentRuntimeConfig in kubernetes/platform/crossplane/config/). This role
# lets Crossplane manage S3 buckets it provisions on behalf of platform users —
# scoped to the project/env bucket prefix so it can't touch unrelated buckets.
module "irsa_crossplane_s3" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "${var.project}-${var.environment}-crossplane-s3"

  oidc_providers = {
    main = {
      provider_arn               = var.oidc_provider_arn
      namespace_service_accounts = ["crossplane-system:provider-aws-s3"]
    }
  }

  role_policy_arns = {
    S3 = aws_iam_policy.crossplane_s3.arn
  }
}

resource "aws_iam_policy" "crossplane_s3" {
  name        = "${var.project}-${var.environment}-crossplane-s3"
  description = "Allow Crossplane to manage S3 buckets under the ${var.project}-${var.environment}- prefix"

  # Every action here is resource-scopable to the bucket ARN, so the policy
  # carries no wildcard Resource. The Upbound S3 provider reconciles buckets by
  # name (Create/Delete + Get/Put of bucket sub-configs: versioning, encryption,
  # public-access-block), so it never needs the account-level s3:ListAllMyBuckets.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "ManageProjectBuckets"
      Effect = "Allow"
      Action = [
        "s3:CreateBucket",
        "s3:DeleteBucket",
        "s3:ListBucket",
        "s3:GetBucketLocation",
        "s3:GetBucket*",
        "s3:PutBucket*",
      ]
      Resource = "arn:aws:s3:::${var.project}-${var.environment}-*"
    }]
  })
}

# ─── ECR pull (worker nodes already have this via node role,  ─────────────────
#     but explicit IRSA is cleaner for restricted nodes) ────────────────────────
module "irsa_ecr_pull" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "${var.project}-${var.environment}-ecr-pull"

  oidc_providers = {
    main = {
      provider_arn               = var.oidc_provider_arn
      namespace_service_accounts = ["apps:default"]
    }
  }

  role_policy_arns = {
    ECRReadOnly = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
  }
}
