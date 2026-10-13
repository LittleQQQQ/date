"""開放範圍：ACTIVE_CITIES 設定哪些縣市可以選。

範圍外的縣市不列在選單、不能排程、爬蟲也不更新，但資料留在資料庫；把設定改回來就能恢復。
"""
from __future__ import annotations

from .config import settings
from .regions import CITY_NAMES


def active_cities() -> tuple[str, ...]:
    """開放的縣市，照全台縣市的順序；空的代表全台開放。"""
    names = {c.strip() for c in settings.active_cities.split(",") if c.strip()}
    return tuple(c for c in CITY_NAMES if c in names)


def is_open(city: str) -> bool:
    cities = active_cities()
    return not cities or city in cities


def require_open(city: str) -> None:
    if not is_open(city):
        raise ValueError(f"目前只開放{'、'.join(active_cities())}")


def require_open_area(area_id: str) -> None:
    """商圈代碼是「縣市/商圈」，用前半段判斷。"""
    require_open(area_id.split("/", 1)[0])
