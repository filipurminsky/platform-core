#!/usr/bin/env python3
"""Opt-in dev Redis pod replacement; never changes the StatefulSet or PVC.

Uses redis-cli from the existing container and its REDIS_PASSWORD environment.
Only unique synthetic keys are written/deleted. Local Python has no extra deps.
"""

import datetime
import hashlib
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

SEED = """if redis.call('EXISTS', KEYS[1], KEYS[2]) ~= 0 then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 2"""
CLEANUP = """local count = 0
if redis.call('GET', KEYS[1]) == ARGV[1] then
 count = redis.call('DEL', KEYS[1])
 if redis.call('GET', KEYS[2]) == ARGV[2] then count = count + redis.call('DEL', KEYS[2]) end
end
return count"""
TTL = 604800


class Drill:
    def __init__(self, context, evidence, timeout=180):
        if not context:
            raise ValueError("KUBE_CONTEXT must explicitly select the disposable dev cluster")
        self.context = context
        self.evidence = Path(evidence)
        self.timeout = timeout
        self.report = {"context": context, "passed": False, "cleanup": "not needed"}
        self.keys = None
        self.payload = None
        self.created = False

    def kubectl(self, *args, body=None):
        completed = subprocess.run(
            ["kubectl", "--context", self.context, "--request-timeout=20s", *args],
            input=body,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if completed.returncode:
            # No secret values are passed as arguments or included in artifacts.
            raise RuntimeError(f"kubectl {args[0]} failed: {completed.stderr.strip()}")
        return completed.stdout

    def get(self, kind, name, namespace=None, optional=False):
        args = ["get", kind, name, "-o", "json"]
        if namespace:
            args += ["-n", namespace]
        if optional:
            args += ["--ignore-not-found"]
        output = self.kubectl(*args)
        return json.loads(output) if output.strip() else None

    def redis(self, commands):
        # One redis-cli connection for seed + WAITAOF: WAITAOF confirms writes
        # issued by THIS connection, not a subsequent independent CLI process.
        body = "\n".join(" ".join(json.dumps(str(arg)) for arg in command) for command in commands)
        output = self.kubectl(
            "-n",
            "platform",
            "exec",
            "-i",
            "redis-0",
            "-c",
            "redis",
            "--",
            "sh",
            "-c",
            'export REDISCLI_AUTH="$REDIS_PASSWORD"; exec redis-cli --json',
            body=body + "\n",
        )
        result = [json.loads(line) for line in output.splitlines() if line.strip()]
        if len(result) != len(commands):
            raise RuntimeError("Redis CLI did not return one result per command")
        return result

    @staticmethod
    def ready(pod):
        return (
            bool(pod)
            and not pod["metadata"].get("deletionTimestamp")
            and any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in pod.get("status", {}).get("conditions", [])
            )
        )

    def identity(self, pod):
        sts = self.get("statefulset", "redis", "platform")
        if sts["spec"].get("replicas") != 1 or sts["spec"].get("serviceName") != "redis":
            raise RuntimeError("Expected the repository's single-instance Redis StatefulSet")
        owners = pod["metadata"].get("ownerReferences", [])
        if not any(
            o.get("controller")
            and o["kind"] == "StatefulSet"
            and o["uid"] == sts["metadata"]["uid"]
            for o in owners
        ):
            raise RuntimeError("redis-0 is not controlled by the expected StatefulSet")
        container = next(c for c in pod["spec"]["containers"] if c["name"] == "redis")
        auth = next(e for e in container["env"] if e["name"] == "REDIS_PASSWORD")
        if auth.get("valueFrom", {}).get("secretKeyRef") != {
            "name": "redis-auth",
            "key": "password",
        }:
            raise RuntimeError("Expected Redis auth environment from redis-auth/password")
        data = next(v for v in pod["spec"]["volumes"] if v["name"] == "data")
        claim = data.get("persistentVolumeClaim", {}).get("claimName")
        if claim != "data-redis-0":
            raise RuntimeError("Redis data is not backed by the expected StatefulSet PVC")
        pvc = self.get("pvc", claim, "platform")
        if pvc.get("status", {}).get("phase") != "Bound":
            raise RuntimeError("Redis PVC is not Bound")
        pv = self.get("pv", pvc["spec"]["volumeName"])
        if pv["spec"].get("claimRef", {}).get("uid") != pvc["metadata"]["uid"]:
            raise RuntimeError("PV claim identity does not match Redis PVC")
        return {
            "image": container["image"],
            "pod_uid": pod["metadata"]["uid"],
            "node": pod["spec"]["nodeName"],
            "statefulset_uid": sts["metadata"]["uid"],
            "pvc_name": claim,
            "pvc_uid": pvc["metadata"]["uid"],
            "pv_name": pv["metadata"]["name"],
            "pv_uid": pv["metadata"]["uid"],
            "storage_class": pvc["spec"].get("storageClassName"),
        }

    def run(self):
        try:
            environment = self.kubectl(
                "-n",
                "argocd",
                "get",
                "secret",
                "in-cluster",
                "-o",
                "jsonpath={.metadata.labels.environment}",
            )
            if environment != "dev":
                raise RuntimeError("Refusing replacement: cluster is not labelled dev")
            pod = self.get("pod", "redis-0", "platform")
            if not self.ready(pod):
                raise RuntimeError("Redis must be Ready before the drill starts")
            before = self.identity(pod)
            config = self.redis([["CONFIG", "GET", "appendonly", "appendfsync"]])[0]
            if isinstance(config, list):
                config = dict(zip(config[::2], config[1::2], strict=True))
            if config.get("appendonly") != "yes" or config.get("appendfsync") not in (
                "everysec",
                "always",
            ):
                raise RuntimeError("Expected AOF enabled with everysec/always fsync")
            self.report["before"] = before
            job = uuid.uuid4().hex
            self.keys = [f"job:{job}", f"dedup:tts:{job}"]
            self.payload = json.dumps(
                {
                    "status": "done",
                    "stage": "tts",
                    "keys": {"speech": f"speech/{job}"},
                    "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
                }
            )
            seeded_at = time.monotonic()
            seeded = self.redis(
                [
                    ["EVAL", SEED, 2, *self.keys, self.payload, "done", TTL],
                    ["WAITAOF", 1, 0, 5000],
                ]
            )
            if seeded[0] != 2:
                raise RuntimeError("Synthetic key collision; no existing keys overwritten")
            self.created = True
            if seeded[1][0] != 1:
                raise RuntimeError("Local AOF fsync was not confirmed; refusing replacement")
            self.report["job_id"] = job
            # UID precondition prevents deleting a different pod if another
            # operator replaced it between our observation and the DELETE.
            started = time.monotonic()
            self.kubectl(
                "delete",
                "--raw",
                "/api/v1/namespaces/platform/pods/redis-0",
                "-f",
                "-",
                body=json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "DeleteOptions",
                        "preconditions": {"uid": before["pod_uid"]},
                    }
                ),
            )
            print("Redis pod replacement requested; waiting for a different Ready UID", flush=True)
            not_ready_samples = 0
            while time.monotonic() - started < self.timeout:
                replacement = self.get("pod", "redis-0", "platform", optional=True)
                if self.ready(replacement) and replacement["metadata"]["uid"] != before["pod_uid"]:
                    break
                not_ready_samples += 1
                time.sleep(1)
            else:
                raise TimeoutError(f"Replacement Redis pod not Ready within {self.timeout}s")
            ready_seconds = time.monotonic() - started
            after = self.identity(replacement)
            self.report["after"] = after
            for field in ("statefulset_uid", "pvc_name", "pvc_uid", "pv_name", "pv_uid"):
                if before[field] != after[field]:
                    raise RuntimeError(f"Storage/workload identity changed: {field}")
            values = self.redis(
                [
                    ["GET", self.keys[0]],
                    ["GET", self.keys[1]],
                    ["PTTL", self.keys[0]],
                    ["PTTL", self.keys[1]],
                ]
            )
            if values[:2] != [self.payload, "done"]:
                raise RuntimeError("Recovered job/dedup state does not match seeded values")
            elapsed = time.monotonic() - seeded_at
            expected_ttl = TTL * 1000 - elapsed * 1000
            if any(abs(ttl - expected_ttl) > 3000 for ttl in values[2:]):
                raise RuntimeError(
                    "TTL was reset, expired, or does not match elapsed recovery time"
                )
            self.report.update(
                {
                    "passed": True,
                    "replacement_ready_seconds": round(ready_seconds, 3),
                    "state_verified_seconds": round(time.monotonic() - started, 3),
                    "not_ready_poll_samples": not_ready_samples,
                    "remaining_ttl_ms": values[2:],
                    "values_match": True,
                    "same_pvc_and_pv": True,
                    "limits": [
                        "single-instance service interruption",
                        "fsync-confirmed synthetic writes",
                        "graceful pod deletion, not node/power loss",
                        "readiness timing is not continuous client outage measurement",
                        "PVC/PV preserved; no HA or lost-volume restore claim",
                    ],
                }
            )
        except BaseException as exc:
            self.report["error"] = str(exc)
            raise
        finally:
            if self.keys:
                try:
                    deleted = self.redis([["EVAL", CLEANUP, 2, *self.keys, self.payload, "done"]])[
                        0
                    ]
                    self.report["cleanup"] = {"owned_keys_deleted": deleted}
                    if self.created and deleted != 2:
                        self.report["passed"] = False
                        self.report["cleanup_error"] = "Expected both owned synthetic keys removed"
                except Exception as exc:
                    self.report["passed"] = False
                    self.report["cleanup_error"] = str(exc)
            self.evidence.mkdir(parents=True, exist_ok=True)
            (self.evidence / "redis-pod-persistence.json").write_text(
                json.dumps(self.report, indent=2) + "\n"
            )
            print(json.dumps(self.report, indent=2))
        if not self.report["passed"]:
            raise RuntimeError("Redis recovery or owned-key cleanup failed; inspect evidence")


def main():
    if os.environ.get("ALLOW_REDIS_POD_REPLACEMENT") != "1":
        raise SystemExit(
            "Set ALLOW_REDIS_POD_REPLACEMENT=1 to acknowledge a single-instance dev Redis outage"
        )
    context = os.environ.get("KUBE_CONTEXT", "")
    evidence = os.environ.get("EVIDENCE_DIR", f"/tmp/redis-pod-persistence-{uuid.uuid4().hex[:8]}")

    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, interrupted)
    drill = Drill(context, evidence)
    script = Path(__file__).resolve()
    source = {"script_sha256": hashlib.sha256(script.read_bytes()).hexdigest()}
    try:
        source["commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=script.parent.parent, text=True, timeout=5
        ).strip()
        source["worktree_dirty"] = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=script.parent.parent, text=True, timeout=5
            ).strip()
        )
    except OSError, subprocess.SubprocessError:
        source["commit"] = "unavailable"
    drill.report["source"] = source
    drill.run()


if __name__ == "__main__":
    main()
