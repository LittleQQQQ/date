"""每個 IP 的查詢限流（規格書 5.5）：REST 和 MCP 共用 rl:api:read:{IP}，每 60 秒 60 次。

送出規劃的限流（每小時 10 次、同時 1 個）在 plan_stream.reserve()。
"""
from __future__ import annotations

import math

from .config import settings
from .redis_utils.ratelimit import SlidingWindow

READ_WINDOW_S = 60


def read_retry_after(ip: str) -> int | None:
    """記一次查詢。沒超過回傳 None；超過回傳幾秒後再試。"""
    decision = SlidingWindow(f"rl:api:read:{ip}", settings.api_read_limit_per_minute, READ_WINDOW_S).try_acquire()
    return None if decision.allowed else max(1, math.ceil(decision.retry_after_ms / 1000))
