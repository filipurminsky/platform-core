"""Guard cache integrity, measured pull settings and sanitized benchmark evidence."""

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


CACHE = module("dev-image-cache")
PERF = module("dev-performance")
COMPARE = module("compare-dev-performance")
RETAINED_CONTENT = (
    '[plugins."io.containerd.grpc.v1.cri".containerd]\ndiscard_unpacked_layers = false\n'
)


class ClockGuardTests(unittest.TestCase):
    def test_equal_advances_pass_and_forward_or_backward_jumps_latch_failure(self):
        for jump in (6, -6, 3600):
            with (
                self.subTest(jump=jump),
                patch.object(PERF.time, "monotonic", return_value=100) as monotonic,
                patch.object(PERF.time, "time", return_value=1000) as wall,
            ):
                guard = PERF.ClockGuard()
                monotonic.return_value, wall.return_value = 110, 1010
                self.assertEqual(guard.observe()["clock_drift_seconds"], 0)
                self.assertTrue(guard.passed)
                wall.return_value = 1015
                guard.observe()
                self.assertTrue(guard.passed, "exactly five seconds remains within the bound")
                wall.return_value = 1010 + jump
                guard.observe()
                self.assertFalse(guard.passed)
                wall.return_value = 1010
                guard.observe()
                self.assertFalse(
                    guard.passed, "a subsequent correction cannot erase a detected jump"
                )
                self.assertEqual(guard.evidence()["max_absolute_drift_seconds"], abs(jump))

    def test_sampler_stops_between_commands_and_preserves_resource_evidence(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(PERF.signal, "signal"),
            patch.object(PERF.time, "monotonic", return_value=100),
            patch.object(PERF.time, "time", return_value=1000) as wall,
        ):
            output = Path(directory) / "performance.json"
            args = SimpleNamespace(
                cluster="fixture",
                context="kind-fixture",
                source_revision="a" * 40,
                image_pull_concurrency=1,
                cache_mode="cold",
                output=output,
                stop_file=Path(directory) / "stop",
                max_duration=7200,
                interval=15,
            )

            def command(arguments, **_):
                if arguments[:2] == ["docker", "info"]:
                    return "{}"
                if arguments[:2] == ["docker", "ps"]:
                    return ""
                if arguments[:3] == ["kind", "get", "nodes"]:
                    return "fixture-worker\n"
                if arguments[:2] == ["docker", "stats"]:
                    wall.return_value = 2000
                    return json.dumps(
                        {
                            "Name": "fixture-worker",
                            "CPUPerc": "10%",
                            "MemUsage": "1GiB / 32GiB",
                            "NetIO": "1MB / 2MB",
                            "BlockIO": "0B / 3MB",
                        }
                    )
                self.fail(f"sampler continued querying after clock discontinuity: {arguments}")

            with patch.object(PERF, "command", side_effect=command):
                self.assertEqual(PERF.monitor(args), 1)
            evidence = json.loads(output.read_text())
            self.assertFalse(evidence["suitable_for_comparison"])
            self.assertEqual(evidence["failure_reason"], "clock-discontinuity")
            self.assertEqual(evidence["clock_guard"]["max_absolute_drift_seconds"], 1000)
            self.assertEqual(evidence["summary"]["peak_cluster_memory_bytes"], 2**30)
            self.assertEqual(evidence["samples"][0]["wall_elapsed_seconds"], 1000)
            self.assertEqual(evidence["samples"][0]["monotonic_elapsed_seconds"], 0)
            self.assertTrue(evidence["completed"])


