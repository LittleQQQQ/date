"""每張表共通的欄位（規格書 4.2）：主鍵 bigint 自動遞增，created_at、updated_at 用 timestamptz。"""
from __future__ import annotations

from datetime import datetime

from geoalchemy2 import Geography, Geometry
from sqlalchemy import BigInteger, DateTime, Identity, cast, func
from sqlalchemy.orm import Mapped, mapped_column


def point() -> Geography:
    """座標：geography(Point, 4326)，距離單位是公尺。空間索引由各表的 __table_args__ 明確建立（GiST）。"""
    return Geography(geometry_type="POINT", srid=4326, spatial_index=False)


def point_wkt(lat: float, lng: float) -> str:
    """寫入座標用的字串。注意 PostGIS 的順序是（經度 緯度）。"""
    return f"SRID=4326;POINT({lng} {lat})"


def lat_of(column):
    """查詢用：geography 欄位的緯度（ST_Y 只吃 geometry，所以先轉型）。"""
    return func.ST_Y(cast(column, Geometry))


def lng_of(column):
    return func.ST_X(cast(column, Geometry))


class IdMixin:
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # ORM 更新時自動帶入；用原生 SQL 的 UPSERT 時要自己寫 updated_at = now()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
