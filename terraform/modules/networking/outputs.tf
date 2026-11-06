output "vpc_id" { value = module.vpc.vpc_id }
output "azs" { value = local.azs }

# Worker nodes (managed node groups + Karpenter) and the EKS control-plane ENIs.
output "private_subnet_ids" { value = module.vpc.private_subnets }

# Internet-facing load balancers and NAT gateways only — never nodes.
output "public_subnet_ids" { value = module.vpc.public_subnets }
