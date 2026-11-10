"""Negative fixtures test the same guards invoked by CI, with no AWS credentials."""

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures"
spec = importlib.util.spec_from_file_location("iac_security", ROOT / "policy/terraform/security.py")
security = importlib.util.module_from_spec(spec)
spec.loader.exec_module(security)


def read(name):
    return json.loads((FIXTURES / name).read_text())


def mutate(document, path, value):
    result = copy.deepcopy(document)
    parent = result
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = value
    return result


class SecurityPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = security.load_source(ROOT)
        cls.plan = read("safe-plan.json")

    def test_plan_cli_returns_nonzero_for_unsafe_settings(self):
        for public, expected in ((False, 0), (None, 1)):
            plan = copy.deepcopy(self.plan)
            config = plan["planned_values"]["root_module"]["child_modules"][0]["resources"][0][
                "values"
            ]["vpc_config"][0]
            config["endpoint_public_access"] = public
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "plan.json"
                path.write_text(json.dumps(plan))
                result = subprocess.run(
                    [sys.executable, str(ROOT / "policy/terraform/security.py"), "plan", str(path)],
                    capture_output=True,
                    text=True,
                )
            self.assertEqual(result.returncode, expected, result.stderr)
            if expected:
                self.assertIn("API_PUBLIC:", result.stderr)

    def test_current_repository_source(self):
        self.assertEqual(security.source_errors(self.source), [])

    def test_safe_expanded_plan(self):
        self.assertEqual(security.plan_errors(self.plan), [])

    def test_missing_source_fails_closed(self):
        self.assertTrue(security.source_errors({}))

    def test_missing_plan_fails_closed(self):
        self.assertTrue(security.plan_errors({}))
        self.assertTrue(security.plan_errors({"planned_values": {"root_module": {}}}))

    def test_restricted_public_api_explicit_opt_in(self):
        plan = copy.deepcopy(self.plan)
        config = plan["planned_values"]["root_module"]["child_modules"][0]["resources"][0][
            "values"
        ]["vpc_config"][0]
        config.update(endpoint_public_access=True, public_access_cidrs=["203.0.113.10/32"])
        self.assertEqual(security.plan_errors(plan), [])

    def test_public_api_rejects_world_empty_unknown_and_split_world(self):
        for cidrs in (["0.0.0.0/0"], ["::/0"], [], None, ["invalid"], ["0.0.0.0/1", "128.0.0.0/1"]):
            with self.subTest(cidrs=cidrs):
                plan = copy.deepcopy(self.plan)
                config = plan["planned_values"]["root_module"]["child_modules"][0]["resources"][0][
                    "values"
                ]["vpc_config"][0]
                config.update(endpoint_public_access=True, public_access_cidrs=cidrs)
                self.assertTrue(any(e.startswith("API_CIDRS:") for e in security.plan_errors(plan)))


def negative(case, mode):
    def test(self):
        document = mutate(getattr(self, mode), case["path"], case["value"])
        errors = getattr(security, mode + "_errors")(document)
        self.assertTrue(any(e.startswith(case["code"] + ":") for e in errors), errors)

    return test


for mode in ("source", "plan"):
    for case in read(f"unsafe-{mode}-cases.json"):
        setattr(SecurityPolicy, "test_" + mode + "_" + case["name"], negative(case, mode))
