# 範圍先縮小到桃園 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 約會商圈先只開放桃園的桃園區、青埔、中壢、八德，出發站也只能選桃園市內的站；其他縣市的資料不刪，改一個設定就能恢復全台。

**Architecture:**
- **資料：** 在 `data/fixture/桃園市.json` 加三個商圈、四個公車站與六條公車路線。資料格式不變，排程與爬蟲本來就從資料庫讀商圈。
- **開放範圍：** 新增設定 `ACTIVE_CITIES` 和小模組 `app/scope.py`。
  - 選單只列開放的縣市。
  - 排程、查場次、查餐廳、查路線，碰到範圍外的商圈或出發站時回錯誤。
  - 開眼、OSM 爬蟲只拿到開放縣市的商圈。
- **最後：** 由控制者在 `dev.db` 匯入資料，並在瀏覽器展示。

**Tech Stack:** Python 3.10+、FastAPI、pydantic-settings、pytest（離線）、JSON 資料檔。

**Spec:** `docs/superpowers/specs/2026-10-13-taoyuan-scope-design.md`（實作前先讀完；座標、車程、班距、末班車以規格表格為準）

## Global Constraints

**環境與流程**
- 專案根目錄 `D:\final\date`，分支 `feat/taoyuan-scope`（從還沒合併的 `feat/jev-transit-choice` 開出）。所有指令在專案根目錄執行。
- Python 一律用 `.venv/Scripts/python.exe`。跑全部測試：`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q`。開始前是 292 個測試、全部通過；每個任務結束時全部測試都要通過。
- 測試完全離線：不連網、不啟動伺服器、不執行 `app.ingest`、不碰 `dev.db`。
- Python、Markdown 檔是 UTF-8、CRLF 換行，修改時保持 CRLF。`data/fixture/*.json` 是 LF，保持 LF。
- 每次 commit 訊息用中文，最後加 `-m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"`。

**資料**
- 縣市一律用「台」字。商圈代碼是「縣市/商圈」：`桃園市/桃園區`、`桃園市/青埔`、`桃園市/中壢`、`桃園市/八德`。
- 公車路線名稱一律以「公車」開頭。
- 不新增機場捷運、桃園捷運綠線；中壢光影電影館不納入；不刪任何資料檔。

**開放範圍**
- 設定 `ACTIVE_CITIES`：逗號分隔，預設 `桃園市`，留空代表全台開放。
- 範圍外的錯誤訊息是「目前只開放」加上開放的縣市，多個時用「、」串起來，依 `app/regions.py` 的縣市順序。例如「目前只開放桃園市」。

---

### Task 1: 桃園四個商圈的資料

**Files:**
- Modify: `data/fixture/桃園市.json`（`areas`、`stations`、`transit_links`）
- Modify: `tests/test_taiwan_network.py`

**Interfaces:**
- Consumes（已存在）：`tests/test_taiwan_network.py` 的 fixture `base`（匯入 base 後的 session）、`CINEMAS`、`MAIN_ISLAND_AREAS`、`_area_stations(session, area_id)`、`test_nineteen_cities_have_areas`；`services.load_graph`；`TransitGraph.earliest_arrival(src, t0, dst)`、`latest_departure(src, dst, deadline)`；`to_min`。
- Produces：
  - 商圈 `桃園市/桃園區`、`桃園市/中壢`、`桃園市/八德`。
  - 桃園市的出發站剛好是 `tyn-taoyuan`、`tyn-zhongli`、`tyn-hsr`、`tyn-bade`。
  - 新車站 `tyn-nanping`、`tyn-sogo`、`tyn-dajiang`、`tyn-bade`。

- [ ] **Step 1: 寫失敗的測試**

`tests/test_taiwan_network.py`：

1. 在 `CINEMAS` 清單最後（`金獅影城` 那列之後）加上：

```python
    ("桃園統領威秀影城", 24.9910614, 121.312373, "桃園市/桃園區"),
    ("桃園站前in89豪華影城", 24.9911841, 121.312827, "桃園市/桃園區"),
    ("桃園桃知道威秀影城", 25.0186174, 121.300141, "桃園市/桃園區"),
    ("桃園新光影城", 25.017023, 121.212666, "桃園市/青埔"),
    ("威尼斯影城", 24.961511, 121.224225, "桃園市/中壢"),
    ("星橋國際影城", 25.0006585, 121.22921, "桃園市/中壢"),
    ("國賓桃園八德", 24.96491, 121.299257, "桃園市/八德"),
    ("中壢光影電影館", 24.936842, 121.238043, None),  # 小型藝術電影館，最近的中壢車站要走 39 分
```

