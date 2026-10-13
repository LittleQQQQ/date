# Jev 選擇去程、回程走法 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development (recommended) or executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 程式列出時間接得上、預算付得起的幾種走法，由 Jev 選每個方案的去程、回程各搭哪一種；加上估計車資，並把來回車資算進每人預算。

**Architecture:**
- 新增 `app/fares.py` 估車資。
- `TransitGraph` 多一個 `excluding()`，回傳拿掉某些交通方式的路網。路線算法不改，多算幾次就能找到替代走法。
- `app/planner.py` 的兩種排法（先看電影、先吃飯）改成「先找走法再挑」：
  - 建立候選組合時，只用預算內最快的走法。
  - 前三名決定後，`_choose_trips` 列出所有走法問 Jev，再用選定的走法重排該方案。
- 畫面、範本摘要、說明文件跟著更新。

**Tech Stack:** Python 3.10+、FastAPI、pytest（離線）、原生 JavaScript 前端。

**Spec:** `docs/superpowers/specs/2026-10-13-jev-transit-choice-design.md`（實作前先讀完）

## Global Constraints

**環境與流程**
- 專案根目錄 `D:\final\date`，分支 `feat/jev-transit-choice`（從還沒合併的 `feat/jev-options-cleanup` 開出）。所有指令在專案根目錄執行。
- Python 一律用 `.venv/Scripts/python.exe`。跑全部測試：`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q`。開始前是 251 個測試、全部通過；每個任務結束時全部測試都要通過。
- 測試完全離線：不連網、不啟動伺服器、不執行 `app.ingest`、不碰 `dev.db`。
- 檔案都是 UTF-8、CRLF 換行，修改時保持 CRLF。
- 每次 commit 訊息用中文，最後加 `-m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"`。

**車資與找走法**
- **車資估法：** 每人每段車（連續搭同一條路線）估一次，每段四捨五入到 5 元，一趟是各段相加。
  - 捷運、輕軌：20 元起，每分鐘加 1 元，最多 65 元。
  - 公車：車程 30 分鐘以內 15 元；超過時每分鐘 2.3 元。
  - 台鐵：每分鐘 2.5 元，最少 15 元。
  - 高鐵：每分鐘 13 元，最少 40 元。
- **找替代走法：** 拿掉的交通方式是 {高鐵, 台鐵, 公車} 的所有子集，共 8 種，第一個是空集合。捷運、輕軌不拿掉。每個方向最多 4 種走法。

**Jev 選擇題**
- key 是 `transit_go`、`transit_back`。
- 題目是「這次約會的去程，最適合哪一種走法？」、「這次約會的回程，最適合哪一種走法？」。
- 對象代碼是 `_subject(req, 片名, 餐廳名)`。

**固定用語**
- 排除原因：「超過每人預算」。
- 估計提醒：「車資為估計」。
- 只有一種走法時：「只有這種走法接得上」。
- 改選題目：「Jev 不太確定去程要搭哪一種，你比較想怎麼去？」、「Jev 不太確定回程要搭哪一種，你比較想怎麼回？」。

**不能改的**
- 方案排名不變（選片機率 × 選餐廳機率），選車的機率不乘進去。
- 換走法只改出門、到家時間，電影和用餐時間不變。
- MCP 的 `check_route`（`services.check_route`）不改。

## 檔案結構

| 檔案 | 狀態 | 內容 |
|---|---|---|
| `app/fares.py` | 新增（Task 1） | 估車資 |
| `app/transit.py` | 修改（Task 1） | `Route.label`、`TransitGraph.excluding()` |
| `tests/test_fares.py` | 新增（Task 1） | 車資 |
| `tests/test_transit.py` | 修改（Task 1） | 走法名稱、拿掉交通方式 |
| `app/planner.py` | 修改（Task 2、3） | Task 2：找走法、預算內最快、時間軸顯示車資、花費明細。Task 3：問 Jev、重排、輸出 `transit` |
| `tests/test_planner.py` | 修改（Task 2、3） | |
| `tests/test_taiwan_network.py` | 修改（Task 2） | 真實路網上的走法 |
| `app/jev/questions.py`、`app/jev/__init__.py`、`app/jev/mock.py`、`app/jev/service.py` | 修改（Task 3） | 選車題目、模擬版規則、context 標籤 |
| `tests/test_jev_mock.py` | 修改（Task 3） | |
| `app/llm.py`、`tests/test_llm.py` | 修改（Task 4） | 範本摘要 |
| `frontend/app.js`、`frontend/style.css` | 修改（Task 4） | 畫面 |
| `app/schemas.py`、`app/mcp_server.py`、`README.md` | 修改（Task 4） | 說明文字 |

---

### Task 1: 估車資、走法名稱、拿掉某些交通方式的路網

**Files:**
- Create: `app/fares.py`、`tests/test_fares.py`
- Modify: `app/transit.py`（`Route` 類別、`TransitGraph.__init__`、新增 `excluding`）、`tests/test_transit.py`

**Interfaces:**
- Consumes（已存在）：`app/transit.py` 的 `Leg`（`mode`、`line`、`ride_min`、`label`…）、`Route(depart, arrive, legs)`、`TransitGraph(nodes, edges)`、`Node(id, name, transfer_min=3)`、`Edge(from_id, to_id, mode, line, ride_min, headway_min, first, last)`。
- Produces（之後的任務會用）：
  - `app.fares.leg_fare(mode: str, ride_min: int) -> int`
  - `app.fares.route_fare(route: Route) -> int`（走路就到是 0）
  - `Route.label -> str`：例如「高鐵 → 捷運板南線」；沒有搭車時是「步行」。
  - `TransitGraph.excluding(modes: frozenset[str]) -> TransitGraph`：空集合時回傳自己；同樣的集合回傳同一個物件。

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_fares.py`：

```python
import pytest

from app.fares import leg_fare, route_fare
from app.timeutil import to_min
from app.transit import Edge, Node, Route, TransitGraph


@pytest.mark.parametrize(
    "mode, ride, fare",
    [
        ("捷運", 3, 25),  # 20 + 3 = 23 → 25
        ("捷運", 24, 45),  # 板橋到市政府
        ("捷運", 80, 65),  # 最多 65 元
        ("輕軌", 10, 30),
        ("公車", 25, 15),  # 市區公車一段票
        ("公車", 60, 140),  # 國道客運：60 × 2.3 = 138 → 140
        ("台鐵", 4, 15),  # 最少 15 元
        ("台鐵", 150, 375),
        ("高鐵", 3, 40),  # 最少 40 元
        ("高鐵", 54, 700),  # 54 × 13 = 702 → 700
    ],
)
def test_leg_fare(mode, ride, fare):
    assert leg_fare(mode, ride) == fare


def test_unknown_mode_is_an_error():
    with pytest.raises(ValueError, match="不認得的交通方式"):
        leg_fare("渡輪", 30)


def test_same_line_is_one_ticket():
    """A→B→C 是同一條捷運，車程合計 10 分鐘，算一段票 30 元，不是兩段 25 + 25。"""
    first, last = to_min("06:00"), to_min("23:00")
    graph = TransitGraph(
        [Node("A", "甲"), Node("B", "乙"), Node("C", "丙")],
        [Edge("A", "B", "捷運", "L1", 5, 10, first, last), Edge("B", "C", "捷運", "L1", 5, 10, first, last)],
    )
    route = graph.earliest_arrival("A", to_min("18:00"), "C")
    assert route_fare(route) == 30


def test_route_fare_adds_up_each_leg():
    first, last = to_min("06:00"), to_min("23:00")
    graph = TransitGraph(
        [Node("A", "台中"), Node("B", "台北"), Node("C", "市政府")],
        [Edge("A", "B", "高鐵", "高鐵", 58, 20, first, last), Edge("B", "C", "捷運", "板南線", 11, 6, first, last)],
    )
    route = graph.earliest_arrival("A", to_min("18:00"), "C")
    assert [leg_fare(leg.mode, leg.ride_min) for leg in route.legs] == [755, 30]
    assert route_fare(route) == 785


def test_walking_costs_nothing():
    assert route_fare(Route(to_min("18:00"), to_min("18:00"), [])) == 0
```

在 `tests/test_transit.py` 檔尾加上：

```python


def test_route_label_names_each_line():
    route = _graph().earliest_arrival("A", to_min("18:00"), "D")
    assert route.label == "捷運L1 → 公車L2"
    assert _graph().earliest_arrival("C", to_min("18:00"), "C").label == "步行"


def _two_ways():
    # A→D：公車 L9 直達 10 分鐘；或搭捷運 L1 到 B，再轉捷運 L3 到 D
    first, last = to_min("06:00"), to_min("23:00")
    nodes = [Node("A", "甲"), Node("B", "乙"), Node("D", "丁")]
    edges = [
        Edge("A", "D", "公車", "L9", 10, 5, first, last),
        Edge("A", "B", "捷運", "L1", 10, 5, first, last),
        Edge("B", "D", "捷運", "L3", 15, 5, first, last),
    ]
    return TransitGraph(nodes, edges)


