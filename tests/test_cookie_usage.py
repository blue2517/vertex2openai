import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import token_counting as tc
from logger import ProxyStats
from models import OpenAIRequest
from upstreams import cookie_proxy as cp


def test_tokenizer_is_bundled_verified_and_does_not_count_characters():
    assert tc.TOKENIZER_PATH.is_file()
    assert tc.count_text("hello") == 1
    assert tc.count_text("hello world") == 2
    assert tc.count_text("") == 0
    assert tc.count_text("hello\r\nworld") != tc.count_text("hello\nworld")


def test_corrupted_bundled_vocabulary_is_rejected(monkeypatch, tmp_path):
    invalid = tmp_path / "invalid.model"
    invalid.write_bytes(b"invalid tokenizer")
    monkeypatch.setattr(tc, "_processor", None)
    monkeypatch.setattr(tc, "TOKENIZER_PATH", invalid)
    with pytest.raises(RuntimeError, match="checksum"):
        tc.warm_tokenizer()


def test_stream_fragments_are_joined_before_tokenization():
    whole, fragmented = tc.CookieUsageCounter(), tc.CookieUsageCounter()
    whole.observe("text", "hello world")
    for char in "hello world":
        fragmented.observe("text", char)
    assert whole.resolve({}) == fragmented.resolve({})
    assert fragmented.resolve({})["usage"]["completion_tokens"] == 2


def test_function_boundaries_do_not_merge_nonadjacent_text():
    counter = tc.CookieUsageCounter()
    counter.observe("text", "hel")
    counter.observe("function_call", {"name": "run", "args": {}})
    counter.observe("text", "lo")
    assert counter.resolve({})["usage"]["completion_tokens"] == tc.count_texts(["hel", "run", "lo"])


def test_function_arguments_include_nested_values_keys_numbers_and_booleans():
    counter = tc.CookieUsageCounter()
    counter.observe("function_call", {"name": "emit", "args": {
        "content": "hello world" * 100, "nested": [{"flag": True, "number": 42, "missing": None}],
    }, "thought_signature": "SECRET_SIGNATURE_NOT_A_TOKEN"})
    expected = tc.count_texts(["emit", "content", "hello world" * 100, "nested", "flag", "true", "number", "42", "missing", "null"])
    assert counter.resolve({})["usage"]["completion_tokens"] == expected


def test_prompt_counts_system_history_tools_schema_and_tool_results():
    body = {"requestContext": {"private": "TRANSPORT_CONTEXT"}, "variables": {
        "systemInstruction": {"parts": [{"text": "system"}]},
        "contents": [
            {"role": "user", "parts": [{"text": "hello"}]},
            {"role": "model", "parts": [{"functionCall": {"name": "emit", "args": {"content": "prior"}}, "thoughtSignature": "HIDDEN_SIGNATURE"}]},
            {"role": "user", "parts": [{"functionResponse": {"name": "emit", "response": {"status": "done"}}}]},
        ],
        "tools": [{"functionDeclarations": [{"name": "emit", "description": "emit content", "parameters": {
            "type": "OBJECT", "properties": [{"key": "content", "value": {"type": "STRING", "description": "reply text"}}],
            "required": ["content"],
        }}]}],
    }}
    texts, media = tc.prompt_texts(body)
    assert not media
    assert texts == ["hello", "emit", "content", "prior", "emit", "status", "done", "system", "emit", "emit content", "content", "content", "reply text"]
    assert "HIDDEN_SIGNATURE" not in texts
    assert "TRANSPORT_CONTEXT" not in texts
    counter = tc.CookieUsageCounter()
    counter.observe("text", "done")
    assert counter.resolve(body)["usage"]["prompt_tokens"] == tc.count_texts(texts)


