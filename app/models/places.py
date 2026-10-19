"""地點：區域、地標、影城、餐廳（規格書 4.3）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import IdMixin, TimestampMixin, point


class Area(IdMixin, TimestampMixin, Base):
    """4 個區域：八德、桃園、中壢、青埔。資料由第一個 migration 寫入。"""

    __tablename__ = "areas"

    code: Mapped[str] = mapped_column(Text, unique=True)  # bade、taoyuan、zhongli、qingpu
    name: Mapped[str] = mapped_column(Text)


class Location(IdMixin, TimestampMixin, Base):
    """地標：出發地和終點共用同一份清單（團隊提供）。座標用 TDX 地理編碼取得。"""

    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("area_id", "name"), Index(None, "area_id"))

    area_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("areas.id"))
    name: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text, server_default="")
    geom = mapped_column(point(), nullable=True)
    geocoded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Cinema(IdMixin, TimestampMixin, Base):
    __tablename__ = "cinemas"
    __table_args__ = (
        Index("ix_cinemas_geom", "geom", postgresql_using="gist"),
        Index(None, "area_id"),
    )

    atmovies_id: Mapped[str] = mapped_column(Text, unique=True)  # 開眼電影網的影城代碼
    name: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text, server_default="")
    geom = mapped_column(point(), nullable=True)  # 地理編碼前是 null
    area_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("areas.id"))  # Jev 判斷
    ticket_price: Mapped[int | None] = mapped_column(Integer)  # 每人票價（新台幣），手動設定
    geocoded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class Restaurant(IdMixin, TimestampMixin, Base):
    """餐廳。Google 來源依條款只存 place_id 和 30 天內的座標，店名等內容一律不存（CHECK 約束把關）。"""

    __tablename__ = "restaurants"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        CheckConstraint("source IN ('google', 'osm')", name="source"),
        # 就算程式寫錯，資料庫也不會存進 Google 的店家內容
        CheckConstraint(
            "source <> 'google' OR (osm_name IS NULL AND osm_opening_hours IS NULL AND osm_tags IS NULL)",
            name="google_stores_no_content",
        ),
        Index("ix_restaurants_geom", "geom", postgresql_using="gist"),
        Index(None, "area_id", "category_code"),
    )

    source: Mapped[str] = mapped_column(Text)  # google 或 osm
    external_id: Mapped[str] = mapped_column(Text)  # Google 是 place_id，OSM 是 node/123 這類格式
    geom = mapped_column(point(), nullable=False)
    geom_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # Google 座標要在 30 天內更新
    area_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("areas.id"))  # Jev 判斷
    category_code: Mapped[str | None] = mapped_column(Text)  # Jev 判斷，對應 categories
    osm_name: Mapped[str | None] = mapped_column(Text)  # 只有 OSM 來源才能有值
    osm_opening_hours: Mapped[str | None] = mapped_column(Text)
    osm_tags: Mapped[dict | None] = mapped_column(JSONB)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
