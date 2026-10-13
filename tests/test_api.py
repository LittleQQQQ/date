import anyio
from mcp import Client

from app.llm import rule_parse
from app.mcp_server import mcp

MESSAGE = "這週六想在信義區看《新片 A》IMAX，從板橋出發，晚上10點半前要到家，想吃義式，每人預算1500"


def test_meta(client):
    data = client.get("/api/meta").json()
    assert {"板橋", "台北車站"} <= {o["name"] for o in data["origins"]}
    assert "新片 A" in data["movies"]
    assert data["data"]["is_demo"]
    assert "愛情" in data["options"]["genres"] and "素食" in data["options"]["diets"]
    assert data["data"]["has_showtimes"]
    assert {s["label"] for s in data["data"]["sources"]} >= {"示意資料", "商圈與交通"}


def test_meta_lists_all_22_cities(client):
    cities = client.get("/api/meta").json()["cities"]
    assert len(cities) == 22
    with_areas = {c["name"] for c in cities if c["areas"]}
    assert with_areas == {"台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市"}
    hsinchu = next(c for c in cities if c["name"] == "新竹縣")
    assert not hsinchu["areas"] and hsinchu["origins"]  # 沒有商圈資料，但可以從高鐵新竹出發


def test_meta_dates_for_dropdowns(client, saturday):
    cities = client.get("/api/meta").json()["cities"]
    xinyi = next(a for c in cities for a in c["areas"] if a["id"] == "台北市/信義")
    dates = xinyi["dates"]
    assert dates[0]["date"] == min(d["date"] for d in dates)
    day = next(d for d in dates if d["date"] == saturday.isoformat())
    assert day["label"].endswith("（六）")
    assert "新片 A" in day["movies"]


def test_rule_parse(client, saturday):
    meta = client.get("/api/meta").json()
    fields = rule_parse(MESSAGE, saturday, meta)
    assert fields["origin"] == "ntp-banqiao"
    assert fields["area"] == "台北市/信義"
    assert fields["movie_query"] == "新片 A"
    assert fields["screen_types"] == ["IMAX"]
    assert fields["home_by"] == "22:30"
    assert fields["cuisines"] == ["義式／西式"]
    assert fields["budget_per_person"] == 1500


def test_plan_endpoint_goes_through_mcp(client, saturday):
    resp = client.post("/api/plan", json={"date": saturday.isoformat(), "home_by": "22:30", "cuisines": ["義式／西式"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["plans"]
    assert data["summary_source"] == "template"  # 測試時不連 LLM
    assert data["summary"]


def test_plan_endpoint_rejects_bad_input(client, saturday):
    resp = client.post("/api/plan", json={"date": saturday.isoformat(), "screen_types": ["巨無霸"]})
    assert resp.status_code == 422


def test_plan_endpoint_rejects_home_before_departure(client, saturday):
    resp = client.post("/api/plan", json={"date": saturday.isoformat(), "depart_after": "20:00", "home_by": "18:00"})
    assert resp.status_code == 422


def test_plan_endpoint_reports_unknown_origin(client, saturday):
    resp = client.post("/api/plan", json={"date": saturday.isoformat(), "origin": "火星"})
    assert resp.status_code == 400
    assert "找不到出發站" in resp.json()["detail"]


def test_ambiguous_station_name_asks_for_code(client, saturday):
    resp = client.post("/api/plan", json={"date": saturday.isoformat(), "origin": "市政府"})
    assert resp.status_code == 400
    assert "tpe-cityhall" in resp.json()["detail"] and "txg-cityhall" in resp.json()["detail"]


def test_area_without_data(client, saturday):
    resp = client.post("/api/plan", json={"date": saturday.isoformat(), "area": "花蓮縣/市區"})
    assert resp.status_code == 400
    assert "還沒有" in resp.json()["detail"]


def test_chat_falls_back_to_rules_without_llm(client):
    data = client.post("/api/chat", json={"message": MESSAGE}).json()
    assert data["parse"]["mode"] == "rules"
    assert data["request"]["origin"] == "ntp-banqiao"
    assert data["result"]["plans"]
    assert data["summary_source"] == "template"


def test_mcp_tools(client, saturday):
    async def run():
        async with Client(mcp) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            result = await c.call_tool("plan_date", {"date": saturday.isoformat(), "movie_query": "新片 A"})
            bad = await c.call_tool("plan_date", {"date": "不是日期"})
            return names, result, bad

    names, result, bad = anyio.run(run)
    assert {"list_areas", "plan_date", "search_showtimes", "find_restaurants", "check_route"} <= names
    assert result.structured_content["plans"]
    assert bad.is_error


def test_chat_before_any_area_has_data(client, monkeypatch):
    from app import services

    real_meta = services.meta
    monkeypatch.setattr(services, "meta", lambda session: {**real_meta(session), "areas": []})
    resp = client.post("/api/chat", json={"message": "明天從板橋出發看電影"})
    assert resp.status_code == 400
    assert "還沒有可以排程的商圈資料" in resp.json()["detail"]