def test_excluding_a_mode_finds_another_way():
    graph = _two_ways()
    assert graph.earliest_arrival("A", to_min("18:00"), "D").label == "公車L9"
    no_bus = graph.excluding(frozenset({"公車"}))
    assert no_bus.earliest_arrival("A", to_min("18:00"), "D").label == "捷運L1 → 捷運L3"
    assert graph.earliest_arrival("A", to_min("18:00"), "D").label == "公車L9"  # 原本的路網不受影響


def test_excluding_is_cached():
    graph = _two_ways()
    assert graph.excluding(frozenset()) is graph
    assert graph.excluding(frozenset({"公車"})) is graph.excluding(frozenset({"公車"}))
```

- [ ] **Step 2: 確認測試失敗**

Run: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_fares.py tests/test_transit.py -q`
Expected:
- `test_fares.py` 收集失敗：`ModuleNotFoundError: No module named 'app.fares'`。
- 新的 transit 測試失敗：`AttributeError`（`Route` 沒有 `label`、`TransitGraph` 沒有 `excluding`）。

- [ ] **Step 3: 實作**

建立 `app/fares.py`：

```python
"""車資估計（每人單程）。

資料來源沒有票價，依交通方式和車程分鐘數估，畫面標「車資為估計」。之後接 TDX 換成真實票價時，只要改這個檔案。
連續搭同一條路線算一段票（transit.Leg）；每段四捨五入到 5 元，一趟是各段相加。
"""
from __future__ import annotations

from .transit import Route

CITY_BUS_MAX_MIN = 30  # 公車車程在這以內算市區公車（一段票），更長算國道客運


def _round5(value: float) -> int:
    return int(value / 5 + 0.5) * 5


def leg_fare(mode: str, ride_min: int) -> int:
    """一段車（連續搭同一條路線）的每人車資。"""
    if mode in ("捷運", "輕軌"):
        fare = min(20 + ride_min, 65)
    elif mode == "公車":
        fare = 15 if ride_min <= CITY_BUS_MAX_MIN else 2.3 * ride_min
    elif mode == "台鐵":
        fare = max(2.5 * ride_min, 15)
    elif mode == "高鐵":
        fare = max(13 * ride_min, 40)
    else:
        raise ValueError(f"不認得的交通方式：{mode}")
    return _round5(fare)


def route_fare(route: Route) -> int:
    """一趟的每人車資；走路就到是 0。"""
    return sum(leg_fare(leg.mode, leg.ride_min) for leg in route.legs)
```

`app/transit.py`：

1. 模組說明的「兩種查詢」清單後面加一行：

```python
- excluding(modes)：拿掉某些交通方式的路網，用來找替代走法（例如不搭高鐵）
```

2. `Route` 加上 `label`：

```python
@dataclass
class Route:
    depart: int  # 從起點開始等車的時間
    arrive: int  # 抵達終點的時間
    legs: list[Leg] = field(default_factory=list)

    @property
    def label(self) -> str:
        """走法名稱：各段路線依序串起來，例如「高鐵 → 捷運板南線」；名稱一樣就算同一種走法。"""
        return " → ".join(leg.label for leg in self.legs) or "步行"
```

3. `TransitGraph.__init__` 最後（`self._backward = {}` 之後）加上：

```python
        # 拿掉某些交通方式的路網（找替代走法用），同一次排程反覆用，建一次就記起來
        self._variants: dict[frozenset[str], TransitGraph] = {}
```

4. 在 `reachable` 前面（`# ---------- 基本工具 ----------` 之後）加上：

```python
    def excluding(self, modes: frozenset[str]) -> TransitGraph:
        """拿掉某些交通方式的路網；沒有要拿掉的就是自己。"""
        if not modes:
            return self
        if modes not in self._variants:
            edges = [e for out in self.out_edges.values() for e in out if e.mode not in modes]
            self._variants[modes] = TransitGraph(list(self.nodes.values()), edges)
        return self._variants[modes]
```

- [ ] **Step 4: 確認測試通過**

Run: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_fares.py tests/test_transit.py -q`
Expected: 全部通過。
再跑全部測試：`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q`，全部通過。

- [ ] **Step 5: Commit**

```bash
git add app/fares.py app/transit.py tests/test_fares.py tests/test_transit.py
git commit -m "新增車資估計、走法名稱，以及拿掉某些交通方式的路網（找替代走法用）" -m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"
```

---

### Task 2: 排程找走法，預算含來回車資

**Files:**
- Modify: `app/planner.py`
  - import 區
  - `ASSUMPTIONS`
  - `class Candidate` 前面新增類別
  - `Candidate` 欄位
  - `# ---------- 交通 ----------` 一節（`_transit_steps`、`_go_out`、`_go_home`）
  - `_movie_first`、`_dinner_first`、`_finish`
  - `_user_text`、`_ask_restaurant` 的選項說明
  - `ESTIMATE_NOTES` 附近、`_plan_dict`
  - `plan()` 的預算檢查
- Modify: `tests/test_planner.py`、`tests/test_taiwan_network.py`

**Interfaces:**
- Consumes：Task 1 的 `leg_fare`、`route_fare`、`Route.label`、`TransitGraph.excluding`。已存在的 `_nearby_stations(ctx, place) -> list[tuple[Place, int]]`（太遠會 `raise Reject`）、`TransitGraph.latest_departure(src, dst, deadline)`、`earliest_arrival(src, t0, dst)`。
- Produces（Task 3 會用）：
  - `TripOption(route: Route, station: Place, walk: int, fare: int)`，`.label -> str`
  - `RouteChoice(every: bool = False, go: str | None = None, back: str | None = None)`（frozen dataclass）
  - `TripSearch(find: Callable[[bool], list[TripOption]], every: bool)`：屬性 `.options`、`.every`，方法 `.expand() -> None`
  - `EXCLUDED_MODES: list[frozenset[str]]`、`MAX_TRIP_OPTIONS = 4`
  - `_distinct(options: list[TripOption]) -> list[TripOption]`
  - `_out_options(ctx, req, place, arrive_by, every) -> list[TripOption]`：由快到慢排，也就是越晚出門越前面。
  - `_home_options(ctx, req, place, ready, every) -> list[TripOption]`：由快到慢排，也就是越早到家越前面。
  - `_affordable_go(req, base_cost, gos, backs) -> list[TripOption]`
  - `_affordable_back(req, base_cost, go, backs) -> list[TripOption]`
  - `_pick_trips(req, base_cost, go: TripSearch, back: TripSearch, choice: RouteChoice) -> tuple[TripOption, TripOption]`
  - `_movie_first(ctx, req, s, r, choice: RouteChoice = RouteChoice()) -> Candidate`，`_dinner_first` 一樣
  - `BUILDERS = {"movie_first": _movie_first, "dinner_first": _dinner_first}`
  - `Candidate` 新欄位 `go`、`back`、`go_options`、`back_options`。`Candidate.cost` 改成電影票＋餐費＋來回車資。
  - `FARE_NOTE = "車資為估計"`
  - 方案 dict 新欄位 `cost_breakdown = {"movie", "meal", "go", "back"}`

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_planner.py` 檔尾加上：

```python


# ---------- 走法與車資 ----------


def _trip(label, fare):
    from types import SimpleNamespace as NS

    return NS(label=label, fare=fare)


def _search(*options, every=False):
    """假的 TripSearch：every=False 時一開始只有最快的，expand() 之後才有其他走法。"""
    from app.planner import TripSearch

    return TripSearch(lambda e: list(options) if e else list(options[:1]), every)


def test_distinct_drops_slower_and_pricier_trips():
    from app.planner import _distinct

    # 由快到慢：高鐵 → 高鐵轉公車（比較慢、也比較貴）→ 台鐵（慢但便宜）→ 同名的台鐵（更慢）
    options = [_trip("高鐵", 1340), _trip("高鐵 → 公車", 1365), _trip("台鐵", 790), _trip("台鐵", 700)]
    assert [(t.label, t.fare) for t in _distinct(options)] == [("高鐵", 1340), ("台鐵", 790)]


def test_distinct_keeps_at_most_four():
    from app.planner import _distinct

    options = [_trip(f"走法{i}", 1000 - 100 * i) for i in range(6)]
    assert [t.label for t in _distinct(options)] == ["走法0", "走法1", "走法2", "走法3"]


def test_pick_trips_takes_the_fastest_when_affordable():
    from app.planner import RouteChoice, _pick_trips

    go, back = _search(_trip("高鐵", 700), _trip("台鐵", 375)), _search(_trip("高鐵", 700))
    req = PlanRequest(date="2026-10-17", budget_per_person=3000)
    assert [t.label for t in _pick_trips(req, 800, go, back, RouteChoice())] == ["高鐵", "高鐵"]
    assert not go.every  # 付得起就不必找其他走法


