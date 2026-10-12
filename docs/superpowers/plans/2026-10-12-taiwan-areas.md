# 可選地區擴大到全台 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 可選商圈從六都 7 個擴大到全台 20 縣市 21 個，每個有影城的縣市 1 個商圈；高雄「巨蛋」改為「三多」；澎湖、金門只能從島上出發。

**Architecture:** 只改 `data/fixture/` 的手寫基本資料（商圈、車站、交通路段，格式不變）與一個新的路網檢查測試檔；排程、爬蟲、匯入程式不用改，因為它們本來就從資料庫讀商圈與路網。另外更新網頁的等待說明與 README。

**Tech Stack:** JSON 資料檔、Python 3.10+、pytest（離線）。

**Spec:** `docs/superpowers/specs/2026-10-12-taiwan-areas-design.md`（實作前先讀完；車站座標、路段數值以規格表格為準）

## Global Constraints

- 專案根目錄 `D:\final\date`，分支 `feat/taiwan-areas`。所有指令在專案根目錄執行。
- Python 一律用 `.venv/Scripts/python.exe`。跑全部測試：`.venv/Scripts/python.exe -m pytest -q`。每個任務結束時全部測試都要通過。
- 測試完全離線，不連網、不啟動伺服器、不執行 `app.ingest`、不碰 `dev.db`。
- 資料檔是 UTF-8 JSON，縣市一律用「台」字（`台東縣`，不是 `臺東縣`）。
- 車站、路段格式照現有 `data/fixture/*.json`：
  - 車站 `{"id", "name", "city", "lat", "lng", "transfer_min", "is_origin", "area_id"}`（沒有商圈就不寫 `area_id`）
  - 路段 `{"from", "to", "mode", "line", "ride", "headway", "first", "last", "both_ways": true}`；`mode` 只能是 `捷運`、`台鐵`、`高鐵`、`輕軌`、`公車`。
- 新的縣市檔只放 `_note`、`areas`、`stations`、`transit_links`，不放虛構影城、餐廳、場次。
- 每次 commit 訊息用中文，最後加 `-m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"`。

## 檔案結構

| 檔案 | 狀態 | 內容 |
|---|---|---|
| `tests/test_taiwan_network.py` | 新增（Task 1），之後的任務往裡面加資料列與測試 | 全台商圈與路網檢查 |
| `data/fixture/高雄市.json` | 修改（Task 1） | 巨蛋 → 三多 |
| `tests/test_planner.py` | 修改（Task 1） | 兩處 `高雄市/巨蛋` → `高雄市/三多` |
| `data/fixture/交通-跨縣市.json` | 新增（Task 2），Task 3 補路段 | 跨縣市台鐵、客運 |
| `data/fixture/基隆市.json`、`新竹市.json`、`新竹縣.json`、`苗栗縣.json`、`宜蘭縣.json`、`花蓮縣.json`、`台東縣.json` | 新增（Task 2） | 北部與東部 |
| `data/fixture/彰化縣.json`、`雲林縣.json`、`南投縣.json`、`嘉義市.json`、`屏東縣.json` | 新增（Task 3） | 中南部 |
| `data/fixture/澎湖縣.json`、`金門縣.json` | 新增（Task 4） | 離島 |
| `frontend/app.js`、`README.md` | 修改（Task 5） | 等待說明、文件 |

---

### Task 1: 路網檢查測試與高雄「三多」

**Files:**
- Create: `tests/test_taiwan_network.py`
- Modify: `data/fixture/高雄市.json`
- Modify: `tests/test_planner.py:76`、`tests/test_planner.py:96`

**Interfaces:**
- Consumes（已存在）：`mem_session` fixture（`tests/conftest.py`）、`BaseCollector`（`app/collectors/fixture.py`）、`upsert_batch(session, source, batch)`（`app/ingest.py`）、`load_coverage(session) -> Coverage`、`Coverage.area_for(lat, lng) -> str | None`（`app/collectors/coverage.py`）、`services.load_graph(session) -> TransitGraph`、`TransitGraph.reachable(src, dst) -> bool`、`services.check_route(session, origin, area, depart_at) -> list[dict]`。
- Produces（之後的任務會用）：`tests/test_taiwan_network.py` 裡的
  - fixture `base`：匯入 base 資料後的 session
  - 模組常數 `CINEMAS: list[tuple[str, float, float, str | None]]`（影城名、緯度、經度、預期商圈）
  - 模組常數 `MAIN_ISLAND_AREAS: list[str]`（本島商圈）
  - helper `_area_stations(session, area_id) -> list[str]`、`_reachable_both_ways(graph, origin, stations) -> bool`
  - 參數化測試 `test_cinema_lands_in_expected_area`、`test_main_island_area_connects_to_taipei`

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_taiwan_network.py`：

```python
"""全台商圈與交通路網（data/fixture 的 base 資料）的檢查。

影城座標取自開眼電影網地圖頁（2026-10-12）；手寫的車站座標打錯時，影城會分不到預期的商圈。
"""
import datetime as dt

import pytest
from sqlalchemy import select

from app import services
from app.collectors.coverage import load_coverage
from app.collectors.fixture import BaseCollector
from app.ingest import upsert_batch
from app.models import Area, Station

DAY = dt.date(2026, 10, 12)

