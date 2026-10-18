from api_errors import error_code_for_status
import asyncio
import gzip
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from models import OpenAIRequest
from upstreams import cookie_proxy as cp


NO_RESPONSE = "Server disconnected without sending a response."
INCOMPLETE = "peer closed connection without sending complete message body (incomplete chunked read)"


def data_frame(parts=None, finish=None):
    candidate = {"content": {"parts": parts or []}}
    if finish:
        candidate["finishReason"] = finish
    return json.dumps({"results": [{"data": {"candidates": [candidate]}}]}, ensure_ascii=False).encode()


TEXT = data_frame([{"text": "完整正文"}])
FINISH = data_frame(finish="STOP")
UI = b'{"results":[{"data":{"ui":{"streamGenerateContentV2":[]}}}]}'


def payloads(frames):
    return [json.loads(line[5:]) for line in "".join(frames).splitlines()
            if line.startswith("data:") and line[5:].strip() != "[DONE]"]


@pytest.fixture
def transport(monkeypatch):
    state = SimpleNamespace(plans=[], calls=0, bodies=[], clock=0.0, header_delay=0.0,
                            body_headers={"content-type": "application/json; charset=utf-8"})
    monkeypatch.setattr(cp, "_get_cookie_string", lambda: "TEST_COOKIE")
    monkeypatch.setattr(cp, "_get_project_id", lambda: "TEST_PROJECT")
    monkeypatch.setattr(cp, "build_headers", lambda _: {"cookie": "TEST_COOKIE"})
    monkeypatch.setattr(cp, "get_proxy_client_args", lambda: {"trust_env": False})
    monkeypatch.setattr(cp, "get_retry_settings", lambda: (2, 0))
    monkeypatch.setattr(cp, "build_batch_graphql_body_async", AsyncMock(return_value={"variables": {}}))
    state.success, state.errors, state.retries = Mock(), Mock(), Mock()
    monkeypatch.setattr(cp.stats, "add_success", state.success)
    monkeypatch.setattr(cp.stats, "add_error", state.errors)
    monkeypatch.setattr(cp.stats, "add_retry", state.retries)
    original_client = httpx.AsyncClient

    class Body(httpx.AsyncByteStream):
        def __init__(self, items):
            self.items = items
            self.closed = False
            state.bodies.append(self)

        async def __aiter__(self):
            for item in self.items:
                if isinstance(item, tuple):
                    delay, item = item
                    state.clock += delay
                if isinstance(item, BaseException):
                    raise item
                yield item

        async def aclose(self):
            self.closed = True

    async def handler(request):
        state.calls += 1
        state.clock += state.header_delay
        plan = state.plans.pop(0)
        if isinstance(plan, BaseException):
            raise plan
        if isinstance(plan, httpx.Response):
            return plan
        return httpx.Response(200, headers=state.body_headers, stream=Body(plan))

    def factory(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cp.httpx, "AsyncClient", factory)
    state.connection = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    state.request = OpenAIRequest(model="gemini-3.8-flash", messages=[{"role": "user", "content": "hello"}])

    async def call(stream=True, image=False):
        state.request.stream = stream
        if image:
            state.request.model = "gemini-3.6-flash-image"
        response = await cp.CookieProxyUpstream().chat_completions(state.request, state.connection)
        if not stream:
            return response
        return [frame async for frame in response.body_iterator]

    def use_clock():
        # Do not replace time.monotonic globally: asyncio uses its own clock.
        monkeypatch.setattr(cp, "time", SimpleNamespace(time=cp.time.time, monotonic=lambda: state.clock))

    state.call, state.use_clock = call, use_clock
    return state


@pytest.mark.parametrize("error", [
    httpx.RemoteProtocolError(NO_RESPONSE), httpx.RemoteProtocolError(INCOMPLETE),
    httpx.ConnectError(""), httpx.ReadError(""), httpx.WriteError(""), httpx.CloseError(""),
    httpx.ReadTimeout(""), httpx.ConnectTimeout(""),
])
def test_transient_transport_types_are_retryable_without_keywords(error):
    assert cp._is_retryable_exception(error)


@pytest.mark.parametrize("error", [
    httpx.LocalProtocolError("invalid header"), httpx.UnsupportedProtocol("invalid URL"),
    httpx.ProxyError("407 proxy authentication required"),
    httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"),
    httpx.ConnectError("certificate verify failed"),
])
def test_configuration_errors_are_terminal(error):
    assert not cp._is_retryable_exception(error)


@pytest.mark.parametrize("error", [httpx.RemoteProtocolError(NO_RESPONSE), httpx.ReadError("")])
def test_disconnect_before_headers_retries_and_succeeds(transport, error, capsys):
    transport.plans = [error, [TEXT, FINISH]]
    frames = asyncio.run(transport.call())
    result = payloads(frames)
    assert transport.calls == 2
    assert not any("error" in p for p in result)
    assert "".join(c["delta"].get("content", "") for p in result for c in p.get("choices", [])) == "完整正文"
    transport.retries.assert_called_once()
    transport.success.assert_called_once()
    transport.errors.assert_not_called()
    assert all(body.closed for body in transport.bodies)
    assert "阶段=等待响应头" in capsys.readouterr().out