def test_budget_limits_the_return_trip():
    from app.planner import RouteChoice, _pick_trips

    go = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    back = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    # 電影加餐費 800：高鐵來回 2200 超出 2000；去程高鐵（800 + 700 + 最便宜回程 375 = 1875）付得起，
    # 回程只剩 500 元，只能搭台鐵
    req = PlanRequest(date="2026-10-17", budget_per_person=2000)
    assert [t.label for t in _pick_trips(req, 800, go, back, RouteChoice())] == ["高鐵", "台鐵"]


def test_budget_switches_both_ways_to_rail():
    from app.planner import RouteChoice, _pick_trips

    go = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    back = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    req = PlanRequest(date="2026-10-17", budget_per_person=1600)
    assert [t.label for t in _pick_trips(req, 800, go, back, RouteChoice())] == ["台鐵", "台鐵"]


def test_trip_over_budget_is_rejected():
    import pytest

    from app.planner import Reject, RouteChoice, _pick_trips

    go = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    back = _search(_trip("高鐵", 700), _trip("台鐵", 375))
    req = PlanRequest(date="2026-10-17", budget_per_person=1500)
    with pytest.raises(Reject, match="^超過每人預算$"):
        _pick_trips(req, 800, go, back, RouteChoice())


def test_pick_trips_follows_the_chosen_labels():
    from app.planner import RouteChoice, _pick_trips

    go = _search(_trip("高鐵", 700), _trip("台鐵", 375), every=True)
    back = _search(_trip("高鐵", 700), _trip("台鐵", 375), every=True)
    req = PlanRequest(date="2026-10-17")
    picked = _pick_trips(req, 800, go, back, RouteChoice(every=True, go="台鐵", back="台鐵"))
    assert [t.label for t in picked] == ["台鐵", "台鐵"]
    # 指定的走法付不起時，改用付得起的最快走法
    tight = PlanRequest(date="2026-10-17", budget_per_person=1600)
    picked = _pick_trips(tight, 800, go, back, RouteChoice(every=True, go="高鐵", back="高鐵"))
    assert [t.label for t in picked] == ["台鐵", "台鐵"]


def test_cost_includes_round_trip_fares(session, saturday):
    res = _plan(session, saturday)
    assert res["plans"]
    for p in res["plans"]:
        b = p["cost_breakdown"]
        assert b["movie"] == p["movie"]["price"] and b["meal"] == p["restaurant"]["price"]
        assert b["go"] > 0 and b["back"] > 0  # 板橋到信義要搭捷運
        assert sum(b.values()) == p["cost_per_person"]


def test_transit_steps_show_fares(session, saturday):
    plan = _plan(session, saturday)["plans"][0]
    rides = [s for s in plan["steps"] if s["kind"] == "transit"]
    assert rides and all("車資約" in s["detail"] for s in rides)
```

把 `tests/test_planner.py` 的 `test_plans_carry_estimates` 改成：

```python
def test_plans_carry_estimates(session, saturday):
    res = _plan(session, saturday)
    assert res["plans"]
    for p in res["plans"]:
        assert p["estimates"] == ["車資為估計"]  # 示意資料的票價、餐廳都不是估計，只有車資是
        assert p["movie"]["estimated"] == [] and p["restaurant"]["estimated"] == []
```

在 `tests/test_taiwan_network.py` 檔尾加上：

```python


def _plan_context(session, origin_id, area_id):
    """只有交通的排程情境（沒有場次和餐廳），用來直接測找走法。"""
    from app.planner import PlanContext, Place

    def place(station):
        return Place(station.id, station.name, station.lat, station.lng)

    stations = session.scalars(select(Station).where(Station.area_id == area_id)).all()
    return PlanContext(
        origin=place(session.get(Station, origin_id)), area_stations=[place(s) for s in stations],
        graph=services.load_graph(session), showtimes=[], restaurants=[], weekend=False, threshold=0.6, ask=None,
    )


def test_tainan_can_be_reached_without_high_speed_rail(base):
    from app.planner import _out_options

    ctx = _plan_context(base, "tpe-main", "台南市/中西區")
    tainan = next(p for p in ctx.area_stations if p.id == "tnn-main")
    early = PlanRequest(date="2026-10-13", depart_after="08:00")
    options = _out_options(ctx, early, tainan, to_min("18:00"), every=True)
    labels = [o.label for o in options]
    assert any("高鐵" in label for label in labels) and any("高鐵" not in label for label in labels), labels
    departs = [o.route.depart for o in options]
    assert departs == sorted(departs, reverse=True)  # 由快到慢：越晚出門越前面
    fares = [o.fare for o in options]
    assert fares == sorted(fares, reverse=True)  # 慢的一定比較便宜，不然會被刪掉
    # 下午兩點才能出門，只搭台鐵趕不上，剩下有搭高鐵的走法
    late = PlanRequest(date="2026-10-13", depart_after="14:00")
    assert all("高鐵" in o.label for o in _out_options(ctx, late, tainan, to_min("18:00"), every=True))


def test_metro_trip_has_a_single_option(base):
    from app.planner import _home_options, _out_options

    ctx = _plan_context(base, "ntp-banqiao", "台北市/信義")
    cityhall = next(p for p in ctx.area_stations if p.id == "tpe-cityhall")
    req = PlanRequest(date="2026-10-13")
    go = _out_options(ctx, req, cityhall, to_min("18:00"), every=True)
    back = _home_options(ctx, req, cityhall, to_min("21:00"), every=True)
    assert [(o.label, o.fare) for o in go] == [("捷運板南線", 45)]
    assert [o.label for o in back] == ["捷運板南線"]
```

- [ ] **Step 2: 確認測試失敗**

Run: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_planner.py tests/test_taiwan_network.py -q`
Expected: 新測試失敗。
- 單元測試：`ImportError: cannot import name 'TripSearch'`（或 `_distinct`、`_pick_trips`、`_out_options`）。
- 整合測試：`KeyError: 'cost_breakdown'`、`"車資約"` 不在說明裡、`estimates` 是 `[]`。

- [ ] **Step 3: import、常數與新類別**

`app/planner.py` import 區：

```python
import hashlib
import itertools
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from .fares import leg_fare, route_fare
from .geo import walk_minutes
```

（其他 import 不變。）

`ASSUMPTIONS` 加兩項，放在 `"候位時間"` 之後、`"怎麼選"` 之前：

```python
    "車資": "依交通方式和車程估計：捷運 20～65 元、市區公車 15 元、國道客運每分鐘約 2.3 元、台鐵每分鐘約 2.5 元、高鐵每分鐘約 13 元；不是實際票價",
    "預算": "每人預算包含電影票、餐費與來回車資",
```

在 `@dataclass class Candidate` 前面加上：

```python
@dataclass
class TripOption:
    """一個方向（去程或回程）的一種走法。"""

    route: Route
    station: Place  # 商圈這一頭的車站
    walk: int  # 這個車站和影城或餐廳之間走幾分鐘
    fare: int  # 每人車資（估計）

    @property
    def label(self) -> str:
        return self.route.label


@dataclass(frozen=True)
class RouteChoice:
    """排一個組合時怎麼挑走法。every：列出所有走法（不只最快的）；go、back：指定的走法名稱，None 是預算內最快的。"""

    every: bool = False
    go: str | None = None
    back: str | None = None


class TripSearch:
    """一個方向的走法，由快到慢。先只找最快的；超出預算、或要給 Jev 選時，才找其他走法（要多算好幾次路線）。"""

    def __init__(self, find: Callable[[bool], list[TripOption]], every: bool):
        self._find = find
        self.every = every
        self.options = find(every)

    def expand(self) -> None:
        if not self.every:
            self.options, self.every = self._find(True), True
```

`Candidate` 在 `wait: int` 之後、`flags` 之前加上：

```python
    go: TripOption  # 去程搭哪一種
    back: TripOption  # 回程搭哪一種
    go_options: list[TripOption]  # 找過的去程走法，由快到慢（只找最快的時只有一個）
    back_options: list[TripOption]
```

- [ ] **Step 4: 換掉交通一節**

`# ---------- 交通 ----------` 一節保留 `station_name`、`_nearby_stations`。把 `_transit_steps`、`_go_out`、`_go_home` 三個函式整段換成下面這些：

