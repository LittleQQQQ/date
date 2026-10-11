from .base import Batch, Collector
from .fixture import FixtureCollector

# 資料來源登記表：新增爬蟲後在這裡加一行
COLLECTORS: dict[str, type[Collector]] = {
    FixtureCollector.name: FixtureCollector,
}


def get_collector(name: str) -> Collector:
    if name not in COLLECTORS:
        raise ValueError(f"沒有這個資料來源：{name}（可用：{', '.join(COLLECTORS)}）")
    return COLLECTORS[name]()


__all__ = ["Batch", "Collector", "COLLECTORS", "get_collector"]
