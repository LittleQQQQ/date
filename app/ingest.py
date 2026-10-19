"""匯入：執行一個資料來源，把結果寫進資料庫，再讓 Jev 預先判斷新資料或有變動的資料。

用法：
    python -m app.ingest                      # 預設：商圈與交通（base）
    python -m app.ingest --source atmovies    # 開眼電影網：影城、電影、場次
    python -m app.ingest --source osm         # OpenStreetMap：餐廳
    python -m app.ingest --source tdx         # 交通部 TDX：台鐵、公車的班次與票價（要在 .env 填金鑰）
    python -m app.ingest --source fixture     # 完整的虛構示意資料（測試用）
    python -m app.ingest --no-jev             # 只匯入，不跑 Jev
    python -m app.ingest --reset              # 資料表結構改過時用：清空並重建所有資料表後再匯入
"""
from __future__ import annotations

import argparse
import datetime as dt
from typing import TypeVar

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .collectors import Batch, Collector, get_collector
from .collectors.base import ScrapeError
from .db import Base, SessionLocal, engine, schema_message, schema_problems
from .config import settings
from .jev import AUDIO, CUISINE, DATE_FIT, GENRE, MEAL_PACE, SCREEN, JevService
from .jev.mock import normalize_title
from .jev.assist import LlmAssist
from .jev.service import label_subject, movie_context, movie_subject, restaurant_context, restaurant_subject
from .models import (
    TDX_SOURCE, Area, Cinema, Movie, Restaurant, ScrapeRun, Showtime, Station, TransitFare, TransitLink,
)
from .scope import is_open
from .timeutil import now_tw, today_tw

T = TypeVar("T", Cinema, Movie, Restaurant)

# 來源這次給空值時，保留資料庫裡原有的值：開眼沒有片型（由 Jev 補上）、簡介偶爾抓不到，不能因此被清空
KEEP_IF_EMPTY = {Movie: ("genre", "synopsis")}


def _find(session: Session, model: type[T], source: str, external_id: str) -> T | None:
    return session.scalar(select(model).where(model.source == source, model.external_id == external_id))


def _upsert(session: Session, model: type[T], source: str, record, fetched_at: dt.datetime) -> T:
    row = _find(session, model, source, record.external_id)
    if row is None:
        row = model(source=source, external_id=record.external_id)
        session.add(row)
    keep = KEEP_IF_EMPTY.get(model, ())
    for field, value in record.model_dump().items():
        if field in keep and not value and getattr(row, field):
            continue
        setattr(row, field, value)
    row.fetched_at = fetched_at
    return row


def _link_key(row) -> tuple[str, str, str]:
    return row.from_station_id, row.to_station_id, row.line


def _remove_orphan_fares(session: Session) -> int:
    """刪掉對不到路段的車資（路段被刪了）。"""
    links = {_link_key(link) for link in session.scalars(select(TransitLink))}
    orphans = [fare for fare in session.scalars(select(TransitFare)) if _link_key(fare) not in links]
    for fare in orphans:
        session.delete(fare)
    session.flush()
    return len(orphans)


def _replace_source_links(session: Session, source: str, batch: Batch) -> dict[str, int]:
    """這批是這個來源的全部路段與車資（TDX）：同來源、這批沒有的刪掉，例如公車路線停駛了。"""
    keep_links = {_link_key(rec) for rec in batch.transit_links}
    keep_fares = {_link_key(rec) for rec in batch.transit_fares}
    links = [l for l in session.scalars(select(TransitLink).where(TransitLink.source == source)) if _link_key(l) not in keep_links]
    fares = [f for f in session.scalars(select(TransitFare).where(TransitFare.source == source)) if _link_key(f) not in keep_fares]
    for row in (*links, *fares):
        session.delete(row)
    session.flush()
    return {"stale_transit_links": len(links), "stale_transit_fares": len(fares) + _remove_orphan_fares(session)}