```python
# 找替代走法時要拿掉的交通方式：{高鐵, 台鐵, 公車} 的所有子集；第一個是空集合（完整路網，找最快的）
EXCLUDED_MODES = [frozenset(c) for n in range(4) for c in itertools.combinations(("高鐵", "台鐵", "公車"), n)]
MAX_TRIP_OPTIONS = 4  # 每個方向最多給 Jev 幾種走法


def _distinct(options: list[TripOption]) -> list[TripOption]:
    """options 已由快到慢排好。同名的走法只留較快的；比前面的慢、又沒有比較便宜的刪掉；最多 MAX_TRIP_OPTIONS 種。"""
    kept: list[TripOption] = []
    for o in options:
        if all(o.label != k.label and o.fare < k.fare for k in kept):
            kept.append(o)
    return kept[:MAX_TRIP_OPTIONS]


def _out_options(ctx: PlanContext, req: PlanRequest, place: Place, arrive_by: int, every: bool) -> list[TripOption]:
    """去程的走法：要在 arrive_by 走到 place，由快到慢（越晚出門越前面）。every=False 只找最快的。"""
    nearby = _nearby_stations(ctx, place)
    found: list[TripOption] = []
    for modes in EXCLUDED_MODES if every else EXCLUDED_MODES[:1]:
        graph = ctx.graph.excluding(modes)
        best: TripOption | None = None
        for station, walk in nearby:
            route = graph.latest_departure(ctx.origin.id, station.id, arrive_by - walk)
            if route is not None and (best is None or route.depart > best.route.depart):
                best = TripOption(route, station, walk, route_fare(route))
        if best is None and not modes:
            raise Reject("去程沒有可以搭的班次")
        if best is not None:
            found.append(best)
    earliest = to_min(req.depart_after)
    if found[0].route.depart < earliest:  # 最快的走法都要更早出門
        raise Reject("要比你能出門的時間更早出發")
    usable = [o for o in found if o.route.depart >= earliest]
    return _distinct(sorted(usable, key=lambda o: (-o.route.depart, o.fare)))


def _home_options(ctx: PlanContext, req: PlanRequest, place: Place, ready: int, every: bool) -> list[TripOption]:
    """回程的走法：ready 時從 place 出發回出發站，由快到慢（越早到家越前面）。every=False 只找最快的。"""
    nearby = _nearby_stations(ctx, place)
    found: list[TripOption] = []
    for modes in EXCLUDED_MODES if every else EXCLUDED_MODES[:1]:
        graph = ctx.graph.excluding(modes)
        best: TripOption | None = None
        for station, walk in nearby:
            route = graph.earliest_arrival(station.id, ready + walk, ctx.origin.id)
            if route is not None and (best is None or route.arrive < best.route.arrive):
                best = TripOption(route, station, walk, route_fare(route))
        if best is None and not modes:
            raise Reject("回程趕不上末班車")
        if best is not None:
            found.append(best)
    home_by = to_min(req.home_by)
    if found[0].route.arrive > home_by:  # 最快的走法都太晚到家
        raise Reject(f"沒辦法在 {fmt(home_by)} 前到家")
    usable = [o for o in found if o.route.arrive <= home_by]
    return _distinct(sorted(usable, key=lambda o: (o.route.arrive, o.fare)))


def _affordable_go(
    req: PlanRequest, base_cost: int, gos: list[TripOption], backs: list[TripOption]
) -> list[TripOption]:
    """付得起的去程：加上最便宜的回程、電影票、餐費，不超過每人預算。"""
    if not req.budget_per_person:
        return list(gos)
    cheapest_back = min(o.fare for o in backs)
    return [o for o in gos if base_cost + o.fare + cheapest_back <= req.budget_per_person]


def _affordable_back(req: PlanRequest, base_cost: int, go: TripOption, backs: list[TripOption]) -> list[TripOption]:
    """付得起的回程：選好的去程之後，剩下的預算付得起的。"""
    if not req.budget_per_person:
        return list(backs)
    return [o for o in backs if base_cost + go.fare + o.fare <= req.budget_per_person]


def _pick_trips(
    req: PlanRequest, base_cost: int, go: TripSearch, back: TripSearch, choice: RouteChoice
) -> tuple[TripOption, TripOption]:
    """挑去程、回程：指定的走法（付得起的話），不然是預算內最快的；怎麼搭都超出預算就排除這個組合。"""
    if not _affordable_go(req, base_cost, go.options[:1], back.options[:1]):
        go.expand()  # 最快的走法超出預算，才找便宜的走法
        back.expand()
    gos = _affordable_go(req, base_cost, go.options, back.options)
    if not gos:
        raise Reject("超過每人預算")
    picked_go = next((o for o in gos if o.label == choice.go), gos[0])
    backs = _affordable_back(req, base_cost, picked_go, back.options)
    picked_back = next((o for o in backs if o.label == choice.back), backs[0])
    return picked_go, picked_back


def _transit_steps(route: Route, departing: bool) -> list[Step]:
    steps = []
    for i, leg in enumerate(route.legs):
        title = f"搭{leg.label}到{station_name(leg.to_name)}"
        if departing and i == 0:
            title = f"從{station_name(leg.from_name)}出發，{title}"
        detail = f"車程 {leg.ride_min} 分鐘，等車最久 {leg.wait} 分鐘，車資約 {leg_fare(leg.mode, leg.ride_min)} 元"
        if leg.transfer_min:
            detail = f"在{station_name(leg.from_name)}轉乘（步行 {leg.transfer_min} 分鐘）；{detail}"
        steps.append(Step(leg.ready - leg.transfer_min, leg.alight, "transit", title, detail))
    return steps


def _out_steps(ctx: PlanContext, place: Place, arrive_by: int, trip: TripOption) -> list[Step]:
    """去程的時間軸：搭車、步行到 place；比 arrive_by 早到時標出空檔。"""
    route, walk = trip.route, trip.walk
    steps = _transit_steps(route, departing=True)
    title = f"步行到{place.name}" if route.legs else f"從{station_name(ctx.origin.name)}步行到{place.name}"
    at_place = route.arrive + walk
    steps.append(Step(route.arrive, at_place, "walk", title, f"約 {walk} 分鐘"))
    if at_place < arrive_by:  # 等車以最壞情況估，通常會提早到
        steps.append(Step(at_place, arrive_by, "buffer", "提早抵達", f"多出 {arrive_by - at_place} 分鐘空檔"))
    return steps


def _home_steps(ctx: PlanContext, ready: int, trip: TripOption, flags: list[str]) -> list[Step]:
    """回程的時間軸：ready 時出發，步行到車站、搭車回出發站；接近末班車時加提醒。"""
    route, walk = trip.route, trip.walk
    if not route.legs:
        return [Step(ready, ready + walk, "walk", f"步行回{station_name(ctx.origin.name)}", f"約 {walk} 分鐘")]
    steps = [Step(ready, ready + walk, "walk", f"步行到{station_name(trip.station.name)}", f"約 {walk} 分鐘")]
    steps += _transit_steps(route, departing=False)
    for leg in route.legs:
        if leg.last - leg.ready < 15:
            flags.append(f"{leg.label}接近末班車（{station_name(leg.from_name)} {fmt(leg.last)} 發車）")
    return steps
```

- [ ] **Step 5: 換掉兩種排法與 `_finish`**

把 `_movie_first`、`_dinner_first`、`_finish` 三個函式整段換成下面這些。中間的電影、用餐時間軸先放進 `middle`，因為它不會隨走法改變；挑好走法後，再把去程、`middle`、回程接起來。

