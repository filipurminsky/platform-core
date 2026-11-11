#!/usr/bin/env python3
"""Sample an explicitly named kind run without changing cluster resources."""

import argparse
import json
import math
import re
import shutil
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path


def configure_pull_concurrency(config_text, concurrency):
    """Change only the reviewed, explicit kubelet pull limit in the kind config."""
    if concurrency not in (1, 2):
        raise ValueError("image-pull-concurrency must be 1 or 2")
    if len(re.findall(r"^    serializeImagePulls: false$", config_text, re.M)) != 1:
        raise ValueError("kind config must contain one explicit nonserialized KubeletConfiguration")
    pattern = r"^    maxParallelImagePulls: [12]$"
    if len(re.findall(pattern, config_text, re.M)) != 1:
        raise ValueError(
            "kind config must contain exactly one reviewed maxParallelImagePulls limit"
        )
    return re.sub(pattern, f"    maxParallelImagePulls: {concurrency}", config_text, flags=re.M)


def command(args, timeout=20):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout


def size_bytes(value):
    match = re.fullmatch(r"\s*([0-9.]+)\s*([kKMGT]?i?B)\s*", value)
    if not match:
        raise ValueError(f"unrecognized Docker size: {value}")
    units = {"B": 1, **{f"{unit}B": 1000**i for i, unit in enumerate("KMGT", 1)}}
    units.update({f"{unit}iB": 1024**i for i, unit in enumerate("KMGT", 1)})
    return round(float(match[1]) * units[match[2].replace("k", "K")])


def parse_stats(raw):
    rows = []
    for line in raw.splitlines():
        item = json.loads(line)
        memory, limit = item["MemUsage"].split("/")
        network_rx, network_tx = item["NetIO"].split("/")
        disk_read, disk_write = item["BlockIO"].split("/")
        rows.append(
            {
                "node": item["Name"],
                "cpu_percent": float(item["CPUPerc"].rstrip("%")),
                "memory_bytes": size_bytes(memory),
                "memory_limit_bytes": size_bytes(limit),
                "network_rx_bytes": size_bytes(network_rx),
                "network_tx_bytes": size_bytes(network_tx),
                "block_read_bytes": size_bytes(disk_read),
                "block_write_bytes": size_bytes(disk_write),
            }
        )
    return rows


def pull_events(items):
    return [
        {
            "uid": item["metadata"]["uid"],
            "namespace": item["metadata"]["namespace"],
            "pod": item.get("involvedObject", {}).get("name"),
            "reason": item.get("reason"),
            "already_present": "already present" in item.get("message", ""),
            "registry_pull": "Successfully pulled image" in item.get("message", ""),
            "pull_duration": (
                match[1]
                if (match := re.search(r" in ([0-9.hms]+) \(", item.get("message", "")))
                else None
            ),
            "pull_duration_including_wait": (
                match[1]
                if (
                    match := re.search(
                        r"\(([0-9.hms]+) including waiting\)", item.get("message", "")
                    )
                )
                else None
            ),
            "image_size_bytes": (
                int(match[1])
                if (match := re.search(r"Image size: ([0-9]+) bytes\.", item.get("message", "")))
                else None
            ),
            "count": item.get("count", 1),
            "first_timestamp": item.get("firstTimestamp") or item.get("eventTime"),
            "last_timestamp": item.get("lastTimestamp") or item.get("eventTime"),
        }
        for item in items
        if item.get("reason") in ("Pulling", "Pulled", "Failed", "BackOff")
        and ("image" in item.get("message", "").lower())
    ]


def summarize(samples, events):
    metrics = {}
    for sample in samples:
        for node in sample.get("nodes", []):
            entry = metrics.setdefault(node["node"], {})
            for field in (
                "cpu_percent",
                "memory_bytes",
                "network_rx_bytes",
                "network_tx_bytes",
                "block_read_bytes",
                "block_write_bytes",
            ):
                entry[f"peak_{field}"] = max(entry.get(f"peak_{field}", 0), node[field])
    return {
        "per_node": metrics,
        "peak_cluster_memory_bytes": max(
            (sum(node["memory_bytes"] for node in sample.get("nodes", [])) for sample in samples),
            default=0,
        ),
        "peak_cluster_cpu_percent": max(
            (sum(node["cpu_percent"] for node in sample.get("nodes", [])) for sample in samples),
            default=0,
        ),
        "successful_pull_events": sum(e["count"] for e in events if e["reason"] == "Pulled"),
        "already_present_events": sum(
            e["count"] for e in events if e["reason"] == "Pulled" and e["already_present"]
        ),
        "registry_pull_events": sum(
            e["count"] for e in events if e["reason"] == "Pulled" and e["registry_pull"]
        ),
    }


