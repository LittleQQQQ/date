"""rq cron 的排程（規格書 5.6）：rq cron app.jobs.cron_config

rq cron 用 UTC 解讀 cron 字串（rq 2.12 的 rq.cron 用 croniter 搭配 UTC 的現在時間），
所以規格書寫的台灣時間要先換成 UTC，例如台灣每天 06:00 是 UTC 前一天 22:00。
rq cron 只負責定時排進佇列；同一個工作就算被排了兩次，也有 lock:job:{名稱} 擋住，不會同時跑。
"""
from __future__ import annotations

from rq import cron

from . import CRAWL, CRAWL_TIMEOUT_S
from .refresh_job import refresh_daily, refresh_transit, rejudge_and_refresh_geom

TW_OFFSET_H = 8
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def tw_cron(hour: int, minute: int = 0, weekday: str | None = None) -> str:
    """台灣時間的「每天 hour:minute」（或每週 weekday）→ UTC 的 cron 字串。"""
    utc_hour = hour - TW_OFFSET_H
    day_shift = -1 if utc_hour < 0 else 0
    utc_hour %= 24
    if weekday is None:
        return f"{minute} {utc_hour} * * *"
    # cron 的星期：0 是星期日，1 是星期一
    cron_day = (DAYS.index(weekday) + 1 + day_shift) % 7
    return f"{minute} {utc_hour} * * {cron_day}"


SCHEDULE = {
    "refresh_daily": tw_cron(6),  # 每天 06:00：開眼、OSM
    "refresh_transit": tw_cron(4, weekday="mon"),  # 每週一 04:00：TDX 班表與票價
    "rejudge_and_refresh_geom": tw_cron(3),  # 每天 03:00：過期的 Jev 判斷重算
}

cron.register(refresh_daily, CRAWL, cron=SCHEDULE["refresh_daily"], job_timeout=CRAWL_TIMEOUT_S, name="refresh_daily")
cron.register(refresh_transit, CRAWL, cron=SCHEDULE["refresh_transit"], job_timeout=CRAWL_TIMEOUT_S, name="refresh_transit")
cron.register(rejudge_and_refresh_geom, CRAWL, cron=SCHEDULE["rejudge_and_refresh_geom"], job_timeout=CRAWL_TIMEOUT_S,
              name="rejudge_and_refresh_geom")
