"""MCP（規格書 6.7）：手寫的 5 個工具、和 REST 相同的輸出與錯誤代碼、限流、進度通知、/mcp 的掛載。

工具的測試用 SDK 的 Client 直接連 MCPServer（不經過 HTTP，IP 是 unknown）；規劃一樣用執行緒假裝 worker。
"""
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from plan_data import build, request
from test_api import fake_worker

from app.config import settings
from app.db import SessionLocal
from app.jobs import PLANS, queue
from app.main import app
from app.mcp_server import mcp

pytestmark = [pytest.mark.usefixtures("redis_client"), pytest.mark.anyio]

IP = "unknown"  # 不經過 HTTP 時沒有 IP


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def locations(committed):
    with SessionLocal() as session:
        found = build(session)
        session.commit()
    return found


async def call(name: str, args: dict | None = None, **kwargs):
    async with Client(mcp) as client:
        return await client.call_tool(name, args or {}, **kwargs)


def error_text(result) -> str:
    assert result.is_error
    return result.content[0].text


async def test_five_tools():
    async with Client(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"list_areas", "list_locations", "list_categories", "data_coverage", "plan_date"}
    assert tools["list_areas"].annotations.read_only_hint and not tools["plan_date"].annotations.read_only_hint
    schema = tools["plan_date"].input_schema
    assert set(schema["required"]) == {"area", "origin_location_id", "destination_location_id", "order", "depart_at",
                                       "budget_total"}
    assert "ctx" not in schema["properties"]


async def test_queries_match_rest(database, locations):
    rest = TestClient(app)
    for name, args, path in [
        ("list_areas", {}, "/api/areas"),
        ("list_locations", {"area": "zhongli"}, "/api/areas/zhongli/locations"),
        ("list_categories", {"kind": "cuisine"}, "/api/categories?kind=cuisine"),
        ("data_coverage", {}, "/api/coverage"),
    ]:
        result = await call(name, args)
        assert not result.is_error and result.structured_content == rest.get(path).json(), name


async def test_query_errors_use_the_rest_codes(database, monkeypatch, redis_client):
    assert "NOT_FOUND：沒有這個區域：tainan" in error_text(await call("list_locations", {"area": "tainan"}))
    assert (await call("list_categories", {"kind": "drink"})).is_error  # 不在選項裡：SDK 先擋下
    redis_client.delete(f"rl:api:read:{IP}")
    monkeypatch.setattr(settings, "api_read_limit_per_minute", 1)
    assert not (await call("list_areas")).is_error
    assert "RATE_LIMITED" in error_text(await call("list_areas"))


async def test_plan_date_reports_progress_then_returns_the_result(locations, redis_client):
    progress = []

    async def on_progress(value, total, message):
        progress.append((value, message))

    worker = fake_worker()
    result = await call("plan_date", request(locations), progress_callback=on_progress)
    worker.join(timeout=10)
    assert not result.is_error
    data = result.structured_content
    assert data["plans"][0]["rank"] == 1 and data["restaurant_source"] == "osm" and data["job_id"]
    values = [v for v, _ in progress]
    assert values == sorted(set(values)) and values[0] == 1  # 一直往上加
    messages = [m for _, m in progress]
    assert messages[0] == "排隊中，第 1 位" and "讀取資料" in messages and any(m.startswith("Jev 判斷") for m in messages)
    assert not redis_client.exists(f"active:plan:{IP}") and redis_client.zcard(f"rl:api:plan:{IP}") == 1


@pytest.mark.parametrize(
    ("overrides", "words"),
    [
        ({"depart_at": "2026-10-20T17:30:00"}, "VALIDATION_ERROR：depart_at：出發時間要帶時區"),
        ({"budget_total": 0}, "VALIDATION_ERROR：budget_total：要大於 0"),
        ({"destination_location_id": 999999}, "NOT_FOUND：找不到終點"),
    ],
)
async def test_plan_date_rejects_bad_input_before_queueing(locations, overrides, words):
    assert words in error_text(await call("plan_date", request(locations, **overrides)))
    assert queue(PLANS).count == 0


async def test_plan_date_limits_and_no_showtimes(locations, redis_client):
    bade = request(locations, area="bade", origin_location_id=locations["bade"], destination_location_id=locations["bade"])
    assert "NO_SHOWTIMES：八德還沒有場次資料" in error_text(await call("plan_date", bade))
    redis_client.set(f"active:plan:{IP}", "another-job")
    assert "PLAN_IN_PROGRESS" in error_text(await call("plan_date", request(locations)))
    assert queue(PLANS).count == 0


# ---------- 掛在 /mcp ----------

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_mcp_is_mounted_next_to_the_api(database, monkeypatch):
    with TestClient(app, base_url="http://localhost:8000") as client:
        resp = client.post("/mcp", json=INITIALIZE, headers=HEADERS)
        assert resp.status_code == 200 and "date-planner" in resp.text
        assert client.get("/api/health").status_code == 200
    # 不在允許清單的網域：擋下（防 DNS rebinding）；MCP_ALLOWED_HOSTS 加上之後才接受
    with TestClient(app, base_url="http://date.example.com") as client:
        assert client.post("/mcp", json=INITIALIZE, headers=HEADERS).status_code in (400, 421)
    monkeypatch.setattr(settings, "mcp_allowed_hosts", "date.example.com")
    with TestClient(app, base_url="http://date.example.com") as client:  # 重新啟動才會套用
        assert client.post("/mcp", json=INITIALIZE, headers=HEADERS).status_code == 200