# （影城, 緯度, 經度, 預期商圈；None 表示這次不涵蓋）
CINEMAS = [
    ("高雄大遠百威秀影城", 22.613072, 120.30395, "高雄市/三多"),
    ("喜樂時代高雄總圖店", 22.6091507, 120.302324, "高雄市/三多"),
    ("大立in89豪華影城", 22.621851, 120.29827, "高雄市/三多"),
]

# 本島商圈：都要能從台北車站去、也回得來
MAIN_ISLAND_AREAS = [
    "高雄市/三多",
]


@pytest.fixture
def base(mem_session):
    upsert_batch(mem_session, "base", BaseCollector().collect(DAY, 1))
    mem_session.commit()
    return mem_session


def _area_stations(session, area_id):
    return [s.id for s in session.scalars(select(Station).where(Station.area_id == area_id))]


def _reachable_both_ways(graph, origin, stations):
    return any(graph.reachable(origin, s) for s in stations) and any(graph.reachable(s, origin) for s in stations)


@pytest.mark.parametrize("name, lat, lng, expected", CINEMAS)
def test_cinema_lands_in_expected_area(base, name, lat, lng, expected):
    assert load_coverage(base).area_for(lat, lng) == expected, name


@pytest.mark.parametrize("area_id", MAIN_ISLAND_AREAS)
def test_main_island_area_connects_to_taipei(base, area_id):
    stations = _area_stations(base, area_id)
    assert stations, f"{area_id} 沒有車站"
    assert _reachable_both_ways(services.load_graph(base), "tpe-main", stations), area_id


def test_dome_is_only_a_transfer_station_now(base):
    assert base.get(Area, "高雄市/巨蛋") is None
    dome = base.get(Station, "khh-dome")
    assert dome.area_id is None and dome.is_origin