@pytest.mark.parametrize("prefix", [UI, b'{"results":', data_frame([{"thoughtSignature": "SIGNATURE_ONLY"}])])
def test_body_disconnect_before_effective_content_can_retry(transport, prefix, capsys):
    transport.plans = [[prefix, httpx.RemoteProtocolError(INCOMPLETE)], [TEXT, FINISH]]
    result = payloads(asyncio.run(transport.call()))
    assert transport.calls == 2
    assert not any("error" in p for p in result)
    transport.retries.assert_called_once()
    logs = capsys.readouterr().out
    assert "阶段=读取响应体" in logs
    assert logs.count("首字耗时") == 1
    assert all(body.closed for body in transport.bodies)


@pytest.mark.parametrize("part", [
    {"text": "部分正文"}, {"text": "部分思考", "thought": True},
    {"functionCall": {"name": "get_time", "args": {}}},
])
def test_disconnect_after_visible_output_keeps_partial_data_without_replay(transport, part, capsys):
    transport.plans = [[data_frame([part]), httpx.RemoteProtocolError(INCOMPLETE)]]
    frames = asyncio.run(transport.call())
    result = payloads(frames)
    assert transport.calls == 1
    assert result[-1]["error"]["code"] == error_code_for_status(502)
    deltas = [c["delta"] for p in result for c in p.get("choices", [])]
    if "functionCall" in part:
        assert any(d.get("tool_calls") for d in deltas)
    else:
        field = "reasoning_content" if part.get("thought") else "content"
        assert "".join(d.get(field, "") for d in deltas) == part["text"]
    assert not any(c.get("finish_reason") for p in result for c in p.get("choices", []))
    assert "".join(frames).count("data: [DONE]") == 1
    transport.retries.assert_not_called()
    transport.success.assert_not_called()
    transport.errors.assert_called_once()
    assert "上游输出中断" in capsys.readouterr().out
    assert all(body.closed for body in transport.bodies)


@pytest.mark.parametrize("limit", [0, 1, 2])
def test_transport_retry_budget_is_bounded(transport, monkeypatch, limit):
    monkeypatch.setattr(cp, "get_retry_settings", lambda: (limit, 0))
    transport.plans = [httpx.RemoteProtocolError(NO_RESPONSE) for _ in range(limit + 1)]
    result = payloads(asyncio.run(transport.call()))
    assert transport.calls == limit + 1
    assert transport.retries.call_count == limit
    assert result[-1]["error"]["code"] == error_code_for_status(502)
    transport.success.assert_not_called()
    transport.errors.assert_called_once()


@pytest.mark.parametrize("error", [httpx.LocalProtocolError("bad"), httpx.ConnectError("certificate verify failed")])
def test_terminal_transport_errors_are_not_resent(transport, error):
    transport.plans = [error]
    assert payloads(asyncio.run(transport.call()))[-1]["error"]["code"] == error_code_for_status(502)
    assert transport.calls == 1
    transport.retries.assert_not_called()


@pytest.mark.parametrize("status", [400, 401, 403])
def test_http_validation_and_auth_failures_do_not_retry(transport, status):
    transport.plans = [httpx.Response(status, json={"error": {"message": "invalid credentials"}})]
    assert payloads(asyncio.run(transport.call()))[-1]["error"]["code"] == error_code_for_status(status)
    assert transport.calls == 1
    transport.retries.assert_not_called()


@pytest.mark.parametrize("stream,image", [(False, False), (True, True)])
@pytest.mark.parametrize("mid_body", [False, True])
def test_buffered_channels_retry_transport_failures_before_returning(transport, stream, image, mid_body):
    failure = httpx.RemoteProtocolError(INCOMPLETE if mid_body else NO_RESPONSE)
    transport.plans = [[UI, failure] if mid_body else failure, [TEXT, FINISH]]
    result = asyncio.run(transport.call(stream=stream, image=image))
    if stream:
        assert not any("error" in p for p in payloads(result))
    else:
        assert result.status_code == 200
        assert json.loads(result.body)["choices"][0]["message"]["content"] == "完整正文"
    assert transport.calls == 2
    transport.retries.assert_called_once()
    transport.success.assert_called_once()


def test_client_disconnect_stops_retry(transport):
    transport.plans = [httpx.RemoteProtocolError(NO_RESPONSE)]
    transport.connection.is_disconnected.side_effect = [False, True]
    frames = asyncio.run(transport.call())
    assert transport.calls == 1
    transport.retries.assert_not_called()
    assert not payloads(frames)


def test_cancellation_is_not_converted_to_retry_or_success(transport):
    transport.plans = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(transport.call())
    assert transport.calls == 1
    transport.retries.assert_not_called()
    transport.success.assert_not_called()


