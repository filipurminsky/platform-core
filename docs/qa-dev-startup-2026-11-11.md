# DEV startup benchmark — 2026-11-11

The benchmark runs three complete DEV lifecycles sequentially on one hosted
runner: fresh nodes with one pull per node, fresh nodes with two pulls, then
fresh nodes with two pulls and a verified OCI cache. Host application-build
layers and the pinned kind image are prepared before timing starts. See the
[reproduction guide](dev-acceptance.md) and
[portable evidence](evidence/dev-startup-benchmark-2026-11-11.json).

## Preserved failed attempt

[Run 42990053557](https://github.com/filipurminsky/platform-core/actions/runs/42990053557)
tested revision `5177ac6f5450779a901fa788caa3deb1dcf283cf`.
The serial run passed its full lifecycle. The parallel run passed convergence,
Redis replacement, Kafka/DLQ/audio processing, all tenant checks, public portal
generation, final health and source checks, then failed during cache export.
The warm run did not start, and no complete comparison was produced.

A read-only reproduction against the retained QA node identified containerd's
optional Docker-compatible `manifest.json` generation failing with
`invalid checksum digest format`. The repair selects OCI-only archives and
excludes synthetic `import-*` references, which otherwise bypassed the explicit
local application-name filter. The next comparison must verify actual export
and import before any cache benefit is claimed.

The direct retest then exposed kind's `discard_unpacked_layers=true` default:
platform containers could run from unpacked snapshots while their compressed
OCI layers had already been removed. Export failed on missing content. The
acceptance runner now retains those layers for every measured configuration,
verifies the setting, and rejects comparisons without that evidence. Ordinary
bootstrap behavior remains unchanged. This adds disk usage to the measured
acceptance environment and requires fresh clusters; the retained QA cluster
was not modified.

Both clusters were removed, repeated teardown succeeded, and both runs reported
no cleanup errors. Clock guards passed with zero recorded drift at the sampler's
precision. The artifact contains 21 JSON files; its ZIP SHA256 is
`499b8b02101479715faa72031082b9c44c283f7d5d232119acf0346546c5cc07`.
The failed attempt remains recorded rather than being replaced by a later pass.

## Focused OCI repair verification

Two sequential disposable single-node clusters proved the corrected OCI transfer
using the complete images bundled with the pinned kind node. Export produced a
386,199,552-byte archive containing 14 image references in 5.023 seconds; import
took 5.009 seconds. All original image digests matched, every reference carried
the CRI-managed label, and all were visible through CRI. Both temporary nodes
were removed; existing clusters, running containers and kubectl context were
preserved. The portable summary identifies the tested helper by SHA256.

This focused check did not exercise registry downloads. Earlier local attempts
hit a registry-pull timeout and incomplete multi-platform content in a cached
Redis image; both attempts cleaned up successfully. The complete hosted batch
is still required to validate platform-image caching and measure startup gains.

The repair passed 90 operational-script tests and 20 workflow tests, plus Ruff,
ShellCheck, workflow lint and diff checks. Benchmark phases now appear separately
in Actions while sharing one runner and requiring successful predecessors.

## Scope

These are DEV mock/stub inference runs on kind. One run per configuration gives
descriptive observations, not a statistically established speedup. Startup
resource samples have gaps, including early node-pressure observations before
Argo/KEDA queries succeed. Cache export cost is separate from startup; cache
import is included in warm bootstrap. No AWS, GPU, HA, or node-loss recovery
claim follows from this experiment.
