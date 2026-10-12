"""背景自動更新爬蟲資料。

- 伺服器啟動後馬上檢查一次：OSM 超過 7 天沒成功更新（或上一次有商圈沒抓到）、開眼今天還沒成功抓過，就在背景執行。
- 之後每天 REFRESH_HOUR 點再檢查一次。
- 失敗只記錄在 scrape_runs，等下一輪再試；網頁的狀態列會顯示正在更新哪個來源。
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .collectors import get_collector
from .config import settings
from .db import SessionLocal
from .ingest import run_collector
from .models import ScrapeRun
from .timeutil import now_tw

log = logging.getLogger(__name__)

OSM_MAX_AGE = dt.timedelta(days=7)
SOURCES = ("osm", "atmovies")  # 先抓餐廳，再抓場次


def due_sources(
    last_success: dict[str, dt.datetime | None],
    now: dt.datetime,
    incomplete: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    due = []
    osm = last_success.get("osm")
    if osm is None or now - osm >= OSM_MAX_AGE or "osm" in incomplete:
        due.append("osm")
    atmovies = last_success.get("atmovies")
    if atmovies is None or atmovies.date() < now.date():
        due.append("atmovies")
    return due


def next_check(now: dt.datetime, hour: int) -> dt.datetime:
    at = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return at if at > now else at + dt.timedelta(days=1)


def last_successes(session: Session) -> dict[str, dt.datetime | None]:
    rows = session.execute(
        select(ScrapeRun.source, func.max(ScrapeRun.finished_at))
        .where(ScrapeRun.status == "ok")
        .group_by(ScrapeRun.source)
    ).all()
    found = dict(rows)
    return {name: found.get(name) for name in SOURCES}


def incomplete_sources(session: Session) -> set[str]:
    """最近一次成功的執行有商圈沒抓到的來源。這些商圈沒有餐廳，不用等到下個週期，下一輪就重抓。

    只看 failed_areas；開眼有幾頁抓不到是正常的（failed_pages），不會因此重抓。
    """
    result = set()
    for name in SOURCES:
        run = session.scalars(
            select(ScrapeRun)
            .where(ScrapeRun.source == name, ScrapeRun.status == "ok")
            .order_by(ScrapeRun.finished_at.desc(), ScrapeRun.id.desc())
            .limit(1)
        ).first()
        if run is not None and (run.counts or {}).get("failed_areas", 0) > 0:
            result.add(name)
    return result


class Refresher:
    def __init__(
        self,
        session_factory: Callable = SessionLocal,
        hour: int | None = None,
        clock: Callable[[], dt.datetime] = now_tw,
    ):
        self.session_factory = session_factory
        self.hour = settings.refresh_hour if hour is None else hour
        self.clock = clock
        self.running: str | None = None  # 正在抓的來源
        self.last_errors: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="refresher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

    def run_due(self) -> list[str]:
        """檢查並執行該更新的來源，回傳這次執行了哪些。"""
        ran = []
        with self.session_factory() as session:
            for name in due_sources(last_successes(session), self.clock(), incomplete_sources(session)):
                if self._stop.is_set():
                    break
                self.running = name
                try:
                    run = run_collector(session, get_collector(name, session))
                except Exception as exc:  # 一個來源出意外，不要讓其他來源跟著被跳過
                    session.rollback()
                    error = f"{type(exc).__name__}: {exc}"[:2000]
                    log.exception("[%s] 更新發生錯誤", name)
                else:
                    error = None if run.status == "ok" else run.error
                    if error is not None:
                        log.warning("[%s] 更新失敗：%s", name, error)
                finally:
                    self.running = None
                ran.append(name)
                if error is None:
                    self.last_errors.pop(name, None)
                else:
                    self.last_errors[name] = error
        return ran

    def status(self) -> dict:
        return {"running": self.running, "errors": dict(self.last_errors), "auto": settings.auto_refresh}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception:  # 不要讓背景執行緒因為意外錯誤就停掉
                log.exception("自動更新發生錯誤")
            wait = (next_check(self.clock(), self.hour) - self.clock()).total_seconds()
            self._stop.wait(max(wait, 60))


refresher = Refresher()
