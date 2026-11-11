#!/usr/bin/env bash
# Run all phases locally, or one phase per Actions step on the same runner.
# Each lifecycle owns its cleanup; advance only after its predecessor succeeds.
set -euo pipefail
cd "$(dirname "$0")/../.."
phase="${1:-all}"
if [[ $# -gt 1 || ! "$phase" =~ ^(all|prepare|cold1|cold2|warm2|compare)$ ]]; then
  echo 'Usage: run-dev-benchmark.sh [all|prepare|cold1|cold2|warm2|compare]' >&2
  exit 2
fi
root="${RUNNER_TEMP:?GitHub runner temporary directory required}/dev-benchmark"
cache="${RUNNER_TEMP}/dev-benchmark-image-cache"
revision="${GITHUB_SHA:?Expected source commit required}"
common=("--expected-revision=$revision" --timeout=2400 --install-timeout=600)

run_phase() {
  local selected="$1" previous=""
  case "$selected" in
    prepare)
      [[ ! -e "$root" && ! -e "$cache" ]] || {
        echo 'Benchmark evidence/cache already exists; use a fresh runner invocation.' >&2
        exit 1
      }
      mkdir -p "$root"
      ;;
    cold1) previous=prepare ;;
    cold2) previous=cold1 ;;
    warm2) previous=cold2 ;;
    compare) previous=warm2 ;;
  esac
  if [[ -n "$previous" ]]; then
    [[ -f "$root/.$previous-complete" && "$(cat "$root/.$previous-complete")" == "$revision" ]] || {
      echo "Benchmark phase $selected requires successful $previous at the same revision." >&2
      exit 1
    }
  fi
  [[ ! -e "$root/.$selected-complete" ]] || {
    echo "Benchmark phase $selected already completed; use a fresh runner invocation." >&2
    exit 1
  }
  case "$selected" in
    prepare)
      python3 .github/scripts/prepare-dev-benchmark.py \
        "--expected-revision=$revision" "--output=$root/preparation.json"
      ;;
    cold1)
      python3 scripts/dev-acceptance.py "${common[@]}" \
        "--evidence-dir=$root/cold1" --image-pull-concurrency=1
      ;;
    cold2)
      python3 scripts/dev-acceptance.py "${common[@]}" \
        "--evidence-dir=$root/cold2" --image-pull-concurrency=2 "--write-image-cache=$cache"
      ;;
    warm2)
      python3 scripts/dev-acceptance.py "${common[@]}" \
        "--evidence-dir=$root/warm2" --image-pull-concurrency=2 "--image-cache=$cache"
      ;;
    compare)
      python3 scripts/compare-dev-performance.py \
        "--cold1=$root/cold1" "--cold2=$root/cold2" "--warm2=$root/warm2" \
        "--output=$root/comparison.json"
      ;;
  esac
  printf '%s\n' "$revision" > "$root/.$selected-complete"
}

if [[ "$phase" == all ]]; then
  for selected in prepare cold1 cold2 warm2 compare; do
    run_phase "$selected"
  done
else
  run_phase "$phase"
fi
