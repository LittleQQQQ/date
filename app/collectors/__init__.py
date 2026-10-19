"""資料來源（爬蟲）。新增爬蟲：繼承 base.Collector、實作 collect()，再登記到 COLLECTORS。"""
from __future__ import annotations

from .atmovies import AtmoviesCollector
from .base import Batch, Collector
from .osm import OsmCollector
from .tdx import TdxCollector

# 名稱 → 爬蟲類別。名稱也是 crawl_runs.job 和 python -m app.ingest 的參數
COLLECTORS: dict[str, type[Collector]] = {
    AtmoviesCollector.name: AtmoviesCollector,
    OsmCollector.name: OsmCollector,
    TdxCollector.name: TdxCollector,
}

__all__ = ["Batch", "COLLECTORS", "Collector"]
