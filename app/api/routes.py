"""REST 端點（規格書 6.2）。查詢直接呼叫 services.py；規劃排進 RQ，用 SSE 推送進度和結果（plan_stream.py）。

| 方法 | 路徑 | 說明 | MCP 工具（第 8 步） |
| GET | /api/areas | 4 個區域 | list_areas |
| GET | /api/areas/{code}/locations | 某區的地標 | list_locations |
| GET | /api/categories?kind=movie_genre|cuisine | 類別清單 | list_categories |
| GET | /api/coverage | 每區場次資料的日期範圍、目前的餐廳來源 | data_coverage |
| POST | /api/plans | 送出規劃，回傳 SSE 串流 | plan_date |
| GET | /api/health | 健康檢查 | — |

限流（每個 IP）：查詢每 60 秒 60 次；規劃每小時 10 次、同時 1 個。IP 取 request.client.host，
放在 nginx 後面時 uvicorn 要開 --proxy-headers 並設定 --forwarded-allow-ips，才拿得到使用者的 IP。
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import redis
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import services
from ..db import SessionLocal, engine, get_session
from ..plan_stream import PlanRun
from ..limits import read_retry_after
from ..redis_utils import get_redis
from ..schemas import PlanRequest
from .errors import APIError

HEARTBEAT_S = 15  # 每 15 秒送一行註解，避免連線被中間的代理伺服器切斷【預設】
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}  # 叫 nginx 不要緩衝

router = APIRouter(prefix="/api")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def read_limit(request: Request) -> None:
    if (retry := read_retry_after(client_ip(request))) is not None:
        raise APIError(429, "RATE_LIMITED", "查詢太頻繁，請稍後再試", {"Retry-After": str(retry)})


@router.get("/areas", dependencies=[Depends(read_limit)])
def areas(session: Session = Depends(get_session)) -> dict:
    return {"areas": services.list_areas(session)}


@router.get("/areas/{code}/locations", dependencies=[Depends(read_limit)])
def locations(code: str, session: Session = Depends(get_session)) -> dict:
    return {"area": code, "locations": services.list_locations(session, code)}


@router.get("/categories", dependencies=[Depends(read_limit)])
def categories(kind: str | None = None, session: Session = Depends(get_session)) -> dict:
    return {"categories": services.list_categories(session, kind)}


@router.get("/coverage", dependencies=[Depends(read_limit)])
def coverage(session: Session = Depends(get_session)) -> dict:
    return services.data_coverage(session)


@router.get("/health")
def health() -> dict:
    """資料庫（含 PostGIS）和 Redis 都連得上才是 ok；任何一個不行回 503。"""
    try:
        with engine.connect() as conn:
            postgis = conn.execute(text("SELECT postgis_lib_version()")).scalar()
    except OperationalError as exc:
        raise APIError(503, "SERVICE_UNAVAILABLE", "資料庫無法使用") from exc
    try:
        get_redis().ping()
    except redis.exceptions.RedisError as exc:
        raise APIError(503, "SERVICE_UNAVAILABLE", "Redis 無法使用") from exc
    return {"status": "ok", "database": "ok", "postgis": postgis, "redis": "ok"}


# ---------- 規劃（SSE） ----------


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def check_plan(req: PlanRequest) -> None:
    """排進佇列前先檢查和資料庫有關的規則（MCP 的 plan_date 也用）。"""
    with SessionLocal() as session:
        services.check_plan_request(session, req)


async def _single(event: str, data: dict) -> AsyncIterator[str]:
    yield sse(event, data)


async def _stream(run: PlanRun, request: Request) -> AsyncIterator[str]:
    last = time.monotonic()
    try:
        async for event in run.events(request.is_disconnected):
            if event is None:
                if time.monotonic() - last >= HEARTBEAT_S:
                    last = time.monotonic()
                    yield ": ping\n\n"
                continue
            last = time.monotonic()
            yield sse(event["event"], event["data"])
    finally:  # 使用者斷線也會走到這裡：取消工作、釋放這個 IP 的名額
        await run.close()


@router.post("/plans")
async def create_plan(req: PlanRequest, request: Request) -> StreamingResponse:
    """送出規劃，直接回傳 SSE 串流（規格書 6.4）。前端要用 fetch 讀串流（EventSource 只支援 GET）。

    事件：queued（排隊順位）→ progress（階段）→ result（完整結果）或 error，之後關閉串流。
    輸入不合法、地標不存在在排進佇列前就回 400／404；該區沒有場次資料時串流只有一個 NO_SHOWTIMES 的 error。
    """
    try:
        await run_in_threadpool(check_plan, req)
    except services.PlanError as exc:
        if exc.code != "NO_SHOWTIMES":
            raise
        body = _single("error", {"code": exc.code, "message": exc.message})
        return StreamingResponse(body, media_type="text/event-stream", headers=SSE_HEADERS)
    run = await PlanRun.start(req, client_ip(request))
    return StreamingResponse(_stream(run, request), media_type="text/event-stream", headers=SSE_HEADERS)