def running_containers():
    return [
        {key: item.get(key) for key in ("ID", "Names", "Image", "State")}
        for line in command(["docker", "ps", "--no-trunc", "--format", "{{json .}}"]).splitlines()
        if (item := json.loads(line))
    ]


def application_states(items):
    return [
        {
            "name": item["metadata"]["name"],
            "sync": item.get("status", {}).get("sync", {}).get("status"),
            "health": item.get("status", {}).get("health", {}).get("status"),
            "phase": item.get("status", {}).get("operationState", {}).get("phase"),
            "retry_count": item.get("status", {}).get("operationState", {}).get("retryCount", 0),
        }
        for item in items
    ]


def kubelet_pull_config(text):
    values = {}
    for field in ("serializeImagePulls", "maxParallelImagePulls"):
        match = re.search(rf"^{field}: (\S+)\s*$", text, re.M)
        if not match:
            raise ValueError(f"live kubelet configuration omits {field}")
        values[field] = match[1] == "true" if field == "serializeImagePulls" else int(match[1])
    return values


def pressure_conditions(items):
    return {
        item["metadata"]["name"]: [
            {key: condition.get(key) for key in ("type", "status", "lastTransitionTime")}
            for condition in item.get("status", {}).get("conditions", [])
            if condition.get("type") in ("MemoryPressure", "DiskPressure", "PIDPressure", "Ready")
        ]
        for item in items
    }


class ClockGuard:
    """Latch suspend/clock-adjustment drift without attributing its cause."""

    threshold_seconds = 5

    def __init__(self):
        self.started_monotonic = time.monotonic()
        self.started_wall = time.time()
        self.max_absolute_drift = 0.0
        self.passed = True

    def observe(self):
        monotonic_elapsed = time.monotonic() - self.started_monotonic
        wall_now = time.time()
        wall_elapsed = wall_now - self.started_wall
        drift = wall_elapsed - monotonic_elapsed
        self.max_absolute_drift = max(self.max_absolute_drift, abs(drift))
        self.passed = self.passed and math.isfinite(drift) and abs(drift) <= self.threshold_seconds
        return {
            "sampled_utc": datetime.fromtimestamp(wall_now, UTC).isoformat(),
            "elapsed_seconds": round(monotonic_elapsed, 3),
            "monotonic_elapsed_seconds": round(monotonic_elapsed, 3),
            "wall_elapsed_seconds": round(wall_elapsed, 3),
            "clock_drift_seconds": round(drift, 3),
        }

    def evidence(self):
        return {
            "passed": self.passed,
            "threshold_seconds": self.threshold_seconds,
            "max_absolute_drift_seconds": round(self.max_absolute_drift, 3),
        }


def write_report(path, report):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n")
    temporary.replace(path)


