import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google import genai
from google.genai import types

import api_helpers
from models import OpenAIRequest
from upstreams import express_sdk


def _sse_body(*chunks: dict) -> list[bytes]:
    frames = []
    for chunk in chunks:
        frames.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
    return frames


def _json_body(parts: list[dict], finish_reason: str = "STOP") -> bytes:
    payload = {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish_reason}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3, "totalTokenCount": 8},
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _sse_payloads(frames: list) -> list[dict]:
    out = []
    for frame in frames:
        text = frame.decode("utf-8") if isinstance(frame, bytes) else str(frame)
        for line in text.splitlines():
            if line.startswith("data:") and line[5:].strip() != "[DONE]":
                out.append(json.loads(line[5:]))
    return out


@pytest.fixture
def express_transport(monkeypatch, isolated_state):
    state = SimpleNamespace(plans=[], calls=0, clock=0.0, header_delay=0.0)
    monkeypatch.setattr(api_helpers, "get_retry_settings", lambda: (2, 0))
    monkeypatch.setattr(api_helpers, "time", SimpleNamespace(time=api_helpers.time.time, monotonic=lambda: state.clock))
    monkeypatch.setattr(express_sdk, "time", SimpleNamespace(time=express_sdk.time.time, monotonic=lambda: state.clock))

    class Body(httpx.AsyncByteStream):
        def __init__(self, items):
            self.items = items

        async def __aiter__(self):
            for item in self.items:
                if isinstance(item, tuple):
                    delay, item = item
                    state.clock += delay
                if isinstance(item, BaseException):
                    raise item
                yield item

    async def handler(request):
        state.calls += 1
        state.clock += state.header_delay
        plan = state.plans.pop(0)
        if isinstance(plan, BaseException):
            raise plan
        if isinstance(plan, httpx.Response):
            return plan
        if isinstance(plan, bytes):
            return httpx.Response(200, headers={"content-type": "application/json"}, content=plan)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body(plan))

    def build_client():
        async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return genai.Client(
            vertexai=True,
            api_key="TEST_KEY",
            http_options=types.HttpOptions(httpx_async_client=async_client),
        )

    async def call(stream=True, fake=False, note="", job_id=None):
        isolated_state.update_settings({"fake_streaming": fake, "fake_streaming_interval": 0})
        monkeypatch.setattr(
            express_sdk,
            "get_http_options",
            lambda: types.HttpOptions(httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))),
        )
        manager = SimpleNamespace(
            get_total_keys=lambda: 1,
            get_express_record=lambda: {"key": "TEST_KEY", "project_id": "", "note": note},
        )
        conn = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(express_key_manager=manager)),
            is_disconnected=AsyncMock(return_value=False),
            job=(SimpleNamespace(id=job_id) if job_id else None),
        )
        req = OpenAIRequest(model="gemini-3.8-flash", stream=stream, messages=[{"role": "user", "content": "hello"}])
        response = await express_sdk.ExpressSDKUpstream().chat_completions(req, conn)
        if not stream:
            return response
        return [frame async for frame in response.body_iterator]

    state.build_client = build_client
    state.call = call
    return state


