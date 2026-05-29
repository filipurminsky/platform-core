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
