"""DEV bootstrap lifecycle contracts without creating or deleting real clusters."""

import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_preflight import ROOT, ScriptSandbox

SPEC = importlib.util.spec_from_file_location("wait_for_dev", ROOT / "scripts/wait-for-dev.py")
WAIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WAIT)


class DevImagesTests(ScriptSandbox):
    def setUp(self):
        super().setUp()
        self.copy_script("dev-images.sh")
        self.tool("kind", "if args == ['get', 'clusters']: print('regression-test')\n")
        self.tool(
            "docker", "if args[:1] == ['build'] and os.environ.get('FAIL_BUILD'): sys.exit(7)\n"
        )

    def test_builds_and_loads_all_dev_names_with_shared_context(self):
        result = self.run_script("dev-images.sh", "--cluster-name=regression-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        builds = [cmd for cmd in self.commands() if cmd[:2] == ["docker", "build"]]
        names = {cmd[5] for cmd in builds}
        self.assertEqual(
            names,
            {
                f"{name}:latest"
                for name in (
                    "echo-service",
                    "worker-service",
                    "llm-gateway",
                    "audio-api",
                    "stt-worker",
                    "llm-worker",
                    "tts-worker",
                )
            },
        )
        for cmd in builds:
            self.assertEqual(cmd[-1], "services/")
        load = next(cmd for cmd in self.commands() if cmd[:3] == ["kind", "load", "docker-image"])
        self.assertEqual(load[3:5], ["--name", "regression-test"])
        self.assertEqual(set(load[5:]), names)

    def test_failed_build_does_not_load_partial_images(self):
        result = self.run_script("dev-images.sh", "--cluster-name=regression-test", FAIL_BUILD="1")
        self.assertEqual(result.returncode, 7)
        self.assertFalse(any("load" in cmd for cmd in self.commands()))


class TeardownTests(ScriptSandbox):
    def setUp(self):
        super().setUp()
        self.copy_script("teardown.sh")
        self.tool("kind", "")
        self.tool("pkill", "sys.exit('Never terminate unrelated forwards')\n")

    def test_named_cluster_only_and_repeatable(self):
        for _ in range(2):
            result = self.run_script(
                "teardown.sh", "--mode=local", "--cluster-name=regression-test"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.commands(), [["kind", "delete", "cluster", "--name", "regression-test"]] * 2
        )

    def test_invalid_mode_is_failure_without_mutation(self):
        result = self.run_script("teardown.sh", "--mode=typo")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())


