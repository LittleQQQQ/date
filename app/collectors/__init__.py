from __future__ import annotations

from sqlalchemy.orm import Session

from ..scope import active_cities
from .atmovies import AtmoviesCollector
from .base import Batch, Collector
from .coverage import Coverage, load_coverage
from .fixture import BaseCollector, FixtureCollector
from .osm import OsmCollector
from .tdx import TdxCollector, load_network

# 資料來源登記表：新增爬蟲後在這裡加一行
COLLECTORS: dict[str, type[Collector]] = {
    BaseCollector.name: BaseCollector,
    FixtureCollector.name: FixtureCollector,
    AtmoviesCollector.name: AtmoviesCollector,
    OsmCollector.name: OsmCollector,
    TdxCollector.name: TdxCollector,
}


def get_collector(name: str, session: Session | None = None) -> Collector:
    if name not in COLLECTORS:
        raise ValueError(f"沒有這個資料來源：{name}（可用：{', '.join(COLLECTORS)}）")
    cls = COLLECTORS[name]
    if cls.needs_coverage or cls.needs_network:
        if session is None:
            raise ValueError(f"{name} 要先從資料庫讀商圈範圍或路網，請傳入 session")
        # 只抓開放範圍內的商圈與路段；其他縣市的資料留在資料庫，不更新也不刪
        if cls.needs_network:
            return cls(network=load_network(session, active_cities()))
        return cls(coverage=load_coverage(session, active_cities()))
    return cls()


__all__ = ["Batch", "Collector", "COLLECTORS", "Coverage", "get_collector", "load_coverage"]
