"""服務層：從資料庫讀資料、準備 Jev 判斷與交通路網，再交給 planner 計算。

REST API（app/api.py）和 MCP 工具（app/mcp_server.py）都呼叫這裡，兩邊的結果一定一致。
"""
from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable

from sqlalchemy import func, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import google_places, planner
from .collectors.base import RestaurantRecord
from .collectors.coverage import load_coverage
from .config import settings
from .geo import DETOUR, WALK_M_PER_MIN, walk_minutes
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
from .jev.assist import PLAN_BUDGET, LlmAssist
from .jev.service import label_subject, place_subject, restaurant_context, restaurant_subject, title_subject
from .models import Area, Cinema, Movie, Restaurant, ScrapeRun, Showtime, Station, TransitLink
from .planner import Place, PlanContext, RestaurantInfo, ShowtimeInfo
from .regions import TAIWAN_CITIES
from .schemas import PlanRequest
from .scope import active_cities, is_open, require_open, require_open_area
from .timeutil import WEEKDAY_KEYS, fmt, to_min, today_tw, weekday_label
from .transit import Edge, Node, TransitGraph

STATION_ALIASES = {
    "北車": "台北車站", "台北101": "台北101/世貿", "101": "台北101/世貿", "八德": "八德（介壽大智路口）",
}
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


PlacesFetch = Callable[[google_places.PlacesClient], list[RestaurantRecord]]


