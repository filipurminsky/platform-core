# platform-core — common developer commands.
# Run `make` or `make help` to list targets. These mirror what CI does
# (.github/workflows/ci.yaml). Security scans, image builds and live-cluster
# exercises remain separate CI gates; validate does not claim to replace them.

# macOS ships GNU Make 3.81, which ignores .SHELLFLAGS. Put the options in
# SHELL so failed renders cannot be hidden by successful downstream commands.
SHELL := /bin/bash -eu -o pipefail
KUBECONFORM ?= ./scripts/kubeconform-k8s

APPS        := echo-service worker-service llm-gateway audio-api stt-worker tts-worker llm-worker
# Shared Python library used by the Kafka workers (tested/linted, no image).
LIBS        := platform-common
PYPROJECTS  := $(LIBS) $(APPS)
DEV_OVERLAY := kustomize/validation/dev
PROD_OVERLAY:= kustomize/validation/prod

.DEFAULT_GOAL := help

# ── Meta ──────────────────────────────────────────────────────────────────────
.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

.PHONY: hooks
hooks: ## Install git pre-commit + pre-push hooks (.pre-commit-config.yaml)
	uvx pre-commit install

# ── Apps (Python) ───────────────────────────────────────────────────────────────
.PHONY: deps
deps: ## Sync each app's (and shared lib's) locked dependencies into its uv venv (.venv)
	@for app in $(PYPROJECTS); do \
	  echo "== $$app =="; ( cd services/$$app && uv sync --frozen ) || exit 1; \
	done

.PHONY: test
test: ## Run unit tests for all apps + shared libs (uv run pytest)
	@for app in $(PYPROJECTS); do \
	  echo "== $$app =="; ( cd services/$$app && uv run --frozen pytest -q ) || exit 1; \
	done

.PHONY: lint
lint: ## Lint + format-check the apps + shared libs (ruff via uvx, root pyproject.toml config)
	@for app in $(PYPROJECTS); do \
	  echo "== $$app =="; \
	  uvx ruff@0.17.4 check services/$$app && uvx ruff@0.17.4 format --check services/$$app || exit 1; \
	done

.PHONY: fmt
fmt: ## Auto-format the apps (ruff format)
	@for app in $(PYPROJECTS); do uvx ruff@0.17.4 format services/$$app; done

.PHONY: images
images: ## Build all app container images locally (no push; context = services/, like CI)
	@for app in $(APPS); do \
	  echo "== $$app =="; DOCKER_BUILDKIT=1 docker build -f services/$$app/Dockerfile -t platform-core/$$app:dev services || exit 1; \
	done

# ── Kubernetes manifests ────────────────────────────────────────────────────────
.PHONY: kustomize
kustomize: ## Validate aggregate, service, Kafka and tenant renders using CI schemas
	@for environment in dev prod; do \
	  kustomize build kustomize/validation/$$environment | $(KUBECONFORM); \
	  kustomize build kubernetes/platform/kafka/overlays/$$environment | $(KUBECONFORM); \
	  for overlay in services/*/k8s/overlays/$$environment/; do \
	    kustomize build "$$overlay" | $(KUBECONFORM); \
	  done; \
	done
	@for tenant in kubernetes/tenants/team-*/; do \
	  kustomize build "$$tenant" | $(KUBECONFORM); \
	done
	kustomize build tests/canary-load | $(KUBECONFORM)
	kustomize build observability/prometheus | $(KUBECONFORM)

.PHONY: schemas
schemas: ## Download pinned CRDs and prepare local validation schemas (requires yq)
	./scripts/setup-crd-schemas.sh

.PHONY: helm-validate
helm-validate: helm-lint ## Render local charts and validate with CI schemas
	helm template demo helm/demo-app --set image.repository=registry.example.com/demo-app | $(KUBECONFORM)
	helm template vllm helm/vllm | $(KUBECONFORM)
	helm template backstage helm/backstage -f helm/backstage/values.yaml | $(KUBECONFORM)

