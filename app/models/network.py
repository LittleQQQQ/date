"""交通路網（規格書 v0.2 4.3）：車站、有方向的路段、真實車資。

骨架（有哪些站、哪兩站之間有車）是手寫的（source = base）；班距、首末班、票價每週由 TDX 更新（source = tdx）。
同一段、同一種交通方式有 tdx 的就不用 base 的。

首末班存「從當天 00:00 起的分鐘數」，跨午夜可以大於 1440（例如 24:09 存 1449）。
這是一天裡的時刻，不是某個時間點，所以不用 timestamptz。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import IdMixin, TimestampMixin, point

TRANSIT_MODES = ("台鐵", "高鐵", "捷運", "輕軌", "公車")
NETWORK_SOURCES = ("base", "tdx")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Station(IdMixin, TimestampMixin, Base):
    __tablename__ = "stations"
    __table_args__ = (Index("ix_stations_geom", "geom", postgresql_using="gist"),)

    code: Mapped[str] = mapped_column(Text, unique=True)  # 例如 tyn-zhongli，手寫骨架用的代碼
    name: Mapped[str] = mapped_column(Text)
    geom = mapped_column(point(), nullable=False)
    transfer_min: Mapped[int] = mapped_column(Integer, server_default="3")  # 在這站換線要走幾分鐘
    area_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("areas.id"))


class TransitLink(IdMixin, TimestampMixin, Base):
    """有方向的路段：某條路線從 from 站開到 to 站。同一條路線（line）連續搭乘不用再等車。"""

    __tablename__ = "transit_links"
    __table_args__ = (
        UniqueConstraint("from_station_id", "to_station_id", "line"),
        CheckConstraint(_in("mode", TRANSIT_MODES), name="mode"),
        CheckConstraint(_in("source", NETWORK_SOURCES), name="source"),
        CheckConstraint("first_min <= last_min", name="first_before_last"),
    )

    from_station_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stations.id", ondelete="CASCADE"))
    to_station_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stations.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(Text)
    line: Mapped[str] = mapped_column(Text)
    ride_min: Mapped[int] = mapped_column(Integer)
    headway_min: Mapped[int] = mapped_column(Integer)  # 最長班距，用來估最久要等多久
    first_min: Mapped[int] = mapped_column(Integer)  # 從 from 站發車的首班
    last_min: Mapped[int] = mapped_column(Integer)  # 從 from 站發車的末班
    source: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TransitFare(IdMixin, TimestampMixin, Base):
    """路段的真實車資（每人單程、成人全票），只有 TDX 提供；沒有的路段由 app/fares.py 估計。"""

    __tablename__ = "transit_fares"
    __table_args__ = (
        UniqueConstraint("from_station_id", "to_station_id", "line"),
        CheckConstraint("fare >= 0", name="fare"),
    )

    from_station_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stations.id", ondelete="CASCADE"))
    to_station_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("stations.id", ondelete="CASCADE"))
    line: Mapped[str] = mapped_column(Text)  # 和 transit_links.line 對應
    fare: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