2. `MAIN_ISLAND_AREAS` 在 `"桃園市/青埔",` 前後改成：

```python
    "桃園市/桃園區",
    "桃園市/青埔",
    "桃園市/中壢",
    "桃園市/八德",
```

3. `test_nineteen_cities_have_areas` 的 `assert len(areas) == 20` 改成 `assert len(areas) == 23`（縣市數 19 不變）。

4. 檔尾加上：

```python


# 範圍縮小到桃園時，出發站和商圈都在桃園市內
TAOYUAN_ORIGINS = ["tyn-taoyuan", "tyn-zhongli", "tyn-hsr", "tyn-bade"]
TAOYUAN_AREAS = ["桃園市/桃園區", "桃園市/青埔", "桃園市/中壢", "桃園市/八德"]


def test_taoyuan_origins(base):
    stations = base.scalars(select(Station).where(Station.city == "桃園市", Station.is_origin.is_(True))).all()
    assert sorted(s.id for s in stations) == sorted(TAOYUAN_ORIGINS)


@pytest.mark.parametrize("origin", TAOYUAN_ORIGINS)
@pytest.mark.parametrize("area_id", TAOYUAN_AREAS)
def test_taoyuan_origin_reaches_taoyuan_area(base, origin, area_id):
    """下午兩點出門到得了，晚上十點前也有車回得去（考慮首末班車）。"""
    graph = services.load_graph(base)
    stations = _area_stations(base, area_id)
    assert stations, area_id
    assert any(
        graph.earliest_arrival(origin, to_min("14:00"), s) is not None
        and graph.latest_departure(s, origin, to_min("22:00")) is not None
        for s in stations
    ), (origin, area_id)
```

- [ ] **Step 2: 確認測試失敗**

Run: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_taiwan_network.py -q`
Expected: 失敗。
- 新影城那幾列會落在 `None` 或 `桃園市/青埔`，不是預期的商圈。
- `test_area_lists_cover_every_area` 失敗：清單有商圈，但資料庫沒有。
- `test_nineteen_cities_have_areas` 失敗：20 個不是 23 個。
- `test_taoyuan_origins` 失敗：沒有 `tyn-bade`。
- 新的往返測試失敗：商圈沒有車站。

- [ ] **Step 3: 加上資料**

`data/fixture/桃園市.json`（LF 換行，縮排兩格，一個物件一行，照現有格式）：

1. `_note` 改成：

```json
  "_note": "商圈、車站、交通為手寫估計值，新增公車站的座標取自 OpenStreetMap（2026-10-13）；影城、餐廳、場次（青埔）為虛構的測試資料。",
```

2. `areas` 換成：

```json
  "areas": [
    {"id": "桃園市/桃園區", "city": "桃園市", "name": "桃園區"},
    {"id": "桃園市/青埔", "city": "桃園市", "name": "青埔"},
    {"id": "桃園市/中壢", "city": "桃園市", "name": "中壢"},
    {"id": "桃園市/八德", "city": "桃園市", "name": "八德"}
  ],
```

3. `stations` 換成：

```json
  "stations": [
    {"id": "tyn-taoyuan", "name": "桃園", "city": "桃園市", "lat": 24.9891, "lng": 121.3135, "transfer_min": 4, "is_origin": true, "area_id": "桃園市/桃園區"},
    {"id": "tyn-nanping", "name": "南平中正路口", "city": "桃園市", "lat": 25.018382, "lng": 121.299095, "transfer_min": 2, "is_origin": false, "area_id": "桃園市/桃園區"},
    {"id": "tyn-zhongli", "name": "中壢", "city": "桃園市", "lat": 24.9536, "lng": 121.2257, "transfer_min": 4, "is_origin": true, "area_id": "桃園市/中壢"},
    {"id": "tyn-sogo", "name": "太平洋SOGO", "city": "桃園市", "lat": 24.962084, "lng": 121.224673, "transfer_min": 2, "is_origin": false, "area_id": "桃園市/中壢"},
    {"id": "tyn-dajiang", "name": "大江購物中心", "city": "桃園市", "lat": 25.000812, "lng": 121.229543, "transfer_min": 2, "is_origin": false, "area_id": "桃園市/中壢"},
    {"id": "tyn-hsr", "name": "高鐵桃園", "city": "桃園市", "lat": 25.0130, "lng": 121.2150, "transfer_min": 8, "is_origin": true, "area_id": "桃園市/青埔"},
    {"id": "tyn-bade", "name": "八德（介壽大智路口）", "city": "桃園市", "lat": 24.963762, "lng": 121.300255, "transfer_min": 2, "is_origin": true, "area_id": "桃園市/八德"}
  ],
