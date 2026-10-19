"""API 和 MCP 共用的請求格式（規格書 6.4）。

這裡只檢查格式；和資料庫有關的規則（地標屬於這個區域、出發時間不晚於場次資料的最後一天）在 services.py 檢查。
"""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field, field_validator

AREA_CODES = ("bade", "taoyuan", "zhongli", "qingpu")


class PlanRequest(BaseModel):
    area: Literal["bade", "taoyuan", "zhongli", "qingpu"] = Field(description="區域代碼：八德 bade、桃園 taoyuan、中壢 zhongli、青埔 qingpu")
    origin_location_id: int = Field(description="出發地的地標 ID（要屬於 area）")
    destination_location_id: int = Field(description="終點的地標 ID（要屬於 area，可以和出發地相同）")
    order: Literal["movie_first", "meal_first"] = Field(description="movie_first 先看電影，meal_first 先吃飯")
    depart_at: dt.datetime = Field(description="出發時間，ISO 8601 並帶時區，例如 2026-10-20T17:30:00+08:00")
    movie_genre: str | None = Field(None, description="電影種類代碼（見 list_categories），null 代表不限")
    cuisine: str | None = Field(None, description="餐廳類別代碼（見 list_categories），null 代表不限")
    party_size: int = Field(2, ge=1, le=10, description="人數：票價和車資會乘以人數")
    budget_total: int = Field(gt=0, description="總預算（新台幣），所有人加起來")

    @field_validator("depart_at")
    @classmethod
    def _aware(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            raise ValueError("出發時間要帶時區，例如 +08:00")
        return value
