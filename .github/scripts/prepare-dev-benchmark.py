#!/usr/bin/env python3
"""Warm shared host build/node caches before any timed full-lifecycle run."""

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPS = (
    "echo-service",
    "worker-service",
    "llm-gateway",
    "audio-api",
    "stt-worker",
    "tts-worker",
    "llm-worker",
)


def node_pin(config):
    pins = re.findall(r"^    image: (kindest/node:[^\s]+@sha256:[a-f0-9]{64})$", config, re.M)
    roles = re.findall(r"^  - role: (control-plane|worker)$", config, re.M)
    if roles != ["control-plane", "worker", "worker"] or len(pins) != 3 or len(set(pins)) != 1:
        raise ValueError("Expected three kind nodes with an identical explicit node-image digest")
    return pins[0]


def prepare(root, revision, output):
    if output.exists():
        raise ValueError("Preparation evidence already exists; use a fresh destination")
    started = time.monotonic()
    report = {"passed": False, "source_revision": revision, "included_in_startup_comparison": False}
    try:
        actual = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=30
        ).strip()
        if actual != revision:
            raise ValueError("Preparation checkout differs from the requested source revision")
        pin = node_pin((root / "terraform/modules/kind/kind-config.yaml").read_text())
        report["kind_node_image"] = pin
        report["phase"] = "build-seven-app-images"
        # Uses the same seven Dockerfiles and services/ build context as bootstrap.
        # The different output tags do not affect Docker's cached build layers.
        subprocess.run(["make", "images"], cwd=root, check=True, timeout=1800)
        report["phase"] = "pull-pinned-kind-node"
        subprocess.run(["docker", "pull", pin], cwd=root, check=True, timeout=900)
        report["phase"] = "verify-app-image-identities"
        images = {}
        for app in APPS:
            tag = f"platform-core/{app}:dev"
            identity = subprocess.check_output(
                ["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
                cwd=root,
                text=True,
                timeout=30,
            ).strip()
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", identity):
                raise ValueError("Built app image has no valid content identity")
            images[tag] = identity
        report.update(passed=True, app_image_ids=images, phase="complete")
        report["scope"] = (
            "Host app-build layers and pinned kind node image; new nodes have no imported platform OCI cache"
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Never copy Docker inspection payloads or raw subprocess output into artifacts.
        report["error_type"] = type(error).__name__
        if isinstance(error, ValueError):
            report["error"] = str(error)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.expected_revision):
        parser.error("expected-revision must be a full commit SHA")
    return prepare(ROOT, args.expected_revision, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