class PullConfigurationTests(unittest.TestCase):
    def test_pull_event_evidence_never_copies_registry_credentials_or_signed_urls(self):
        events = [
            {
                "metadata": {"uid": "failure", "namespace": "apps"},
                "reason": "Failed",
                "message": 'Failed to pull image "example/app": https://user:password@registry/blob?X-Amz-Signature=SECRET Bearer PRIVATE token=HIDDEN',
            },
            {
                "metadata": {"uid": "success", "namespace": "apps"},
                "reason": "Pulled",
                "message": 'Successfully pulled image "example/app" in 3.397s (5m10.943s including waiting). Image size: 43827963 bytes.',
            },
        ]
        observed = PERF.pull_events(events)
        encoded = json.dumps(observed)
        for secret in ("password", "SECRET", "PRIVATE", "HIDDEN", "https://"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(observed[1]["pull_duration"], "3.397s")
        self.assertEqual(observed[1]["pull_duration_including_wait"], "5m10.943s")
        self.assertEqual(observed[1]["image_size_bytes"], 43827963)
        self.assertEqual(PERF.summarize([], observed)["registry_pull_events"], 1)

    def test_actual_kubelet_configuration_requires_both_fields(self):
        self.assertEqual(
            PERF.kubelet_pull_config("serializeImagePulls: false\nmaxParallelImagePulls: 2\n"),
            {"serializeImagePulls": False, "maxParallelImagePulls": 2},
        )
        with self.assertRaises(ValueError):
            PERF.kubelet_pull_config("serializeImagePulls: false\n")

    def test_one_or_two_pulls_change_only_kubelet_limit_and_keep_node_pins(self):
        original = (ROOT / "terraform/modules/kind/kind-config.yaml").read_text()
        serial = PERF.configure_pull_concurrency(original, 1)
        self.assertEqual(serial, original.rstrip() + "\n" + PERF.CONTENT_RETENTION_PATCH)
        parallel = PERF.configure_pull_concurrency(original, 2)
        self.assertEqual(
            parallel.replace("maxParallelImagePulls: 2", "maxParallelImagePulls: 1"), serial
        )
        self.assertEqual(PERF.configure_pull_concurrency(serial, 1), serial)
        for invalid in (0, 3, -1):
            with self.assertRaises(ValueError):
                PERF.configure_pull_concurrency(original, invalid)
        for invalid in (
            original.replace("serializeImagePulls: false", "serializeImagePulls: true"),
            original + "\n    maxParallelImagePulls: 1\n",
        ):
            with self.assertRaises(ValueError):
                PERF.configure_pull_concurrency(invalid, 2)

    def test_retention_requires_explicit_false_in_v2_or_v3_runtime_config(self):
        for text in (
            RETAINED_CONTENT,
            '[plugins."io.containerd.cri.v1.images"]\ndiscard_unpacked_layers = false\n',
        ):
            self.assertEqual(
                PERF.containerd_image_retention(text), {"discard_unpacked_layers": False}
            )
        self.assertIsNone(PERF.containerd_image_retention("")["discard_unpacked_layers"])
        self.assertTrue(
            PERF.containerd_image_retention(RETAINED_CONTENT.replace("false", "true"))[
                "discard_unpacked_layers"
            ]
        )
        with self.assertRaisesRegex(ValueError, "unsupported containerd"):
            PERF.configure_image_content_retention("containerdConfigPatches: []\n")

    def test_stats_convert_real_docker_units_and_keep_per_core_cpu(self):
        rows = PERF.parse_stats(
            json.dumps(
                {
                    "Name": "qa-worker",
                    "CPUPerc": "175.2%",
                    "MemUsage": "1.5GiB / 31.29GiB",
                    "NetIO": "8.58kB / 2MB",
                    "BlockIO": "0B / 3GB",
                }
            )
        )
        self.assertEqual(rows[0]["cpu_percent"], 175.2)
        self.assertEqual(rows[0]["memory_bytes"], 1610612736)
        self.assertEqual(rows[0]["network_rx_bytes"], 8580)
        self.assertEqual(rows[0]["block_write_bytes"], 3000000000)
        with self.assertRaises(ValueError):
            PERF.size_bytes("unknown")

    def test_app_observations_never_include_source_values_or_controller_messages(self):
        items = [
            {
                "metadata": {"name": "demo"},
                "spec": {"source": {"helm": {"values": "password: secret"}}},
                "status": {
                    "operationState": {
                        "phase": "Running",
                        "retryCount": 7,
                        "message": "token=secret",
                    },
                    "sync": {"status": "OutOfSync", "comparedTo": {"source": "secret"}},
                    "health": {"status": "Healthy"},
                },
            }
        ]
        observation = PERF.application_states(items)
        self.assertEqual(observation[0]["retry_count"], 7)
        self.assertNotIn("secret", json.dumps(observation))
        self.assertNotIn("message", observation[0])


class ImageCacheTests(unittest.TestCase):
    def test_preflight_checks_native_node_pin_without_accessing_any_cluster(self):
        image = {"Id": "sha256:" + "b" * 64, "Os": "linux", "Architecture": "arm64"}
        with patch.object(
            CACHE, "command", side_effect=[json.dumps([image]), "aarch64\n"]
        ) as command:
            nodes = CACHE.expected_nodes(ROOT / "terraform/modules/kind/kind-config.yaml")
        self.assertEqual(set(nodes), {"control-plane", "worker", "worker2"})
        self.assertEqual(nodes["worker"]["platform"], "linux/arm64")
        self.assertTrue(all(call.args[0][0] == "docker" for call in command.call_args_list))
        with patch.object(CACHE, "command", side_effect=[json.dumps([image]), "x86_64\n"]):
            with self.assertRaisesRegex(ValueError, "architecture"):
                CACHE.expected_nodes(ROOT / "terraform/modules/kind/kind-config.yaml")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.revision = "a" * 40
        self.identity = "sha256:" + "b" * 64
        self.images = {"registry.example.com/platform:v1": self.identity}
        self.nodes = {
            suffix: {
                "container": f"qa-{suffix}",
                "node_image_id": self.identity,
                "node_image_reference": "kindest/node@" + self.identity,
                "platform": "linux/arm64",
            }
            for suffix in ("control-plane", "worker")
        }
        self.manifest = {"schema_version": 1, "source_revision": self.revision, "nodes": {}}
        for suffix, node in self.nodes.items():
            archive = self.directory / f"{suffix}.tar"
            archive.write_bytes(b"fixture archive " + suffix.encode())
            self.manifest["nodes"][suffix] = {
                key: value for key, value in node.items() if key != "container"
            } | {
                "archive_bytes": archive.stat().st_size,
                "archive_sha256": CACHE.digest(archive),
                "images": self.images,
            }
        self.write_manifest()

    def write_manifest(self):
        (self.directory / "manifest.json").write_text(json.dumps(self.manifest))

    def test_local_apps_are_never_exported_even_under_digest_names(self):
        inventory = "REF TYPE DIGEST SIZE PLATFORMS LABELS\n"
        for reference in (
            "docker.io/library/audio-api:latest",
            "docker.io/library/echo-service@" + self.identity,
            "import-2026-11-10@" + self.identity,
            "sha256:" + "c" * 64,
            "registry.example.com/platform:v1",
        ):
            inventory += f"{reference} application/vnd.oci.image.manifest.v1+json {self.identity} 10MB linux/arm64 -\n"
        self.assertEqual(CACHE.inventory(inventory), self.images)

    def test_mismatched_revision_architecture_node_image_or_topology_rejects_before_import(self):
        for change in ("revision", "platform", "node_image_id", "topology"):
            with self.subTest(change=change), patch.object(CACHE.subprocess, "run") as run:
                manifest = json.loads(json.dumps(self.manifest))
                if change == "revision":
                    manifest["source_revision"] = "c" * 40
                elif change == "topology":
                    del manifest["nodes"]["worker"]
                else:
                    manifest["nodes"]["worker"][change] = "wrong"
                (self.directory / "manifest.json").write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    CACHE.import_cache(self.directory, self.revision, self.nodes)
                run.assert_not_called()

    def test_corrupt_second_archive_does_not_partially_import_first(self):
        (self.directory / "worker.tar").write_bytes(b"bad archive")
        with patch.object(CACHE.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "checksum"):
                CACHE.import_cache(self.directory, self.revision, self.nodes)
            run.assert_not_called()

    def test_restore_checks_actual_image_digests_after_import(self):
        with (
            patch.object(CACHE.subprocess, "run") as run,
            patch.object(CACHE, "node_inventory", return_value=self.images),
        ):
            CACHE.import_cache(self.directory, self.revision, self.nodes)
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                self.assertIn("--local", call.args[0])
                self.assertIn("io.cri-containerd.image=managed", call.args[0])
        with (
            patch.object(CACHE.subprocess, "run"),
            patch.object(CACHE, "node_inventory", return_value={}),
        ):
            with self.assertRaisesRegex(ValueError, "identities differ"):
                CACHE.import_cache(self.directory, self.revision, self.nodes)

    def test_export_refuses_overwriting_prior_cache(self):
        with patch.object(CACHE.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "empty"):
                CACHE.export_cache(self.directory, self.revision, self.nodes)
            run.assert_not_called()

    def test_export_uses_oci_only_and_preserves_digest_references(self):
        destination = self.directory / "export"
        images = {"registry.example.com/platform@" + self.identity: self.identity}

        def export(arguments, **kwargs):
            self.assertIn("--skip-manifest-json", arguments)
            self.assertIn("--local", arguments)
            self.assertIn(next(iter(images)), arguments)
            kwargs["stdout"].write(b"OCI archive")

        with (
            patch.object(CACHE, "command", return_value=RETAINED_CONTENT),
            patch.object(CACHE.subprocess, "run", side_effect=export),
            patch.object(CACHE, "node_inventory", return_value=images),
        ):
            manifest = CACHE.export_cache(destination, self.revision, self.nodes)
        self.assertEqual(manifest["nodes"]["worker"]["images"], images)
        CACHE.validate_cache(destination, self.revision, self.nodes)

    def test_failed_export_preserves_safe_context_and_private_diagnostics(self):
        destination = self.directory / "failed-export"
        report = self.directory / "summary.json"
        diagnostic = self.directory / "private.log"
        private_stderr = b"unable to create manifests file: token=PRIVATE"
        error = subprocess.CalledProcessError(1, ["ctr"], stderr=private_stderr)
        with (
            patch.object(CACHE, "command", return_value=RETAINED_CONTENT),
            patch.object(CACHE, "nodes_for", return_value=self.nodes),
            patch.object(CACHE, "node_inventory", return_value=self.images),
            patch.object(CACHE.subprocess, "run", side_effect=error),
            patch.object(sys, "stdout", new_callable=io.StringIO) as stdout,
            patch.object(
                sys,
                "argv",
                [
                    "dev-image-cache.py",
                    "export",
                    "--cluster=fixture",
                    f"--cache-dir={destination}",
                    f"--source-revision={self.revision}",
                    f"--report={report}",
                    f"--diagnostic-log={diagnostic}",
                ],
            ),
        ):
            self.assertEqual(CACHE.main(), 1)
        result = json.loads(report.read_text())
        self.assertEqual(result["stage"], "export-archive")
        self.assertEqual(result["node"], "control-plane")
        self.assertEqual(result["exit_code"], 1)
        self.assertNotIn("PRIVATE", report.read_text() + stdout.getvalue())
        self.assertEqual(diagnostic.read_bytes(), private_stderr)
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
        self.assertFalse((destination / "manifest.json").exists())
        self.assertEqual(list(destination.iterdir()), [])

    def test_export_rejects_discarded_content_before_any_archive_is_created(self):
        for config in ("", RETAINED_CONTENT.replace("false", "true")):
            destination = self.directory / "discarded"
            with (
                self.subTest(config=config),
                patch.object(CACHE, "command", return_value=config),
                patch.object(CACHE, "node_inventory") as inventory,
            ):
                with self.assertRaisesRegex(ValueError, "fresh cluster"):
                    CACHE.export_cache(destination, self.revision, self.nodes)
                inventory.assert_not_called()
                self.assertFalse(destination.exists())


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = [self.root / name for name in ("cold1", "cold2", "warm2")]
        for path, concurrency, mode, duration in zip(
            self.paths, (1, 2, 2), ("cold", "cold", "warm"), (100, 80, 60), strict=True
        ):
            summary = path / "summary"
            summary.mkdir(parents=True)
            (summary / "result.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "functional_checks_passed": True,
                        "source_revision": "a" * 40,
                        "cleanup_errors": [],
                        "steps": [
                            {"name": "bootstrap", "exit_code": 0, "seconds": duration},
                            {"name": "convergence", "exit_code": 0, "seconds": 10},
                            {"name": "cache-export", "exit_code": 0, "seconds": 999},
                        ],
                    }
                )
            )
            (summary / "performance.json").write_text(
                json.dumps(
                    {
                        "completed": True,
                        "clock_guard": {
                            "passed": True,
                            "threshold_seconds": 5,
                            "max_absolute_drift_seconds": 0,
                        },
                        "suitable_for_comparison": True,
                        "kubelet_pull_config_matches": True,
                        "containerd_image_retention_matches": True,
                        "source_revision": "a" * 40,
                        "image_pull_concurrency_per_node": concurrency,
                        "cache_mode": mode,
                        "cluster": path.name,
                        "docker": dict.fromkeys(COMPARE.HARDWARE, 32),
                        "running_containers_at_start": [],
                        "running_containers_at_end": [],
                        "summary": {},
                        "sample_errors": [],
                        "samples": [
                            {
                                "elapsed_seconds": 10,
                                "sampled_utc": "2026-11-11T00:00:10+00:00",
                                "wall_elapsed_seconds": 10,
                                "monotonic_elapsed_seconds": 10,
                                "clock_drift_seconds": 0,
                                "node_conditions": {
                                    "worker": [{"type": "MemoryPressure", "status": "False"}]
                                },
                            }
                        ],
                    }
                )
            )
            for name in ("import", "export"):
                (summary / f"image-cache-{name}.json").write_text(
                    json.dumps(
                        {
                            "success": True,
                            "source_revision": "a" * 40,
                            "manifest_sha256": "b" * 64,
                            "elapsed_seconds": 5,
                        }
                    )
                )

    def test_comparison_includes_import_and_excludes_export_from_startup(self):
        report = COMPARE.compare(*self.paths)
        self.assertEqual(report["cold_parallel_vs_serial"]["seconds_saved"], 20)
        self.assertEqual(report["runs"]["warm2"]["bootstrap_and_convergence_seconds"], 70)
        self.assertEqual(report["runs"]["warm2"]["cache_import_seconds"], 5)
        self.assertEqual(report["runs"]["cold2"]["cache_export_seconds_excluded"], 999)

    def test_comparison_rejects_missing_failed_and_invalid_clock_evidence(self):
        path = self.paths[1] / "summary/performance.json"
        original = path.read_text()
        for mutation in (
            "missing",
            "failed",
            "nonfinite",
            "negative",
            "sample_missing",
            "sample_inconsistent",
            "sample_jump",
            "naive_utc",
        ):
            with self.subTest(mutation=mutation):
                data = json.loads(original)
                if mutation == "missing":
                    del data["clock_guard"]
                elif mutation == "failed":
                    data["clock_guard"]["passed"] = False
                elif mutation == "nonfinite":
                    data["clock_guard"]["max_absolute_drift_seconds"] = float("nan")
                elif mutation == "negative":
                    data["clock_guard"]["max_absolute_drift_seconds"] = -1
                elif mutation == "sample_missing":
                    del data["samples"][0]["monotonic_elapsed_seconds"]
                elif mutation == "sample_inconsistent":
                    data["samples"][0]["wall_elapsed_seconds"] = 11
                elif mutation == "sample_jump":
                    data["samples"][0]["clock_drift_seconds"] = 3600
                else:
                    data["samples"][0]["sampled_utc"] = "2026-11-11T00:00:10"
                path.write_text(json.dumps(data))
                with self.assertRaisesRegex(ValueError, "clock-discontinuity"):
                    COMPARE.compare(*self.paths)

    def test_changed_hardware_revision_or_failed_acceptance_is_not_a_controlled_comparison(self):
        for filename, field, value in (
            ("performance.json", "docker", {}),
            ("performance.json", "containerd_image_retention_matches", False),
            ("performance.json", "containerd_image_retention_matches", None),
            ("result.json", "source_revision", "c" * 40),
            ("result.json", "passed", False),
        ):
            with self.subTest(field=field):
                path = self.paths[1] / "summary" / filename
                original = path.read_text()
                data = json.loads(original)
                data[field] = value
                path.write_text(json.dumps(data))
                with self.assertRaises((ValueError, KeyError)):
                    COMPARE.compare(*self.paths)
                path.write_text(original)


if __name__ == "__main__":
    unittest.main()
