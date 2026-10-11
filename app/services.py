"""服務層：從資料庫讀資料、準備 Jev 判斷，再交給 planner 計算。

REST API（app/api.py）和 MCP 工具（app/mcp_server.py）都呼叫這裡，兩邊的結果一定一致。
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import planner
from .config import settings
from .geo import walk_minutes
from .jev import AUDIO, CUISINE, DATE_FIT, MEAL_MINUTES, MEAL_PACE, SCREEN, TITLE_MATCH, JevService
from .jev.service import label_subject, restaurant_context, restaurant_subject, title_subject
from .models import Cinema, Movie, Restaurant, ScrapeRun, Showtime, Station, TransitLink
from .planner import LinkInfo, Place, PlanContext, RestaurantInfo, ShowtimeInfo
from .schemas import PlanRequest
from .timeutil import WEEKDAY_KEYS, fmt, to_min, today_tw, weekday_label

STATION_ALIASES = {"北車": "台北車站", "台北101": "台北101/世貿", "101": "台北101/世貿"}


def _place(row) -> Place:
    return Place(row.id, row.name, row.lat, row.lng)


def _save_judgments(session: Session) -> None:
    """Jev 判斷只是快取，同時有兩個請求寫入同一筆而衝突時，放掉就好。"""
    try:
        session.commit()
    except IntegrityError:
        session.rollback()


def resolve_station(session: Session, value: str) -> Station | None:
    """用代碼或站名找捷運站，「板橋站」「板橋」「BL07」都可以。"""
    raw = value.strip().replace("臺", "台")
    for name in (raw, raw.removesuffix("站"), raw + "站"):
        name = STATION_ALIASES.get(name, name)
        station = session.get(Station, name) or session.scalar(select(Station).where(Station.name == name))
        if station:
            return station
    return None


def hours_for(hours: dict, day: dt.date) -> tuple[int, int] | None:
    """某天的營業時間（分鐘數）；公休回傳 None。"""
    key = WEEKDAY_KEYS[day.weekday()]
    span = hours[key] if key in hours else hours.get("default")
    if not span:
        return None
    return to_min(span[0]), to_min(span[1])


def _origins(session: Session) -> list[Station]:
    return list(session.scalars(select(Station).where(Station.is_origin.is_(True)).order_by(Station.id)))


def build_context(session: Session, req: PlanRequest, jev: JevService) -> tuple[PlanContext, dict]:
    origin = resolve_station(session, req.origin)
    if origin is None or not origin.is_origin:
        names = "、".join(s.name for s in _origins(session))
        raise ValueError(f"找不到出發站「{req.origin}」，目前可以選：{names}")
    area_stations = list(session.scalars(select(Station).where(Station.area == req.area)))
    if not area_stations:
        raise ValueError(f"還沒有「{req.area}」商圈的資料")

    links = {
        (link.from_station_id, link.to_station_id): LinkInfo(
            link.ride_min, link.headway_min, to_min(link.first_train), to_min(link.last_train)
        )
        for link in session.scalars(select(TransitLink))
    }

    rows = session.execute(
        select(Showtime, Cinema, Movie)
        .join(Cinema, Showtime.cinema_id == Cinema.id)
        .outerjoin(Movie, Showtime.movie_id == Movie.id)
        .where(Showtime.date == req.date.isoformat(), Cinema.area == req.area)
        .order_by(Showtime.start)
    ).all()

    ov = req.overrides
    showtimes, without_movie = [], 0
    for st, cinema, movie in rows:
        if movie is None:  # 對不到電影就不知道片長，先跳過
            without_movie += 1
            continue
        title_match = None
        if req.movie_query:
            title_match = jev.ask(
                TITLE_MATCH, title_subject(st.raw_title, req.movie_query), {"query": req.movie_query, "title": st.raw_title}
            ).apply_override(ov)
        showtimes.append(
            ShowtimeInfo(
                id=st.id,
                cinema=_place(cinema),
                movie_title=movie.title,
                raw_title=st.raw_title,
                runtime_min=movie.runtime_min,
                start=to_min(st.start),
                label=st.label,
                price=st.price,
                screen=jev.ask(SCREEN, label_subject(st.label), {"label": st.label}).apply_override(ov),
                audio=jev.ask(AUDIO, label_subject(st.label), {"label": st.label}).apply_override(ov),
                title_match=title_match,
            )
        )

    restaurants, closed = [], 0
    for r in session.scalars(select(Restaurant).where(Restaurant.area == req.area)):
        span = hours_for(r.hours, req.date)
        if span is None:
            closed += 1
            continue
        subject, context = restaurant_subject(r), restaurant_context(r)
        restaurants.append(
            RestaurantInfo(
                place=_place(r),
                price=r.price_per_person,
                open=span[0],
                close=span[1],
                last_order_min=r.last_order_min,
                typical_wait_min=r.typical_wait_min,
                reservable=r.reservable,
                date_fit=jev.ask(DATE_FIT, subject, context).apply_override(ov),
                pace=jev.ask(MEAL_PACE, subject, context).apply_override(ov),
                cuisine=jev.ask(CUISINE, subject, context).apply_override(ov),
            )
        )

    ctx = PlanContext(
        origin=_place(origin),
        area_stations=[_place(s) for s in area_stations],
        links=links,
        showtimes=showtimes,
        restaurants=restaurants,
        weekend=req.date.weekday() >= 4,  # 週五晚上到週日
        threshold=settings.jev_confidence_threshold,
    )
    return ctx, {"showtimes_without_movie": without_movie, "restaurants_closed": closed}


def plan_date(session: Session, req: PlanRequest) -> dict:
    jev = JevService(session)
    ctx, skipped = build_context(session, req, jev)
    result = planner.plan(req, ctx)
    _save_judgments(session)
    result["stats"].update(skipped)
    result["request"] = req.model_dump(mode="json")
    result["jev"] = {"mode": jev.backend.name, "threshold": ctx.threshold, "new_calls": jev.calls}
    result["data"] = data_status(session)
    return result


def search_showtimes(session: Session, date: dt.date, area: str = "信義", movie_query: str = "") -> list[dict]:
    jev = JevService(session)
    rows = session.execute(
        select(Showtime, Cinema, Movie)
        .join(Cinema, Showtime.cinema_id == Cinema.id)
        .outerjoin(Movie, Showtime.movie_id == Movie.id)
        .where(Showtime.date == date.isoformat(), Cinema.area == area)
        .order_by(Showtime.start)
    ).all()
    out = []
    for st, cinema, movie in rows:
        item = {
            "id": st.id,
            "cinema_id": cinema.id,
            "cinema": cinema.name,
            "title": st.raw_title,
            "start": st.start,
            "label": st.label,
            "price": st.price,
            "runtime_min": movie.runtime_min if movie else None,
            "screen": jev.ask(SCREEN, label_subject(st.label), {"label": st.label}).top,
            "audio": jev.ask(AUDIO, label_subject(st.label), {"label": st.label}).top,
        }
        if movie_query:
            match = jev.ask(TITLE_MATCH, title_subject(st.raw_title, movie_query), {"query": movie_query, "title": st.raw_title})
            if match.prob("是") < 0.3:
                continue
            item["title_match"] = round(match.prob("是"), 2)
        out.append(item)
    _save_judgments(session)
    return out


def find_restaurants(
    session: Session, cinema_id: int, max_walk_min: int = 10, date: dt.date | None = None, open_at: str = ""
) -> list[dict]:
    cinema = session.get(Cinema, cinema_id)
    if cinema is None:
        raise ValueError(f"找不到影城 {cinema_id}")
    day = date or today_tw()
    jev = JevService(session)
    out = []
    for r in session.scalars(select(Restaurant).where(Restaurant.area == cinema.area)):
        walk = walk_minutes(cinema, r)
        if walk > max_walk_min:
            continue
        span = hours_for(r.hours, day)
        subject, context = restaurant_subject(r), restaurant_context(r)
        fit = jev.ask(DATE_FIT, subject, context)
        item = {
            "id": r.id,
            "name": r.name,
            "walk_min": walk,
            "price_per_person": r.price_per_person,
            "hours": f"{fmt(span[0])}–{fmt(span[1])}" if span else "公休",
            "last_order": fmt(span[1] - r.last_order_min) if span else None,
            "reservable": r.reservable,
            "cuisine": jev.ask(CUISINE, subject, context).top,
            "pace": jev.ask(MEAL_PACE, subject, context).top,
            "date_fit": fit.top,
            "date_fit_confidence": round(fit.confidence, 2),
        }
        if open_at:
            t = to_min(open_at)
            item["can_order_at_time"] = bool(span and span[0] <= t <= span[1] - r.last_order_min)
        out.append(item)
    _save_judgments(session)
    return sorted(out, key=lambda x: x["walk_min"])


def check_route(session: Session, origin: str, area: str = "信義", depart_at: str = "") -> list[dict]:
    station = resolve_station(session, origin)
    if station is None:
        raise ValueError(f"找不到出發站「{origin}」")
    out = []
    for target in session.scalars(select(Station).where(Station.area == area)):
        go = session.scalar(
            select(TransitLink).where(TransitLink.from_station_id == station.id, TransitLink.to_station_id == target.id)
        )
        back = session.scalar(
            select(TransitLink).where(TransitLink.from_station_id == target.id, TransitLink.to_station_id == station.id)
        )
        if go is None:
            continue
        item = {
            "to": target.name,
            "ride_min": go.ride_min,
            "headway_min": go.headway_min,
            "note": go.note,
            "last_train_back": fmt(to_min(back.last_train)) if back else None,
        }
        if depart_at:
            item["latest_arrival"] = fmt(to_min(depart_at) + go.headway_min + go.ride_min)
        out.append(item)
    return out


def data_status(session: Session) -> dict:
    runs = session.scalars(select(ScrapeRun).where(ScrapeRun.status == "ok").order_by(ScrapeRun.finished_at.desc())).all()
    latest: dict[str, ScrapeRun] = {}
    for run in runs:
        latest.setdefault(run.source, run)
    sources = [
        {"source": run.source, "finished_at": run.finished_at.strftime("%Y-%m-%d %H:%M") if run.finished_at else None}
        for run in latest.values()
    ]
    return {"sources": sources, "is_demo": "fixture" in latest}


def available_dates(session: Session) -> list[dict]:
    """今天以後有場次的日期，以及當天有上映的電影（給網頁的下拉選單用）。"""
    rows = session.execute(
        select(Showtime.date, Movie.title)
        .join(Movie, Showtime.movie_id == Movie.id)
        .where(Showtime.date >= today_tw().isoformat())
        .distinct()
    ).all()
    by_date: dict[str, set[str]] = {}
    for day, title in rows:
        by_date.setdefault(day, set()).add(title)
    return [
        {"date": day, "label": weekday_label(dt.date.fromisoformat(day)), "movies": sorted(titles)}
        for day, titles in sorted(by_date.items())
    ]


def meta(session: Session) -> dict:
    return {
        "today": today_tw().isoformat(),
        "dates": available_dates(session),
        "origins": [{"id": s.id, "name": s.name} for s in _origins(session)],
        "areas": sorted({a for a in session.scalars(select(Station.area).where(Station.area.is_not(None)))}),
        "movies": sorted({m for m in session.scalars(select(Movie.title))}),
        "options": {
            "screen_types": list(SCREEN.options),
            "audio": list(AUDIO.options),
            "cuisines": list(CUISINE.options),
            "meal_minutes": MEAL_MINUTES,
        },
        "jev": {"mode": settings.jev_mode, "threshold": settings.jev_confidence_threshold},
        "llm": {"model": settings.ollama_model},
        "data": data_status(session),
    }
