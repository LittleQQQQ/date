"""外部 API 失敗時重試（規格書 8.3）：Google Places、TypeSafe Jev 共用。

- 逾時（10 秒）、連線失敗或 5xx：最多重試 2 次，間隔 1 秒、2 秒。
- 4xx 不重試（金鑰、格式、找不到這類問題，重試也一樣）。
- 開眼、OSM、TDX 走 collectors/http.py 的 Fetcher，它有自己的重試（間隔比較長，配合各網站的限制）；
  TDX 回 429 時交給限流機制排隊。
"""
from __future__ import annotations

import time
from collections.abc import Callable

import httpx

RETRY_DELAYS_S = (1.0, 2.0)


def send_with_retry(send: Callable[[], httpx.Response], sleep: Callable[[float], None] = time.sleep) -> httpx.Response:
    """呼叫 send()，失敗就照上面的規則重試。最後一次還是失敗時：5xx 照常回傳回應，連線問題丟出原本的例外。"""
    for delay in (*RETRY_DELAYS_S, None):
        try:
            resp = send()
        except httpx.TransportError:  # 逾時、連不上、連線中斷
            if delay is None:
                raise
        else:
            if resp.status_code < 500 or delay is None:
                return resp
        sleep(delay)
    raise AssertionError("不會走到這裡")
