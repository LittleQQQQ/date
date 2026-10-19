"""快取（規格書 5.3）：JSON 存進 Redis 字串，帶過期時間。

- 路線快取（A2）：route:v1:{路網版本}:{起點}:{終點}:{時段}，6 小時。
  出發時間往後取整到 15 分鐘（18:07 → 18:15），查「18:15 出發」，使用者不會錯過車。
  路網版本是最近一次成功更新路網的 crawl_runs.id；TDX 每週更新後版本改變，舊快取自然過期。
- Google 的資料一律不快取（條款，規格書 3.3）。
"""
from __future__ import annotations

import json
from typing import Any

import redis

from . import get_redis

ROUTE_TTL_S = 6 * 60 * 60
ROUTE_SLOT_MIN = 15
ROUTE_VERSION = "v1"  # 路線計算的邏輯改版時改成 v2，舊快取放著自然過期


def get_json(key: str, client: redis.Redis | None = None) -> Any | None:
    raw = (client or get_redis()).get(key)
    return None if raw is None else json.loads(raw)


def set_json(key: str, value: Any, ttl_s: int, client: redis.Redis | None = None) -> bool:
    """寫入快取。記憶體滿了（noeviction）寫入會失敗：快取寫不進去就算了，不影響規劃（規格書 5.8）。"""
    try:
        return bool((client or get_redis()).set(key, json.dumps(value, ensure_ascii=False), ex=ttl_s))
    except redis.exceptions.ResponseError:
        return False


def route_slot(minutes: int) -> int:
    """出發時間（從當天 00:00 起的分鐘數）往後取整到 15 分鐘。"""
    return -(-minutes // ROUTE_SLOT_MIN) * ROUTE_SLOT_MIN


def route_key(network_version: int, origin: str, destination: str, depart_min: int) -> str:
    """起點、終點用「前綴＋資料庫 ID」，例如 loc12、cin3、res45、sta7。"""
    return f"route:{ROUTE_VERSION}:{network_version}:{origin}:{destination}:{route_slot(depart_min)}"