```

- [ ] **Step 2: 執行測試，確認失敗**

Run: `.venv/Scripts/python.exe -m pytest tests/test_taiwan_network.py -v`
Expected: FAIL——三筆影城分不到 `高雄市/三多`（回傳 None）、`高雄市/三多` 沒有車站、`高雄市/巨蛋` 還存在。

- [ ] **Step 3: 改寫 `data/fixture/高雄市.json`**

整個檔案換成下面內容。虛構影城與餐廳（給測試用）的座標往南移到三多商圈站附近，`area_id` 改成 `高雄市/三多`；名稱裡的「巨蛋」改成「三多」，其他欄位不變：

```json
{
  "_note": "示意資料：影城、餐廳、場次皆為虛構；捷運、台鐵車程與班次為估計值。三多商圈、中央公園座標取自 OpenStreetMap（2026-10-12）。",
  "areas": [
    {"id": "高雄市/三多", "city": "高雄市", "name": "三多"}
  ],
  "stations": [
    {"id": "khh-zuoying", "name": "左營", "city": "高雄市", "lat": 22.6870, "lng": 120.3076, "transfer_min": 8, "is_origin": true},
    {"id": "khh-dome", "name": "巨蛋", "city": "高雄市", "lat": 22.6672, "lng": 120.3027, "is_origin": true},
    {"id": "khh-main", "name": "高雄車站", "city": "高雄市", "lat": 22.6394, "lng": 120.3025, "transfer_min": 5, "is_origin": true},
    {"id": "khh-formosa", "name": "美麗島", "city": "高雄市", "lat": 22.6313, "lng": 120.3019, "transfer_min": 4, "is_origin": true},
    {"id": "khh-central", "name": "中央公園", "city": "高雄市", "lat": 22.62487, "lng": 120.30112, "transfer_min": 3, "is_origin": true, "area_id": "高雄市/三多"},
    {"id": "khh-sanduo", "name": "三多商圈", "city": "高雄市", "lat": 22.61415, "lng": 120.30442, "transfer_min": 3, "is_origin": true, "area_id": "高雄市/三多"}
  ],
  "transit_links": [
    {"from": "khh-zuoying", "to": "khh-dome", "mode": "捷運", "line": "紅線", "ride": 4, "headway": 8, "first": "06:00", "last": "24:00", "both_ways": true},
    {"from": "khh-dome", "to": "khh-main", "mode": "捷運", "line": "紅線", "ride": 7, "headway": 8, "first": "06:00", "last": "24:00", "both_ways": true},
    {"from": "khh-main", "to": "khh-formosa", "mode": "捷運", "line": "紅線", "ride": 2, "headway": 8, "first": "06:00", "last": "24:00", "both_ways": true},
    {"from": "khh-formosa", "to": "khh-central", "mode": "捷運", "line": "紅線", "ride": 2, "headway": 8, "first": "06:00", "last": "24:00", "both_ways": true},
    {"from": "khh-central", "to": "khh-sanduo", "mode": "捷運", "line": "紅線", "ride": 2, "headway": 8, "first": "06:00", "last": "24:00", "both_ways": true},
    {"from": "khh-main", "to": "khh-zuoying", "mode": "台鐵", "line": "台鐵縱貫線", "ride": 8, "headway": 15, "first": "05:30", "last": "24:00", "both_ways": true}
  ],
  "cinemas": [
    {"external_id": "KHH-JD-C1", "name": "三多影城", "area_id": "高雄市/三多", "lat": 22.6145, "lng": 120.3020},
    {"external_id": "KHH-JD-C2", "name": "博愛影城", "area_id": "高雄市/三多", "lat": 22.6110, "lng": 120.3045}
  ],
  "showtime_templates": [
    {"cinema": "KHH-JD-C1", "movie": "M1", "label": "IMAX 2D 英語", "price": 380, "times": ["15:30", "18:10", "20:50"]},
    {"cinema": "KHH-JD-C1", "movie": "M2", "label": "數位 英語", "price": 270, "times": ["17:00", "19:40"]},
    {"cinema": "KHH-JD-C1", "movie": "M3", "label": "4DX 3D 英語", "price": 440, "times": ["19:20"]},
    {"cinema": "KHH-JD-C1", "movie": "M7", "label": "數位 英語", "price": 270, "times": ["21:00"]},
    {"cinema": "KHH-JD-C2", "movie": "M1", "label": "數位 國語配音", "price": 260, "times": ["16:40", "19:30"]},
    {"cinema": "KHH-JD-C2", "movie": "M4", "label": "數位 英語", "price": 250, "times": ["18:00"]},
    {"cinema": "KHH-JD-C2", "movie": "M2", "label": "數位", "price": 250, "times": ["20:30"]},
    {"cinema": "KHH-JD-C2", "movie": "M5", "label": "數位 國語配音", "price": 250, "times": ["16:30"]}
  ],
  "restaurants": [
    {
      "external_id": "KHH-JD-R01", "name": "港景高空餐廳", "area_id": "高雄市/三多", "lat": 22.6135, "lng": 120.3035,
      "description": "高樓西式餐廳，看得到城市夜景，套餐精緻，適合紀念日。",
      "reviews": ["夜景很美", "求婚成功", "價格偏高"],
      "price_per_person": 1600, "hours": {"default": ["17:30", "22:30"]}, "last_order_min": 90, "typical_wait_min": 0, "reservable": true
    },
    {
      "external_id": "KHH-JD-R02", "name": "巷仔海產粥", "area_id": "高雄市/三多", "lat": 22.6120, "lng": 120.3010,
      "description": "台式海產粥與小吃，平價，宵夜人潮多。",
      "reviews": ["料很多", "很快就上菜", "很吵"],
      "price_per_person": 150, "hours": {"default": ["17:00", "27:00"]}, "last_order_min": 30, "typical_wait_min": 10, "reservable": false
    },
    {
      "external_id": "KHH-JD-R03", "name": "二樓咖啡", "area_id": "高雄市/三多", "lat": 22.6150, "lng": 120.3040,
      "description": "咖啡與甜點，安靜的二樓座位，適合聊天。",
      "reviews": ["拿鐵好喝", "很適合約會", "九點半打烊"],
      "price_per_person": 300, "hours": {"default": ["10:00", "21:30"]}, "last_order_min": 30, "typical_wait_min": 10, "reservable": false
    },
    {
      "external_id": "KHH-JD-R04", "name": "南風燒肉", "area_id": "高雄市/三多", "lat": 22.6105, "lng": 120.3020,
      "description": "燒肉專門店，包廂座位，適合兩人慢慢烤。",
      "reviews": ["肉質好", "包廂很有隱私", "建議訂位"],
      "price_per_person": 950, "hours": {"default": ["17:00", "24:00"]}, "last_order_min": 60, "typical_wait_min": 10, "reservable": true
    },
    {
      "external_id": "KHH-JD-R05", "name": "三多漢堡", "area_id": "高雄市/三多", "lat": 22.6125, "lng": 120.3050,
      "description": "美式漢堡快餐，可以外帶，出餐快。",
      "reviews": ["漢堡大份", "座位少", "很快"],
      "price_per_person": 260, "hours": {"default": ["10:30", "22:00"]}, "last_order_min": 30, "typical_wait_min": 5, "reservable": false
    },
    {
      "external_id": "KHH-JD-R06", "name": "南洋小廚", "area_id": "高雄市/三多", "lat": 22.6140, "lng": 120.3005,
      "description": "越南與泰式料理，可以做成素食，空間明亮，價格平實。",
      "reviews": ["河粉好吃", "份量剛好", "評價不錯"],
      "price_per_person": 350, "hours": {"default": ["11:00", "21:30"]}, "last_order_min": 45, "typical_wait_min": 10, "reservable": true
    }
  ]
}
```

- [ ] **Step 4: `tests/test_planner.py` 換商圈代碼**

第 76 行 `"高雄市/巨蛋": "左營",` 改成 `"高雄市/三多": "左營",`；第 96 行 `{"origin": "左營", "area": "高雄市/巨蛋"},` 改成 `{"origin": "左營", "area": "高雄市/三多"},`。

- [ ] **Step 5: 執行測試，確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_taiwan_network.py tests/test_planner.py -v`
Expected: 全部通過（包括 `test_every_area_can_be_planned_from_its_own_city` 與 `test_labels_tell_the_truth` 的高雄案例）。

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全部通過。

- [ ] **Step 6: Commit**

```bash
git add tests/test_taiwan_network.py data/fixture/高雄市.json tests/test_planner.py
git commit -m "高雄商圈從巨蛋改為三多，新增全台路網檢查測試" -m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"
```

---

### Task 2: 北部與東部（基隆、新竹市、新竹縣、苗栗、宜蘭、花蓮、台東）

**Files:**
- Create: `data/fixture/交通-跨縣市.json`
- Create: `data/fixture/基隆市.json`、`新竹市.json`、`新竹縣.json`、`苗栗縣.json`、`宜蘭縣.json`、`花蓮縣.json`、`台東縣.json`
- Modify: `tests/test_taiwan_network.py`（`CINEMAS`、`MAIN_ISLAND_AREAS` 加資料列）

