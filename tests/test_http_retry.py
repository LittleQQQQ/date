"""外部 API 失敗時重試（規格書 8.3）：逾時、連線失敗、5xx 最多重試 2 次，間隔 1 秒、2 秒；4xx 不重試。"""
import httpx
import pytest

from app.http_retry import send_with_retry


def sequence(*outcomes):
    """依序回傳狀態碼或丟出例外的 send()。"""
    calls = []

    def send():
        outcome = outcomes[len(calls)]
        calls.append(outcome)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome)

    return send, calls


def test_timeouts_and_server_errors_are_retried_twice():
    waits = []
    send, calls = sequence(503, httpx.ReadTimeout("太慢了"), 200)
    assert send_with_retry(send, waits.append).status_code == 200
    assert len(calls) == 3 and waits == [1.0, 2.0]


def test_gives_up_after_three_tries():
    waits = []
    send, calls = sequence(500, 502, 503)
    assert send_with_retry(send, waits.append).status_code == 503  # 交給呼叫端當成失敗處理
    assert len(calls) == 3 and waits == [1.0, 2.0]
    send, _ = sequence(httpx.ConnectError("連不上"), httpx.ConnectError("連不上"), httpx.ConnectTimeout("連不上"))
    with pytest.raises(httpx.ConnectTimeout):
        send_with_retry(send, lambda seconds: None)


@pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 429])
def test_success_and_client_errors_are_not_retried(status):
    send, calls = sequence(status, 200)
    assert send_with_retry(send, lambda seconds: pytest.fail("不該重試")).status_code == status
    assert len(calls) == 1
