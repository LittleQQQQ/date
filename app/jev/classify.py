"""寫入時的 Jev 判斷（規格書 7.2、7.3）：答案不因人而異，存進 jev_judgments，也寫回主表的欄位。

| 對象 | 題目 | 寫回 |
| 電影 | genre（電影種類＋other） | movies.genre_code |
| 影城 | area（4 個區域＋none） | cinemas.area_id |
| 餐廳 | cuisine（餐廳類別＋other）、area | restaurants.category_code、restaurants.area_id |

什麼時候要（重新）判斷：還沒判斷過、判斷超過 30 天、或由 Jev 判斷但 Jev 的版本換了。
Jev 沒把握、LLM 也沒幫上忙（沒開、逾時、說無法判斷）的答案照樣存（資料才能用），但隔天就再判斷一次，
不要讓一次 LLM 不能用，就把沒把握的答案留一個月。Jev 和 LLM 都沒結果的，這次不存，下次再判斷。
Google 來源的餐廳店名不能存，在每週找餐廳時（資料還在記憶體裡）由 judge_google_restaurants 判斷。
"""
from __future__ import annotations

import datetime as dt
from collections import Counter

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..collectors.coverage import Coverage, load_coverage
from ..config import settings
from ..collectors.osm import address, cuisine_category, describe
from ..models import Area, Category, Cinema, JevJudgment, Movie, Restaurant, lat_of, lng_of
from ..timeutil import now_tw
from .client import Answer
from .questions import NONE, area_question, cuisine_question, genre_question
from .service import get_jev, judge, model_name

REJUDGE_AFTER = dt.timedelta(days=30)
RETRY_UNSURE_AFTER = dt.timedelta(days=1)


def _stale(session: Session, subject_type: str, question: str, ids: list[int], model: str | None,
           retry_unsure_after: dt.timedelta = RETRY_UNSURE_AFTER) -> list[int]:
    """這些對象裡，這一題需要（重新）判斷的。"""
    if not ids:
        return []
    rows = session.execute(
        select(JevJudgment.subject_id, JevJudgment.judged_at, JevJudgment.decided_by, JevJudgment.model_version,
               JevJudgment.confidence)
        .where(JevJudgment.subject_type == subject_type, JevJudgment.question == question, JevJudgment.subject_id.in_(ids))
    ).all()
    done = {sid: (at, by, version, conf) for sid, at, by, version, conf in rows}
    now = now_tw()
    threshold = settings.jev_confidence_threshold
    stale = []
    for sid in ids:
        if sid not in done:
            stale.append(sid)
            continue
        at, by, version, conf = done[sid]
        unsure = by == "jev" and conf is not None and conf < threshold
        if (at < now - REJUDGE_AFTER or (by == "jev" and model and version != model)
                or (unsure and at <= now - retry_unsure_after)):
            stale.append(sid)
    return stale


def _save(session: Session, subject_type: str, subject_id: int, answer: Answer) -> None:
    stmt = insert(JevJudgment).values(
        subject_type=subject_type, subject_id=subject_id, question=answer.question, answer=answer.choice,
        probabilities=answer.probabilities, confidence=answer.confidence, decided_by=answer.decided_by,
        model_version=answer.model, judged_at=now_tw(),
    )
    session.execute(stmt.on_conflict_do_update(
        index_elements=["subject_type", "subject_id", "question"],
        set_={c: stmt.excluded[c] for c in ("answer", "probabilities", "confidence", "decided_by", "model_version", "judged_at")}
        | {"updated_at": now_tw()},
    ))


def _nearest_line(coverage: Coverage, area_names: dict[str, str], lat: float, lng: float) -> str:
    found = coverage.nearest(lat, lng)
    if found is None:
        return "最近的車站：800 公尺內沒有車站"
    station, meters = found
    return f"最近的車站：{station.name}（區域：{area_names.get(station.area_code, station.area_code)}，{meters:.0f} 公尺）"


