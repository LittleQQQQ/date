"""REST API（規格書第 6 節）：查詢端點、錯誤格式、IP 限流、規劃的 SSE 串流、斷線取消。

規劃的測試沒有真的 worker：用一個執行緒等工作進佇列，再直接呼叫 run_plan（和 worker 做的事一樣）。
"""
import json
import threading
import time

import pytest
import redis
from fastapi.testclient import TestClient
from plan_data import TOMORROW, build, request
from rq.job import JobStatus

from app import plan_stream
from app.config import settings
from app.db import SessionLocal
from app.jobs import PLANS, queue
from app.jobs.plan_job import run_plan
from app.main import app
from app.plan_stream import PlanRun
from app.redis_utils.ratelimit import SlidingWindow
from app.schemas import PlanRequest

pytestmark = pytest.mark.usefixtures("redis_client")

IP = "testclient"  # TestClient 的 request.client.host


@pytest.fixture
def client(database):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def locations(committed):
    with SessionLocal() as session:
        found = build(session)
        session.commit()
    return found


def error_of(resp) -> tuple[int, str]:
    body = resp.json()
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}
    return resp.status_code, body["error"]["code"]


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = [line for line in block.splitlines() if not line.startswith(":")]
        if not lines:
            continue
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = next(line[6:] for line in lines if line.startswith("data: "))
        events.append((event, json.loads(data)))
    return events


# ---------- 查詢 ----------


def test_areas(client):
    resp = client.get("/api/areas")
    assert resp.status_code == 200
    assert [a["code"] for a in resp.json()["areas"]] == ["bade", "taoyuan", "zhongli", "qingpu"]


def test_locations_and_categories(client, locations):
    found = client.get("/api/areas/zhongli/locations").json()
    station = next(loc for loc in found["locations"] if loc["name"] == "中壢車站")
    assert found["area"] == "zhongli" and station["id"] == locations["station"] and station["lat"] == pytest.approx(24.9536)
    assert error_of(client.get("/api/areas/tainan/locations")) == (404, "NOT_FOUND")

    cuisines = client.get("/api/categories", params={"kind": "cuisine"}).json()["categories"]
    assert cuisines and {c["kind"] for c in cuisines} == {"cuisine"} and "japanese" in {c["code"] for c in cuisines}
    both = client.get("/api/categories").json()["categories"]
    assert {c["kind"] for c in both} == {"movie_genre", "cuisine"}
    assert error_of(client.get("/api/categories", params={"kind": "drink"})) == (400, "VALIDATION_ERROR")


def test_coverage(client, locations):
    body = client.get("/api/coverage").json()
    by_code = {a["code"]: a for a in body["areas"]}
    assert by_code["zhongli"]["showtime_from"] == by_code["zhongli"]["showtime_to"] == TOMORROW.isoformat()
    assert by_code["bade"]["showtime_from"] is None and body["restaurant_source"] == "osm"


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == body["database"] == body["redis"] == "ok" and body["postgis"]


def test_unknown_path_uses_the_error_format(client):
    assert error_of(client.get("/api/nothing")) == (404, "NOT_FOUND")


def test_read_rate_limit_per_ip(client, monkeypatch):
    monkeypatch.setattr(settings, "api_read_limit_per_minute", 2)
    assert client.get("/api/areas").status_code == client.get("/api/categories").status_code == 200
    resp = client.get("/api/areas")
    assert error_of(resp) == (429, "RATE_LIMITED") and 1 <= int(resp.headers["Retry-After"]) <= 60
    assert client.get("/api/health").status_code == 200  # 健康檢查不限流


def test_redis_down_is_503(client, monkeypatch):
    def down(self):
        raise redis.exceptions.ConnectionError("連不上")

    monkeypatch.setattr(SlidingWindow, "try_acquire", down)
    assert error_of(client.get("/api/areas")) == (503, "SERVICE_UNAVAILABLE")


# ---------- 送出規劃：檢查 ----------


@pytest.mark.parametrize(
    ("overrides", "status", "code", "words"),
    [
        ({"budget_total": None}, 400, "VALIDATION_ERROR", "budget_total"),
        ({"order": "dessert_first"}, 400, "VALIDATION_ERROR", "只能是"),
        ({"party_size": 11}, 400, "VALIDATION_ERROR", "不能大於 10"),
        ({"depart_at": "2026-10-20T17:30:00"}, 400, "VALIDATION_ERROR", "時區"),
        ({"origin_location_id": "bade"}, 400, "VALIDATION_ERROR", "不在中壢"),
        ({"destination_location_id": 999999}, 404, "NOT_FOUND", "找不到終點"),
    ],
)
def test_invalid_plan_requests_are_rejected_before_queueing(client, locations, overrides, status, code, words):
    body = request(locations)
    for key, value in overrides.items():
        if value is None:
            body.pop(key)
        else:
            body[key] = locations.get(value, value) if isinstance(value, str) and key.endswith("_id") else value
    resp = client.post("/api/plans", json=body)
    assert error_of(resp) == (status, code) and words in resp.json()["error"]["message"]
    assert queue(PLANS).count == 0


def test_area_without_showtimes_streams_one_error(client, locations, redis_client):
    body = request(locations, area="bade", origin_location_id=locations["bade"], destination_location_id=locations["bade"])
    resp = client.post("/api/plans", json=body)
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert parse_sse(resp.text) == [("error", {"code": "NO_SHOWTIMES", "message": "八德還沒有場次資料"})]
    assert queue(PLANS).count == 0 and not redis_client.exists(f"rl:api:plan:{IP}")


def test_one_plan_at_a_time_per_ip(client, locations, redis_client):
    redis_client.set(f"active:plan:{IP}", "another-job")
    assert error_of(client.post("/api/plans", json=request(locations))) == (409, "PLAN_IN_PROGRESS")
    assert redis_client.get(f"active:plan:{IP}") == "another-job" and queue(PLANS).count == 0


