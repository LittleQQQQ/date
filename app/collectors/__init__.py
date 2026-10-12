from __future__ import annotations

from sqlalchemy.orm import Session

from .base import Batch, Collector
from .coverage import Coverage, load_coverage
from .fixture import FixtureCollector

# 資料來源登記表：新增爬蟲後在這裡加一行
COLLECTORS: dict[str, type[Collector]] = {
    FixtureCollector.name: FixtureCollector,
}


def get_collector(name: str, session: Session | None = None) -> Collector:
    if name not in COLLECTORS:
        raise ValueError(f"沒有這個資料來源：{name}（可用：{', '.join(COLLECTORS)}）")
    cls = COLLECTORS[name]
    if cls.needs_coverage:
        if session is None:
            raise ValueError(f"{name} 要先從資料庫讀商圈範圍，請傳入 session")
        return cls(coverage=load_coverage(session))
    return cls()


__all__ = ["Batch", "Collector", "COLLECTORS", "Coverage", "get_collector", "load_coverage"]
