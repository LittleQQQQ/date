"""資料表。

- 時間一律存成 "HH:MM" 字串，跨午夜寫成 24:30、25:00，方便直接相減。
- 會被爬蟲更新的資料都有 source（哪個資料來源）、external_id（來源網站上的編號）、fetched_at（抓取時間）。
  同一個來源重複抓到同一筆資料時，用 (source, external_id) 找到舊資料更新，而不是新增一筆。
- 片名、場次標示、餐廳介紹、評論都保留原始文字，這些是 Jev 的判斷對象。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .timeutil import now_tw


class Station(Base):
    __tablename__ = "stations"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # 捷運站代碼，例如 BL07
    name: Mapped[str] = mapped_column(String(50))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    is_origin: Mapped[bool] = mapped_column(Boolean, default=False)  # 可以當出發地
    area: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 商圈站才有，例如「信義」
    source: Mapped[str] = mapped_column(String(30))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TransitLink(Base):
    __tablename__ = "transit_links"
    __table_args__ = (UniqueConstraint("from_station_id", "to_station_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    to_station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    ride_min: Mapped[int] = mapped_column(Integer)  # 車程（含轉乘）
    headway_min: Mapped[int] = mapped_column(Integer)  # 班距，用來估最久要等多久
    first_train: Mapped[str] = mapped_column(String(5))
    last_train: Mapped[str] = mapped_column(String(5))  # 從出發站發車的末班時間
    note: Mapped[str] = mapped_column(String(100), default="")
    source: Mapped[str] = mapped_column(String(30))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Cinema(Base):
    __tablename__ = "cinemas"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(50))
    area: Mapped[str] = mapped_column(String(20))
    address: Mapped[str] = mapped_column(String(200), default="")
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    source_url: Mapped[str] = mapped_column(String(500), default="")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Movie(Base):
    __tablename__ = "movies"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(100))
    runtime_min: Mapped[int] = mapped_column(Integer)
    genre: Mapped[str] = mapped_column(String(20), default="")
    source_url: Mapped[str] = mapped_column(String(500), default="")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Showtime(Base):
    __tablename__ = "showtimes"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(150))
    cinema_id: Mapped[int] = mapped_column(ForeignKey("cinemas.id"))
    # 對得到電影才知道片長；對不到時為 NULL，排程會先跳過
    movie_id: Mapped[int | None] = mapped_column(ForeignKey("movies.id"), nullable=True)
    raw_title: Mapped[str] = mapped_column(String(100))  # 影城網頁上的原始片名，寫法可能和片名不同
    date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    start: Mapped[str] = mapped_column(String(5))
    label: Mapped[str] = mapped_column(String(50))  # 原始場次標示，例如「IMAX 2D 英語」
    price: Mapped[int] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(String(500), default="")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Restaurant(Base):
    __tablename__ = "restaurants"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(50))
    area: Mapped[str] = mapped_column(String(20))
    address: Mapped[str] = mapped_column(String(200), default="")
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    description: Mapped[str] = mapped_column(Text)
    reviews: Mapped[list] = mapped_column(JSON, default=list)
    price_per_person: Mapped[int] = mapped_column(Integer)
    # {"default": ["11:30", "22:00"], "mon": null}：沒寫的星期用 default，null 表示公休
    hours: Mapped[dict] = mapped_column(JSON)
    last_order_min: Mapped[int] = mapped_column(Integer)  # 打烊前幾分鐘最後點餐
    typical_wait_min: Mapped[int] = mapped_column(Integer)  # 一般時段的候位分鐘數
    reservable: Mapped[bool] = mapped_column(Boolean, default=False)
    source_url: Mapped[str] = mapped_column(String(500), default="")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JevJudgment(Base):
    """Jev 判斷結果的快取。原始文字沒變（input_hash 相同）就直接用，不用再問一次 Jev。"""

    __tablename__ = "jev_judgments"
    __table_args__ = (UniqueConstraint("question", "subject_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    question: Mapped[str] = mapped_column(String(32))
    subject_key: Mapped[str] = mapped_column(String(200))
    input_hash: Mapped[str] = mapped_column(String(64))
    options: Mapped[list] = mapped_column(JSON)
    probs: Mapped[list] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(10))  # mock / api
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_tw)


class ScrapeRun(Base):
    """每次執行資料來源（示意資料或爬蟲）的紀錄。"""

    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(10))  # running / ok / error
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now_tw)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