```

4. `transit_links` 保留原有三條，後面加上六條：

```json
    {"from": "tyn-taoyuan", "to": "tyn-nanping", "mode": "公車", "line": "公車（桃園車站—桃知道）", "ride": 15, "headway": 10, "first": "06:00", "last": "23:00", "both_ways": true},
    {"from": "tyn-zhongli", "to": "tyn-sogo", "mode": "公車", "line": "公車（中壢車站—SOGO）", "ride": 5, "headway": 10, "first": "06:00", "last": "23:00", "both_ways": true},
    {"from": "tyn-zhongli", "to": "tyn-dajiang", "mode": "公車", "line": "公車（中壢車站—大江）", "ride": 20, "headway": 20, "first": "06:00", "last": "23:00", "both_ways": true},
    {"from": "tyn-hsr", "to": "tyn-dajiang", "mode": "公車", "line": "公車（高鐵桃園—大江）", "ride": 10, "headway": 20, "first": "06:00", "last": "23:00", "both_ways": true},
    {"from": "tyn-taoyuan", "to": "tyn-bade", "mode": "公車", "line": "公車（桃園車站—八德）", "ride": 20, "headway": 15, "first": "06:00", "last": "23:00", "both_ways": true},
    {"from": "tyn-zhongli", "to": "tyn-bade", "mode": "公車", "line": "公車（中壢車站—八德）", "ride": 30, "headway": 20, "first": "06:00", "last": "23:00", "both_ways": true}
```

（`cinemas`、`showtime_templates`、`restaurants` 不動。）

- [ ] **Step 4: 確認測試通過**

Run: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_taiwan_network.py -q`
Expected: 全部通過。
再跑全部測試：`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q`，全部通過。
`tests/test_planner.py` 的 `test_every_area_can_be_planned_from_its_own_city` 用「中壢 → 桃園市/青埔」，中壢車站改歸中壢商圈後仍要通過。

- [ ] **Step 5: Commit**

```bash
git add data/fixture/桃園市.json tests/test_taiwan_network.py
git commit -m "桃園新增桃園區、中壢、八德商圈：七家大型影城都走得到，出發站限桃園車站、中壢車站、高鐵桃園、八德" -m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"
```

---

### Task 2: 開放範圍設定 `ACTIVE_CITIES`

**Files:**
- Modify: `app/config.py`（`Settings` 新欄位與驗證）
- Create: `app/scope.py`
- Modify: `app/services.py`（`_area_and_origin`、`search_showtimes`、`find_restaurants`、`list_cities`）
- Modify: `app/collectors/coverage.py`（`load_coverage`）、`app/collectors/__init__.py`（`get_collector`）
- Modify: `app/api.py`（`/showtimes`）、`app/mcp_server.py`（說明文字、`search_showtimes`）
- Modify: `app/regions.py`（模組說明）、`frontend/app.js`（第 64 行附近的註解）
- Modify: `tests/conftest.py`、`.env.example`、`README.md`
- Create: `tests/test_scope.py`

**Interfaces:**
- Consumes：Task 1 的桃園商圈與出發站；`app/regions.py` 的 `CITY_NAMES`、`TAIWAN_CITIES`；`services.plan_date`、`search_showtimes`、`find_restaurants`、`check_route`、`list_cities`；`get_collector(name, session)`；`AtmoviesCollector.coverage`、`OsmCollector.coverage`（屬性，`Coverage.cities() -> set[str]`）；測試 fixture `session`、`saturday`、`client`（`tests/conftest.py`）。
- Produces：
  - `settings.active_cities: str`
  - `app.scope.active_cities() -> tuple[str, ...]`
  - `app.scope.is_open(city: str) -> bool`
  - `app.scope.require_open(city: str) -> None`
  - `app.scope.require_open_area(area_id: str) -> None`
  - `load_coverage(session, cities: Collection[str] = ()) -> Coverage`

- [ ] **Step 1: 寫失敗的測試**

`tests/conftest.py`：在 `os.environ["AUTO_REFRESH"] = "false"` 下面加一行（測試預設全台開放，現有測試照舊）：

