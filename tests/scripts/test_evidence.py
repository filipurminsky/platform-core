"""Regression cases for operational claims that previously passed without proof."""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


canary = load("canary-evidence")
smoke = load("smoke-evidence")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def write(self, name, value):
        (self.path / f"{name}.json").write_text(json.dumps(value))

    def fixture(self):
        def rollout(generation, phase, stable, current, image):
            return {
                "metadata": {"uid": "rollout-1", "generation": generation},
                "spec": {
                    "template": {"spec": {"containers": [{"name": "echo-service", "image": image}]}}
                },
                "status": {
                    "phase": phase,
                    "observedGeneration": str(generation),
                    "stableRS": stable,
                    "currentPodHash": current,
                },
            }

        self.write("baseline", rollout(1, "Healthy", "old", "old", "original"))
        self.write("rollout", rollout(2, "Degraded", "old", "new", "broken"))
        self.write("application", {})
        self.write("previous-analysis", {"items": []})
        self.write(
            "analysis",
            {
                "items": [
                    {
                        "metadata": {
                            "uid": "analysis-2",
                            "ownerReferences": [{"uid": "rollout-1"}],
                        },
                        "spec": {"args": [{"name": "canary-hash", "value": "new"}]},
                        "status": {
                            "phase": "Failed",
                            "metricResults": [{"name": "success-rate", "phase": "Failed"}],
                        },
                    }
                ]
            },
        )
        self.write(
            "route",
            {
                "metadata": {"generation": 3},
                "spec": {
                    "rules": [
                        {
                            "backendRefs": [
                                {"name": "echo-service", "weight": 100},
                                {"name": "echo-service-canary", "weight": 0},
                            ]
                        }
                    ]
                },
                "status": {
                    "parents": [
                        {
                            "parentRef": {"name": "platform", "namespace": "envoy-gateway-system"},
                            "conditions": [
                                {"type": kind, "status": "True", "observedGeneration": 3}
                                for kind in ("Accepted", "ResolvedRefs")
                            ],
                        }
                    ]
                },
            },
        )
        self.write("service", {"spec": {"selector": {"rollouts-pod-template-hash": "old"}}})
        self.write(
            "replicasets",
            {
                "items": [
                    {
                        "metadata": {
                            "labels": {"rollouts-pod-template-hash": "old"},
                            "ownerReferences": [{"uid": "rollout-1"}],
                        },
                        "status": {"availableReplicas": 1},
                    }
                ]
            },
        )

    def test_complete_abort_evidence(self):
        self.fixture()
        self.assertEqual(canary.verify(self.path, "bad", "broken"), "old")

    def test_restoration_requires_observed_generation(self):
        self.fixture()
        restored = canary.read(self.path, "baseline")
        restored["metadata"]["generation"] = 3
        self.write("restored", restored)
        with self.assertRaisesRegex(ValueError, "not yet observed"):
            canary.restored(self.path)
        restored["status"]["observedGeneration"] = "3"
        self.write("restored", restored)
        canary.restored(self.path)

    def test_stale_terminal_phase_cannot_pass(self):
        self.fixture()
        rollout = canary.read(self.path, "rollout")
        rollout["status"]["observedGeneration"] = "1"
        self.write("rollout", rollout)
        with self.assertRaisesRegex(ValueError, "observed"):
            canary.verify(self.path, "bad", "broken")

    def test_historical_analysis_cannot_pass(self):
        self.fixture()
        self.write("previous-analysis", canary.read(self.path, "analysis"))
        with self.assertRaisesRegex(ValueError, "No new SLO"):
            canary.verify(self.path, "bad", "broken")

    def test_missing_metrics_is_not_proof_of_slo_rollback(self):
        self.fixture()
        analysis = canary.read(self.path, "analysis")
        analysis["items"][0]["status"]["metricResults"][0]["name"] = "canary-metrics-present"
        self.write("analysis", analysis)
        with self.assertRaisesRegex(ValueError, "No new SLO"):
            canary.verify(self.path, "bad", "broken")

    def test_remaining_canary_traffic_fails(self):
        self.fixture()
        route = canary.read(self.path, "route")
        route["spec"]["rules"][0]["backendRefs"][1]["weight"] = 10
        self.write("route", route)
        with self.assertRaisesRegex(ValueError, "non-stable"):
            canary.verify(self.path, "bad", "broken")

    def test_gateway_response_must_identify_ready_stable_pod(self):
        self.fixture()
        self.write(
            "pods",
            {
                "items": [
                    {
                        "metadata": {
                            "name": "old-pod",
                            "labels": {"rollouts-pod-template-hash": "old"},
                        },
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    }
                ]
            },
        )
        self.write("response-1", {"service": "echo-service", "hostname": "new-pod"})
        with self.assertRaisesRegex(ValueError, "ready stable pod"):
            canary.response(self.path, "bad", 1)
        self.write("response-1", {"service": "echo-service", "hostname": "old-pod"})
        canary.response(self.path, "bad", 1)

    def test_all_partitions_and_specific_job_required(self):
        (self.path / "offsets.txt").write_text(
            "worker-service jobs 0 4 4 0\nworker-service jobs 1 3 4 1\n"
        )
        (self.path / "worker.log").write_text(
            json.dumps({"event": "job_processed", "job_id": "our-job", "result": {"pong": True}})
        )
        self.assertFalse(smoke.verify("processed", self.path, "our-job"))
        (self.path / "offsets.txt").write_text(
            "worker-service jobs 0 4 4 0\nworker-service jobs 1 4 4 0\n"
        )
        self.assertFalse(smoke.verify("processed", self.path, "other-job"))
        self.assertTrue(smoke.verify("processed", self.path, "our-job"))

    def test_dlq_matches_identity_not_substring(self):
        (self.path / "dlq.jsonl").write_text(
            json.dumps({"id": "other", "error": "mentions our-job"})
        )
        self.assertFalse(smoke.verify("dlq", self.path, "our-job"))
        (self.path / "dlq.jsonl").write_text(
            json.dumps({"original_job": {"id": "our-job"}, "error": "Unknown operation"})
        )
        self.assertTrue(smoke.verify("dlq", self.path, "our-job"))

    def test_done_without_outputs_fails(self):
        self.write("job", {"status": "done", "keys": {"audio": "audio/job"}})
        self.assertFalse(smoke.verify("completed", self.path, "job"))

    def test_smoke_scale_timeout_exits_nonzero(self):
        fake = self.path / "kubectl"
        fake.write_text("""#!/bin/sh
case "$*" in
  *"config current-context"*) echo test ;;
  *"get secret in-cluster"*) echo dev ;;
  *"get pods"*) echo kafka-pod ;;
esac
""")
        fake.chmod(0o755)
        result = subprocess.run(
            ["bash", "scripts/smoke-test.sh"],
            cwd=ROOT,
            env={
                **os.environ,
                "PATH": f"{self.path}:{os.environ['PATH']}",
                "WORKER_TIMEOUT": "0",
                "EVIDENCE_DIR": str(self.path),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not become ready", result.stderr)
        self.assertNotIn("Smoke test complete", result.stdout)

    def test_make_rejects_failed_render_even_when_validator_succeeds(self):
        for executable, body in (("kustomize", "exit 42"), ("conftest", "cat >/dev/null; exit 0")):
            path = self.path / executable
            path.write_text(f"#!/bin/sh\n{body}\n")
            path.chmod(0o755)
        for target in ("policy", "kustomize"):
            with self.subTest(target=target):
                result = subprocess.run(
                    ["make", target, "KUBECONFORM=cat >/dev/null"],
                    cwd=ROOT,
                    env={**os.environ, "PATH": f"{self.path}:{os.environ['PATH']}"},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)


if __name__ == "__main__":
    unittest.main()
