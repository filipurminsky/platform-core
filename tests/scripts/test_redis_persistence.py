"""Safety/failure regressions for the opt-in Redis pod persistence drill."""

import contextlib
import copy
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "redis_drill", ROOT / "scripts/test-redis-persistence.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

POD = {
    "metadata": {
        "uid": "pod-original",
        "ownerReferences": [{"controller": True, "kind": "StatefulSet", "uid": "sts-original"}],
    },
    "spec": {
        "nodeName": "node-one",
        "containers": [
            {
                "name": "redis",
                "image": "redis:7.4.12-alpine",
                "env": [
                    {
                        "name": "REDIS_PASSWORD",
                        "valueFrom": {"secretKeyRef": {"name": "redis-auth", "key": "password"}},
                    }
                ],
            }
        ],
        "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data-redis-0"}}],
    },
    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
}


class SimulatedCluster(MODULE.Drill):
    def __init__(self, evidence, scenario="normal"):
        super().__init__("kind-disposable", evidence)
        self.scenario = scenario
        self.deleted = False
        self.commands = []

    def kubectl(self, *args, body=None):
        self.commands.append((args, body))
        if args[0] == "delete":
            assert self.created, "pod deletion must follow confirmed state writes"
            options = json.loads(body)
            assert options["preconditions"] == {"uid": "pod-original"}
            assert args[2] == "/api/v1/namespaces/platform/pods/redis-0"
            self.deleted = True
            return "{}"
        return "prod" if self.scenario == "production" else "dev"

    def get(self, kind, name, namespace=None, optional=False):
        if kind == "pod":
            pod = copy.deepcopy(POD)
            if self.deleted:
                pod["metadata"]["uid"] = "pod-replaced"
            if self.scenario == "unowned":
                pod["metadata"]["ownerReferences"] = []
            return pod
        if kind == "statefulset":
            return {
                "metadata": {"uid": "sts-original"},
                "spec": {"replicas": 1, "serviceName": "redis"},
            }
        if kind == "pvc":
            return {
                "metadata": {
                    "uid": "pvc-other"
                    if self.deleted and self.scenario == "changed-volume"
                    else "pvc-original"
                },
                "status": {"phase": "Bound"},
                "spec": {"volumeName": "pv-original", "storageClassName": "standard"},
            }
        if kind == "pv":
            return {
                "metadata": {"name": "pv-original", "uid": "pv-original"},
                "spec": {
                    "claimRef": {
                        "uid": "pvc-other"
                        if self.deleted and self.scenario == "changed-volume"
                        else "pvc-original"
                    }
                },
            }
        raise AssertionError(kind)

    def redis(self, commands):
        self.commands.append(("redis", commands))
        if commands[0][0] == "CONFIG":
            return [
                [
                    "appendonly",
                    "no" if self.scenario == "no-aof" else "yes",
                    "appendfsync",
                    "everysec",
                ]
            ]
        if commands[0][0:2] == ["EVAL", MODULE.SEED]:
            assert commands[1] == ["WAITAOF", 1, 0, 5000], (
                "seed and fsync must share one connection"
            )
            return [2, [0 if self.scenario == "fsync-failed" else 1, 0]]
        if commands[0][0:2] == ["EVAL", MODULE.CLEANUP]:
            assert commands[0][3:5] == self.keys
            assert commands[0][5:] == [self.payload, "done"], "cleanup must compare owned values"
            return [1 if self.scenario == "cleanup-failed" else 2]
        ttl = MODULE.TTL * 1000 + (10000 if self.scenario == "wrong-ttl" else 0)
        return ["changed" if self.scenario == "wrong-state" else self.payload, "done", ttl, ttl]


class RedisPersistenceTests(unittest.TestCase):
    def run_drill(self, scenario):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        drill = SimulatedCluster(temp.name, scenario)
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                drill.run()
            except RuntimeError:
                pass
        evidence = json.loads((Path(temp.name) / "redis-pod-persistence.json").read_text())
        return drill, evidence

    def test_replacement_is_uid_guarded_and_preserves_storage(self):
        drill, evidence = self.run_drill("normal")
        self.assertTrue(drill.deleted)
        self.assertTrue(evidence["passed"])
        self.assertNotEqual(evidence["before"]["pod_uid"], evidence["after"]["pod_uid"])
        self.assertEqual(evidence["before"]["pv_uid"], evidence["after"]["pv_uid"])
        self.assertEqual(evidence["cleanup"], {"owned_keys_deleted": 2})

    def test_unsafe_prerequisites_never_delete_pod(self):
        for scenario in ("production", "unowned", "no-aof", "fsync-failed"):
            with self.subTest(scenario=scenario):
                drill, evidence = self.run_drill(scenario)
                self.assertFalse(drill.deleted)
                self.assertFalse(evidence["passed"])

    def test_changed_storage_state_or_ttl_fails_and_cleans_owned_keys(self):
        for scenario in ("changed-volume", "wrong-state", "wrong-ttl"):
            with self.subTest(scenario=scenario):
                drill, evidence = self.run_drill(scenario)
                self.assertTrue(drill.deleted)
                self.assertFalse(evidence["passed"])
                self.assertEqual(evidence["cleanup"], {"owned_keys_deleted": 2})

    def test_incomplete_cleanup_cannot_report_success(self):
        _, evidence = self.run_drill("cleanup-failed")
        self.assertFalse(evidence["passed"])
        self.assertIn("cleanup_error", evidence)

    def test_requires_explicit_operator_opt_in_and_context(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(SystemExit):
            MODULE.main()
        with self.assertRaises(ValueError):
            MODULE.Drill("", "/tmp/unused")


if __name__ == "__main__":
    unittest.main()
