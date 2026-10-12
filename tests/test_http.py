import httpx
import pytest

from app.collectors.http import Fetcher, FetchError


def make(handler, cache_dir=None, min_interval=1.0, **options):
    sleeps: list[float] = []
    fetcher = Fetcher(
        cache_dir=cache_dir,
        min_interval=min_interval,
        **options,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        clock=lambda: 0.0,  # 時間不會前進，所以同一網站的第二個請求一定要等滿間隔
    )
    return fetcher, sleeps


def test_get_returns_text():
    fetcher, _ = make(lambda req: httpx.Response(200, text="你好"))
    assert fetcher.get("https://a.test/x") == "你好"


def test_same_host_waits_between_requests():
    fetcher, sleeps = make(lambda req: httpx.Response(200, text="ok"))
    fetcher.get("https://a.test/1")
    fetcher.get("https://a.test/2")
    fetcher.get("https://b.test/1")
    assert sleeps == [1.0]  # 只有第二次打 a.test 要等；b.test 是別的網站


def test_retries_on_503_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, text="ok")

    fetcher, sleeps = make(handler, min_interval=0)
    assert fetcher.get("https://a.test/x") == "ok"
    assert len(calls) == 3
    assert sleeps == [2, 4]


def test_backoff_sets_the_retry_wait():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, text="ok")

    fetcher, sleeps = make(handler, min_interval=0, backoff=15)
    assert fetcher.get("https://a.test/x") == "ok"
    assert sleeps == [15, 30]


def test_gives_up_after_retries():
    fetcher, sleeps = make(lambda req: httpx.Response(503), min_interval=0)
    with pytest.raises(FetchError, match="重試 3 次"):
        fetcher.get("https://a.test/x")
    assert sleeps == [2, 4]


def test_404_is_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(404)

    fetcher, _ = make(handler, min_interval=0)
    with pytest.raises(FetchError, match="404"):
        fetcher.get("https://a.test/x")
    assert len(calls) == 1


def test_connection_error_is_retried():
    def handler(req):
        raise httpx.ConnectError("連不上", request=req)

    fetcher, _ = make(handler, min_interval=0)
    with pytest.raises(FetchError, match="ConnectError"):
        fetcher.get("https://a.test/x")


def test_request_errors_beyond_transport_errors_become_fetch_errors():
    """重導向太多次、內容解不開這類錯誤不是 TransportError，也要轉成 FetchError，讓呼叫端只跳過這一頁。"""
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.TooManyRedirects("loop", request=req)

    fetcher, sleeps = make(handler, min_interval=0)
    with pytest.raises(FetchError, match="TooManyRedirects"):
        fetcher.get("https://a.test/x")
    assert len(calls) == 3 and sleeps == [2, 4]  # 和連線失敗一樣重試


def test_sites_with_different_subdomains_share_one_wait():
    fetcher, sleeps = make(lambda req: httpx.Response(200, text="ok"))
    fetcher.get("https://www.a.test/x")
    fetcher.get("https://ww2.a.test/y")  # 同一個網站的另一個主機名稱，仍要間隔 1 秒
    assert sleeps == [1.0]


def test_cache_avoids_second_request(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, text="地圖")

    fetcher, _ = make(handler, cache_dir=tmp_path, min_interval=0)
    assert fetcher.get("https://a.test/map", cache_days=30) == "地圖"
    assert fetcher.get("https://a.test/map", cache_days=30) == "地圖"
    assert len(calls) == 1
    fetcher.get("https://a.test/map")  # 沒指定 cache_days 就一定重抓
    assert len(calls) == 2


def test_forget_drops_cached_page(tmp_path):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, text="地圖")

    fetcher, _ = make(handler, cache_dir=tmp_path, min_interval=0)
    fetcher.get("https://a.test/map", cache_days=30)
    fetcher.forget("https://a.test/map")
    fetcher.get("https://a.test/map", cache_days=30)  # 快取被清掉，所以重抓
    assert len(calls) == 2


def test_forget_without_cache_does_nothing(tmp_path):
    fetcher, _ = make(lambda req: httpx.Response(200, text="ok"), cache_dir=tmp_path)
    fetcher.forget("https://a.test/never-fetched")  # 沒快取過
    fetcher, _ = make(lambda req: httpx.Response(200, text="ok"), cache_dir=None)
    fetcher.forget("https://a.test/x")  # 沒開快取


def test_post_sends_form_data():
    seen = {}

    def handler(req):
        seen["body"] = req.content.decode()
        return httpx.Response(200, text="{}")

    fetcher, _ = make(handler, min_interval=0)
    assert fetcher.post("https://a.test/api", {"data": "[out:json];"}) == "{}"
    assert seen["body"].startswith("data=")
