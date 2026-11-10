"""Execute operational scripts against isolated tools to verify mutation boundaries."""

import ast
import asyncio
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


class ScriptSandbox(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.root / "scripts").mkdir()
        self.log = self.root / "commands.jsonl"
        self.environment = {key: value for key, value in os.environ.items() if key != "CANARY_PORT"}
        self.environment.update(
            PATH=f"{self.bin}:{os.environ['PATH']}",
            COMMAND_LOG=str(self.log),
            CLUSTER_NAME="regression-test",
            KUBE_CONTEXT="kind-regression-test",
            EVIDENCE_DIR=str(self.root / "evidence"),
        )

    def copy_script(self, name):
        shutil.copyfile(ROOT / "scripts" / name, self.root / "scripts" / name)
        (self.root / "scripts" / name).chmod(0o755)

    def tool(self, name, body):
        target = self.bin / name
        target.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['COMMAND_LOG'], 'a') as log:\n"
            f"    log.write(json.dumps([{name!r}, *args]) + '\\n')\n" + body
        )
        target.chmod(0o755)

    def run_script(self, name, *arguments, **environment):
        return subprocess.run(
            ["bash", f"scripts/{name}", *arguments],
            cwd=self.root,
            env={**self.environment, **environment},
            text=True,
            capture_output=True,
            timeout=15,
        )

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class BootstrapCompatibilityTests(ScriptSandbox):
    def setUp(self):
        super().setUp()
        self.copy_script("bootstrap.sh")
        self.copy_script("doctor.sh")
        self.tool("docker", "print('Docker available')\n")
        self.tool(
            "kind",
            "if args == ['version']: print('kind v0.33.0')\n"
            "elif args == ['get', 'clusters']: print('regression-test')\n"
            "else: sys.exit('Unexpected kind mutation')\n",
        )
        self.tool(
            "helm",
            "if args[:1] == ['version']: print('v4.3.1')\n"
            "elif args[:1] == ['list']: pass\n"
            "else: sys.exit(91)  # stop at the first attempted installation\n",
        )
        self.tool(
            "kubectl",
            "if args[-4:] == ['get', 'nodes', '-o', 'json']:\n"
            "    print(json.dumps({'items':[{'status':{'nodeInfo':{'kubeletVersion':version}}} "
            "for version in json.loads(os.environ['NODE_VERSIONS'])]}))\n"
            "elif args[-3:] == ['get', 'daemonset', 'kindnet']:\n"
            "    sys.exit(0 if os.environ.get('HAS_KINDNET') == 'true' else 1)\n"
            "elif args[-3:-1] == ['config', 'use-context']: pass\n"
            "else: sys.exit('Unexpected kubectl operation')\n",
        )

    def assert_no_installation(self):
        commands = self.commands()
        self.assertFalse(
            any(command[0] == "helm" and command[1] != "version" for command in commands)
        )
        self.assertFalse(any("use-context" in command for command in commands))
        self.assertFalse(any("apply" in command or "create" in command for command in commands))
        for command in commands:
            if command[0] == "kubectl":
                self.assertEqual(command[1:3], ["--context", "kind-regression-test"])

    def test_old_helm_rejected_before_installation(self):
        self.tool("helm", "print('v3.18.0')\n")
        result = self.run_script(
            "bootstrap.sh", "--cluster-name=regression-test", NODE_VERSIONS='["v1.36.4"]'
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Helm >= 4.3.1 required", result.stderr)
        self.assert_no_installation()

    def test_installation_budget_is_bounded_configurable_and_reported_on_failure(self):
        self.tool(
            "helm",
            "if args[:1] == ['version']: print('v4.3.1')\n"
            "elif args[:1] == ['upgrade']: sys.exit(91)\n",
        )
        for arguments, expected in (([], "600s"), (["--install-timeout=777"], "777s")):
            with self.subTest(expected=expected):
                self.log.write_text("")
                result = self.run_script(
                    "bootstrap.sh",
                    "--cluster-name=regression-test",
                    *arguments,
                    NODE_VERSIONS='["v1.36.4"]',
                )
                self.assertEqual(result.returncode, 91)
                upgrade = next(cmd for cmd in self.commands() if cmd[:2] == ["helm", "upgrade"])
                self.assertEqual(upgrade[upgrade.index("--timeout") + 1], expected)
                self.assertIn(f"Helm readiness budget: {expected}", result.stderr)
                self.assertIn(
                    "--context kind-regression-test -n kube-system get events", result.stderr
                )
                self.assertIn("--install-timeout", result.stderr)

    def test_invalid_installation_budget_fails_before_any_tool_or_mutation(self):
        for value in ("0", "-1", "1m", "unlimited"):
            with self.subTest(value=value):
                result = self.run_script("bootstrap.sh", f"--install-timeout={value}")
                self.assertEqual(result.returncode, 2)
                self.assertIn("--install-timeout must be positive seconds", result.stderr)
                self.assertFalse(self.log.exists())

    def test_convergence_budget_reaches_waiter_and_its_failure_is_propagated(self):
        self.tool("helm", "if args[:1] == ['version']: print('v4.3.1')\n")
        self.tool(
            "kubectl",
            "if args[-4:] == ['get', 'nodes', '-o', 'json']:\n"
            "    print(json.dumps({'items':[{'status':{'nodeInfo':{'kubeletVersion':'v1.36.4'}}}]}))\n"
            "elif args[-3:] == ['get', 'daemonset', 'kindnet']: sys.exit(1)\n"
            "elif args[:2] == ['get', 'secret']: sys.exit(1)\n"
            "elif args == ['apply', '-f', '-']: sys.stdin.read()\n",
        )
        (self.root / "scripts/wait-for-dev.py").write_text(
            "import json, os, sys\n"
            "with open(os.environ['COMMAND_LOG'], 'a') as stream:\n"
            "    stream.write(json.dumps(['wait-for-dev', *sys.argv[1:]]) + '\\n')\n"
            "sys.exit(int(os.environ.get('WAITER_EXIT', '0')))\n"
        )
        for arguments, expected, status in (
            ([], "2400", 0),
            (["--timeout=777"], "777", 0),
            (["--timeout=777"], "777", 93),
        ):
            with self.subTest(expected=expected, status=status):
                self.log.write_text("")
                result = self.run_script(
                    "bootstrap.sh",
                    "--cluster-name=regression-test",
                    "--skip-images",
                    "--wait",
                    *arguments,
                    WAITER_EXIT=str(status),
                )
                self.assertEqual(result.returncode, status, result.stderr)
                waiter = next(cmd for cmd in self.commands() if cmd[0] == "wait-for-dev")
                self.assertIn(f"--timeout={expected}", waiter)
                self.assertIn("--context=kind-regression-test", waiter)
                if status:
                    self.assertIn("DEV convergence and routed endpoints", result.stderr)
                    self.assertNotIn("DEV acceptance passed", result.stdout)

    def test_invalid_convergence_budget_fails_before_any_tool_or_mutation(self):
        for value in ("0", "-1", "1m", "unlimited"):
            with self.subTest(value=value):
                result = self.run_script("bootstrap.sh", f"--timeout={value}")
                self.assertEqual(result.returncode, 2)
                self.assertIn("--timeout must be positive seconds", result.stderr)
                self.assertFalse(self.log.exists())

    def test_existing_old_or_mixed_nodes_rejected_before_cilium_or_context_change(self):
        for versions in (["v1.30.0"], ["v1.36.4", "v1.30.0"], []):
            with self.subTest(versions=versions):
                self.log.write_text("")
                result = self.run_script(
                    "bootstrap.sh",
                    "--cluster-name=regression-test",
                    NODE_VERSIONS=json.dumps(versions),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Existing cluster is not Kubernetes 1.36", result.stderr)
                self.assert_no_installation()

    def test_kindnet_rejected_even_when_kubernetes_version_matches(self):
        result = self.run_script(
            "bootstrap.sh",
            "--cluster-name=regression-test",
            NODE_VERSIONS='["v1.36.4"]',
            HAS_KINDNET="true",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Existing cluster uses kindnet", result.stderr)
        self.assert_no_installation()

    def test_compatible_existing_cluster_reaches_cilium_only_after_checks(self):
        result = self.run_script(
            "bootstrap.sh",
            "--cluster-name=regression-test",
            NODE_VERSIONS='["v1.36.4"]',
        )
        self.assertEqual(result.returncode, 91)  # fake Helm stops before installation
        commands = self.commands()
        version_check = next(index for index, command in enumerate(commands) if "nodes" in command)
        cni_check = next(index for index, command in enumerate(commands) if "kindnet" in command)
        context_change = next(
            index for index, command in enumerate(commands) if "use-context" in command
        )
        install = next(
            index
            for index, command in enumerate(commands)
            if command[0:3] == ["helm", "repo", "add"]
        )
        self.assertLess(version_check, cni_check)
        self.assertLess(cni_check, context_change)
        self.assertLess(context_change, install)


class CanaryImagePreparationTests(ScriptSandbox):
    def setUp(self):
        super().setUp()
        self.copy_script("prepare-canary-images.sh")
        service = self.root / "services/echo-service"
        service.mkdir(parents=True)
        shutil.copyfile(ROOT / "services/echo-service/main.py", service / "main.py")
        self.tool(
            "kind",
            "if args == ['get', 'clusters']: print('regression-test')\n",
        )
        self.tool(
            "docker",
            "if args[:1] == ['build'] and '--build-arg' in args:\n"
            "    source = pathlib.Path(args[-1], 'main.py').read_text()\n"
            "    pathlib.Path(os.environ['CAPTURED_SOURCE']).write_text(source)\n",
        )
        self.captured = self.root / "injected.py"

    def test_nested_body_assignment_does_not_confuse_fault_injection(self):
        result = self.run_script("prepare-canary-images.sh", CAPTURED_SOURCE=str(self.captured))
        self.assertEqual(result.returncode, 0, result.stderr)
        original = ast.parse((self.root / "services/echo-service/main.py").read_text())
        generated = ast.parse(self.captured.read_text())
        original_handlers = {
            node.name: node for node in original.body if isinstance(node, ast.AsyncFunctionDef)
        }
        generated_handlers = {
            node.name: node for node in generated.body if isinstance(node, ast.AsyncFunctionDef)
        }
        assignments = [
            node
            for node in ast.walk(original_handlers["echo"])
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "body" for target in node.targets)
        ]
        self.assertGreaterEqual(
            len(assignments), 2, "fixture must retain the nested body=None exception path"
        )
        for name in ("healthz", "readyz", "metrics"):
            self.assertEqual(ast.dump(original_handlers[name]), ast.dump(generated_handlers[name]))
        echo = generated_handlers["echo"]
        echo.decorator_list = []
        echo.args.args[0].annotation = None
        namespace = {"Response": lambda **kwargs: SimpleNamespace(**kwargs)}
        exec(compile(ast.Module(body=[echo], type_ignores=[]), "injected.py", "exec"), namespace)
        response = asyncio.run(namespace["echo"](None))
        self.assertEqual(response.status_code, 500)
        self.assertIn("intentional canary demo failure", response.content)
        self.assertTrue(
            any(command[:3] == ["kind", "load", "docker-image"] for command in self.commands())
        )

    def test_changed_handler_fails_before_loading_a_misleading_bad_image(self):
        source = self.root / "services/echo-service/main.py"
        source.write_text(source.read_text().replace("\n    body = None\n", "\n    body = {}\n"))
        result = self.run_script("prepare-canary-images.sh", CAPTURED_SOURCE=str(self.captured))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("echo handler changed", result.stderr)
        self.assertFalse(self.captured.exists())
        self.assertFalse(
            any(command[:3] == ["kind", "load", "docker-image"] for command in self.commands())
        )


class CanaryConnectionPreflightTests(ScriptSandbox):
    def setUp(self):
        super().setUp()
        self.copy_script("canary-demo.sh")
        self.port_forward_pid = self.root / "port-forward.pid"
        self.environment["PORT_FORWARD_PID_FILE"] = str(self.port_forward_pid)
        self.addCleanup(self.cleanup_port_forward)
        # Evidence correctness is tested in test_evidence.py. Here isolate shell
        # sequencing and connection allocation from controller-specific JSON.
        (self.root / "scripts/canary-evidence.py").write_text(
            "import sys\nif sys.argv[1] == 'baseline': print('echo-service:stable')\n"
        )
        self.tool(
            "kubectl",
            "joined = ' '.join(args)\n"
            "if 'port-forward' in args:\n"
            "    pathlib.Path(os.environ['PORT_FORWARD_PID_FILE']).write_text(str(os.getpid()))\n"
            "    if os.environ.get('PORT_FORWARD_FAIL'):\n"
            "        sys.exit('Unable to listen on port: address already in use')\n"
            "    print('Forwarding from 127.0.0.1:43123 -> 80', flush=True)\n"
            "    import time; time.sleep(60)\n"
            "elif 'get secret in-cluster' in joined: print('dev')\n"
            "elif 'get application' in joined:\n"
            "    print('true' if 'jsonpath=' in joined else '{\"metadata\":{\"annotations\":{}}}')\n"
            "elif 'get rollout' in joined and 'jsonpath=' in joined: print('echo-service:stable')\n"
            "elif 'get job' in joined or 'get configmap' in joined: pass\n"
            "elif ' get ' in ' ' + joined + ' ': print('{\"items\":[]}')\n",
        )
        self.tool(
            "curl",
            "if os.environ.get('CURL_FAIL'): sys.exit(22)\n"
            'print(\'{"service":"echo-service","hostname":"stable-pod"}\')\n',
        )

    def port_forward_alive(self):
        if not self.port_forward_pid.exists():
            return False
        try:
            os.kill(int(self.port_forward_pid.read_text()), 0)
            return True
        except ProcessLookupError:
            return False

    def cleanup_port_forward(self):
        # This runs even after an assertion or subprocess timeout, so testing
        # the historical orphan bug never leaves a long-lived fake process.
        if self.port_forward_alive():
            try:
                os.kill(int(self.port_forward_pid.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass

    def assert_port_forward_stopped(self):
        self.assertTrue(self.port_forward_pid.exists(), "port-forward must have started")
        deadline = time.monotonic() + 2
        while self.port_forward_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.port_forward_alive(), "cleanup orphaned the actual kubectl process")

    def test_port_conflict_fails_before_pause_load_or_image_mutation(self):
        result = self.run_script(
            "canary-demo.sh", "bad", PORT_FORWARD_FAIL="1", CANARY_PORT="18081"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("address already in use", result.stderr)
        for command in self.commands():
            self.assertFalse(
                any(operation in command for operation in ("annotate", "apply", "delete", "set")),
                command,
            )
            self.assertNotEqual(command[0], "curl")

    def test_allocated_port_is_used_for_all_gateway_requests(self):
        result = self.run_script("canary-demo.sh", "bad")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_port_forward_stopped()
        commands = self.commands()
        forwarding = next(
            index for index, command in enumerate(commands) if "port-forward" in command
        )
        self.assertIn("0:80", commands[forwarding])
        mutation = next(index for index, command in enumerate(commands) if "annotate" in command)
        self.assertLess(forwarding, mutation)
        requests = [command for command in commands if command[0] == "curl"]
        self.assertEqual(len(requests), 5)
        self.assertTrue(all(command[-1] == "http://localhost:43123/" for command in requests))
        self.assertTrue(
            any("argocd.argoproj.io/skip-reconcile-" in command for command in commands)
        )
        self.assertTrue(
            any("delete" in command and "tests/canary-load" in command for command in commands)
        )

    def test_request_failure_also_terminates_the_actual_port_forward(self):
        result = self.run_script("canary-demo.sh", "bad", CURL_FAIL="1")
        self.assertEqual(result.returncode, 22, result.stderr)
        self.assert_port_forward_stopped()
        commands = self.commands()
        self.assertTrue(
            any("argocd.argoproj.io/skip-reconcile-" in command for command in commands)
        )
        self.assertTrue(
            any("delete" in command and "tests/canary-load" in command for command in commands)
        )


if __name__ == "__main__":
    unittest.main()