**Interfaces:**
- Consumes: Task 1 的 `CINEMAS`、`MAIN_ISLAND_AREAS` 與兩個參數化測試；既有車站 `tpe-main`、`tyn-zhongli`、`txg-main`、`hsz-hsr`（`data/fixture/台北市.json`、`桃園市.json`、`台中市.json`、`交通-高鐵.json`）。
- Produces: 車站 `kel-main`、`hsc-main`、`hsc-bigcity`、`hsq-ziqiang`、`mia-zhunan`、`mia-toufen`、`ila-main`、`hua-main`、`ttt-main`、`ttt-terminal`；檔案 `data/fixture/交通-跨縣市.json`（Task 3 會加路段）。

- [ ] **Step 1: 測試加資料列（先讓它失敗）**

`tests/test_taiwan_network.py` 的 `CINEMAS` 清單最後加上：

```python
    ("基隆秀泰影城", 25.130757, 121.74469, "基隆市/基隆車站"),
    ("新竹大遠百威秀影城", 24.801696, 120.965874, "新竹市/東區"),
    ("新竹巨城威秀影城", 24.809639, 120.973868, "新竹市/東區"),
    ("百老匯影城竹北店", 24.8188713, 121.027148, "新竹縣/竹北"),
    ("頭份尚順威秀影城", 24.693709, 120.896761, "苗栗縣/頭份"),
    ("新月豪華影城", 24.754852, 121.750711, "宜蘭縣/宜蘭市"),
    ("花蓮秀泰影城", 23.990824, 121.605383, "花蓮縣/花蓮車站"),
    ("台東秀泰影城", 22.752125, 121.148693, "台東縣/台東市"),
```

`MAIN_ISLAND_AREAS` 清單最後加上：

```python
    "基隆市/基隆車站",
    "新竹市/東區",
    "新竹縣/竹北",
    "苗栗縣/頭份",
    "宜蘭縣/宜蘭市",
    "花蓮縣/花蓮車站",
    "台東縣/台東市",
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_taiwan_network.py -v`
Expected: 新加的 15 個案例 FAIL（影城分不到商圈、商圈沒有車站）。

- [ ] **Step 2: 建立 `data/fixture/交通-跨縣市.json`**

```json
{
  "_note": "跨縣市交通（手寫估計值，2026-10-12）：車程、班距、首末班依公開資訊估計，不是真正的時刻表。",
  "transit_links": [
    {"from": "tpe-main", "to": "kel-main", "mode": "台鐵", "line": "台鐵縱貫線", "ride": 45, "headway": 15, "first": "05:40", "last": "24:00", "both_ways": true},
    {"from": "tyn-zhongli", "to": "hsc-main", "mode": "台鐵", "line": "台鐵縱貫線", "ride": 40, "headway": 20, "first": "05:30", "last": "23:30", "both_ways": true},
    {"from": "hsc-main", "to": "mia-zhunan", "mode": "台鐵", "line": "台鐵縱貫線", "ride": 20, "headway": 20, "first": "05:30", "last": "23:30", "both_ways": true},
    {"from": "mia-zhunan", "to": "txg-main", "mode": "台鐵", "line": "台鐵山線", "ride": 75, "headway": 30, "first": "05:40", "last": "22:40", "both_ways": true},
    {"from": "tpe-main", "to": "ila-main", "mode": "台鐵", "line": "台鐵宜蘭線", "ride": 80, "headway": 30, "first": "06:00", "last": "22:30", "both_ways": true},
    {"from": "tpe-main", "to": "ila-main", "mode": "公車", "line": "國道客運（台北—宜蘭）", "ride": 60, "headway": 15, "first": "05:30", "last": "24:00", "both_ways": true},
    {"from": "ila-main", "to": "hua-main", "mode": "台鐵", "line": "台鐵北迴線", "ride": 75, "headway": 60, "first": "06:00", "last": "22:00", "both_ways": true},
    {"from": "hua-main", "to": "ttt-main", "mode": "台鐵", "line": "台鐵台東線", "ride": 120, "headway": 60, "first": "06:00", "last": "21:00", "both_ways": true},
    {"from": "hsz-hsr", "to": "hsc-main", "mode": "台鐵", "line": "台鐵六家線", "ride": 20, "headway": 30, "first": "06:00", "last": "23:30", "both_ways": true}
  ]
}
```

- [ ] **Step 3: 建立七個縣市檔**

`data/fixture/基隆市.json`：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。",
  "areas": [
    {"id": "基隆市/基隆車站", "city": "基隆市", "name": "基隆車站"}
  ],
  "stations": [
    {"id": "kel-main", "name": "基隆", "city": "基隆市", "lat": 25.13303, "lng": 121.73923, "transfer_min": 5, "is_origin": true, "area_id": "基隆市/基隆車站"}
  ],
  "transit_links": []
}
```

`data/fixture/新竹市.json`：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。",
  "areas": [
    {"id": "新竹市/東區", "city": "新竹市", "name": "東區"}
  ],
  "stations": [
    {"id": "hsc-main", "name": "新竹", "city": "新竹市", "lat": 24.80140, "lng": 120.97168, "transfer_min": 5, "is_origin": true, "area_id": "新竹市/東區"},
    {"id": "hsc-bigcity", "name": "巨城購物", "city": "新竹市", "lat": 24.80931, "lng": 120.97558, "transfer_min": 2, "is_origin": false, "area_id": "新竹市/東區"}
  ],
  "transit_links": [
    {"from": "hsc-main", "to": "hsc-bigcity", "mode": "公車", "line": "公車（新竹車站—巨城）", "ride": 10, "headway": 15, "first": "06:00", "last": "22:30", "both_ways": true}
  ]
}
```

