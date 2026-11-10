"""Client-side assertions for the isolated Redis recovery drill (synthetic data only)."""

import json
import os
import sys
import time
import uuid
from pathlib import Path

import redis
from platform_common.dedup import Deduper
from platform_common.job_state import merge_job_state

EVIDENCE = Path("/evidence")
phase = sys.argv[1]
r = redis.Redis(
    host="redis-restore" if phase.startswith("verify-restore") else "redis",
    password=os.environ["REDIS_PASSWORD"],
    decode_responses=True,
    socket_timeout=5,
    socket_connect_timeout=5,
)
manifest = EVIDENCE / "redis-expected.json"
if phase == "seed":
    job_id = uuid.uuid4().hex
    assert merge_job_state(
        r,
        job_id,
        {"status": "done", "stage": "tts", "keys": {"speech": f"speech/{job_id}"}},
        604800,
    )
    deduper = Deduper(r, "tts", lease_seconds=300, done_ttl_seconds=604800)
    deduper.claim(job_id)
    deduper.mark_done(job_id)
    pending = uuid.uuid4().hex
    assert merge_job_state(r, pending, {"status": "transcribing", "stage": "stt"}, 604800)
    Deduper(r, "stt", lease_seconds=300, done_ttl_seconds=604800).claim(pending)
    keys = [f"job:{job_id}", f"dedup:tts:{job_id}", f"job:{pending}", f"dedup:stt:{pending}"]
    expected = {key: {"value": r.get(key), "ttl": r.ttl(key)} for key in keys}
    manifest.write_text(json.dumps({"at": time.time(), "keys": expected}, indent=2))
    # Define the durable boundary: don't claim everysec provides zero-loss writes.
    assert r.execute_command("WAITAOF", 1, 0, 5000)[0] == 1
elif phase == "backup":
    # Tiny synthetic dataset: synchronous SAVE makes the backup boundary explicit.
    assert r.save()
    r.set("recovery:after-backup", "must-not-exist-in-restore", ex=604800)
elif phase.startswith("verify-"):
    if phase == "verify-restore":
        assert r.config_set("appendonly", "yes")
        deadline = time.monotonic() + 30
        while r.info("persistence")["aof_rewrite_in_progress"]:
            assert time.monotonic() < deadline, "restored AOF rewrite did not finish"
            time.sleep(0.1)
        assert r.info("persistence")["aof_last_bgrewrite_status"] == "ok"
        r.set("recovery:restored-aof-only", "written-after-RDB-restore", ex=300)
        assert r.execute_command("WAITAOF", 1, 0, 5000)[0] == 1
    assert r.info("persistence")["aof_enabled"] == 1
    expected = json.loads(manifest.read_text())
    elapsed = time.time() - expected["at"]
    assert elapsed < 180, "fixture exceeded its recovery budget; short lease could expire"
    actual = {}
    for key, item in expected["keys"].items():
        value, ttl = r.get(key), r.ttl(key)
        assert value == item["value"], f"state changed or missing after {phase}: {key}"
        # TTL must continue counting down across restart/restore, not reset to seven days.
        assert item["ttl"] - elapsed - 3 <= ttl <= item["ttl"] - elapsed + 3, (key, ttl)
        actual[key] = {"matches": True, "remaining_ttl_seconds": ttl}
    if phase.startswith("verify-restore"):
        assert r.get("recovery:after-backup") is None, "restore must use the backup boundary"
        assert r.get("recovery:restored-aof-only") == "written-after-RDB-restore"
    (EVIDENCE / f"redis-{phase}.json").write_text(json.dumps(actual, indent=2))
else:
    raise SystemExit(f"Unknown recovery phase: {phase}")
print(f"Redis recovery {phase}: passed", flush=True)
r.close()
