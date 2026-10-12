"""網頁用的 REST API。"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from mcp import Client
from pydantic import ValidationError
from sqlalchemy.orm import Session

from . import llm, services
from .config import settings
from .db import get_session
from .mcp_server import mcp
from .refresher import refresher
from .schemas import ChatRequest, PlanRequest
from .timeutil import today_tw

router = APIRouter()


async def call_mcp_tool(name: str, arguments: dict) -> dict | list:
    """透過 MCP 呼叫工具：預設在同一個程序內連線；設定 MCP_CLIENT_URL 就改走 HTTP。"""
    async with Client(settings.mcp_client_url or mcp) as client:
        result = await client.call_tool(name, arguments)
    if result.is_error:
        message = result.content[0].text if result.content else "MCP 工具執行失敗"
        raise HTTPException(400, message.split(": ", 1)[-1])
    data = result.structured_content
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    return data


@router.get("/meta")
def get_meta(session: Session = Depends(get_session)) -> dict:
    return services.meta(session)


@router.get("/status")
async def get_status() -> dict:
    return {"llm": await llm.status(), "jev": {"mode": settings.jev_mode}, "refresh": refresher.status()}


@router.post("/plan")
async def post_plan(req: PlanRequest) -> dict:
    """網頁用：下拉選單的條件 → 透過 MCP 呼叫 plan_date → LLM 寫摘要。"""
    result = await call_mcp_tool("plan_date", req.model_dump(mode="json"))
    summary, summary_source = await llm.summarize(result)
    return {"result": result, "summary": summary, "summary_source": summary_source}


@router.post("/chat")
async def post_chat(body: ChatRequest, session: Session = Depends(get_session)) -> dict:
    """自然語言 API（網頁目前沒用到）：一句話 → LLM 解析條件 → 透過 MCP 呼叫 plan_date → LLM 寫摘要。"""
    fields, parse_info = await llm.parse_request(body.message, today_tw(), services.meta(session))
    try:
        req = PlanRequest(**fields)
    except ValidationError as exc:
        raise HTTPException(422, "；".join(e["msg"] for e in exc.errors())) from exc
    result = await call_mcp_tool("plan_date", req.model_dump(mode="json"))
    summary, summary_source = await llm.summarize(result)
    return {
        "request": req.model_dump(mode="json"),
        "parse": parse_info,
        "result": result,
        "summary": summary,
        "summary_source": summary_source,
    }


@router.get("/showtimes")
def get_showtimes(date: dt.date, area: str = "信義", movie_query: str = "", session: Session = Depends(get_session)):
    return services.search_showtimes(session, date, area, movie_query)


@router.get("/restaurants")
def get_restaurants(
    cinema_id: int, max_walk_min: int = 10, date: dt.date | None = None, session: Session = Depends(get_session)
):
    try:
        return services.find_restaurants(session, cinema_id, max_walk_min, date)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
