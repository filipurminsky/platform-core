#!/usr/bin/env bash
# Validate GitHub expressions/contexts and workflow structure with pinned actionlint.
set -euo pipefail
cd "$(dirname "$0")/.."
version=1.7.12
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64) platform=linux_amd64; digest=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8 ;;
  Linux/aarch64|Linux/arm64) platform=linux_arm64; digest=325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6 ;;
  Darwin/arm64) platform=darwin_arm64; digest=aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f ;;
  Darwin/x86_64) platform=darwin_amd64; digest=5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644 ;;
  *) echo 'Unsupported actionlint platform; use Linux/macOS AMD64 or ARM64.' >&2; exit 1 ;;
esac
cache="${WORKFLOW_TOOL_CACHE:-.cache/platform-validation/tools}/actionlint-${version}-${platform}"
if [[ ! -x "$cache/actionlint" ]]; then
  command -v curl >/dev/null || { echo 'Install curl to download pinned actionlint.' >&2; exit 1; }
  temporary="$(mktemp -d)"
  trap 'rm -rf "$temporary"' EXIT
  curl -fsSL "https://github.com/rhysd/actionlint/releases/download/v${version}/actionlint_${version}_${platform}.tar.gz" -o "$temporary/actionlint.tar.gz"
  actual="$(shasum -a 256 "$temporary/actionlint.tar.gz" | awk '{print $1}')"
  [[ "$actual" == "$digest" ]] || { echo 'actionlint checksum mismatch.' >&2; exit 1; }
  tar -xzf "$temporary/actionlint.tar.gz" -C "$temporary" actionlint
  mkdir -p "$cache"
  install -m 0755 "$temporary/actionlint" "$cache/actionlint"
  rm -rf "$temporary"
  trap - EXIT
fi
# Shell/Python tools vary locally; workflow expression/structure validation is
# deterministic. Operational shell checks remain in make script-test.
exec "$cache/actionlint" -shellcheck= -pyflakes= "$@"
