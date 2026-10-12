import datetime as dt

from sqlalchemy import event, func, select

from app import ingest
from app.collectors.base import Batch, CinemaRecord, Collector, MovieRecord, RestaurantRecord, ShowtimeRecord
from app.collectors.fixture import FixtureCollector
from app.ingest import precompute_jev, run_collector, upsert_batch
from app.models import Cinema, Movie, Restaurant, ScrapeRun, Showtime

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


def test_precompute_jev_commits_in_small_chunks(mem_session):
    """每家餐廳、每個場次標示、每部電影問完就提交，Jev 很慢時也不會長時間鎖住資料庫。"""
    _base(mem_session)
    upsert_batch(mem_session, "scraper", Batch(
        cinemas=[cinema("A")], movies=[MOVIE], showtimes=[show("A", "18:00")],
        showtime_dates=(DAY, DAY), showtime_cinemas=["A"],
        restaurants=[restaurant("R1"), restaurant("R2")], restaurant_areas=["台北市/信義"],
    ))
    # _base 的示意資料也有餐廳、場次、電影，所以從資料庫算出該有幾次提交
    expected = (
        mem_session.scalar(select(func.count()).select_from(Restaurant))
        + len(mem_session.scalars(select(Showtime.label).distinct()).all())
        + mem_session.scalar(select(func.count()).select_from(Movie).where(Movie.genre == ""))
    )
    assert expected >= 4  # 至少有我們加的兩家餐廳、一個場次標示、一部沒有片型的電影
    commits = []
    event.listen(mem_session, "after_commit", lambda session: commits.append(1))
    precompute_jev(mem_session)
    assert len(commits) == expected


class OneRestaurant(Collector):
    name = "scraper"

    def collect(self, start, days):
        return Batch(restaurants=[restaurant("R1")], restaurant_areas=["台北市/信義"])


def _restaurants(session):
    session.rollback()  # 還沒提交的寫入會在這裡一起丟掉
    return list(session.scalars(select(Restaurant.external_id).where(Restaurant.source == "scraper")))


def test_scraped_data_is_committed_before_jev_starts(mem_session, monkeypatch):
    _base(mem_session)
    seen = {}

    def fake_precompute(session):
        seen["in_transaction"] = session.in_transaction()
        seen["restaurants"] = _restaurants(session)
        return 0

    monkeypatch.setattr(ingest, "precompute_jev", fake_precompute)
    run = run_collector(mem_session, OneRestaurant())
    assert seen == {"in_transaction": False, "restaurants": ["R1"]}
    assert run.status == "ok"


def test_jev_failure_keeps_scraped_data_but_marks_run_as_error(mem_session, monkeypatch):
    _base(mem_session)

    def broken_precompute(session):
        raise RuntimeError("Jev 掛了")

    monkeypatch.setattr(ingest, "precompute_jev", broken_precompute)
    run = run_collector(mem_session, OneRestaurant())
    assert run.status == "error"
    assert run.error == "RuntimeError: Jev 掛了"
    assert _restaurants(mem_session) == ["R1"]
    stored = mem_session.scalars(select(ScrapeRun).where(ScrapeRun.source == "scraper")).one()
    assert (stored.status, stored.error) == ("error", "RuntimeError: Jev 掛了")
    assert stored.finished_at is not None


def test_failed_collect_leaves_no_data(mem_session):
    class Broken(Collector):
        name = "scraper"

        def collect(self, start, days):
            raise RuntimeError("網站掛了")

    _base(mem_session)
    run = run_collector(mem_session, Broken())
    assert run.status == "error" and run.error.startswith("RuntimeError: 網站掛了")
    assert _restaurants(mem_session) == []


def _movie_row(session, eid):
    return session.scalar(select(Movie).where(Movie.source == "scraper", Movie.external_id == eid))


def test_reupsert_keeps_jev_filled_genre_when_source_has_none(mem_session):
    _base(mem_session)
    scary = MovieRecord(external_id="H", title="鬼入室", runtime_min=95, synopsis="惡靈附身的驚悚故事，怪事不斷。")
    upsert_batch(mem_session, "scraper", Batch(movies=[scary]))
    precompute_jev(mem_session)
    assert _movie_row(mem_session, "H").genre == "恐怖"
    upsert_batch(mem_session, "scraper", Batch(movies=[scary]))  # 開眼這次一樣沒給片型（genre 是空字串）
    assert _movie_row(mem_session, "H").genre == "恐怖"


def test_reupsert_keeps_old_synopsis_when_new_one_is_empty(mem_session):
    _base(mem_session)
    upsert_batch(mem_session, "scraper", Batch(movies=[MovieRecord(external_id="S", title="片", runtime_min=100, synopsis="舊簡介")]))
    upsert_batch(mem_session, "scraper", Batch(movies=[MovieRecord(external_id="S", title="片", runtime_min=100, synopsis="")]))  # 網站忙碌，這次沒抓到
    assert _movie_row(mem_session, "S").synopsis == "舊簡介"


def test_reupsert_replaces_synopsis_with_a_new_one(mem_session):
    _base(mem_session)
    upsert_batch(mem_session, "scraper", Batch(movies=[MovieRecord(external_id="S", title="片", runtime_min=100, synopsis="舊簡介")]))
    upsert_batch(mem_session, "scraper", Batch(movies=[MovieRecord(external_id="S", title="片", runtime_min=100, synopsis="新簡介")]))
    assert _movie_row(mem_session, "S").synopsis == "新簡介"
