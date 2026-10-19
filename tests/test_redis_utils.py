"""Redis 共用元件（規格書 5.3～5.5）：鎖的取得、延長、釋放；滑動窗口的邊界；每月額度；快取；TDX token 共用。"""
import datetime as dt
import time

import pytest

from app.collectors.tdx import TOKEN_KEY, TOKEN_LOCK, shared_token
from app.redis_utils.cache import get_json, route_key, route_slot, set_json
from app.redis_utils.lock import RedisLock
from app.redis_utils.quota import MonthlyQuota
from app.redis_utils.ratelimit import RateLimitTimeout, SlidingWindow
from app.timeutil import TW

pytestmark = pytest.mark.usefixtures("redis_client")


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# ---------- 鎖 ----------


def test_lock_is_exclusive_and_only_the_owner_can_release(redis_client):
    mine, theirs = RedisLock("lock:job:refresh_daily", 60_000), RedisLock("lock:job:refresh_daily", 60_000)
    assert mine.acquire()
    assert not theirs.acquire()  # 沒搶到：排程工作直接跳過
    assert not theirs.release() and not theirs.extend()  # 值不同，刪不到也延長不了別人的鎖
    assert mine.release()
    assert theirs.acquire()


def test_lock_expires_and_extend_refreshes_it(redis_client):
    lock = RedisLock("lock:job:x", 200)
    assert lock.acquire()
    assert lock.extend() and redis_client.pttl("lock:job:x") > 150
    time.sleep(0.3)
    assert not lock.extend()  # 已經過期：不是自己的鎖了


def test_heartbeat_keeps_a_long_job_locked(redis_client):
    lock = RedisLock("lock:job:long", 300)
    assert lock.acquire()
    lock.start_heartbeat(0.1)
    time.sleep(0.7)  # 比鎖的有效時間久
    assert redis_client.get("lock:job:long") == lock.token
    assert lock.release() and redis_client.get("lock:job:long") is None


# ---------- 滑動窗口 ----------


def test_sliding_window_boundary():
    clock = FakeClock()
    window = SlidingWindow("rl:tdx", 5, 60, clock=clock)
    for _ in range(5):
        assert window.try_acquire().allowed
        clock.sleep(1)
    denied = window.try_acquire()  # 第 6 次，距離第 1 次 5 秒
    assert not denied.allowed and denied.retry_after_ms == 55_000
    clock.sleep(54.9)
    assert not window.try_acquire().allowed  # 第 1 次還在窗口裡
    clock.sleep(0.2)
    assert window.try_acquire().allowed  # 第 1 次滑出窗口，空出一個位置


def test_sliding_window_is_shared_by_every_instance():
    clock = FakeClock()
    a, b = SlidingWindow("rl:jev", 2, 60, clock=clock), SlidingWindow("rl:jev", 2, 60, clock=clock)
    assert a.try_acquire().allowed and b.try_acquire().allowed
    assert not a.try_acquire().allowed and not b.try_acquire().allowed


def test_wait_queues_until_there_is_room():
    clock = FakeClock()
    window = SlidingWindow("rl:tdx", 1, 60, clock=clock)
    waited = []
    window.wait(sleep=clock.sleep)
    window.wait(sleep=clock.sleep, on_wait=waited.append)  # 第 2 次要等到第 1 次滑出窗口
    assert len(waited) == 1 and 60 <= waited[0] < 61
    with pytest.raises(RateLimitTimeout):
        window.wait(timeout_s=10, sleep=clock.sleep)


# ---------- 每月額度 ----------


def test_monthly_quota_counts_per_calendar_month(redis_client):
    now = dt.datetime(2026, 12, 20, 10, 0, tzinfo=TW)
    quota = MonthlyQuota("google:details_enterprise", 950, clock=lambda: now)
    assert quota.key == "quota:google:details_enterprise:2026-12"
    assert quota.consume(5) == 5 and quota.remaining() == 945
    assert quota.available(945) and not quota.available(946)
    expires = dt.datetime.fromtimestamp(redis_client.expiretime(quota.key), TW)
    assert expires == dt.datetime(2027, 1, 5, tzinfo=TW)  # 下個月 5 日（跨年）
    next_month = MonthlyQuota("google:details_enterprise", 950, clock=lambda: now + dt.timedelta(days=15))
    assert next_month.used() == 0  # 新的月份重新計算


# ---------- 快取 ----------


def test_route_slot_rounds_up_to_15_minutes():
    assert route_slot(18 * 60 + 7) == 18 * 60 + 15  # 18:07 → 18:15，不會錯過車
    assert route_slot(18 * 60) == 18 * 60
    assert route_key(42, "loc12", "cin3", 18 * 60 + 7) == "route:v1:42:loc12:cin3:1095"


def test_json_cache_round_trip(redis_client):
    assert set_json("route:v1:1:a:b:600", {"legs": ["公車（170）"], "fare": 18}, ttl_s=60)
    assert get_json("route:v1:1:a:b:600") == {"legs": ["公車（170）"], "fare": 18}
    assert 0 < redis_client.ttl("route:v1:1:a:b:600") <= 60
    assert get_json("route:v1:1:a:b:615") is None


# ---------- TDX token 共用（A1） ----------


def test_token_is_requested_once_and_shared(redis_client):
    calls = []

    def request():
        calls.append(1)
        return "token-1", 86400

    assert shared_token(request, redis_client) == "token-1"
    assert shared_token(request, redis_client) == "token-1"
    assert len(calls) == 1  # 第二次直接讀 Redis
    assert 86400 - 300 - 5 <= redis_client.ttl(TOKEN_KEY) <= 86400 - 300  # 比有效期限早 5 分鐘過期


def test_waits_while_another_instance_refreshes_the_token(redis_client):
    RedisLock(TOKEN_LOCK, 30_000, redis_client).acquire()  # 另一個實例正在換 token

    def sleep(_):
        redis_client.set(TOKEN_KEY, "token-from-other")  # 等的這 1 秒裡，對方換好寫回了

    def request():
        raise AssertionError("沒搶到鎖的一方不能自己去換")

    assert shared_token(request, redis_client, sleep=sleep) == "token-from-other"
