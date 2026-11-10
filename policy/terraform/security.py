#!/usr/bin/env python3
"""Blocking source-wiring and evaluated-plan guards; no AWS calls or Terraform apply."""

import argparse
import ipaddress
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = (
    "terraform/environments/dev/main.tf",
    "terraform/environments/prod/main.tf",
    "terraform/modules/eks/main.tf",
    "terraform/modules/eks/variables.tf",
    "terraform/modules/networking/main.tf",
    "kubernetes/platform/karpenter/config/ec2nodeclass.yaml",
    ".trivyignore.yaml",
)


def load_source(root=ROOT):
    documents = {}
    for filename in SOURCES:
        parser = "hcl2" if filename.endswith(".tf") else "yaml"
        result = subprocess.run(
            ["conftest", "parse", "--parser", parser, str(root / filename)],
            check=True,
            capture_output=True,
            text=True,
        )
        documents[filename] = json.loads(result.stdout)
    return documents


def source_errors(documents):
    """Pin reviewed module wiring, not a general-purpose HCL evaluator.

    Registry resource behavior is separately checked on Terraform's plan JSON.
    Missing blocks/changed interfaces fail closed and require a policy review.
    """
    errors = []

    def require(condition, code, detail):
        if not condition:
            errors.append(f"{code}: {detail}")

    try:
        eks = documents["terraform/modules/eks/main.tf"]["module"]["eks"][0]
        variables = documents["terraform/modules/eks/variables.tf"]["variable"]
        vpc = documents["terraform/modules/networking/main.tf"]["module"]["vpc"][0]
        prod = documents["terraform/environments/prod/main.tf"]
        require(
            prod["variable"]["api_public_access"][0].get("default") is False,
            "API_DEFAULT",
            "prod API must default to private-only",
        )
        require(
            prod["variable"]["api_allowed_cidrs"][0].get("default") == [],
            "API_CIDRS",
            "prod must not default to a public CIDR allowlist",
        )
        require(
            variables["endpoint_public_access"][0].get("default") is False,
            "API_DEFAULT",
            "shared EKS module must default to private-only",
        )
        require(
            variables["allowed_cidrs"][0].get("default") == [],
            "API_CIDRS",
            "shared module must not default to public CIDRs",
        )
        conditions = {v["condition"] for v in variables["allowed_cidrs"][0]["validation"]}
        required_guards = {
            "${!var.endpoint_public_access || length(var.allowed_cidrs) > 0}",
            '${var.environment != "prod" || !contains(var.allowed_cidrs, "0.0.0.0/0")}',
            "${alltrue([for c in var.allowed_cidrs : can(cidrnetmask(c))])}",
        }
        require(
            required_guards <= conditions, "API_CIDRS", "reviewed CIDR validation guards missing"
        )
        require(
            eks.get("endpoint_private_access") is True,
            "API_PRIVATE",
            "private API must remain enabled",
        )
        require(
            eks.get("endpoint_public_access") == "${var.endpoint_public_access}"
            and eks.get("endpoint_public_access_cidrs")
            == "${var.endpoint_public_access ? var.allowed_cidrs : null}",
            "API_WIRING",
            "public API must use the validated inputs",
        )
        for env in ("dev", "prod"):
            config = documents[f"terraform/environments/{env}/main.tf"]
            module = config["module"]["eks"][0]
            require(
                module.get("private_subnet_ids") == "${module.networking.private_subnet_ids}",
                "PRIVATE_NODES",
                f"{env} must pass the networking private subnets",
            )
            require(
                module.get("allowed_cidrs") == "${var.api_allowed_cidrs}",
                "API_WIRING",
                f"{env} must pass its reviewed CIDR input",
            )
        require(
            prod["module"]["eks"][0].get("endpoint_public_access") == "${var.api_public_access}",
            "API_DEFAULT",
            "prod must use its private-default API input",
        )
        for field in ("subnet_ids", "control_plane_subnet_ids"):
            require(
                eks.get(field) == "${var.private_subnet_ids}",
                "PRIVATE_NODES",
                f"EKS {field} must be private",
            )
        groups = eks.get("eks_managed_node_groups", {})
        require(bool(groups), "NODE_GROUPS", "no managed node groups found")
        for name, group in groups.items():
            require(
                group.get("subnet_ids") == "${var.private_subnet_ids}",
                "PRIVATE_NODES",
                f"{name} must use private subnets",
            )
            require(
                group.get("create_launch_template", True) is True
                and group.get("use_custom_launch_template", True) is True
                and not group.get("launch_template_id"),
                "IMDS",
                f"{name} must use the module-managed launch template to enforce metadata options",
            )
            metadata = group.get("metadata_options", {})
            require(
                metadata.get("http_tokens") == "required"
                and metadata.get("http_put_response_hop_limit") == 1,
                "IMDS",
                f"{name} must explicitly require IMDSv2 and hop limit 1",
            )
        gpu = documents["terraform/modules/eks/main.tf"]["module"]["gpu_nodegroup"][0]
        require(
            gpu.get("subnet_ids") == "${var.private_subnet_ids}",
            "PRIVATE_NODES",
            "optional GPU group must use private subnets",
        )
        require(
            vpc.get("map_public_ip_on_launch") is False,
            "PUBLIC_IP",
            "subnets must not auto-assign public IPs",
        )
        require(
            vpc.get("enable_nat_gateway") is True,
            "PRIVATE_EGRESS",
            "private nodes require configured NAT egress",
        )
        require(
            "karpenter.sh/discovery" in vpc.get("private_subnet_tags", {})
            and "karpenter.sh/discovery" not in vpc.get("public_subnet_tags", {}),
            "KARPENTER_SUBNETS",
            "only private subnets may carry Karpenter discovery tags",
        )
        nodeclass = documents["kubernetes/platform/karpenter/config/ec2nodeclass.yaml"]["spec"]
        metadata = nodeclass.get("metadataOptions", {})
        require(
            metadata.get("httpTokens") == "required"
            and metadata.get("httpPutResponseHopLimit") == 1,
            "IMDS",
            "Karpenter must require IMDSv2 and hop limit 1",
        )
        require(
            nodeclass.get("subnetSelectorTerms")
            == [{"tags": {"karpenter.sh/discovery": "platform-core-prod"}}],
            "KARPENTER_SUBNETS",
            "Karpenter subnet selection must use the private discovery tag",
        )
        exemptions = documents[".trivyignore.yaml"].get("misconfigurations", [])
        require(
            not any(e.get("id") in {"AVD-AWS-0164", "AVD-AWS-0130"} for e in exemptions),
            "SECURITY_EXEMPTION",
            "public-IP and IMDS checks must not be exempted",
        )
    except (KeyError, TypeError, IndexError) as exc:
        errors.append(f"SOURCE_SHAPE: missing or changed security input: {exc}")
    return errors


