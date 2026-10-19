"""每月額度計數（規格書 5.5）：照日曆月（台灣時間）計數，跟帳單一致。

key 是 quota:{對象}:{YYYY-MM}，例如 quota:google:details_enterprise:2026-10，下個月 5 日自動過期。
呼叫外部服務前先用 available() 檢查，呼叫成功後再 consume()；Redis 重開最多少記最近 60 秒，靠 5% 邊際吸收。

| 對象 | 上限 | 用完時 |
| tdx_points | 9,000 單位 | 停止地理編碼和班表更新 |
| google:nearby_pro | 4,750 | 暫停找新餐廳 |
| google:details_enterprise | 950 | 規劃改用 OSM 餐廳 |
| google:details_essentials | 9,500 | 暫停更新座標 |
| jev_cost_micro_usd | 1,000,000（1 美元） | 暫停 Jev，全部交給 LLM 備援 |
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import redis

from ..timeutil import TW, now_tw
from . import get_redis

EXPIRE_DAY = 5  # 下個月 5 日才過期，月初幾天還查得到上個月用了多少


class MonthlyQuota:
    def __init__(self, name: str, limit: int, client: redis.Redis | None = None,
                 clock: Callable[[], dt.datetime] = now_tw):
        self.name = name
        self.limit = limit
        self.client = client or get_redis()
        self.clock = clock

    @property
    def key(self) -> str:
        return f"quota:{self.name}:{self.clock():%Y-%m}"

    def used(self) -> int:
        return int(self.client.get(self.key) or 0)

    def remaining(self) -> int:
        return max(0, self.limit - self.used())

    def available(self, amount: int = 1) -> bool:
        return self.used() + amount <= self.limit

    def consume(self, amount: int = 1) -> int:
        """記下用了 amount，回傳這個月累計用量。"""
        pipe = self.client.pipeline()
        pipe.incrby(self.key, amount)
        pipe.expireat(self.key, _next_month_expiry(self.clock()))
        total, _ = pipe.execute()
        return int(total)


def _next_month_expiry(now: dt.datetime) -> int:
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return int(dt.datetime(year, month, EXPIRE_DAY, tzinfo=TW).timestamp())
