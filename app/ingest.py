"""匯入：執行一個資料來源，把結果寫進資料庫，再讓 Jev 預先判斷新資料或有變動的資料。

用法：
    python -m app.ingest                      # 預設：商圈與交通（base）
    python -m app.ingest --source atmovies    # 開眼電影網：影城、電影、場次
    python -m app.ingest --source osm         # OpenStreetMap：餐廳
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
from .db import Base, SessionLocal, engine, schema_message, schema_problems
from .config import settings
from .jev import AUDIO, CUISINE, DATE_FIT, GENRE, MEAL_PACE, SCREEN, JevService
from .jev.mock import normalize_title
from .jev.service import label_subject, movie_context, movie_subject, restaurant_context, restaurant_subject
from .models import Area, Cinema, Movie, Restaurant, ScrapeRun, Showtime, Station, TransitLink
from .timeutil import now_tw, today_tw

T = TypeVar("T", Cinema, Movie, Restaurant)


def _find(session: Session, model: type[T], source: str, external_id: str) -> T | None:
    return session.scalar(select(model).where(model.source == source, model.external_id == external_id))


def _upsert(session: Session, model: type[T], source: str, record, fetched_at: dt.datetime) -> T:
    row = _find(session, model, source, record.external_id)
    if row is None:
        row = model(source=source, external_id=record.external_id)
        session.add(row)
    for field, value in record.model_dump().items():
        setattr(row, field, value)
    row.fetched_at = fetched_at
    return row


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
    """事先問好不會隨使用者輸入改變的題目（餐廳、場次標示、片型），回傳實際呼叫 Jev 的次數。"""
    jev = JevService(session)
    for r in session.scalars(select(Restaurant)):
        for question in (DATE_FIT, MEAL_PACE, CUISINE):
            jev.ask(question, restaurant_subject(r), restaurant_context(r))
    for label in session.scalars(select(Showtime.label).distinct()):
        for question in (SCREEN, AUDIO):
            jev.ask(question, label_subject(label), {"label": label})
    # 來源沒給片型的電影（開眼）由 Jev 判斷；沒把握就留空
    for m in session.scalars(select(Movie).where(Movie.genre == "")):
        answer = jev.ask(GENRE, movie_subject(m), movie_context(m))
        if answer.confidence >= settings.jev_confidence_threshold:
            m.genre = answer.top
    session.flush()
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
