#!/usr/bin/env python3
"""Convert CustomResourceDefinition YAML bundles into kubeconform JSON schemas.

Usage: crd-to-jsonschema.py <out-dir> <crds.yaml> [<crds.yaml> ...]

Writes one schema per served CRD version to
    <out-dir>/<group>/<kind-lowercase>_<version>.json
which matches the kubeconform schema-location template
    '<out-dir>/{{ .Group }}/{{ .ResourceKind }}_{{ .ResourceAPIVersion }}.json'

Behaviour mirrors yannh/kubeconform's scripts/openapi2jsonschema.py (the
reference converter) so `kubeconform -strict` treats CRs like kubectl would:
  - every nested object that declares `properties` gets
    `additionalProperties: false` (unknown fields are rejected), except the
    root (apiVersion/kind/metadata are handled by kubeconform) and nodes that
    opt out with `x-kubernetes-preserve-unknown-fields: true`;
  - `format: int-or-string` becomes `oneOf: [string, integer]`.

Stdlib-only on purpose: multi-document YAML is decoded by `yq` (mikefarah,
preinstalled on GitHub-hosted runners), so CI needs no pip install.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _strict(node, root: bool = False):
    if isinstance(node, dict):
        if (
            not root
            and "properties" in node
            and "additionalProperties" not in node
            and not node.get("x-kubernetes-preserve-unknown-fields")
        ):
            node["additionalProperties"] = False
        for value in node.values():
            _strict(value)
    elif isinstance(node, list):
        for item in node:
            _strict(item)
    return node


def _int_or_string(node):
    if isinstance(node, dict):
        if node.get("format") == "int-or-string":
            return {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        return {k: _int_or_string(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_int_or_string(v) for v in node]
    return node


def _crds(path: str):
    out = subprocess.run(
        ["yq", "-o=json", "-I=0", 'select(.kind == "CustomResourceDefinition")', path],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for line in out.splitlines():
        line = line.strip()
        if line and line != "null":
            yield json.loads(line)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    out_dir = Path(argv[1])
    written = 0
    for path in argv[2:]:
        for crd in _crds(path):
            spec = crd.get("spec", {})
            group = spec["group"]
            kind = spec["names"]["kind"].lower()
            for version in spec.get("versions") or []:
                schema = (version.get("schema") or {}).get("openAPIV3Schema")
                if not schema:
                    continue
                schema = _int_or_string(_strict(schema, root=True))
                target = out_dir / group / f"{kind}_{version['name']}.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(schema, indent=1))
                written += 1
    print(f"wrote {written} JSON schemas to {out_dir}")
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
