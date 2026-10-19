"""資料更新的工作（規格書 5.6、7.2），在 RQ worker 裡執行。

每個工作開始時先搶 lock:job:{工作名稱}：搶到才執行，做完釋放；沒搶到（別的 worker 正在跑同一個工作）直接跳過。
鎖 10 分鐘過期，工作還在跑就每 3 分鐘延長一次。每個資料來源的結果另外記在 crawl_runs。
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..ingest import load_network, load_seed, make_collector, run_collector, run_job
from ..models import CrawlRun
from ..redis_utils.lock import RedisLock
from ..timeutil import TW, now_tw
from . import CRAWL, CRAWL_TIMEOUT_S, queue

log = logging.getLogger(__name__)

LOCK_TTL_MS = 10 * 60 * 1000
HEARTBEAT_S = 3 * 60
TRANSIT_MAX_AGE = dt.timedelta(days=7)
SKIPPED = "skipped"


def _locked(name: str, work: Callable[[], dict]) -> dict | str:
    lock = RedisLock(f"lock:job:{name}", LOCK_TTL_MS)
    if not lock.acquire():
        log.info("[%s] 別的 worker 正在執行，這次跳過", name)
        return SKIPPED
    lock.start_heartbeat(HEARTBEAT_S)
    try:
        return work()
    finally:
        lock.release()


def _run_sources(names: list[str]) -> dict:
    """依序執行資料來源，每個來源的結果記在 crawl_runs；一個失敗不影響後面的。"""
    results = {}
    with SessionLocal() as session:
        for name in names:
            run = run_collector(session, make_collector(name, session))
            results[name] = run.status
            if run.status != "success":
                log.warning("[%s] 更新失敗：%s", name, run.error)
    return results


def refresh_daily() -> dict | str:
    """每天：開眼場次、OSM 餐廳（規格書 7.2）。"""
    return _locked("refresh_daily", lambda: _run_sources(["atmovies", "osm"]))


def refresh_transit() -> dict | str:
    """每週：TDX 台鐵、公車的班表與票價。沒填 TDX 金鑰就跳過，路網沿用估計值。"""
    if not settings.use_tdx:
        log.info("[refresh_transit] 沒有設定 TDX 金鑰，跳過")
        return SKIPPED
    return _locked("refresh_transit", lambda: _run_sources(["tdx"]))


def sync_static_data(session: Session) -> dict:
    """路網骨架和暫定資料來自程式碼裡的檔案，每次 worker 啟動都同步一次（部署新版後就會生效）。"""
    network = run_job(session, "network", lambda: load_network(session))
    seed = run_job(session, "seed", lambda: load_seed(session))
    return {"network": network.status, "seed": seed.status}


def last_success(session: Session, job: str) -> dt.datetime | None:
    return session.scalar(select(func.max(CrawlRun.finished_at)).where(CrawlRun.job == job, CrawlRun.status == "success"))


def due_jobs(session: Session, now: dt.datetime | None = None) -> list[str]:
    """worker 啟動時該補做的工作：今天（台灣時間）還沒成功抓過開眼，或 7 天內沒成功更新過 TDX。"""
    now = now or now_tw()
    due = []
    atmovies = last_success(session, "atmovies")
    if atmovies is None or atmovies.astimezone(TW).date() < now.astimezone(TW).date():
        due.append("refresh_daily")
    tdx = last_success(session, "tdx")
    if settings.use_tdx and (tdx is None or now - tdx >= TRANSIT_MAX_AGE):
        due.append("refresh_transit")
    return due


JOBS: dict[str, Callable[[], dict | str]] = {"refresh_daily": refresh_daily, "refresh_transit": refresh_transit}


def catch_up() -> list[str]:
    """worker 啟動時呼叫：同步路網與暫定資料，再把該補做的工作排進 crawl 佇列。"""
    with SessionLocal() as session:
        sync_static_data(session)
        due = due_jobs(session)
    for name in due:
        queue(CRAWL).enqueue(JOBS[name], job_timeout=CRAWL_TIMEOUT_S, description=f"{name}（啟動時補做）")
    return due