class PublishedSourceTests(ScriptSandbox):
    def setUp(self):
        super().setUp()
        self.copy_script("verify-dev-source.py")
        target = self.root / "kubernetes/platform/argocd"
        target.mkdir(parents=True)
        (target / "app-of-apps.yaml").write_text(
            "spec:\n  source:\n    repoURL: https://github.com/example/platform.git\n    targetRevision: main\n"
        )
        self.tool(
            "git",
            "if args[0] == 'status': print(os.environ.get('DIRTY', ''))\n"
            "elif args[0] == 'rev-parse': print('a'*40)\n"
            "elif args[0] == 'ls-remote': print(os.environ.get('REMOTE_SHA', 'a'*40) + '\\trefs/heads/main')\n",
        )

    def verify(self, **environment):
        import subprocess

        return subprocess.run(
            [sys.executable, "scripts/verify-dev-source.py"],
            cwd=self.root,
            env={**self.environment, **environment},
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_matching_published_checkout_passes(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not an immutable fleet pin", result.stdout)

    def test_unpublished_commit_fails_before_mutation(self):
        result = self.verify(REMOTE_SHA="b" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from published main", result.stderr)

    def test_dirty_build_inputs_fail_before_remote_lookup(self):
        result = self.verify(DIRTY=" M services/echo-service/main.py")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("local changes", result.stderr)
        self.assertFalse(any("ls-remote" in cmd for cmd in self.commands()))


class AcceptanceTests(unittest.TestCase):
    def healthy_apps(self):
        return [
            {
                "metadata": {"name": name},
                "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}},
            }
            for name in WAIT.REQUIRED
        ]

    def test_all_missing_apps_fail_not_vacuously_pass(self):
        self.assertEqual(len(WAIT.application_failures([])), len(WAIT.REQUIRED))

    def test_only_intentionally_paused_vllm_may_be_suspended(self):
        apps = self.healthy_apps()
        apps.append(
            {
                "metadata": {"name": "vllm-inference"},
                "status": {"sync": {"status": "Synced"}, "health": {"status": "Suspended"}},
            }
        )
        self.assertEqual(WAIT.application_failures(apps), [])
        next(app for app in apps if app["metadata"]["name"] != "vllm-inference")["status"][
            "health"
        ]["status"] = "Suspended"
        self.assertTrue(WAIT.application_failures(apps))

    def test_controller_error_fails_even_with_stale_healthy_status(self):
        apps = self.healthy_apps()
        apps[0]["status"]["conditions"] = [
            {"type": "ComparisonError", "message": "revision unavailable"}
        ]
        self.assertIn("revision unavailable", " ".join(WAIT.application_failures(apps)))

    def inspect_fixture(self, observed_generation):
        apps = self.healthy_apps()
        secret_source = {
            "repoURL": "https://github.com/example/platform.git",
            "targetRevision": "main",
            "helm": {"values": "password: do-not-record"},
        }
        for app in apps:
            app["spec"] = {"source": secret_source}
            app["status"]["sync"]["comparedTo"] = {"source": secret_source}
            app["status"]["sync"]["revision"] = "a" * 40
        gateway = {
            "metadata": {"generation": 2},
            "status": {
                "conditions": [
                    {
                        "type": "Programmed",
                        "status": "True",
                        "observedGeneration": observed_generation,
                    }
                ]
            },
        }
        with (
            patch.object(
                WAIT.subprocess,
                "run",
                side_effect=[
                    SimpleNamespace(stdout=json.dumps({"items": apps})),
                    SimpleNamespace(stdout=json.dumps(gateway)),
                ],
            ) as kubectl,
            patch.object(
                WAIT.urllib.request,
                "urlopen",
                return_value=nullcontext(SimpleNamespace(status=200)),
            ),
        ):
            failures, observation = WAIT.inspect("kind-regression-test", "http://localhost:18000")
            for call in kubectl.call_args_list:
                self.assertEqual(call.args[0][1:3], ["--context", "kind-regression-test"])
        return failures, observation

    def test_gateway_programmed_for_old_generation_is_not_ready(self):
        failures, _ = self.inspect_fixture(1)
        self.assertIn("current generation is not Programmed=True", " ".join(failures))

    def test_evidence_excludes_compared_to_source_values(self):
        failures, observation = self.inspect_fixture(2)
        self.assertEqual(failures, [])
        serialized = json.dumps(observation)
        self.assertNotIn("do-not-record", serialized)
        self.assertNotIn("comparedTo", serialized)
        self.assertIn("a" * 40, serialized)

    def test_failed_endpoint_times_out_and_retains_failure_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            args = [
                "wait-for-dev.py",
                "--context=kind-regression-test",
                "--timeout=1",
                f"--evidence-dir={directory}",
            ]
            with (
                patch.object(sys, "argv", args),
                patch.object(
                    WAIT, "inspect", return_value=(["echo/: expected HTTP 200, got 503"], {})
                ),
                patch.object(WAIT.time, "monotonic", side_effect=[0, 1]),
            ):
                self.assertEqual(WAIT.main(), 1)
            evidence = json.loads((Path(directory) / "result.json").read_text())
            self.assertEqual(evidence["context"], "kind-regression-test")
            self.assertIn("503", evidence["failures"][0])


if __name__ == "__main__":
    unittest.main()