def test_standard_and_studio_schema_shapes_count_identically():
    schema = {"type": "object", "properties": {"nested": {"type": "array", "items": {
        "type": "object", "properties": {"value": {"type": "string", "enum": ["a", "b"]}},
    }}}, "required": ["nested"]}
    def count(s):
        return tc.CookieUsageCounter().resolve({"variables": {"tools": [{"functionDeclarations": [{"name": "tool", "parameters": s}]}]}})["usage"]["prompt_tokens"]
    assert count(schema) == count(cp._cookie_ui_schema(schema))


def test_local_reasoning_is_in_completion_and_reported_separately():
    counter = tc.CookieUsageCounter()
    counter.observe("thought", "reason")
    counter.observe("thought", "ing")
    counter.observe("text", "hello")
    result = counter.resolve({})
    assert result["usage"]["completion_tokens"] == tc.count_texts(["reasoning", "hello"])
    assert result["usage"]["completion_tokens_details"]["reasoning_tokens"] == tc.count_text("reasoning")
    assert result["usage_metadata"]["source"] == "local_tokenizer"
    assert result["usage_metadata"]["estimated"] is True


@pytest.mark.parametrize("metadata", [None, {}, {"promptTokenCount": 0, "candidatesTokenCount": 0, "totalTokenCount": 0}, {"promptTokenCount": -1, "candidatesTokenCount": "bad"}])
def test_absent_zero_or_invalid_usage_is_locally_computed(metadata):
    counter = tc.CookieUsageCounter()
    counter.observe("text", "hello")
    result = counter.resolve({"variables": {"contents": [{"parts": [{"text": "hi"}]}]}}, metadata)
    assert result["usage"] == {"prompt_tokens": tc.count_text("hi"), "completion_tokens": 1, "total_tokens": tc.count_text("hi") + 1}
    assert result["usage_metadata"]["source"] == "local_tokenizer"


def test_complete_upstream_counts_have_priority_without_local_counting(monkeypatch):
    monkeypatch.setattr(tc, "count_texts", lambda *_: pytest.fail("unexpected local counting"))
    result = tc.CookieUsageCounter().resolve({}, {"promptTokenCount": 100, "candidatesTokenCount": 20, "thoughtsTokenCount": 5, "totalTokenCount": 125, "cachedContentTokenCount": 40})
    assert result["usage"] == {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125,
                               "completion_tokens_details": {"reasoning_tokens": 5}, "prompt_tokens_details": {"cached_tokens": 40}}
    assert result["usage_metadata"] == {"source": "upstream", "estimated": False}


@pytest.mark.parametrize("metadata,expected", [
    ({"promptTokenCount": 10}, {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}),
    ({"candidatesTokenCount": 20}, {"prompt_tokens": 0, "completion_tokens": 20, "total_tokens": 20}),
    ({"promptTokenCount": 10, "totalTokenCount": 30}, {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}),
    ({"totalTokenCount": 30, "candidatesTokenCount": 20}, {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}),
])
def test_partial_upstream_metadata_is_completed_consistently(metadata, expected):
    counter = tc.CookieUsageCounter()
    counter.observe("text", "hello")
    assert counter.resolve({}, metadata)["usage"] == expected


