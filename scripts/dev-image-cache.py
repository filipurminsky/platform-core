#!/usr/bin/env python3
"""Export or restore verified platform OCI archives for an explicit disposable kind cluster.

Only images are cached: no node filesystem, kubeconfig, Secret, or workload volume.
Archives belong to one source revision, node image and architecture. Local app
images are excluded so bootstrap always builds current source.
"""

import argparse
import hashlib
import json
import re
import subprocess
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

APPS = {
    "audio-api",
    "echo-service",
    "llm-gateway",
    "llm-worker",
    "stt-worker",
    "tts-worker",
    "worker-service",
}


class CacheCommandError(RuntimeError):
    """Keep safe operation context separate from private subprocess diagnostics."""

    def __init__(self, stage, node, error):
        super().__init__(f"{stage} failed; inspect the local diagnostic log")
        self.stage = stage
        self.node = node
        self.exit_code = getattr(error, "returncode", None)
        self.stderr = error.stderr or b""
        self.diagnostics = {}


def classify_command_error(error):
    """Expose only fixed categories and content identities, never raw command output."""
    text = (
        error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else error.stderr
    )
    category = "command-failed"
    for marker, name in (
        ("no space left on device", "disk-full"),
        ("no manifest found for platform", "platform-unavailable"),
        ("invalid checksum digest format", "invalid-digest"),
        ("not found", "content-not-found"),
        ("unexpected digest", "digest-mismatch"),
        ("failed to copy to tar", "archive-write-failed"),
    ):
        if marker in text.lower():
            category = name
            break
    if error.exit_code is None:
        category = "timeout"
    return {
        "category": category,
        "content_digests": sorted(set(re.findall(r"\bsha256:[a-f0-9]{64}\b", text)))[:4],
    }


def export_command(node, references):
    return [
        "docker",
        "exec",
        node["container"],
        "ctr",
        "--namespace",
        "k8s.io",
        "images",
        "export",
        "--local",
        "--skip-manifest-json",
        "--platform",
        node["platform"],
        "-",
        *references,
    ]


