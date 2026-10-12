from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    if url.startswith("sqlite"):
        # 背景更新和網頁請求可能同時寫入，等鎖最多 30 秒
        return create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    return create_engine(url, pool_pre_ping=True)


engine = _make_engine(settings.db_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def schema_problems() -> list[str]:
    """資料庫裡的資料表欄位和程式不一致的地方（create_all 只會建新表，不會幫舊表加欄位）。"""
    inspector = inspect(engine)
    problems = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        missing = [col.name for col in table.columns if col.name not in existing]
        if missing:
            problems.append(f"{table.name} 缺少欄位：{', '.join(missing)}")
    return problems


def schema_message(problems: list[str]) -> str:
    return (
        "資料庫的資料表結構是舊版的：\n  " + "\n  ".join(problems) + "\n"
        "請執行 python -m app.ingest --reset 重建（會清空資料庫裡的資料，再重新匯入商圈與交通；影城、場次、餐廳會在伺服器啟動時重新抓）。"
    )
