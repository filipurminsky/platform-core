#!/usr/bin/env python3
"""Verify that local build inputs match the published GitOps branch before setup."""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUTS = (
    "scripts",
    "services",
    "kubernetes",
    "helm",
    "backstage",
    "observability",
    "terraform/modules/kind",
)


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True, timeout=60
    ).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, default=ROOT)
    root = parser.parse_args().checkout
    manifest = (root / "kubernetes/platform/argocd/app-of-apps.yaml").read_text()
    repo = re.search(r"^\s+repoURL: (https://github.com/[^\s]+)\s*$", manifest, re.MULTILINE)
    revision = re.search(r"^\s+targetRevision: ([\w./-]+)\s*$", manifest, re.MULTILINE)
    if not repo or not revision or revision[1] != "main":
        raise ValueError(
            "Expected the published GitHub main source in app-of-apps.yaml; review fork/revision configuration explicitly."
        )
    dirty = git(root, "status", "--porcelain", "--untracked-files=normal", "--", *INPUTS)
    if dirty:
        raise ValueError(
            "Deployment/build inputs have local changes. Publish the intended revision and use a clean checkout for --require-published-source. Normal bootstrap can still be used for development."
        )
    local = git(root, "rev-parse", "HEAD")
    remote = git(root, "ls-remote", repo[1], "refs/heads/main").split()
    if not remote:
        raise ValueError(
            "Published main could not be resolved; check repository access and connectivity."
        )
    print(f"Checkout: {local}\nGitOps: {repo[1]} refs/heads/main -> {remote[0]}")
    if local != remote[0]:
        raise ValueError(
            "Checkout differs from published main. Check out that published revision before building images for this walkthrough."
        )
    print(
        "Published source verified. Child Applications track main; this records the starting revision, not an immutable fleet pin."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"DEV source check failed: {error}", file=sys.stderr)
        sys.exit(1)
