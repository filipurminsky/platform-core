# ${{ values.name }}

${{ values.description }}

## Run and test

```bash
uv sync --frozen
OTEL_SDK_DISABLED=true uv run --frozen pytest -q
uv run --frozen uvicorn app.main:app --reload --port 8080
docker build -t ${{ values.name }}:dev .
docker run --rm -p 8080:8080 ${{ values.name }}:dev
```

The service exposes `/`, `/healthz`, `/readyz`, and `/metrics`. Its Docker runtime
is non-root and contains locked runtime dependencies without uv.

## Delivery and required account setup

Backstage publishes this repository and opens a **separate registration PR** in
the platform repository. That PR adds an environment-selected ApplicationSet
and an AppProject scoped to this repository, the `apps` namespace, and only the
resource kinds used here. A platform owner reviews ownership and permissions
before merging. The platform's root Application discovers that registration.
The `apps` namespace and platform controllers must already exist.

Before using delivery, configure the GitHub integration to create repositories,
workflows and pull requests in both repositories. Enable Actions permission to
create pull requests, protect `main`, and configure the **production** GitHub
Environment with required reviewers and a `main`-only deployment rule. Environment
protection cannot be installed by a template; without it GitHub does not pause
production promotion. Review generated code before enabling workflows.

`.github/workflows/delivery.yaml` tests on PRs, builds on main, scans amd64 before
publishing, then publishes amd64/arm64 with SBOM/provenance, scans both immutable
variants, and signs only after both pass. It verifies the exact workflow signer
before opening a PR that pins the digest and enables one dev or two prod replicas.
Dev promotion runs on application changes; prod is selected with `workflow_dispatch`.
Image promotion PRs are never automatically merged. Overlay-only merge commits
do not trigger image rebuilds. A PR opened with `GITHUB_TOKEN` does not trigger
new workflows automatically: promotion already renders the changed overlay;
run the test job manually on the PR branch if branch protection requires a check.
Manual runs on non-main branches run tests only and cannot release or promote.
For example, after opening a promotion PR, run its required test check on that
exact head branch before merging:

```bash
gh workflow run delivery.yaml --ref promote/dev/RUN_ID-ATTEMPT -f environment=dev
```

Replace the branch with the PR's actual head branch (including `prod` for a prod
PR). If organizational rules require a `pull_request` event specifically, use a
reviewed GitHub App integration to open promotion PRs instead; the default
`GITHUB_TOKEN` cannot trigger that event. No personal access token is required or
assumed by the scaffold.

Both overlays start at **zero replicas** with an all-zero placeholder digest.
This is deliberately undeployable until the first verified promotion PR merges.
Do not manually enable replicas against that placeholder.

The repository is public; GHCR **package visibility is separate**. Make its
package public before deploying, or configure reviewed namespace pull credentials
and `imagePullSecrets`. No registry password is embedded in the scaffold.
The platform's existing signature admission policy matches ECR platform images,
not this GHCR repository. This scaffold verifies signatures at promotion;
cluster admission enforcement requires a separately reviewed scoped GHCR policy.

After registration and the dev promotion merge, visit
`http://${{ values.name }}.platform-core.local`. Configure local DNS/hosts as in
platform-core's local development guide. In Argo CD inspect the `${{ values.name }}`
Application; Prometheus discovers its ServiceMonitor. Query
`${{ values.name | replace('-', '_') }}_requests_total` in Grafana's Prometheus
Explore view. If sync fails, check AppProject permissions and platform CRDs; if
pods cannot pull, check the promoted digest and GHCR package visibility.

Generated overlays are rendered and policy-tested in platform-core's scaffold
suite. Live GitHub publishing/signing and cluster onboarding require the account
configuration above and are not claimed as locally verified.
