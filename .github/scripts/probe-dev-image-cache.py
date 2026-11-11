#!/usr/bin/env python3
"""Exercise real platform-image OCI export/import on two sequential disposable kind nodes."""

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


CACHE = module(ROOT / "scripts/dev-image-cache.py", "probe_cache")
PERFORMANCE = module(ROOT / "scripts/dev-performance.py", "probe_performance")
PREPARATION = module(ROOT / ".github/scripts/prepare-dev-benchmark.py", "probe_preparation")


def fixture_images(path):
    images = json.loads(path.read_text())["images"]
    pattern = r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}/[a-z0-9._/-]+@sha256:[a-f0-9]{64}"
    if not isinstance(images, list) or not 1 <= len(images) <= 128:
        raise ValueError("fixture must contain 1 to 128 immutable public image references")
    if any(not isinstance(ref, str) or not re.fullmatch(pattern, ref) for ref in images):
        raise ValueError("fixture references must be immutable public registry image identities")
    if len(set(images)) != len(images):
        raise ValueError("fixture references must be unique")
    if any(ref.split("@", 1)[0].rsplit("/", 1)[-1] in CACHE.APPS for ref in images):
        raise ValueError("fixture must not contain locally built application images")
    return images


class Probe:
    def __init__(self, root, revision, evidence):
        self.root, self.revision, self.evidence = root, revision, evidence
        evidence.mkdir(parents=True, exist_ok=False)
        self.summary = evidence / "summary"
        self.logs = evidence / "logs"
        self.summary.mkdir()
        self.logs.mkdir(mode=0o700)
        self.private = Path(tempfile.mkdtemp(prefix="platform-cache-probe-private-"))
        self.cluster = "cache-probe-" + uuid.uuid4().hex[:12]
        self.env = {**os.environ, "KUBECONFIG": str(self.private / "kubeconfig")}
        self.started = time.monotonic()
        self.deadline = self.started + 1500
        self.owned = False
        self.original_clusters = self.original_containers = self.original_context = None
        self.report = {
            "passed": False,
            "source_revision": revision,
            "cluster": self.cluster,
            "scope": "real CRI pulls and OCI transfer; no full platform deployment or startup comparison",
            "phase": "preflight",
            "pulls": [],
            "cleanup_errors": [],
        }

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("cache probe exceeded its 25-minute execution budget")
        return remaining

    def run(self, args, *, timeout=60, log=None, cleanup=False, optional=False, env=None):
        budget = timeout if cleanup else min(timeout, self.remaining())
        result = subprocess.run(
            args,
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=budget,
            check=False,
        )
        if log:
            target = self.logs / (log + ".log")
            with open(
                target, "x", opener=lambda path, flags: os.open(path, flags, 0o600)
            ) as stream:
                stream.write(result.stdout + result.stderr)
        if result.returncode and not optional:
            raise subprocess.CalledProcessError(result.returncode, args, stderr=result.stderr)
        return result.stdout

    def state(self, *, cleanup=False):
        timeout = 20 if cleanup else 60
        return (
            set(self.run(["kind", "get", "clusters"], cleanup=cleanup, timeout=timeout).split()),
            set(
                self.run(
                    ["docker", "ps", "--no-trunc", "--format", "{{.ID}}"],
                    cleanup=cleanup,
                    timeout=timeout,
                ).split()
            ),
            self.run(
                ["kubectl", "config", "current-context"],
                optional=True,
                cleanup=cleanup,
                timeout=timeout,
            ).strip(),
        )

    def phase(self, name):
        self.report["phase"] = name
        print(name, flush=True)

    def create(self, label):
        self.phase("create-" + label)
        self.owned = True  # Partial create also owns cleanup.
        self.run(
            [
                "kind",
                "create",
                "cluster",
                "--name",
                self.cluster,
                "--config",
                str(self.private / "kind.yaml"),
                "--kubeconfig",
                str(self.private / "kubeconfig"),
            ],
            env=self.env,
            log="create-" + label,
            timeout=240,
        )
        nodes = CACHE.nodes_for(self.cluster)
        if set(nodes) != {"control-plane"}:
            raise ValueError("probe must own exactly one control-plane node")
        node = nodes["control-plane"]["container"]
        retention = PERFORMANCE.containerd_image_retention(
            self.run(["docker", "exec", node, "cat", "/etc/containerd/config.toml"])
        )
        self.report[label + "_retention"] = retention
        if retention["discard_unpacked_layers"] is not False:
            raise ValueError("fresh probe node did not retain compressed image layers")
        return nodes

    def delete(self, label):
        self.run(
            [
                "kind",
                "delete",
                "cluster",
                "--name",
                self.cluster,
                "--kubeconfig",
                str(self.private / "kubeconfig"),
            ],
            env=self.env,
            log="delete-" + label,
            timeout=180,
            cleanup=True,
        )
        self.owned = False

    def disk(self, label):
        usage = shutil.disk_usage(self.private)
        self.report[label + "_host_disk"] = {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }

    def execute(self):
        actual = self.run(["git", "rev-parse", "HEAD"]).strip()
        if actual != self.revision:
            raise ValueError("probe checkout differs from the requested source revision")
        images = fixture_images(self.root / "tests/fixtures/dev-cache-images.json")
        self.report["fixture_sha256"] = CACHE.digest(
            self.root / "tests/fixtures/dev-cache-images.json"
        )
        pin = PREPARATION.node_pin(
            (self.root / "terraform/modules/kind/kind-config.yaml").read_text()
        )
        self.report["kind_node_image"] = pin
        self.original_clusters, self.original_containers, self.original_context = self.state()
        if self.cluster in self.original_clusters:
            raise ValueError("refusing to reuse an existing cluster name")
        config = (
            "kind: Cluster\napiVersion: kind.x-k8s.io/v1alpha4\n"
            "networking:\n  disableDefaultCNI: true\nnodes:\n"
            "  - role: control-plane\n    image: " + pin + "\n"
        )
        (self.private / "kind.yaml").write_text(
            PERFORMANCE.configure_image_content_retention(config)
        )
        source = self.create("source")
        node = source["control-plane"]["container"]
        self.phase("pull-platform-images")
        pull_deadline = min(self.deadline, time.monotonic() + 1200)
        for index, reference in enumerate(images):
            remaining = pull_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("platform image pull budget exhausted")
            record = {"reference": reference, "passed": False}
            self.report["pulls"].append(record)
            print(f"PULL {index + 1}/{len(images)} {reference}", flush=True)
            started = time.monotonic()
            self.run(
                ["docker", "exec", node, "crictl", "pull", reference],
                timeout=min(120, remaining),
                log=f"pull-{index:03d}",
            )
            record.update(passed=True, seconds=round(time.monotonic() - started, 3))
        self.phase("export")
        self.disk("before_export")
        started = time.monotonic()
        manifest = CACHE.export_cache(self.private / "cache", self.revision, source)
        self.report["export"] = {
            "seconds": round(time.monotonic() - started, 3),
            "manifest_sha256": CACHE.digest(self.private / "cache/manifest.json"),
            "nodes": manifest["nodes"],
        }
        self.delete("source")
        destination = self.create("destination")
        self.phase("import")
        started = time.monotonic()
        CACHE.import_cache(self.private / "cache", self.revision, destination)
        self.report["import_seconds"] = round(time.monotonic() - started, 3)
        self.phase("verify-cri")
        node = destination["control-plane"]["container"]
        expected = manifest["nodes"]["control-plane"]["images"]
        inventory = self.run(
            ["docker", "exec", node, "ctr", "--namespace", "k8s.io", "images", "list"]
        )
        managed = {
            line.split()[0]
            for line in inventory.splitlines()[1:]
            if "io.cri-containerd.image=managed" in line
        }
        if not set(expected) <= managed:
            raise ValueError("imported images are missing CRI-managed labels")
        deadline = min(self.deadline, time.monotonic() + 30)
        while True:
            cri = json.loads(self.run(["docker", "exec", node, "crictl", "images", "-o", "json"]))
            visible = {
                ref
                for image in cri["images"]
                for ref in image.get("repoTags", []) + image.get("repoDigests", [])
            }
            missing = sorted(set(expected) - visible)
            if not missing or time.monotonic() >= deadline:
                break
            time.sleep(1)
        self.report["cri_missing_references"] = missing
        if missing:
            raise ValueError("imported image references are not all visible through CRI")
        self.report.update(exact_image_digests=True, cri_managed_labels=True, passed=True)
        self.phase("complete")

    def perform(self):
        original_run = CACHE.run_command

        def bounded_run(args, **kwargs):
            kwargs["timeout"] = min(kwargs.get("timeout", 60), self.remaining())
            return original_run(args, **kwargs)

        CACHE.run_command = bounded_run
        try:
            self.execute()
        except (
            OSError,
            ValueError,
            KeyError,
            subprocess.SubprocessError,
            CACHE.CacheCommandError,
        ) as error:
            self.report["failure"] = {"error_type": type(error).__name__}
            if isinstance(error, ValueError):
                self.report["failure"]["message"] = str(error)
            if isinstance(error, CACHE.CacheCommandError):
                self.report["failure"].update(
                    stage=error.stage,
                    node=error.node,
                    exit_code=error.exit_code,
                    details=CACHE.classify_command_error(error),
                    diagnostics=error.diagnostics,
                )
                diagnostic = (
                    error.stderr.encode() if isinstance(error.stderr, str) else error.stderr
                )
                with open(
                    self.logs / "cache-stderr.log",
                    "xb",
                    opener=lambda path, flags: os.open(path, flags, 0o600),
                ) as stream:
                    stream.write(diagnostic)
            self.disk("failure")
        finally:
            CACHE.run_command = original_run
            if self.owned:
                try:
                    self.delete("final")
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    self.report["cleanup_errors"].append(
                        {"phase": "delete", "error_type": type(error).__name__}
                    )
            if self.original_clusters is not None:
                try:
                    clusters, containers, context = self.state(cleanup=True)
                    self.report.update(
                        owned_cluster_absent=self.cluster not in clusters,
                        clusters_preserved=clusters == self.original_clusters,
                        containers_preserved=self.original_containers <= containers,
                        context_preserved=context == self.original_context,
                    )
                    if not all(
                        self.report[key]
                        for key in (
                            "owned_cluster_absent",
                            "clusters_preserved",
                            "containers_preserved",
                            "context_preserved",
                        )
                    ):
                        self.report["cleanup_errors"].append(
                            {"phase": "preservation", "error_type": "AssertionError"}
                        )
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    self.report["cleanup_errors"].append(
                        {"phase": "preservation", "error_type": type(error).__name__}
                    )
            shutil.rmtree(self.private)
            self.report["passed"] = self.report["passed"] and not self.report["cleanup_errors"]
            self.report["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
            (self.summary / "result.json").write_text(json.dumps(self.report, indent=2) + "\n")
        print(
            json.dumps({key: self.report[key] for key in ("passed", "phase", "cleanup_errors")}),
            flush=True,
        )
        return 0 if self.report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.expected_revision):
        parser.error("expected-revision must be a full commit SHA")

    def interrupt(signum, _frame):
        raise InterruptedError(f"signal {signum}")

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    return Probe(ROOT, args.expected_revision, args.evidence_dir.resolve()).perform()


if __name__ == "__main__":
    raise SystemExit(main())
