"""S3 / MinIO client construction (§4)."""

import boto3
from botocore.config import Config

from app import config


def make_s3_client():
    kwargs: dict = {"region_name": config.S3_REGION}
    if config.S3_ENDPOINT_URL:
        # MinIO (dev): path-style addressing required
        kwargs["endpoint_url"] = config.S3_ENDPOINT_URL
        kwargs["config"] = Config(s3={"addressing_style": "path"})
    return boto3.client("s3", **kwargs)
