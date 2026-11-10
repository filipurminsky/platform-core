"""Exercise early rejection and the resource artifact's explicit field boundary."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "dev_resources", ROOT / ".github/scripts/check-dev-resources.py"
)
resources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resources)


class RunnerResources(unittest.TestCase):
    def test_private_runner_memory_is_rejected(self):
        self.assertTrue(resources.failures(8 * resources.GIB, 4, 100 * resources.GIB))

    def test_documented_standard_disk_capacity_is_rejected(self):
        self.assertTrue(resources.failures(16 * resources.GIB, 4, 14 * resources.GIB))

    def test_too_few_cpus_are_rejected(self):
        self.assertTrue(resources.failures(16 * resources.GIB, 2, 100 * resources.GIB))

    def test_nominal_16_gib_with_kernel_reservation_is_accepted(self):
        self.assertFalse(resources.failures(15 * resources.GIB, 4, 40 * resources.GIB))

    def test_missing_docker_leaves_failed_sanitized_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "resources.json"
            with (
                patch("sys.argv", ["check-dev-resources", "--output", str(output)]),
                patch.object(resources.subprocess, "check_output", side_effect=FileNotFoundError),
                patch("builtins.print"),
            ):
                self.assertEqual(resources.main(), 1)
            report = json.loads(output.read_text())
            self.assertFalse(report["passed"])
            self.assertIn("verify daemon access", report["failures"][0])

    def test_failure_artifact_excludes_unrelated_docker_configuration(self):
        info = {
            "DockerRootDir": "/var/lib/docker",
            "MemTotal": 8 * resources.GIB,
            "NCPU": 2,
            "HttpProxy": "http://example:test-credential@proxy.invalid",
            "RegistryConfig": {"unrelated": "test-credential"},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "resources.json"
            with (
                patch("sys.argv", ["check-dev-resources", "--output", str(output)]),
                patch.object(resources.subprocess, "check_output", return_value=json.dumps(info)),
                patch.object(
                    resources.shutil,
                    "disk_usage",
                    return_value=resources.shutil._ntuple_diskusage(100, 86, 14),
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(resources.main(), 1)
            evidence = output.read_text()
            self.assertNotIn("test-credential", evidence)
            self.assertNotIn("RegistryConfig", evidence)
            self.assertFalse(json.loads(evidence)["passed"])