def test_only_first_packet_and_first_effective_output_are_timed(transport, capsys):
    transport.use_clock()
    transport.header_delay = 2
    transport.plans = [[(1, UI), (3, data_frame([{"text": "思考", "thought": True}])), (5, TEXT), (2, FINISH)]]
    asyncio.run(transport.call())
    logs = capsys.readouterr().out
    assert "首包耗时 3.00s" in logs
    assert "首字耗时 6.00s（思考）" in logs
    assert "总耗时 13.00s" in logs
    assert logs.count("首包耗时") == 1
    assert logs.count("首字耗时") == 1
    assert "正文首字" not in logs
    assert "首段耗时" not in logs
    assert "本次等待" not in logs
    assert f"本次接收={len(UI) + len(data_frame([{'text': '思考', 'thought': True}])) + len(TEXT) + len(FINISH)}B/4JSON" in logs
    assert "思考=有 | 正文=有" in logs


def test_total_timing_includes_prior_attempt_and_backoff(transport, monkeypatch, capsys):
    transport.use_clock()
    transport.header_delay = 2
    monkeypatch.setattr(cp, "get_retry_settings", lambda: (1, 7))

    async def backoff(seconds):
        transport.clock += seconds
        yield cp._sse_heartbeat()

    monkeypatch.setattr(cp, "_sleep_with_heartbeat", backoff)
    transport.plans = [httpx.RemoteProtocolError(NO_RESPONSE), [(3, TEXT), (2, FINISH)]]
    asyncio.run(transport.call())
    logs = capsys.readouterr().out
    assert "首包耗时 14.00s" in logs
    assert "首字耗时 14.00s（正文）" in logs
    assert "总耗时 16.00s" in logs
    assert "上游请求=2" in logs


def test_first_packet_precedes_gzip_decoding_and_complete_tool_event(transport, capsys):
    transport.use_clock()
    transport.header_delay = 2
    transport.body_headers["content-encoding"] = "gzip"
    body = gzip.compress(data_frame([{"functionCall": {"name": "emit_reply", "args": {"content": "长篇内容" * 100}}}]) + FINISH)
    transport.plans = [[(1, body[:10]), (5, body[10:])]]
    result = payloads(asyncio.run(transport.call()))
    logs = capsys.readouterr().out
    assert "首包耗时 3.00s" in logs
    assert "首字耗时 8.00s（工具调用）" in logs
    assert "首包=3.00s | 首字=8.00s" in logs
    assert logs.count("首包耗时") == 1
    assert logs.count("首字耗时") == 1
    assert not any("error" in item for item in result)
    assert all(body.closed for body in transport.bodies)


def test_first_packet_does_not_depend_on_ui_metadata(transport, capsys):
    transport.use_clock()
    transport.header_delay = 2
    transport.plans = [[(1, TEXT[:10]), (5, TEXT[10:]), FINISH]]
    asyncio.run(transport.call())
    logs = capsys.readouterr().out
    assert "首包耗时 3.00s" in logs
    assert "首字耗时 8.00s（正文）" in logs


def test_first_packet_is_not_reset_after_a_retry(transport, monkeypatch, capsys):
    transport.use_clock()
    transport.header_delay = 2
    monkeypatch.setattr(cp, "get_retry_settings", lambda: (1, 7))

    async def backoff(seconds):
        transport.clock += seconds
        yield cp._sse_heartbeat()

    monkeypatch.setattr(cp, "_sleep_with_heartbeat", backoff)
    transport.plans = [[(1, UI), httpx.RemoteProtocolError(INCOMPLETE)], [(3, TEXT), (2, FINISH)]]
    asyncio.run(transport.call())
    logs = capsys.readouterr().out
    assert "首包耗时 3.00s" in logs
    assert "首字耗时 15.00s（正文）" in logs
    assert "总耗时 17.00s" in logs
    assert logs.count("首包耗时") == 1
    assert logs.count("首字耗时") == 1


def test_header_only_failures_do_not_claim_a_first_packet(transport, monkeypatch, capsys):
    monkeypatch.setattr(cp, "get_retry_settings", lambda: (0, 0))
    transport.plans = [[httpx.RemoteProtocolError(INCOMPLETE)]]
    asyncio.run(transport.call())
    logs = capsys.readouterr().out
    assert "响应头=已收" in logs
    assert "首包=未到达 | 首字=未到达" in logs
    assert "首包耗时" not in logs
    assert "首字耗时" not in logs


def test_transport_diagnostics_do_not_log_private_exception_details(transport, capsys):
    transport.plans = [httpx.RemoteProtocolError("https://example.test/?key=PRIVATE_KEY Cookie=PRIVATE_COOKIE"), [TEXT, FINISH]]
    asyncio.run(transport.call())
    logs = capsys.readouterr().out
    assert "异常=RemoteProtocolError" in logs
    assert "PRIVATE_KEY" not in logs
    assert "PRIVATE_COOKIE" not in logs
