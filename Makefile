# platform-core — common developer commands.
# Run `make` or `make help` to list targets. These mirror what CI does
# (.github/workflows/ci.yaml) and the runbook in CLAUDE.md, so "green locally"
# means "green in CI".

APPS        := echo-service worker-service llm-gateway
DEV_OVERLAY := kustomize/overlays/dev
PROD_OVERLAY:= kustomize/overlays/prod

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
deps: ## Sync each app's locked dependencies into its uv venv (.venv)
	@for app in $(APPS); do \
	  echo "== $$app =="; ( cd apps/$$app && uv sync --frozen ) || exit 1; \
	done

.PHONY: test
test: ## Run unit tests for all apps (uv run pytest)
	@for app in $(APPS); do \
	  echo "== $$app =="; ( cd apps/$$app && uv run --frozen pytest -q ) || exit 1; \
	done

.PHONY: lint
lint: ## Lint + format-check the apps (ruff via uvx, config in root pyproject.toml)
	@for app in $(APPS); do \
	  echo "== $$app =="; \
	  uvx ruff check apps/$$app/main.py && uvx ruff format --check apps/$$app/main.py || exit 1; \
	done

.PHONY: fmt
fmt: ## Auto-format the apps (ruff format)
	@for app in $(APPS); do uvx ruff format apps/$$app/main.py; done

.PHONY: images
images: ## Build all app container images locally (no push)
	@for app in $(APPS); do \
	  echo "== $$app =="; docker build -t platform-core/$$app:dev apps/$$app || exit 1; \
	done

# ── Kubernetes manifests ────────────────────────────────────────────────────────
.PHONY: kustomize
kustomize: ## Build dev + prod overlays and validate with kubeconform
	kustomize build $(DEV_OVERLAY)  | kubeconform -strict -summary -schema-location default
	kustomize build $(PROD_OVERLAY) | kubeconform -strict -summary -schema-location default
	kustomize build kubernetes/platform/kafka/overlays/dev  | kubeconform -strict -summary
	kustomize build kubernetes/platform/kafka/overlays/prod | kubeconform -strict -summary

.PHONY: helm-lint
helm-lint: ## Lint all Helm charts
	@for chart in helm/*/; do echo "== $$chart =="; helm lint "$$chart" || exit 1; done

.PHONY: policy
policy: ## Run OPA/conftest policies against the dev overlay
	kustomize build $(DEV_OVERLAY) | conftest test --policy policy/ -

.PHONY: kube-lint
kube-lint: ## Run kube-linter best-practice checks
	kube-linter lint kubernetes/ --config .kube-linter.yaml

.PHONY: kyverno
kyverno: ## Check Kyverno policies are well-formed (dry-run)
	kyverno apply kubernetes/platform/kyverno-policies/policies/

# ── Terraform ─────────────────────────────────────────────────────────────────
.PHONY: tf-fmt
tf-fmt: ## Check Terraform formatting
	terraform fmt -check -recursive terraform/

.PHONY: tf-validate
tf-validate: ## Validate the dev + prod Terraform root modules
	cd terraform/environments/dev  && terraform init -backend=false && terraform validate
	cd terraform/environments/prod && terraform init -backend=false && terraform validate

# ── Aggregate ─────────────────────────────────────────────────────────────────
.PHONY: validate
validate: lint test kustomize helm-lint policy tf-fmt ## Run the full local check suite (mirrors CI)

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
