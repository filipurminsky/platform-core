locals {
  name = "${var.project}-${var.environment}"
  azs  = slice(data.aws_availability_zones.available.names, 0, var.az_count)

  # Derived subnet layout for a /16:
  #   private: /19 per AZ at the bottom of the range (10.x.0.0/19, 10.x.32.0/19,
  #            10.x.64.0/19) — the VPC CNI hands every pod a VPC IP, so node
  #            subnets need room (/24s exhaust quickly with prefix delegation).
  #   public:  /24 per AZ at 10.x.101+i.0 — only ALB/NLB ENIs and NAT gateways
  #            live here. Matches the historical public CIDRs.
  private_subnet_cidrs = length(var.private_subnet_cidrs) > 0 ? var.private_subnet_cidrs : [
    for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 3, i)
  ]
  public_subnet_cidrs = length(var.public_subnet_cidrs) > 0 ? var.public_subnet_cidrs : [
    for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 8, 101 + i)
  ]

  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.7"

  name = local.name
  cidr = var.vpc_cidr

  azs             = local.azs
  private_subnets = local.private_subnet_cidrs
  public_subnets  = local.public_subnet_cidrs

  # Nodes live in the private subnets and egress through NAT. Prod gets one NAT
  # per AZ so an AZ outage can't take out egress for the surviving AZs; dev
  # shares a single NAT to keep cost down.
  enable_nat_gateway     = true
  single_nat_gateway     = var.single_nat_gateway
  one_nat_gateway_per_az = !var.single_nat_gateway

  enable_dns_hostnames = true
  enable_dns_support   = true

  # Nothing launched into the public subnets should get a public IP implicitly
  # (load balancers / NAT gateways get their EIPs explicitly).
  map_public_ip_on_launch = false

  # Private subnets: internal load balancers + worker nodes. Karpenter discovers
  # which subnets to launch nodes into via karpenter.sh/discovery (matches
  # subnetSelectorTerms in kubernetes/platform/karpenter/config/ec2nodeclass.yaml).
  # Harmless where Karpenter isn't deployed (dev).
  private_subnet_tags = {
    "kubernetes.io/cluster/${local.name}" = "shared"
    "kubernetes.io/role/internal-elb"     = "1"
    "karpenter.sh/discovery"              = local.name
  }

  # Public subnets: internet-facing load balancers only.
  public_subnet_tags = {
    "kubernetes.io/cluster/${local.name}" = "shared"
    "kubernetes.io/role/elb"              = "1"
  }

  tags = local.tags
}

data "aws_availability_zones" "available" {
  #checkov:skip=CKV_AWS_394:Only the first az_count names are used (slice 0..az_count, sorted); AWS appending a new AZ cannot change that selection, and the opt-in filter below excludes Local/Wavelength Zones.
  state = "available"
  # Standard AZs only — never pick up opt-in Local/Wavelength Zones, which lack
  # most EKS/NAT/endpoint services and would break subnet placement.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

# ─── VPC endpoints ───────────────────────────────────────────────────────────
# Keep AWS API + image-pull traffic off the NAT gateways (cost + no internet
# path for node bootstrap). S3 is a free gateway endpoint (ECR image layers are
# served from S3); the interface endpoints cover image pulls (ECR api/dkr),
# IRSA token exchange (STS), Karpenter (EC2 + SQS interruption queue) and
# CloudWatch Logs.
resource "aws_security_group" "vpc_endpoints" {
  #checkov:skip=CKV2_AWS_5:Attached to the interface endpoints via module.vpc_endpoints (security_group_ids) — Checkov can't trace module inputs.
  count = var.enable_interface_endpoints ? 1 : 0

  name_prefix = "${local.name}-vpce-"
  description = "HTTPS from inside the VPC to interface VPC endpoints"
  vpc_id      = module.vpc.vpc_id

  tags = merge(local.tags, { Name = "${local.name}-vpce" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "vpc_endpoints_https" {
  count = var.enable_interface_endpoints ? 1 : 0

  security_group_id = aws_security_group.vpc_endpoints[0].id
  description       = "HTTPS from the VPC"
  cidr_ipv4         = var.vpc_cidr
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

module "vpc_endpoints" {
  source  = "terraform-aws-modules/vpc/aws//modules/vpc-endpoints"
  version = "~> 6.7"

  vpc_id = module.vpc.vpc_id

  security_group_ids = aws_security_group.vpc_endpoints[*].id
  subnet_ids         = module.vpc.private_subnets

  endpoints = merge(
    {
      s3 = {
        service         = "s3"
        service_type    = "Gateway"
        route_table_ids = module.vpc.private_route_table_ids
        tags            = { Name = "${local.name}-s3" }
      }
    },
    var.enable_interface_endpoints ? {
      for svc in ["ecr.api", "ecr.dkr", "sts", "ec2", "sqs", "logs"] : svc => {
        service             = svc
        private_dns_enabled = true
        tags                = { Name = "${local.name}-${replace(svc, ".", "-")}" }
      }
    } : {}
  )

  tags = local.tags
}