```python
def _movie_first(
    ctx: PlanContext, req: PlanRequest, s: ShowtimeInfo, r: RestaurantInfo, choice: RouteChoice = RouteChoice()
) -> Candidate:
    flags: list[str] = []
    arrive_cinema = s.start - PRE_SHOW_MIN
    go = TripSearch(lambda every: _out_options(ctx, req, s.cinema, arrive_cinema, every), choice.every)
    film_end = s.start + ADS_MIN + s.runtime_min
    middle = _movie_steps(s, arrive_cinema, film_end)

    walk_cr = walk_minutes(s.cinema, r.place)
    arrive_r = film_end + walk_cr
    middle.append(Step(film_end, arrive_r, "walk", f"步行到{r.place.name}", f"約 {walk_cr} 分鐘"))
    ready = max(arrive_r, r.open)
    if ready > arrive_r:
        middle.append(Step(arrive_r, ready, "wait", "等餐廳開門"))
    wait = _wait(ctx, r, ready)
    seat = ready + wait
    if wait:
        middle.append(Step(ready, seat, "wait", "候位", f"預估 {wait} 分鐘"))
    last_order = r.close - r.last_order_min
    if seat > last_order:
        raise Reject("散場後趕不上餐廳最後點餐")
    meal_end = seat + MEAL_MINUTES[r.pace.top]
    if meal_end > r.close:
        raise Reject("用餐時間會超過餐廳打烊")
    middle.append(_meal_step(r, seat, meal_end, last_order))

    back = TripSearch(lambda every: _home_options(ctx, req, r.place, meal_end, every), choice.every)
    trips = _pick_trips(req, s.price + r.price, go, back, choice)
    steps = _out_steps(ctx, s.cinema, arrive_cinema, trips[0]) + middle + _home_steps(ctx, meal_end, trips[1], flags)
    order_slack = last_order - seat
    if order_slack < 15:
        flags.append(f"散場後到最後點餐只剩 {order_slack} 分鐘，候位久一點就吃不到")
    home_slack = to_min(req.home_by) - trips[1].route.arrive
    return _finish(
        ctx, req, "movie_first", s, r, steps, flags, trips, (go.options, back.options), film_end,
        trips[0].walk + walk_cr + trips[1].walk, wait, min(order_slack, home_slack),
    )


def _dinner_first(
    ctx: PlanContext, req: PlanRequest, s: ShowtimeInfo, r: RestaurantInfo, choice: RouteChoice = RouteChoice()
) -> Candidate:
    flags: list[str] = []
    arrive_cinema = s.start - PRE_SHOW_MIN
    walk_rc = walk_minutes(r.place, s.cinema)
    leave_r = arrive_cinema - walk_rc
    meal_end = leave_r - DINNER_BUFFER_MIN
    seat = meal_end - MEAL_MINUTES[r.pace.top]
    if seat < r.open:
        raise Reject("電影開演前餐廳還沒開")
    last_order = r.close - r.last_order_min
    if seat > last_order:
        raise Reject("趕不上餐廳最後點餐")
    if meal_end > r.close:
        raise Reject("用餐時間會超過餐廳打烊")
    wait = _wait(ctx, r, seat)
    arrive_r = seat - wait

    go = TripSearch(lambda every: _out_options(ctx, req, r.place, arrive_r, every), choice.every)
    middle: list[Step] = []
    if wait:
        middle.append(Step(arrive_r, seat, "wait", "候位", f"預估 {wait} 分鐘"))
    middle.append(_meal_step(r, seat, meal_end, last_order))
    middle.append(Step(meal_end, leave_r, "buffer", "預留彈性時間", f"上菜慢或結帳排隊都還來得及（{DINNER_BUFFER_MIN} 分鐘）"))
    middle.append(Step(leave_r, arrive_cinema, "walk", f"步行到{s.cinema.name}", f"約 {walk_rc} 分鐘"))
    film_end = s.start + ADS_MIN + s.runtime_min
    middle += _movie_steps(s, arrive_cinema, film_end)

    back = TripSearch(lambda every: _home_options(ctx, req, s.cinema, film_end, every), choice.every)
    trips = _pick_trips(req, s.price + r.price, go, back, choice)
    steps = _out_steps(ctx, r.place, arrive_r, trips[0]) + middle + _home_steps(ctx, film_end, trips[1], flags)
    connection = PRE_SHOW_MIN + DINNER_BUFFER_MIN
    if wait >= 15:
        flags.append(f"候位預估 {wait} 分鐘；比預估多等超過 {connection} 分鐘就會趕不上開場")
    home_slack = to_min(req.home_by) - trips[1].route.arrive
    return _finish(
        ctx, req, "dinner_first", s, r, steps, flags, trips, (go.options, back.options), film_end,
        trips[0].walk + walk_rc + trips[1].walk, wait, min(connection, home_slack),
    )


def _finish(
    ctx: PlanContext, req: PlanRequest, order: str, s: ShowtimeInfo, r: RestaurantInfo, steps: list[Step],
    flags: list[str], trips: tuple[TripOption, TripOption], options: tuple[list[TripOption], list[TripOption]],
    film_end: int, walk_total: int, wait: int, tightest: int,
) -> Candidate:
    go, back = trips
    arrive_home = back.route.arrive
    home_by = to_min(req.home_by)
    home_slack = home_by - arrive_home
    if home_slack < 15:
        flags.append(f"只比 {fmt(home_by)} 早 {home_slack} 分鐘到家")
    tips = []
    if r.reservable:
        tips.append("記得先訂位（已假設不用候位）")
    elif wait >= 20:
        flags.append(f"尖峰時段，候位預估 {wait} 分鐘")
    steps.append(Step(arrive_home, None, "arrive", f"回到{station_name(ctx.origin.name)}", f"比 {fmt(home_by)} 早 {home_slack} 分鐘"))
    return Candidate(
        order=order, showtime=s, restaurant=r, steps=steps, leave_home=go.route.depart, arrive_home=arrive_home,
        film_end=film_end, cost=s.price + r.price + go.fare + back.fare, tightest=tightest, walk_total=walk_total,
        wait=wait, go=go, back=back, go_options=options[0], back_options=options[1], flags=flags, tips=tips,
    )


BUILDERS = {"movie_first": _movie_first, "dinner_first": _dinner_first}
```

- [ ] **Step 6: 預算檢查、Jev 讀到的文字、方案輸出**

`plan()` 的迴圈裡，刪掉下面三行。預算檢查已經移到 `_pick_trips`，超出預算時同樣記成「超過每人預算」：

```python
                if req.budget_per_person and c.cost > req.budget_per_person:
                    rejections["超過每人預算"] += 1
                    continue
```

`_user_text` 裡的 `f"想吃：{'、'.join(req.cuisines) or '不限'}；每人預算：{budget}；"` 改成：

```python
        f"想吃：{'、'.join(req.cuisines) or '不限'}；每人預算（含來回車資）：{budget}；"
```

`_ask_restaurant` 的選項說明，第二行改成分開寫電影加餐費和車資：

```python
        options.append(
            f"「{name}」：{r.cuisine.top}，每人約 {r.price} 元。介紹：{r.description}。評論：{'；'.join(r.reviews)}。"
            f"搭配這部片：{order}，和影城步行 {walk} 分鐘，最緊緩衝 {best.tightest} 分鐘，"
            f"電影加餐費每人 {best.showtime.price + r.price} 元，來回車資約 {best.go.fare + best.back.fare} 元。"
        )
```

（`features[name]["cost"]` 維持 `best.cost`，現在是含車資的總額，模擬版拿它和預算比較。）

在 `ESTIMATE_NOTES` 下面加上：

```python
FARE_NOTE = "車資為估計"  # 車資一律是估計（app/fares.py）
```

`_plan_dict` 在 `"cost_per_person": c.cost,` 下面加一行：

```python
        "cost_breakdown": {"movie": s.price, "meal": r.price, "go": c.go.fare, "back": c.back.fare},
```

`_plan_dict` 的 `"estimates"` 改成：

```python
        "estimates": _estimate_notes(s, r) + ([FARE_NOTE] if c.go.fare + c.back.fare else []),
```

- [ ] **Step 7: 確認測試通過**

Run: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_planner.py tests/test_taiwan_network.py tests/test_fares.py tests/test_transit.py -q`
Expected: 全部通過。
再跑全部測試：`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q`，全部通過。

- [ ] **Step 8: 確認排程速度沒有變慢**

改動前同一台機器上，三種情況都在 0.11 秒內。

Run：

```bash
PYTHONUTF8=1 JEV_MODE=mock OLLAMA_URL= .venv/Scripts/python.exe - <<'EOF'
import datetime as dt, time
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models import Base
from app.ingest import upsert_batch, precompute_jev
from app.collectors.fixture import FixtureCollector
from app import services
from app.schemas import PlanRequest
eng = create_engine("sqlite://"); Base.metadata.create_all(eng)
day = dt.date(2026, 10, 17)
with Session(eng) as s:
    upsert_batch(s, "fixture", FixtureCollector().collect(day, 1)); s.commit(); precompute_jev(s)
    taichung = dict(origin="台中車站", area="台北市/信義", depart_after="10:00", home_by="24:30")
    for kw in [dict(), taichung, taichung | dict(budget_per_person=3000)]:
        req = PlanRequest(date=day, **({"origin": "板橋", "depart_after": "16:00", "home_by": "22:30"} | kw))
        services.plan_date(s, req)
        t = time.perf_counter(); r = services.plan_date(s, req); el = time.perf_counter() - t
        print(kw.get("origin", "板橋"), kw.get("budget_per_person", 0), f"{el:.2f}s", r["stats"]["feasible"])
