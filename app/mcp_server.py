"""MCP server：把排程功能包成 MCP 工具。

- 網頁聊天（/api/chat）透過 MCP 呼叫這些工具。
- 外部 MCP 用戶端（例如 MCP Inspector、Claude Desktop）也能連到 http://127.0.0.1:8000/mcp 使用。
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from . import services
from .db import SessionLocal
from .schemas import PlanRequest

mcp = MCPServer(
    "date-planner",
    title="約會一條龍",
    instructions=(
        "排出「電影＋餐廳＋交通」的約會行程。主要工具是 plan_date；"
        "結果裡的 confirmations 是 Jev 沒把握的判斷，請使用者選完後把答案放進 overrides 再呼叫一次。"
    ),
)


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"日期格式應為 YYYY-MM-DD：{value}") from exc


@mcp.tool(structured_output=True)
def plan_date(
    date: str,
    origin: str = "板橋",
    area: str = "信義",
    depart_after: str = "16:00",
    home_by: str = "23:00",
    movie_query: str = "",
    screen_types: list[str] | None = None,
    audio: str = "",
    cuisines: list[str] | None = None,
    budget_per_person: int = 0,
    meal_order: str = "any",
    max_walk_min: int = 12,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """排出約會行程：電影＋餐廳＋捷運，回傳最推薦、最穩、最省三個方案。

    date 為 YYYY-MM-DD；時間為 24 小時制 HH:MM；字串留空、數字填 0 代表不限。
    meal_order：after＝看完電影再吃，before＝先吃再看，any＝都可以。
    overrides：使用者確認過的 Jev 答案，格式 {confirmations[].key: 選項}。
    """
    try:
        req = PlanRequest(
            date=date, origin=origin, area=area, depart_after=depart_after, home_by=home_by,
            movie_query=movie_query, screen_types=screen_types or [], audio=audio, cuisines=cuisines or [],
            budget_per_person=budget_per_person, meal_order=meal_order, max_walk_min=max_walk_min,
            overrides=overrides or {},
        )
    except ValidationError as exc:
        raise ToolError("；".join(e["msg"] for e in exc.errors())) from exc
    with SessionLocal() as session:
        try:
            return services.plan_date(session, req)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@mcp.tool(structured_output=True)
def search_showtimes(date: str, area: str = "信義", movie_query: str = "") -> list[dict[str, Any]]:
    """查某天某商圈的電影場次。給 movie_query 時，只回傳 Jev 判斷可能是這部片的場次。"""
    with SessionLocal() as session:
        return services.search_showtimes(session, _date(date), area, movie_query)


@mcp.tool(structured_output=True)
def find_restaurants(cinema_id: int, max_walk_min: int = 10, date: str = "", open_at: str = "") -> list[dict[str, Any]]:
    """查某家影城步行範圍內的餐廳，附上 Jev 對約會適合度、料理類型、用餐快慢的判斷。

    open_at（HH:MM）有填時，會標出那個時間還能不能點餐。
    """
    with SessionLocal() as session:
        try:
            return services.find_restaurants(session, cinema_id, max_walk_min, _date(date) if date else None, open_at)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@mcp.tool(structured_output=True)
def check_route(origin: str, area: str = "信義", depart_at: str = "") -> list[dict[str, Any]]:
    """查出發站到商圈各捷運站的車程、班距與回程末班車。depart_at 有填時，估算最晚幾點會到。"""
    with SessionLocal() as session:
        try:
            return services.check_route(session, origin, area, depart_at)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
