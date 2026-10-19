"""規劃測試共用的資料：中壢一家影城（太平洋 SOGO 旁）、一部片、明天兩個場次、一家拉麵店，加上路網骨架和暫定地標。"""
import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest import load_network, load_seed
from app.models import Area, Cinema, Location, Movie, Restaurant, Showtime, point_wkt
from app.timeutil import TW

TOMORROW = dt.datetime.now(TW).date() + dt.timedelta(days=1)


def at(hhmm: str, day: dt.date = TOMORROW) -> dt.datetime:
    h, m = map(int, hhmm.split(":"))
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=TW)


def build(session: Session, opening_hours: str = "Mo-Su 11:00-22:00", genre: str = "action") -> dict:
    load_network(session)
    load_seed(session)
    zhongli = session.scalar(select(Area.id).where(Area.code == "zhongli"))
    cinema = Cinema(atmovies_id="t03315", name="威尼斯影城", address="桃園市中壢區元化路357號",
                    geom=point_wkt(24.9620, 121.2246), area_id=zhongli, ticket_price=300)
    movie = Movie(atmovies_id="fm1", title="特務任務", runtime_min=110, genre_code=genre)
    ramen = Restaurant(source="osm", external_id="node/1", geom=point_wkt(24.9622, 121.2249), area_id=zhongli,
                       category_code="japanese", osm_name="一樂拉麵", osm_opening_hours=opening_hours,
                       osm_tags={"amenity": "restaurant", "cuisine": "ramen", "name": "一樂拉麵", "opening_hours": opening_hours})
    session.add_all([cinema, movie, ramen])
    session.flush()
    for start in ("18:00", "20:00"):
        session.add(Showtime(cinema_id=cinema.id, movie_id=movie.id, starts_at=at(start),
                             ends_at=at(start) + dt.timedelta(minutes=110), version="數位"))
    session.flush()
    locations = dict(session.execute(select(Location.name, Location.id)).all())
    return {"station": locations["中壢車站"], "sogo": locations["太平洋SOGO 中壢店"], "bade": locations["介壽大智路口"]}


def request(locations: dict, **overrides) -> dict:
    body = {
        "area": "zhongli", "origin_location_id": locations["station"], "destination_location_id": locations["station"],
        "order": "movie_first", "depart_at": at("17:00").isoformat(), "movie_genre": None, "cuisine": None,
        "party_size": 2, "budget_total": 2000,
    }
    return body | overrides
