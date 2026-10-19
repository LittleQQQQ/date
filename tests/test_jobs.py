"""背景工作（規格書 5.6）：排程時間、工作鎖、啟動時補做、實際經過 RQ 執行。"""
import datetime as dt

import pytest
from rq import Queue
from rq.cron import CronScheduler
from sqlalchemy import func, select

from app.collectors.base import Batch, Collector, RestaurantRecord
from app.config import settings
from app.db import SessionLocal
from app.jobs import cron_config, refresh_job
from app.jobs.cron_config import tw_cron
from app.jobs.refresh_job import SKIPPED, catch_up, due_jobs, refresh_daily, refresh_transit
from app.models import CrawlRun, Restaurant, Station
from app.redis_utils import get_rq_redis
from app.redis_utils.lock import RedisLock
from app.timeutil import TW

pytestmark = pytest.mark.usefixtures("redis_client")


class FakeCollector(Collector):
    def __init__(self, name, batch=None, error=None):
        self.name, self.batch, self.error = name, batch or Batch(), error

    def collect(self):
        if self.error:
            raise self.error
        return self.batch


def _fake_sources(monkeypatch, **collectors):
    monkeypatch.setattr(refresh_job, "make_collector", lambda name, session: collectors[name])


# ---------- 排程時間 ----------


def test_taiwan_times_become_utc_cron_strings():
    assert tw_cron(6) == "0 22 * * *"  # 台灣每天 06:00 是 UTC 前一天 22:00
    assert tw_cron(4, weekday="mon") == "0 20 * * 0"  # 台灣星期一 04:00 是 UTC 星期日 20:00
    assert tw_cron(9, 30) == "30 1 * * *"  # 不跨日
    assert tw_cron(9, weekday="mon") == "0 1 * * 1"


def test_cron_config_fires_at_taiwan_time():
    scheduler = CronScheduler(connection=get_rq_redis())
    scheduler.load_config_from_file("app.jobs.cron_config")
    jobs = {job.name: job for job in scheduler.get_jobs()}
    assert set(jobs) == {"refresh_daily", "refresh_transit", "rejudge_and_refresh_geom"}
    assert all(job.queue_name == "crawl" for job in jobs.values())
    daily = jobs["refresh_daily"].next_enqueue_time.astimezone(TW)
    weekly = jobs["refresh_transit"].next_enqueue_time.astimezone(TW)
    assert (daily.hour, daily.minute) == (6, 0)
    assert (weekly.weekday(), weekly.hour) == (0, 4)  # 星期一 04:00
    assert cron_config.SCHEDULE["refresh_daily"] == jobs["refresh_daily"].cron


# ---------- 工作 ----------


def test_job_is_skipped_while_another_worker_runs_it(monkeypatch):
    _fake_sources(monkeypatch)  # 沒有任何來源：真的執行就會出錯
    other = RedisLock("lock:job:refresh_daily", 60_000)
    assert other.acquire()
    assert refresh_daily() == SKIPPED
    other.release()


def test_refresh_daily_runs_each_source_and_records_it(monkeypatch, committed):
    osm = Batch(restaurants=[RestaurantRecord(external_id="node/1", lat=24.962, lng=121.2246, osm_name="店")])
    _fake_sources(monkeypatch, atmovies=FakeCollector("atmovies", error=RuntimeError("網站改版")),
                  osm=FakeCollector("osm", osm))
    assert refresh_daily() == {"atmovies": "failed", "osm": "success", "judge": "success"}  # 一個失敗不影響後面的
    with SessionLocal() as s:
        runs = dict(s.execute(select(CrawlRun.job, CrawlRun.status)).all())
        assert runs == {"atmovies": "failed", "osm": "success", "judge": "success"}
        assert s.scalar(select(func.count()).select_from(Restaurant)) == 1
    assert RedisLock("lock:job:refresh_daily", 1000).acquire()  # 做完有釋放鎖


def test_refresh_transit_is_skipped_without_a_tdx_key():
    assert not settings.use_tdx  # 測試裡清空了金鑰
    assert refresh_transit() == SKIPPED


# ---------- 啟動時補做 ----------


def _run(session, job, status, finished):
    session.add(CrawlRun(job=job, status=status, started_at=finished, finished_at=finished))
    session.flush()


def test_due_jobs_follow_the_last_successful_runs(session, monkeypatch):
    now = dt.datetime(2026, 10, 19, 9, 0, tzinfo=TW)
    assert due_jobs(session, now) == ["refresh_daily"]  # 從來沒抓過；沒有 TDX 金鑰就不補交通
    _run(session, "atmovies", "success", now.replace(hour=6))
    _run(session, "atmovies", "failed", now.replace(hour=8))
    assert due_jobs(session, now) == []  # 今天已經成功過一次
    monkeypatch.setattr(settings, "tdx_client_id", "id")
    monkeypatch.setattr(settings, "tdx_client_secret", "secret")
    assert due_jobs(session, now) == ["refresh_transit"]
    _run(session, "tdx", "success", now - dt.timedelta(days=6))
    assert due_jobs(session, now) == []
    assert due_jobs(session, now + dt.timedelta(days=1)) == ["refresh_daily", "refresh_transit"]


def test_run_times_are_stored_with_the_right_time_zone(session):
    """不帶時區的時間會被 PostgreSQL 當成 UTC，差 8 小時；due_jobs 拿它和現在相減也會出錯。"""
    from app.ingest import run_job

    run = run_job(session, "network", lambda: {})
    session.refresh(run)
    assert abs(run.finished_at - dt.datetime.now(dt.timezone.utc)) < dt.timedelta(minutes=1)
    assert due_jobs(session) == ["refresh_daily"]  # 不會因為時區出錯


def test_catch_up_syncs_static_data_and_enqueues_due_jobs(monkeypatch, committed):
    enqueued = []

    class FakeQueue:
        def enqueue(self, func, **kwargs):
            enqueued.append(func.__name__)

    monkeypatch.setattr(refresh_job, "queue", lambda name: FakeQueue())
    assert catch_up() == ["refresh_daily"]
    assert enqueued == ["refresh_daily"]
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(Station)) == 7  # 路網骨架同步進來了
        statuses = set(s.scalars(select(CrawlRun.status).where(CrawlRun.job.in_(["network", "seed"]))))
        assert statuses == {"success"}


def test_jobs_run_through_rq():
    """實際經過 RQ：用函式的匯入路徑把工作排進佇列再執行（不 fork，Windows 也能跑）。"""
    q = Queue("crawl", connection=get_rq_redis(), is_async=False)
    job = q.enqueue(refresh_transit)
    assert job.is_finished and job.return_value() == SKIPPED