`data/fixture/新竹縣.json`（高鐵新竹 `hsz-hsr` 維持在 `交通-高鐵.json`）：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。高鐵新竹站在 交通-高鐵.json。",
  "areas": [
    {"id": "新竹縣/竹北", "city": "新竹縣", "name": "竹北"}
  ],
  "stations": [
    {"id": "hsq-ziqiang", "name": "自強南路", "city": "新竹縣", "lat": 24.81819, "lng": 121.02704, "transfer_min": 2, "is_origin": false, "area_id": "新竹縣/竹北"}
  ],
  "transit_links": [
    {"from": "hsz-hsr", "to": "hsq-ziqiang", "mode": "公車", "line": "公車（高鐵新竹—竹北）", "ride": 10, "headway": 20, "first": "06:00", "last": "22:30", "both_ways": true}
  ]
}
```

`data/fixture/苗栗縣.json`：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。",
  "areas": [
    {"id": "苗栗縣/頭份", "city": "苗栗縣", "name": "頭份"}
  ],
  "stations": [
    {"id": "mia-zhunan", "name": "竹南", "city": "苗栗縣", "lat": 24.68656, "lng": 120.88075, "transfer_min": 5, "is_origin": true},
    {"id": "mia-toufen", "name": "德義路社區", "city": "苗栗縣", "lat": 24.69130, "lng": 120.89767, "transfer_min": 2, "is_origin": false, "area_id": "苗栗縣/頭份"}
  ],
  "transit_links": [
    {"from": "mia-zhunan", "to": "mia-toufen", "mode": "公車", "line": "公車（竹南—頭份）", "ride": 12, "headway": 20, "first": "06:00", "last": "22:00", "both_ways": true}
  ]
}
```

`data/fixture/宜蘭縣.json`：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。",
  "areas": [
    {"id": "宜蘭縣/宜蘭市", "city": "宜蘭縣", "name": "宜蘭市"}
  ],
  "stations": [
    {"id": "ila-main", "name": "宜蘭", "city": "宜蘭縣", "lat": 24.75456, "lng": 121.75831, "transfer_min": 5, "is_origin": true, "area_id": "宜蘭縣/宜蘭市"}
  ],
  "transit_links": []
}
```

`data/fixture/花蓮縣.json`：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。",
  "areas": [
    {"id": "花蓮縣/花蓮車站", "city": "花蓮縣", "name": "花蓮車站"}
  ],
  "stations": [
    {"id": "hua-main", "name": "花蓮", "city": "花蓮縣", "lat": 23.99264, "lng": 121.60095, "transfer_min": 5, "is_origin": true, "area_id": "花蓮縣/花蓮車站"}
  ],
  "transit_links": []
}
```

`data/fixture/台東縣.json`：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。台東秀泰在舊市區，離台東車站約 5 公里，要轉公車。",
  "areas": [
    {"id": "台東縣/台東市", "city": "台東縣", "name": "台東市"}
  ],
  "stations": [
    {"id": "ttt-main", "name": "台東", "city": "台東縣", "lat": 22.79346, "lng": 121.12243, "transfer_min": 5, "is_origin": true},
    {"id": "ttt-terminal", "name": "台東轉運站", "city": "台東縣", "lat": 22.75153, "lng": 121.14770, "transfer_min": 3, "is_origin": true, "area_id": "台東縣/台東市"}
  ],
  "transit_links": [
    {"from": "ttt-main", "to": "ttt-terminal", "mode": "公車", "line": "公車（台東車站—台東轉運站）", "ride": 15, "headway": 30, "first": "06:00", "last": "22:00", "both_ways": true}
  ]
}
```

- [ ] **Step 4: 執行測試，確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_taiwan_network.py -v`
Expected: 全部通過。

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全部通過（`test_meta_lists_all_22_cities` 不受影響：新縣市沒有虛構場次）。

- [ ] **Step 5: Commit**

```bash
git add data/fixture tests/test_taiwan_network.py
git commit -m "新增北部與東部商圈：基隆、新竹市、竹北、頭份、宜蘭、花蓮、台東" -m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"
```

---

### Task 3: 中南部（彰化員林、雲林斗六、南投、嘉義市、屏東）

**Files:**
- Modify: `data/fixture/交通-跨縣市.json`（加路段）
- Create: `data/fixture/彰化縣.json`、`雲林縣.json`、`南投縣.json`、`嘉義市.json`、`屏東縣.json`
- Modify: `tests/test_taiwan_network.py`（加資料列）

**Interfaces:**
- Consumes: Task 1 的測試結構；Task 2 的 `交通-跨縣市.json` 與 `ttt-main`；既有 `txg-main`、`txg-hsr`、`tnn-main`、`khh-main`、`cyi-hsr`。
- Produces: 車站 `chw-yuanlin`、`yun-douliu`、`yun-cinema`、`nan-nantou`、`cyq-main`、`pif-main`。

- [ ] **Step 1: 測試加資料列（先讓它失敗）**

`CINEMAS` 最後加上：