def test_plans_per_hour_per_ip(client, locations, redis_client, monkeypatch):
    monkeypatch.setattr(settings, "api_plan_limit_per_hour", 1)
    SlidingWindow(f"rl:api:plan:{IP}", 1, 3600).try_acquire()
    resp = client.post("/api/plans", json=request(locations))
    assert error_of(resp) == (429, "RATE_LIMITED") and int(resp.headers["Retry-After"]) > 3500
    assert not redis_client.exists(f"active:plan:{IP}")  # 被擋下來也要釋放名額


# ---------- 送出規劃：串流 ----------


def fake_worker() -> threading.Thread:
    """等工作進 plans 佇列，取出來執行 run_plan。"""
    def work():
        q = queue(PLANS)
        deadline = time.monotonic() + 10
        while not q.job_ids and time.monotonic() < deadline:
            time.sleep(0.05)
        job = q.fetch_job(q.job_ids[0])
        q.remove(job.id)
        job.set_status(JobStatus.STARTED)
        run_plan(*job.args, job_id=job.id)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def test_plan_streams_queued_progress_then_result(client, locations, redis_client):
    worker = fake_worker()
    resp = client.post("/api/plans", json=request(locations))
    worker.join(timeout=10)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache" and resp.headers["x-accel-buffering"] == "no"
    events = parse_sse(resp.text)
    names = [name for name, _ in events]
    assert names[0] == "queued" and events[0][1]["position"] == 1 and names[-1] == "result"
    stages = [data["stage"] for name, data in events if name == "progress"]
    assert stages[0] == "loading_data" and "judging" in stages
    result = events[-1][1]
    assert result["job_id"] == events[0][1]["job_id"] and result["plans"][0]["rank"] == 1
    # 結束後：名額釋放，計一次每小時的規劃次數，結果沒有存進 Redis
    assert not redis_client.exists(f"active:plan:{IP}") and redis_client.zcard(f"rl:api:plan:{IP}") == 1
    strings = [redis_client.get(k) for k in redis_client.scan_iter() if redis_client.type(k) == "string"]
    assert strings and not any("一樂拉麵" in v or '"plans"' in v for v in strings)  # 路線快取也不存地點名稱


# ---------- 斷線、逾時、worker 掛掉 ----------


@pytest.fixture
def anyio_backend():
    return "asyncio"


def plan_request() -> PlanRequest:
    return PlanRequest.model_validate(request({"station": 1}))


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(plan_stream, "POLL_S", 0.05)
    monkeypatch.setattr(plan_stream, "STATUS_EVERY_S", 0)
    monkeypatch.setattr(plan_stream, "GRACE_S", 0)


async def first_events(run: PlanRun, n: int = 99, **kwargs) -> list[dict]:
    """前 n 個事件（略過「沒事」的 None），或到串流結束為止。"""
    found = []
    async for event in run.events(**kwargs):
        if event is not None:
            found.append(event)
            if len(found) == n:
                break
    return found


async def next_event(events) -> dict:
    while (event := await anext(events)) is None:
        pass
    return event


@pytest.mark.anyio
async def test_disconnect_cancels_the_queued_job_and_frees_the_slot(redis_client):
    run = await PlanRun.start(plan_request(), "1.2.3.4")
    assert redis_client.get("active:plan:1.2.3.4") == run.job_id
    [queued] = await first_events(run, 1)
    assert queued == {"event": "queued", "data": {"job_id": run.job_id, "position": 1}}
    await run.close()  # 使用者斷線：串流的 finally 會呼叫
    assert run.job.get_status() == JobStatus.CANCELED and queue(PLANS).count == 0
    assert not redis_client.exists("active:plan:1.2.3.4")


@pytest.mark.anyio
async def test_queue_position_updates(redis_client, fast):
    ahead = queue(PLANS).enqueue(run_plan, {}, job_id="ahead")
    run = await PlanRun.start(plan_request(), "1.2.3.4")
    try:
        events = run.events()
        assert (await next_event(events))["data"]["position"] == 2
        ahead.cancel()  # 前面的工作離開佇列
        assert await next_event(events) == {"event": "queued", "data": {"job_id": run.job_id, "position": 1}}
        await events.aclose()
    finally:
        await run.close()


@pytest.mark.anyio
async def test_timeout_and_dead_worker_end_with_an_error(redis_client, fast):
    run = await PlanRun.start(plan_request(), "1.2.3.4")
    try:
        events = await first_events(run, timeout_s=0)
        assert events[-1]["data"]["code"] == "TIMEOUT" and not run.done
    finally:
        await run.close()
    assert run.job.get_status() == JobStatus.CANCELED  # 逾時也要取消工作

    run = await PlanRun.start(plan_request(), "1.2.3.4")
    try:
        run.job.set_status(JobStatus.FAILED)  # worker 掛掉：工作失敗卻沒有推送任何結果
        events = await first_events(run)
        assert [e["event"] for e in events] == ["queued", "error"] and events[-1]["data"]["code"] == "UPSTREAM_ERROR"
    finally:
        await run.close()
    assert not redis_client.exists("active:plan:1.2.3.4")


# ---------- 網頁 ----------


def test_frontend_is_served_next_to_the_api(client):
    page = client.get("/")
    assert page.status_code == 200 and 'id="plan-form"' in page.text and page.headers["content-type"].startswith("text/html")
    script = client.get("/app.js")
    assert script.status_code == 200 and "/api/plans" in script.text
    assert error_of(client.get("/api/nothing")) == (404, "NOT_FOUND")  # API 的路徑不會被網頁蓋掉
