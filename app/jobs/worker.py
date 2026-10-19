"""worker 進入點：python -m app.jobs.worker [--simple] [--burst]

啟動時先補做（同步路網與暫定資料、補排今天還沒做的更新），再監聽 plans、crawl 兩個佇列（plans 優先）。
- 預設用 RQ 的 Worker（每個工作 fork 一個子程序），要在 Linux／Docker 裡跑。
- --simple：不 fork，工作逾時改用計時器。只給 Windows 上開發用。
- --burst：處理完佇列裡的工作就結束（測試、手動補資料用）。
"""
from __future__ import annotations

import argparse
import logging

from rq import SimpleWorker, Worker
from rq.timeouts import TimerDeathPenalty

from ..redis_utils import get_rq_redis
from . import CRAWL, PLANS, queue
from .refresh_job import catch_up

log = logging.getLogger("app.jobs.worker")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="RQ worker：處理規劃與資料更新")
    parser.add_argument("--simple", action="store_true", help="不 fork（Windows 開發用）")
    parser.add_argument("--burst", action="store_true", help="處理完佇列裡的工作就結束")
    parser.add_argument("--no-catch-up", action="store_true", help="啟動時不補做")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s：%(message)s")

    if not args.no_catch_up:
        due = catch_up()
        log.info("啟動時補做：%s", "、".join(due) or "不用")

    queues = [queue(PLANS), queue(CRAWL)]
    if args.simple:
        worker = SimpleWorker(queues, connection=get_rq_redis())
        worker.death_penalty_class = TimerDeathPenalty
    else:
        worker = Worker(queues, connection=get_rq_redis())
    worker.work(burst=args.burst)


if __name__ == "__main__":
    main()
