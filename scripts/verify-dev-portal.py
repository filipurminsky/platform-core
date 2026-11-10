#!/usr/bin/env python3
"""Read-only DEV catalog/source acceptance and remote scaffolder fetch dry-run."""

import argparse
import copy
import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TEMPLATES = {"llm-service", "microservice", "platform-app"}
EXPECTED_FILES = {
    "registration/applicationset.yaml",
    *(
        "service/" + name
        for name in (
            ".dockerignore",
            ".gitignore",
            ".python-version",
            "Dockerfile",
            "README.md",
            "catalog-info.yaml",
            "mkdocs.yml",
            "pyproject.toml",
            "test_main.py",
            "uv.lock",
            "app/main.py",
            "docs/index.md",
            "scripts/promote.py",
            ".github/workflows/delivery.yaml",
            ".github/actions/setup-trivy/action.yaml",
            "k8s/base/deployment.yaml",
            "k8s/base/kustomization.yaml",
            "k8s/base/monitor.yaml",
            "k8s/base/network-policy.yaml",
            "k8s/base/pdb.yaml",
            "k8s/base/route.yaml",
            "k8s/base/service.yaml",
            "k8s/overlays/dev/kustomization.yaml",
            "k8s/overlays/prod/kustomization.yaml",
        )
    ),
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        # Never forward a guest token outside the explicitly selected endpoint.
        return None


def fetch_template(entity, repository):
    """Construct only supported read-only actions; no publishing/registration."""
    template = copy.deepcopy(entity)
    steps = [s for s in template["spec"]["steps"] if s["action"] == "fetch:template"]
    if len(steps) != 2 or {s["id"] for s in steps} != {"fetch", "registration-files"}:
        raise ValueError("Microservice template must contain the two expected fetch actions")
    for step in steps:
        directory = "skeleton" if step["id"] == "fetch" else "registration"
        step["input"]["url"] = (
            repository.removesuffix(".git")
            + "/tree/main/backstage/templates/microservice/"
            + directory
        )
    template["spec"]["steps"] = steps
    template["spec"]["output"] = {}
    owner, repo = urllib.parse.urlparse(repository).path.strip("/").removesuffix(".git").split("/")
    return {
        "template": template,
        "values": {
            "name": "acceptance-reader",
            "description": "Read-only remote template acceptance",
            "owner": "group:default/platform-team",
            "repoUrl": "github.com?owner=acceptance-owner&repo=acceptance-reader",
            "platformRepoUrl": f"github.com?owner={owner}&repo={repo}",
        },
        "directoryContents": [],
    }


def source_failures(apps, repository, revision):
    failures, sources = [], []
    for app in apps:
        sync = app.get("status", {}).get("sync", {})
        spec = app["spec"]
        revisions = sync.get("revisions", [sync.get("revision")])
        for index, source in enumerate(spec.get("sources", [spec.get("source", {})])):
            if "path" not in source:
                continue
            observed = revisions[index] if index < len(revisions) else None
            name = app["metadata"]["name"]
            sources.append({"application": name, "observed_revision": observed})
            if source.get("repoURL", "").removesuffix(".git") != repository.removesuffix(".git"):
                failures.append(f"{name}: unexpected Git repository")
            if observed != revision:
                failures.append(f"{name}: observed revision differs from clean checkout")
    if not sources:
        failures.append("No first-party Git sources were discovered")
    return failures, sources


def verify(args):
    def kubectl(*arguments):
        p = subprocess.run(
            [
                "kubectl",
                "--context",
                args.context,
                "--request-timeout=15s",
                *arguments,
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if p.returncode:
            raise RuntimeError("Portal resource inspection failed; check context and API access")
        return json.loads(p.stdout)

    headers = {
        "Host": "backstage.platform-core.local",
        "Origin": "http://backstage.platform-core.local",
        "X-Requested-With": "XMLHttpRequest",
    }
    opener = urllib.request.build_opener(NoRedirect)

    def request(path, data=None):
        request_headers = {**headers, "Content-Type": "application/json"}
        req = urllib.request.Request(
            args.gateway_url.rstrip("/") + path,
            headers=request_headers,
            data=json.dumps(data).encode() if data is not None else None,
        )
        with opener.open(req, timeout=120) as response:
            return json.load(response)

    apps = kubectl("-n", "argocd", "get", "applications")["items"]
    sets = kubectl("-n", "argocd", "get", "applicationsets")["items"]
    failures, sources = source_failures(apps, args.repository, args.expected_revision)
    for item in sets:
        if "host.docker.internal" in json.dumps(item["spec"]):
            failures.append("ApplicationSet depends on a temporary host server")
    config = kubectl("-n", "platform", "get", "configmap", "backstage-config")["data"][
        "app-config.local.yaml"
    ]
    public_templates = args.repository.removesuffix(".git") + "/blob/main/backstage/templates"
    if "host.docker.internal" in config or public_templates not in config:
        failures.append("Catalog is not configured for published GitHub templates")
    identity = request("/api/auth/guest/refresh")
    headers["Authorization"] = "Bearer " + identity["backstageIdentity"]["token"]
    entities = request("/api/catalog/entities")
    templates = {e["metadata"]["name"] for e in entities if e["kind"] == "Template"}
    if templates != TEMPLATES:
        failures.append("Unexpected catalog template inventory")
    for entity in entities:
        if any(s.get("level") == "error" for s in entity.get("status", {}).get("items", [])):
            failures.append("Catalog entity has an error status")
        if "host.docker.internal" in json.dumps(entity["metadata"].get("annotations", {})):
            failures.append("Catalog entity references a temporary host server")
    entity = request("/api/catalog/entities/by-name/template/default/microservice")
    started = time.monotonic()
    result = request("/api/scaffolder/v2/dry-run", fetch_template(entity, args.repository))
    paths = [f["path"] for f in result.get("directoryContents", [])]
    if set(paths) != EXPECTED_FILES or len(paths) != len(EXPECTED_FILES):
        failures.append("Remote template output differs from the expected 25-file golden path")
    return {
        "passed": not failures,
        "failures": failures,
        "context": args.context,
        "expected_revision": args.expected_revision,
        "application_count": len(apps),
        "git_sources": sources,
        "applicationset_count": len(sets),
        "catalog_entity_count": len(entities),
        "catalog_templates": sorted(templates),
        "guest_auth": "passed; token held in memory only",
        "external_writes": False,
        "dry_run_actions": ["fetch:template", "fetch:template"],
        "generated_file_count": len(paths),
        "dry_run_seconds": round(time.monotonic() - started, 3),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if urllib.parse.urlparse(args.gateway_url).scheme not in {"http", "https"}:
        parser.error("gateway-url must use HTTP(S)")
    try:
        result = verify(args)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        # Never persist HTTP bodies, tokens, or a raw ConfigMap on failure.
        result = {"passed": False, "error_type": type(error).__name__}
        if isinstance(error, urllib.error.HTTPError):
            result["http_status"] = error.code
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
