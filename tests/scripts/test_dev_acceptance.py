"""Owned acceptance lifecycle boundaries; no real Docker or Kubernetes mutations."""

import importlib.util
import json
import os
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_"), ROOT / "scripts" / (name + ".py")
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


ACCEPT = load("dev-acceptance")
PORTAL = load("verify-dev-portal")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.args = SimpleNamespace(
            evidence_dir=self.base / "evidence", pause_cluster=None, restore_gateway_url=None
        )
        self.runner = ACCEPT.Acceptance(self.args)
        self.runner.source.mkdir()
        self.commands = []
        self.runner.run = lambda name, command, **kwargs: self.commands.append((name, command)) or 0
        self.runner.diagnostics = lambda: None
        self.runner.clusters = lambda: {"untouched"}
        self.runner.initial_clusters = {"untouched"}
        self.runner.initial_context = "original"
        self.runner.capture = lambda *a, **kw: "original"
        self.runner.initial_containers = {"unrelated-id": "untouched"}
        self.runner.running_containers = lambda: {"unrelated-id": "untouched"}

    def tearDown(self):
        if self.runner.private.exists():
            import shutil

            shutil.rmtree(self.runner.private)
        self.temp.cleanup()

    def test_failure_before_ownership_never_deletes_a_cluster(self):
        def fail():
            raise RuntimeError("Published main moved")

        self.runner.execute = fail
        self.assertEqual(self.runner.perform(), 1)
        self.assertEqual(self.commands, [])
        self.assertFalse(self.runner.private.exists())
        self.assertFalse(self.runner.report["passed"])

    def test_success_requires_two_teardowns_and_absence(self):
        def execute():
            self.runner.owned = True
            self.runner.report["functional_checks_passed"] = True

        self.runner.execute = execute
        self.assertEqual(self.runner.perform(), 0)
        self.assertEqual([name for name, _ in self.commands], ["teardown-1", "teardown-2"])
        self.assertTrue(self.runner.report["owned_cluster_absent"])
        self.assertTrue(self.runner.report["context_preserved"])
        self.assertTrue(self.runner.report["original_running_containers_preserved"])

    def test_cleanup_failure_does_not_hide_functional_result_or_skip_restore(self):
        self.args.pause_cluster = "retained"
        self.args.restore_gateway_url = "http://127.0.0.1:1234"
        self.runner.paused = {"retained-id": True}
        self.runner.containers = lambda _: {"retained-id": True}
        self.runner.wait_nodes = lambda: None
        self.runner.health = lambda *a: self.commands.append(("restored-health", []))

        def run(name, command, **kwargs):
            self.commands.append((name, command))
            if name == "teardown-1":
                raise RuntimeError("teardown failed")

        self.runner.run = run

        def execute():
            self.runner.owned = True
            self.runner.pause_attempted = True
            self.runner.report["functional_checks_passed"] = True

        self.runner.execute = execute
        self.assertEqual(self.runner.perform(), 1)
        self.assertEqual(
            [n for n, _ in self.commands],
            ["teardown-1", "teardown-2", "restore-retained", "restored-health"],
        )
        self.assertTrue(self.runner.report["retained_cluster_restored"])
        self.assertTrue(self.runner.report["functional_checks_passed"])

    def test_remaining_cluster_is_failure_even_if_teardown_exits_zero(self):
        self.runner.owned = True
        self.runner.clusters = lambda: {"untouched", self.runner.cluster}
        self.runner.execute = lambda: self.runner.report.update(functional_checks_passed=True)
        self.assertEqual(self.runner.perform(), 1)
        self.assertIn(
            "owned-cluster-absent", [x["step"] for x in self.runner.report["cleanup_errors"]]
        )

    def test_changed_retained_identity_is_never_started(self):
        self.args.pause_cluster = "retained"
        self.runner.pause_attempted = True
        self.runner.paused = {"original-id": True}
        self.runner.containers = lambda _: {"replacement-id": True}
        self.runner.execute = lambda: self.runner.report.update(functional_checks_passed=True)
        self.assertEqual(self.runner.perform(), 1)
        self.assertFalse(any(n == "restore-retained" for n, _ in self.commands))

    def test_signal_failure_still_tears_down(self):
        def interrupted():
            self.runner.owned = True
            raise InterruptedError("SIGTERM")

        self.runner.execute = interrupted
        self.assertEqual(self.runner.perform(), 1)
        self.assertEqual([name for name, _ in self.commands], ["teardown-1", "teardown-2"])
        self.assertEqual(self.runner.report["failure"]["error_type"], "InterruptedError")

    def test_unrelated_running_container_loss_is_failure(self):
        self.runner.execute = lambda: self.runner.report.update(functional_checks_passed=True)
        self.runner.running_containers = lambda: {}
        self.assertEqual(self.runner.perform(), 1)
        self.assertIn(
            "running-containers", [x["step"] for x in self.runner.report["cleanup_errors"]]
        )

    def test_restoration_retries_api_startup_without_mutation(self):
        self.args.pause_cluster = "retained"
        self.runner.paused = {"id": True}
        data = json.dumps(
            {"items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}
        )
        with (
            patch.object(
                self.runner, "capture", side_effect=[RuntimeError("Forbidden"), data]
            ) as capture,
            patch.object(ACCEPT.time, "sleep"),
        ):
            self.runner.wait_nodes()
        self.assertEqual(capture.call_count, 2)
        self.assertTrue(
            all(
                call.args[0][0] == "kubectl" and "get" in call.args[0]
                for call in capture.call_args_list
            )
        )

    def test_evidence_contains_no_private_directory(self):
        self.assertFalse(self.runner.private.is_relative_to(self.runner.evidence))
        self.runner.execute = lambda: self.runner.report.update(functional_checks_passed=True)
        self.runner.perform()
        self.assertNotIn(
            str(self.runner.private), (self.runner.summary / "result.json").read_text()
        )


class ProcessTests(unittest.TestCase):
    def test_bind_failure_closes_the_failing_socket(self):
        listener = Mock()
        listener.bind.side_effect = PermissionError("bind denied")
        with patch.object(ACCEPT.socket, "socket", return_value=listener):
            with self.assertRaises(PermissionError):
                ACCEPT.reserve_ports(1)
        listener.close.assert_called_once()

    def test_successful_parent_cannot_leave_a_background_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = ACCEPT.Acceptance(SimpleNamespace(evidence_dir=root / "evidence"))
            code = "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])"
            try:
                with self.assertRaisesRegex(RuntimeError, "background process"):
                    runner.run("orphan", [sys.executable, "-c", code], cwd=root, timeout=5)
                self.assertTrue(runner.report["steps"][0]["interrupted"])
            finally:
                shutil.rmtree(runner.private)

    def test_timeout_terminates_descendants_and_redacts_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = ACCEPT.Acceptance(SimpleNamespace(evidence_dir=root / "evidence"))
            pid_file = root / "child.pid"
            child = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
            code = f"import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c',{child!r}]);Path({str(pid_file)!r}).write_text(str(p.pid));print('Authorization: Bearer fake-secret-token',flush=True);print('password=privatevalue',flush=True);time.sleep(60)"
            try:
                with self.assertRaises(ACCEPT.subprocess.TimeoutExpired):
                    runner.run("timeout", [sys.executable, "-c", code], cwd=root, timeout=1)
                log = (runner.evidence / "logs/timeout.log").read_text()
                self.assertNotIn("fake-secret-token", log)
                self.assertNotIn("privatevalue", log)
                self.assertTrue(runner.report["steps"][0]["interrupted"])
                # The process group no longer contains a live descendant (zombies
                # can briefly remain on Linux until the host init reaps them).
                child_pid = int(pid_file.read_text())
                state = ACCEPT.subprocess.run(
                    ["ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True
                ).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
            finally:
                if pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                import shutil

                shutil.rmtree(runner.private)


class CommandPlanTests(unittest.TestCase):
    def test_invalid_cache_fails_before_pause_or_cluster_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(
                evidence_dir=root / "evidence",
                expected_revision=None,
                pause_cluster="retained",
                restore_gateway_url="http://127.0.0.1:1234",
                image_pull_concurrency=1,
                image_cache=root / "bad-cache",
                write_image_cache=None,
                install_timeout=600,
                timeout=2400,
            )
            runner = ACCEPT.Acceptance(args)
            runner.clusters = lambda: {"retained"}
            runner.running_containers = lambda: {"retained-id": "retained"}
            runner.capture = lambda command, **kwargs: (
                "a" * 40 if command[0] == "git" else "original-context"
            )
            calls = []

            def run(name, command, **kwargs):
                calls.append(name)
                if name == "clone":
                    shutil.copytree(ROOT / "scripts", runner.source / "scripts")
                    target = runner.source / "terraform/modules/kind"
                    target.mkdir(parents=True)
                    shutil.copyfile(
                        ROOT / "terraform/modules/kind/kind-config.yaml",
                        target / "kind-config.yaml",
                    )
                elif name == "cache-preflight":
                    raise RuntimeError("cache checksum mismatch")

            runner.run = run
            with patch.object(
                ACCEPT,
                "reserve_ports",
                return_value=([Mock(), Mock(), Mock()], [12341, 12342, 12343]),
            ):
                self.assertEqual(runner.perform(), 1)
            self.assertEqual(calls, ["clone", "preflight", "cache-preflight"])
            self.assertFalse(runner.owned)
            self.assertFalse(runner.pause_attempted)

    def test_complete_plan_keeps_all_acceptance_checks_and_private_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(
                evidence_dir=root / "evidence",
                expected_revision="a" * 40,
                pause_cluster=None,
                restore_gateway_url=None,
                image_pull_concurrency=2,
                image_cache=None,
                write_image_cache=root / "cache",
                install_timeout=600,
                timeout=2400,
            )
            runner = ACCEPT.Acceptance(args)
            calls = []

            def capture(command, **kwargs):
                if command[:2] == ["git", "rev-parse"]:
                    return "a" * 40
                if command[:3] == ["kubectl", "config", "current-context"]:
                    return "untouched-context"
                if command[:2] == ["docker", "ps"]:
                    return json.dumps({"ID": "unrelated", "Names": "unrelated"})
                raise AssertionError(command)

            runner.capture = capture
            runner.clusters = lambda: {"untouched-cluster"}
            runner.diagnostics = lambda: None

            def run(name, command, **kwargs):
                calls.append((name, command, kwargs))
                runner.report["steps"].append({"name": name, "exit_code": 0, "seconds": 1})
                if name == "clone":
                    shutil.copytree(ROOT / "scripts", runner.source / "scripts")
                    target = runner.source / "terraform/modules/kind"
                    target.mkdir(parents=True)
                    shutil.copyfile(
                        ROOT / "terraform/modules/kind/kind-config.yaml",
                        target / "kind-config.yaml",
                    )
                elif name in ("convergence", "final-health"):
                    target = runner.evidence / name
                    target.mkdir()
                    (target / "result.json").write_text('{"failures": []}')
                elif name == "redis":
                    target = runner.evidence / "redis"
                    target.mkdir()
                    (target / "redis-pod-persistence.json").write_text('{"passed":true}')
                elif name == "tenant":
                    target = runner.evidence / "tenant"
                    target.mkdir()
                    (target / "tenant-access.json").write_text('{"passed":true}')
                return 0

            runner.run = run
            monitor = Mock(returncode=0)
            with (
                patch.object(
                    ACCEPT,
                    "reserve_ports",
                    return_value=([Mock(), Mock(), Mock()], [12341, 12342, 12343]),
                ),
                patch.object(ACCEPT.subprocess, "Popen", return_value=monitor),
                patch.object(ACCEPT, "smoke_summary", return_value={"passed": True}),
            ):
                self.assertEqual(runner.perform(), 0)
            names = [name for name, _, _ in calls]
            self.assertEqual(
                names,
                [
                    "clone",
                    "preflight",
                    "bootstrap",
                    "convergence",
                    "redis",
                    "smoke",
                    "tenant",
                    "portal",
                    "final-health",
                    "final-source",
                    "cache-export",
                    "teardown-1",
                    "teardown-2",
                ],
            )
            bootstrap = next(command for name, command, _ in calls if name == "bootstrap")
            self.assertIn("--require-published-source", bootstrap)
            self.assertIn("--cluster-name=" + runner.cluster, bootstrap)
            self.assertNotIn("--skip-images", bootstrap)
            smoke = next(kwargs for name, _, kwargs in calls if name == "smoke")
            self.assertEqual(smoke["env"]["KUBE_CONTEXT"], runner.context)
            self.assertEqual(smoke["env"]["KUBECONFIG"], str(runner.private / "kubeconfig"))
            self.assertEqual(runner.report["bootstrap_and_convergence_seconds"], 2)
            self.assertFalse(runner.private.exists())


class SmokeSummaryTests(unittest.TestCase):
    def test_summary_uses_actual_matched_ids_and_output_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "logs").mkdir()
            (root / "smoke").mkdir()
            ping, poison, audio = "smoke-" + "1" * 32, "poison-" + "2" * 32, "3" * 32
            (root / "logs/smoke.log").write_text(ping + "\n" + poison)
            (root / "smoke/worker.log").write_text(
                json.dumps({"event": "job_processed", "job_id": ping, "result": {"pong": True}})
            )
            (root / "smoke/offsets.txt").write_text("worker-service jobs 0 1 1 0\n")
            (root / "smoke/dlq.jsonl").write_text(
                json.dumps({"original_job": {"id": poison}, "error": "synthetic"})
            )
            keys = {
                key: prefix + "/" + audio
                for key, prefix in (
                    ("audio", "audio"),
                    ("transcript", "transcripts"),
                    ("summary", "summaries"),
                    ("speech", "speech"),
                )
            }
            (root / "smoke/job.json").write_text(
                json.dumps({"status": "done", "keys": keys, "unexpected_private": "not exported"})
            )
            summary = ACCEPT.smoke_summary(ROOT, root)
            self.assertEqual(summary["ping_job_id"], ping)
            self.assertEqual(summary["output_keys"], keys)
            self.assertNotIn("not exported", json.dumps(summary))
            (root / "smoke/offsets.txt").write_text("worker-service jobs 0 0 1 1\n")
            with self.assertRaises(RuntimeError):
                ACCEPT.smoke_summary(ROOT, root)


class PortalTests(unittest.TestCase):
    def entity(self):
        return {
            "spec": {
                "steps": [
                    {"id": "fetch", "action": "fetch:template", "input": {"url": "./skeleton"}},
                    {"id": "publish", "action": "publish:github", "input": {}},
                    {
                        "id": "registration-files",
                        "action": "fetch:template",
                        "input": {"url": "./registration"},
                    },
                    {"id": "registration-pr", "action": "publish:github:pull-request", "input": {}},
                    {"id": "register", "action": "catalog:register", "input": {}},
                ],
                "output": {"links": ["external"]},
            }
        }

    def test_dryrun_contains_only_two_remote_fetches_and_no_uploaded_files(self):
        original = self.entity()
        payload = PORTAL.fetch_template(original, "https://github.com/example/platform.git")
        self.assertEqual(
            [s["action"] for s in payload["template"]["spec"]["steps"]], ["fetch:template"] * 2
        )
        self.assertEqual(payload["template"]["spec"]["output"], {})
        self.assertEqual(payload["directoryContents"], [])
        self.assertEqual(len(original["spec"]["steps"]), 5)
        self.assertTrue(
            all(
                s["input"]["url"].startswith("https://github.com/example/platform/tree/main/")
                for s in payload["template"]["spec"]["steps"]
            )
        )
        self.assertEqual(len(PORTAL.EXPECTED_FILES), 25)

    def test_unknown_fetch_step_rejected_instead_of_running_it(self):
        entity = self.entity()
        entity["spec"]["steps"][0]["id"] = "unexpected"
        with self.assertRaises(ValueError):
            PORTAL.fetch_template(entity, "https://github.com/example/platform.git")

    def test_multisource_revision_mismatch_fails_without_leaking_values(self):
        app = {
            "metadata": {"name": "seaweedfs"},
            "spec": {
                "sources": [
                    {"chart": "seaweedfs", "repoURL": "https://chart.example"},
                    {
                        "path": "manifests",
                        "repoURL": "https://github.com/example/platform.git",
                        "helm": {"values": "token: SECRET"},
                    },
                ]
            },
            "status": {"sync": {"revisions": ["1.0", "different-sha"]}},
        }
        failures, evidence = PORTAL.source_failures(
            [app], "https://github.com/example/platform", "expected-sha"
        )
        self.assertEqual(len(failures), 1)
        self.assertNotIn("SECRET", json.dumps(evidence))
        self.assertEqual(evidence[0]["observed_revision"], "different-sha")


if __name__ == "__main__":
    unittest.main()
