import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from models import OpenAIRequest
from upstreams import cookie_proxy


@pytest.fixture
def nonstream_call(monkeypatch):
    monkeypatch.setattr(cookie_proxy, "_get_cookie_string", lambda: "TEST_COOKIE")
    monkeypatch.setattr(cookie_proxy, "_get_project_id", lambda: "TEST_PROJECT")
    monkeypatch.setattr(cookie_proxy, "build_headers", lambda _: {"cookie": "TEST_COOKIE"})
    monkeypatch.setattr(cookie_proxy, "get_proxy_client_args", lambda: {"trust_env": False})
    monkeypatch.setattr(cookie_proxy, "get_retry_settings", lambda: (3, 0))
    monkeypatch.setattr(
        cookie_proxy, "build_batch_graphql_body_async",
        AsyncMock(return_value={"variables": {}}),
    )
    upstream_client = AsyncMock()
    context = AsyncMock()
    context.__aenter__.return_value = upstream_client
    client_factory = Mock(return_value=context)
    monkeypatch.setattr(cookie_proxy.httpx, "AsyncClient", client_factory)
    retry = Mock()
    traceback = Mock()
    sleep = AsyncMock()
    monkeypatch.setattr(cookie_proxy.stats, "add_retry", retry)
    monkeypatch.setattr(cookie_proxy.traceback, "print_exc", traceback)
    monkeypatch.setattr(cookie_proxy.asyncio, "sleep", sleep)

    request = OpenAIRequest(
        model="gemini-4-argon", messages=[{"role": "user", "content": "hello"}],
        stream=False,
    )
    fastapi_request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

    def call():
        return asyncio.run(cookie_proxy.CookieProxyUpstream().chat_completions(
            request, fastapi_request,
        ))

    return SimpleNamespace(
        call=call, client=upstream_client, retry=retry, traceback=traceback, sleep=sleep,
        client_factory=client_factory, request=request, fastapi_request=fastapi_request,
    )


@pytest.mark.parametrize("exception_type,reason", [
    (httpx.ReadTimeout, "读取上游响应超时"),
    (httpx.ConnectTimeout, "连接上游超时"),
    (httpx.WriteTimeout, "发送上游请求超时"),
    (httpx.PoolTimeout, "等待上游连接池超时"),
    (httpx.TimeoutException, "上游请求超时"),
])
@pytest.mark.parametrize("detail", ["", "timeout: PRIVATE_DETAIL"])
def test_nonstream_timeout_is_concise_and_terminal(
    nonstream_call, capsys, exception_type, reason, detail,
):
    nonstream_call.client.post.side_effect = exception_type(detail)

    response = nonstream_call.call()

    assert response.status_code == 504
    assert json.loads(response.body) == {
        "error": {"message": f"{reason}，本次请求已结束。", "type": "timeout", "code": "upstream_timeout", "param": None},
    }
    nonstream_call.client.post.assert_awaited_once()
    nonstream_call.retry.assert_not_called()
    nonstream_call.sleep.assert_not_awaited()
    nonstream_call.traceback.assert_not_called()
    captured = capsys.readouterr()
    error_lines = [line for line in captured.out.splitlines() if line.startswith("❌")]
    assert len(error_lines) == 1
    assert reason in error_lines[0]
    assert "Traceback" not in captured.out + captured.err
    assert "PRIVATE_DETAIL" not in captured.out + captured.err


def test_nonstream_http_retry_behavior_is_unchanged(nonstream_call):
    nonstream_call.client.post.side_effect = [
        httpx.Response(503, text="busy"),
        httpx.Response(418, text="other upstream error"),
    ]

    response = nonstream_call.call()

    assert response.status_code == 418
    assert json.loads(response.body)["error"]["message"] == "other upstream error"
    assert nonstream_call.client.post.await_count == 2
    nonstream_call.retry.assert_called_once()
    nonstream_call.sleep.assert_awaited_once_with(0)


def test_studio_read_timeout_updates_for_new_requests(nonstream_call, isolated_state):
    nonstream_call.client.post.side_effect = httpx.ReadTimeout("")

    for seconds in [180, 900, 30]:
        isolated_state.update_settings({"studio_read_timeout_seconds": seconds})
        assert nonstream_call.call().status_code == 504
        timeout = nonstream_call.client_factory.call_args.kwargs["timeout"]
        assert timeout.read == seconds
        assert (timeout.connect, timeout.write, timeout.pool) == (30.0, 30.0, 10.0)


@pytest.mark.parametrize("model", ["gemini-4-argon", "gemini-3.6-flash-image"])
def test_streaming_uses_configured_studio_read_timeout(
    nonstream_call, isolated_state, monkeypatch, model,
):
    isolated_state.update_settings({"studio_read_timeout_seconds": 900})
    nonstream_call.request.model = model
    nonstream_call.request.stream = True

    async def stream_events(*args, **kwargs):
        yield "text", "hello"

    monkeypatch.setattr(cookie_proxy, "_execute_stream_request_generator", stream_events)
    # 生图假流式走完整响应收集；用普通 HTTP 错误尽快结束，检查它收到的客户端配置。
    nonstream_call.client.post.return_value = httpx.Response(418, text="test finished")

    async def consume():
        response = await cookie_proxy.CookieProxyUpstream().chat_completions(
            nonstream_call.request, nonstream_call.fastapi_request,
        )
        return [frame async for frame in response.body_iterator]

    frames = asyncio.run(consume())

    assert frames[-1] == "data: [DONE]\n\n"
    assert nonstream_call.client_factory.call_args.kwargs["timeout"].read == 900
