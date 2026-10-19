"""資料表與 migration（規格書 4.3）：結構、約束、PostGIS。"""
import datetime as dt

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from app.db import Base
from app.models import Area, Category, Cinema, Movie, Restaurant, Showtime, Station, TransitLink

TPE = dt.timezone(dt.timedelta(hours=8))
CINEMA_AT = "SRID=4326;POINT(121.2246 24.9620)"  # 中壢 SOGO 一帶


def _point_north_of_cinema(meters: float) -> str:
    """影城正北方 meters 公尺的點（緯度 1 度約 111 公里）。"""
    return f"SRID=4326;POINT(121.2246 {24.9620 + meters / 111_000:.6f})"


def test_migration_builds_every_table(database):
    tables = set(inspect(database).get_table_names())
    expected = {
        "areas", "locations", "cinemas", "movies", "showtimes", "restaurants", "categories",
        "jev_judgments", "crawl_runs", "stations", "transit_links", "transit_fares",
    }
    assert expected <= tables


def test_models_match_the_migration(database):
    """改了 app/models 卻忘了產生 migration，這個測試就會失敗。"""
    def ours(obj, name, type_, reflected, compare_to):
        return not (type_ == "table" and reflected and compare_to is None)  # PostGIS 自帶的表不算

    with database.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"include_object": ours, "compare_type": True})
        assert compare_metadata(ctx, Base.metadata) == []


def test_four_areas_are_seeded(session):
    areas = session.execute(select(Area.code, Area.name).order_by(Area.id)).all()
    assert areas == [("bade", "八德"), ("taoyuan", "桃園"), ("zhongli", "中壢"), ("qingpu", "青埔")]


def test_google_restaurants_cannot_store_shop_content(session):
    """依 Google 條款只能存 place_id 和座標；就算程式寫錯，資料庫也會擋下來。"""
    session.add(Restaurant(source="osm", external_id="node/1", geom=CINEMA_AT, osm_name="巷口拉麵"))
    session.add(Restaurant(source="google", external_id="ChIJ-ok", geom=CINEMA_AT))
    session.flush()
    with pytest.raises(IntegrityError, match="google_stores_no_content"):
        with session.begin_nested():
            session.add(Restaurant(source="google", external_id="ChIJ-bad", geom=CINEMA_AT, osm_name="不能存"))
            session.flush()
    with pytest.raises(IntegrityError, match="ck_restaurants_source"):
        with session.begin_nested():
            session.add(Restaurant(source="yelp", external_id="x", geom=CINEMA_AT))
            session.flush()


def test_only_google_restaurants_may_lose_their_coordinates(session):
    """Google 的座標最多存 30 天，更新不到就清掉；OSM 的座標一定要有。"""
    session.add(Restaurant(source="google", external_id="ChIJ-expired", geom=None, is_active=False))
    session.flush()
    with pytest.raises(IntegrityError, match="osm_has_geom"):
        with session.begin_nested():
            session.add(Restaurant(source="osm", external_id="node/2", geom=None, osm_name="沒有座標"))
            session.flush()


def test_postgis_finds_restaurants_within_walking_distance(session):
    zhongli = session.scalar(select(Area.id).where(Area.code == "zhongli"))
    cinema = Cinema(atmovies_id="t03a01", name="威尼斯影城", geom=CINEMA_AT, area_id=zhongli)
    session.add(cinema)
    for i, meters in enumerate((300, 790, 1200)):
        session.add(Restaurant(source="osm", external_id=f"node/{i}", geom=_point_north_of_cinema(meters), area_id=zhongli))
    session.flush()

    rows = session.execute(
        select(Restaurant.external_id, func.ST_Distance(Restaurant.geom, Cinema.geom))
        .join(Cinema, Cinema.id == cinema.id)
        .where(Restaurant.area_id == zhongli, func.ST_DWithin(Restaurant.geom, Cinema.geom, 800))
        .order_by(func.ST_Distance(Restaurant.geom, Cinema.geom))
    ).all()
    assert [r[0] for r in rows] == ["node/0", "node/1"]  # 1.2 公里那家走不到
    assert 290 < rows[0][1] < 310  # 距離單位是公尺


def test_showtime_version_defaults_to_empty_so_the_unique_key_works(session):
    """version 沒填時是空字串而不是 null，同一場次才不會因為 null 不等於 null 而重複存兩筆。"""
    cinema = Cinema(atmovies_id="t03a01", name="威尼斯影城")
    movie = Movie(atmovies_id="fm1", title="測試片", runtime_min=120)
    session.add_all([cinema, movie])
    session.flush()
    starts = dt.datetime(2026, 10, 24, 18, 0, tzinfo=TPE)
    session.add(Showtime(cinema_id=cinema.id, movie_id=movie.id, starts_at=starts, ends_at=starts + dt.timedelta(hours=2)))
    session.flush()
    assert session.scalar(select(Showtime.version)) == ""
    with pytest.raises(IntegrityError, match="uq_showtimes"):
        with session.begin_nested():
            session.add(Showtime(cinema_id=cinema.id, movie_id=movie.id, starts_at=starts, ends_at=starts))
            session.flush()


def test_transit_and_category_checks(session):
    a = Station(code="tyn-a", name="甲", geom=CINEMA_AT)
    b = Station(code="tyn-b", name="乙", geom=CINEMA_AT)
    session.add_all([a, b])
    session.flush()
    common = dict(from_station_id=a.id, to_station_id=b.id, ride_min=5, headway_min=10, source="base")
    session.add(TransitLink(mode="公車", line="公車（甲—乙）", first_min=360, last_min=24 * 60 + 9, **common))
    session.flush()  # 跨午夜的末班車存成大於 1440 的分鐘數
    for bad in (dict(mode="渡輪", line="x", first_min=360, last_min=1380), dict(mode="公車", line="y", first_min=1380, last_min=360)):
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(TransitLink(**common, **bad))
            session.flush()
    with pytest.raises(IntegrityError, match="ck_categories_kind"), session.begin_nested():
        session.add(Category(kind="drink", code="tea", label="茶"))
        session.flush()


def test_health_reports_postgis(database):
    from app.main import app

    body = TestClient(app).get("/api/health").json()
    assert body["status"] == "ok" and body["postgis"].startswith("3.")