EOF
```

Expected：每一行都在 0.5 秒內。超過的話，先找出原因再往下做，例如是不是每個組合都在找所有走法。

- [ ] **Step 9: Commit**

```bash
git add app/planner.py tests/test_planner.py tests/test_taiwan_network.py
git commit -m "排程找出各種走法：預算改成含來回車資，超出預算時改搭便宜的走法；時間軸顯示車資" -m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"
```

---

### Task 3: Jev 選去程、回程

**Files:**
- Modify: `app/jev/questions.py`、`app/jev/__init__.py`、`app/jev/mock.py`、`app/jev/service.py`
- Modify: `app/planner.py`
  - import 區
  - `ASSUMPTIONS["怎麼選"]`
  - `_jev_pick` 之後新增函式
  - `plan()`
  - `_plan_dict`
- Modify: `tests/test_jev_mock.py`、`tests/test_planner.py`

**Interfaces:**
- Consumes：Task 2 的 `TripOption`、`RouteChoice`、`BUILDERS`、`_affordable_go`、`_affordable_back`、`Candidate.go/back/go_options/back_options`。已存在的 `_subject(req, *extra)`、`_user_text(req)`、`_choice_info(answer, choice, rank) -> dict`、`_ask(asks, ans, question, priority, kind)`、`JevAnswer(key, question, options, probs, source)`、`ctx.ask(question, subject, context) -> JevAnswer`（已套用 overrides）。
- Produces：
  - `app.jev.questions.TRANSIT_KEYS = {"去程": "transit_go", "回程": "transit_back"}`
  - `transit_choice_question(direction: str, labels: list[str]) -> Question`（也從 `app.jev` 匯出）
  - `app.jev.mock.transit_choice(options: tuple[str, ...], context: dict) -> list[float]`
    - `context["_features"][label] = {"saved": int, "transfers": int, "fare": int, "last_margin": int | None}`
    - `context["_budget_left"]` 是這個方向剩下的預算；`0` 代表沒設預算。
  - `planner._choose_trips(req, ctx, picked: list[dict], asks: dict) -> None`：每個 pick 的 `"candidate"` 換成重排後的組合，並加上 `"go"`、`"back"`（`_choice_info` 格式）和 `"trip_options"`（`(去程走法, 回程走法)`）。
  - 方案 dict 新欄位：
    - `jev_choice.go`、`jev_choice.back`
    - `transit = {"go": {...}, "back": {...}}`，每個方向有 `choice`、`fare`、`options`。
    - `options` 每一項有 `label`、`depart`、`arrive`、`ride_min`、`transfers`、`fare`。

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_jev_mock.py` 檔尾加上：

```python


def test_transit_question_keys():
    from app.jev import transit_choice_question

    go = transit_choice_question("去程", ["高鐵", "台鐵"])
    assert go.key == "transit_go" and go.options == ("高鐵", "台鐵")
    assert go.text == "這次約會的去程，最適合哪一種走法？"
    assert transit_choice_question("回程", ["高鐵"]).key == "transit_back"


def test_transit_choice_weighs_time_against_fare():
    from app.jev.mock import transit_choice

    features = {
        "高鐵": {"saved": 120, "transfers": 0, "fare": 700, "last_margin": None},
        "台鐵": {"saved": 0, "transfers": 0, "fare": 375, "last_margin": None},
    }
    options = ("高鐵", "台鐵")
    free = transit_choice(options, {"_features": features, "_budget_left": 0})
    assert free[0] > 0.75 and abs(sum(free) - 1) < 1e-9  # 沒設預算：省兩小時值得
    tight = transit_choice(options, {"_features": features, "_budget_left": 800})
    assert tight[1] > 0.6  # 高鐵幾乎用光剩下的預算，改搭台鐵


def test_transit_choice_avoids_the_last_train():
    from app.jev.mock import transit_choice

    features = {
        "末班高鐵": {"saved": 10, "transfers": 0, "fare": 700, "last_margin": 5},
        "早一班高鐵": {"saved": 0, "transfers": 0, "fare": 700, "last_margin": 40},
    }
    probs = transit_choice(("末班高鐵", "早一班高鐵"), {"_features": features, "_budget_left": 0})
    assert probs[1] > probs[0]
```

`tests/test_planner.py`：把開頭的 `_plan` 換成下面兩個函式，讓測試能用同樣的條件算出對象代碼：

```python
def _params(saturday, **kwargs):
    return {"date": saturday, "origin": "板橋", "depart_after": "16:00", "home_by": "22:30"} | kwargs


def _plan(session, saturday, **kwargs):
    return services.plan_date(session, PlanRequest(**_params(saturday, **kwargs)))
```

在 `tests/test_planner.py` 檔尾加上：

```python


# ---------- Jev 選車 ----------

# 台中到信義：去程有高鐵、台鐵兩種走法
TAICHUNG = {"origin": "台中車站", "area": "台北市/信義", "depart_after": "10:00", "home_by": "24:30"}


def test_single_way_to_travel_skips_jev(session, saturday):
    res = _plan(session, saturday)  # 板橋到信義只有捷運板南線
    assert res["plans"]
    for p in res["plans"]:
        for direction in ("go", "back"):
            assert p["jev_choice"][direction]["source"] == "program"
            trip = p["transit"][direction]
            assert [o["label"] for o in trip["options"]] == [trip["choice"]]
    assert not any(c["key"].startswith("transit_") for c in res["confirmations"])


def test_jev_chooses_how_to_travel_when_there_is_a_choice(session, saturday):
    best = _plan(session, saturday, **TAICHUNG)["plans"][0]
    go = best["transit"]["go"]
    assert len(go["options"]) >= 2, go["options"]
    assert go["choice"] in [o["label"] for o in go["options"]]
    assert best["jev_choice"]["go"]["source"] == "mock"
    assert best["jev_choice"]["go"]["choice"] == go["choice"]
    assert best["cost_breakdown"]["go"] == go["fare"]
    assert sum(best["cost_breakdown"].values()) == best["cost_per_person"]


def _fixed_parts(plan):
    return [(s["time"], s["title"]) for s in plan["steps"] if s["kind"] in ("movie", "meal")]


def _picks(res):
    return [(p["movie"]["title"], p["restaurant"]["name"], p["jev_confidence"]) for p in res["plans"]]


def test_switching_trip_keeps_movie_and_meal(session, saturday):
    from app import planner

    before = _plan(session, saturday, **TAICHUNG)
    best = before["plans"][0]
    other = next(o for o in best["transit"]["go"]["options"] if o["label"] != best["transit"]["go"]["choice"])
    req = PlanRequest(**_params(saturday, **TAICHUNG))
    key = f"transit_go:{planner._subject(req, best['movie']['title'], best['restaurant']['name'])}"
    after = _plan(session, saturday, **TAICHUNG, overrides={key: other["label"]})
    switched = after["plans"][0]
    assert switched["transit"]["go"]["choice"] == other["label"]
    assert switched["jev_choice"]["go"]["source"] == "user"
    assert switched["leave_home"] == other["depart"]
    assert switched["cost_breakdown"]["go"] == other["fare"]
    assert _fixed_parts(switched) == _fixed_parts(best)  # 電影、用餐時間不變
    assert _picks(after) == _picks(before)  # 前三名的片、餐廳、組合信心都不變


def test_unsure_trip_choice_is_offered(session, saturday):
    """Jev 選車沒把握時（這裡讓每種走法機率一樣），第一名方案的去程放進「想換換看嗎」。"""
    from app import planner
    from app.jev import JevAnswer, JevService

    req = PlanRequest(**_params(saturday, **TAICHUNG))
    ctx, _ = services.build_context(session, req, JevService(session))
    original = ctx.ask

    def ask(question, subject, context):
        if question.key.startswith("transit_"):
            n = len(question.options)
            return JevAnswer(f"{question.key}:{subject}", question.text, list(question.options), [1 / n] * n)
        return original(question, subject, context)

    ctx.ask = ask
    res = planner.plan(req, ctx)
    item = next(c for c in res["confirmations"] if c["key"].startswith("transit_go:"))
    assert item["kind"] == "choice" and len(item["options"]) >= 2
    assert item["question"] == "Jev 不太確定去程要搭哪一種，你比較想怎麼去？"
```

- [ ] **Step 2: 確認測試失敗**

Run: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_jev_mock.py tests/test_planner.py -q`
Expected: 新測試失敗。
- `ImportError: cannot import name 'transit_choice_question'`、`ImportError: cannot import name 'transit_choice'`。
- 方案沒有 `transit` 欄位：`KeyError: 'transit'`。

- [ ] **Step 3: Jev 題目、模擬版、context 標籤**

`app/jev/questions.py`：在 `restaurant_choice_question` 後面加上：

```python
TRANSIT_KEYS = {"去程": "transit_go", "回程": "transit_back"}


def transit_choice_question(direction: str, labels: list[str]) -> Question:
    """direction 是「去程」或「回程」；選項是走法名稱，由快到慢。"""
    return Question(TRANSIT_KEYS[direction], f"這次約會的{direction}，最適合哪一種走法？", tuple(labels))
```

`app/jev/__init__.py`：從 `.questions` 多匯入 `transit_choice_question`，`__all__` 也加上 `"transit_choice_question"`。照字母順序放在 `"restaurant_choice_question"` 後面。

`app/jev/mock.py`：在 `restaurant_choice` 後面加上：

```python
def transit_choice(options: tuple[str, ...], context: dict) -> list[float]:
    """選去程或回程的走法：省下的時間加分；轉乘、車資（佔剩下的預算越多扣越多）、回程太接近末班車扣分。"""
    budget_left = context["_budget_left"]
    scores = []
    for label in options:
        f = context["_features"][label]
        fare = 6 * f["fare"] / budget_left if budget_left > 0 else 0.001 * f["fare"]
        last_train = 1.0 if f["last_margin"] is not None and f["last_margin"] < 15 else 0.0
        scores.append(0.015 * f["saved"] - 0.5 * f["transfers"] - fare - last_train)
    return softmax(scores)