```python
os.environ["ACTIVE_CITIES"] = ""
```

建立 `tests/test_scope.py`：

```python
"""開放範圍（ACTIVE_CITIES）：範圍外的縣市不列在選單、不能排、不更新，但資料留在資料庫。"""
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app import scope, services
from app.collectors import get_collector, load_coverage
from app.config import Settings, settings
from app.models import Cinema
from app.schemas import PlanRequest

TAOYUAN_AREAS = {"桃園市/桃園區", "桃園市/青埔", "桃園市/中壢", "桃園市/八德"}
TAOYUAN_ORIGINS = {"tyn-taoyuan", "tyn-zhongli", "tyn-hsr", "tyn-bade"}
CLOSED = "^目前只開放桃園市$"


@pytest.fixture
def taoyuan_only(monkeypatch):
    monkeypatch.setattr(settings, "active_cities", "桃園市")


def test_empty_setting_opens_everything(monkeypatch):
    monkeypatch.setattr(settings, "active_cities", "")
    assert scope.active_cities() == ()
    assert scope.is_open("台北市")


def test_cities_follow_the_island_order(monkeypatch):
    monkeypatch.setattr(settings, "active_cities", " 桃園市 , 台北市 ")
    assert scope.active_cities() == ("台北市", "桃園市")
    with pytest.raises(ValueError, match="^目前只開放台北市、桃園市$"):
        scope.require_open("台中市")


def test_unknown_city_is_rejected_at_startup():
    with pytest.raises(ValidationError, match="桃員市"):
        Settings(active_cities="桃員市")
    assert Settings(active_cities="桃園市").active_cities == "桃園市"


def test_menu_lists_only_taoyuan(session, taoyuan_only):
    cities = services.list_cities(session)
    assert [c["name"] for c in cities] == ["桃園市"]
    # 測試資料只有青埔有虛構場次，所以只有青埔會出現在選單（真實資料四個商圈都有）
    areas = {a["id"] for a in cities[0]["areas"]}
    assert "桃園市/青埔" in areas and areas <= TAOYUAN_AREAS
    assert {o["id"] for o in cities[0]["origins"]} == TAOYUAN_ORIGINS


def test_taoyuan_plan_still_works(session, saturday, taoyuan_only):
    res = services.plan_date(session, PlanRequest(date=saturday, origin="中壢", area="桃園市/青埔", home_by="23:30"))
    assert res["plans"], res["rejections"]


def test_closed_area_is_rejected(session, saturday, taoyuan_only):
    with pytest.raises(ValueError, match=CLOSED):
        services.plan_date(session, PlanRequest(date=saturday, origin="中壢", area="台北市/信義"))


def test_closed_origin_is_rejected(session, saturday, taoyuan_only):
    with pytest.raises(ValueError, match=CLOSED):
        services.plan_date(session, PlanRequest(date=saturday, origin="台北車站", area="桃園市/中壢"))
    with pytest.raises(ValueError, match=CLOSED):
        services.check_route(session, "台北車站", "桃園市/中壢")


def test_closed_area_lookups_are_rejected(session, saturday, taoyuan_only):
    with pytest.raises(ValueError, match=CLOSED):
        services.search_showtimes(session, saturday, "台北市/信義")
    xinyi = session.scalars(select(Cinema).where(Cinema.area_id == "台北市/信義")).first()
    with pytest.raises(ValueError, match=CLOSED):
        services.find_restaurants(session, xinyi.id)


def test_showtimes_api_reports_closed_area(client, saturday, taoyuan_only):
    resp = client.get("/api/showtimes", params={"date": saturday.isoformat(), "area": "台北市/信義"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "目前只開放桃園市"


def test_scrapers_only_cover_open_cities(session, taoyuan_only):
    for name in ("atmovies", "osm"):
        assert get_collector(name, session).coverage.cities() == {"桃園市"}
    # 直接呼叫 load_coverage（例如路網檢查）時不受設定影響
    assert len(load_coverage(session).cities()) > 1
```

- [ ] **Step 2: 確認測試失敗**

