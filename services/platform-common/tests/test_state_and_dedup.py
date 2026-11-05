from platform_common.dedup import Claim, Deduper
from platform_common.job_state import merge_job_state


def test_merge_accumulates_keys(fake_redis):
    assert merge_job_state(fake_redis, "j", {"status": "queued", "keys": {"audio": "a"}}, 60)
    merge_job_state(fake_redis, "j", {"status": "transcribing", "keys": {"transcript": "t"}}, 60)
    st = fake_redis.state("j")
    assert st["status"] == "transcribing"
    assert st["keys"] == {"audio": "a", "transcript": "t"}
    assert fake_redis.ttl["job:j"] == 60


def test_merge_never_raises():
    class Broken:
        def eval(self, *a):
            raise ConnectionError("down")

    assert merge_job_state(Broken(), "j", {"status": "x"}, 60) is False
    assert merge_job_state(Broken(), "", {"status": "x"}, 60) is False


def test_claim_lifecycle(fake_redis):
    a = Deduper(fake_redis, "stt", lease_seconds=900, done_ttl_seconds=604800)
    b = Deduper(fake_redis, "stt", lease_seconds=900, done_ttl_seconds=604800)
    assert a.claim("j1") is Claim.CLAIMED
    assert fake_redis.ttl["dedup:stt:j1"] == 900
    assert b.claim("j1") is Claim.IN_PROGRESS  # atomic: second pod can't also start
    a.mark_done("j1")
    assert fake_redis.store["dedup:stt:j1"] == "done"
    assert fake_redis.ttl["dedup:stt:j1"] == 604800
    assert b.claim("j1") is Claim.ALREADY_DONE


def test_release_only_own_claim_and_legacy_value(fake_redis):
    a = Deduper(fake_redis, "llm", lease_seconds=10, done_ttl_seconds=100)
    b = Deduper(fake_redis, "llm", lease_seconds=10, done_ttl_seconds=100)
    assert a.claim("j") is Claim.CLAIMED
    b.release("j")  # b holds no token → no-op
    assert "dedup:llm:j" in fake_redis.store
    a.release("j")
    assert "dedup:llm:j" not in fake_redis.store
    assert b.claim("j") is Claim.CLAIMED
    fake_redis.store["dedup:llm:old"] = "1"
    assert a.claim("old") is Claim.ALREADY_DONE


def test_claim_fails_open():
    class Broken:
        def set(self, *a, **k):
            raise ConnectionError("down")

    assert Deduper(Broken(), "tts", lease_seconds=1, done_ttl_seconds=1).claim("j") is (
        Claim.UNAVAILABLE
    )