def _remove_stale_network(session: Session, batch: Batch) -> dict[str, int]:
    """匯入完整的商圈與交通（base）後，刪掉資料檔裡已經沒有的路段、商圈與車站。

    被刪掉的商圈裡爬蟲抓到的影城、場次、餐廳也一起刪；否則它們留在資料庫裡沒有車站可接，
    永遠排不到，還會讓頁尾的餐廳數偏多。
    """
    if not batch.areas or not batch.stations:
        # data/fixture 被清空或讀不到時，照常刪會把整個路網和爬到的資料全部清掉
        raise ScrapeError("商圈與交通資料是空的（data/fixture 可能被清空），這次不匯入、也不刪除任何資料")
    keep_links = {(l.from_station_id, l.to_station_id, l.line) for l in batch.transit_links}
    keep_pairs = {(l.from_station_id, l.to_station_id, l.mode) for l in batch.transit_links}

    def stale(link: TransitLink) -> bool:
        if link.source == TDX_SOURCE:  # TDX 的路段跟著骨架走：骨架拿掉這一段（或車站），TDX 的也刪
            return (link.from_station_id, link.to_station_id, link.mode) not in keep_pairs
        return (link.from_station_id, link.to_station_id, link.line) not in keep_links

    stale_links = [link for link in session.scalars(select(TransitLink)) if stale(link)]
    for link in stale_links:
        session.delete(link)
    session.flush()
    stale_fares = _remove_orphan_fares(session)

    stale_areas = list(session.scalars(select(Area.id).where(Area.id.not_in({a.id for a in batch.areas}))))
    cinema_ids = list(session.scalars(select(Cinema.id).where(Cinema.area_id.in_(stale_areas))))
    # 刪除順序要照外鍵：場次參照影城、影城與車站參照商圈（PostgreSQL 會檢查，順序錯就失敗）
    showtimes = session.execute(delete(Showtime).where(Showtime.cinema_id.in_(cinema_ids))).rowcount
    cinemas = session.execute(delete(Cinema).where(Cinema.id.in_(cinema_ids))).rowcount
    restaurants = session.execute(delete(Restaurant).where(Restaurant.area_id.in_(stale_areas))).rowcount
    stations = session.execute(delete(Station).where(Station.id.not_in({s.id for s in batch.stations}))).rowcount
    areas = session.execute(delete(Area).where(Area.id.in_(stale_areas))).rowcount
    session.flush()
    return {
        "stale_transit_links": len(stale_links), "stale_transit_fares": stale_fares,
        "stale_showtimes": showtimes, "stale_cinemas": cinemas,
        "stale_restaurants": restaurants, "stale_stations": stations, "stale_areas": areas,
    }


