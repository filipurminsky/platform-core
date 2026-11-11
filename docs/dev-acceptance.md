# Full DEV acceptance and startup measurements

Run the complete local lifecycle from the published repository with:

```bash
make dev-acceptance
```

The runner creates a clean checkout of public `main`, a uniquely named three-node
kind cluster, private kubeconfig, and unused loopback ports. It builds all seven
service images, bootstraps the platform, and checks these stages in order:

1. All expected Argo CD Applications converge, the Gateway is programmed, and
   four routed HTTP health checks pass.
2. Redis retains exact synthetic job/dedup records and their TTLs after replacing
   its pod on the same volume.
3. The Kafka worker processes the matching ping, a poison message reaches its
   DLQ, and an audio job completes with output object keys.
4. Eight tenant network and authentication assertions pass.
5. The public catalog has the expected templates without processing errors; the
   microservice template performs two real remote fetches and generates 25 files.
6. Final platform health and published-source consistency pass.
7. The owned cluster is deleted, repeated teardown succeeds, and the original
   kubectl context and existing running containers remain intact.

The portal check performs a fetch-only dry run. It does not publish a repository
or create a registration PR. Audio inference uses the DEV mock/stub backends.
Redis replacement proves same-volume recovery, not node-loss recovery or HA.

## Prerequisites and ownership