```

`MockJev._CHOICE_RULES` 改成：

```python
    _CHOICE_RULES = {
        "movie_choice": movie_choice,
        "restaurant_choice": restaurant_choice,
        "transit_go": transit_choice,
        "transit_back": transit_choice,
    }
```

`app/jev/service.py`：`_CONTEXT_LABELS` 加上 `"plan": "這個方案",`，放在 `"movie": "要看的電影",` 後面。

- [ ] **Step 4: planner 問 Jev、重排、輸出**

`app/planner.py` 的 import：`from .jev.questions import (...)` 裡多加 `transit_choice_question`。

`ASSUMPTIONS["怎麼選"]` 改成：

```python
    "怎麼選": "程式先篩出時間、交通、預算都接得起來的組合；看哪部片、吃哪一家由 Jev 選，前三名方案的去程、回程搭什麼也由 Jev 從接得上、付得起的走法裡選；同一組片和餐廳有好幾個場次時，程式挑緩衝最充裕的",
```

在 `_jev_pick` 後面（`_labels` 前面）加上：

```python
def _transfers(route: Route) -> int:
    return max(len(route.legs) - 1, 0)


def _last_margin(route: Route) -> int | None:
    """這趟車離末班車最近的一段還有幾分鐘；走路就到的沒有末班車。"""
    return min((leg.last - leg.ready for leg in route.legs), default=None)


def _trip_text(o: TripOption, going: bool) -> str:
    r = o.route
    ride = sum(leg.ride_min for leg in r.legs)
    longest_wait = max((leg.wait for leg in r.legs), default=0)
    when = f"{fmt(r.depart)} 出門、{fmt(r.arrive)} 到站" if going else f"{fmt(r.depart)} 開始等車、{fmt(r.arrive)} 到家"
    text = (
        f"「{o.label}」：{when}，車程 {ride} 分鐘（不含等車），轉乘 {_transfers(r)} 次，"
        f"最久等車 {longest_wait} 分鐘，每人車資約 {o.fare} 元（估計）"
    )
    margin = None if going else _last_margin(r)
    if margin is not None:
        text += f"，離末班車還有 {margin} 分鐘"
    return text + "。"


def _trip_user_text(req: PlanRequest, ctx: PlanContext) -> str:
    return (
        f"{_user_text(req)}；從{station_name(ctx.origin.name)}出發，到{req.area}；"
        f"最早 {req.depart_after} 出門，最晚 {req.home_by} 回到出發站"
    )


def _ask_trip(
    req: PlanRequest, ctx: PlanContext, movie: str, restaurant: str, base_cost: int,
    direction: str, options: list[TripOption], budget_left: int,
) -> JevAnswer:
    """問 Jev 這個方案的去程或回程搭哪一種；options 由快到慢。只有一種走法時不問。"""
    labels = [o.label for o in options]
    question = transit_choice_question(direction, labels)
    subject = _subject(req, movie, restaurant)
    if len(options) == 1:
        return JevAnswer(f"{question.key}:{subject}", question.text, labels, [1.0], source="program")
    going = direction == "去程"
    if going:  # 去程比出門時間：可以晚出門幾分鐘
        saved = {o.label: o.route.depart - min(x.route.depart for x in options) for o in options}
    else:  # 回程比到家時間：可以早到家幾分鐘
        saved = {o.label: max(x.route.arrive for x in options) - o.route.arrive for o in options}
    features = {
        o.label: {
            "saved": saved[o.label],
            "transfers": _transfers(o.route),
            "fare": o.fare,
            "last_margin": None if going else _last_margin(o.route),
        }
        for o in options
    }
    context = {
        "user": _trip_user_text(req, ctx),
        "plan": f"看《{movie}》、在{restaurant}吃飯，電影加餐費每人 {base_cost} 元",
        "options": [_trip_text(o, going) for o in options],
        "_features": features,
        "_budget_left": budget_left,
    }
    return ctx.ask(question, subject, context)


def _choose_trips(req: PlanRequest, ctx: PlanContext, picked: list[dict], asks: dict) -> None:
    """前三名決定後，請 Jev 選每個方案的去程、回程走法，再用選定的走法重排該方案（電影、用餐時間不變）。

    去程只列付得起的（加上最便宜的回程不超出預算）；回程只列選好去程之後、剩下的預算付得起的。
    """
    budget = req.budget_per_person
    for rank, pick in enumerate(picked, start=1):
        c = pick["candidate"]
        build = BUILDERS[c.order]
        full = build(ctx, req, c.showtime, c.restaurant, RouteChoice(every=True))
        base = c.showtime.price + c.restaurant.price
        movie, restaurant = c.showtime.movie_title, c.restaurant.place.name

        gos = _affordable_go(req, base, full.go_options, full.back_options)
        left = budget - base - min(o.fare for o in full.back_options) if budget else 0
        go_ans = _ask_trip(req, ctx, movie, restaurant, base, "去程", gos, left)
        go = next(o for o in gos if o.label == go_ans.top)

        backs = _affordable_back(req, base, go, full.back_options)
        left = budget - base - go.fare if budget else 0
        back_ans = _ask_trip(req, ctx, movie, restaurant, base, "回程", backs, left)

        pick["candidate"] = build(ctx, req, c.showtime, c.restaurant, RouteChoice(True, go.label, back_ans.top))
        pick["go"] = _choice_info(go_ans, go_ans.top, 1)
        pick["back"] = _choice_info(back_ans, back_ans.top, 1)
        pick["trip_options"] = (gos, backs)
        if rank == 1:
            for ans, question, priority in (
                (go_ans, "Jev 不太確定去程要搭哪一種，你比較想怎麼去？", 6),
                (back_ans, "Jev 不太確定回程要搭哪一種，你比較想怎麼回？", 7),
            ):
                if len(ans.options) > 1 and not ans.confident(ctx.threshold):
                    _ask(asks, ans, question, priority, kind="choice")
```

`plan()` 裡，`picked = _jev_pick(req, ctx, candidates, asks, notes)` 的下一行加上：

```python
    _choose_trips(req, ctx, picked, asks)  # 選車在選片、選餐廳之後，不影響前三名
```

在 `_plan_dict` 前面加上：

```python
def _option_dict(o: TripOption) -> dict:
    return {
        "label": o.label,
        "depart": fmt(o.route.depart),
        "arrive": fmt(o.route.arrive),
        "ride_min": sum(leg.ride_min for leg in o.route.legs),
        "transfers": _transfers(o.route),
        "fare": o.fare,
    }


def _trip_dict(chosen: TripOption, options: list[TripOption]) -> dict:
    return {"choice": chosen.label, "fare": chosen.fare, "options": [_option_dict(o) for o in options]}
```

`_plan_dict`：
- `"jev_choice"` 改成：

```python
        "jev_choice": {
            "movie": pick["movie"], "restaurant": pick["restaurant"], "go": pick["go"], "back": pick["back"],
        },
```

- `"cost_breakdown"` 下面加上：

```python
        "transit": {
            "go": _trip_dict(c.go, pick["trip_options"][0]),
            "back": _trip_dict(c.back, pick["trip_options"][1]),
        },
```

- [ ] **Step 5: 確認測試通過**

Run: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_jev_mock.py tests/test_planner.py -q`
Expected: 全部通過。
再跑全部測試：`PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q`，全部通過。

- [ ] **Step 6: Commit**

```bash
git add app/jev/questions.py app/jev/__init__.py app/jev/mock.py app/jev/service.py app/planner.py tests/test_jev_mock.py tests/test_planner.py
git commit -m "Jev 選每個方案的去程、回程走法：只列接得上、付得起的，只有一種時不問；方案輸出走法與車資" -m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"
```

---

### Task 4: 畫面、範本摘要、說明文件

**Files:**
- Modify: `app/llm.py`（`template_summary`）、`tests/test_llm.py`
- Modify: `frontend/app.js`（`choiceRow`、`renderJev`、`renderPlan`）、`frontend/style.css`
- Modify: `app/schemas.py`（`budget_per_person` 說明）、`app/mcp_server.py`（`plan_date` 說明）、`README.md`

**Interfaces:**
- Consumes：方案 dict 的 `transit.go/back.choice`、`transit.go/back.fare`、`cost_breakdown`、`jev_choice.go/back`（Task 2、3）。
- Produces：沒有新的程式介面。

- [ ] **Step 1: 寫失敗的測試**

在 `tests/test_llm.py` 檔尾加上：

