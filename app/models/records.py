"""紀錄：Jev 判斷結果、抓取紀錄（規格書 4.3）。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Numeric, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base
from .base import IdMixin, TimestampMixin


class JevJudgment(IdMixin, TimestampMixin, Base):
    """寫入時的 Jev 判斷（答案不因人而異）。每個對象、每一題只保留最新一次，重算時覆蓋。

    答案也會寫回主表的欄位（例如 restaurants.category_code），規劃時直接用來篩選；這裡存完整細節。
    Jev 沒把握、改由 LLM 判斷時，decided_by = llm、confidence 是 null。
    """

    __tablename__ = "jev_judgments"
    __table_args__ = (
        UniqueConstraint("subject_type", "subject_id", "question"),
        CheckConstraint("subject_type IN ('movie', 'cinema', 'restaurant')", name="subject_type"),
        CheckConstraint("decided_by IN ('jev', 'llm')", name="decided_by"),
        Index(None, "model_version"),
        Index(None, "judged_at"),
    )

    subject_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[int] = mapped_column(BigInteger)  # 對應資料表的 id（多型關聯，不設外鍵）
    question: Mapped[str] = mapped_column(Text)  # genre、cuisine、area
    answer: Mapped[str] = mapped_column(Text)  # 選中的選項代碼
    probabilities: Mapped[dict] = mapped_column(JSONB)  # 每個選項的機率
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    decided_by: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str] = mapped_column(Text)  # 例如 jev-1.14.0 或 qwen2.5:3b
    judged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CrawlRun(IdMixin, TimestampMixin, Base):
    __tablename__ = "crawl_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'success', 'failed')", name="status"),
        Index("ix_crawl_runs_job_started_at", "job", text("started_at DESC")),
    )

    job: Mapped[str] = mapped_column(Text)  # 例如 atmovies、google_nearby、osm、refresh_transit、rejudge、geom_refresh
    status: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))  # 新增、更新、刪除的筆數等
    error: Mapped[str | None] = mapped_column(Text)
