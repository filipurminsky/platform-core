#!/usr/bin/env python3
"""Create an owned kind cluster, verify the published DEV stack, and always clean up.

Only summary/ is suitable for CI artifact upload. Kubeconfigs and the cloned
checkout live in a separate private temporary directory, never under evidence.
An existing cluster is paused only with explicit --pause-cluster and restored.
"""

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def module(path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive seconds")
    return number


def redact(line):
    line = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", line)
    return re.sub(
        r"(?i)((?:password|token|api[-_]?key|client[-_]?key[-_]?data|client[-_]?certificate[-_]?data)\s*[:=]\s*)[^\s,]+",
        r"\1[REDACTED]",
        line,
    )


def reserve_ports(count):
    sockets = []
    try:
        for _ in range(count):
            listener = socket.socket()
            sockets.append(listener)
            listener.bind(("127.0.0.1", 0))
        return sockets, [s.getsockname()[1] for s in sockets]
    except BaseException:
        for listener in sockets:
            listener.close()
        raise


def stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    # A shell may exit before its descendants. Also terminate those descendants.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin can return EPERM for an already reaped process group. The
        # caller also verifies the stdout pipe closes; a live orphan fails.
        if process.poll() is None:
            raise
    process.wait(timeout=10)


def smoke_summary(source, evidence):
    log = (evidence / "logs/smoke.log").read_text()
    ping = set(re.findall(r"\bsmoke-[a-f0-9]{32}\b", log))
    poison = set(re.findall(r"\bpoison-[a-f0-9]{32}\b", log))
    if len(ping) != 1 or len(poison) != 1:
        raise RuntimeError("Smoke log does not identify exactly one owned ping and poison job")
    job = json.loads((evidence / "smoke/job.json").read_text())
    audio = re.fullmatch(r"audio/([a-f0-9]{32})", job.get("keys", {}).get("audio", ""))
    if not audio:
        raise RuntimeError("Smoke audio result has no valid owned object key")
    verifier = module(source / "scripts/smoke-evidence.py")
    ping_id, poison_id, audio_id = next(iter(ping)), next(iter(poison)), audio[1]
    if not all(
        verifier.verify(mode, evidence / "smoke", identifier)
        for mode, identifier in (
            ("processed", ping_id),
            ("dlq", poison_id),
            ("completed", audio_id),
        )
    ):
        raise RuntimeError("Smoke result evidence does not match its actual submitted jobs")
    return {
        "passed": True,
        "ping_job_id": ping_id,
        "poison_job_id": poison_id,
        "audio_job_id": audio_id,
        "audio_status": job["status"],
        "output_keys": {
            key: job["keys"][key] for key in ("audio", "transcript", "summary", "speech")
        },
    }


class Acceptance:
    def __init__(self, args):
        self.args = args
        suffix = (
            datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%S")
            + "-"
            + uuid.uuid4().hex[:8]
        )
        self.cluster = "platform-acceptance-" + suffix
        self.context = "kind-" + self.cluster
        self.evidence = args.evidence_dir or Path(tempfile.gettempdir()) / self.cluster
        self.evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.summary = self.evidence / "summary"
        self.summary.mkdir()
        (self.evidence / "logs").mkdir()
        self.private = Path(tempfile.mkdtemp(prefix=self.cluster + "-private-"))
        self.source = self.private / "source"
        self.original_env = os.environ.copy()
        self.env = {
            **self.original_env,
            "KUBECONFIG": str(self.private / "kubeconfig"),
            "KUBE_CONTEXT": self.context,
        }
        self.report = {
            "passed": False,
            "cluster": self.cluster,
            "steps": [],
            "cleanup_errors": [],
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "started_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "limitations": [
                "DEV stubs; no AWS/GPU claim",
                "No external scaffolder publishing",
                "GitOps tracks published main; revision checked again after tests",
            ],
        }
        self.owned = False
        self.pause_attempted = False
        self.paused = {}
        self.ports = []
        self.monitor = None
        self.monitor_stopped = False
        self.initial_context = None
        self.initial_clusters = None
        self.initial_containers = None

    def save(self):
        (self.summary / "result.json").write_text(json.dumps(self.report, indent=2) + "\n")

    def capture(self, command, *, env=None, cwd=None, timeout=60, optional=False):
        result = subprocess.run(
            command,
            env=env or self.original_env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode and not optional:
            raise RuntimeError(f"{command[0]} inspection failed (exit {result.returncode})")
        return result.stdout.strip()

    def run(self, name, command, *, env=None, cwd=None, timeout=180, required=True):
        print(f"START {name}", flush=True)
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=cwd or self.source,
            env=env or self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        log_path = self.evidence / "logs" / (name + ".log")

        def drain():
            with log_path.open("w") as log:
                for line in process.stdout:
                    log.write(redact(line))
                    log.flush()

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        error = None
        try:
            while True:
                self.check_monitor(name)
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    process.wait(timeout=min(60, remaining))
                    self.check_monitor(name)
                    break
                except subprocess.TimeoutExpired:
                    print(f"RUNNING {name}: {int(time.monotonic() - started)}s", flush=True)
        except BaseException as caught:
            error = caught
            stop_group(process)
        finally:
            reader.join(timeout=1)
            if reader.is_alive():
                stop_group(process)
                reader.join(timeout=10)
                error = error or RuntimeError(f"{name} left a background process")
            process.stdout.close()
            self.report["steps"].append(
                {
                    "name": name,
                    "exit_code": process.returncode,
                    "seconds": round(time.monotonic() - started, 3),
                    "interrupted": error is not None,
                }
            )
            self.save()
        print(f"END {name}: exit {process.returncode}", flush=True)
        if error:
            raise error
        if required and process.returncode:
            raise RuntimeError(f"{name} failed; inspect its local log")
        return process.returncode

    def check_monitor(self, phase):
        if phase not in {"bootstrap", "convergence"} or self.monitor_stopped:
            return
        if self.monitor is not None:
            status = self.monitor.poll()
            if status is not None:
                raise RuntimeError(
                    f"Performance sampler exited unexpectedly during {phase} (exit {status}); "
                    "inspect summary/performance.json"
                )

    def clusters(self):
        return set(self.capture(["kind", "get", "clusters"]).split())

    def running_containers(self):
        rows = self.capture(["docker", "ps", "--no-trunc", "--format", "{{json .}}"])
        return {row["ID"]: row["Names"] for row in map(json.loads, rows.splitlines())}

    def diagnostics(self):
        """Allowlist status fields; never save Pod specs, ConfigMaps or Secrets."""
        output = {}
        for resource in ("pods", "applications.argoproj.io"):
            raw = self.capture(
                [
                    "kubectl",
                    "--context",
                    self.context,
                    "--request-timeout=10s",
                    "get",
                    resource,
                    "-A",
                    "-o",
                    "json",
                ],
                env=self.env,
                timeout=20,
            )
            output[resource] = []
            for item in json.loads(raw)["items"]:
                status = item.get("status", {})
                output[resource].append(
                    {
                        "name": item["metadata"]["name"],
                        "namespace": item["metadata"].get("namespace"),
                        "phase": status.get("phase"),
                        "health": status.get("health", {}).get("status"),
                        "sync": status.get("sync", {}).get("status"),
                        "operation_phase": status.get("operationState", {}).get("phase"),
                        "retry_count": status.get("operationState", {}).get("retryCount"),
                        "containers": [
                            {
                                "name": c["name"],
                                "ready": c.get("ready"),
                                "restarts": c.get("restartCount"),
                                "waiting_reason": c.get("state", {})
                                .get("waiting", {})
                                .get("reason"),
                            }
                            for c in status.get("containerStatuses", [])
                        ],
                    }
                )
        (self.summary / "resource-status.json").write_text(json.dumps(output, indent=2) + "\n")

    def containers(self, cluster):
        ids = self.capture(
            ["docker", "ps", "-aq", "--filter", "label=io.x-k8s.kind.cluster=" + cluster]
        ).split()
        if not ids:
            raise RuntimeError("Explicit pause cluster has no kind containers")
        items = json.loads(self.capture(["docker", "inspect", *ids]))
        if any(c["Config"]["Labels"].get("io.x-k8s.kind.cluster") != cluster for c in items):
            raise RuntimeError("Kind container ownership labels differ")
        return {c["Id"]: c["State"]["Running"] for c in items}

    def wait_nodes(self):
        deadline = time.monotonic() + 300
        expected = len(self.paused)
        context = "kind-" + self.args.pause_cluster
        while time.monotonic() < deadline:
            try:
                data = json.loads(
                    self.capture(
                        [
                            "kubectl",
                            "--context",
                            context,
                            "--request-timeout=10s",
                            "get",
                            "nodes",
                            "-o",
                            "json",
                        ],
                        timeout=20,
                    )
                )
                if len(data["items"]) == expected and all(
                    any(
                        c["type"] == "Ready" and c["status"] == "True"
                        for c in node.get("status", {}).get("conditions", [])
                    )
                    for node in data["items"]
                ):
                    return
            except ValueError, RuntimeError, subprocess.SubprocessError:
                pass  # API transport/RBAC caches can be unavailable immediately after restart.
            time.sleep(5)
        raise RuntimeError("Paused cluster API/nodes did not recover within 300 seconds")

    def health(self, name, context, gateway, timeout, env):
        self.run(
            name,
            [
                sys.executable,
                "scripts/wait-for-dev.py",
                "--context=" + context,
                "--gateway-url=" + gateway,
                "--timeout=" + str(timeout),
                "--evidence-dir=" + str(self.evidence / name),
            ],
            env=env,
            timeout=timeout + 90,
        )
        shutil.copyfile(self.evidence / name / "result.json", self.summary / (name + ".json"))

    def stop_monitor(self):
        if self.monitor and not self.monitor_stopped:
            (self.private / "monitor.stop").touch()
            try:
                self.monitor.wait(timeout=45)
            except subprocess.TimeoutExpired:
                stop_group(self.monitor)
                raise RuntimeError("Performance sampler did not stop cleanly") from None
            self.monitor_stopped = True
            if self.monitor.returncode:
                raise RuntimeError("Performance sampler failed")

    def execute(self):
        self.initial_context = self.capture(["kubectl", "config", "current-context"], optional=True)
        self.initial_clusters = self.clusters()
        self.initial_containers = self.running_containers()
        self.report["initial_running_containers"] = self.initial_containers
        self.report["initial_context"] = self.initial_context
        if self.cluster in self.initial_clusters:
            raise RuntimeError("Generated cluster name exists; refusing ownership")
        manifest = (ROOT / "kubernetes/platform/argocd/app-of-apps.yaml").read_text()
        match = re.search(r"^\s+repoURL: (https://github.com/[^\s]+)\s*$", manifest, re.MULTILINE)
        if not match:
            raise RuntimeError("Expected a published GitHub repository in app-of-apps.yaml")
        repository = match[1]
        self.run(
            "clone",
            ["git", "clone", "--depth", "1", "--branch", "main", repository, str(self.source)],
            cwd=self.private,
        )
        revision = self.capture(["git", "rev-parse", "HEAD"], cwd=self.source)
        if self.args.expected_revision and self.args.expected_revision != revision:
            raise RuntimeError("Published main differs from --expected-revision")
        self.report.update({"source_revision": revision, "repository": repository})
        self.report["source_helpers_sha256"] = {
            name: hashlib.sha256((self.source / "scripts" / name).read_bytes()).hexdigest()
            for name in (
                "bootstrap.sh",
                "dev-performance.py",
                "verify-dev-portal.py",
                "test-redis-persistence.py",
                "test-tenant-access.py",
                "smoke-test.sh",
            )
        }
        self.run(
            "preflight",
            ["./scripts/bootstrap.sh", "--mode=local", "--check", "--require-published-source"],
        )
        self.ports, ports = reserve_ports(3)
        self.report["ports"] = dict(zip(("http", "https", "smoke"), ports, strict=True))
        gateway = f"http://127.0.0.1:{ports[0]}"
        config = (self.source / "terraform/modules/kind/kind-config.yaml").read_text()
        for original, replacement in zip((80, 443), ports[:2], strict=True):
            old = f"        hostPort: {original}\n"
            if config.count(old) != 1:
                raise RuntimeError("Unexpected kind port mapping; refusing unsafe transformation")
            config = config.replace(
                old, f"        hostPort: {replacement}\n        listenAddress: 127.0.0.1\n"
            )
        performance = module(self.source / "scripts/dev-performance.py")
        config = performance.configure_pull_concurrency(config, self.args.image_pull_concurrency)
        config_path = self.private / "kind.yaml"
        config_path.write_text(config)
        if self.args.image_cache:
            self.run(
                "cache-preflight",
                [
                    sys.executable,
                    "scripts/dev-image-cache.py",
                    "validate",
                    "--cache-dir=" + str(self.args.image_cache),
                    "--source-revision=" + revision,
                    "--kind-config=" + str(config_path),
                    "--report=" + str(self.summary / "image-cache-preflight.json"),
                ],
                timeout=900,
            )
        if self.args.pause_cluster:
            if self.args.pause_cluster not in self.initial_clusters:
                raise RuntimeError("Explicit pause cluster does not exist")
            self.paused = self.containers(self.args.pause_cluster)
            self.report["paused_container_ids"] = list(self.paused)
            if not all(self.paused.values()):
                raise RuntimeError(
                    "All explicitly paused kind containers must initially be running"
                )
            environment = self.capture(
                [
                    "kubectl",
                    "--context",
                    "kind-" + self.args.pause_cluster,
                    "--request-timeout=10s",
                    "-n",
                    "argocd",
                    "get",
                    "secret",
                    "in-cluster",
                    "-o",
                    "jsonpath={.metadata.labels.environment}",
                ]
            )
            if environment != "dev":
                raise RuntimeError("Only an explicitly selected DEV kind cluster can be paused")
            self.health(
                "retained-before",
                "kind-" + self.args.pause_cluster,
                self.args.restore_gateway_url,
                60,
                self.original_env,
            )
            self.pause_attempted = True
            self.run(
                "pause-retained",
                ["docker", "stop", "--time", "60", *self.paused],
                env=self.original_env,
                timeout=240,
            )
        if self.cluster in self.clusters():
            raise RuntimeError("Fresh cluster name appeared after preflight; refusing ownership")
        # Bind reservations guard selection; kind must bind the released ports itself.
        # A concurrent external bind fails bootstrap; we never stop its owner.
        for listener in self.ports[:2]:
            listener.close()
        monitor_command = [
            sys.executable,
            "scripts/dev-performance.py",
            "monitor",
            "--cluster=" + self.cluster,
            "--context=" + self.context,
            "--output=" + str(self.summary / "performance.json"),
            "--stop-file=" + str(self.private / "monitor.stop"),
            "--source-revision=" + revision,
            "--image-pull-concurrency=" + str(self.args.image_pull_concurrency),
            "--cache-mode=" + ("warm" if self.args.image_cache else "cold"),
        ]
        self.monitor = subprocess.Popen(
            monitor_command,
            cwd=self.source,
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.owned = True
        command = [
            "./scripts/bootstrap.sh",
            "--mode=local",
            "--cluster-name=" + self.cluster,
            "--kind-config=" + str(config_path),
            "--require-published-source",
            "--install-timeout=" + str(self.args.install_timeout),
        ]
        if self.args.image_cache:
            command += [
                "--image-cache=" + str(self.args.image_cache),
                "--image-cache-report=" + str(self.summary / "image-cache-import.json"),
            ]
        self.run("bootstrap", command, timeout=1800 + 2 * self.args.install_timeout)
        self.health("convergence", self.context, gateway, self.args.timeout, self.env)
        self.report["bootstrap_and_convergence_seconds"] = round(
            sum(
                step["seconds"]
                for step in self.report["steps"]
                if step["name"] in ("bootstrap", "convergence")
            ),
            3,
        )
        self.stop_monitor()
        self.run(
            "redis",
            [sys.executable, "scripts/test-redis-persistence.py"],
            env={
                **self.env,
                "ALLOW_REDIS_POD_REPLACEMENT": "1",
                "EVIDENCE_DIR": str(self.evidence / "redis"),
            },
            timeout=360,
        )
        shutil.copyfile(
            self.evidence / "redis/redis-pod-persistence.json", self.summary / "redis.json"
        )
        self.ports[2].close()
        self.run(
            "smoke",
            ["./scripts/smoke-test.sh"],
            env={
                **self.env,
                "EVIDENCE_DIR": str(self.evidence / "smoke"),
                "LOCAL_PORT": str(ports[2]),
            },
            timeout=1200,
        )
        (self.summary / "smoke.json").write_text(
            json.dumps(smoke_summary(self.source, self.evidence), indent=2) + "\n"
        )
        self.run(
            "tenant",
            [sys.executable, "scripts/test-tenant-access.py"],
            env={**self.env, "EVIDENCE_DIR": str(self.evidence / "tenant")},
            timeout=420,
        )
        shutil.copyfile(self.evidence / "tenant/tenant-access.json", self.summary / "tenant.json")
        self.run(
            "portal",
            [
                sys.executable,
                "scripts/verify-dev-portal.py",
                "--context=" + self.context,
                "--gateway-url=" + gateway,
                "--repository=" + repository,
                "--expected-revision=" + revision,
                "--output=" + str(self.summary / "portal.json"),
            ],
            timeout=300,
        )
        self.health("final-health", self.context, gateway, 300, self.env)
        self.run("final-source", [sys.executable, "scripts/verify-dev-source.py"])
        if self.args.write_image_cache:
            self.run(
                "cache-export",
                [
                    sys.executable,
                    "scripts/dev-image-cache.py",
                    "export",
                    "--cluster=" + self.cluster,
                    "--cache-dir=" + str(self.args.write_image_cache),
                    "--source-revision=" + revision,
                    "--report=" + str(self.summary / "image-cache-export.json"),
                    "--diagnostic-log=" + str(self.evidence / "logs/cache-export-stderr.log"),
                ],
                timeout=900,
            )
        self.report["functional_checks_passed"] = True

    def cleanup(self):
        def attempt(name, action):
            try:
                action()
            except BaseException as error:
                self.report["cleanup_errors"].append(
                    {
                        "step": name,
                        "error_type": type(error).__name__,
                        "message": str(error)
                        if isinstance(error, RuntimeError)
                        else "cleanup operation failed",
                    }
                )
                self.save()

        attempt("sampler", self.stop_monitor)
        if self.owned:
            attempt("diagnostics", self.diagnostics)
            for index in (1, 2):
                attempt(
                    f"teardown-{index}",
                    lambda i=index: self.run(
                        f"teardown-{i}",
                        ["./scripts/teardown.sh", "--mode=local", "--cluster-name=" + self.cluster],
                        timeout=180,
                    ),
                )

            def absent():
                if self.cluster in self.clusters():
                    raise RuntimeError("Owned cluster still exists after teardown")
                self.report["owned_cluster_absent"] = True

            attempt("owned-cluster-absent", absent)
        if self.pause_attempted:

            def restore():
                if set(self.containers(self.args.pause_cluster)) != set(self.paused):
                    raise RuntimeError("Paused container identities changed; refusing replacements")
                self.run(
                    "restore-retained", ["docker", "start", *self.paused], env=self.original_env
                )
                self.wait_nodes()
                self.health(
                    "retained-restored",
                    "kind-" + self.args.pause_cluster,
                    self.args.restore_gateway_url,
                    600,
                    self.original_env,
                )
                current = self.containers(self.args.pause_cluster)
                if current != self.paused:
                    raise RuntimeError("Restored container identities or running states differ")
                self.report["retained_cluster_restored"] = True

            attempt("restore-retained", restore)
        if self.initial_context is not None:

            def context():
                if (
                    self.capture(["kubectl", "config", "current-context"], optional=True)
                    != self.initial_context
                ):
                    raise RuntimeError("Original kubectl context changed")
                self.report["context_preserved"] = True

            attempt("context", context)
        if self.initial_clusters is not None:

            def existing():
                if not self.initial_clusters.issubset(self.clusters()):
                    raise RuntimeError("An existing cluster is missing after acceptance")
                self.report["existing_clusters_preserved"] = True

            attempt("existing-clusters", existing)
        if self.initial_containers is not None:

            def containers():
                current = self.running_containers()
                if not self.initial_containers.items() <= current.items():
                    raise RuntimeError("An originally running container is missing or renamed")
                self.report["original_running_containers_preserved"] = True

            attempt("running-containers", containers)
        for listener in self.ports:
            listener.close()
        attempt("private-files", lambda: shutil.rmtree(self.private))

    def perform(self):
        try:
            self.execute()
        except BaseException as error:
            self.report["failure"] = {
                "error_type": type(error).__name__,
                "message": str(error)
                if isinstance(error, RuntimeError)
                else "Acceptance interrupted or subprocess failed; inspect local phase logs",
            }
        finally:
            # A second ordinary termination request must not bypass restoration.
            previous = {
                s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM)
            }
            try:
                self.cleanup()
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        self.report["passed"] = bool(
            self.report.get("functional_checks_passed")
            and not self.report["cleanup_errors"]
            and "failure" not in self.report
        )
        self.report["finished_at"] = datetime.datetime.now(datetime.UTC).isoformat()
        self.save()
        print(
            f"Acceptance {'PASSED' if self.report['passed'] else 'FAILED'}; upload-safe summary: {self.summary}",
            flush=True,
        )
        return 0 if self.report["passed"] else 1


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--expected-revision")
    parser.add_argument("--timeout", type=positive, default=2400)
    parser.add_argument("--install-timeout", type=positive, default=600)
    parser.add_argument("--pause-cluster")
    parser.add_argument("--restore-gateway-url")
    parser.add_argument("--image-pull-concurrency", type=int, choices=(1, 2), default=1)
    parser.add_argument("--image-cache", type=Path)
    parser.add_argument("--write-image-cache", type=Path)
    args = parser.parse_args()
    if bool(args.pause_cluster) != bool(args.restore_gateway_url):
        parser.error("--pause-cluster and --restore-gateway-url must be supplied together")
    if args.pause_cluster and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.pause_cluster):
        parser.error("pause-cluster must be a kind cluster name")
    if args.restore_gateway_url and not args.restore_gateway_url.startswith(
        ("http://", "https://")
    ):
        parser.error("restore-gateway-url must use HTTP(S)")
    if args.expected_revision and not re.fullmatch(r"[a-f0-9]{40}", args.expected_revision):
        parser.error("expected-revision must be a full commit SHA")
    for name in ("evidence_dir", "image_cache", "write_image_cache"):
        if getattr(args, name):
            setattr(args, name, getattr(args, name).resolve())
    return args


def main():
    args = arguments()

    def interrupted(signum, _frame):
        raise InterruptedError(f"signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    return Acceptance(args).perform()


if __name__ == "__main__":
    raise SystemExit(main())
