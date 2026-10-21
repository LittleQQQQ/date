"""匯入（規格書 4.4、4.5）：UPSERT 重複執行結果相同、過期與取消的資料會清掉、失敗時保留舊資料。"""
import datetime as dt
import json

from sqlalchemy import func, select

from app.collectors.base import Batch, CinemaRecord, FareRecord, LinkRecord, MovieRecord, RestaurantRecord, ShowtimeRecord
from app.ingest import NETWORK_DIR, load_network, load_seed, run_job, write_batch
from app.models import (
    Area, Category, Cinema, CrawlRun, Location, Movie, Restaurant, Showtime, Station, TransitFare, TransitLink,
)
from app.timeutil import TW

SOON = dt.datetime.now(TW).replace(microsecond=0) + dt.timedelta(days=1)


def _count(session, model, *where):
    return session.scalar(select(func.count()).select_from(model).where(*where))


def _network_file(tmp_path, drop_station=None, drop_line=None):
    """複製正式的路網骨架，拿掉一個站（連同它的路段）或一條路線。"""
    data = json.loads(next(NETWORK_DIR.glob("*.json")).read_text(encoding="utf-8"))
    if drop_station:
        data["stations"] = [s for s in data["stations"] if s["code"] != drop_station]
        data["links"] = [l for l in data["links"] if drop_station not in (l["from"], l["to"])]
    if drop_line:
        data["links"] = [l for l in data["links"] if l["line"] != drop_line]
    (tmp_path / "network.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return tmp_path


# ---------- 路網 ----------


def test_network_loads_stations_with_areas_and_both_directions(session):
    stats = load_network(session)
    assert stats["stations"] == 7 and stats["links"] == 18  # 9 條路線，兩個方向
    zhongli = session.scalar(select(Station).where(Station.code == "tyn-zhongli"))
    assert session.get(Area, zhongli.area_id).code == "zhongli"
    first, last = session.execute(
        select(TransitLink.first_min, TransitLink.last_min).where(TransitLink.line == "台鐵縱貫線").limit(1)
    ).one()
    assert (first, last) == (5 * 60 + 30, 24 * 60)  # 末班 24:00 存成 1440


def test_network_is_idempotent(session):
    load_network(session)
    load_network(session)
    assert _count(session, Station) == 7 and _count(session, TransitLink) == 18


def test_removing_a_line_also_removes_its_tdx_data(session, tmp_path):
    load_network(session)
    ids = dict(session.execute(select(Station.code, Station.id)).all())
    a, b = ids["tyn-zhongli"], ids["tyn-sogo"]
    session.add(TransitLink(from_station_id=a, to_station_id=b, mode="公車", line="公車（170）", ride_min=4,
                            headway_min=30, first_min=390, last_min=1260, source="tdx"))
    session.add(TransitFare(from_station_id=a, to_station_id=b, line="公車（170）", fare=18, source="tdx"))
    session.flush()

    stats = load_network(session, _network_file(tmp_path, drop_line="公車（中壢車站—SOGO）"))
    assert stats["stale_links"] == 3  # 手寫的兩個方向，加上 TDX 的那一段
    assert stats["stale_fares"] == 1
    assert _count(session, TransitLink, TransitLink.line == "公車（170）") == 0


def test_removing_a_station_cascades(session, tmp_path):
    load_network(session)
    stats = load_network(session, _network_file(tmp_path, drop_station="tyn-bade"))
    assert stats["stale_stations"] == 1
    assert _count(session, Station) == 6
    assert _count(session, TransitLink) == 14  # 八德的兩條路線、四個方向一起刪掉


def test_empty_network_folder_keeps_everything(session, tmp_path):
    load_network(session)
    run = run_job(session, "network", lambda: load_network(session, tmp_path))
    assert run.status == "failed" and "沒有車站資料" in run.error
    assert _count(session, Station) == 7
    assert session.scalar(select(CrawlRun.status).where(CrawlRun.id == run.id)) == "failed"


# ---------- 暫定資料 ----------


def test_seed_loads_categories_locations_and_prices(session):
    load_network(session)
    session.add(Cinema(atmovies_id="t03a01", name="威尼斯影城"))
    session.flush()
    stats = load_seed(session)
    assert (stats["categories"], stats["locations"], stats["ticket_prices"]) == (16, 7, 1)
    assert _count(session, Category, Category.kind == "cuisine") == 9
    assert session.scalar(select(Cinema.ticket_price)) == 300
    # 地標暫時拿車站當地標，座標和車站一樣
    distance = session.scalar(
        select(func.ST_Distance(Location.geom, Station.geom))
        .join(Station, Station.code == "tyn-zhongli").where(Location.name == "中壢車站")
    )
    assert distance == 0
    assert load_seed(session)["ticket_prices"] == 0  # 再跑一次沒有變動


# ---------- 場次 ----------


def _cinema_batch(times, cinemas=("t03a01",), synopsis="一部片"):
    return Batch(
        cinemas=[CinemaRecord(atmovies_id=c, name=f"影城{c}", lat=24.962, lng=121.2246) for c in cinemas],
        movies=[MovieRecord(atmovies_id="fm1", title="測試片", runtime_min=110, synopsis=synopsis)],
        showtimes=[
            ShowtimeRecord(cinema_atmovies_id=cinemas[0], movie_atmovies_id="fm1", starts_at=t,
                           ends_at=t + dt.timedelta(minutes=110), version="數位")
            for t in times
        ],
        showtime_cinemas=list(cinemas),
        showtime_window=(SOON - dt.timedelta(hours=12), SOON + dt.timedelta(days=6)),
    )


def test_showtimes_upsert_is_idempotent(session):
    times = [SOON, SOON + dt.timedelta(hours=3)]
    write_batch(session, _cinema_batch(times))
    write_batch(session, _cinema_batch(times))
    assert _count(session, Showtime) == 2 and _count(session, Cinema) == 1 and _count(session, Movie) == 1


def test_showtimes_missing_from_the_new_batch_are_cancelled(session):
    write_batch(session, _cinema_batch([SOON, SOON + dt.timedelta(hours=3)]))
    stats = write_batch(session, _cinema_batch([SOON]))
    assert stats["showtimes_cancelled"] == 1 and _count(session, Showtime) == 1


def test_past_showtimes_are_deleted(session):
    stale = dt.datetime.now(TW) - dt.timedelta(hours=2)
    write_batch(session, _cinema_batch([stale, SOON]))
    assert _count(session, Showtime) == 1


def test_empty_synopsis_keeps_the_old_one(session):
    write_batch(session, _cinema_batch([SOON], synopsis="原本的簡介"))
    write_batch(session, _cinema_batch([SOON], synopsis=""))
    assert session.scalar(select(Movie.synopsis)) == "原本的簡介"


# ---------- 餐廳、TDX ----------


def _restaurants(*ids):
    return [RestaurantRecord(external_id=i, lat=24.962, lng=121.2246, osm_name=f"店{i}", osm_tags={"amenity": "restaurant"}) for i in ids]


def test_closed_restaurants_become_inactive_only_after_a_complete_fetch(session):
    write_batch(session, Batch(restaurants=_restaurants("node/1", "node/2"), restaurants_complete=True))
    write_batch(session, Batch(restaurants=_restaurants("node/1")))  # 有區域沒抓到：不判斷收店
    assert _count(session, Restaurant, Restaurant.is_active) == 2
    stats = write_batch(session, Batch(restaurants=_restaurants("node/1"), restaurants_complete=True))
    assert stats["restaurants_closed"] == 1
    assert session.scalar(select(Restaurant.osm_tags).where(Restaurant.external_id == "node/1")) == {"amenity": "restaurant"}


def test_tdx_batch_replaces_the_previous_one(session):
    load_network(session)
    link = LinkRecord(from_code="tyn-zhongli", to_code="tyn-sogo", mode="公車", line="公車（170）",
                      ride_min=4, headway_min=30, first_min=390, last_min=1260)
    fare = FareRecord(from_code="tyn-zhongli", to_code="tyn-sogo", line="公車（170）", fare=18)
    write_batch(session, Batch(links=[link], fares=[fare], replace_tdx=True))
    write_batch(session, Batch(links=[link], fares=[fare], replace_tdx=True))
    assert _count(session, TransitLink, TransitLink.source == "tdx") == 1
    stats = write_batch(session, Batch(replace_tdx=True))  # 路線停駛了
    assert stats["stale_transit_links"] == 1
    assert _count(session, TransitLink, TransitLink.source == "tdx") == 0 and _count(session, TransitFare) == 0


def test_judge_command_runs_the_pending_judgments(committed, capsys):
    """手動匯入後，影城與餐廳的區域、類別要等 Jev 判斷才有；python -m app.ingest judge 直接跑一次，不用等每天的排程。"""
    from app.db import SessionLocal
    from app.ingest import main

    main(["judge"])
    assert "[judge] 判斷完成" in capsys.readouterr().out
    with SessionLocal() as s:
        run = s.scalar(select(CrawlRun).order_by(CrawlRun.id.desc()))
    assert (run.job, run.status) == ("judge", "success")
