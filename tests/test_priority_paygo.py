# -*- coding: utf-8 -*-
"""Express 通道 Priority PayGo 请求头的条件注入。"""
import asyncio
import inspect

import httpx
from google import genai
from google.genai import types

import config as app_config
from api_helpers import execute_gemini_call
from models import OpenAIRequest
from http_options import (
    PRIORITY_PAYGO_HEADERS,
    get_http_options,
    should_use_priority_paygo,
)
from upstreams.express_sdk import ExpressSDKUpstream


def _clear_http_overrides(monkeypatch):
    monkeypatch.setattr(app_config, "PROXY_URL", None, raising=False)
    monkeypatch.setattr(app_config, "SSL_CERT_FILE", None, raising=False)
    monkeypatch.setattr(app_config, "VERTEX_BASE_URL", None, raising=False)


def test_global_model_path_enables_priority_paygo():
    path = "projects/proj/locations/global/publishers/google/models/gemini-example"
    assert should_use_priority_paygo(path) is True


def test_non_global_or_unpinned_path_uses_standard_request():
    paths = [
        "gemini-example",
        "projects/proj/locations/us-central1/publishers/google/models/gemini-example",
        "projects/proj/locations/asia-east1/publishers/google/models/gemini-example",
        "",
    ]
    assert all(should_use_priority_paygo(path) is False for path in paths)


def test_priority_http_options_contain_exact_headers(monkeypatch):
    _clear_http_overrides(monkeypatch)
    options = get_http_options(priority_paygo=True)
    assert options is not None
    assert options.headers == PRIORITY_PAYGO_HEADERS
    assert options.headers == {
        "X-Vertex-AI-LLM-Request-Type": "shared",
        "X-Vertex-AI-LLM-Shared-Request-Type": "priority",
    }


def test_standard_http_options_do_not_inject_headers(monkeypatch):
    _clear_http_overrides(monkeypatch)
    assert get_http_options(priority_paygo=False) is None


def test_google_sdk_sends_priority_headers_on_the_http_request(monkeypatch):
    """不只检查配置对象，还在 SDK 传输层截获一次真实构造的 HTTP 请求。"""
    _clear_http_overrides(monkeypatch)
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={
            "candidates": [{
                "content": {"role": "model", "parts": [{"text": "ok"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {
                "promptTokenCount": 1,
                "candidatesTokenCount": 1,
                "totalTokenCount": 2,
                "trafficType": "ON_DEMAND_PRIORITY",
            },
        })

    options = get_http_options(priority_paygo=True)
    options.client_args = {"transport": httpx.MockTransport(handler)}
    client = genai.Client(vertexai=True, api_key="test-key", http_options=options)
    response = client.models.generate_content(
        model="projects/test-project/locations/global/publishers/google/models/gemini-example",
        contents="test",
    )

    assert len(captured) == 1
    headers = captured[0].headers
    assert headers["X-Vertex-AI-LLM-Request-Type"] == "shared"
    assert headers["X-Vertex-AI-LLM-Shared-Request-Type"] == "priority"
    assert response.usage_metadata.traffic_type.value == "ON_DEMAND_PRIORITY"


def _response():
    return types.GenerateContentResponse.model_validate({
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": "ok"}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {
            "promptTokenCount": 1,
            "candidatesTokenCount": 1,
            "totalTokenCount": 2,
        },
    })


class _Models:
    def __init__(self, calls, *, fail=False):
        self.calls = calls
        self.fail = fail

    async def generate_content(self, *, model, contents, config):
        self.calls.append(model)
        if self.fail:
            raise RuntimeError(f"404 NOT_FOUND Publisher model {model} was not found")
        return _response()

    async def generate_content_stream(self, *, model, contents, config):
        self.calls.append(model)
        if self.fail:
            raise RuntimeError(f"404 NOT_FOUND Publisher model {model} was not found")

        async def chunks():
            if False:
                yield None

        return chunks()


class _Client:
    def __init__(self, calls, *, fail=False):
        self.aio = type("Aio", (), {"models": _Models(calls, fail=fail)})()


def _request(*, stream):
    return OpenAIRequest(
        model="gemini-example",
        messages=[{"role": "user", "content": "test"}],
        stream=stream,
    )


def test_non_streaming_fallback_switches_to_standard_client():
    priority_calls, standard_calls = [], []
    full = "projects/test/locations/global/publishers/google/models/gemini-example"

    async def run():
        await execute_gemini_call(
            _Client(priority_calls, fail=True),
            full,
            lambda messages: [],
            {},
            _request(stream=False),
            fallback_model="gemini-example",
            fallback_client_factory=lambda: _Client(standard_calls),
        )

    asyncio.run(run())
    assert priority_calls == [full]
    assert standard_calls == ["gemini-example"]


def test_streaming_fallback_switches_to_standard_client(monkeypatch):
    from runtime_state import app_state

    app_state.update_settings({"fake_streaming": False})
    priority_calls, standard_calls = [], []
    full = "projects/test/locations/global/publishers/google/models/gemini-example"

    async def run():
        response = await execute_gemini_call(
            _Client(priority_calls, fail=True),
            full,
            lambda messages: [],
            {},
            _request(stream=True),
            fallback_model="gemini-example",
            fallback_client_factory=lambda: _Client(standard_calls),
        )
        async for _ in response.body_iterator:
            pass

    asyncio.run(run())
    assert priority_calls == [full]
    assert standard_calls == ["gemini-example"]


def test_priority_headers_are_wired_into_express_client():
    source = inspect.getsource(ExpressSDKUpstream.chat_completions)
    assert "priority_paygo = should_use_priority_paygo(model_to_call)" in source
    assert "get_http_options(priority_paygo=priority_paygo)" in source
    assert "fallback_client_factory=" in source