```python
    ("員林影城", 23.954479, 120.573115, "彰化縣/員林"),
    ("環球中華影城", 23.705753, 120.535669, "雲林縣/斗六"),
    ("南投戲院", 23.907963, 120.687916, "南投縣/南投市"),
    ("嘉年華戲院", 23.478163, 120.441261, "嘉義市/嘉義車站"),
    ("中影屏東影城", 22.670618, 120.491952, "屏東縣/屏東市"),
    ("嘉義秀泰影城", 23.494348, 120.444086, None),
    ("雲林北港秀泰", 23.569749, 120.299199, None),
```

`MAIN_ISLAND_AREAS` 最後加上：

```python
    "彰化縣/員林",
    "雲林縣/斗六",
    "南投縣/南投市",
    "嘉義市/嘉義車站",
    "屏東縣/屏東市",
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_taiwan_network.py -v`
Expected: 新加的 5 個影城與 5 個商圈案例 FAIL；兩個 `None` 的案例已經通過（本來就分不到商圈），之後也要維持通過。

- [ ] **Step 2: `data/fixture/交通-跨縣市.json` 加路段**

在 `transit_links` 清單最後（`hsz-hsr`→`hsc-main` 那筆之後）加上：

```json
    {"from": "txg-main", "to": "chw-yuanlin", "mode": "台鐵", "line": "台鐵縱貫線", "ride": 30, "headway": 20, "first": "05:30", "last": "23:30", "both_ways": true},
    {"from": "chw-yuanlin", "to": "yun-douliu", "mode": "台鐵", "line": "台鐵縱貫線", "ride": 30, "headway": 30, "first": "05:40", "last": "23:00", "both_ways": true},
    {"from": "yun-douliu", "to": "cyq-main", "mode": "台鐵", "line": "台鐵縱貫線", "ride": 25, "headway": 30, "first": "05:40", "last": "23:00", "both_ways": true},
    {"from": "cyq-main", "to": "tnn-main", "mode": "台鐵", "line": "台鐵縱貫線", "ride": 50, "headway": 30, "first": "05:30", "last": "23:00", "both_ways": true},
    {"from": "tnn-main", "to": "khh-main", "mode": "台鐵", "line": "台鐵縱貫線", "ride": 35, "headway": 20, "first": "05:30", "last": "23:30", "both_ways": true},
    {"from": "khh-main", "to": "pif-main", "mode": "台鐵", "line": "台鐵屏東線", "ride": 25, "headway": 20, "first": "05:30", "last": "23:30", "both_ways": true},
    {"from": "ttt-main", "to": "pif-main", "mode": "台鐵", "line": "台鐵南迴線", "ride": 130, "headway": 90, "first": "06:00", "last": "20:30", "both_ways": true},
    {"from": "txg-main", "to": "nan-nantou", "mode": "公車", "line": "公車（台中車站—南投）", "ride": 50, "headway": 20, "first": "06:00", "last": "22:30", "both_ways": true},
    {"from": "txg-hsr", "to": "nan-nantou", "mode": "公車", "line": "公車（高鐵台中—南投）", "ride": 40, "headway": 30, "first": "06:30", "last": "22:00", "both_ways": true},
    {"from": "cyi-hsr", "to": "cyq-main", "mode": "公車", "line": "BRT（高鐵嘉義—嘉義車站）", "ride": 30, "headway": 15, "first": "06:00", "last": "23:30", "both_ways": true}
```

（記得在原本最後一筆後面補逗號，JSON 不能有多餘的逗號。）

- [ ] **Step 3: 建立五個縣市檔**

`data/fixture/彰化縣.json`：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。",
  "areas": [
    {"id": "彰化縣/員林", "city": "彰化縣", "name": "員林"}
  ],
  "stations": [
    {"id": "chw-yuanlin", "name": "員林", "city": "彰化縣", "lat": 23.95935, "lng": 120.56963, "transfer_min": 5, "is_origin": true, "area_id": "彰化縣/員林"}
  ],
  "transit_links": []
}
```

`data/fixture/雲林縣.json`：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。環球中華影城離斗六車站步行約 16 分鐘，商圈車站用影城旁的公車站。",
  "areas": [
    {"id": "雲林縣/斗六", "city": "雲林縣", "name": "斗六"}
  ],
  "stations": [
    {"id": "yun-douliu", "name": "斗六", "city": "雲林縣", "lat": 23.71181, "lng": 120.54116, "transfer_min": 5, "is_origin": true},
    {"id": "yun-cinema", "name": "雲林溪（環球中華影城）", "city": "雲林縣", "lat": 23.70582, "lng": 120.53547, "transfer_min": 2, "is_origin": false, "area_id": "雲林縣/斗六"}
  ],
  "transit_links": [
    {"from": "yun-douliu", "to": "yun-cinema", "mode": "公車", "line": "公車（斗六車站—雲林溪）", "ride": 6, "headway": 20, "first": "06:00", "last": "22:00", "both_ways": true}
  ]
}
```

`data/fixture/南投縣.json`：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。南投市沒有火車站，從台中車站或高鐵台中搭客運。",
  "areas": [
    {"id": "南投縣/南投市", "city": "南投縣", "name": "南投市"}
  ],
  "stations": [
    {"id": "nan-nantou", "name": "南投站（統聯）", "city": "南投縣", "lat": 23.90572, "lng": 120.68840, "transfer_min": 3, "is_origin": true, "area_id": "南投縣/南投市"}
  ],
  "transit_links": []
}
```

`data/fixture/嘉義市.json`：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。",
  "areas": [
    {"id": "嘉義市/嘉義車站", "city": "嘉義市", "name": "嘉義車站"}
  ],
  "stations": [
    {"id": "cyq-main", "name": "嘉義", "city": "嘉義市", "lat": 23.48043, "lng": 120.44213, "transfer_min": 5, "is_origin": true, "area_id": "嘉義市/嘉義車站"}
  ],
  "transit_links": []
}
```

