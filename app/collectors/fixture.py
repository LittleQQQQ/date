"""示意資料：讀 data/fixture/ 底下的所有 JSON（每個縣市一個檔）。

- base：只有商圈、車站、交通路段，正式執行時用；影城、電影、場次、餐廳由爬蟲提供。
- fixture：完整的虛構資料（含影城、餐廳、場次範本），給自動測試用。

捷運、台鐵、高鐵、公車的車程與班次是估計值。
交通路段在 JSON 裡可以寫 "both_ways": true，代表反方向也有一樣的路段（末班車可用 last_back 另外指定）。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from ..config import ROOT_DIR
from .base import (
    AreaRecord,
    Batch,
    CinemaRecord,
    Collector,
    MovieRecord,
    RestaurantRecord,
    ShowtimeRecord,
    StationRecord,
    TransitLinkRecord,
)

LIST_KEYS = ("areas", "stations", "transit_links", "cinemas", "movies", "restaurants", "showtime_templates")


def _links(item: dict) -> list[TransitLinkRecord]:
    common = {"mode": item["mode"], "line": item["line"], "ride_min": item["ride"], "headway_min": item["headway"]}
    links = [
        TransitLinkRecord(
            from_station_id=item["from"], to_station_id=item["to"],
            first_train=item["first"], last_train=item["last"], **common,
        )
    ]
    if item.get("both_ways"):
        links.append(
            TransitLinkRecord(
                from_station_id=item["to"], to_station_id=item["from"],
                first_train=item.get("first_back", item["first"]), last_train=item.get("last_back", item["last"]),
                **common,
            )
        )
    return links


class FixtureCollector(Collector):
    name = "fixture"

    def __init__(self, folder: Path | None = None):
        self.folder = folder or ROOT_DIR / "data" / "fixture"

    def _load(self) -> dict[str, list]:
        merged: dict[str, list] = {key: [] for key in LIST_KEYS}
        for path in sorted(self.folder.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in LIST_KEYS:
                merged[key].extend(data.get(key, []))
        return merged

    def collect(self, start: dt.date, days: int) -> Batch:
        data = self._load()
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
            areas=[AreaRecord(**a) for a in data["areas"]],
            stations=[StationRecord(**s) for s in data["stations"]],
            transit_links=[link for item in data["transit_links"] for link in _links(item)],
            cinemas=[CinemaRecord(**c) for c in data["cinemas"]],
            movies=[MovieRecord(**m) for m in data["movies"]],
            restaurants=[RestaurantRecord(**r) for r in data["restaurants"]],
            showtimes=showtimes,
            showtime_dates=(start, start + dt.timedelta(days=days - 1)),
        )


class BaseCollector(FixtureCollector):
    """只匯入商圈、車站、交通路段。"""

    name = "base"

    def collect(self, start: dt.date, days: int) -> Batch:
        data = self._load()
        return Batch(
            areas=[AreaRecord(**a) for a in data["areas"]],
            stations=[StationRecord(**s) for s in data["stations"]],
            transit_links=[link for item in data["transit_links"] for link in _links(item)],
        )
