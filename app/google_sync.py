"""Google 餐廳：每週找餐廳、更新座標、規劃時補店家資料（規格書 3.3、4.5、7.1、7.2）。

資料庫只存 place_id 和 30 天內的座標；店名、地址、營業時間、價位只在記憶體裡用。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import replace

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .collectors.base import ScrapeError
from .collectors.coverage import WALK_RADIUS_M, load_coverage
from .collectors.google_places import (
    ESTIMATED_SPAN, GooglePlaces, PlaceNotFound, PlacesError, QuotaExhausted, name_of, parse_hours, price_of,
)
from .ingest import _upsert
from .jev.classify import judge_google_restaurants
from .models import Restaurant, point_wkt
from .planner import Eatery
from .routing import Place
from .timeutil import WEEKDAY_KEYS, now_tw, to_min

GEOM_REFRESH_AFTER = dt.timedelta(days=25)  # 比條款的 30 天提早更新【預設】
GEOM_MAX_AGE = dt.timedelta(days=30)  # 條款：座標最多存 30 天


def refresh_google_places(session: Session, client: GooglePlaces | None = None, jev=None) -> dict:
    """每週：每個區域車站周圍 800 公尺做 Nearby Search（Pro），存 place_id 和座標，新的（或過期的）請 Jev 判斷類別和區域。"""
    client = client or GooglePlaces()
    coverage = load_coverage(session)
    seen: dict[str, dict] = {}
    failed = 0
    stopped = False
    for station in coverage.stations:
        try:
            places = client.search_nearby(station.lat, station.lng, WALK_RADIUS_M)
        except QuotaExhausted:
            stopped = True  # 這個月的 Pro 額度用完：暫停找新餐廳（規格書 5.5）
            break
        except PlacesError:
            failed += 1
            continue
        for place in places:
            seen.setdefault(place["id"], place)
    if coverage.stations and not seen and (failed == len(coverage.stations) or stopped):
        raise ScrapeError("Google Nearby Search 這次一家都沒查到（全部失敗或額度用完）")
    now = now_tw()
    _upsert(session, Restaurant, [
        {"source": "google", "external_id": pid, "geom": point_wkt(p["location"]["latitude"], p["location"]["longitude"]),
         "geom_fetched_at": now, "is_active": True}
        for pid, p in seen.items()
    ], ("source", "external_id"))
    ids = dict(session.execute(
        select(Restaurant.external_id, Restaurant.id).where(Restaurant.source == "google", Restaurant.external_id.in_(seen))
    ).all())
    judged = judge_google_restaurants(session, {ids[pid]: place for pid, place in seen.items()}, jev)
    return {"places": len(seen), "failed_searches": failed, "quota_exhausted": int(stopped),
            **{f"judge_{k}": v for k, v in judged.items()}}


def _expire(rest: Restaurant) -> None:
    rest.is_active, rest.geom, rest.geom_fetched_at = False, None, None


def refresh_google_geoms(session: Session, client: GooglePlaces | None = None, now: dt.datetime | None = None) -> dict:
    """座標超過 25 天就用 Place Details（Essentials）更新；店不在了，或超過 30 天還更新不了，就清掉座標、設為無效。"""
    client = client or GooglePlaces()
    now = now or now_tw()
    stats = {"refreshed": 0, "gone": 0, "expired": 0, "kept": 0}
    due = session.scalars(select(Restaurant).where(
        Restaurant.source == "google", Restaurant.geom.is_not(None),
        or_(Restaurant.geom_fetched_at.is_(None), Restaurant.geom_fetched_at < now - GEOM_REFRESH_AFTER),
    ))
    for rest in due:
        try:
            lat, lng = client.location(rest.external_id)
        except PlaceNotFound:
            _expire(rest)
            stats["gone"] += 1
            continue
        except PlacesError:  # 額度用完、連線失敗：還沒滿 30 天就先留著，滿了就清掉
            if rest.geom_fetched_at is None or rest.geom_fetched_at < now - GEOM_MAX_AGE:
                _expire(rest)
                stats["expired"] += 1
            else:
                stats["kept"] += 1
            continue
        rest.geom, rest.geom_fetched_at, rest.is_active = point_wkt(lat, lng), now, True
        stats["refreshed"] += 1
    session.flush()
    return stats


# ---------- 規劃時 ----------


def google_eatery(rest: Restaurant, lat: float, lng: float) -> Eatery:
    """還沒向 Google 補資料的餐廳：資料庫裡只有座標，先用估計的營業時間和價位組候選。"""
    return Eatery(
        id=rest.id, place=Place(f"res{rest.id}", "（Google 地圖上的餐廳）", lat, lng), source="google",
        category_code=rest.category_code, open=to_min(ESTIMATED_SPAN[0]), close=to_min(ESTIMATED_SPAN[1]),
        hours_text="（規劃時向 Google 查詢）", hours_estimated=True, price_per_person=500, price_level="MODERATE",
        external_id=rest.external_id,
    )


def make_enricher(client: GooglePlaces, day: dt.date) -> Callable[[Eatery], Eatery | None]:
    """規劃時對候選的 Google 餐廳呼叫 Place Details（Enterprise），補店名、地址、營業時間、價位。只在記憶體裡。

    查不到（額度用完、連線失敗、店不在了）回傳 None：這個候選不用，改用下一個（規格書 7.1 第 4 步）。
    """
    def enrich(eatery: Eatery) -> Eatery | None:
        try:
            details = client.details(eatery.external_id)
        except PlacesError:
            return None
        hours = parse_hours(details.get("regularOpeningHours"))
        estimated = hours is None
        if hours is None:
            hours = {"default": list(ESTIMATED_SPAN)}
        span = hours.get(WEEKDAY_KEYS[day.weekday()], hours.get("default"))
        level, price, _ = price_of(details)
        text = "公休" if not span else f"{span[0]}–{span[1]}" + ("（估計）" if estimated else "")
        return replace(
            eatery, place=replace(eatery.place, name=name_of(details) or eatery.place.name),
            open=to_min(span[0]) if span else None, close=to_min(span[1]) if span else None, hours_text=text,
            hours_estimated=estimated, price_per_person=price, price_level=level,
            address=details.get("formattedAddress", ""), enriched=True,
        )

    return enrich