def upsert_batch(session: Session, source: str, batch: Batch) -> dict[str, int]:
    now = now_tw()
    counts: dict[str, int] = {}

    for model, records in ((Area, batch.areas), (Station, batch.stations)):
        for rec in records:
            row = session.get(model, rec.id) or model(id=rec.id)
            for field, value in rec.model_dump().items():
                setattr(row, field, value)
            row.source, row.fetched_at = source, now
            session.add(row)
        session.flush()
    counts.update(areas=len(batch.areas), stations=len(batch.stations))

    for rec in batch.transit_links:
        row = session.scalar(
            select(TransitLink).where(
                TransitLink.from_station_id == rec.from_station_id,
                TransitLink.to_station_id == rec.to_station_id,
                TransitLink.line == rec.line,
            )
        ) or TransitLink()
        for field, value in rec.model_dump().items():
            setattr(row, field, value)
        row.source, row.fetched_at = source, now
        session.add(row)
    counts["transit_links"] = len(batch.transit_links)
    for rec in batch.transit_fares:
        row = session.scalar(
            select(TransitFare).where(
                TransitFare.from_station_id == rec.from_station_id,
                TransitFare.to_station_id == rec.to_station_id,
                TransitFare.line == rec.line,
            )
        ) or TransitFare()
        for field, value in rec.model_dump().items():
            setattr(row, field, value)
        row.source, row.fetched_at = source, now
        session.add(row)
    counts["transit_fares"] = len(batch.transit_fares)
    if batch.replace_network:
        session.flush()
        counts.update(_remove_stale_network(session, batch))
    if batch.replace_source_links:
        session.flush()
        counts.update(_replace_source_links(session, source, batch))

    # 影城、餐廳要屬於資料庫裡有的商圈；爬蟲已經篩過，這裡再擋一次
    area_ids = set(session.scalars(select(Area.id)))
    outside = 0
    cinema_rows: dict[str, Cinema] = {}
    for rec in batch.cinemas:
        if rec.area_id not in area_ids:
            outside += 1
            continue
        cinema_rows[rec.external_id] = _upsert(session, Cinema, source, rec, now)
    movie_rows = {rec.external_id: _upsert(session, Movie, source, rec, now) for rec in batch.movies}
    seen_restaurants: set[str] = set()
    for rec in batch.restaurants:
        if rec.area_id not in area_ids:
            outside += 1
            continue
        _upsert(session, Restaurant, source, rec, now)
        seen_restaurants.add(rec.external_id)
    session.flush()

    removed = 0
    if batch.restaurant_areas:
        stale = select(Restaurant).where(
            Restaurant.source == source,
            Restaurant.area_id.in_(batch.restaurant_areas),
            Restaurant.external_id.not_in(seen_restaurants),
        )
        for row in session.scalars(stale).all():
            session.delete(row)
            removed += 1
    counts.update(
        cinemas=len(cinema_rows), movies=len(movie_rows), restaurants=len(seen_restaurants),
        restaurants_removed=removed, places_outside_areas=outside,
    )
    session.flush()

    if batch.showtime_dates:
        first, last = batch.showtime_dates
        stmt = delete(Showtime).where(
            Showtime.source == source,
            Showtime.date >= first.isoformat(),
            Showtime.date <= last.isoformat(),
        )
        if batch.showtime_cinemas is not None:
            ids = [
                row.id
                for eid in batch.showtime_cinemas
                if (row := cinema_rows.get(eid) or _find(session, Cinema, source, eid))
            ]
            stmt = stmt.where(Showtime.cinema_id.in_(ids))
        session.execute(stmt)

    # 對不到電影編號時，用正規化後的片名找同一來源的電影
    movies_by_title = {
        normalize_title(m.title): m.id for m in session.scalars(select(Movie).where(Movie.source == source))
    }
    added = skipped = unlinked = 0
    for rec in batch.showtimes:
        cinema = cinema_rows.get(rec.cinema_external_id) or _find(session, Cinema, source, rec.cinema_external_id)
        if cinema is None:
            skipped += 1
            continue
        movie = None
        if rec.movie_external_id:
            movie = movie_rows.get(rec.movie_external_id) or _find(session, Movie, source, rec.movie_external_id)
        movie_id = movie.id if movie else movies_by_title.get(normalize_title(rec.raw_title))
        unlinked += movie_id is None
        session.add(
            Showtime(
                source=source,
                external_id=rec.external_id,
                cinema_id=cinema.id,
                movie_id=movie_id,
                raw_title=rec.raw_title,
                date=rec.date.isoformat(),
                start=rec.start,
                label=rec.label,
                price=rec.price,
                source_url=rec.source_url,
                estimated=rec.estimated,
                fetched_at=now,
            )
        )
        added += 1
    counts.update(showtimes=added, showtimes_skipped=skipped, showtimes_without_movie=unlinked)
    counts.update(batch.stats)
    session.flush()
    return counts


