#!/usr/bin/env python3
"""Fail before cluster creation when the selected CI runner cannot host the stack."""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

GIB = 1024**3
# A nominal 16 GiB VM exposes slightly less after kernel reservation. This is
# an explicit 15 GiB usable floor, not permission to run on an 8 GiB VM.
MINIMUM_USABLE_MEMORY = 15 * GIB


def failures(memory_bytes, cpus, free_bytes):
    errors = []
    if memory_bytes < MINIMUM_USABLE_MEMORY:
        errors.append("Use a nominal 16 GiB or larger runner (at least 15 GiB Docker-visible RAM)")
    if cpus < 4:
        errors.append("Docker must expose at least four CPUs")
    if free_bytes < 40 * GIB:
        errors.append("Docker's filesystem must have at least 40 GiB free")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {
        "nominal_memory_gib": 16,
        "minimum_docker_visible_memory_bytes": MINIMUM_USABLE_MEMORY,
        "minimum_storage_free_bytes": 40 * GIB,
    }
    try:
        info = json.loads(
            subprocess.check_output(
                ["docker", "info", "--format", "{{json .}}"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        )
        # Linux hosted daemon storage is local. Never retain the full Docker
        # info: it can include registry/proxy configuration unrelated to evidence.
        free = shutil.disk_usage(info["DockerRootDir"]).free
        errors = failures(info["MemTotal"], info["NCPU"], free)
        report.update(
            docker_memory_bytes=info["MemTotal"],
            docker_cpus=info["NCPU"],
            docker_storage_free_bytes=free,
        )
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        errors = [
            f"Cannot inspect local Docker storage/resources ({type(error).__name__}); verify daemon access"
        ]
    report.update(passed=not errors, failures=errors)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if errors:
        print(
            "Select a sufficiently sized Linux x64 runner using the repository variable "
            "DEV_ACCEPTANCE_RUNNER. GitHub's standard public Ubuntu runner documents "
            "16 GB RAM and 14 GB disk; larger runners need explicit account setup. "
            "No cluster was created."
        )
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
