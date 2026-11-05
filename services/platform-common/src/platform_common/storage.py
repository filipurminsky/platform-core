"""S3 client construction (contract §4). MinIO/SeaweedFS in dev, AWS S3 (IRSA) in prod."""

from __future__ import annotations

import boto3
from botocore.config import Config


def make_s3_client(
    *, region: str, endpoint_url: str = "", connect_timeout: float = 5.0, read_timeout: float = 60.0
):
    # botocore's own retries stay small ("standard", 3 attempts): our retry
    # layer owns the back-off so the per-message budget remains predictable.
    cfg = Config(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        retries={"mode": "standard", "max_attempts": 3},
    )
    kwargs: dict = {"region_name": region}
    if endpoint_url:
        # S3-compatible dev store: path-style addressing required.
        kwargs["endpoint_url"] = endpoint_url
        cfg = cfg.merge(Config(s3={"addressing_style": "path"}))
    kwargs["config"] = cfg
    return boto3.client("s3", **kwargs)