def test_media_base64_signatures_ui_and_safety_markup_are_not_counted_as_text():
    counter = tc.CookieUsageCounter()
    counter.observe("text", "hello")
    counter.observe("image", "![image](data:image/png;base64," + "AAAA" * 10000 + ")")
    counter.observe("thought_signature", "SIGNATURE" * 1000)
    counter.observe("safety", [{"category": "HARM_CATEGORY_HATE_SPEECH", "probability": "LOW"}])
    result = counter.resolve({"variables": {"contents": [{"parts": [{"inlineData": {"mimeType": "image/png", "data": "AAAA" * 10000}}]}]}})
    assert result["usage"] == {"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1}
    assert result["usage_metadata"]["contains_media"] is True
    assert result["usage_metadata"]["media_tokens_included"] is False


@pytest.fixture
def cookie_api(monkeypatch):
    monkeypatch.setattr(cp, "_get_cookie_string", lambda: "TEST_COOKIE")
    monkeypatch.setattr(cp, "_get_project_id", lambda: "TEST_PROJECT")
    monkeypatch.setattr(cp, "build_headers", lambda _: {"cookie": "TEST_COOKIE"})
    monkeypatch.setattr(cp, "get_proxy_client_args", lambda: {"trust_env": False})
    monkeypatch.setattr(cp, "get_retry_settings", lambda: (1, 0))
    stats = ProxyStats()
    monkeypatch.setattr(cp, "stats", stats)
    state = SimpleNamespace(objects=[], calls=0, sent=[], fail_native=False, stats=stats)
    original = httpx.AsyncClient

    async def handler(request):
        state.calls += 1
        state.sent.append(json.loads(request.content))
        if state.fail_native and state.calls == 1:
            return httpx.Response(400, text='Unsupported function declaration in variables.tools')
        return httpx.Response(200, text="\n".join(json.dumps(obj, ensure_ascii=False) for obj in state.objects))

    def factory(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cp.httpx, "AsyncClient", factory)
    state.request = OpenAIRequest(model="gemini-3.8-flash", messages=[{"role": "user", "content": "hello"}],
                                  stream_options={"include_usage": True})

    async def call(stream):
        state.request.stream = stream
        response = await cp.CookieProxyUpstream().chat_completions(state.request, SimpleNamespace(is_disconnected=AsyncMock(return_value=False)))
        if not stream:
            assert response.status_code == 200
            return [json.loads(response.body)]
        frames = "".join([frame async for frame in response.body_iterator])
        return [json.loads(line[5:]) for line in frames.splitlines() if line.startswith("data:") and line[5:].strip() != "[DONE]"]

    state.call = call
    return state


def response(parts, usage=None):
    data = {"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]}
    if usage is not None:
        data["usageMetadata"] = usage
    return {"results": [{"data": data}]}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tool_only", [False, True])
def test_cookie_api_reports_nonzero_usage_for_text_and_tool_only_output(cookie_api, stream, tool_only):
    call = {"name": "emit", "args": {"content": "hello world" * 200}}
    parts = [{"functionCall": call}] if tool_only else [{"text": "hello world" * 200}]
    cookie_api.objects = [response(parts)]
    result = asyncio.run(cookie_api.call(stream))
    final = next(item for item in result if "usage" in item)
    expected = tc.CookieUsageCounter()
    expected.observe("function_call" if tool_only else "text", call if tool_only else parts[0]["text"])
    assert final["usage"] == expected.resolve(cookie_api.sent[-1])["usage"]
    assert final["usage"]["completion_tokens"] > 200
    assert final["usage_metadata"]["source"] == "local_tokenizer"
    stats = cookie_api.stats.get_json_stats()
    assert stats["success"] == 1
    assert stats["prompt_tokens"] == final["usage"]["prompt_tokens"]
    assert stats["completion_tokens"] == final["usage"]["completion_tokens"]


def test_stream_usage_is_opt_in_but_local_stats_still_count(cookie_api):
    cookie_api.request.stream_options = {"include_usage": False}
    cookie_api.objects = [response([{"text": "hello world"}])]
    assert not any("usage" in item for item in asyncio.run(cookie_api.call(True)))
    assert cookie_api.stats.get_json_stats()["completion_tokens"] == 2


@pytest.mark.parametrize("stream", [False, True])
def test_usage_fields_from_separate_upstream_events_are_merged(cookie_api, stream):
    cookie_api.objects = [response([{"text": "hel"}], {"promptTokenCount": 123}),
                          response([{"text": "lo"}], {"candidatesTokenCount": 45, "totalTokenCount": 168})]
    final = next(item for item in asyncio.run(cookie_api.call(stream)) if "usage" in item)
    assert final["usage"] == {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168}
    assert final["usage_metadata"]["source"] == "upstream"


def test_stream_text_split_across_events_does_not_overcount(cookie_api):
    cookie_api.objects = [response([{"text": char}]) for char in "hello"]
    final = next(item for item in asyncio.run(cookie_api.call(True)) if "usage" in item)
    assert final["usage"]["completion_tokens"] == 1


def test_failed_response_does_not_record_a_successful_usage(cookie_api):
    cookie_api.objects = [{"results": [{"errors": [{"code": 7, "message": "permission denied"}]}]}]
    result = asyncio.run(cookie_api.call(True))
    assert any("error" in item for item in result)
    assert not any("usage" in item for item in result)
    assert cookie_api.stats.get_json_stats()["success"] == 0
    assert cookie_api.stats.get_json_stats()["prompt_tokens"] == 0
    assert cookie_api.stats.get_json_stats()["completion_tokens"] == 0


@pytest.mark.parametrize("stream", [False, True])
def test_tool_none_does_not_charge_unsent_declarations(cookie_api, stream):
    cookie_api.request.tools = [{"type": "function", "function": {"name": "unused", "description": "large " * 1000, "parameters": {"type": "object"}}}]
    cookie_api.request.tool_choice = "none"
    cookie_api.objects = [response([{"text": "hello"}])]
    final = next(item for item in asyncio.run(cookie_api.call(stream)) if "usage" in item)
    assert "tools" not in cookie_api.sent[-1]["variables"]
    assert final["usage"]["prompt_tokens"] == tc.count_text("hello")


@pytest.mark.parametrize("stream", [False, True])
def test_counting_uses_actual_native_tool_fallback_payload(cookie_api, stream):
    cookie_api.request.tools = [{"type": "function", "function": {"name": "tool", "description": "large " * 1000, "parameters": {"type": "object"}}}]
    cookie_api.fail_native = True
    cookie_api.objects = [response([{"text": "hello"}])]
    final = next(item for item in asyncio.run(cookie_api.call(stream)) if "usage" in item)
    assert cookie_api.calls == 2
    assert "tools" in cookie_api.sent[0]["variables"]
    assert "tools" not in cookie_api.sent[-1]["variables"]
    assert final["usage"]["prompt_tokens"] == tc.count_text("hello")
    assert cookie_api.stats.get_json_stats()["success"] == 1


@pytest.mark.parametrize("stream", [False, True])
def test_image_channels_count_text_not_base64_and_mark_partial(cookie_api, stream):
    cookie_api.request.model = "gemini-3.6-flash-image"
    cookie_api.objects = [response([{"text": "hello world"}, {"inlineData": {"mimeType": "image/png", "data": "AAAA" * 1000}}])]
    final = next(item for item in asyncio.run(cookie_api.call(stream)) if "usage" in item)
    assert final["usage"]["completion_tokens"] == 2
    assert final["usage_metadata"]["contains_media"] is True
    assert final["usage_metadata"]["media_tokens_included"] is False
    assert cookie_api.stats.get_json_stats()["success"] == 1


@pytest.mark.parametrize("stream", [False, True])
def test_api_passes_upstream_usage_through_with_priority(cookie_api, stream):
    cookie_api.objects = [response([{"text": "hello"}], {"promptTokenCount": 123, "candidatesTokenCount": 45, "totalTokenCount": 168})]
    final = next(item for item in asyncio.run(cookie_api.call(stream)) if "usage" in item)
    assert final["usage"] == {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168}
    assert final["usage_metadata"] == {"source": "upstream", "estimated": False}


def test_counter_statistics_do_not_double_count_successes():
    stats = ProxyStats()
    stats.add_success()
    stats.add_tokens(10, 20, count_success=False)
    stats.add_tokens(30, 40)
    assert stats.get_json_stats()["success"] == 2
    assert stats.get_json_stats()["prompt_tokens"] == 40
    assert stats.get_json_stats()["completion_tokens"] == 60
