"""服務層（規格書 2.2）：API 和 MCP 共用。組查詢條件、讀資料、檢查和資料庫有關的規則。

規劃本身在 worker 裡跑（planner.py）；這裡準備它要的資料（PlanContext）。
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .collectors.osm import address, cuisine_category, estimate_price, place_hours
from .models import (
    Area, Cinema, CrawlRun, Location, Movie, Restaurant, Showtime, Station, TransitFare, TransitLink, lat_of, lng_of,
)
from .planner import Eatery, PlanContext, ShowInfo
from .routing import Place, Router, StationPlace
from .schemas import PlanRequest
from .timeutil import TW, WEEKDAY_KEYS, now_tw, to_min
from .transit import Edge, Node, TransitGraph

PLAN_WINDOW = dt.timedelta(hours=12)  # 出發後幾小時內開演的場次才考慮【預設】
DEFAULT_TICKET_PRICE = 300  # 影城沒設票價時（data/seed/ticket_prices.json 的預設值也是 300）
PRICE_LEVELS = ((300, "INEXPENSIVE"), (800, "MODERATE"), (1500, "EXPENSIVE"))


class PlanError(Exception):
    """規劃送不出去或做不了。code 對應規格書 6.1、6.4 的錯誤代碼。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------- 路網 ----------


def load_router(session: Session) -> Router:
    """路網：同一段、同一種交通方式有 TDX 的就不用手寫的估計值；路段有真實車資就帶上。"""
    stations = session.execute(select(Station.id, Station.code, Station.name, Station.transfer_min,
                                      lat_of(Station.geom), lng_of(Station.geom))).all()
    code_of = {sid: code for sid, code, *_ in stations}
    links = list(session.scalars(select(TransitLink)))
    real = {(l.from_station_id, l.to_station_id, l.mode) for l in links if l.source == "tdx"}
    fares = {(f.from_station_id, f.to_station_id, f.line): f.fare for f in session.scalars(select(TransitFare))}
    edges = [
        Edge(code_of[l.from_station_id], code_of[l.to_station_id], l.mode, l.line, l.ride_min, l.headway_min,
             l.first_min, l.last_min, fares.get((l.from_station_id, l.to_station_id, l.line)))
        for l in links
        if l.source == "tdx" or (l.from_station_id, l.to_station_id, l.mode) not in real
    ]
    graph = TransitGraph([Node(code, name, transfer) for _, code, name, transfer, _, _ in stations], edges)
    places = [StationPlace(code, name, lat, lng) for _, code, name, _, lat, lng in stations]
    return Router(graph, places, network_version(session))


def network_version(session: Session) -> int:
    """路網版本：最近一次成功更新路網骨架或 TDX 的 crawl_runs.id。路線快取的 key 帶著它，更新後舊快取自然過期。"""
    return session.scalar(select(func.max(CrawlRun.id)).where(CrawlRun.job.in_(["network", "tdx"]), CrawlRun.status == "success")) or 0


# ---------- 規劃要用的資料 ----------


