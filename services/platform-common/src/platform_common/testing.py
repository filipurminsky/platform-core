"""In-memory fakes for unit-testing workers without Kafka / Redis / S3."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from platform_common import dedup, job_state


class FakeRedis:
    """Just enough Redis for job_state + dedup: SET NX/EX, GET, and both Lua scripts."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttl[key] = ex
        return True

    def get(self, key):
        return self.store.get(key)

    def eval(self, script, _numkeys, key, *args):
        if script == job_state.MERGE_LUA:
            update_json, ttl, now = args
            update = json.loads(update_json)
            cur = json.loads(self.store[key]) if key in self.store else {}
            if isinstance(update.get("keys"), dict):
                cur["keys"] = {**cur.get("keys", {}), **update.pop("keys")}
            cur.update(update)
            cur["updated_at"] = now
            self.store[key] = json.dumps(cur)
            self.ttl[key] = int(ttl)
            return 1
        if script == dedup._RELEASE_LUA:
            (token,) = args
            if self.store.get(key) == token:
                del self.store[key]
                return 1
            return 0
        raise AssertionError("unexpected Lua script")

    def state(self, job_id: str) -> dict | None:
        raw = self.store.get(f"job:{job_id}")
        return json.loads(raw) if raw else None


class FakeProducer:
    """Records produced messages; topics in ``fail_topics`` report a delivery error."""

    def __init__(self, fail_topics=()):
        self.produced: list[dict] = []
        self.fail_topics = set(fail_topics)

    def produce(self, topic, value=None, key=None, headers=None, on_delivery=None):
        self.produced.append({"topic": topic, "value": value, "key": key, "headers": headers})
        if on_delivery is not None:
            on_delivery("broker unavailable" if topic in self.fail_topics else None, None)

    def flush(self, timeout=None):
        return 0

    def on(self, topic: str) -> list[dict]:
        return [json.loads(m["value"]) for m in self.produced if m["topic"] == topic]


class FakeS3:
    """In-memory S3 whose get_object raises a real botocore NoSuchKey ClientError."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects: dict[str, bytes] = dict(objects or {})
        self.puts: list[dict] = []

    def get_object(self, Bucket, Key):  # noqa: N803
        from botocore.exceptions import ClientError

        if Key not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey", "Message": "not found"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "GetObject",
            )
        return {"Body": MagicMock(read=MagicMock(return_value=self.objects[Key]))}

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        data = Body if isinstance(Body, bytes) else Body.encode()
        self.objects[Key] = data
        self.puts.append({"Key": Key, "Body": data, "ContentType": ContentType})
        return {}
