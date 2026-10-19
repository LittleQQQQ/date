"""滑動窗口限流（規格書 5.5）。

一個 Lua script 原子地完成：ZREMRANGEBYSCORE 移除窗口外的紀錄 → ZCARD 計數 → 沒超過就 ZADD → PEXPIRE。
多個程式實例共用同一個 key，所以 TDX 每分鐘 5 次是所有實例加起來 5 次。

| 對象 | key | 窗口 | 上限 |
| TDX（所有 API 共用） | rl:tdx | 60 秒 | 5 |
| Jev | rl:jev | 60 秒 | 300 |
| 建立規劃（每個 IP） | rl:api:plan:{IP} | 1 小時 | 10 |
| 其他查詢（每個 IP） | rl:api:read:{IP} | 60 秒 | 60 |
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import redis

from . import get_redis

_SLIDING_WINDOW = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', tonumber(ARGV[1]) - tonumber(ARGV[2]))
local count = redis.call('ZCARD', KEYS[1])
if count < tonumber(ARGV[3]) then
  redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return {1, 0}
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return {0, tonumber(oldest[2]) + tonumber(ARGV[2]) - tonumber(ARGV[1])}
"""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_ms: int  # 不允許時，最早幾毫秒後再試會有空位


class RateLimitTimeout(Exception):
    """等太久還是沒有空位。"""


class SlidingWindow:
    def __init__(self, key: str, limit: int, window_s: float, client: redis.Redis | None = None,
                 clock: Callable[[], float] = time.time):
        self.key = key
        self.limit = limit
        self.window_ms = int(window_s * 1000)
        self.client = client or get_redis()
        self.clock = clock

    def try_acquire(self) -> Decision:
        now_ms = int(self.clock() * 1000)
        member = f"{now_ms}-{uuid.uuid4().hex}"
        allowed, retry = self.client.eval(_SLIDING_WINDOW, 1, self.key, now_ms, self.window_ms, self.limit, member)
        return Decision(bool(allowed), max(0, int(retry)))

    def wait(self, timeout_s: float = 600, sleep: Callable[[float], None] = time.sleep,
             on_wait: Callable[[float], None] | None = None) -> None:
        """等到有空位再回傳（TDX、Jev 用：碰到上限時排隊等，規格書 5.5）。on_wait(秒) 可以用來推送「等待額度」。"""
        deadline = self.clock() + timeout_s
        while True:
            decision = self.try_acquire()
            if decision.allowed:
                return
            wait_s = decision.retry_after_ms / 1000 + 0.05
            if self.clock() + wait_s > deadline:
                raise RateLimitTimeout(f"{self.key} 等了 {timeout_s:.0f} 秒還是沒有空位")
            if on_wait is not None:
                on_wait(wait_s)
            sleep(wait_s)


def tdx_limiter(client: redis.Redis | None = None) -> SlidingWindow:
    return SlidingWindow("rl:tdx", 5, 60, client)
