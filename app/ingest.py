"""匯入：執行資料來源，寫進資料庫（規格書 4.4、4.5、7.2）。

用法：
    python -m app.ingest network    # 路網骨架（data/network/）
    python -m app.ingest seed       # 暫定資料：類別、地標、影城票價（data/seed/）
    python -m app.ingest atmovies   # 開眼電影網：影城、電影、場次
    python -m app.ingest osm        # OpenStreetMap 餐廳
    python -m app.ingest tdx        # TDX 台鐵、公車的班表與票價（要在 .env 填 TDX 金鑰）

- 寫入一律用 PostgreSQL 的 INSERT … ON CONFLICT（UPSERT），重複執行結果相同。
- 每次執行都記在 crawl_runs；失敗時整批 rollback，保留舊資料。
- 影城、餐廳屬於哪個區域、電影種類、餐廳類別由 Jev 判斷（第 5 步），這裡先不填。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Table, delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .collectors.base import Batch, Collector, ScrapeError
from .config import ROOT_DIR
from .models import (
    Area, Category, Cinema, CrawlRun, Location, Movie, Restaurant, Showtime, Station, TransitFare, TransitLink,
    lat_of, lng_of, point_wkt,
)
from .timeutil import now_tw, to_min

NETWORK_DIR = ROOT_DIR / "data" / "network"
SEED_DIR = ROOT_DIR / "data" / "seed"


# ---------- 共用 ----------


def _upsert(session: Session, model, rows: list[dict], keys: tuple[str, ...]) -> int:
    """INSERT … ON CONFLICT (keys) DO UPDATE：其他欄位改成新值，updated_at 改成現在。"""
    if not rows:
        return 0
    table: Table = model.__table__
    stmt = insert(table).values(rows)
    changed = {c: stmt.excluded[c] for c in rows[0] if c not in keys}
    changed["updated_at"] = func.now()
    session.execute(stmt.on_conflict_do_update(index_elements=list(keys), set_=changed))
    return len(rows)


def _area_ids(session: Session) -> dict[str, int]:
    return dict(session.execute(select(Area.code, Area.id)).all())


def _station_ids(session: Session) -> dict[str, int]:
    return dict(session.execute(select(Station.code, Station.id)).all())


def run_job(session: Session, job: str, work: Callable[[], dict]) -> CrawlRun:
    """執行一個工作並記在 crawl_runs：先記 running，成功記 success 和統計，失敗 rollback 並記 failed。"""
    run = CrawlRun(job=job, status="running")
    session.add(run)
    session.commit()
    try:
        stats = work()
        session.flush()
        run.status, run.stats = "success", stats
    except Exception as exc:  # 抓取或寫入失敗：保留舊資料，只記錄錯誤
        session.rollback()
        run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"[:2000]
    run.finished_at = now_tw()
    session.add(run)
    session.commit()
    return run


# ---------- 路網骨架 ----------


def load_network(session: Session, folder: Path = NETWORK_DIR) -> dict:
    """匯入手寫的路網骨架：車站、路段（source = base）。檔案裡沒有的站和路段刪掉，連帶刪掉 TDX 的對應資料。"""
    stations, links = [], []
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        stations += data.get("stations", [])
        links += data.get("links", [])
    if not stations:
        # 資料夾被清空時照常刪，會把整個路網和 TDX 資料全部清掉
        raise ScrapeError(f"{folder} 沒有車站資料，這次不匯入、也不刪除")

    areas = _area_ids(session)
    _upsert(session, Station, [
        {"code": s["code"], "name": s["name"], "geom": point_wkt(s["lat"], s["lng"]),
         "transfer_min": s.get("transfer_min", 3), "area_id": areas[s["area"]] if s.get("area") else None}
        for s in stations
    ], ("code",))
    ids = _station_ids(session)

    rows = []
    for item in links:
        pairs = [(item["from"], item["to"])] + ([(item["to"], item["from"])] if item.get("both_ways") else [])
        for a, b in pairs:
            rows.append({
                "from_station_id": ids[a], "to_station_id": ids[b], "mode": item["mode"], "line": item["line"],
                "ride_min": item["ride"], "headway_min": item["headway"], "first_min": to_min(item["first"]),
                "last_min": to_min(item["last"]), "source": "base", "fetched_at": now_tw(),
            })
    _upsert(session, TransitLink, rows, ("from_station_id", "to_station_id", "line"))

    keep_codes = {s["code"] for s in stations}
    stale_stations = session.execute(delete(Station).where(Station.code.not_in(keep_codes))).rowcount
    keep_links = {(r["from_station_id"], r["to_station_id"], r["line"]) for r in rows}
    keep_pairs = {(r["from_station_id"], r["to_station_id"], r["mode"]) for r in rows}
    stale_links = 0
    for link in session.scalars(select(TransitLink)):
        if link.source == "tdx":  # TDX 的路段跟著骨架走：骨架拿掉這一段，TDX 的也刪
            stale = (link.from_station_id, link.to_station_id, link.mode) not in keep_pairs
        else:
            stale = (link.from_station_id, link.to_station_id, link.line) not in keep_links
        if stale:
            session.delete(link)
            stale_links += 1
    session.flush()
    return {
        "stations": len(stations), "links": len(rows), "stale_stations": stale_stations,
        "stale_links": stale_links, "stale_fares": _remove_orphan_fares(session),
    }


def _remove_orphan_fares(session: Session) -> int:
    """刪掉對不到路段的車資（路段被刪了）。"""
    links = select(TransitLink.from_station_id, TransitLink.to_station_id, TransitLink.line)
    key = tuple_(TransitFare.from_station_id, TransitFare.to_station_id, TransitFare.line)
    return session.execute(delete(TransitFare).where(key.not_in(links))).rowcount


# ---------- 暫定資料 ----------


def load_seed(session: Session, folder: Path = SEED_DIR) -> dict:
    """匯入類別、地標、影城票價。檔案是唯一的來源：檔案裡沒有的類別和地標刪掉。"""
    categories = json.loads((folder / "categories.json").read_text(encoding="utf-8"))
    rows = [
        {"kind": kind, "code": c["code"], "label": c["label"], "jev_description": c.get("jev_description", ""), "sort_order": i}
        for kind in ("movie_genre", "cuisine") for i, c in enumerate(categories.get(kind, []))
    ]
    _upsert(session, Category, rows, ("kind", "code"))
    keep = {(r["kind"], r["code"]) for r in rows}
    stale_categories = session.execute(delete(Category).where(tuple_(Category.kind, Category.code).not_in(keep))).rowcount

    areas = _area_ids(session)
    stations = {code: (lat, lng) for code, lat, lng in session.execute(
        select(Station.code, lat_of(Station.geom), lng_of(Station.geom))
    )}
    places = json.loads((folder / "locations.json").read_text(encoding="utf-8"))
    loc_rows = []
    for area_code, items in places.items():
        if area_code.startswith("_"):
            continue
        for item in items:
            lat, lng = stations[item["station"]] if "station" in item else (item["lat"], item["lng"])
            loc_rows.append({"area_id": areas[area_code], "name": item["name"], "address": item.get("address", ""),
                             "geom": point_wkt(lat, lng)})
    _upsert(session, Location, loc_rows, ("area_id", "name"))
    keep_loc = {(r["area_id"], r["name"]) for r in loc_rows}
    stale_locations = session.execute(delete(Location).where(tuple_(Location.area_id, Location.name).not_in(keep_loc))).rowcount

    return {
        "categories": len(rows), "stale_categories": stale_categories,
        "locations": len(loc_rows), "stale_locations": stale_locations,
        "ticket_prices": apply_ticket_prices(session, folder),
    }


def apply_ticket_prices(session: Session, folder: Path = SEED_DIR) -> int:
    """依 ticket_prices.json 設定每家影城的票價；沒列到的用 default。抓到新影城後也會再套一次。"""
    prices = json.loads((folder / "ticket_prices.json").read_text(encoding="utf-8"))
    changed = 0
    for cinema in session.scalars(select(Cinema)):
        price = prices.get("by_atmovies_id", {}).get(cinema.atmovies_id, prices["default"])
        if cinema.ticket_price != price:
            cinema.ticket_price = price
            changed += 1
    session.flush()
    return changed


# ---------- 爬蟲的資料 ----------


def write_batch(session: Session, batch: Batch) -> dict:
    stats: dict[str, int] = {}
    stats.update(_write_cinemas(session, batch))
    stats.update(_write_restaurants(session, batch))
    stats.update(_write_tdx(session, batch))
    return stats | batch.stats


def _write_cinemas(session: Session, batch: Batch) -> dict:
    if not (batch.cinemas or batch.movies or batch.showtimes):
        return {}
    _upsert(session, Cinema, [
        {"atmovies_id": c.atmovies_id, "name": c.name, "address": c.address, "geom": point_wkt(c.lat, c.lng),
         "geocoded_at": now_tw(), "is_active": True}
        for c in batch.cinemas
    ], ("atmovies_id",))
    if batch.movies:
        stmt = insert(Movie).values([m.model_dump() for m in batch.movies])
        # 簡介偶爾抓不到：這次是空的就保留上次抓到的
        session.execute(stmt.on_conflict_do_update(index_elements=["atmovies_id"], set_={
            "title": stmt.excluded.title, "runtime_min": stmt.excluded.runtime_min, "updated_at": func.now(),
            "synopsis": func.coalesce(func.nullif(stmt.excluded.synopsis, ""), Movie.synopsis),
        }))
    cinema_ids = dict(session.execute(select(Cinema.atmovies_id, Cinema.id)).all())
    movie_ids = dict(session.execute(select(Movie.atmovies_id, Movie.id)).all())
    rows = [
        {"cinema_id": cinema_ids[s.cinema_atmovies_id], "movie_id": movie_ids[s.movie_atmovies_id],
         "starts_at": s.starts_at, "ends_at": s.ends_at, "version": s.version}
        for s in batch.showtimes
    ]
    _upsert(session, Showtime, rows, ("cinema_id", "movie_id", "starts_at", "version"))

    cancelled = 0
    if batch.showtime_cinemas and batch.showtime_window:
        start, end = batch.showtime_window
        fetched = [cinema_ids[c] for c in batch.showtime_cinemas]
        keep = {(r["cinema_id"], r["movie_id"], r["starts_at"], r["version"]) for r in rows}
        for st in session.scalars(select(Showtime).where(
            Showtime.cinema_id.in_(fetched), Showtime.starts_at >= start, Showtime.starts_at <= end
        )):
            if (st.cinema_id, st.movie_id, st.starts_at, st.version) not in keep:  # 這次沒抓到：取消了
                session.delete(st)
                cancelled += 1
    # 規格書 4.5：過期的場次每天抓取時刪掉
    past = session.execute(delete(Showtime).where(Showtime.starts_at < func.now())).rowcount
    session.flush()
    return {
        "cinemas": len(batch.cinemas), "movies": len(batch.movies), "showtimes": len(rows),
        "showtimes_cancelled": cancelled, "showtimes_past": past, "ticket_prices": apply_ticket_prices(session),
    }


def _write_restaurants(session: Session, batch: Batch) -> dict:
    if not batch.restaurants:
        return {}
    _upsert(session, Restaurant, [
        {"source": "osm", "external_id": r.external_id, "geom": point_wkt(r.lat, r.lng), "geom_fetched_at": now_tw(),
         "osm_name": r.osm_name, "osm_opening_hours": r.osm_opening_hours, "osm_tags": r.osm_tags, "is_active": True}
        for r in batch.restaurants
    ], ("source", "external_id"))
    closed = 0
    if batch.restaurants_complete:  # 每個區域都抓成功，這次沒出現的店就是收了
        seen = [r.external_id for r in batch.restaurants]
        closed = session.execute(
            update(Restaurant)
            .where(Restaurant.source == "osm", Restaurant.is_active, Restaurant.external_id.not_in(seen))
            .values(is_active=False, updated_at=func.now())
        ).rowcount
    session.flush()
    return {"restaurants": len(batch.restaurants), "restaurants_closed": closed}


def _write_tdx(session: Session, batch: Batch) -> dict:
    if not (batch.links or batch.fares or batch.replace_tdx):
        return {}
    ids = _station_ids(session)
    now = now_tw()
    links = [
        {"from_station_id": ids[l.from_code], "to_station_id": ids[l.to_code], "mode": l.mode, "line": l.line,
         "ride_min": l.ride_min, "headway_min": l.headway_min, "first_min": l.first_min, "last_min": l.last_min,
         "source": "tdx", "fetched_at": now}
        for l in batch.links
    ]
    fares = [
        {"from_station_id": ids[f.from_code], "to_station_id": ids[f.to_code], "line": f.line, "fare": f.fare,
         "source": "tdx", "fetched_at": now}
        for f in batch.fares
    ]
    _upsert(session, TransitLink, links, ("from_station_id", "to_station_id", "line"))
    _upsert(session, TransitFare, fares, ("from_station_id", "to_station_id", "line"))
    stale = 0
    if batch.replace_tdx:  # 這批是 TDX 的全部：同來源、這批沒有的刪掉（例如公車路線停駛）
        keep = {(r["from_station_id"], r["to_station_id"], r["line"]) for r in links}
        for link in session.scalars(select(TransitLink).where(TransitLink.source == "tdx")):
            if (link.from_station_id, link.to_station_id, link.line) not in keep:
                session.delete(link)
                stale += 1
        keep_fares = {(r["from_station_id"], r["to_station_id"], r["line"]) for r in fares}
        for fare in session.scalars(select(TransitFare).where(TransitFare.source == "tdx")):
            if (fare.from_station_id, fare.to_station_id, fare.line) not in keep_fares:
                session.delete(fare)
        session.flush()
    return {"transit_links": len(links), "transit_fares": len(fares), "stale_transit_links": stale,
            "stale_transit_fares": _remove_orphan_fares(session)}


def run_collector(session: Session, collector: Collector) -> CrawlRun:
    return run_job(session, collector.name, lambda: write_batch(session, collector.collect()))


# ---------- 指令 ----------


def _run_google(session: Session) -> CrawlRun:
    """手動跑一次 Google 找餐廳（每個車站一次 Nearby Search），跑完印出這個月各 SKU 用了幾次。"""
    from .collectors.google_places import GooglePlaces, use_google
    from .google_sync import refresh_google_places

    if not use_google():
        raise SystemExit("沒有設定 GOOGLE_PLACES_API_KEY（.env），不呼叫 Google")
    google = GooglePlaces()
    run = run_job(session, "google_nearby", lambda: refresh_google_places(session, google))
    for quota in (google.nearby_quota, google.details_quota, google.location_quota):
        print(f"{quota.name}：這個月用了 {quota.used()} / {quota.limit} 次")
    return run


def make_collector(name: str, session: Session) -> Collector:
    """建立爬蟲；需要涵蓋範圍或路網骨架的，從資料庫讀出來交給它。"""
    from .collectors import COLLECTORS

    if name not in COLLECTORS:
        raise SystemExit(f"沒有這個資料來源：{name}（可用：network、seed、{'、'.join(COLLECTORS)}）")
    return COLLECTORS[name].create(session)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="執行資料來源並匯入資料庫（資料表要先用 alembic upgrade head 建好）")
    parser.add_argument("source", help="network、seed、google（Google 找餐廳，要有金鑰），或爬蟲名稱（atmovies、osm、tdx）")
    args = parser.parse_args(argv)

    from .db import SessionLocal

    with SessionLocal() as session:
        if args.source == "network":
            run = run_job(session, "network", lambda: load_network(session))
        elif args.source == "seed":
            run = run_job(session, "seed", lambda: load_seed(session))
        elif args.source == "google":
            run = _run_google(session)
        else:
            run = run_collector(session, make_collector(args.source, session))
    if run.status == "success":
        print(f"[{run.job}] 匯入完成：{run.stats}")
    else:
        print(f"[{run.job}] 匯入失敗：{run.error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
