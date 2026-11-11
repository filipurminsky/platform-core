"""Check prewarming inputs and failure evidence without building or pulling images."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "preparation", ROOT / ".github/scripts/prepare-dev-benchmark.py"
)
preparation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preparation)
CONFIG = (ROOT / "terraform/modules/kind/kind-config.yaml").read_text()


class DevPreparation(unittest.TestCase):
    def test_three_identical_committed_node_pins_required(self):
        pin = preparation.node_pin(CONFIG)
        self.assertTrue(pin.startswith("kindest/node:v1.36.4@sha256:"))
        invalid = (
            CONFIG.replace(pin, "kindest/node:latest"),
            CONFIG.replace(pin, "kindest/node:v1.36.4@sha256:" + "b" * 64, 1),
            CONFIG.replace("  - role: worker\n", "  - role: extra-worker\n", 1),
        )
        for config in invalid:
            with self.subTest(config=config[:30]), self.assertRaises(ValueError):
                preparation.node_pin(config)

    def test_prepare_builds_all_apps_then_pulls_exact_node_and_records_ids(self):
        calls = []

        def capture(command, **_):
            calls.append(command)
            return "a" * 40 if command[0] == "git" else "sha256:" + "b" * 64

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "preparation.json"
            with (
                patch.object(preparation.subprocess, "check_output", side_effect=capture),
                patch.object(
                    preparation.subprocess,
                    "run",
                    side_effect=lambda command, **_: calls.append(command),
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(preparation.prepare(ROOT, "a" * 40, output), 0)
            result = json.loads(output.read_text())
        self.assertEqual(calls[1], ["make", "images"])
        self.assertEqual(calls[2], ["docker", "pull", preparation.node_pin(CONFIG)])
        self.assertEqual(len(result["app_image_ids"]), 7)
        self.assertEqual(result["source_revision"], "a" * 40)
        self.assertFalse(result["included_in_startup_comparison"])
        self.assertTrue(result["passed"])

    def test_wrong_source_stops_before_any_build_or_pull(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "preparation.json"
            with (
                patch.object(preparation.subprocess, "check_output", return_value="b" * 40),
                patch.object(preparation.subprocess, "run") as run,
                patch("builtins.print"),
            ):
                self.assertEqual(preparation.prepare(ROOT, "a" * 40, output), 1)
                run.assert_not_called()
            self.assertFalse(json.loads(output.read_text())["passed"])

    def test_build_failure_does_not_pull_or_persist_subprocess_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "preparation.json"
            with (
                patch.object(preparation.subprocess, "check_output", return_value="a" * 40),
                patch.object(
                    preparation.subprocess,
                    "run",
                    side_effect=subprocess.CalledProcessError(
                        9, ["make", "images"], output="private payload"
                    ),
                ) as run,
                patch("builtins.print"),
            ):
                self.assertEqual(preparation.prepare(ROOT, "a" * 40, output), 1)
                self.assertEqual(run.call_count, 1)
            text = output.read_text()
            self.assertNotIn("private payload", text)
            self.assertEqual(json.loads(text)["phase"], "build-seven-app-images")