Run: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_scope.py -q`
Expected: 收集失敗：`ImportError: cannot import name 'scope' from 'app'`。

- [ ] **Step 3: 設定與 `app/scope.py`**

`app/config.py`：
- import 加上 `from pydantic import Field, field_validator` 和 `from .regions import CITY_NAMES`。
- 在 `refresh_hour` 後面加上：

```python
    # 開放哪些縣市（逗號分隔，例如 桃園市,台北市）；留空 = 全台。
    # 範圍外的縣市不列在選單、不能排程、爬蟲不更新，但資料留在資料庫，改回來就能恢復
    active_cities: str = "桃園市"

    @field_validator("active_cities")
    @classmethod
    def _check_active_cities(cls, value: str) -> str:
        unknown = [c.strip() for c in value.split(",") if c.strip() and c.strip() not in CITY_NAMES]
        if unknown:
            raise ValueError(f"ACTIVE_CITIES 有不認得的縣市：{'、'.join(unknown)}（請用「台」字，例如桃園市）")
        return value
```

建立 `app/scope.py`：

```python
"""開放範圍：ACTIVE_CITIES 設定哪些縣市可以選。

範圍外的縣市不列在選單、不能排程、爬蟲也不更新，但資料留在資料庫；把設定改回來就能恢復。
"""
from __future__ import annotations

from .config import settings
from .regions import CITY_NAMES


def active_cities() -> tuple[str, ...]:
    """開放的縣市，照全台縣市的順序；空的代表全台開放。"""
    names = {c.strip() for c in settings.active_cities.split(",") if c.strip()}
    return tuple(c for c in CITY_NAMES if c in names)


def is_open(city: str) -> bool:
    cities = active_cities()
    return not cities or city in cities


def require_open(city: str) -> None:
    if not is_open(city):
        raise ValueError(f"目前只開放{'、'.join(active_cities())}")


def require_open_area(area_id: str) -> None:
    """商圈代碼是「縣市/商圈」，用前半段判斷。"""
    require_open(area_id.split("/", 1)[0])
```

- [ ] **Step 4: 服務層、爬蟲、API、MCP**

`app/services.py`：
- import 加上 `from .scope import is_open, require_open, require_open_area`。
- `_area_and_origin`：在 `if area is None: raise ...` 之後加 `require_open(area.city)`；在 `if station is None: raise ...` 之後加 `require_open(station.city)`。
- `search_showtimes`：函式一開頭（`jev = JevService(session)` 之前）加 `require_open_area(area)`。
- `find_restaurants`：在 `if cinema is None: raise ...` 之後加 `require_open_area(cinema.area_id)`。
- `list_cities`：
  - 說明改成 `"""開放範圍內的縣市（ACTIVE_CITIES，預設桃園市），附上各縣市有資料的商圈（含可選日期）與出發站。"""`。
  - 迴圈 `for region, city in TAIWAN_CITIES:` 改成：

```python
    for region, city in TAIWAN_CITIES:
        if not is_open(city):
            continue
```

`app/collectors/coverage.py`：
- 檔頭 import 加上 `from collections.abc import Collection`。
- `load_coverage` 改成：

```python
def load_coverage(session: Session, cities: Collection[str] = ()) -> Coverage:
    """cities 有值時只載入這些縣市的商圈（爬蟲用，見 app/scope.py）；空的代表全部。"""
    areas = []
    for area in session.scalars(select(Area).order_by(Area.id)):
        if cities and area.city not in cities:
            continue
```

  （迴圈裡其餘內容不變。）

`app/collectors/__init__.py`：
- import 加上 `from ..scope import active_cities`。
- `get_collector` 的 `return cls(coverage=load_coverage(session))` 改成：

```python
        # 只抓開放範圍內的商圈；其他縣市的資料留在資料庫，不更新也不刪
        return cls(coverage=load_coverage(session, active_cities()))
```

`app/api.py` 的 `/showtimes` 改成：

```python
@router.get("/showtimes")
def get_showtimes(date: dt.date, area: str = "信義", movie_query: str = "", session: Session = Depends(get_session)):
    try:
        return services.search_showtimes(session, date, area, movie_query)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
```

`app/mcp_server.py`：
- `instructions` 的 `"排出「電影＋餐廳＋交通」的約會行程，涵蓋全台。先用 list_areas 查有哪些商圈與出發站，再呼叫 plan_date。"` 改成 `"排出「電影＋餐廳＋交通」的約會行程。先用 list_areas 查目前開放的商圈與出發站，再呼叫 plan_date。"`。
- `list_areas` 說明改成 `"""列出目前開放的縣市（ACTIVE_CITIES）、各縣市有資料的商圈（含可選日期與當天上映的片）和可以出發的車站。"""`。
- `search_showtimes` 的內容改成：

```python
    with SessionLocal() as session:
        try:
            return services.search_showtimes(session, _date(date), area, movie_query)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
