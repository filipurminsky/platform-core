"""Verify cache-probe ownership, failure evidence and real-runtime command ordering."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "cache_probe", ROOT / ".github/scripts/probe-dev-image-cache.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
REFERENCE = "registry.example.com/platform/image@sha256:" + "b" * 64
IDENTITY = "sha256:" + "b" * 64
REVISION = "a" * 40
CONFIG = (ROOT / "terraform/modules/kind/kind-config.yaml").read_text()


class Runtime:
    def __init__(self, runner, fail_export=False, fail_cleanup=False):
        self.runner = runner
        self.active = False
        self.events = []
        self.fail_export = fail_export
        self.fail_cleanup = fail_cleanup
        self.deletes = 0

    def command(self, args, **kwargs):
        if args[:2] == ["git", "rev-parse"]:
            return REVISION
        if args[:3] == ["kind", "get", "clusters"]:
            return "existing\n" + (self.runner.cluster if self.active else "")
        if args[:2] == ["docker", "ps"]:
            return "unrelated-id\n" + ("owned-id" if self.active else "")
        if args[:3] == ["kubectl", "config", "current-context"]:
            return "existing-context"
        if args[:3] == ["kind", "create", "cluster"]:
            self.assert_private_context(kwargs)
            self.events.append("create")
            self.active = True
            return ""
        if args[:3] == ["kind", "delete", "cluster"]:
            self.assert_private_context(kwargs)
            self.events.append("delete")
            self.deletes += 1
            if self.fail_cleanup and self.deletes > 1:
                raise subprocess.CalledProcessError(9, args, stderr="private delete payload")
            self.active = False
            return ""
        if args[-2:] == ["cat", "/etc/containerd/config.toml"]:
            return '[plugins."io.containerd.grpc.v1.cri".containerd]\ndiscard_unpacked_layers = false\n'
        if args[-3:] == ["crictl", "pull", REFERENCE]:
            self.events.append("pull")
            return ""
        if args[-2:] == ["images", "list"]:
            return f"REF TYPE DIGEST LABELS\n{REFERENCE} index {IDENTITY} io.cri-containerd.image=managed\n"
        if args[-4:] == ["crictl", "images", "-o", "json"]:
            return json.dumps({"images": [{"repoDigests": [REFERENCE]}]})
        raise AssertionError(args)

    def assert_private_context(self, kwargs):
        assert kwargs["env"]["KUBECONFIG"] == str(self.runner.private / "kubeconfig")

    def nodes(self, cluster):
        assert cluster == self.runner.cluster and self.active
        return {
            "control-plane": {"container": cluster + "-control-plane", "platform": "linux/amd64"}
        }

    def export(self, cache, revision, nodes):
        self.events.append("export")
        assert revision == REVISION
        if self.fail_export:
            error = probe.CACHE.CacheCommandError(
                "export-archive",
                "control-plane",
                subprocess.CalledProcessError(
                    1, [], stderr="private registry credential content not found " + IDENTITY
                ),
            )
            error.diagnostics = {"failed_node_images": {REFERENCE: IDENTITY}}
            raise error
        cache.mkdir()
        manifest = {
            "nodes": {"control-plane": {"images": {REFERENCE: IDENTITY}, "archive_bytes": 512}}
        }
        (cache / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    def restore(self, cache, revision, nodes):
        self.events.append("import")
        assert revision == REVISION and self.active


class CacheProbeTests(unittest.TestCase):
    def runner(self, directory):
        root = Path(directory) / "repo"
        (root / "terraform/modules/kind").mkdir(parents=True)
        (root / "terraform/modules/kind/kind-config.yaml").write_text(CONFIG)
        (root / "tests/fixtures").mkdir(parents=True)
        (root / "tests/fixtures/dev-cache-images.json").write_text(
            json.dumps({"images": [REFERENCE]})
        )
        return probe.Probe(root, REVISION, Path(directory) / "evidence")

    def execute(self, runner, runtime):
        with (
            patch.object(runner, "run", side_effect=runtime.command),
            patch.object(probe.CACHE, "nodes_for", side_effect=runtime.nodes),
            patch.object(probe.CACHE, "export_cache", side_effect=runtime.export),
            patch.object(probe.CACHE, "import_cache", side_effect=runtime.restore),
            patch("builtins.print"),
        ):
            status = runner.perform()
        return status, json.loads((runner.summary / "result.json").read_text())

    def test_complete_transfer_recreates_node_and_preserves_context_and_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            runtime = Runtime(runner)
            status, report = self.execute(runner, runtime)
            self.assertEqual(status, 0, report)
            self.assertEqual(
                runtime.events, ["create", "pull", "export", "delete", "create", "import", "delete"]
            )
            self.assertTrue(report["passed"])
            self.assertTrue(report["exact_image_digests"])
            self.assertEqual(report["cri_missing_references"], [])
            self.assertTrue(report["context_preserved"])
            self.assertTrue(report["containers_preserved"])
            self.assertTrue(report["owned_cluster_absent"])
            self.assertFalse(runner.private.exists())

    def test_export_failure_preserves_safe_diagnostic_and_cleans_up_without_import(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            runtime = Runtime(runner, fail_export=True)
            status, report = self.execute(runner, runtime)
            self.assertEqual(status, 1)
            self.assertEqual(runtime.events, ["create", "pull", "export", "delete"])
            self.assertEqual(report["phase"], "export")
            self.assertEqual(report["failure"]["details"]["category"], "content-not-found")
            self.assertNotIn("private registry credential", json.dumps(report))
            self.assertIn(
                "private registry credential", (runner.logs / "cache-stderr.log").read_text()
            )
            self.assertEqual((runner.logs / "cache-stderr.log").stat().st_mode & 0o777, 0o600)
            self.assertTrue(report["owned_cluster_absent"])
            self.assertIn("failure_host_disk", report)

    def test_cleanup_failure_makes_successful_transfer_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            status, report = self.execute(runner, Runtime(runner, fail_cleanup=True))
            self.assertEqual(status, 1)
            self.assertTrue(report["exact_image_digests"])
            self.assertFalse(report["passed"])
            self.assertFalse(report["owned_cluster_absent"])
            self.assertTrue(report["cleanup_errors"])
            self.assertNotIn("private delete payload", json.dumps(report))

    def test_source_mismatch_stops_before_cluster_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            with patch.object(runner, "run", return_value="c" * 40) as run, patch("builtins.print"):
                self.assertEqual(runner.perform(), 1)
            self.assertEqual(run.call_args_list[0].args[0], ["git", "rev-parse", "HEAD"])
            self.assertEqual(run.call_count, 1)
            self.assertFalse(runner.private.exists())

    def test_fixture_rejects_credentials_mutable_and_local_app_references(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "images.json"
            for images in (
                [],
                ["registry.example.com/platform/image:latest"],
                ["https://user:password@registry.example.com/image@" + IDENTITY],
                ["registry.example.com/echo-service@" + IDENTITY],
                [REFERENCE, REFERENCE],
            ):
                path.write_text(json.dumps({"images": images}))
                with self.assertRaises(ValueError):
                    probe.fixture_images(path)
            path.write_text(json.dumps({"images": [REFERENCE]}))
            self.assertEqual(probe.fixture_images(path), [REFERENCE])

    def test_cache_helper_commands_obey_probe_deadline_and_wrapper_is_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)

            def execute():
                probe.CACHE.run_command(["docker", "exec", "owned", "ctr"], timeout=1200)
                runner.report["passed"] = True

            with (
                patch.object(probe.CACHE, "run_command") as original,
                patch.object(runner, "remaining", return_value=0.25),
                patch.object(runner, "execute", side_effect=execute),
                patch("builtins.print"),
            ):
                self.assertEqual(runner.perform(), 0)
                self.assertEqual(original.call_args.kwargs["timeout"], 0.25)
                self.assertIs(probe.CACHE.run_command, original)

    def test_timeout_budget_prevents_late_command(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = self.runner(directory)
            runner.deadline = 0
            with patch.object(probe.subprocess, "run") as run:
                with self.assertRaises(TimeoutError):
                    runner.run(["kind", "create", "cluster"])
                run.assert_not_called()
            with patch("builtins.print"):
                self.assertEqual(runner.perform(), 1)
            self.assertFalse(runner.private.exists())
