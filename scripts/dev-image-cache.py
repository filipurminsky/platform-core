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


def command(args, **kwargs):
    return subprocess.run(
        args, check=True, capture_output=True, text=True, timeout=60, **kwargs
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
        if reference.startswith("sha256:") or name in APPS:
            continue
        images[reference] = fields[2]
    return images


def node_inventory(node):
    return inventory(
        command(["docker", "exec", node, "ctr", "--namespace", "k8s.io", "images", "list"])
    )


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
    except subprocess.CalledProcessError as error:
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
        if any(ref.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0] in APPS for ref in images):
            raise ValueError(
                "cache contains a local app image; current source must be built separately"
            )
    return manifest


def export_cache(cache_dir, revision, nodes):
    if cache_dir.exists() and any(cache_dir.iterdir()):
        raise ValueError("cache directory must be empty; existing archives are never overwritten")
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
        try:
            with temporary.open("xb") as stream:
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        node["container"],
                        "ctr",
                        "--namespace",
                        "k8s.io",
                        "images",
                        "export",
                        "--local",
                        "--platform",
                        node["platform"],
                        "-",
                        *sorted(images),
                    ],
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    check=True,
                    timeout=1200,
                )
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
        manifest["nodes"][suffix] = {
            **{key: value for key, value in node.items() if key != "container"},
            "images": images,
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
            subprocess.run(
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
                stdin=stream,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
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
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        # Captured command stderr can contain registry credentials; retain only safe error types.
        report["error"] = str(error) if isinstance(error, ValueError) else type(error).__name__
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "nodes"}))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