def test_express_real_stream_logs_first_packet_first_token_and_summary(express_transport, capsys):
    express_transport.header_delay = 2.0
    thought_chunk = {"candidates": [{"content": {"parts": [{"text": "思考中", "thought": True}]}}]}
    text_chunk = {"candidates": [{"content": {"parts": [{"text": "正文"}]}, "finishReason": "STOP"}]}
    empty_chunk = {"candidates": [{"content": {"parts": [{"text": ""}]}}]}
    raw_frames = _sse_body(empty_chunk, thought_chunk, text_chunk)
    express_transport.plans = [[(1.0, raw_frames[0]), (3.0, raw_frames[1]), (4.0, raw_frames[2])]]

    frames = asyncio.run(express_transport.call(stream=True, fake=False, note="主账号", job_id="job-1"))
    logs = capsys.readouterr().out

    assert "→ [Express] gemini-3.8-flash | 请求=" in logs
    assert "凭证=主账号 | 任务=job-1 | 1 条消息 | 流式" in logs
    assert "📦 [Express]" in logs and "首包耗时 3.00s" in logs
    assert "⚡ [Express]" in logs and "首字耗时 6.00s（思考）" in logs
    assert "流式传输顺利完毕" in logs
    assert "思考=有 | 正文=有 | 工具=无" in logs
    assert "首包=3.00s | 首字=6.00s | 总耗时 10.00s" in logs
    assert logs.count("首包耗时") == 1
    assert logs.count("首字耗时") == 1
    content = "".join(c["delta"].get("content") or "" for p in _sse_payloads(frames) for c in p.get("choices", []))
    assert content == "正文"


def test_express_fake_stream_logs_status_and_first_token(express_transport, capsys):
    express_transport.header_delay = 1.5
    body = _json_body([{"text": "假流式正文"}])
    express_transport.plans = [[(2.5, body)]]

    frames = asyncio.run(express_transport.call(stream=True, fake=True))
    logs = capsys.readouterr().out

    assert "→ [Express]" in logs and "| 假流式" in logs
    assert "首包耗时 4.00s" in logs
    assert "首字耗时 4.00s（正文）" in logs
    assert "假流式完成" in logs
    assert "正文=有" in logs
    assert "总耗时 4.00s" in logs
    content = "".join(c["delta"].get("content") or "" for p in _sse_payloads(frames) for c in p.get("choices", []))
    assert content == "假流式正文"


def test_express_nonstream_logs_status_and_timing(express_transport, capsys):
    express_transport.header_delay = 2.0
    body = _json_body([{"text": "思考", "thought": True}, {"text": "非流式回答"}])
    express_transport.plans = [[(3.0, body)]]

    response = asyncio.run(express_transport.call(stream=False))
    logs = capsys.readouterr().out

    assert response.status_code == 200
    assert json.loads(response.body)["choices"][0]["message"]["content"] == "非流式回答"
    assert "→ [Express]" in logs and "| 非流式" in logs
    assert "首包耗时 5.00s" in logs
    assert "首字耗时 5.00s（思考）" in logs
    assert "非流式完成" in logs
    assert "响应头=已收" in logs
    assert "思考=有 | 正文=有" in logs
    assert "总耗时 5.00s" in logs


def test_express_retry_preserves_first_packet_and_sanitizes_transport_errors(express_transport, capsys):
    express_transport.header_delay = 1.0
    express_transport.plans = [
        httpx.Response(429, json={"error": {"code": 429, "message": "quota exceeded"}}),
        [(2.0, _sse_body({"candidates": [{"content": {"parts": [{"text": "恢复"}]}}]})[0])],
    ]

    frames = asyncio.run(express_transport.call(stream=True, fake=False))
    logs = capsys.readouterr().out

    content = "".join(c["delta"].get("content") or "" for p in _sse_payloads(frames) for c in p.get("choices", []))
    assert content == "恢复"
    assert "首段输出前暂时失败" in logs
    assert "上游请求=2" in logs
    assert logs.count("首包耗时") == 1
    assert "首包耗时 4.00s" in logs
    assert "首字耗时 4.00s（正文）" in logs


def test_express_transport_failure_reports_stage_without_leaking_secrets(express_transport, capsys):
    trace = api_helpers.UpstreamTrace("Express", "gemini-3.8-flash", "req1234", 0.0, unit_label="块", monotonic=lambda: 1.5)
    trace.begin_attempt()
    trace.transport_failure(httpx.RemoteProtocolError("https://example.test/?key=SECRET_API_KEY"))
    logs = capsys.readouterr().out
    assert "异常=RemoteProtocolError" in logs
    assert "阶段=等待响应头" in logs
    assert "SECRET_API_KEY" not in logs

    trace.received_headers()
    trace.transport_failure(httpx.ReadTimeout("timeout"))
    logs = capsys.readouterr().out
    assert "阶段=读取响应体" in logs