`data/fixture/屏東縣.json`：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。",
  "areas": [
    {"id": "屏東縣/屏東市", "city": "屏東縣", "name": "屏東市"}
  ],
  "stations": [
    {"id": "pif-main", "name": "屏東", "city": "屏東縣", "lat": 22.66885, "lng": 120.48613, "transfer_min": 5, "is_origin": true, "area_id": "屏東縣/屏東市"}
  ],
  "transit_links": []
}
```

- [ ] **Step 4: 執行測試，確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_taiwan_network.py -v`
Expected: 全部通過。

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全部通過。

- [ ] **Step 5: Commit**

```bash
git add data/fixture tests/test_taiwan_network.py
git commit -m "新增中南部商圈：員林、斗六、南投、嘉義市、屏東，補上西部幹線與南迴線" -m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"
```

---

### Task 4: 離島（澎湖、金門）與全台總檢查

**Files:**
- Create: `data/fixture/澎湖縣.json`、`data/fixture/金門縣.json`
- Modify: `tests/test_taiwan_network.py`（加資料列與四個測試）

**Interfaces:**
- Consumes: Task 1 的 fixture `base`、`_area_stations`、`_reachable_both_ways`、`CINEMAS`；Task 2、3 的全部商圈。
- Produces: 車站 `pen-magong`、`pen-nanhai`、`kin-jincheng`、`kin-shanwai`、`kin-boyu`。

- [ ] **Step 1: 測試加資料列與新測試（先讓它失敗）**

`CINEMAS` 最後加上：

```python
    ("in89澎湖影城", 23.568284, 119.573911, "澎湖縣/馬公"),
    ("金獅影城", 24.446036, 118.369248, "金門縣/金湖"),
```

檔案最後加上：

```python
# 離島商圈：只能從島上的出發站去
ISLAND_AREAS = {
    "澎湖縣/馬公": ["pen-magong"],
    "金門縣/金湖": ["kin-jincheng", "kin-shanwai"],
}


@pytest.mark.parametrize("area_id, island_origins", ISLAND_AREAS.items())
def test_island_area_only_reachable_from_the_island(base, area_id, island_origins):
    stations = _area_stations(base, area_id)
    assert stations, f"{area_id} 沒有車站"
    graph = services.load_graph(base)
    assert not any(graph.reachable("tpe-main", s) for s in stations)
    for origin in island_origins:
        assert _reachable_both_ways(graph, origin, stations), (area_id, origin)


def test_twenty_cities_have_areas(base):
    areas = base.scalars(select(Area)).all()
    assert len(areas) == 21
    cities = {a.city for a in areas}
    assert len(cities) == 20
    assert "嘉義縣" not in cities and "連江縣" not in cities


def test_every_area_has_a_station(base):
    for area in base.scalars(select(Area)):
        assert _area_stations(base, area.id), area.id


def test_check_route_reaches_new_areas(base):
    assert services.check_route(base, "基隆", "基隆市/基隆車站", "18:00")
    assert services.check_route(base, "馬公總站", "澎湖縣/馬公", "18:00")
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_taiwan_network.py -v`
Expected: 兩個離島影城案例、兩個離島測試、`test_twenty_cities_have_areas`（目前 19 個商圈、18 個縣市）、`test_check_route_reaches_new_areas`（澎湖還沒有）FAIL；`test_every_area_has_a_station` 已經通過。

- [ ] **Step 2: 建立兩個離島檔**

