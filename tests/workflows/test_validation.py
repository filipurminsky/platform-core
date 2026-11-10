"""Exercise the real workflow validator, including the hosted rejection regression."""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class WorkflowValidation(unittest.TestCase):
    def validate(self, fixture):
        return subprocess.run(
            [
                str(ROOT / "scripts/validate-workflows.sh"),
                str(ROOT / "tests/workflows/fixtures" / fixture),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_runner_context_rejected_at_job_env(self):
        result = self.validate("invalid-job-context.yaml")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('context "runner"', result.stdout + result.stderr)

    def test_runner_context_accepted_at_step_env(self):
        result = self.validate("valid-step-context.yaml")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
