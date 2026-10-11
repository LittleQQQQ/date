from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .jev.questions import AUDIO, CUISINE, SCREEN
from .timeutil import hhmm, to_min


class PlanRequest(BaseModel):
    """排程條件。字串留空、數字填 0 代表「不限」。"""

    date: dt.date
    origin: str = Field("板橋", description="出發的捷運站名或代碼")
    area: str = Field("信義", description="約會的商圈")
    depart_after: str = Field("16:00", description="最早幾點可以出門（HH:MM）")
    home_by: str = Field("23:00", description="最晚幾點要回到出發站（HH:MM，跨午夜可寫 00:30 或 24:30）")
    movie_query: str = Field("", description="想看的片名，空字串代表不限")
    screen_types: list[str] = Field(default_factory=list, description=f"想看的影廳：{'、'.join(SCREEN.options)}")
    audio: str = Field("", description=f"語言：{'、'.join(AUDIO.options)}，空字串代表不限")
    cuisines: list[str] = Field(default_factory=list, description=f"想吃的料理：{'、'.join(CUISINE.options)}")
    budget_per_person: int = Field(0, ge=0, description="每人預算（電影票＋餐費），0 代表不限")
    meal_order: Literal["any", "after", "before"] = Field("any", description="after＝看完電影再吃，before＝先吃再看")
    max_walk_min: int = Field(12, ge=1, le=30, description="影城和餐廳之間最多走幾分鐘")
    overrides: dict[str, str] = Field(default_factory=dict, description="使用者確認過的 Jev 答案：{key: 選項}")

    @field_validator("depart_after", "home_by")
    @classmethod
    def _check_clock(cls, value: str) -> str:
        return hhmm(to_min(value))

    @field_validator("screen_types")
    @classmethod
    def _check_screens(cls, value: list[str]) -> list[str]:
        bad = [v for v in value if v not in SCREEN.options]
        if bad:
            raise ValueError(f"不認得的影廳：{'、'.join(bad)}")
        return value

    @field_validator("cuisines")
    @classmethod
    def _check_cuisines(cls, value: list[str]) -> list[str]:
        bad = [v for v in value if v not in CUISINE.options]
        if bad:
            raise ValueError(f"不認得的料理類型：{'、'.join(bad)}")
        return value

    @field_validator("audio")
    @classmethod
    def _check_audio(cls, value: str) -> str:
        if value and value not in AUDIO.options:
            raise ValueError(f"語言只能是：{'、'.join(AUDIO.options)}")
        return value

    @model_validator(mode="after")
    def _overnight(self) -> PlanRequest:
        home, depart = to_min(self.home_by), to_min(self.depart_after)
        if home <= depart and home < 12 * 60:  # 跨午夜，例如 00:30 → 24:30
            home += 24 * 60
            self.home_by = hhmm(home)
        if home <= depart:
            raise ValueError("最晚到家時間要晚於最早出門時間")
        return self


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