```

`app/regions.py` 的模組說明改成：`"""全台 22 縣市。下拉選單列出開放範圍內的縣市（ACTIVE_CITIES，見 app/scope.py）；沒有資料的縣市會標成「尚無資料」。"""`

`frontend/app.js` 第 64 行附近的註解 `// 全台 22 縣市依地區分組；沒有資料的縣市可以看到但不能選` 改成 `// 開放範圍內的縣市依地區分組；沒有資料的縣市可以看到但不能選`。

- [ ] **Step 5: 說明文件**

`.env.example`：在 `REFRESH_HOUR=6` 後面加上：

```
# 開放哪些縣市（逗號分隔，例如 桃園市,台北市）；留空 = 全台。
# 範圍外的縣市不列在選單、不能排程、不更新，但資料留在資料庫
ACTIVE_CITIES=桃園市
```

`README.md`：
- 開頭清單第 6 行換成：

```markdown
- 目前只開放桃園市（設定 `ACTIVE_CITIES`）：桃園區、青埔、中壢、八德四個有大電影院的商圈，出發站是桃園車站、中壢車站、高鐵桃園、八德。資料檔裡還有全台其他縣市的商圈與交通（19 個縣市），把 `ACTIVE_CITIES` 留空就恢復全台；那時嘉義縣、連江縣、金門縣顯示「尚無資料」，澎湖只能從島上出發。商圈與交通是手寫的估計值。
```

- 「## 設定（.env）」表格最後加一列：

```markdown
| `ACTIVE_CITIES` | `桃園市` | 開放哪些縣市（逗號分隔）；留空 = 全台。範圍外的縣市不列在選單、不能排程、爬蟲不更新，但資料留在資料庫 |
```

- 「## 升級後補抓資料」最後加一段：

```markdown
**改變開放範圍（`ACTIVE_CITIES`）時：** 範圍外的縣市不會被刪，只是不再更新。改完重新啟動，開眼的場次每天會自動補上；餐廳要等 7 天的更新週期，急的話照上面第 4 步手動執行。
```

- MCP 表格 `list_areas` 那列換成：`| \`list_areas\` | 目前開放的縣市（ACTIVE_CITIES）、有資料的商圈（含可選日期與片單）、可以出發的車站 |`
- 「## 目前的限制」最後加一條：`- 目前只開放桃園市；其他縣市的資料還在，但不再更新，恢復後要等爬蟲補抓。`

- [ ] **Step 6: 確認測試通過**

Run: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_scope.py -q`
Expected: 全部通過。
再跑全部測試：`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q`，全部通過（conftest 讓測試預設全台開放）。

- [ ] **Step 7: Commit**

```bash
git add app/config.py app/scope.py app/services.py app/collectors/coverage.py app/collectors/__init__.py app/api.py app/mcp_server.py app/regions.py frontend/app.js tests/conftest.py tests/test_scope.py .env.example README.md
git commit -m "新增開放範圍設定 ACTIVE_CITIES（預設桃園市）：選單、排程、查詢與爬蟲只限開放的縣市，其他資料保留" -m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"
```

---

### Task 3: 匯入桃園資料並展示（由控制者執行，不交給子代理）

需要連網，而且會寫入 `dev.db`，所以在 Task 1、2 審查完之後由控制者執行。

- [ ] **Step 1:** 停止開發伺服器，避免和背景更新同時寫入資料庫。
- [ ] **Step 2:** 依序匯入，每一步都確認結果是 ok：
  1. 商圈與交通：`.venv/Scripts/python.exe -m app.ingest --no-jev`
     - 自動清理的筆數應該都是 0，代表沒有刪到東西。
  2. 開眼：`.venv/Scripts/python.exe -m app.ingest --source atmovies`
     - 只抓桃園的地區頁；桃園的七家影城應該都有場次。
  3. OSM：`.venv/Scripts/python.exe -m app.ingest --source osm`
     - 只查桃園四個商圈。
- [ ] **Step 3:** 確認其他縣市的資料還在：信義的影城、餐廳筆數和匯入前一樣。
- [ ] **Step 4:** 重新啟動開發伺服器，在瀏覽器確認：
  - 縣市只有桃園市，商圈有四個，出發站有四個。
  - 從桃園車站出發排中壢、從中壢車站出發排八德，都能排出方案。
  - console 沒有錯誤。
