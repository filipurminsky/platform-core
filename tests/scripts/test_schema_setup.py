"""A refresh must remove obsolete schemas without destroying a working cache on error."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class SchemaSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.cache = self.path / "cache"
        self.cache.mkdir()
        (self.cache / "removed-v1.json").write_text("old schema")
        crd = {
            "spec": {
                "group": "test.example",
                "names": {"kind": "Example"},
                "versions": [
                    {
                        "name": "v2",
                        "served": True,
                        "schema": {"openAPIV3Schema": {"type": "object"}},
                    }
                ],
            }
        }
        self.executable(
            "yq",
            f"""#!/usr/bin/env python3
import sys
print({json.dumps(crd)!r} if "-o=json" in sys.argv else "1.0.0")
""",
        )
        self.executable(
            "curl",
            """#!/usr/bin/env python3
import os, pathlib, sys
if os.environ.get("FAIL_FETCH"):
    sys.exit(22)
pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_text("fixture")
""",
        )

    def executable(self, name, content):
        path = self.path / name
        path.write_text(content)
        path.chmod(0o755)

    def run_setup(self, **extra):
        return subprocess.run(
            ["bash", "scripts/setup-crd-schemas.sh"],
            cwd=ROOT,
            env={
                **os.environ,
                "PATH": f"{self.path}:{os.environ['PATH']}",
                "SCHEMA_DIR": str(self.cache),
                **extra,
            },
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_success_removes_obsolete_schemas(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.cache / "removed-v1.json").exists())
        self.assertTrue((self.cache / "test.example/example_v2.json").exists())

    def test_failed_download_preserves_existing_cache(self):
        result = self.run_setup(FAIL_FETCH="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.cache / "removed-v1.json").read_text(), "old schema")


if __name__ == "__main__":
    unittest.main()
