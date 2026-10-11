"""程式進入點：uvicorn app.main:app

- /          網頁（frontend/）
- /api/...   REST API
- /mcp       MCP 端點（Streamable HTTP）
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import models  # noqa: F401  註冊資料表
from .api import router
from .config import ROOT_DIR
from .db import Base, SessionLocal, engine
from .ingest import ensure_demo_data
from .mcp_server import mcp

mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        ensure_demo_data(session)
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="約會一條龍", lifespan=lifespan)
app.include_router(router, prefix="/api")
app.router.routes.extend(mcp_app.routes)  # 把 /mcp 掛在同一個服務上
app.mount("/", StaticFiles(directory=ROOT_DIR / "frontend", html=True), name="frontend")
