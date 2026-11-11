#!/usr/bin/env python3
"""Compare accepted cold1/cold2/warm2 runs on the same source and Docker hardware."""

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

HARDWARE = ("NCPU", "MemTotal", "Architecture", "ServerVersion", "OperatingSystem")


def require_valid_clock(performance):
    """Reject legacy, incomplete, failed or internally inconsistent timing evidence."""
    try:
        guard = performance["clock_guard"]
        maximum = guard["max_absolute_drift_seconds"]
        if (
            guard["passed"] is not True
            or performance["suitable_for_comparison"] is not True
            or guard["threshold_seconds"] != 5
            or isinstance(maximum, bool)
            or not math.isfinite(maximum)
            or not 0 <= maximum <= 5
            or not performance["samples"]
        ):
            raise ValueError("failed clock guard")
        for sample in performance["samples"]:
            wall = sample["wall_elapsed_seconds"]
            monotonic = sample["monotonic_elapsed_seconds"]
            drift = sample["clock_drift_seconds"]
            timestamp = datetime.fromisoformat(sample["sampled_utc"])
            if (
                timestamp.utcoffset() is None
                or any(
                    isinstance(value, bool) or not math.isfinite(value)
                    for value in (wall, monotonic, drift)
                )
                or monotonic < 0
                or abs(drift) > 5
                or abs(drift) > maximum + 0.003
                or abs(wall - monotonic - drift) > 0.003
            ):
                raise ValueError("invalid sample clock evidence")
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            "clock-discontinuity guard missing, failed or invalid; timing is unsuitable for comparison"
        ) from error


def load_run(directory, concurrency, cache_mode):
    summary = directory / "summary"
    result = json.loads((summary / "result.json").read_text())
    performance = json.loads((summary / "performance.json").read_text())
    require_valid_clock(performance)
    if (
        not result.get("passed")
        or not result.get("functional_checks_passed")
        or result.get("cleanup_errors")
    ):
        raise ValueError(f"{directory}: full functional acceptance and cleanup must pass")
    if not performance.get("completed") or not performance.get("kubelet_pull_config_matches"):
        raise ValueError(
            f"{directory}: measured kubelet pull configuration is missing or mismatched"
        )
    if performance.get("containerd_image_retention_matches") is not True:
        raise ValueError(f"{directory}: retained OCI image content was not verified on every node")
    if performance["source_revision"] != result["source_revision"]:
        raise ValueError(f"{directory}: sampler and acceptance revisions differ")
    if (
        performance["image_pull_concurrency_per_node"] != concurrency
        or performance["cache_mode"] != cache_mode
    ):
        raise ValueError(f"{directory}: expected {cache_mode} run with concurrency {concurrency}")
    steps = {step["name"]: step for step in result["steps"]}
    for phase in ("bootstrap", "convergence"):
        if phase not in steps or steps[phase]["exit_code"] != 0:
            raise ValueError(f"{directory}: {phase} did not pass")
    import_report = (
        json.loads((summary / "image-cache-import.json").read_text())
        if cache_mode == "warm"
        else None
    )
    if import_report and (
        not import_report["success"]
        or import_report["source_revision"] != result["source_revision"]
    ):
        raise ValueError(f"{directory}: cache import did not verify this source revision")

    def unrelated(records):
        return sorted(
            (item["ID"], item["Names"], item["Image"])
            for item in records
            if not item["Names"].startswith(performance["cluster"] + "-")
        )

    before = unrelated(performance["running_containers_at_start"])
    after = unrelated(performance["running_containers_at_end"])
    if before != after:
        raise ValueError(
            f"{directory}: unrelated running container identities changed during measurement"
        )
    return {
        "source_revision": result["source_revision"],
        "runner_sha256": result.get("runner_sha256"),
        "source_helpers_sha256": result.get("source_helpers_sha256"),
        "hardware": {key: performance["docker"][key] for key in HARDWARE},
        "other_running_containers": before,
        "bootstrap_seconds": steps["bootstrap"]["seconds"],
        "convergence_seconds": steps["convergence"]["seconds"],
        "bootstrap_and_convergence_seconds": round(
            steps["bootstrap"]["seconds"] + steps["convergence"]["seconds"], 3
        ),
        "cache_import_seconds": import_report["elapsed_seconds"] if import_report else 0,
        "cache_preflight_seconds": steps.get("cache-preflight", {}).get("seconds", 0),
        "cache_manifest_sha256": import_report["manifest_sha256"] if import_report else None,
        "cache_export_seconds_excluded": steps.get("cache-export", {}).get("seconds", 0),
        "resources_and_pulls": performance["summary"],
        "sampling_errors": len(performance["sample_errors"]),
        "clock_guard": performance["clock_guard"],
        "node_pressure_observations": [
            {"elapsed_seconds": sample["elapsed_seconds"], "nodes": sample["node_conditions"]}
            for sample in performance["samples"]
            if sample.get("node_conditions")
        ],
    }


def compare(cold1, cold2, warm2):
    runs = {
        name: load_run(directory, concurrency, mode)
        for name, directory, concurrency, mode in (
            ("cold1", cold1, 1, "cold"),
            ("cold2", cold2, 2, "cold"),
            ("warm2", warm2, 2, "warm"),
        )
    }
    baseline = runs["cold1"]
    for name, run in runs.items():
        for field in (
            "source_revision",
            "runner_sha256",
            "source_helpers_sha256",
            "hardware",
            "other_running_containers",
        ):
            if run[field] != baseline[field]:
                raise ValueError(f"{name}: {field} differs; do not claim a controlled speedup")
        if not run["node_pressure_observations"]:
            raise ValueError(f"{name}: node pressure evidence is missing")
    export = json.loads((cold2 / "summary/image-cache-export.json").read_text())
    if (
        not export.get("success")
        or export["manifest_sha256"] != runs["warm2"]["cache_manifest_sha256"]
    ):
        raise ValueError("warm2 must restore the verified cache exported by cold2")

    def delta(first, second):
        original = runs[first]["bootstrap_and_convergence_seconds"]
        candidate = runs[second]["bootstrap_and_convergence_seconds"]
        return {
            "seconds_saved": round(original - candidate, 3),
            "percent_saved": round((original - candidate) / original * 100, 2),
        }

    return {
        "controlled_comparison": True,
        "runs": runs,
        "cold_parallel_vs_serial": delta("cold1", "cold2"),
        "warm_cache_vs_cold_parallel": delta("cold2", "warm2"),
        "limitations": [
            "One accepted run per configuration is descriptive evidence, not a statistically established speedup.",
            "Cold means fresh kind node image stores without imported platform archives; host node-image and application-build caches remain available.",
            "Warm bootstrap includes cache import and bootstrap cache validation. Preflight before pausing retained clusters is reported separately; add it for end-to-end preparation cost. Cache export is reported separately and excluded from startup comparisons.",
            "Network/registry conditions and background activity inside unchanged containers can vary.",
            "Node pressure observations are sampled; absence of observed pressure does not prove there was no transient pressure.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cold1", "cold2", "warm2"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = compare(args.cold1, args.cold2, args.warm2)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Cannot compare runs: {error}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "runs"}, indent=2))


if __name__ == "__main__":
    main()
