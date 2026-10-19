"""資料來源（collector）介面：每個爬蟲都輸出同一種 Batch，再由 app/ingest.py 寫進資料庫。

爬蟲只負責抓和解析，不碰資料庫；寫入、刪除過期資料、記錄 crawl_runs 都在 ingest.py。
"""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ..models import TRANSIT_MODES


class ScrapeError(Exception):
    """整批抓取失敗（例如網站改版、全部請求都失敗）；資料庫保留舊資料。"""


class CinemaRecord(BaseModel):
    atmovies_id: str
    name: str
    address: str = ""
    lat: float
    lng: float


class MovieRecord(BaseModel):
    atmovies_id: str
    title: str
    runtime_min: int = Field(gt=0)
    synopsis: str = ""


class ShowtimeRecord(BaseModel):
    cinema_atmovies_id: str
    movie_atmovies_id: str
    starts_at: dt.datetime
    ends_at: dt.datetime
    version: str = ""

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _aware(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            raise ValueError("場次時間要帶時區")
        return value


class RestaurantRecord(BaseModel):
    """OSM 來源的餐廳。Google 來源不經過這裡（依條款只存 place_id 和座標，見規格書 3.3）。"""

    external_id: str  # node/123、way/456
    lat: float
    lng: float
    osm_name: str
    osm_opening_hours: str | None = None
    osm_tags: dict[str, str] = Field(default_factory=dict)


class LinkRecord(BaseModel):
    """有方向的路段。站用路網骨架的代碼（stations.code）。"""

    from_code: str
    to_code: str
    mode: str
    line: str
    ride_min: int = Field(ge=0)
    headway_min: int = Field(gt=0)
    first_min: int = Field(ge=0)
    last_min: int = Field(ge=0)

    @field_validator("mode")
    @classmethod
    def _mode(cls, value: str) -> str:
        if value not in TRANSIT_MODES:
            raise ValueError(f"交通方式只能是：{'、'.join(TRANSIT_MODES)}")
        return value


class FareRecord(BaseModel):
    from_code: str
    to_code: str
    line: str
    fare: int = Field(ge=0)


class Batch(BaseModel):
    cinemas: list[CinemaRecord] = Field(default_factory=list)
    movies: list[MovieRecord] = Field(default_factory=list)
    showtimes: list[ShowtimeRecord] = Field(default_factory=list)
    # 有值時：這些影城在 showtime_window 時段內、這次沒抓到的場次會被刪掉（取消了）。抓失敗的影城不列入，保留上次的場次
    showtime_cinemas: list[str] | None = None
    showtime_window: tuple[dt.datetime, dt.datetime] | None = None
    restaurants: list[RestaurantRecord] = Field(default_factory=list)
    # True：這批是完整的 OSM 餐廳（每個區域都抓成功），這次沒出現的 OSM 餐廳設為無效（店收了）
    restaurants_complete: bool = False
    links: list[LinkRecord] = Field(default_factory=list)
    fares: list[FareRecord] = Field(default_factory=list)
    # True：這批是 TDX 的全部路段與車資，資料庫裡 source = tdx、這批沒有的刪掉
    replace_tdx: bool = False
    stats: dict[str, int] = Field(default_factory=dict)  # 額外的統計，會寫進 crawl_runs.stats


class Collector(ABC):
    name: str = ""  # 也是 crawl_runs.job

    @classmethod
    def create(cls, session: Session) -> Collector:
        """從資料庫準備需要的東西（例如涵蓋範圍、路網骨架）再建立爬蟲；預設不需要。"""
        return cls()

    @abstractmethod
    def collect(self) -> Batch:
        """抓取資料。失敗時丟出 ScrapeError，資料庫保留舊資料。"""