def precompute_jev(session: Session) -> int:
    """事先問好不會隨使用者輸入改變的題目（餐廳、場次標示、片型），回傳實際呼叫 Jev 的次數。

    每問完一家餐廳、一個場次標示、一部電影就提交一次，Jev 很慢時也不會長時間鎖住資料庫，網頁的排程請求才寫得進去。
    Jev 沒把握的題目也在這裡先請 LLM 幫忙（JEV_LLM_ASSIST），排程時直接讀存好的答案；
    餐廳只問開放範圍內的（ACTIVE_CITIES），其他縣市的不會被排到，不必花時間。用餐快慢只用到 Jev 的首選，不必問。
    """
    jev = JevService(session)
    helper = LlmAssist(session)
    for r in session.scalars(select(Restaurant)).all():
        subject, context = restaurant_subject(r), restaurant_context(r)
        jev.ask(MEAL_PACE, subject, context)
        for question in (DATE_FIT, CUISINE):
            answer = jev.ask(question, subject, context)
            if is_open(r.area_id.split("/", 1)[0]):
                helper.check(answer)
        session.commit()
    for label in session.scalars(select(Showtime.label).distinct()).all():
        for question in (SCREEN, AUDIO):
            helper.check(jev.ask(question, label_subject(label), {"label": label}))
        session.commit()
    # 來源沒給片型的電影（開眼）由 Jev 判斷，沒把握就請 LLM；都沒把握就留空
    for m in session.scalars(select(Movie).where(Movie.genre == "")).all():
        answer = helper.check(jev.ask(GENRE, movie_subject(m), movie_context(m)))
        if answer.confident(settings.jev_confidence_threshold):
            m.genre = answer.top
        session.commit()
    return jev.calls


def run_collector(
    session: Session, collector: Collector, start: dt.date | None = None, days: int = 14, judge: bool = True
) -> ScrapeRun:
    run = ScrapeRun(source=collector.name, status="running", started_at=now_tw())
    session.add(run)
    session.commit()
    try:
        batch = collector.collect(start or today_tw(), days)
        counts = upsert_batch(session, collector.name, batch)
        session.commit()  # 先把抓到的資料存起來，之後 Jev 再慢也不會一直鎖住資料庫
        if judge:
            counts["jev_calls"] = precompute_jev(session)
        run.status, run.counts = "ok", counts
    except Exception as exc:  # 抓取失敗時保留舊資料，只記錄錯誤
        session.rollback()
        run.status, run.error = "error", f"{type(exc).__name__}: {exc}"[:2000]
    run.finished_at = now_tw()
    session.add(run)
    session.commit()
    return run


DEMO_SOURCE = "fixture"


def remove_demo_content(session: Session) -> int:
    """刪掉示意資料裡虛構的場次、餐廳、影城、電影（商圈與交通保留），回傳刪掉的筆數。"""
    removed = 0
    for model in (Showtime, Restaurant, Cinema, Movie):  # 場次先刪，它參照影城與電影
        removed += session.execute(delete(model).where(model.source == DEMO_SOURCE)).rowcount
    session.execute(delete(ScrapeRun).where(ScrapeRun.source == DEMO_SOURCE))
    session.commit()
    return removed


def ensure_base_data(session: Session) -> ScrapeRun:
    """伺服器啟動時呼叫：匯入商圈與交通（base），並清掉之前匯入的虛構內容。"""
    run = run_collector(session, get_collector("base"), judge=False)
    remove_demo_content(session)
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description="執行資料來源並匯入資料庫")
    parser.add_argument("--source", default="base", help="資料來源名稱（見 app/collectors/__init__.py）")
    parser.add_argument("--days", type=int, default=14, help="抓幾天的場次")
    parser.add_argument("--no-jev", action="store_true", help="只匯入，不跑 Jev 預先判斷")
    parser.add_argument("--reset", action="store_true", help="清空並重建所有資料表（會刪除現有資料）")
    args = parser.parse_args()

    if args.reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    if problems := schema_problems():
        raise SystemExit(schema_message(problems))
    with SessionLocal() as session:
        run = run_collector(session, get_collector(args.source, session), days=args.days, judge=not args.no_jev)
    if run.status == "ok":
        print(f"[{run.source}] 匯入完成：{run.counts}")
    else:
        print(f"[{run.source}] 匯入失敗：{run.error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
