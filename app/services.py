"""服務層：從資料庫讀資料、準備 Jev 判斷與交通路網，再交給 planner 計算。

REST API（app/api.py）和 MCP 工具（app/mcp_server.py）都呼叫這裡，兩邊的結果一定一致。
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import planner
from .config import settings
from .geo import walk_minutes
from .jev import (
    AUDIO,
    CUISINE,
    DATE_FIT,
    DIETS,
    GENRES,
    MEAL_MINUTES,
    MEAL_PACE,
    SCREEN,
    TITLE_MATCH,
    JevService,
    diet_question,
)
from .jev.service import label_subject, restaurant_context, restaurant_subject, title_subject
from .models import Area, Cinema, Movie, Restaurant, ScrapeRun, Showtime, Station, TransitLink
from .planner import Place, PlanContext, RestaurantInfo, ShowtimeInfo
from .regions import TAIWAN_CITIES
from .schemas import PlanRequest
from .timeutil import WEEKDAY_KEYS, fmt, to_min, today_tw, weekday_label
from .transit import Edge, Node, TransitGraph

STATION_ALIASES = {"北車": "台北車站", "台北101": "台北101/世貿", "101": "台北101/世貿"}
# 和本島之間沒有當天往返的大眾運輸（金門暫時沒有商圈：OSM 在影城附近沒有餐廳資料）
ISLAND_CITIES = ("澎湖縣",)


def _place(row) -> Place:
    return Place(row.id, row.name, row.lat, row.lng)


def _save_judgments(session: Session) -> None:
    """Jev 判斷只是快取，同時有兩個請求寫入同一筆而衝突時，放掉就好。"""
    try:
        session.commit()
    except IntegrityError:
        session.rollback()


def resolve_station(session: Session, value: str) -> Station | None:
    """用代碼或站名找車站，「板橋站」「板橋」「ntp-banqiao」都可以。站名重複（例如兩個市政府）時要用代碼。"""
    raw = value.strip().replace("臺", "台")
    if station := session.get(Station, raw):
        return station
    for name in (raw, raw.removesuffix("站"), raw.removesuffix("車站"), raw + "站"):
        name = STATION_ALIASES.get(name, name)
        matches = list(session.scalars(select(Station).where(Station.name == name)))
        if len(matches) > 1:
            options = "、".join(f"{s.city}{s.name}（{s.id}）" for s in matches)
            raise ValueError(f"有不只一個「{name}」站，請改用代碼：{options}")
        if matches:
            return matches[0]
    return None


def hours_for(hours: dict, day: dt.date) -> tuple[int, int] | None:
    """某天的營業時間（分鐘數）；公休回傳 None。"""
    key = WEEKDAY_KEYS[day.weekday()]
    span = hours[key] if key in hours else hours.get("default")
    if not span:
        return None
    return to_min(span[0]), to_min(span[1])


def load_graph(session: Session) -> TransitGraph:
    nodes = [Node(s.id, s.name, s.transfer_min) for s in session.scalars(select(Station))]
    edges = [
        Edge(
            link.from_station_id, link.to_station_id, link.mode, link.line, link.ride_min, link.headway_min,
            to_min(link.first_train), to_min(link.last_train),
        )
        for link in session.scalars(select(TransitLink))
    ]
    return TransitGraph(nodes, edges)


def _area_and_origin(session: Session, area_id: str, origin: str) -> tuple[Area, Station, list[Station]]:
    area = session.get(Area, area_id)
    if area is None:
        raise ValueError(f"還沒有「{area_id}」這個商圈的資料")
    station = resolve_station(session, origin)
    if station is None:
        raise ValueError(f"找不到出發站「{origin}」")
    area_stations = list(session.scalars(select(Station).where(Station.area_id == area.id)))
    if not area_stations:
        raise ValueError(f"「{area.city}{area.name}」沒有可以抵達的車站資料")
    return area, station, area_stations


def build_context(session: Session, req: PlanRequest, jev: JevService) -> tuple[PlanContext, dict]:
    area, origin, area_stations = _area_and_origin(session, req.area, req.origin)
    if not origin.is_origin:
        raise ValueError(f"「{origin.name}」不能當出發站")
    graph = load_graph(session)
    if not any(graph.reachable(origin.id, s.id) for s in area_stations) or not any(
        graph.reachable(s.id, origin.id) for s in area_stations
    ):
        if origin.city != area.city and (origin.city in ISLAND_CITIES or area.city in ISLAND_CITIES):
            raise ValueError("澎湖只能從島上出發，請改選島上的出發站（例如馬公總站）")
        raise ValueError(f"目前沒有從{origin.city}{planner.station_name(origin.name)}到{area.city}{area.name}的交通資料")

    rows = session.execute(
        select(Showtime, Cinema, Movie)
        .join(Cinema, Showtime.cinema_id == Cinema.id)
        .outerjoin(Movie, Showtime.movie_id == Movie.id)
        .where(Showtime.date == req.date.isoformat(), Cinema.area_id == area.id)
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
                genre=movie.genre,
                synopsis=movie.synopsis,
                estimated=list(st.estimated or []),
            )
        )

    diet = diet_question(req.diet) if req.diet else None
    restaurants, closed = [], 0
    for r in session.scalars(select(Restaurant).where(Restaurant.area_id == area.id)):
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
                description=r.description,
                reviews=list(r.reviews or []),
                diet_ok=(
                    jev.ask(diet, f"{subject}|diet:{req.diet}", {**context, "diet": req.diet}).apply_override(ov)
                    if diet
                    else None
                ),
                estimated=list(r.estimated or []),
            )
        )

    ctx = PlanContext(
        origin=_place(origin),
        area_stations=[_place(s) for s in area_stations],
        graph=graph,
        showtimes=showtimes,
        restaurants=restaurants,
        weekend=req.date.weekday() >= 4,  # 週五晚上到週日
        threshold=settings.jev_confidence_threshold,
        ask=lambda question, subject, context: jev.ask(question, subject, context).apply_override(ov),
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


def search_showtimes(session: Session, date: dt.date, area: str = "台北市/信義", movie_query: str = "") -> list[dict]:
    jev = JevService(session)
    rows = session.execute(
        select(Showtime, Cinema, Movie)
        .join(Cinema, Showtime.cinema_id == Cinema.id)
        .outerjoin(Movie, Showtime.movie_id == Movie.id)
        .where(Showtime.date == date.isoformat(), Cinema.area_id == area)
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
            "estimated": list(st.estimated or []),
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
    for r in session.scalars(select(Restaurant).where(Restaurant.area_id == cinema.area_id)):
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
            "estimated": list(r.estimated or []),
        }
        if open_at:
            t = to_min(open_at)
            item["can_order_at_time"] = bool(span and span[0] <= t <= span[1] - r.last_order_min)
        out.append(item)
    _save_judgments(session)
    return sorted(out, key=lambda x: x["walk_min"])


def check_route(session: Session, origin: str, area: str = "台北市/信義", depart_at: str = "18:00") -> list[dict]:
    """從出發站在 depart_at 出發，到商圈各車站最早幾點到、怎麼轉乘。"""
    _, station, area_stations = _area_and_origin(session, area, origin)
    graph = load_graph(session)
    start = to_min(depart_at or "18:00")
    out = []
    for target in area_stations:
        route = graph.earliest_arrival(station.id, start, target.id)
        if route is None:
            continue
        out.append(
            {
                "to": target.name,
                "arrive": fmt(route.arrive),
                "legs": [
                    {"line": leg.label, "from": leg.from_name, "to": leg.to_name, "board": fmt(leg.board), "alight": fmt(leg.alight)}
                    for leg in route.legs
                ],
            }
        )
    return out


SOURCE_LABELS = {"atmovies": "開眼", "osm": "OSM", "base": "商圈與交通", "fixture": "示意資料"}


def data_status(session: Session) -> dict:
    runs = session.scalars(select(ScrapeRun).where(ScrapeRun.status == "ok").order_by(ScrapeRun.finished_at.desc())).all()
    latest: dict[str, ScrapeRun] = {}
    for run in runs:
        latest.setdefault(run.source, run)
    sources = [
        {
            "source": run.source,
            "label": SOURCE_LABELS.get(run.source, run.source),
            "finished_at": run.finished_at.strftime("%Y-%m-%d %H:%M") if run.finished_at else None,
        }
        for run in latest.values()
    ]
    has_showtimes = bool(
        session.scalar(select(func.count()).select_from(Showtime).where(Showtime.date >= today_tw().isoformat()))
    )
    return {"sources": sources, "is_demo": "fixture" in latest, "has_showtimes": has_showtimes}


def available_dates(session: Session) -> dict[str, list[dict]]:
    """每個商圈今天以後有場次的日期，以及當天有上映的電影（給網頁的下拉選單用）。"""
    rows = session.execute(
        select(Cinema.area_id, Showtime.date, Movie.title)
        .join(Cinema, Showtime.cinema_id == Cinema.id)
        .join(Movie, Showtime.movie_id == Movie.id)
        .where(Showtime.date >= today_tw().isoformat())
        .distinct()
    ).all()
    grouped: dict[str, dict[str, set[str]]] = {}
    for area, day, title in rows:
        grouped.setdefault(area, {}).setdefault(day, set()).add(title)
    return {
        area: [
            {"date": day, "label": weekday_label(dt.date.fromisoformat(day)), "movies": sorted(titles)}
            for day, titles in sorted(days.items())
        ]
        for area, days in grouped.items()
    }


def list_cities(session: Session) -> list[dict]:
    """全台 22 縣市，附上各縣市有資料的商圈（含可選日期）與出發站。"""
    dates = available_dates(session)
    areas = session.scalars(select(Area).order_by(Area.id)).all()
    # 轉乘時間長的通常是大站（台北車站、左營），排前面當預設出發站
    origins = session.scalars(
        select(Station).where(Station.is_origin.is_(True)).order_by(Station.transfer_min.desc(), Station.id)
    ).all()
    cities = []
    for region, city in TAIWAN_CITIES:
        city_areas = [{"id": a.id, "name": a.name, "dates": dates[a.id]} for a in areas if a.city == city and a.id in dates]
        city_origins = [{"id": s.id, "name": s.name} for s in origins if s.city == city]
        cities.append({"name": city, "region": region, "areas": city_areas, "origins": city_origins})
    return cities


def meta(session: Session) -> dict:
    cities = list_cities(session)
    return {
        "today": today_tw().isoformat(),
        "cities": cities,
        "areas": [{"id": a["id"], "name": a["name"], "city": c["name"]} for c in cities for a in c["areas"]],
        "origins": [{"id": o["id"], "name": o["name"], "city": c["name"]} for c in cities for o in c["origins"]],
        "movies": sorted(set(session.scalars(select(Movie.title)))),
        "options": {
            "screen_types": list(SCREEN.options),
            "audio": list(AUDIO.options),
            "cuisines": list(CUISINE.options),
            "genres": list(GENRES),
            "diets": list(DIETS),
            "meal_minutes": MEAL_MINUTES,
        },
        "counts": {
            "cinemas": session.scalar(select(func.count()).select_from(Cinema)),
            "restaurants": session.scalar(select(func.count()).select_from(Restaurant)),
        },
        "jev": {"mode": settings.jev_mode, "threshold": settings.jev_confidence_threshold},
        "llm": {"model": settings.ollama_model},
        "data": data_status(session),
    }
