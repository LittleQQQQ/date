"""背景工作（規格書 5.6）：RQ 佇列、工作函式、rq cron 的排程。

- 佇列 plans（規劃，優先）、crawl（抓取）。worker 監聽順序是 plans crawl，會先處理規劃。
- rq cron 只負責定時把工作排進佇列，真正執行的是 worker。
- RQ worker 預設用 fork()，不支援 Windows：worker 和 cron 跑在 Docker 裡（規格書 8.1）。
  Windows 上開發時可以用 python -m app.jobs.worker --simple（不 fork，只適合開發）。
"""
from __future__ import annotations

from rq import Queue

from ..redis_utils import get_rq_redis

PLANS = "plans"
CRAWL = "crawl"
PLAN_TIMEOUT_S = 10 * 60  # 規劃逾時【預設】
CRAWL_TIMEOUT_S = 30 * 60  # 抓取逾時【預設】


def queue(name: str) -> Queue:
    return Queue(name, connection=get_rq_redis())