def probe_export_failure(node, images):
    """Bounded read-only isolation; a timed-out probe is explicitly inconclusive."""
    started = time.monotonic()
    deadline = started + 60
    probes, seen = [], set()
    report = {"budget_seconds": 60, "per_image_timeout_seconds": 10, "probes": probes}
    for reference, identity in sorted(images.items()):
        if identity in seen:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0 or len(probes) >= 64:
            break
        seen.add(identity)
        probe = {"reference": reference, "digest": identity}
        try:
            run_command(
                export_command(node, [reference]),
                stage="diagnose-export",
                node=node["container"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=min(10, remaining),
            )
            probe["result"] = "passed"
        except CacheCommandError as error:
            probe.update(
                result="inconclusive" if error.exit_code is None else "failed",
                exit_code=error.exit_code,
                error=classify_command_error(error),
            )
        probes.append(probe)
        if probe["result"] == "failed":
            report["first_failed_reference"] = reference
            break
    report["all_unique_images_checked"] = len(seen) == len(set(images.values())) and all(
        probe["result"] != "inconclusive" for probe in probes
    )
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def run_command(args, *, stage, node=None, **kwargs):
    try:
        return subprocess.run(args, check=True, **kwargs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise CacheCommandError(stage, node, error) from error


def command(args, *, stage="inspect-nodes", node=None, **kwargs):
    return run_command(
        args, stage=stage, node=node, capture_output=True, text=True, timeout=60, **kwargs
    ).stdout


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(text):
    images = {}
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 3 or not re.fullmatch(r"sha256:[a-f0-9]{64}", fields[2]):
            raise ValueError("unexpected containerd image inventory")
        reference = fields[0]
        name = reference.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
        # ctr import creates synthetic index names as well as runnable references.
        # They can retain old local app content after its named tag is replaced.
        if reference.startswith(("sha256:", "import-")) or name in APPS:
            continue
        images[reference] = fields[2]
    return images


def node_inventory(node):
    return inventory(
        command(
            ["docker", "exec", node, "ctr", "--namespace", "k8s.io", "images", "list"],
            stage="inventory-images",
            node=node,
        )
    )


def incomplete_images(text, images):
    """Parse native-platform content STATUS; UNPACKED is deliberately irrelevant."""
    lines = text.splitlines()
    if not lines or lines[0].split() != ["REF", "TYPE", "DIGEST", "STATUS", "SIZE", "UNPACKED"]:
        raise ValueError("unexpected containerd image check header")
    seen, incomplete = set(), {}
    for line in lines[1:]:
        fields = line.split()
        if not fields or fields[0] not in images:
            continue
        reference = fields[0]
        if len(fields) < 4 or fields[2] != images[reference]:
            raise ValueError("image identity changed during native content check")
        if fields[3] not in ("complete", "incomplete"):
            raise ValueError("native image content is unavailable or could not be checked")
        seen.add(reference)
        if fields[3] == "incomplete":
            incomplete.setdefault(fields[2], reference)
    if seen != set(images):
        raise ValueError("native content check omitted an inventoried image")
    return incomplete


def immutable_reference(reference, identity):
    repository = reference.split("@", 1)[0]
    # A registry's port is part of the repository, not an image tag.
    if repository.rfind(":") > repository.rfind("/"):
        repository = repository.rsplit(":", 1)[0]
    return f"{repository}@{identity}"


def hydrate_image_content(node, images):
    """Fetch skipped compressed blobs without unpacking or changing original image identities."""
    started = time.monotonic()
    check = ["docker", "exec", node["container"], "ctr", "--namespace", "k8s.io", "images", "check"]
    incomplete = incomplete_images(
        command(check, stage="check-native-content", node=node["container"]), images
    )
    report = {"checked_references": len(images), "fetches": []}
    for identity, reference in incomplete.items():
        pinned = immutable_reference(reference, identity)
        fetch_started = time.monotonic()
        fetch = {"reference": reference, "pinned_reference": pinned, "digest": identity}
        report["fetches"].append(fetch)
        try:
            run_command(
                [
                    "docker",
                    "exec",
                    node["container"],
                    "ctr",
                    "--namespace",
                    "k8s.io",
                    "content",
                    "fetch",
                    "--platform",
                    node["platform"],
                    "--skip-metadata",
                    pinned,
                ],
                stage="hydrate-native-content",
                node=node["container"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=120,
            )
        except CacheCommandError as error:
            error.diagnostics["content_hydration"] = report
            raise
        finally:
            fetch["elapsed_seconds"] = round(time.monotonic() - fetch_started, 3)
    if incomplete:
        actual = node_inventory(node["container"])
        if any(actual.get(reference) != identity for reference, identity in images.items()):
            raise ValueError("image identities changed while fetching missing native content")
        if incomplete_images(
            command(check, stage="verify-native-content", node=node["container"]), images
        ):
            raise ValueError("native image content remains incomplete after hydration")
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def nodes_for(cluster):
    names = command(["kind", "get", "nodes", "--name", cluster]).split()
    if not names:
        raise ValueError("explicit kind cluster has no nodes")
    nodes = {}
    for name in sorted(names):
        info = json.loads(command(["docker", "inspect", name]))[0]
        if info["Config"].get("Labels", {}).get("io.x-k8s.kind.cluster") != cluster:
            raise ValueError("refusing a node that does not belong to the explicit kind cluster")
        suffix = name.removeprefix(f"{cluster}-")
        if not name.startswith(f"{cluster}-") or not re.fullmatch(
            r"control-plane\d*|worker\d*", suffix
        ):
            raise ValueError("unsupported kind node topology")
        image = json.loads(command(["docker", "image", "inspect", info["Image"]]))[0]
        nodes[suffix] = {
            "container": name,
            "node_image_id": info["Image"],
            "node_image_reference": info["Config"]["Image"],
            "platform": f"{image['Os']}/{image['Architecture']}",
        }
    return nodes


def expected_nodes(config):
    """Read only the reviewed kind node list; fail closed on unsupported config shapes."""
    text = config.read_text()
    roles = re.findall(r"^  - role: (control-plane|worker)$", text, re.M)
    pins = re.findall(r"^    image: (kindest/node:[^\s]+@sha256:[a-f0-9]{64})$", text, re.M)
    if not roles or len(roles) != len(pins) or len(set(pins)) != 1:
        raise ValueError("kind config must explicitly pin one node image digest for every node")
    try:
        image = json.loads(command(["docker", "image", "inspect", pins[0]]))[0]
    except CacheCommandError as error:
        raise ValueError(
            f"cache preflight needs the pinned node image locally; run docker pull {pins[0]}"
        ) from error
    architecture = command(["docker", "info", "--format", "{{.Architecture}}"]).strip()
    architecture = {"aarch64": "arm64", "x86_64": "amd64"}.get(architecture, architecture)
    if image["Architecture"] != architecture or image["Os"] != "linux":
        raise ValueError("node image architecture must match the native Linux Docker platform")
    nodes, counts = {}, {}
    for role in roles:
        counts[role] = counts.get(role, 0) + 1
        suffix = role + (str(counts[role]) if counts[role] > 1 else "")
        nodes[suffix] = {
            "node_image_id": image["Id"],
            "node_image_reference": pins[0],
            "platform": f"linux/{architecture}",
        }
    return nodes


def validate_cache(cache_dir, revision, nodes):
    manifest_path = cache_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1 or manifest.get("source_revision") != revision:
        raise ValueError(
            "cache source revision/schema mismatch; export a new cache from this revision"
        )
    if set(manifest.get("nodes", {})) != set(nodes):
        raise ValueError("cache node topology differs from the new kind cluster")
    for suffix, node in nodes.items():
        cached = manifest["nodes"][suffix]
        for field in ("node_image_id", "node_image_reference", "platform"):
            if cached.get(field) != node[field]:
                raise ValueError(f"cache {suffix} {field} mismatch; export a compatible cache")
        archive = cache_dir / f"{suffix}.tar"
        if archive.is_symlink() or not archive.is_file():
            raise ValueError(f"missing regular cache archive for {suffix}")
        if archive.stat().st_size != cached.get("archive_bytes") or digest(archive) != cached.get(
            "archive_sha256"
        ):
            raise ValueError(f"cache checksum mismatch for {suffix}; nothing has been imported")
        images = cached.get("images", {})
        if not images or any(
            not re.fullmatch(r"sha256:[a-f0-9]{64}", value) for value in images.values()
        ):
            raise ValueError(f"cache image identities missing or invalid for {suffix}")
        if any(
            ref.startswith(("sha256:", "import-"))
            or ref.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0] in APPS
            for ref in images
        ):
            raise ValueError(
                "cache contains a local app image; current source must be built separately"
            )
    return manifest


def export_cache(cache_dir, revision, nodes):
    if cache_dir.exists() and any(cache_dir.iterdir()):
        raise ValueError("cache directory must be empty; existing archives are never overwritten")
    for suffix, node in nodes.items():
        config = tomllib.loads(
            command(
                ["docker", "exec", node["container"], "cat", "/etc/containerd/config.toml"],
                stage="check-content-retention",
                node=suffix,
            )
        )
        plugins = config.get("plugins", {})
        settings = plugins.get("io.containerd.cri.v1.images")
        if settings is None:
            settings = plugins.get("io.containerd.grpc.v1.cri", {}).get("containerd", {})
        if settings.get("discard_unpacked_layers") is not False:
            raise ValueError(
                f"{suffix}: compressed image layers are not retained. Create a fresh cluster "
                "with discard_unpacked_layers=false before pulling images; use dev-acceptance.py "
                "for a matched benchmark. Changing an existing node cannot restore discarded blobs."
            )
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "source_revision": revision,
        "created_utc": datetime.now(UTC).isoformat(),
        "scope": "per-node platform OCI images; excludes local app images and all workload data",
        "nodes": {},
    }
    for suffix, node in nodes.items():
        images = node_inventory(node["container"])
        if not images:
            raise ValueError("no platform images to cache")
        archive = cache_dir / f"{suffix}.tar"
        temporary = archive.with_suffix(".partial")
        hydration = {}
        try:
            hydration = hydrate_image_content(node, images)
            with temporary.open("xb") as stream:
                run_command(
                    export_command(node, sorted(images)),
                    stage="export-archive",
                    node=suffix,
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    timeout=1200,
                )
            temporary.replace(archive)
        except CacheCommandError as error:
            error.diagnostics.update(
                {
                    "completed_nodes": manifest["nodes"],
                    "failed_node_images": images,
                    "error": classify_command_error(error),
                }
            )
            if error.stage == "export-archive":
                error.diagnostics["content_hydration"] = hydration
                try:
                    error.diagnostics["isolation"] = probe_export_failure(node, images)
                except (OSError, ValueError, subprocess.SubprocessError) as diagnostic_error:
                    error.diagnostics["isolation_error_type"] = type(diagnostic_error).__name__
            raise
        finally:
            temporary.unlink(missing_ok=True)
        manifest["nodes"][suffix] = {
            **{key: value for key, value in node.items() if key != "container"},
            "images": images,
            "content_hydration": hydration,
            "archive_bytes": archive.stat().st_size,
            "archive_sha256": digest(archive),
        }
    # A failed/incomplete export has no manifest and therefore cannot be restored.
    (cache_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def import_cache(cache_dir, revision, nodes):
    # Check every archive before importing any, so corruption fails without a partial restore.
    manifest = validate_cache(cache_dir, revision, nodes)
    for suffix, node in nodes.items():
        with (cache_dir / f"{suffix}.tar").open("rb") as stream:
            run_command(
                [
                    "docker",
                    "exec",
                    "-i",
                    node["container"],
                    "ctr",
                    "--namespace",
                    "k8s.io",
                    "images",
                    "import",
                    "--local",
                    "--platform",
                    node["platform"],
                    "--label",
                    "io.cri-containerd.image=managed",
                    "-",
                ],
                stage="import-archive",
                node=suffix,
                stdin=stream,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=1200,
            )
        actual = node_inventory(node["container"])
        expected = manifest["nodes"][suffix]["images"]
        if any(actual.get(reference) != identity for reference, identity in expected.items()):
            raise ValueError(f"restored image identities differ for {suffix}")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["validate", "export", "import"])
    parser.add_argument("--cluster")
    parser.add_argument("--kind-config", type=Path)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--diagnostic-log", type=Path, help="Private stderr file; never upload")
    args = parser.parse_args()
    if args.operation == "validate" and not args.kind_config:
        parser.error("validate requires --kind-config and never changes a cluster")
    if args.operation != "validate" and not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]*", args.cluster or ""
    ):
        parser.error("cluster must be an explicit valid kind cluster name")
    if not re.fullmatch(r"[a-f0-9]{40}", args.source_revision):
        parser.error("source-revision must be a full commit SHA")
    started = time.monotonic()
    report = {
        "operation": args.operation,
        "cluster": args.cluster,
        "source_revision": args.source_revision,
        "success": False,
    }
    try:
        if args.operation == "validate":
            manifest = validate_cache(
                args.cache_dir, args.source_revision, expected_nodes(args.kind_config)
            )
        else:
            nodes = nodes_for(args.cluster)
            manifest = (export_cache if args.operation == "export" else import_cache)(
                args.cache_dir, args.source_revision, nodes
            )
        report.update(
            success=True,
            manifest_sha256=digest(args.cache_dir / "manifest.json"),
            nodes=manifest["nodes"],
        )
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, CacheCommandError) as error:
        # Captured command stderr can contain registry credentials; retain only safe error types.
        report["error"] = str(error) if isinstance(error, ValueError) else type(error).__name__
        if isinstance(error, CacheCommandError):
            report.update(
                stage=error.stage,
                node=error.node,
                exit_code=error.exit_code,
                failure_details=classify_command_error(error),
                diagnostics=error.diagnostics,
            )
            if args.diagnostic_log:
                args.diagnostic_log.parent.mkdir(parents=True, exist_ok=True)
                diagnostic = error.stderr
                if isinstance(diagnostic, str):
                    diagnostic = diagnostic.encode()
                with args.diagnostic_log.open("xb") as stream:
                    stream.write(diagnostic)
                args.diagnostic_log.chmod(0o600)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "nodes"}))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
