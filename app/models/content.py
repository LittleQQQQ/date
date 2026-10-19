"""電影、場次、類別清單（規格書 4.3）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import IdMixin, TimestampMixin


class Movie(IdMixin, TimestampMixin, Base):
    __tablename__ = "movies"

    atmovies_id: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    runtime_min: Mapped[int] = mapped_column(Integer)
    rating: Mapped[str | None] = mapped_column(Text)  # 分級
    genre_code: Mapped[str | None] = mapped_column(Text)  # Jev 判斷，對應 categories
    # 規格書沒列，實作時補上：Jev 判斷電影種類要讀簡介（開眼的電影頁）
    synopsis: Mapped[str] = mapped_column(Text, server_default="")


class Showtime(IdMixin, TimestampMixin, Base):
    __tablename__ = "showtimes"
    __table_args__ = (
        UniqueConstraint("cinema_id", "movie_id", "starts_at", "version"),
        Index(None, "cinema_id", "starts_at"),
    )

    cinema_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("cinemas.id", ondelete="CASCADE"))
    movie_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("movies.id", ondelete="CASCADE"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 開演時間＋片長
    # 數位、IMAX、語言等。預設空字串，避免 null 讓唯一鍵失效（null 不等於 null）
    version: Mapped[str] = mapped_column(Text, server_default="")


class Category(IdMixin, TimestampMixin, Base):
    """電影種類、餐廳類別（團隊提供）。也是 Jev 分類題的選項。"""

    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("kind", "code"),
        CheckConstraint("kind IN ('movie_genre', 'cuisine')", name="kind"),
    )

    kind: Mapped[str] = mapped_column(Text)  # movie_genre 或 cuisine
    code: Mapped[str] = mapped_column(Text)  # 例如 japanese
    label: Mapped[str] = mapped_column(Text)  # 畫面上顯示的名稱
    jev_description: Mapped[str] = mapped_column(Text, server_default="")  # 給 Jev 的選項說明
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