def load_restaurants(
    session: Session, area_id: str, fetch: PlacesFetch
) -> tuple[list[tuple[int | str, str, Restaurant | RestaurantRecord]], dict]:
    """商圈的餐廳：[(編號, Jev 判斷的對象, 餐廳)]，以及 {"source": "google" 或 "osm", "note": 給使用者的提示}。

    有 Google 金鑰就用 fetch 即時查 Google（不存進資料庫）；沒有金鑰、查詢失敗或超過每日上限時，
    用資料庫裡的餐廳（OSM；示意資料模式下是示意資料）。
    """
    note = ""
    if settings.use_google_places:
        try:
            with google_places.make_client() as client:
                records = fetch(client)
        except google_places.PlacesError as exc:
            note = f"Google 地圖查詢失敗（{exc}），這次改用 OpenStreetMap 的餐廳資料。"
        else:
            return [(r.external_id, place_subject(r.external_id), r) for r in records], {"source": "google", "note": ""}
    rows = session.scalars(select(Restaurant).where(Restaurant.area_id == area_id)).all()
    return [(r.id, restaurant_subject(r), r) for r in rows], {"source": "osm", "note": note}


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
    require_open(area.city)
    station = resolve_station(session, origin)
    if station is None:
        raise ValueError(f"找不到出發站「{origin}」")
    require_open(station.city)
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

    # 先查餐廳：Google 的每日次數計數會 commit，這時 session 裡還沒有待寫入的 Jev 判斷
    def fetch(client: google_places.PlacesClient) -> list[RestaurantRecord]:
        return google_places.area_restaurants(session, load_coverage(session, [area.city]).get(area.id), client)

    restaurant_rows, restaurant_source = load_restaurants(session, area.id, fetch)

    rows = session.execute(
        select(Showtime, Cinema, Movie)
        .join(Cinema, Showtime.cinema_id == Cinema.id)
        .outerjoin(Movie, Showtime.movie_id == Movie.id)
        .where(Showtime.date == req.date.isoformat(), Cinema.area_id == area.id)
        .order_by(Showtime.start)
    ).all()

    ov = req.overrides
    # Jev 沒把握時請 LLM 幫忙（JEV_LLM_ASSIST）：
    # - 固定的題目（影廳、語言、約會適合度、料理）在匯入時先問過，這裡只讀存好的答案，不多花時間
    # - 跟這次條件有關的題目（片名比對、飲食限制）最多問 JEV_LLM_ASSIST_MAX_CALLS 題
    # - 選片、選餐廳、選車（planner 經由 ctx.ask 問）不限題數，每次排程最多 10 題
    # - 一次排程等 LLM 的時間合計最多 PLAN_BUDGET 秒，超過的照舊請使用者確認
    deadline = time.monotonic() + PLAN_BUDGET
    stored = LlmAssist(session, max_calls=0)
    request_specific = LlmAssist(session, max_calls=settings.jev_llm_assist_max_calls, deadline=deadline)
    choices = LlmAssist(session, deadline=deadline)
    showtimes, without_movie = [], 0
    for st, cinema, movie in rows:
        if movie is None:  # 對不到電影就不知道片長，先跳過
            without_movie += 1
            continue
        title_match = None
        if req.movie_query:
            title_match = request_specific.check(jev.ask(
                TITLE_MATCH, title_subject(st.raw_title, req.movie_query), {"query": req.movie_query, "title": st.raw_title}
            ).apply_override(ov))
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
                screen=stored.check(jev.ask(SCREEN, label_subject(st.label), {"label": st.label}).apply_override(ov)),
                audio=stored.check(jev.ask(AUDIO, label_subject(st.label), {"label": st.label}).apply_override(ov)),
                title_match=title_match,
                genre=movie.genre,
                synopsis=movie.synopsis,
                estimated=list(st.estimated or []),
            )
        )

    diet = diet_question(req.diet) if req.diet else None
    restaurants, closed = [], 0
    for rid, subject, r in restaurant_rows:
        span = hours_for(r.hours, req.date)
        if span is None:
            closed += 1
            continue
        context = restaurant_context(r)
        restaurants.append(
            RestaurantInfo(
                place=Place(rid, r.name, r.lat, r.lng),
                price=r.price_per_person,
                open=span[0],
                close=span[1],
                last_order_min=r.last_order_min,
                typical_wait_min=r.typical_wait_min,
                reservable=r.reservable,
                date_fit=stored.check(jev.ask(DATE_FIT, subject, context).apply_override(ov)),
                pace=jev.ask(MEAL_PACE, subject, context).apply_override(ov),
                cuisine=stored.check(jev.ask(CUISINE, subject, context).apply_override(ov)),
                description=r.description,
                reviews=list(r.reviews or []),
                diet_ok=(
                    request_specific.check(
                        jev.ask(diet, f"{subject}|diet:{req.diet}", {**context, "diet": req.diet}).apply_override(ov)
                    )
                    if diet
                    else None
                ),
                estimated=list(r.estimated or []),
                url=r.source_url,
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
        ask=lambda question, subject, context: choices.check(jev.ask(question, subject, context).apply_override(ov)),
    )
    return ctx, {
        "showtimes_without_movie": without_movie, "restaurants_closed": closed, "restaurant_source": restaurant_source,
        "llm_assists": (stored, request_specific, choices),
    }


def plan_date(session: Session, req: PlanRequest) -> dict:
    jev = JevService(session)
    ctx, extra = build_context(session, req, jev)
    result = planner.plan(req, ctx)
    _save_judgments(session)
    result["restaurant_source"] = extra.pop("restaurant_source")
    llm_calls = sum(a.calls for a in extra.pop("llm_assists"))  # planner 問完選擇題才算得出來
    result["stats"].update(extra)
    result["request"] = req.model_dump(mode="json")
    result["jev"] = {
        "mode": jev.backend.name, "threshold": ctx.threshold, "new_calls": jev.calls,
        "llm_assist": settings.jev_llm_assist and bool(settings.ollama_url), "llm_calls": llm_calls,
    }
    result["data"] = data_status(session)
    return result


def search_showtimes(session: Session, date: dt.date, area: str = "台北市/信義", movie_query: str = "") -> list[dict]:
    require_open_area(area)
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
    require_open_area(cinema.area_id)
    day = date or today_tw()
    radius_m = round(max(1, max_walk_min) * WALK_M_PER_MIN / DETOUR)  # 步行 max_walk_min 分鐘的直線距離
    rows, source = load_restaurants(
        session,
        cinema.area_id,
        lambda client: google_places.nearby_restaurants(session, cinema, radius_m, cinema.area_id, client),
    )
    jev = JevService(session)
    out = []
    for rid, subject, r in rows:
        walk = walk_minutes(cinema, r)
        if walk > max_walk_min:
            continue
        span = hours_for(r.hours, day)
        context = restaurant_context(r)
        fit = jev.ask(DATE_FIT, subject, context)
        item = {
            "id": rid,
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
            "url": r.source_url,
            "source": source["source"],
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


def _in_open_area(area_id_column):
    """資料列的商圈在開放範圍內（見 app/scope.py）；沒設範圍就不限。"""
    cities = active_cities()
    return area_id_column.in_(select(Area.id).where(Area.city.in_(cities))) if cities else true()


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
        session.scalar(
            select(func.count())
            .select_from(Showtime)
            .join(Cinema, Showtime.cinema_id == Cinema.id)
            .where(Showtime.date >= today_tw().isoformat(), _in_open_area(Cinema.area_id))
        )
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
    """開放範圍內的縣市（ACTIVE_CITIES，預設桃園市），附上各縣市有資料的商圈（含可選日期）與出發站。"""
    dates = available_dates(session)
    areas = session.scalars(select(Area).order_by(Area.id)).all()
    # 轉乘時間長的通常是大站（台北車站、左營），排前面當預設出發站
    origins = session.scalars(
        select(Station).where(Station.is_origin.is_(True)).order_by(Station.transfer_min.desc(), Station.id)
    ).all()
    cities = []
    for region, city in TAIWAN_CITIES:
        if not is_open(city):
            continue
        city_areas = [{"id": a.id, "name": a.name, "dates": dates[a.id]} for a in areas if a.city == city and a.id in dates]
        city_origins = [{"id": s.id, "name": s.name, "area": s.area_id} for s in origins if s.city == city]
        cities.append({"name": city, "region": region, "areas": city_areas, "origins": city_origins})
    return cities


def _open_movie_titles(session: Session) -> list[str]:
    """片名清單（給網頁與聊天比對）；有設開放範圍時，只列開放縣市的影城有場次的片。"""
    query = select(Movie.title)
    if active_cities():
        query = (
            query.join(Showtime, Showtime.movie_id == Movie.id)
            .join(Cinema, Showtime.cinema_id == Cinema.id)
            .where(_in_open_area(Cinema.area_id))
        )
    return sorted(set(session.scalars(query)))


def meta(session: Session) -> dict:
    cities = list_cities(session)
    return {
        "today": today_tw().isoformat(),
        "cities": cities,
        "areas": [{"id": a["id"], "name": a["name"], "city": c["name"]} for c in cities for a in c["areas"]],
        "origins": [{**o, "city": c["name"]} for c in cities for o in c["origins"]],
        "movies": _open_movie_titles(session),
        "options": {
            "screen_types": list(SCREEN.options),
            "audio": list(AUDIO.options),
            "cuisines": list(CUISINE.options),
            "genres": list(GENRES),
            "diets": list(DIETS),
            "meal_minutes": MEAL_MINUTES,
        },
        "counts": {
            "cinemas": session.scalar(select(func.count()).select_from(Cinema).where(_in_open_area(Cinema.area_id))),
            "restaurants": session.scalar(
                select(func.count()).select_from(Restaurant).where(_in_open_area(Restaurant.area_id))
            ),
        },
        "jev": {"mode": settings.jev_mode, "threshold": settings.jev_confidence_threshold},
        "llm": {"model": settings.ollama_model},
        # google：排程時即時查 Google（counts.restaurants 是查詢失敗時備用的 OSM 餐廳數）；osm：用資料庫裡的餐廳
        "restaurants": {"source": "google" if settings.use_google_places else "osm"},
        "data": data_status(session),
    }