`data/fixture/澎湖縣.json`：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。和本島之間沒有當天往返的大眾運輸，只能從島上出發。in89 澎湖影城離馬公總站步行約 17 分鐘，商圈車站用影城旁的公車站。",
  "areas": [
    {"id": "澎湖縣/馬公", "city": "澎湖縣", "name": "馬公"}
  ],
  "stations": [
    {"id": "pen-magong", "name": "馬公總站", "city": "澎湖縣", "lat": 23.56743, "lng": 119.56453, "transfer_min": 3, "is_origin": true},
    {"id": "pen-nanhai", "name": "南海遊客中心", "city": "澎湖縣", "lat": 23.56777, "lng": 119.57299, "transfer_min": 2, "is_origin": false, "area_id": "澎湖縣/馬公"}
  ],
  "transit_links": [
    {"from": "pen-magong", "to": "pen-nanhai", "mode": "公車", "line": "公車（馬公總站—南海遊客中心）", "ride": 5, "headway": 30, "first": "06:30", "last": "21:30", "both_ways": true}
  ]
}
```

`data/fixture/金門縣.json`：

```json
{
  "_note": "商圈與交通為手寫估計值；座標取自 OpenStreetMap（2026-10-12）。和本島之間沒有當天往返的大眾運輸，只能從島上出發。",
  "areas": [
    {"id": "金門縣/金湖", "city": "金門縣", "name": "金湖"}
  ],
  "stations": [
    {"id": "kin-jincheng", "name": "金城車站", "city": "金門縣", "lat": 24.43311, "lng": 118.32008, "transfer_min": 3, "is_origin": true},
    {"id": "kin-shanwai", "name": "山外車站", "city": "金門縣", "lat": 24.44229, "lng": 118.41407, "transfer_min": 3, "is_origin": true},
    {"id": "kin-boyu", "name": "伯玉路三段", "city": "金門縣", "lat": 24.44637, "lng": 118.37173, "transfer_min": 2, "is_origin": false, "area_id": "金門縣/金湖"}
  ],
  "transit_links": [
    {"from": "kin-jincheng", "to": "kin-boyu", "mode": "公車", "line": "公車（金城—伯玉路）", "ride": 15, "headway": 30, "first": "06:00", "last": "21:30", "both_ways": true},
    {"from": "kin-shanwai", "to": "kin-boyu", "mode": "公車", "line": "公車（山外—伯玉路）", "ride": 15, "headway": 30, "first": "06:00", "last": "21:30", "both_ways": true}
  ]
}
```

- [ ] **Step 3: 執行測試，確認通過**

Run: `.venv/Scripts/python.exe -m pytest tests/test_taiwan_network.py -v`
Expected: 全部通過。

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全部通過。

- [ ] **Step 4: Commit**

```bash
git add data/fixture tests/test_taiwan_network.py
git commit -m "新增離島商圈：澎湖馬公、金門金湖（只能從島上出發）" -m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"
```

---

### Task 5: 網頁等待說明與 README

**Files:**
- Modify: `frontend/app.js:216`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1～4 的商圈清單（文件內容以規格為準）。

- [ ] **Step 1: 網頁等待說明**

`frontend/app.js` 的 `renderWaiting` 第一行：

```js
  const what = refresh.running === "atmovies" ? "正在抓取影城場次（第一次約 10 分鐘）" : "正在抓取餐廳資料（約 3～6 分鐘）";
```

改成：

```js
  const what = refresh.running === "atmovies" ? "正在抓取影城場次（第一次約 20 分鐘）" : "正在抓取餐廳資料（約 10～20 分鐘）";
```

Run: `node --check frontend/app.js`
Expected: 沒有輸出。

- [ ] **Step 2: README**

逐處修改：

1. 開頭條列第一點：

`- 地區涵蓋全台 22 縣市；目前六都各有 1～2 個商圈（商圈與交通是手寫的示意資料），其他縣市顯示「尚無資料」。`

改成：

`- 地區涵蓋全台 22 縣市：20 個縣市各有 1 個商圈（台北市 2 個），繞著該縣市的主要影城；嘉義縣（沒有影城）與連江縣（沒有場次資料）顯示「尚無資料」，但嘉義縣的高鐵嘉義仍可當出發站。澎湖、金門只能從島上出發。商圈與交通是手寫的估計值。`

2. 「快速開始」裡 `接著在背景抓餐廳（約 3～6 分鐘，視 Overpass 忙碌程度）和影城場次（第一次約 10 分鐘，之後每天約 2 分鐘）` 改成 `接著在背景抓餐廳（約 10～20 分鐘，視 Overpass 忙碌程度）和影城場次（第一次約 20 分鐘，之後每天約 5 分鐘）`。

3. 在「改用 PostgreSQL」一節之前加一節：

```markdown
## 升級後補抓資料

新增商圈（例如更新 `data/fixture/`）後，伺服器重新啟動就會載入新的商圈與路網，但自動更新會以為今天已經抓過，新商圈的影城要等隔天、餐廳要等一週才會補上。升級後手動執行一次：

    python -m app.ingest --source atmovies
    python -m app.ingest --source osm
```

4. 「目前的限制」第二點：

`- 商圈與交通是手寫的示意資料，只有六都 7 個商圈；交通以「車程＋最長班距」估算，不是真正的時刻表。`

改成：

`- 商圈與交通是手寫的估計值，每個縣市 1 個商圈；離商圈車站步行超過 15 分鐘的影城排不到（例如嘉義秀泰、花蓮新天堂樂園、羅東日新、高雄夢時代）。交通以「車程＋最長班距」估算，不是真正的時刻表；東部、南迴線班距大，跨縣市行程會偏保守。`

- [ ] **Step 3: 跑全部測試**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全部通過。

- [ ] **Step 4: Commit**

```bash
git add frontend/app.js README.md
git commit -m "README 與網頁說明：全台商圈、升級後補抓、抓取時間" -m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"
```

---

### Task 6: 實際補抓與驗收（由主控執行，不派子代理）

需要瀏覽器窗格與連網，由主控（controller）自己做：

- [ ] **Step 1:** 重新啟動伺服器（preview 工具），確認啟動時 `base` 匯入成功（21 個商圈）。
- [ ] **Step 2:** `.venv/Scripts/python.exe -m app.ingest --source atmovies`：新縣市影城有抓到（預期約 40 家影城），`failed_pages` 少量。
- [ ] **Step 3:** `.venv/Scripts/python.exe -m app.ingest --source osm`：21 個商圈，`failed_areas` 為 0；有失敗就再跑一次。
- [ ] **Step 4:** 瀏覽器：約會縣市選單有 20 個可選縣市；排一次「台北車站 → 基隆市/基隆車站」、一次「花蓮車站出發 → 花蓮縣/花蓮車站」、一次「馬公總站 → 澎湖縣/馬公」，都排得出方案；主控台沒有錯誤。
- [ ] **Step 5:** 量一次排程時間（任選新商圈，3 秒內）。