```python


def test_template_summary_names_the_trips():
    from app.llm import template_summary

    plan = {
        "movie": {"start": "18:10", "cinema": "信義威秀", "title": "新片 A", "label": "IMAX 2D 英語"},
        "restaurant": {"name": "霧光義式餐館"},
        "order_text": "先看電影再吃飯",
        "leave_home": "17:30",
        "arrive_home": "22:10",
        "cost_per_person": 1310,
        "transit": {"go": {"choice": "捷運板南線", "fare": 45}, "back": {"choice": "步行", "fare": 0}},
    }
    text = template_summary({"plans": [plan], "stats": {"feasible": 5}, "notes": []})
    assert "去程搭捷運板南線、回程步行" in text
    assert "每人約 1310 元（含來回車資約 45 元）" in text
```

- [ ] **Step 2: 確認測試失敗**

Run: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_llm.py -q`
Expected: `test_template_summary_names_the_trips` 失敗（摘要裡沒有去程、回程）。

- [ ] **Step 3: 範本摘要**

`app/llm.py`：在 `template_summary` 前面加上：

```python
def _trip_phrase(direction: str, trip: dict) -> str:
    return f"{direction}步行" if trip["choice"] == "步行" else f"{direction}搭{trip['choice']}"
```

`template_summary` 最後的 `return (...)`（有方案時）換成：

```python
    go, back = best["transit"]["go"], best["transit"]["back"]
    return (
        f"從 {feasible} 個接得起來的組合裡，Jev 選出 {len(plans)} 個方案。"
        + "".join(result.get("notes") or [])
        + f"Jev 首選：{m['start']} 在{m['cinema']}看《{m['title']}》（{m['label']}），{best['order_text']}，"
        f"到{r['name']}用餐；{_trip_phrase('去程', go)}、{_trip_phrase('回程', back)}，"
        f"{best['leave_home']} 出門、{best['arrive_home']} 到家，"
        f"每人約 {best['cost_per_person']} 元（含來回車資約 {go['fare'] + back['fare']} 元）。"
    )
```

Run: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest tests/test_llm.py tests/test_api.py -q`
Expected: 全部通過。

- [ ] **Step 4: 畫面**

`frontend/app.js`：

1. `choiceRow` 加一個參數，用來寫「只有一個選項」時的說明：

```js
function choiceRow(label, c, only = "只有這個選項排得進去") {
  const pct = Math.round(c.prob * 100);
  const who = c.source === "user" ? "（你指定或確認的）" : c.source === "program" ? `（${only}）` : `（Jev 第 ${c.rank} 名）`;
```

（函式其他部分不變。）

2. `renderJev` 裡「Jev 的選擇」的 `<ul>` 換成：

```js
      <ul>${choiceRow("看哪部片", p.jev_choice.movie)}${choiceRow("吃哪一家", p.jev_choice.restaurant)}${choiceRow("去程搭什麼", p.jev_choice.go, "只有這種走法接得上")}${choiceRow("回程搭什麼", p.jev_choice.back, "只有這種走法接得上")}</ul>
```

3. `renderPlan` 裡，在 `const estimates = ...` 之後加上：

```js
  const b = p.cost_breakdown;
  const costLine = `<p class="cost-line">每人花費：電影票 $${b.movie.toLocaleString()}・餐費 $${b.meal.toLocaleString()}・去程車資 $${b.go.toLocaleString()}・回程車資 $${b.back.toLocaleString()}</p>`;
```

然後在模板的 `</dl>` 後面接上 `${costLine}`：

```js
      </dl>
      ${costLine}
      ${flags}${estimates}
```

`frontend/style.css`：在 `.timeline` 相關規則前面加上：

```css
.plan .cost-line { margin: 6px 0 0; font-size: 13px; color: var(--muted); }
```

- [ ] **Step 5: 說明文件**

`app/schemas.py`：`budget_per_person` 的說明改成：

```python
    budget_per_person: int = Field(0, ge=0, description="每人預算（電影票＋餐費＋來回車資），0 代表不限")
```

`app/mcp_server.py`：`plan_date` 說明的第一行改成：

```python
    """排出約會行程：電影＋餐廳＋大眾運輸（可跨縣市）。看哪部片、吃哪一家由 Jev 選，回傳 Jev 的前三名組合；每個方案的去程、回程搭什麼也由 Jev 從接得上、付得起的走法裡選（transit 列出各種走法與估計車資）。
```

並在 `meal_order：` 那一行後面加一行：

```python
    budget_per_person：每人預算，包含電影票、餐費與來回車資（車資為估計）。
```

`README.md`：

- 第 4 行的 `**看哪部片、吃哪一家由 Jev 決定**` 改成 `**看哪部片、吃哪一家、去程回程搭什麼由 Jev 決定**`。
- 第 10 行的 `> 交通班次是**估計值**；` 改成 `> 交通班次與車資是**估計值**；`。
- 「Jev 選擇」那列表格改成：

```markdown
| **Jev 選擇** | `app/planner.py`（`_jev_pick`、`_choose_trips`） | **看哪部片**（參考想看的片型、簡介）、**吃哪一家**（參考飲食限制、想吃的、預算、介紹與評論）、**去程回程搭什麼**（參考車程、轉乘、等車、車資與剩下的預算） |
```

- 「## Jev 怎麼做選擇」的清單換成：

```markdown
1. 程式篩出接得起來的組合後，問 Jev「這次約會最適合看下面哪一部電影？」，選項是排得進去的片，最多 8 部（使用者指定了片就不問）。
2. 針對 Jev 機率前三名的片，各問一次「看這部片，最適合去下面哪一家吃飯？」，選項是配得上這部片時間的餐廳，最多 8 家。
3. 組合信心 ＝ 選片機率 × 選餐廳機率，取前三名當方案；同一組片和餐廳有好幾個場次時，程式挑緩衝最充裕的。
4. 前三名方案決定後，程式列出每個方案時間接得上、預算付得起的走法（拿掉高鐵、台鐵、客運各算一次；又慢又不便宜的刪掉；每個方向最多 4 種），問 Jev「這次約會的去程／回程，最適合哪一種走法？」；只有一種走法時不問。選車的機率另外顯示，不乘進組合信心。
5. Jev 對選擇沒把握時，方案照樣排出來，另外列出 Jev 的前幾名讓使用者改選；改選後就用使用者選的重新排。
6. 卡片上的「最省」「最穩」是程式算出的客觀標籤，不是 Jev 的判斷。
```

- 在「**需要和 Jev 確認：…**」那一行後面空一行，加上：

```markdown
車資依交通方式和車程估計（`app/fares.py`）：捷運 20～65 元、市區公車 15 元、國道客運每分鐘約 2.3 元、台鐵每分鐘約 2.5 元、高鐵每分鐘約 13 元，連續搭同一條路線算一段票。每人預算包含電影票、餐費與來回車資，超出預算的走法程式先篩掉。
```

- 「## 目前的限制」：
  - `- Jev 模擬版的選片、選餐廳規則很簡單（片型符不符合、約會適合度、緩衝時間），…` 改成 `- Jev 模擬版的選片、選餐廳、選車規則很簡單（片型符不符合、約會適合度、緩衝時間、省多少時間與車資），只用來展示流程；實際效果要等接上 Jev API。`
  - `- 票價、沒標營業時間的餐廳營業時間、餐廳價位都是估計值；餐廳沒有評論。` 改成 `- 票價、車資、沒標營業時間的餐廳營業時間、餐廳價位都是估計值；餐廳沒有評論。車資不含學生票、敬老票等優惠。`

- [ ] **Step 6: 跑全部測試**

Run: `PYTHONUTF8=1 .venv/Scripts/python.exe -m pytest -q`
Expected: 全部通過。

- [ ] **Step 7: 在瀏覽器確認畫面**

1. 用 `.claude/launch.json` 的設定啟動開發伺服器。已經在跑的話先重新啟動，才會載入新的程式。
2. 打開網頁，排一個跨縣市的行程，例如出發站「台北車站」、商圈「台中市/七期」、最早 10:00 出門、最晚 24:30 到家。
3. 確認：
   - 卡片有「每人花費：電影票…・餐費…・去程車資…・回程車資…」，四項加起來等於「每人」。
   - 「Jev 怎麼選、怎麼判斷的」裡有「去程搭什麼」「回程搭什麼」兩列。
   - 時間軸每段車都有「車資約 … 元」。
   - 估計提醒有「車資為估計」。
   - 摘要寫出去程、回程搭什麼和來回車資。
4. 再排一次「板橋 → 台北市/信義」，確認「去程搭什麼」顯示「（只有這種走法接得上）」。
5. 瀏覽器 console 沒有錯誤。

- [ ] **Step 8: Commit**

```bash
git add app/llm.py tests/test_llm.py frontend/app.js frontend/style.css app/schemas.py app/mcp_server.py README.md
git commit -m "畫面與摘要顯示去程、回程搭什麼與車資；更新預算說明與 README" -m "Co-Authored-By: <實作的模型名稱> <noreply@anthropic.com>"
```
