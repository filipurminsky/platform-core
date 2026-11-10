#!/usr/bin/env bash
# Minimal Linux AMD64 toolchain for the manual DEV lifecycle workflow.
# Reuse exact existing versions; downloads are verified against pinned hashes.
set -euo pipefail
[[ $(uname -s)/$(uname -m) == Linux/x86_64 ]] || {
  echo 'DEV acceptance CI requires a Linux AMD64 runner.' >&2; exit 1;
}
for tool in curl tar sha256sum docker git openssl make python3 jq; do
  command -v "$tool" >/dev/null || { echo "Required runner tool missing: $tool" >&2; exit 1; }
done
python3 -c 'import sys; assert sys.version_info[:2] == (3, 14), sys.version'
docker info >/dev/null
docker version --format 'Docker client={{.Client.Version}} server={{.Server.Version}}'
git --version
openssl version
jq --version

kind_version=v0.33.0
kind_sha=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d
kubectl_version=v1.36.4
kubectl_sha=8b8f088da2dab964f853b38464033b1be15ede2839eca751482357c45abdd05a
helm_version=v4.3.1
helm_sha=0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b
destination="${RUNNER_TEMP:?GitHub runner temporary directory required}/dev-acceptance-bin"
mkdir -p "$destination"
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
fetch() {
  curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 300 "$1" -o "$2"
  printf '%s  %s\n' "$3" "$2" | sha256sum --check --status
}

current_kind=$(kind version 2>/dev/null | awk '{print $2}' || true)
if [[ "$current_kind" != "$kind_version" ]]; then
  fetch "https://github.com/kubernetes-sigs/kind/releases/download/$kind_version/kind-linux-amd64" "$temporary/kind" "$kind_sha"
  install -m 0755 "$temporary/kind" "$destination/kind"
fi
current_kubectl=$(kubectl version --client -o json 2>/dev/null | jq -r '.clientVersion.gitVersion' || true)
if [[ "$current_kubectl" != "$kubectl_version" ]]; then
  fetch "https://dl.k8s.io/release/$kubectl_version/bin/linux/amd64/kubectl" "$temporary/kubectl" "$kubectl_sha"
  install -m 0755 "$temporary/kubectl" "$destination/kubectl"
fi
current_helm=$(helm version --template '{{.Version}}' 2>/dev/null || true)
if [[ "$current_helm" != "$helm_version" ]]; then
  fetch "https://get.helm.sh/helm-$helm_version-linux-amd64.tar.gz" "$temporary/helm.tar.gz" "$helm_sha"
  tar -xzf "$temporary/helm.tar.gz" -C "$temporary" linux-amd64/helm
  install -m 0755 "$temporary/linux-amd64/helm" "$destination/helm"
fi
export PATH="$destination:$PATH"
printf '%s\n' "$destination" >> "${GITHUB_PATH:?GitHub PATH file required}"
[[ $(kind version | awk '{print $2}') == "$kind_version" ]]
[[ $(kubectl version --client -o json | jq -r '.clientVersion.gitVersion') == "$kubectl_version" ]]
[[ $(helm version --template '{{.Version}}') == "$helm_version" ]]
kind version
kubectl version --client
helm version --short
./scripts/bootstrap.sh --mode=local --check
