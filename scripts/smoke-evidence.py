#!/usr/bin/env python3
"""Strict evidence checks for the dev smoke test; infrastructure errors cannot pass."""

import json
import sys
from pathlib import Path


def records(path):
    for line in path.read_text().splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


def verify(mode, directory, job_id):
    if mode == "processed":
        rows = [
            line.split()
            for line in (directory / "offsets.txt").read_text().splitlines()
            if line.startswith("worker-service ") or line.startswith("worker-service\t")
        ]
        rows = [row for row in rows if len(row) >= 6 and row[1] == "jobs"]
        return (
            bool(rows)
            and all(row[5] == "0" for row in rows)
            and any(
                row.get("event") == "job_processed"
                and row.get("job_id") == job_id
                and row.get("result", {}).get("pong") is True
                for row in records(directory / "worker.log")
            )
        )
    if mode == "dlq":
        return any(
            isinstance(row.get("original_job"), dict)
            and row["original_job"].get("id") == job_id
            and row.get("error")
            for row in records(directory / "dlq.jsonl")
        )
    if mode == "completed":
        row = json.loads((directory / "job.json").read_text())
        keys = row.get("keys", {})
        return row.get("status") == "done" and all(
            keys.get(key) == f"{prefix}/{job_id}"
            for key, prefix in (
                ("audio", "audio"),
                ("transcript", "transcripts"),
                ("summary", "summaries"),
                ("speech", "speech"),
            )
        )
    raise ValueError(f"Unknown mode: {mode}")


if __name__ == "__main__":
    sys.exit(0 if verify(sys.argv[1], Path(sys.argv[2]), sys.argv[3]) else 1)