def _minutes(day: dt.date, when: dt.datetime) -> int:
    base = dt.datetime(day.year, day.month, day.day, tzinfo=TW)
    return int((when - base).total_seconds() // 60)


def _price_level(price: int) -> str:
    return next((level for limit, level in PRICE_LEVELS if price <= limit), "VERY_EXPENSIVE")


def osm_eatery(rest: Restaurant, lat: float, lng: float, day: dt.date) -> Eatery:
    """OSM 的餐廳：營業時間、價位從標籤算（看不懂或沒標的依店的類型估計）。"""
    tags = rest.osm_tags or {}
    hours, estimated = place_hours(tags)
    span = hours.get(WEEKDAY_KEYS[day.weekday()], hours.get("default"))
    category = rest.category_code or cuisine_category(tags)
    price = estimate_price(tags.get("amenity", "restaurant"), category)
    text = "公休" if not span else f"{span[0]}–{span[1]}" + ("（估計）" if estimated else "")
    return Eatery(
        id=rest.id, place=Place(f"res{rest.id}", rest.osm_name or "（沒有店名）", lat, lng), source="osm",
        category_code=rest.category_code, open=to_min(span[0]) if span else None, close=to_min(span[1]) if span else None,
        hours_text=text, hours_estimated=estimated, price_per_person=price, price_level=_price_level(price),
        address=address(tags),
    )


def _location(session: Session, location_id: int, area: Area, role: str) -> Place:
    row = session.execute(
        select(Location, lat_of(Location.geom), lng_of(Location.geom)).where(Location.id == location_id)
    ).first()
    if row is None:
        raise PlanError("NOT_FOUND", f"找不到{role}（地標 {location_id}）")
    loc, lat, lng = row
    if loc.area_id != area.id:
        raise PlanError("VALIDATION_ERROR", f"{role}「{loc.name}」不在{area.name}")
    if lat is None:
        raise PlanError("VALIDATION_ERROR", f"{role}「{loc.name}」還沒有座標")
    return Place(f"loc{loc.id}", loc.name, lat, lng)


def last_showtime_date(session: Session, area_id: int) -> dt.date | None:
    last = session.scalar(
        select(func.max(Showtime.starts_at)).join(Cinema, Cinema.id == Showtime.cinema_id).where(Cinema.area_id == area_id)
    )
    return last.astimezone(TW).date() if last else None


def load_plan_context(session: Session, req: PlanRequest, now: dt.datetime | None = None) -> PlanContext:
    area = session.scalar(select(Area).where(Area.code == req.area))
    if area is None:
        raise PlanError("NOT_FOUND", f"沒有這個區域：{req.area}")
    origin = _location(session, req.origin_location_id, area, "出發地")
    destination = _location(session, req.destination_location_id, area, "終點")
    now = now or now_tw()
    depart_at = req.depart_at.astimezone(TW)
    if depart_at <= now:
        raise PlanError("VALIDATION_ERROR", "出發時間要晚於現在")
    last = last_showtime_date(session, area.id)
    if last is None:
        raise PlanError("NO_SHOWTIMES", f"{area.name}還沒有場次資料")
    if depart_at.date() > last:
        raise PlanError("VALIDATION_ERROR", f"{area.name}的場次資料只到 {last.isoformat()}")

    day = depart_at.date()
    query = (
        select(Showtime, Movie, Cinema, lat_of(Cinema.geom), lng_of(Cinema.geom))
        .join(Movie, Movie.id == Showtime.movie_id).join(Cinema, Cinema.id == Showtime.cinema_id)
        .where(Cinema.area_id == area.id, Cinema.is_active, Cinema.geom.is_not(None),
               Showtime.starts_at > depart_at, Showtime.starts_at <= depart_at + PLAN_WINDOW)
        .order_by(Showtime.starts_at, Showtime.id)
    )
    if req.movie_genre:
        query = query.where(Movie.genre_code == req.movie_genre)
    showtimes = [
        ShowInfo(st.id, Place(f"cin{cin.id}", cin.name, lat, lng), movie.title, st.version,
                 _minutes(day, st.starts_at), _minutes(day, st.ends_at), cin.ticket_price or DEFAULT_TICKET_PRICE)
        for st, movie, cin, lat, lng in session.execute(query)
    ]

    rq = select(Restaurant, lat_of(Restaurant.geom), lng_of(Restaurant.geom)).where(
        Restaurant.area_id == area.id, Restaurant.is_active, Restaurant.source == "osm")
    if req.cuisine:
        rq = rq.where(Restaurant.category_code == req.cuisine)
    eateries = [osm_eatery(r, lat, lng, day) for r, lat, lng in session.execute(rq.order_by(Restaurant.id))]

    return PlanContext(
        day=day, depart=_minutes(day, depart_at), origin=origin, destination=destination, order=req.order,
        party_size=req.party_size, budget_total=req.budget_total, showtimes=showtimes, eateries=eateries,
        router=load_router(session), restaurant_source="osm",
    )

