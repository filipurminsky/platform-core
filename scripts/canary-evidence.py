#!/usr/bin/env python3
"""Reject stale rollout phases and unrelated failures as canary success evidence."""

import json
import sys
from pathlib import Path


def read(directory, name):
    return json.loads((directory / f"{name}.json").read_text())


def check(condition, reason):
    if not condition:
        raise ValueError(reason)


def image(rollout):
    return next(
        c["image"]
        for c in rollout["spec"]["template"]["spec"]["containers"]
        if c["name"] == "echo-service"
    )


def baseline(directory, new_image):
    before = read(directory, "baseline")
    check(before["status"].get("phase") == "Healthy", "Start from a Healthy rollout")
    check(before["status"].get("stableRS"), "No stable ReplicaSet recorded")
    check(
        str(before["status"].get("observedGeneration")) == str(before["metadata"]["generation"]),
        "Initial generation has not reconciled",
    )
    check(
        image(before) != new_image,
        "Demo image must differ from the current image; prepare the demo images first",
    )
    app = read(directory, "application")
    check(
        app.get("status", {}).get("operationState", {}).get("phase")
        not in ("Running", "Terminating"),
        "Wait for the active Argo CD sync to finish",
    )
    return image(before)


def verify(directory, mode, new_image):
    before = read(directory, "baseline")
    rollout = read(directory, "rollout")
    status = rollout["status"]
    check(rollout["metadata"]["uid"] == before["metadata"]["uid"], "Rollout was replaced")
    check(
        rollout["metadata"]["generation"] > before["metadata"]["generation"],
        "New image has not changed generation",
    )
    check(
        str(status.get("observedGeneration")) == str(rollout["metadata"]["generation"]),
        "Controller has not observed this generation",
    )
    check(image(rollout) == new_image, "Desired image changed externally")
    check(
        status.get("phase") == ("Degraded" if mode == "bad" else "Healthy"),
        "Expected terminal phase not reached",
    )
    current_hash = status.get("currentPodHash")
    old_hash = before["status"]["stableRS"]
    check(current_hash and current_hash != old_hash, "No new canary ReplicaSet")
    expected_hash = old_hash if mode == "bad" else current_hash
    check(status.get("stableRS") == expected_hash, "Unexpected stable ReplicaSet")
    previous = {run["metadata"]["uid"] for run in read(directory, "previous-analysis")["items"]}
    matching = [
        run
        for run in read(directory, "analysis")["items"]
        if run["metadata"]["uid"] not in previous
        and any(
            owner.get("uid") == rollout["metadata"]["uid"]
            for owner in run["metadata"].get("ownerReferences", [])
        )
        and any(
            arg.get("name") == "canary-hash" and arg.get("value") == current_hash
            for arg in run["spec"].get("args", [])
        )
    ]
    if mode == "bad":
        check(
            any(
                run.get("status", {}).get("phase") == "Failed"
                and any(
                    metric.get("name") in ("success-rate", "p95-latency-seconds")
                    and metric.get("phase") == "Failed"
                    for metric in run["status"].get("metricResults", [])
                )
                for run in matching
            ),
            "No new SLO failure for this canary (missing metrics or image-pull failures do not count)",
        )
    else:
        check(
            any(run.get("status", {}).get("phase") == "Successful" for run in matching),
            "No successful analysis for this canary",
        )
    route = read(directory, "route")
    refs = [ref for rule in route["spec"]["rules"] for ref in rule.get("backendRefs", [])]
    check(
        any(ref.get("name") == "echo-service" and ref.get("weight", 1) > 0 for ref in refs),
        "Stable backend has no traffic",
    )
    check(
        all(ref.get("weight", 1) == 0 for ref in refs if ref.get("name") != "echo-service"),
        "Traffic still reaches a non-stable backend",
    )
    parents = [
        parent
        for parent in route.get("status", {}).get("parents", [])
        if parent.get("parentRef", {}).get("name") == "platform"
        and parent["parentRef"].get("namespace", "apps") == "envoy-gateway-system"
    ]
    check(
        any(
            all(
                any(
                    c.get("type") == kind
                    and c.get("status") == "True"
                    and c.get("observedGeneration") == route["metadata"]["generation"]
                    for c in parent.get("conditions", [])
                )
                for kind in ("Accepted", "ResolvedRefs")
            )
            for parent in parents
        ),
        "Gateway has not accepted this route generation",
    )
    check(
        read(directory, "service")["spec"]["selector"].get("rollouts-pod-template-hash")
        == expected_hash,
        "Stable Service selects the wrong ReplicaSet",
    )
    check(
        any(
            rs["metadata"].get("labels", {}).get("rollouts-pod-template-hash") == expected_hash
            and any(
                owner.get("uid") == rollout["metadata"]["uid"]
                for owner in rs["metadata"].get("ownerReferences", [])
            )
            and rs.get("status", {}).get("availableReplicas", 0) > 0
            for rs in read(directory, "replicasets")["items"]
        ),
        "No available stable ReplicaSet",
    )
    return expected_hash


def response(directory, mode, attempt):
    before = read(directory, "baseline")
    rollout = read(directory, "rollout")
    expected_hash = (
        before["status"]["stableRS"] if mode == "bad" else rollout["status"]["currentPodHash"]
    )
    hosts = {
        pod["metadata"]["name"]
        for pod in read(directory, "pods")["items"]
        if pod["metadata"].get("labels", {}).get("rollouts-pod-template-hash") == expected_hash
        and any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in pod.get("status", {}).get("conditions", [])
        )
    }
    reply = read(directory, f"response-{attempt}")
    check(
        reply.get("service") == "echo-service" and reply.get("hostname") in hosts,
        "Response did not come from a ready stable pod",
    )


def restored(directory):
    before = read(directory, "baseline")
    rollout = read(directory, "restored")
    check(image(rollout) == image(before), "Original image not restored")
    check(
        str(rollout["status"].get("observedGeneration")) == str(rollout["metadata"]["generation"]),
        "Restored generation not yet observed",
    )
    check(rollout["status"].get("phase") == "Healthy", "Restored rollout not yet healthy")
    check(
        rollout["status"].get("stableRS") == before["status"]["stableRS"],
        "Original stable ReplicaSet not yet restored",
    )


if __name__ == "__main__":
    try:
        command, location, mode, new_image = sys.argv[1:5]
        directory = Path(location)
        if command == "baseline":
            print(baseline(directory, new_image))
        elif command == "verify":
            verify(directory, mode, new_image)
        elif command == "response":
            response(directory, mode, sys.argv[5])
        elif command == "restored":
            restored(directory)
        else:
            raise ValueError(f"Unknown command: {command}")
    except (ValueError, KeyError, StopIteration) as exc:
        sys.exit(str(exc))