def monitor(args):
    stop = False

    def request_stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    clock = ClockGuard()
    started = clock.started_monotonic
    samples, events, errors, kubelets = [], {}, [], {}

    def stopping():
        clock.observe()
        return (
            not clock.passed
            or stop
            or args.stop_file.exists()
            or time.monotonic() - started >= args.max_duration
        )

    info = json.loads(command(["docker", "info", "--format", "{{json .}}"]))
    report = {
        "schema_version": 1,
        "cluster": args.cluster,
        "context": args.context,
        "source_revision": args.source_revision,
        "image_pull_concurrency_per_node": args.image_pull_concurrency,
        "cache_mode": args.cache_mode,
        "started_utc": datetime.fromtimestamp(clock.started_wall, UTC).isoformat(),
        "docker": {
            key: info.get(key)
            for key in ("NCPU", "MemTotal", "Architecture", "ServerVersion", "OperatingSystem")
        },
        "host_free_disk_bytes_at_start": shutil.disk_usage(args.output.parent).free,
        "running_containers_at_start": running_containers(),
        "limitations": [
            "Docker node CPU percent uses 100 percent per core; memory excludes reclaimable cache per docker stats.",
            "Network and block I/O are cumulative node-container counters, not registry bytes or host physical disk throughput.",
            "Fresh kind nodes still reuse the host kind image and application BuildKit cache; cold means no imported platform OCI cache.",
            "Other running containers are recorded to expose contention; they are never stopped or changed by the sampler.",
            "Kubernetes Events may aggregate attempts or expire; sample errors are retained, never treated as zero usage.",
            "Wall/monotonic drift over five seconds invalidates timing; it can indicate suspend or a clock correction, without identifying which.",
        ],
    }
    while True:
        sample = clock.observe()
        if not clock.passed:
            samples.append(sample)
            report.update(
                samples=samples,
                elapsed_seconds=sample["elapsed_seconds"],
                completed=True,
                clock_guard=clock.evidence(),
                suitable_for_comparison=False,
                failure_reason="clock-discontinuity",
            )
            write_report(args.output, report)
            return 1
        try:
            nodes = command(["kind", "get", "nodes", "--name", args.cluster]).split()
            if nodes:
                sample["nodes"] = parse_stats(
                    command(["docker", "stats", "--no-stream", "--format", "{{json .}}", *nodes])
                )
                for node in nodes:
                    if stopping():
                        break
                    if node not in kubelets:
                        kubelets[node] = kubelet_pull_config(
                            command(
                                ["docker", "exec", node, "cat", "/var/lib/kubelet/config.yaml"],
                                timeout=10,
                            )
                        )
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(
                {
                    "elapsed_seconds": sample["elapsed_seconds"],
                    "operation": "node-stats",
                    "error_type": type(error).__name__,
                }
            )
        try:
            if stopping():
                raise InterruptedError()
            data = json.loads(
                command(
                    [
                        "kubectl",
                        "--context",
                        args.context,
                        "--request-timeout=10s",
                        "get",
                        "events",
                        "-A",
                        "-o",
                        "json",
                    ]
                )
            )
            for event in pull_events(data["items"]):
                events[event["uid"]] = event
        except InterruptedError:
            pass
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(
                {
                    "elapsed_seconds": sample["elapsed_seconds"],
                    "operation": "pull-events",
                    "error_type": type(error).__name__,
                }
            )
        try:
            if stopping():
                raise InterruptedError()
            data = json.loads(
                command(
                    [
                        "kubectl",
                        "--context",
                        args.context,
                        "--request-timeout=10s",
                        "-n",
                        "argocd",
                        "get",
                        "applications",
                        "-o",
                        "json",
                    ]
                )
            )
            sample["applications"] = application_states(data["items"])
            if stopping():
                raise InterruptedError()
            conditions = json.loads(
                command(
                    [
                        "kubectl",
                        "--context",
                        args.context,
                        "--request-timeout=10s",
                        "get",
                        "crd",
                        "scaledobjects.keda.sh",
                        "-o",
                        "jsonpath={.status.conditions}",
                    ]
                )
            )
            sample["keda_established"] = [
                {key: item.get(key) for key in ("type", "status", "lastTransitionTime")}
                for item in conditions
                if item.get("type") == "Established"
            ]
            if stopping():
                raise InterruptedError()
            node_data = json.loads(
                command(
                    [
                        "kubectl",
                        "--context",
                        args.context,
                        "--request-timeout=10s",
                        "get",
                        "nodes",
                        "-o",
                        "json",
                    ]
                )
            )
            sample["node_conditions"] = pressure_conditions(node_data["items"])
        except InterruptedError:
            pass
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(
                {
                    "elapsed_seconds": sample["elapsed_seconds"],
                    "operation": "dependency-state",
                    "error_type": type(error).__name__,
                }
            )
        samples.append(sample)
        ended = stopping()
        if ended and clock.passed:
            report["running_containers_at_end"] = running_containers()
        sample.update(clock.observe())
        report.update(
            samples=samples,
            pull_events=list(events.values()),
            sample_errors=errors,
            summary=summarize(samples, list(events.values())),
            elapsed_seconds=round(time.monotonic() - started, 3),
            completed=ended or not clock.passed,
            clock_guard=clock.evidence(),
            suitable_for_comparison=clock.passed,
            kubelet_pull_config=kubelets,
            kubelet_pull_config_matches=bool(kubelets)
            and all(
                config
                == {
                    "serializeImagePulls": False,
                    "maxParallelImagePulls": args.image_pull_concurrency,
                }
                for config in kubelets.values()
            )
            and set(kubelets)
            == {node["node"] for record in samples for node in record.get("nodes", [])},
        )
        if not clock.passed:
            report["failure_reason"] = "clock-discontinuity"
        write_report(args.output, report)
        if ended or not clock.passed:
            return 0 if clock.passed and report["kubelet_pull_config_matches"] else 1
        deadline = time.monotonic() + args.interval
        while time.monotonic() < deadline and not stopping():
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["monitor"])
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--image-pull-concurrency", type=int, choices=[1, 2], required=True)
    parser.add_argument("--cache-mode", choices=["cold", "warm"], required=True)
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--max-duration", type=float, default=7200)
    args = parser.parse_args()
    if args.context != f"kind-{args.cluster}" or not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]*", args.cluster
    ):
        parser.error("context must match the explicit kind cluster")
    if not re.fullmatch(r"[a-f0-9]{40}", args.source_revision):
        parser.error("source-revision must be a full commit SHA")
    if args.interval <= 0 or args.max_duration <= 0:
        parser.error("sampling interval and maximum duration must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    return monitor(args)


if __name__ == "__main__":
    raise SystemExit(main())
