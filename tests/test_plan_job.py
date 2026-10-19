"""規劃工作（規格書 5.7、7.1）：透過 Redis Pub/Sub 推送進度和結果；結果只推一次、不存。"""
import json

import pytest
from plan_data import build, request

from app.db import SessionLocal
from app.jobs.plan_job import run_plan
from app.progress import channel, status_key

pytestmark = pytest.mark.usefixtures("redis_client")


@pytest.fixture
def locations(committed):
    with SessionLocal() as session:
        found = build(session)
        session.commit()
    return found


def run(redis_client, body, job_id="job-1"):
    sub = redis_client.pubsub()
    sub.subscribe(channel(job_id))
    sub.get_message(timeout=1)  # 訂閱成功的通知
    returned = run_plan(body, job_id=job_id)
    events = []
    while (msg := sub.get_message(timeout=1)) is not None:
        events.append(json.loads(msg["data"]))
    sub.close()
    return returned, events


def test_progress_then_result(redis_client, locations):
    returned, events = run(redis_client, request(locations))
    stages = [e["data"]["stage"] for e in events if e["event"] == "progress"]
    assert stages[0] == "loading_data" and "building_candidates" in stages and "routing" in stages and "judging" in stages
    final = events[-1]
    assert final["event"] == "result" and final["data"]["job_id"] == "job-1"
    plans = final["data"]["plans"]
    assert plans and plans[0]["rank"] == 1 and final["data"]["restaurant_source"] == "osm"
    first = plans[0]["timeline"]
    assert first[0]["from"] == "中壢車站" and first[1]["title"] == "特務任務" and first[3]["name"] == "一樂拉麵"
    assert returned == final["data"]
    status = redis_client.hgetall(status_key("job-1"))
    assert status["state"] == "done" and "plans" not in json.dumps(status)  # 狀態裡不含結果
    assert 0 < redis_client.ttl(status_key("job-1")) <= 3600


def test_errors_are_pushed_with_a_code(redis_client, locations):
    _, events = run(redis_client, request(locations, origin_location_id=locations["bade"]), job_id="job-2")
    assert events[-1] == {"event": "error", "data": {"code": "VALIDATION_ERROR", "message": "出發地「介壽大智路口」不在中壢"}}
    _, events = run(redis_client, request(locations, movie_genre="horror"), job_id="job-3")
    assert events[-1]["data"]["code"] == "NO_SHOWTIMES"
    _, events = run(redis_client, request(locations, cuisine="korean"), job_id="job-4")
    assert events[-1]["data"]["code"] == "NO_CANDIDATES"