def judge_pending(session: Session, jev=None, limit: int | None = None,
                  retry_unsure_after: dt.timedelta = RETRY_UNSURE_AFTER) -> dict:
    """判斷所有需要判斷的對象，回傳統計。

    limit：每一類最多判斷幾筆；retry_unsure_after：沒把握的答案多久後重問（手動補判斷時可以設 0）。
    """
    jev = jev or get_jev()
    model = model_name(jev)
    stats: Counter = Counter()
    genres = [tuple(r) for r in session.execute(
        select(Category.code, Category.label, Category.jev_description).where(Category.kind == "movie_genre").order_by(Category.sort_order))]
    cuisines = [tuple(r) for r in session.execute(
        select(Category.code, Category.label, Category.jev_description).where(Category.kind == "cuisine").order_by(Category.sort_order))]
    area_ids = dict(session.execute(select(Area.code, Area.id)).all())
    areas = [(code, name) for code, name in session.execute(select(Area.code, Area.name).order_by(Area.id))]
    coverage = load_coverage(session)
    area_names = dict(areas)

    def record(subject_type: str, subject_id: int, answers: dict) -> dict:
        saved = {}
        for qid, answer in answers.items():
            if answer is None:
                stats["failed"] += 1
                continue
            _save(session, subject_type, subject_id, answer)
            stats[f"by_{answer.decided_by}"] += 1
            saved[qid] = answer.choice
        return saved

    if genres:
        question = genre_question(genres)
        ids = _stale(session, "movie", "genre", list(session.scalars(select(Movie.id).order_by(Movie.id))), model, retry_unsure_after)[:limit]
        for movie in session.scalars(select(Movie).where(Movie.id.in_(ids))):
            state = f"片名：{movie.title}\n片長：{movie.runtime_min} 分鐘\n劇情簡介：{movie.synopsis or '（沒有簡介）'}"
            saved = record("movie", movie.id, judge(state, [question], jev))
            if "genre" in saved:
                movie.genre_code = saved["genre"]
            stats["movies"] += 1

    area_q = area_question(areas)
    ids = _stale(session, "cinema", "area", list(session.scalars(select(Cinema.id).where(Cinema.geom.is_not(None)))), model, retry_unsure_after)[:limit]
    for cinema, lat, lng in session.execute(
        select(Cinema, lat_of(Cinema.geom), lng_of(Cinema.geom)).where(Cinema.id.in_(ids))
    ):
        state = f"影城：{cinema.name}\n地址：{cinema.address or '（沒有地址）'}\n{_nearest_line(coverage, area_names, lat, lng)}"
        saved = record("cinema", cinema.id, judge(state, [area_q], jev))
        if "area" in saved:
            cinema.area_id = area_ids.get(saved["area"]) if saved["area"] != NONE else None
        stats["cinemas"] += 1

    if cuisines:
        cuisine_q = cuisine_question(cuisines)
        osm_ids = list(session.scalars(select(Restaurant.id).where(Restaurant.source == "osm", Restaurant.is_active)))
        ids = sorted(set(_stale(session, "restaurant", "cuisine", osm_ids, model, retry_unsure_after)) | set(_stale(session, "restaurant", "area", osm_ids, model, retry_unsure_after)))[:limit]
        for rest, lat, lng in session.execute(
            select(Restaurant, lat_of(Restaurant.geom), lng_of(Restaurant.geom)).where(Restaurant.id.in_(ids))
        ):
            tags = rest.osm_tags or {}
            state = restaurant_state(rest.osm_name or "", tags, address(tags), coverage, area_names, lat, lng)
            saved = record("restaurant", rest.id, judge(state, [cuisine_q, area_q], jev))
            if "cuisine" in saved:
                rest.category_code = saved["cuisine"]
            if "area" in saved:
                rest.area_id = area_ids.get(saved["area"]) if saved["area"] != NONE else None
            stats["restaurants"] += 1

    session.flush()
    return dict(stats)


def restaurant_state(name: str, tags: dict, addr: str, coverage: Coverage, area_names: dict[str, str],
                     lat: float, lng: float) -> str:
    """給 Jev 判斷餐廳類別、區域的文字（OSM、Google 共用）。Google 的店名只在記憶體裡，不存。"""
    return (
        f"店名：{name}\n介紹：{describe(tags, cuisine_category(tags))}\n"
        f"地址：{addr or '（沒有地址）'}\n{_nearest_line(coverage, area_names, lat, lng)}"
    )


def judge_google_restaurants(session: Session, places: dict[int, dict], jev=None,
                             retry_unsure_after: dt.timedelta = RETRY_UNSURE_AFTER) -> dict:
    """Google 來源的餐廳：places 是 restaurant id → 這次 Nearby Search 拿到的資料（只在記憶體裡）。

    Google 的店名不能存，所以只能在每週找餐廳的時候、資料還在記憶體裡時判斷；需要（重新）判斷的才問。
    """
    from ..collectors.google_places import name_of, to_tags

    jev = jev or get_jev()
    model = model_name(jev)
    stats: Counter = Counter()
    cuisines = [tuple(r) for r in session.execute(
        select(Category.code, Category.label, Category.jev_description).where(Category.kind == "cuisine").order_by(Category.sort_order))]
    areas = [(code, name) for code, name in session.execute(select(Area.code, Area.name).order_by(Area.id))]
    if not cuisines or not places:
        return {}
    area_ids, area_names, coverage = dict(session.execute(select(Area.code, Area.id)).all()), dict(areas), load_coverage(session)
    questions = [cuisine_question(cuisines), area_question(areas)]
    ids = list(places)
    stale = set(_stale(session, "restaurant", "cuisine", ids, model, retry_unsure_after)) | set(
        _stale(session, "restaurant", "area", ids, model, retry_unsure_after))
    for rest in session.scalars(select(Restaurant).where(Restaurant.id.in_(sorted(stale)))):
        place = places[rest.id]
        loc = place["location"]
        state = restaurant_state(name_of(place), to_tags(place), place.get("formattedAddress", ""), coverage, area_names,
                                 loc["latitude"], loc["longitude"])
        for qid, answer in judge(state, questions, jev).items():
            if answer is None:
                stats["failed"] += 1
                continue
            _save(session, "restaurant", rest.id, answer)
            stats[f"by_{answer.decided_by}"] += 1
            if qid == "cuisine":
                rest.category_code = answer.choice
            else:
                rest.area_id = area_ids.get(answer.choice) if answer.choice != NONE else None
        stats["restaurants"] += 1
    session.flush()
    return dict(stats)
