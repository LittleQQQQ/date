"""匯入：執行一個資料來源，把結果寫進資料庫，再讓 Jev 預先判斷新資料或有變動的資料。

用法：
    python -m app.ingest                      # 預設：示意資料，今天起 14 天的場次
    python -m app.ingest --source fixture --days 7
    python -m app.ingest --no-jev             # 只匯入，不跑 Jev
"""
from __future__ import annotations

import argparse
import datetime as dt
from typing import TypeVar

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .collectors import Batch, Collector, get_collector
from .db import Base, SessionLocal, engine
from .jev import AUDIO, CUISINE, DATE_FIT, MEAL_PACE, SCREEN, JevService
from .jev.mock import normalize_title
from .jev.service import label_subject, restaurant_context, restaurant_subject
from .models import Cinema, Movie, Restaurant, ScrapeRun, Showtime, Station, TransitLink
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

    for rec in batch.stations:
        row = session.get(Station, rec.id) or Station(id=rec.id)
        for field, value in rec.model_dump().items():
            setattr(row, field, value)
        row.source, row.fetched_at = source, now
        session.add(row)
    counts["stations"] = len(batch.stations)
    session.flush()

    for rec in batch.transit_links:
        row = session.scalar(
            select(TransitLink).where(
                TransitLink.from_station_id == rec.from_station_id, TransitLink.to_station_id == rec.to_station_id
            )
        ) or TransitLink()
        for field, value in rec.model_dump().items():
            setattr(row, field, value)
        row.source, row.fetched_at = source, now
        session.add(row)
    counts["transit_links"] = len(batch.transit_links)

    cinema_rows = {rec.external_id: _upsert(session, Cinema, source, rec, now) for rec in batch.cinemas}
    movie_rows = {rec.external_id: _upsert(session, Movie, source, rec, now) for rec in batch.movies}
    for rec in batch.restaurants:
        _upsert(session, Restaurant, source, rec, now)
    counts.update(cinemas=len(batch.cinemas), movies=len(batch.movies), restaurants=len(batch.restaurants))
    session.flush()

    if batch.showtime_dates:
        first, last = batch.showtime_dates
        session.execute(
            delete(Showtime).where(
                Showtime.source == source,
                Showtime.date >= first.isoformat(),
                Showtime.date <= last.isoformat(),
            )
        )

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
                fetched_at=now,
            )
        )
        added += 1
    counts.update(showtimes=added, showtimes_skipped=skipped, showtimes_without_movie=unlinked)
    session.flush()
    return counts


def precompute_jev(session: Session) -> int:
    """事先問好不會隨使用者輸入改變的題目（餐廳、場次標示），回傳實際呼叫 Jev 的次數。"""
    jev = JevService(session)
    for r in session.scalars(select(Restaurant)):
        for question in (DATE_FIT, MEAL_PACE, CUISINE):
            jev.ask(question, restaurant_subject(r), restaurant_context(r))
    for label in session.scalars(select(Showtime.label).distinct()):
        for question in (SCREEN, AUDIO):
            jev.ask(question, label_subject(label), {"label": label})
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


def ensure_demo_data(session: Session) -> ScrapeRun | None:
    """伺服器啟動時呼叫：資料庫裡沒有今天以後的場次，就用示意資料補上。"""
    has_future = session.scalar(
        select(func.count()).select_from(Showtime).where(Showtime.date >= today_tw().isoformat())
    )
    if has_future:
        return None
    return run_collector(session, get_collector("fixture"))


def main() -> None:
    parser = argparse.ArgumentParser(description="執行資料來源並匯入資料庫")
    parser.add_argument("--source", default="fixture", help="資料來源名稱（見 app/collectors/__init__.py）")
    parser.add_argument("--days", type=int, default=14, help="抓幾天的場次")
    parser.add_argument("--no-jev", action="store_true", help="只匯入，不跑 Jev 預先判斷")
    args = parser.parse_args()

    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        run = run_collector(session, get_collector(args.source), days=args.days, judge=not args.no_jev)
    if run.status == "ok":
        print(f"[{run.source}] 匯入完成：{run.counts}")
    else:
        print(f"[{run.source}] 匯入失敗：{run.error}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
