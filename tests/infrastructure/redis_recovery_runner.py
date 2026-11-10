"""Host orchestration; never mounts the Docker socket in test containers.

Called only by scripts/test-infrastructure.sh with its unique Compose project.
The runner's EXIT trap owns cleanup on success, failure, or interruption.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

project = sys.argv[1]
assert project.startswith("platform-core-infra-")
evidence = Path(os.environ["INFRA_EVIDENCE_DIR"])
compose = ["docker", "compose", "-p", project, "-f", "tests/infrastructure/compose.yaml"]


def run(*args, capture=False):
    return subprocess.run(
        [*compose, *args], check=True, text=True, capture_output=capture, timeout=120
    ).stdout


def client(phase):
    run(
        "run",
        "--rm",
        "--no-deps",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--entrypoint",
        "python",
        "tests",
        "/tests/redis_recovery.py",
        phase,
    )


def container(service):
    identifier = run("ps", "-q", service, capture=True).strip()
    assert identifier, f"Missing {service} container"
    # Inspect only mount metadata; never serialize credentials from Config.Env.
    mounts = json.loads(
        subprocess.check_output(
            ["docker", "inspect", "--format", "{{json .Mounts}}", identifier], text=True, timeout=15
        )
    )
    volume = next(m["Name"] for m in mounts if m["Destination"] == "/data")
    return {"id": identifier, "volume": volume}


client("seed")
before = container("redis")
started = time.monotonic()
run("kill", "--signal", "SIGKILL", "redis")
run("up", "-d", "--no-deps", "--force-recreate", "--wait", "--wait-timeout", "60", "redis")
after = container("redis")
assert before["id"] != after["id"], "must replace the container, not just restart it"
assert before["volume"] == after["volume"], "replacement must reuse the original data volume"
client("verify-replacement")
replacement_seconds = time.monotonic() - started
client("backup")
run("cp", "redis:/data/dump.rdb", str(evidence / "redis-backup.rdb"))
started = time.monotonic()
run(
    "--profile",
    "recovery",
    "up",
    "-d",
    "--no-deps",
    "--wait",
    "--wait-timeout",
    "60",
    "redis-restore",
)
restored = container("redis-restore")
assert restored["volume"] != after["volume"], "backup must restore onto a separate empty volume"
client("verify-restore")
restore_seconds = time.monotonic() - started
started = time.monotonic()
run("--profile", "recovery", "kill", "--signal", "SIGKILL", "redis-restore")
run(
    "--profile",
    "recovery",
    "up",
    "-d",
    "--no-deps",
    "--force-recreate",
    "--wait",
    "--wait-timeout",
    "60",
    "redis-restore",
)
restored_again = container("redis-restore")
assert restored_again["id"] != restored["id"]
assert restored_again["volume"] == restored["volume"]
client("verify-restored-restart")
restored_restart_seconds = time.monotonic() - started
report = {
    "passed": True,
    "replacement_seconds_until_state_verified": round(replacement_seconds, 3),
    "restore_seconds_until_state_verified": round(restore_seconds, 3),
    "restored_aof_replacement_seconds_until_state_verified": round(restored_restart_seconds, 3),
    "source_container_replaced": True,
    "replacement_reused_volume": True,
    "restore_used_separate_volume": True,
    "backup_sha256": hashlib.sha256((evidence / "redis-backup.rdb").read_bytes()).hexdigest(),
    "verified": [
        "completed job",
        "completed dedup marker",
        "in-progress job",
        "processing lease",
        "remaining TTLs",
        "post-backup write excluded",
        "restored AOF survives forced container replacement including post-restore write",
    ],
    "limits": [
        "single instance; outage during replacement",
        "fsync-confirmed writes only",
        "synthetic small RDB backup",
        "no node/PVC loss or HA/failover proof",
        "backup RPO is snapshot time",
    ],
}
(evidence / "redis-recovery.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
