"""示意資料來源：讀 data/fixture.json，場次依範本產生指定日期範圍。

店名、影城、場次、捷運時間都是虛構或估計的，只用來展示流程。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from ..config import ROOT_DIR
from .base import (
    Batch,
    CinemaRecord,
    Collector,
    MovieRecord,
    RestaurantRecord,
    ShowtimeRecord,
    StationRecord,
    TransitLinkRecord,
)


class FixtureCollector(Collector):
    name = "fixture"

    def __init__(self, path: Path | None = None):
        self.path = path or ROOT_DIR / "data" / "fixture.json"

    def collect(self, start: dt.date, days: int) -> Batch:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        movie_titles = {m["external_id"]: m["title"] for m in data["movies"]}

        showtimes = []
        for offset in range(days):
            day = start + dt.timedelta(days=offset)
            for tpl in data["showtime_templates"]:
                for start_time in tpl["times"]:
                    showtimes.append(
                        ShowtimeRecord(
                            external_id=f"{tpl['cinema']}|{tpl['movie']}|{tpl['label']}|{day.isoformat()}|{start_time}",
                            cinema_external_id=tpl["cinema"],
                            movie_external_id=tpl["movie"],
                            raw_title=tpl.get("raw_title") or movie_titles[tpl["movie"]],
                            date=day,
                            start=start_time,
                            label=tpl["label"],
                            price=tpl["price"],
                        )
                    )

        return Batch(
            stations=[StationRecord(**s) for s in data["stations"]],
            transit_links=[TransitLinkRecord(**link) for link in data["transit_links"]],
            cinemas=[CinemaRecord(**c) for c in data["cinemas"]],
            movies=[MovieRecord(**m) for m in data["movies"]],
            restaurants=[RestaurantRecord(**r) for r in data["restaurants"]],
            showtimes=showtimes,
            showtime_dates=(start, start + dt.timedelta(days=days - 1)),
        )
