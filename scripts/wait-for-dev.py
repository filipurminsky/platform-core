#!/usr/bin/env python3
"""Bounded DEV convergence check with explicit context and retained evidence."""

import argparse
import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REQUIRED = {
    # Default dev registration inventory, including the two example tenants.
    # Also validate any additional Applications returned by the API.
    "akhq",
    "cert-manager",
    "external-secrets",
    "opencost",
    "reflector",
    "platform",
    "echo-service",
    "worker-service",
    "llm-gateway",
    "audio-api",
    "stt-worker",
    "llm-worker",
    "tts-worker",
    "backstage",
    "kafka",
    "redis",
    "seaweedfs",
    "monitoring",
    "argo-rollouts",
    "envoy-gateway-config",
    "envoy-gateway",
    "gateway-api-crds",
    "namespaces",
    "vllm-inference",
    "kyverno",
    "kyverno-policies",
    "strimzi-operator",
    "keda",
    "monitoring-rules",
    "grafana-dashboards",
    "alloy",
    "loki",
    "loki-config",
    "tempo",
    "opentelemetry-collector",
    "tenant-alpha",
    "tenant-bravo",
}
ENDPOINTS = (
    ("echo.platform-core.local", "/"),
    ("echo.platform-core.local", "/healthz"),
    ("audio-api.platform-core.local", "/healthz"),
    ("backstage.platform-core.local", "/.backstage/health/v1/readiness"),
)


def application_failures(items):
    names = {item["metadata"]["name"] for item in items}
    failures = [f"Application missing: {name}" for name in sorted(REQUIRED - names)]
    for item in items:
        name = item["metadata"]["name"]
        status = item.get("status", {})
        sync = status.get("sync", {}).get("status", "Unknown")
        health = status.get("health", {}).get("status", "Unknown")
        allowed = {"Healthy", "Suspended"} if name == "vllm-inference" else {"Healthy"}
        if sync != "Synced" or health not in allowed:
            failures.append(f"{name}: sync={sync}, health={health}")
        for condition in status.get("conditions", []):
            if condition.get("type", "").endswith("Error"):
                failures.append(f"{name}: {condition['type']}: {condition.get('message', '')}")
    return failures


def inspect(context, gateway_url):
    def source_identity(source):
        # Never retain inline Helm values or parameters in diagnostic bundles.
        return {
            key: source[key]
            for key in ("repoURL", "targetRevision", "path", "chart", "ref")
            if key in source
        }

    def kubectl(*args):
        result = subprocess.run(
            ["kubectl", "--context", context, "--request-timeout=10s", *args, "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        return json.loads(result.stdout)

    applications = kubectl("-n", "argocd", "get", "applications")["items"]
    failures = application_failures(applications)
    gateway = kubectl("-n", "envoy-gateway-system", "get", "gateway", "platform")
    programmed = any(
        condition.get("type") == "Programmed"
        and condition.get("status") == "True"
        and condition.get("observedGeneration") == gateway["metadata"].get("generation")
        for condition in gateway.get("status", {}).get("conditions", [])
    )
    if not programmed:
        failures.append("Gateway platform: current generation is not Programmed=True")
    endpoints = []
    for host, path in ENDPOINTS:
        request = urllib.request.Request(gateway_url.rstrip("/") + path, headers={"Host": host})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                status = response.status
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            status = str(error)
        endpoints.append({"host": host, "path": path, "status": status})
        if status != 200:
            failures.append(f"{host}{path}: expected HTTP 200, got {status}")
    # Source and observed revision are separate: main can advance during setup.
    observations = [
        {
            "name": app["metadata"]["name"],
            "source": source_identity(app["spec"].get("source", {})),
            "sources": [source_identity(source) for source in app["spec"].get("sources", [])],
            "sync": {
                key: value
                for key, value in app.get("status", {}).get("sync", {}).items()
                if key in {"status", "revision", "revisions"}
            },
            "health": app.get("status", {}).get("health", {}).get("status", "Unknown"),
        }
        for app in applications
    ]
    return failures, {
        "applications": observations,
        "endpoints": endpoints,
        "gateway_programmed": programmed,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--gateway-url", default="http://localhost")
    parser.add_argument("--timeout", type=int, default=2400)
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    if args.timeout <= 0 or args.interval <= 0:
        parser.error("timeout and interval must be positive seconds")
    if not args.gateway_url.startswith(("http://", "https://")):
        parser.error("gateway-url must use http:// or https://")
    evidence = args.evidence_dir or Path(tempfile.mkdtemp(prefix="platform-core-dev-acceptance-"))
    evidence.mkdir(parents=True, exist_ok=True)
    print(f"Inspecting {args.context}; evidence: {evidence}", flush=True)
    started = time.monotonic()
    while True:
        observation = {}
        try:
            failures, observation = inspect(args.context, args.gateway_url)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            detail = (
                error.stderr.strip()
                if isinstance(error, subprocess.CalledProcessError)
                else str(error)
            )
            failures = [f"Unable to inspect DEV resources: {detail}"]
        elapsed = round(time.monotonic() - started, 1)
        report = {
            "context": args.context,
            "gateway_url": args.gateway_url,
            "elapsed_seconds": elapsed,
            "failures": failures,
            **observation,
        }
        (evidence / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        with (evidence / "observations.jsonl").open("a") as stream:
            stream.write(json.dumps(report) + "\n")
        if not failures:
            print(
                f"DEV acceptance passed after {elapsed}s: Applications synced, Gateway programmed, four routed HTTP checks passed."
            )
            print(
                "This does not verify GPU/AWS paths, scaffolder publishing, audio completion, or canary recovery."
            )
            return 0
        print(f"Waiting ({elapsed}s): " + "; ".join(failures), flush=True)
        if elapsed >= args.timeout:
            print(
                f"DEV acceptance failed. Inspect {evidence / 'result.json'} and docs/local-dev.md."
            )
            print(f"kubectl --context {args.context} -n argocd get applications")
            print(f"kubectl --context {args.context} get pods -A")
            return 1
        time.sleep(min(args.interval, max(0, args.timeout - elapsed)))


if __name__ == "__main__":
    raise SystemExit(main())
