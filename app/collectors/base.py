"""資料來源（collector）介面。

每個資料來源——示意資料、影城場次爬蟲、餐廳爬蟲、TDX 交通 API——都輸出同一種 Batch，
再由 app/ingest.py 寫進資料庫。排程只讀資料庫，所以換資料來源時其他程式都不用改。

新增一個爬蟲的步驟：
1. 在 app/collectors/ 新增檔案，寫一個繼承 Collector 的類別，實作 collect()
2. 在 app/collectors/__init__.py 的 COLLECTORS 登記名稱
3. 執行 python -m app.ingest --source 名稱
"""
from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod

from pydantic import BaseModel, Field, field_validator, model_validator

from ..regions import CITY_NAMES, area_id
from ..timeutil import WEEKDAY_KEYS, to_min

TRANSIT_MODES = ("捷運", "台鐵", "高鐵", "輕軌", "公車")


def _check_city(value: str) -> str:
    if value not in CITY_NAMES:
        raise ValueError(f"不是台灣的縣市名稱：{value}（請用「台」字，例如台北市）")
    return value


class AreaRecord(BaseModel):
    id: str  # 縣市/商圈，例如「台北市/信義」
    city: str
    name: str

    @field_validator("city")
    @classmethod
    def _city(cls, value: str) -> str:
        return _check_city(value)

    @model_validator(mode="after")
    def _check_id(self) -> AreaRecord:
        if self.id != area_id(self.city, self.name):
            raise ValueError(f"商圈代碼應為「{area_id(self.city, self.name)}」，不是「{self.id}」")
        return self


class StationRecord(BaseModel):
    id: str
    name: str
    city: str
    lat: float
    lng: float
    transfer_min: int = 3
    is_origin: bool = False
    area_id: str | None = None

    @field_validator("city")
    @classmethod
    def _city(cls, value: str) -> str:
        return _check_city(value)


class TransitLinkRecord(BaseModel):
    from_station_id: str
    to_station_id: str
    mode: str
    line: str
    ride_min: int
    headway_min: int
    first_train: str
    last_train: str

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, value: str) -> str:
        if value not in TRANSIT_MODES:
            raise ValueError(f"交通方式只能是：{'、'.join(TRANSIT_MODES)}")
        return value


class TransitFareRecord(BaseModel):
    """某條路線從 from 站搭到 to 站的每人車資（成人全票）。"""

    from_station_id: str
    to_station_id: str
    line: str
    fare: int = Field(ge=0)


class CinemaRecord(BaseModel):
    external_id: str
    name: str
    area_id: str
    address: str = ""
    lat: float
    lng: float
    source_url: str = ""


class MovieRecord(BaseModel):
    external_id: str
    title: str
    runtime_min: int
    genre: str = ""
    synopsis: str = ""
    source_url: str = ""


class ShowtimeRecord(BaseModel):
    external_id: str
    cinema_external_id: str
    # 來源網站有連到電影頁就填；沒有的話 ingest 會用片名比對
    movie_external_id: str | None = None
    raw_title: str
    date: dt.date
    start: str
    label: str
    price: int
    source_url: str = ""
    # 哪些欄位是估計的，例如 ["price"]（網站沒有票價，依版本估）
    estimated: list[str] = Field(default_factory=list)


class RestaurantRecord(BaseModel):
    external_id: str
    name: str
    area_id: str
    address: str = ""
    lat: float
    lng: float
    description: str
    reviews: list[str] = Field(default_factory=list)
    price_per_person: int
    hours: dict[str, list[str] | None]
    last_order_min: int = 30
    typical_wait_min: int = 10
    reservable: bool = False
    source_url: str = ""
    # 哪些欄位是估計的，例如 ["hours", "price"]
    estimated: list[str] = Field(default_factory=list)

    @field_validator("hours")
    @classmethod
    def _check_hours(cls, value: dict[str, list[str] | None]) -> dict[str, list[str] | None]:
        if "default" not in value:
            raise ValueError("hours 至少要有 default")
        for key, span in value.items():
            if key != "default" and key not in WEEKDAY_KEYS:
                raise ValueError(f"hours 的 key 只能是 default 或 {', '.join(WEEKDAY_KEYS)}：{key}")
            if span is not None:
                if len(span) != 2 or to_min(span[0]) >= to_min(span[1]):
                    raise ValueError(f"營業時間格式錯誤：{span}")
        return value


class Batch(BaseModel):
    areas: list[AreaRecord] = Field(default_factory=list)
    stations: list[StationRecord] = Field(default_factory=list)
    transit_links: list[TransitLinkRecord] = Field(default_factory=list)
    transit_fares: list[TransitFareRecord] = Field(default_factory=list)
    cinemas: list[CinemaRecord] = Field(default_factory=list)
    movies: list[MovieRecord] = Field(default_factory=list)
    showtimes: list[ShowtimeRecord] = Field(default_factory=list)
    restaurants: list[RestaurantRecord] = Field(default_factory=list)
    # 這次抓取涵蓋的場次日期（含頭尾）。範圍內同一來源的舊場次會被整批取代，已取消的場次就會消失
    showtime_dates: tuple[dt.date, dt.date] | None = None
    # 有值時，只替換這些影城（external_id）的場次；抓失敗的影城不列入，保留上次的場次
    showtime_cinemas: list[str] | None = None
    # 有值時，這些商圈裡這次沒出現的同來源餐廳會被移除（店收了）；抓失敗的商圈不列入
    restaurant_areas: list[str] | None = None
    # 額外的統計（例如失敗頁數），會寫進 scrape_runs.counts
    stats: dict[str, int] = Field(default_factory=dict)
    # True：這批是完整的商圈與交通（base）。資料庫裡這批沒有的路段、車站、商圈（連同其中的影城、場次、餐廳）會被刪掉
    replace_network: bool = False
    # True：這批是這個來源的全部路段與車資（TDX）。資料庫裡同來源、這批沒有的路段和車資會被刪掉
    replace_source_links: bool = False


class ScrapeError(Exception):
    """整批抓取失敗（例如網站改版、全部請求都失敗）；資料庫保留舊資料。"""


class Collector(ABC):
    name: str = ""  # 也會寫進資料表的 source 欄位
    # True：建立時要傳入 coverage（資料庫裡的商圈與車站），由 get_collector 準備
    needs_coverage: bool = False
    # True：建立時要傳入 network（資料庫裡手寫的路網骨架），由 get_collector 準備
    needs_network: bool = False

    @abstractmethod
    def collect(self, start: dt.date, days: int) -> Batch:
        """抓取資料。start、days 是希望抓的場次日期範圍；不需要場次的來源可以忽略。"""
