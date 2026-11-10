"""Guard cache integrity, measured pull settings and sanitized benchmark evidence."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
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
        self.assertEqual(PERF.configure_pull_concurrency(original, 1), original)
        parallel = PERF.configure_pull_concurrency(original, 2)
        self.assertEqual(
            parallel.replace("maxParallelImagePulls: 2", "maxParallelImagePulls: 1"), original
        )
        for invalid in (0, 3, -1):
            with self.assertRaises(ValueError):
                PERF.configure_pull_concurrency(original, invalid)
        for invalid in (
            original.replace("serializeImagePulls: false", "serializeImagePulls: true"),
            original + "\n    maxParallelImagePulls: 1\n",
        ):
            with self.assertRaises(ValueError):
                PERF.configure_pull_concurrency(invalid, 2)

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
                        "kubelet_pull_config_matches": True,
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

    def test_changed_hardware_revision_or_failed_acceptance_is_not_a_controlled_comparison(self):
        for filename, field, value in (
            ("performance.json", "docker", {}),
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
