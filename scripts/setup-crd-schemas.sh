#!/usr/bin/env bash
# Shared by local validation and the CI setup action. Versions come from CI.
set -euo pipefail
cd "$(dirname "$0")/.."
for tool in curl yq python3; do
  command -v "$tool" >/dev/null || { echo "Missing $tool; see docs/tool-upgrades.md" >&2; exit 1; }
done
SCHEMA_DIR="${SCHEMA_DIR:-$PWD/.cache/platform-validation/crd-schemas}"
mkdir -p "$(dirname "$SCHEMA_DIR")"
src=$(mktemp -d "$(dirname "$SCHEMA_DIR")/.schemas.XXXXXX")
cleanup() {
  local status=$?
  if [[ ! -d "$SCHEMA_DIR" && -d "$src/previous" ]]; then mv "$src/previous" "$SCHEMA_DIR"; fi
  rm -rf "$src"
  exit "$status"
}
trap cleanup EXIT
for pin in STRIMZI_VERSION KEDA_VERSION GATEWAY_API_VERSION ENVOY_GATEWAY_VERSION EXTERNAL_SECRETS_VERSION ROLLOUTS_VERSION KUBERNETES_VERSION; do
  value=$(yq -r ".env.${pin}" .github/workflows/ci.yaml)
  [[ -n "$value" && "$value" != null ]] || { echo "Missing CI pin: $pin" >&2; exit 1; }
  # CI may explicitly pass action inputs; local defaults read the same pins.
  printf -v "$pin" '%s' "${!pin:-$value}"
done
fetch() { curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 180 "$1" -o "$src/$2"; }
fetch "https://github.com/strimzi/strimzi-kafka-operator/releases/download/${STRIMZI_VERSION}/strimzi-crds-${STRIMZI_VERSION}.yaml" strimzi.yaml
fetch "https://github.com/kedacore/keda/releases/download/v${KEDA_VERSION}/keda-${KEDA_VERSION}-crds.yaml" keda.yaml
fetch "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/standard-install.yaml" gateway-api.yaml
fetch "https://github.com/envoyproxy/gateway/releases/download/${ENVOY_GATEWAY_VERSION}/envoy-gateway-crds.yaml" envoy-gateway.yaml
fetch "https://github.com/external-secrets/external-secrets/releases/download/${EXTERNAL_SECRETS_VERSION}/external-secrets.yaml" external-secrets.yaml
fetch "https://github.com/argoproj/argo-rollouts/releases/download/${ROLLOUTS_VERSION}/install.yaml" argo-rollouts.yaml
python3 .github/scripts/crd-to-jsonschema.py "$src/generated" "$src"/*.yaml
printf '%s\n' "$KUBERNETES_VERSION $STRIMZI_VERSION $KEDA_VERSION $GATEWAY_API_VERSION $ENVOY_GATEWAY_VERSION $EXTERNAL_SECRETS_VERSION $ROLLOUTS_VERSION" > "$src/generated/versions"
# Replace the generated cache only after all downloads and conversion succeed.
# Keeping old JSON files would falsely validate API versions removed upstream.
if [[ -d "$SCHEMA_DIR" ]]; then mv "$SCHEMA_DIR" "$src/previous"; fi
mv "$src/generated" "$SCHEMA_DIR"
echo "Schemas ready in $SCHEMA_DIR. Run make validate."
