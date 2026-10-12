import datetime as dt

from sqlalchemy import select

from app.collectors.base import Batch, CinemaRecord, MovieRecord, RestaurantRecord, ShowtimeRecord
from app.collectors.fixture import FixtureCollector
from app.ingest import precompute_jev, upsert_batch
from app.models import Cinema, Movie, Restaurant, Showtime

DAY = dt.date(2026, 10, 12)
MOVIE = MovieRecord(external_id="M", title="片", runtime_min=100)


def _base(session):
    upsert_batch(session, "fixture", FixtureCollector().collect(DAY, 1))  # 商圈、車站、交通


def cinema(eid, area="台北市/信義"):
    return CinemaRecord(external_id=eid, name=f"影城{eid}", area_id=area, lat=25.036, lng=121.566)


def show(cinema_eid, start):
    return ShowtimeRecord(
        external_id=f"{cinema_eid}|M|{DAY}|{start}", cinema_external_id=cinema_eid, movie_external_id="M",
        raw_title="片", date=DAY, start=start, label="數位", price=300, estimated=["price"],
    )


def restaurant(eid, area="台北市/信義"):
    return RestaurantRecord(
        external_id=eid, name=f"餐廳{eid}", area_id=area, lat=25.036, lng=121.566, description="餐廳",
        price_per_person=500, hours={"default": ["11:30", "21:00"]}, estimated=["hours", "price"],
    )


def _times(session, cinema_eid):
    rows = session.scalars(
        select(Showtime.start)
        .join(Cinema, Showtime.cinema_id == Cinema.id)
        .where(Showtime.source == "scraper", Cinema.external_id == cinema_eid)
    )
    return sorted(rows)


def test_only_covered_cinemas_lose_old_showtimes(mem_session):
    _base(mem_session)
    upsert_batch(mem_session, "scraper", Batch(
        cinemas=[cinema("A"), cinema("B")], movies=[MOVIE],
        showtimes=[show("A", "18:00"), show("B", "19:00")],
        showtime_dates=(DAY, DAY), showtime_cinemas=["A", "B"],
    ))
    # 第二次 B 抓失敗，只帶 A 的新場次
    upsert_batch(mem_session, "scraper", Batch(
        cinemas=[cinema("A")], movies=[MOVIE], showtimes=[show("A", "20:00")],
        showtime_dates=(DAY, DAY), showtime_cinemas=["A"],
    ))
    assert _times(mem_session, "A") == ["20:00"]
    assert _times(mem_session, "B") == ["19:00"]
    row = mem_session.scalar(select(Showtime).where(Showtime.source == "scraper"))
    assert row.estimated == ["price"]


def test_restaurants_missing_from_a_covered_area_are_removed(mem_session):
    _base(mem_session)
    upsert_batch(mem_session, "scraper", Batch(
        restaurants=[restaurant("R1"), restaurant("R2"), restaurant("X1", "台北市/西門町")],
        restaurant_areas=["台北市/信義", "台北市/西門町"],
    ))
    counts = upsert_batch(mem_session, "scraper", Batch(restaurants=[restaurant("R1")], restaurant_areas=["台北市/信義"]))
    rows = {r.external_id: r for r in mem_session.scalars(select(Restaurant).where(Restaurant.source == "scraper"))}
    assert set(rows) == {"R1", "X1"}  # 西門町這次沒抓，保留
    assert counts["restaurants_removed"] == 1
    assert rows["R1"].estimated == ["hours", "price"]


def test_places_in_unknown_areas_are_skipped(mem_session):
    _base(mem_session)
    counts = upsert_batch(mem_session, "scraper", Batch(
        cinemas=[cinema("Z", "花蓮縣/市區")], restaurants=[restaurant("Z", "花蓮縣/市區")],
    ))
    assert counts["places_outside_areas"] == 2
    assert counts["cinemas"] == 0 and counts["restaurants"] == 0


def test_batch_stats_are_reported(mem_session):
    _base(mem_session)
    counts = upsert_batch(mem_session, "scraper", Batch(stats={"failed_pages": 2}))
    assert counts["failed_pages"] == 2


def test_jev_fills_missing_genre(mem_session):
    _base(mem_session)
    upsert_batch(mem_session, "scraper", Batch(movies=[
        MovieRecord(external_id="H", title="鬼入室", runtime_min=95, synopsis="惡靈附身的驚悚故事，怪事不斷。"),
        MovieRecord(external_id="N", title="週末的尼諾", runtime_min=90, synopsis="一個男孩的週末。"),
    ]))
    precompute_jev(mem_session)
    genres = {m.external_id: m.genre for m in mem_session.scalars(select(Movie).where(Movie.source == "scraper"))}
    assert genres == {"H": "恐怖", "N": ""}  # 沒把握就留空，畫面顯示「片型未知」
