"""Execute CI orchestration with stub lifecycles; verify sequencing and upload scope."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/run-dev-benchmark.sh"


class DevBenchmark(unittest.TestCase):
    def run_benchmark(self, directory, fail_phase=""):
        binary = directory / "bin"
        binary.mkdir()
        executable = binary / "python3"
        executable.write_text(
            '#!/bin/bash\nprintf "%s\\0" "$@" >> "$CALL_LOG"\n'
            'printf "\\n" >> "$CALL_LOG"\n'
            'if [[ -n "$FAIL_PHASE" && "$*" == *"/$FAIL_PHASE"* ]]; then exit 7; fi\n'
        )
        executable.chmod(0o755)
        log = directory / "calls"
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env={
                **os.environ,
                "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                "RUNNER_TEMP": str(directory),
                "GITHUB_SHA": "a" * 40,
                "CALL_LOG": str(log),
                "FAIL_PHASE": fail_phase,
            },
            capture_output=True,
            text=True,
        )
        calls = (
            [line.split("\0")[:-1] for line in log.read_text().splitlines()] if log.exists() else []
        )
        return result, calls

    def test_three_full_runs_share_revision_and_cache_then_compare(self):
        with tempfile.TemporaryDirectory(prefix="benchmark space ") as temp:
            directory = Path(temp)
            result, calls = self.run_benchmark(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(calls), 5)
            self.assertEqual(calls[0][0], ".github/scripts/prepare-dev-benchmark.py")
            self.assertIn("--expected-revision=" + "a" * 40, calls[0])
            self.assertIn(f"--output={directory}/dev-benchmark/preparation.json", calls[0])
            for call, phase, concurrency in zip(
                calls[1:4], ("cold1", "cold2", "warm2"), (1, 2, 2), strict=True
            ):
                self.assertEqual(call[0], "scripts/dev-acceptance.py")
                self.assertIn("--expected-revision=" + "a" * 40, call)
                self.assertIn(f"--evidence-dir={directory}/dev-benchmark/{phase}", call)
                self.assertIn(f"--image-pull-concurrency={concurrency}", call)
                self.assertIn("--timeout=2400", call)
                self.assertIn("--install-timeout=600", call)
            self.assertIn(f"--write-image-cache={directory}/dev-benchmark-image-cache", calls[2])
            self.assertIn(f"--image-cache={directory}/dev-benchmark-image-cache", calls[3])
            self.assertEqual(calls[4][0], "scripts/compare-dev-performance.py")
            self.assertIn(f"--output={directory}/dev-benchmark/comparison.json", calls[4])

    def test_any_failure_stops_subsequent_phases_and_propagates_exit(self):
        for phase, count in (
            ("prepare-dev-benchmark.py", 1),
            ("cold1", 2),
            ("cold2", 3),
            ("warm2", 4),
            ("compare-dev-performance.py", 5),
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                result, calls = self.run_benchmark(Path(temp), phase)
                self.assertEqual(result.returncode, 7)
                self.assertEqual(len(calls), count)

    def test_existing_cache_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            cache = directory / "dev-benchmark-image-cache"
            cache.mkdir()
            marker = cache / "manifest.json"
            marker.write_text("existing")
            result, calls = self.run_benchmark(directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(calls, [])
            self.assertEqual(marker.read_text(), "existing")

    def test_actual_workflow_artifact_patterns_exclude_private_files_and_cache(self):
        workflow = (ROOT / ".github/workflows/dev-acceptance.yaml").read_text()
        prefix = "${{ runner.temp }}/"
        patterns = [
            line.strip().removeprefix(prefix)
            for line in workflow.splitlines()
            if line.strip().startswith(prefix)
        ]
        safe = {
            "dev-acceptance/summary/result.json",
            "dev-benchmark/cold1/summary/result.json",
            "dev-benchmark/cold2/summary/result.json",
            "dev-benchmark/warm2/summary/result.json",
            "dev-benchmark/comparison.json",
            "dev-benchmark/preparation.json",
            "dev-acceptance-resources.json",
        }
        private = {
            "dev-benchmark/cold1/logs/bootstrap.log",
            "dev-benchmark/cold2/secret.json",
            "dev-benchmark/warm2/kubeconfig",
            "dev-acceptance/logs/bootstrap.log",
            "dev-benchmark-image-cache/manifest.json",
            "dev-benchmark-image-cache/worker.tar",
        }
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for name in safe | private:
                target = directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            selected = {
                str(path.relative_to(directory))
                for pattern in patterns
                for path in directory.glob(pattern)
            }
            self.assertEqual(selected, safe)
