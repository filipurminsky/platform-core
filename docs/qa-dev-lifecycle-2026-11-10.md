# Repository-owned DEV lifecycle acceptance — 2026-11-10

Revision `80b2c051c5a5f944ecb603e133f2dc4ff9a0d67d` replaces the temporary
fresh-cluster procedure with `make dev-acceptance`. The runner clones public
`main`, isolates its cluster/kubeconfig/ports, records phase results, and treats
cleanup failure as an overall failure. See the [reproduction guide](dev-acceptance.md).

## Hosted full-stack acceptance

The [manually dispatched workflow](https://github.com/filipurminsky/platform-core/actions/runs/42859286693)
passed at that revision. The separate
[main CI run](https://github.com/filipurminsky/platform-core/actions/runs/42859193770)
also passed. Before publication, 79 operational-script regressions and eight
workflow/resource tests passed, alongside Ruff, ShellCheck, workflow lint,
diff checks, and commit/push hooks.

The full lifecycle used `ubuntu-24.04`, four Docker CPUs, 16,760,348,672 bytes of
Docker-visible RAM, and 91,910,963,200 bytes of free Docker storage at preflight.
This is the capacity observed on this runner, not a guarantee for that label.
Its three fresh kind nodes had two concurrent image pulls each; the sampler
verified the actual kubelet settings on every node. No OCI image cache was
imported.

| Gate | Verified result |
|---|---|
| Bootstrap and convergence | 207.399 s + 577.671 s = **785.070 s** |
| Final platform health | 37 synced Applications: 36 Healthy, intentionally paused vLLM Suspended; four routed HTTP checks passed |
| Redis replacement | New pod Ready after 12.783 s; exact synthetic state and TTLs verified after 13.064 s; same PVC/PV; both test keys removed |
| Kafka/DLQ/audio | Matching ping and committed offsets, matching poison DLQ entry, completed audio job with all four exact output keys; 95.242 s |
| Tenant access | All eight network/authentication assertions passed; cleanup succeeded |
| Published portal | 28 catalog entities, three templates, exact published Git revisions; two real remote fetches generated all 25 expected files in 3.688 s |
| Cleanup | Both teardown invocations succeeded, owned cluster absent, original context and existing containers preserved |

The downloaded artifact contains ten JSON files and no raw logs, kubeconfigs,
or Secret manifests. ZIP SHA256 is
`70dad9897a6d84da01ed77085ff9ffe642700463d702da9541416b9f04fb635a`.
Hosted artifact retention is seven days; the
[portable summary](evidence/dev-lifecycle-2026-11-10.json) retains the measured
outcomes separately.

The sampler recorded 44 samples, a peak summed node memory usage of about
10.88 GiB, and 445.11% CPU using Docker's per-core convention. It observed 68
completed registry pulls and 48 already-present image events. Early API and
node-inspection failures are retained rather than treated as zero usage; this
is not gap-free monitoring. Node-pressure observations start after the required
Argo/KEDA queries succeed. The hosted timing is a single functional run, not a
matched performance comparison with the local machine.

## Preserved interrupted local attempt

The first local serial-pull attempt started on November 10 and failed its
convergence gate. It crossed substantial host-time discontinuities: recorded
UTC start/end span **26,684.440 seconds**, while the sum of measured phase times
is **3,101.196 seconds**. API timeouts, node readiness changes and pull failures
appeared after the interruptions. These timings are unsuitable for a startup
comparison and are not evidence of a parallel-pull improvement.

Both teardown invocations succeeded. The owned cluster was absent, the exact
retained QA containers were restarted, and full retained-QA acceptance passed
after 46.6 seconds. Cleanup reported no errors. A separate November 11 health
check passed again. The failed attempt remains in its own evidence directory;
subsequent measurements use new directories.

This exposed an observation gap in the first harness version. The follow-up
adds UTC-versus-monotonic discontinuity detection and early sampler-failure
propagation through the same teardown/restoration path. A fresh comparison is
required after that repair; the successful hosted run above remains valid.

## Scope

All tests use DEV mock/stub inference, local kind storage, and synthetic jobs.
The portal test generates files through remote fetch actions without publishing
a repository or creating a registration PR. Redis proves graceful pod replacement
on the same volume, not node loss, HA, or lost-volume restoration. GitOps and
template sources still track `main`; the runner verifies the observed revision
and checks that public `main` did not move during acceptance.