.PHONY: helm-lint
helm-lint: ## Lint all Helm charts
	@for chart in helm/*/; do echo "== $$chart =="; helm lint "$$chart" || exit 1; done

.PHONY: policy
policy: ## Evaluate combined cross-resource OPA policies in both environments
	@for environment in dev prod; do \
	  kustomize build kustomize/validation/$$environment | conftest test --combine --policy policy/ -; \
	done

.PHONY: kube-lint
kube-lint: ## Run kube-linter best-practice checks
	kube-linter lint kubernetes/ --config .kube-linter.yaml
	kube-linter lint . --config .kube-linter-pdb.yaml

.PHONY: kyverno
kyverno: ## Check Kyverno policies are well-formed (dry-run)
	kyverno apply kubernetes/platform/kyverno-policies/base/

# ── Terraform ─────────────────────────────────────────────────────────────────
.PHONY: tf-fmt
tf-fmt: ## Check Terraform formatting
	terraform fmt -check -recursive terraform/

.PHONY: tf-validate
tf-validate: ## Validate the dev + prod Terraform root modules
	cd terraform/environments/dev  && terraform init -backend=false && terraform validate
	cd terraform/environments/prod && terraform init -backend=false && terraform validate

# ── Aggregate ─────────────────────────────────────────────────────────────────
.PHONY: integration-test
integration-test: ## Run cross-service audio pipeline integration tests (uses audio-api venv)
	cd services/audio-api && uv run --frozen pytest ../../tests/integration/ -q

.PHONY: infrastructure-test
infrastructure-test: ## Run isolated Kafka/Redis/S3 worker recovery tests (requires Docker)
	./scripts/test-infrastructure.sh

.PHONY: scaffold-test
scaffold-test: ## Render and test generated services (requires Node, uv, kustomize, conftest)
	npm ci --prefix tests/scaffolding
	npm test --prefix tests/scaffolding

.PHONY: workflow-lint
workflow-lint: ## Validate GitHub workflow contexts (pinned tool; curl/network needed on first run)
	./scripts/validate-workflows.sh
	python3 -m unittest discover -s tests/workflows -v

.PHONY: iac-security
iac-security: ## Block unsafe IaC wiring and test negative fixtures (requires conftest)
	python3 policy/terraform/security.py source
	python3 -m unittest discover -s tests/iac -v

.PHONY: script-test
script-test: ## Run operational-script regression checks without a cluster
	python3 -m unittest discover -s tests/scripts -v
	@for script in scripts/*.sh scripts/kubeconform-k8s; do bash -n "$$script"; done

.PHONY: validate
validate: workflow-lint iac-security lint test integration-test script-test scaffold-test kustomize helm-validate policy kube-lint tf-fmt tf-validate ## Non-cluster checks; run make schemas first (security/build/e2e remain separate)

.PHONY: doctor
doctor: ## Inspect local tools, Docker and the selected cluster without changing them
	./scripts/doctor.sh

# ── Cluster lifecycle + demos ───────────────────────────────────────────────────
.PHONY: up
up: ## Bootstrap a local kind cluster (zero AWS cost)
	./scripts/bootstrap.sh --mode=local

.PHONY: up-aws
up-aws: ## Bootstrap against AWS EKS (terraform must be applied first)
	./scripts/bootstrap.sh --mode=aws

.PHONY: down
down: ## Tear down the local cluster
	./scripts/teardown.sh --mode=local

.PHONY: canary-demo
canary-demo: ## Deploy a broken image → SLO analysis auto-rolls-back
	./scripts/canary-demo.sh bad

.PHONY: load-test
load-test: ## Apply the k6 load test (drives canary analysis + KEDA)
	kubectl apply -k tests/load

.PHONY: crossplane-demo
crossplane-demo: ## Apply the example S3 bucket claim (AWS clusters only)
	kubectl apply -f kubernetes/platform/crossplane/examples/bucket-claim.yaml

.PHONY: argocd-password
argocd-password: ## Print the initial ArgoCD admin password
	@kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d && echo
