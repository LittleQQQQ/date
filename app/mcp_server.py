"""MCP（規格書 6.7）：用官方 MCP SDK 手寫工具，掛在 /mcp（Streamable HTTP、無狀態模式）。

工具和 REST 端點一一對應，直接呼叫 services.py、plan_stream.py，輸入輸出格式相同：

| 工具 | 對應的 REST |
| list_areas | GET /api/areas |
| list_locations | GET /api/areas/{code}/locations |
| list_categories | GET /api/categories |
| data_coverage | GET /api/coverage |
| plan_date | POST /api/plans（進度改用 MCP 進度通知，最後回傳完整結果） |

- 限流和 REST 共用同一組 key（以 IP 計）：查詢每 60 秒 60 次；規劃每小時 10 次、同時 1 個。
- 錯誤回傳 is_error，內容是「代碼：說明」，代碼和 REST 相同（例如 NOT_FOUND、RATE_LIMITED）。
- 用戶端取消（關閉連線）時，排隊中或執行中的規劃會被取消，和 REST 斷線一樣。
- LLM 備援：sampling 在 SDK 2.3 的無狀態模式下送不出去（舊協定沒有回傳的通道），新協定（2026-07-28）
  又把 sampling 列為淘汰功能，所以 MCP 和 REST 一樣用自家 Ollama（規格書 8.5 有記錄）。
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

import redis
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field, ValidationError
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool

from . import services
from .api.errors import validation_message
from .api.routes import check_plan
from .config import settings
from .db import SessionLocal
from .limits import read_retry_after
from .plan_stream import PlanRejected, PlanRun
from .schemas import PlanRequest

mcp = MCPServer(
    "date-planner",
    title="約會行程規劃",
    version="2.0",
    instructions=(
        "規劃桃園八德、桃園、中壢、青埔的約會行程：看電影＋吃飯＋大眾運輸。"
        "先用 list_areas、list_locations 找區域和地標（出發地、終點都要選地標的 id），"
        "用 data_coverage 看哪幾天有場次資料，需要時用 list_categories 查電影種類和餐廳類別代碼，"
        "最後呼叫 plan_date。plan_date 要幾秒到幾十秒，執行中會送進度通知。"
    ),
)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
STAGE_TEXT = {
    "loading_data": "讀取資料",
    "building_candidates": "組合場次和餐廳",
    "routing": "計算交通",
    "waiting_quota": "等待 Jev 額度",
    "fetching_places": "查詢 Google 店家資料",
    "judging": "Jev 判斷",
    "llm_fallback": "Jev 沒把握，交給 LLM",
}


def _ip(ctx: Context) -> str:
    request = ctx.request_context.request  # Streamable HTTP 時是 Starlette 的 Request
    client = getattr(request, "client", None)
    return client.host if client else "unknown"


def _fail(code: str, message: str) -> ToolError:
    return ToolError(f"{code}：{message}")


def _query(ctx: Context, work):
    """查詢工具共用：限流、開 session、把錯誤換成「代碼：說明」。"""
    try:
        if (retry := read_retry_after(_ip(ctx))) is not None:
            raise _fail("RATE_LIMITED", f"查詢太頻繁，請 {retry} 秒後再試")
        with SessionLocal() as session:
            return work(session)
    except services.PlanError as exc:
        raise _fail(exc.code, exc.message) from exc
    except (redis.exceptions.RedisError, OperationalError) as exc:
        raise _fail("SERVICE_UNAVAILABLE", "服務暫時無法使用，請稍後再試") from exc


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def list_areas(ctx: Context) -> dict[str, Any]:
    """列出 4 個區域（代碼和名稱）：八德 bade、桃園 taoyuan、中壢 zhongli、青埔 qingpu。"""
    return _query(ctx, lambda s: {"areas": services.list_areas(s)})


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def list_locations(
    area: Annotated[str, Field(description="區域代碼，例如 zhongli")],
    ctx: Context,
) -> dict[str, Any]:
    """列出某區的地標（id、名稱、地址、座標）。出發地和終點都從這份清單選，傳 id 給 plan_date。"""
    return _query(ctx, lambda s: {"area": area, "locations": services.list_locations(s, area)})


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def list_categories(
    ctx: Context,
    kind: Annotated[Literal["movie_genre", "cuisine"] | None,
                    Field(description="movie_genre 電影種類、cuisine 餐廳類別；不給就兩種都列")] = None,
) -> dict[str, Any]:
    """列出電影種類和餐廳類別的代碼與名稱，給 plan_date 的 movie_genre、cuisine 用。"""
    return _query(ctx, lambda s: {"categories": services.list_categories(s, kind)})


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def data_coverage(ctx: Context) -> dict[str, Any]:
    """每區有場次資料的日期範圍（沒有資料是 null），以及目前規劃用的餐廳來源（google 或 osm）。

    plan_date 的出發時間要在這個範圍內。餐廳來源是 google 時，顯示結果要註明資料來自 Google 地圖。
    """
    return _query(ctx, services.data_coverage)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True),
          structured_output=True)
async def plan_date(
    area: Annotated[Literal["bade", "taoyuan", "zhongli", "qingpu"], Field(description="區域代碼")],
    origin_location_id: Annotated[int, Field(description="出發地的地標 id（要屬於 area，見 list_locations）")],
    destination_location_id: Annotated[int, Field(description="終點的地標 id（要屬於 area，可以和出發地相同）")],
    order: Annotated[Literal["movie_first", "meal_first"], Field(description="movie_first 先看電影，meal_first 先吃飯")],
    depart_at: Annotated[str, Field(description="出發時間，ISO 8601 並帶時區，例如 2026-10-20T17:30:00+08:00")],
    budget_total: Annotated[int, Field(description="總預算（新台幣），所有人加起來，要大於 0")],
    ctx: Context,
    movie_genre: Annotated[str | None, Field(description="電影種類代碼（見 list_categories），null 代表不限")] = None,
    cuisine: Annotated[str | None, Field(description="餐廳類別代碼（見 list_categories），null 代表不限")] = None,
    party_size: Annotated[int, Field(description="人數 1～10：票價和車資會乘以人數")] = 2,
) -> dict[str, Any]:
    """排出約會行程：電影＋餐廳＋大眾運輸，回傳最多 5 個依 Jev 分數排序的方案（格式同 REST 的規劃結果）。

    每個方案有時間軸（交通、電影、用餐）、費用估計（票價和車資已乘以人數）和 Jev 的判斷。
    結果不會保存，也不能重新取得。
    """
    try:
        req = PlanRequest.model_validate({
            "area": area, "origin_location_id": origin_location_id, "destination_location_id": destination_location_id,
            "order": order, "depart_at": depart_at, "movie_genre": movie_genre, "cuisine": cuisine,
            "party_size": party_size, "budget_total": budget_total,
        })
    except ValidationError as exc:
        raise _fail("VALIDATION_ERROR", validation_message(list(exc.errors()))) from exc
    try:
        await run_in_threadpool(check_plan, req)
        run = await PlanRun.start(req, _ip(ctx))
    except services.PlanError as exc:
        raise _fail(exc.code, exc.message) from exc
    except PlanRejected as exc:
        retry = f"（{exc.retry_after_s} 秒後再試）" if exc.retry_after_s else ""
        raise _fail(exc.code, exc.message + retry) from exc
    except (redis.exceptions.RedisError, OperationalError) as exc:
        raise _fail("SERVICE_UNAVAILABLE", "服務暫時無法使用，請稍後再試") from exc

    step = 0
    try:  # 用戶端取消時這裡會被中斷，finally 取消工作、釋放名額
        async for event in run.events():
            if event is None:
                continue
            kind, data = event["event"], event["data"]
            if kind == "result":
                return data
            if kind == "error":
                raise _fail(data["code"], data["message"])
            step += 1  # MCP 的進度要一直往上加
            if kind == "queued":
                message = f"排隊中，第 {data['position']} 位"
            else:
                message = STAGE_TEXT.get(data["stage"], data["stage"])
                if data.get("total"):
                    message += f"（{data['done']}/{data['total']}）"
            await ctx.report_progress(step, None, message)
    finally:
        await run.close()
    raise _fail("UPSTREAM_ERROR", "規劃中斷了，請重新送出")


def transport_security() -> TransportSecuritySettings:
    """防 DNS rebinding：只接受 localhost 和 MCP_ALLOWED_HOSTS 列出的網域。"""
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    for host in settings.mcp_allowed_host_list:
        hosts += [host, f"{host}:*"]
        origins += [f"https://{host}", f"http://{host}"]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def new_session_manager():
    """每次啟動建立新的 session manager（SDK 規定一個只能 run 一次；測試會啟動很多次）。"""
    mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, transport_security=transport_security())
    return mcp.session_manager


class MCPEndpoint:
    """/mcp 的 ASGI 端點：交給目前的 session manager 處理。"""

    async def __call__(self, scope, receive, send) -> None:
        await mcp.session_manager.handle_request(scope, receive, send)
