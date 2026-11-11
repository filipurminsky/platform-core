#!/usr/bin/env bash
# Sequential full lifecycles on one runner. Each invocation owns its cleanup;
# fail immediately instead of comparing incomplete or failed runs.
set -euo pipefail
cd "$(dirname "$0")/../.."
root="${RUNNER_TEMP:?GitHub runner temporary directory required}/dev-benchmark"
cache="${RUNNER_TEMP}/dev-benchmark-image-cache"
revision="${GITHUB_SHA:?Expected source commit required}"
[[ ! -e "$root" && ! -e "$cache" ]] || {
  echo 'Benchmark evidence/cache already exists; use a fresh runner invocation.' >&2
  exit 1
}
mkdir -p "$root"
python3 .github/scripts/prepare-dev-benchmark.py \
  "--expected-revision=$revision" "--output=$root/preparation.json"
common=("--expected-revision=$revision" --timeout=2400 --install-timeout=600)
python3 scripts/dev-acceptance.py "${common[@]}" \
  "--evidence-dir=$root/cold1" --image-pull-concurrency=1
python3 scripts/dev-acceptance.py "${common[@]}" \
  "--evidence-dir=$root/cold2" --image-pull-concurrency=2 "--write-image-cache=$cache"
python3 scripts/dev-acceptance.py "${common[@]}" \
  "--evidence-dir=$root/warm2" --image-pull-concurrency=2 "--image-cache=$cache"
python3 scripts/compare-dev-performance.py \
  "--cold1=$root/cold1" "--cold2=$root/cold2" "--warm2=$root/warm2" \
  "--output=$root/comparison.json"