def test_attach_express_trace_wraps_default_httpx_transport_and_mounts(capsys):
    clock = 0.0
    trace = api_helpers.UpstreamTrace(
        "Express", "gemini-3.8-flash", "req9999", 0.0, unit_label="块", monotonic=lambda: clock,
    )
    proxy_options = types.HttpOptions(
        async_client_args={"trust_env": False, "proxy": "http://127.0.0.1:8080"},
    )
    client = genai.Client(vertexai=True, api_key="TEST_KEY", http_options=proxy_options)
    api_helpers.attach_express_trace(client, trace, read_timeout_seconds=42)
    httpx_client = client._api_client._async_httpx_client

    async def verify():
        target = (
            httpx_client._client_for_running_loop()
            if getattr(httpx_client, "_per_loop", False)
            else httpx_client
        )
        assert isinstance(target._transport, api_helpers._ObservingTransportMixin)
        assert all(isinstance(m, api_helpers._ObservingTransportMixin) for m in target._mounts.values())
        assert target._transport.timeout.read == 42.0
        # Idempotent if called again
        first_transport = target._transport
        api_helpers.attach_express_trace(client, trace, read_timeout_seconds=77)
        target_again = (
            httpx_client._client_for_running_loop()
            if getattr(httpx_client, "_per_loop", False)
            else httpx_client
        )
        assert target_again._transport is first_transport
        assert target_again._transport.timeout.read == 77.0
        await client.aio.aclose()

    asyncio.run(verify())


@pytest.mark.parametrize("resumable", [False, True])
def test_express_real_stream_delivers_chunks_incrementally_without_buffering(monkeypatch, isolated_state, resumable):
    import auth
    import main
    from routes import chat_api
    from resumable import ResumableStore

    isolated_state.update_settings({"fake_streaming": False, "resumable_enabled": resumable})
    monkeypatch.setattr(auth, "API_KEY", "test-key")
    monkeypatch.setattr(chat_api, "resumable_store", ResumableStore())
    monkeypatch.setattr(
        main.app.state,
        "express_key_manager",
        SimpleNamespace(get_total_keys=lambda: 1, get_express_record=lambda: {"key": "TEST_KEY", "project_id": ""}),
        raising=False,
    )

    async def scenario():
        first_sent = asyncio.Event()
        allow_second = asyncio.Event()
        events = []

        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield _sse_body({"candidates": [{"content": {"parts": [{"text": "first "}]}}]})[0]
                await asyncio.wait_for(allow_second.wait(), timeout=2.0)
                yield _sse_body({"candidates": [{"content": {"parts": [{"text": "second"}]}, "finishReason": "STOP"}]})[0]

        async def handler(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SlowStream())

        monkeypatch.setattr(
            express_sdk,
            "get_http_options",
            lambda: types.HttpOptions(httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))),
        )

        body = json.dumps({
            "model": "gemini-3.8-flash",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode("utf-8")
        req_sent = False

        async def receive():
            nonlocal req_sent
            if not req_sent:
                req_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(10)
            return {"type": "http.disconnect"}

        async def send(msg):
            events.append(msg)
            if msg["type"] == "http.response.body" and b"first " in msg.get("body", b""):
                first_sent.set()
                allow_second.set()

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer test-key"), (b"content-type", b"application/json")],
        }
        await main.app(scope, receive, send)
        assert first_sent.is_set()
        start_msg = next(m for m in events if m["type"] == "http.response.start")
        headers = {k.lower(): v for k, v in start_msg["headers"]}
        assert headers.get(b"x-accel-buffering") == b"no"
        body_bytes = b"".join(m.get("body", b"") for m in events if m["type"] == "http.response.body")
        assert b"first " in body_bytes and b"second" in body_bytes

    asyncio.run(scenario())