def resources(module):
    yield from module.get("resources", [])
    for child in module.get("child_modules", []):
        yield from resources(child)


def plan_errors(plan):
    """Inspect expanded post-plan resources, including nested registry modules.

    Unknown required security values fail closed. Node subnet placement is also
    guarded at source; generated subnet IDs can legitimately be unknown on create.
    """
    errors = []
    try:
        planned = list(resources(plan["planned_values"]["root_module"]))
    except KeyError, TypeError:
        return ["PLAN_SHAPE: expected terraform show -json output with planned_values.root_module"]
    if not any(r.get("type") == "aws_eks_cluster" for r in planned):
        errors.append("PLAN_COVERAGE: no EKS cluster found; cannot establish foundation posture")
    for resource in planned:
        kind = resource.get("type")
        value = resource.get("values") or {}
        address = resource.get("address", "unknown")

        def fail(code, message, address=address):
            errors.append(f"{code}: {address}: {message}")

        if kind == "aws_eks_cluster":
            config = (value.get("vpc_config") or [{}])[0]
            if config.get("endpoint_private_access") is not True:
                fail("API_PRIVATE", "private API access is disabled or unknown")
            public = config.get("endpoint_public_access")
            if public not in (True, False):
                fail("API_PUBLIC", "public API access is unknown")
            elif public:
                cidrs = config.get("public_access_cidrs")
                try:
                    networks = [ipaddress.ip_network(c, strict=False) for c in cidrs]
                    if not networks or any(n.prefixlen == 0 for n in networks):
                        raise ValueError("unrestricted")
                    # Also reject split catch-all lists such as two IPv4 /1s.
                    for version in (4, 6):
                        collapsed = ipaddress.collapse_addresses(
                            n for n in networks if n.version == version
                        )
                        if any(n.prefixlen == 0 for n in collapsed):
                            raise ValueError("unrestricted aggregate")
                except TypeError, ValueError:
                    fail("API_CIDRS", "public API requires known, restricted CIDRs")
        if kind == "aws_subnet" and value.get("map_public_ip_on_launch") is not False:
            fail("PUBLIC_IP", "subnet public-IP assignment must be false, not unknown")
        if kind in {"aws_launch_template", "aws_instance"}:
            metadata = (value.get("metadata_options") or [{}])[0]
            if (
                metadata.get("http_tokens") != "required"
                or metadata.get("http_put_response_hop_limit") != 1
            ):
                fail("IMDS", "require known IMDSv2 tokens and hop limit 1")
            if value.get("associate_public_ip_address") is True or value.get("public_ip"):
                fail("PUBLIC_IP", "node must not have a public IP")
            for interface in value.get("network_interfaces") or []:
                if interface.get("associate_public_ip_address") is True:
                    fail("PUBLIC_IP", "launch-template network interface requests a public IP")
        if kind == "aws_eks_node_group" and (
            not value.get("launch_template")
            or not any(
                candidate.get("type") == "aws_launch_template"
                and candidate.get("address", "").rsplit(".aws_", 1)[0]
                == address.rsplit(".aws_", 1)[0]
                for candidate in planned
            )
        ):
            fail(
                "IMDS",
                "node group requires an inspected launch template; implicit EC2 defaults are insufficient",
            )
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    source = sub.add_parser("source")
    source.add_argument("--root", type=Path, default=ROOT)
    plan = sub.add_parser("plan")
    plan.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        errors = (
            source_errors(load_source(args.root))
            if args.mode == "source"
            else plan_errors(json.loads(args.file.read_text()))
        )
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"Cannot validate IaC: {exc}", file=sys.stderr)
        return 1
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print(f"IaC {args.mode} security checks passed")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