Use the [local Kubernetes prerequisites](local-dev.md#prerequisites), including
Python 3.14, Docker, kind, kubectl, Helm, and the bootstrap shell tools. Allocate
at least 16 GiB of Docker memory for one full stack and enough disk for fresh
node images, builds, and any optional image archives. Keep public `main` stable
for the duration: GitOps and templates track that branch. The runner records the
source SHA and helper hashes, checks deployed Git revisions, and fails if the
published revision changes during the run.

The runner never implicitly stops another cluster. If a retained full platform
must be stopped to free resources, name it explicitly and supply its Gateway:

```bash
make dev-acceptance DEV_ACCEPTANCE_ARGS="--pause-cluster=platform-core-qa --restore-gateway-url=http://localhost:18000"
```

This checks its health first, records its exact node container IDs, stops those
containers, and restores the same IDs with bounded node/API and full platform
health checks afterward. Restoration runs after failures and ordinary interrupts
as well. Unrelated clusters and containers are preserved. A forced process kill,
host shutdown, or Docker failure can prevent cleanup; inspect the recorded
cluster name and container IDs before manually recovering resources.

For a reproducible run, specify a new evidence directory and the full published
commit SHA:

```bash
python3 scripts/dev-acceptance.py \
  --expected-revision="$(git rev-parse HEAD)" \
  --evidence-dir=/tmp/platform-acceptance-run-1
```

The evidence directory must not already exist. The command prints each phase,
its progress and exit code. Inspect `logs/<phase>.log` when a phase fails.
Cache-export subprocess failures also retain private stderr in
`logs/cache-export-stderr.log`; only the operation, node and exit code enter the
upload-safe cache summary.
`summary/result.json` records functional results and cleanup errors; cleanup
failure makes the overall command fail. Only `summary/*.json` is intended for
artifact upload. The cloned source and kubeconfig live in a separate private
temporary directory and are removed during cleanup.

The default application convergence budget is 2400 seconds; each initial Helm
installation has a separate 600-second budget. Override these with `--timeout`
and `--install-timeout`. A larger deadline does not repair a failed image pull,
invalid manifest, or missing permission; use the phase logs and status summaries
to identify the cause first.

## Controlled startup comparison

The [accepted November 11 comparison](qa-dev-startup-2026-11-11.md) measured
10m40s for cold serial startup, 11m50s for cold parallel startup, and 17m20s
with cache reuse on one four-CPU runner. All functional and cleanup gates passed.
One pull per node remains the default; use the options below to measure your
environment. Neither option demonstrated a startup gain in that experiment.

Measure the same published revision on the same Docker allocation, with the
same unrelated workloads present. Run one full lifecycle at a time. Compare
fresh nodes with one image pull per node against fresh nodes with two concurrent
pulls per node. Kubernetes exposes this bound through `serializeImagePulls` and
`maxParallelImagePulls`; the latter is stable in Kubernetes 1.35 and later.
See the [Kubernetes image-pull documentation](https://kubernetes.io/docs/concepts/containers/images/#serial-and-parallel-image-pulls).

```bash
make dev-acceptance DEV_ACCEPTANCE_ARGS="--image-pull-concurrency=1 --evidence-dir=/tmp/platform-serial"
make dev-acceptance DEV_ACCEPTANCE_ARGS="--image-pull-concurrency=2 --write-image-cache=/tmp/platform-image-cache --evidence-dir=/tmp/platform-parallel"
```

Add the same explicit pause/restore options to both commands when needed. These
are cold **cluster** runs: new kind nodes have no platform image cache, while
host tools, the kind node image, and Docker service build caches may already
exist. Report those conditions rather than calling this a cold host.

Every acceptance run explicitly retains containerd's compressed image layers
(`discard_unpacked_layers=false`), and the sampler verifies that setting on each
node. Kind normally discards those layers after unpacking, which can leave a
running image without the content needed for OCI export. Retention uses additional
disk space; it is identical for all three configurations. The ordinary bootstrap
template retains kind's default behavior. Exporting from an older cluster with
layer discarding enabled fails with instructions to create a fresh acceptance
cluster; changing the setting cannot restore blobs already discarded.

The performance summary records resource samples and image-pull observations.
Compare the combined bootstrap and application convergence duration, then check
full functional acceptance and cleanup before treating a timing as successful.
Registry latency and other host workloads can vary; one pair of runs is a
measurement, not a capacity guarantee or statistical performance result.

### Reusing platform images explicitly

The parallel run above exports an image cache after its functional checks pass.
Use it for a separate warm run at the same published revision:

```bash
make dev-acceptance DEV_ACCEPTANCE_ARGS="--image-pull-concurrency=2 --image-cache=/tmp/platform-image-cache --evidence-dir=/tmp/platform-cache-reuse"
python3 scripts/compare-dev-performance.py \
  --cold1=/tmp/platform-serial --cold2=/tmp/platform-parallel \
  --warm2=/tmp/platform-cache-reuse --output=/tmp/platform-comparison.json
```

The cache contains per-node OCI image archives. It excludes the seven locally
built application images and all workload volumes, Secrets, and kubeconfigs.
Its manifest binds archives to the source SHA, node image, architecture, and node
topology. Every archive checksum is checked before import, and restored image
digests are checked afterward. A different source revision requires a new cache;
an existing nonempty export directory is never overwritten. Use caches you
created and control: checksum consistency is not publisher authentication.

Import happens before Cilium installation. Application images are still built
and loaded from the current checkout. Images are cached per node, so different
scheduling can still cause registry pulls. Record the import cost within warm
bootstrap time, the export cost separately, archive sizes, observed cache hits,
and any remaining registry pulls. A warm-run gain is not evidence that parallel
pulls improved an uncached run. Cache archives remain local and are excluded
from the CI evidence artifact. Archives use OCI format without the optional
Docker-compatible manifest, and synthetic containerd import aliases are excluded.

Before export, the cache tool checks native image completeness. Reusing an
unpacked snapshot can leave a compressed blob absent even when layer retention
is enabled. The tool fetches only incomplete native content from the original
registry using the recorded image digest, verifies identities afterward, and
records fetch durations in the cache manifest. Export therefore may require
registry access; this preparation cost is excluded from startup timing and must
be reported alongside the archive size and reuse benefit.

Keep the host awake throughout a measurement. The sampler compares UTC elapsed
time with its monotonic clock and rejects a discrepancy greater than five
seconds, which can indicate host suspension or a clock correction. The runner
then stops its current phase and cleans up/restores owned resources. It does
not override explicit system sleep. Repeat an interrupted run in a new evidence
directory; never use its timing in the startup comparison.

The comparison rejects failed acceptance/cleanup, mismatched measured kubelet
settings, different source revisions or Docker allocations, changed unrelated
container identities, and a warm run that did not use the parallel run's cache.
It reports warm cache validation and import costs separately from export costs.

## Manual CI

Dispatch [Full DEV acceptance](../.github/workflows/dev-acceptance.yaml) on `main`.
It has no automatic push, PR, or scheduled trigger. Select image-pull concurrency
1 or 2. The workflow performs an uncached full lifecycle and uploads only
sanitized JSON summaries, including failure evidence, for seven days.

Select `run_benchmark` to run the serial, parallel, and cached lifecycles
sequentially on the same runner, followed by the guarded comparison. Every run
must pass functional checks and cleanup before the next one starts. Image
archives stay outside the uploaded evidence. This option isolates the comparison
from laptop sleep and changing local workloads:

```bash
gh workflow run dev-acceptance.yaml --ref main -f run_benchmark=true
```

Preparation, serial startup, parallel startup/export, warm startup/import, and
comparison appear as separate Actions steps. They share one job and runner;
each phase requires successful completion of its predecessor at the same source
revision. A failed phase prevents later measurements from running.

For an archive-format regression, select `cache_probe_only` instead of
`run_benchmark`. This creates two sequential disposable single-node clusters,
pulls the immutable platform-image corpus in
`tests/fixtures/dev-cache-images.json`, and checks actual OCI export/import and
CRI visibility. It does not install the platform or measure startup performance:

```bash
gh workflow run dev-acceptance.yaml --ref main -f cache_probe_only=true
```

The fixture is a regression corpus, not a deployment-version source. Cache
failures retain safe error categories, content digests, completed-node archive
details, and bounded per-image isolation results. Raw stderr and archives remain
outside the uploaded JSON summaries.

Before timing the first run, the workflow builds all seven service images and
pulls the pinned kind node image. Thus every measured run starts with those host
caches available. Preparation time and image identities are recorded separately
in `preparation.json`; each cold run still creates fresh node image stores with
no imported platform archive. Archive export is excluded from startup timing,
while warm import and its bootstrap validation are included.

The selected Linux x64 runner must expose at least four Docker CPUs, 15 GiB of
usable Docker memory (a nominal 16 GiB machine), and 40 GiB of free space on
Docker's filesystem. The resource gate runs before cluster creation. GitHub's
standard public Ubuntu runner documents 16 GB RAM and 14 GB SSD, so do not assume
that its available disk meets this full-stack requirement. See
[GitHub runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
Select a sufficiently sized runner with the dispatch `runner_label` input, or
set repository variable `DEV_ACCEPTANCE_RUNNER` to override it. Prefer an
ephemeral runner; provisioning a larger runner is separate account setup.
