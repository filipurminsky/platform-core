# Generated service contract tests

Requires Node.js 22+, uv/Python 3.14, Helm, Kustomize and conftest. Tool pins match
`.github/workflows/ci.yaml`; npm dependencies are integrity-locked here.

```bash
npm ci --prefix tests/scaffolding --ignore-scripts
npm test --prefix tests/scaffolding
node tests/scaffolding/render.mjs /tmp/generated-service
```

The suite renders with Nunjucks using Backstage's `${{ ... }}` delimiters,
preserves GitHub workflow expressions in excluded paths, renders dev/prod with
Kustomize, evaluates combined platform OPA policies, checks scoped GitOps
registration and signed promotion ordering, executes promotion with valid and
invalid inputs, and runs the generated Python service's frozen-lock tests.
It also verifies development-only Backstage guest authentication, production
rejection, configuration rollout checksums and Secret references.
It includes punctuation and Unicode in user metadata to catch escaping failures.

`test-scaffolding.yaml` additionally validates Kubernetes/CRD schemas using the
platform's strict wrapper and builds the generated Docker runtime. These tests
do not execute Backstage actions or publish repositories/images. GitHub account
permissions, GHCR visibility, protected environments and live Argo CD onboarding
remain external integration checks; see the generated service README.
