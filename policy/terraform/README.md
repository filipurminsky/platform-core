# Infrastructure security gates

`make iac-security` requires Python 3 and conftest (the version pinned in CI).
It parses actual HCL/YAML through conftest, checks reviewed module wiring, and
runs positive/negative fixtures without credentials or an AWS plan. It rejects
changes to the private prod API default, CIDR guards, private-node subnet wiring,
public-IP assignment, and explicit IMDSv2/hop-limit settings. Karpenter discovery
must select private subnets. Public-IP/IMDS scanner exemptions are rejected.

This source check deliberately pins known expressions and interfaces; it is not
a Terraform evaluator. A legitimate refactor may need a reviewed guard update.
The fixtures mutate parsed configurations, including nested registry-module
wiring, and assert specific rejection reasons. Missing inputs fail validation.

When AWS is configured, `terraform-plan.yaml` additionally creates a saved plan,
converts it with `terraform show -json`, and runs:

```bash
python3 policy/terraform/security.py plan /path/to/plan.json
```

The blocking plan check descends into expanded registry modules. It requires
private API access, explicit restricted CIDRs for any opted-in public API,
no subnet/node public-IP assignment, and explicit IMDSv2 with hop limit 1 on
launch templates/instances. It rejects unknown required endpoint, CIDR, subnet,
and metadata settings. The optional legacy GPU node-group path needs an explicit
launch template before activation; implicit instance defaults cannot establish
its IMDS posture. Karpenter EC2 instances are created after Terraform, so their
settings are covered by the source manifest guard, not a nonexistent TF instance.

Plans stay runner-local because JSON can expose sensitive values; only concise
policy errors are printed. Forks and repositories without AWS OIDC configuration
skip the AWS plan job, but always run source checks and negative fixtures in CI.
A source/fixture pass does not establish a successful AWS plan or deployment.
Full external-module Checkov findings remain informational; local Checkov and
Trivy scans remain separate blocking checks with reviewed exceptions. Trivy
source scanning cannot render the demo Helm chart without its required image
input; chart rendering/schema validation runs separately in CI.

Managed-node metadata is explicitly set to IMDSv2 and a response hop limit of 1.
Normal application pods use IRSA instead of the node IAM role; workloads relying
on IMDS from a pod network must migrate to an explicit workload identity before
this configuration is applied. Host-networked pods still require review because
the hop limit is not isolation from processes sharing the node network.

Workflow validation is separate: `make workflow-lint` downloads actionlint
1.7.12 with a platform-specific pinned checksum, then checks every GitHub workflow
and regression fixtures. Its fixture reproduces the invalid `runner.temp` job
environment expression. Curl/network access is needed on the first local run;
binaries are cached under `.cache/platform-validation/tools`. Optional ShellCheck
and Pyflakes integrations are disabled so workflow semantics are checked equally
on developer machines and CI; operational shell checks remain in `script-test`.
