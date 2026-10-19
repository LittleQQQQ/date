"""FastAPI 進入點：/api（規格書第 6 節）、/mcp（6.7，Streamable HTTP），其他路徑是網頁（frontend/）。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api import errors
from .api.routes import router
from .config import ROOT_DIR, settings
from .mcp_server import MCPEndpoint, new_session_manager


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with new_session_manager().run():
        yield


app = FastAPI(title="約會行程規劃", version="2.0", lifespan=lifespan)
errors.install(app)
if settings.cors_origin_list:  # 只允許前端網域（規格書 6.1）；同一個網域就不用開
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"])
app.include_router(router)
app.add_route("/mcp", MCPEndpoint(), methods=["GET", "POST", "DELETE"], include_in_schema=False)
app.mount("/", StaticFiles(directory=ROOT_DIR / "frontend", html=True), name="frontend")  # 放最後：前面的路徑優先
