"""Redis 共用元件（規格書第 5 節）：分散式鎖、滑動窗口限流、每月額度、快取。

所有 key 的命名見規格書 5.2。Google 的店家資料一律不放進 Redis（條款，規格書 3.3）。
"""
from __future__ import annotations

from functools import lru_cache

import redis

from ..config import settings


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    """同一個程序共用一個連線池。回傳的值是字串（decode_responses）。"""
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)
