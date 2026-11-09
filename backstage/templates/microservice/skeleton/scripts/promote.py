"""Update one overlay after the delivery workflow verifies the image signature."""

import os
import re
from pathlib import Path


def promote(environment: str, image: str, digest: str) -> None:
    if environment not in {"dev", "prod"}:
        raise ValueError("Environment must be dev or prod")
    expected_image = "ghcr.io/" + os.environ["GITHUB_REPOSITORY"].lower()
    if image != expected_image or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("Expected this repository's image and an immutable sha256 digest")
    path = Path("k8s/overlays") / environment / "kustomization.yaml"
    source = path.read_text()
    if f"name: {image}\n" not in source:
        raise ValueError("Overlay image does not match the release repository")
    source, count = re.subn(r"digest: sha256:[a-f0-9]{64}", f"digest: {digest}", source)
    if count != 1:
        raise ValueError("Expected exactly one image digest")
    source, count = re.subn(r"count: \d+", f"count: {1 if environment == 'dev' else 2}", source)
    if count != 1:
        raise ValueError("Expected exactly one replica setting")
    path.write_text(source)


if __name__ == "__main__":
    promote(os.environ["TARGET_ENVIRONMENT"], os.environ["IMAGE"], os.environ["DIGEST"])
