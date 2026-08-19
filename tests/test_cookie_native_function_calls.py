# -*- coding: utf-8 -*-
"""Native custom Function Calling for the Cookie/batchGraphql channel."""
import asyncio
import base64
import json
from pathlib import Path

import pytest

from models import OpenAIMessage, OpenAIRequest
from runtime_state import app_state
from signature_store import SKIP_VALIDATOR_SENTINEL, SignatureState, signature_store
import upstreams.cookie_proxy as cp

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "cookie_function_call_shapes.json").read_text()
)


def _function_tool(name="lookup_weather", parameters=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Call {name}",
            "parameters": parameters or {"type": "object", "properties": {}},
        },
    }


def _request(*, tools=None, tool_choice=None, messages=None, stream=False):
    return OpenAIRequest(
        model="gemini-3.7-flash",
        messages=messages or [OpenAIMessage(role="user", content="Use a tool")],
        tools=tools,
        tool_choice=tool_choice,
        stream=stream,
    )


def _events(shape):
    return list(cp._extract_from_results(shape))


def test_private_ui_schema_recurses_through_objects_and_arrays():
    schema = {
        "type": "object",
        "required": ["place", "days"],
        "properties": {
            "place": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City"},
                    "coords": {
                        "type": "array",
                        "items": {"type": "number"},
                    },
                },
            },
            "days": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"date": {"type": "string"}},
                },
            },
        },
    }
    body = cp._build_batch_graphql_body("example-project", "gemini-3.7-flash",
                                        _request(tools=[_function_tool(parameters=schema)]))
    params = body["variables"]["tools"][0]["functionDeclarations"][0]["parameters"]
    assert params["type"] == "OBJECT"
    assert params["required"] == ["place", "days"]
    assert [p["key"] for p in params["properties"]] == ["place", "days"]
    place = params["properties"][0]["value"]
    assert place["type"] == "OBJECT"
    assert place["properties"][1]["value"] == {
        "type": "ARRAY", "items": {"type": "NUMBER"}}
    days = params["properties"][1]["value"]
    assert days["items"]["properties"][0] == {
        "key": "date", "value": {"type": "STRING"}}


@pytest.mark.parametrize(
    "choice, expected",
    [
        (None, {"mode": "AUTO"}),
        ("auto", {"mode": "AUTO"}),
        ("none", {"mode": "NONE"}),
        ("required", {"mode": "ANY", "allowedFunctionNames": ["one", "two"]}),
        ({"type": "function", "function": {"name": "two"}},
         {"mode": "ANY", "allowedFunctionNames": ["two"]}),
    ],
)
def test_tool_choice_wire_modes(choice, expected):
    req = _request(tools=[_function_tool("one"), _function_tool("two")], tool_choice=choice)
    config = cp._build_batch_graphql_body(
        "example-project", "gemini-3.7-flash", req)["variables"]["toolConfig"]
    assert config == {"functionCallingConfig": expected}


def test_custom_functions_and_google_search_coexist():
    req = _request(tools=[_function_tool("lookup_weather"), _function_tool("google_search")])
    variables = cp._build_batch_graphql_body(
        "example-project", "gemini-3.7-flash", req)["variables"]
    assert variables["tools"][0]["functionDeclarations"][0]["name"] == "lookup_weather"
    assert variables["tools"][1] == {"googleSearch": {}}


def test_history_and_parallel_results_round_trip_with_signature_topology():
    signature_store.clear()
    encoded = base64.b64encode(b"first-signature").decode()
    messages = [
        OpenAIMessage(role="assistant", content="Calling both", reasoning_content="Need both",
                      tool_calls=[
                          {"id": "first-id", "type": "function",
                           "function": {"name": "first_tool", "arguments": '{"value":1}'},
                           "extra_content": {"google": {"thought_signature": encoded}}},
                          {"id": "second-id", "type": "function",
                           "function": {"name": "second_tool", "arguments": '{"value":2}'}},
                      ]),
        OpenAIMessage(role="tool", tool_call_id="first-id", content='{"ok":true}'),
        OpenAIMessage(role="tool", name="second_tool", tool_call_id="second-id", content="done"),
    ]
    contents, _ = cp._convert_messages_to_contents(messages)
    assert [c["role"] for c in contents] == ["model", "user"]
    model_parts = contents[0]["parts"]
    assert [p["functionCall"]["name"] for p in model_parts[:2]] == ["first_tool", "second_tool"]
    assert base64.b64decode(model_parts[0]["thoughtSignature"]) == b"first-signature"
    assert "thoughtSignature" not in model_parts[1]
    assert any(p.get("thought") is True and p.get("text") == "Need both" for p in model_parts)
    assert any(p.get("text") == "Calling both" for p in model_parts)
    responses = contents[1]["parts"]
    assert [p["functionResponse"]["name"] for p in responses] == ["first_tool", "second_tool"]
    assert all("thoughtSignature" not in p for p in responses)
    assert not any("text" in p for p in responses)


def test_missing_first_history_signature_uses_store_then_sentinel():
    signature_store.clear()
    signature_store.put("cached-id", b"cached")
    cached = OpenAIMessage(role="assistant", tool_calls=[{
        "id": "cached-id", "type": "function", "function": {"name": "one", "arguments": "{}"}}])
    part = cp._convert_messages_to_contents([cached])[0][0]["parts"][0]
    assert base64.b64decode(part["thoughtSignature"]) == b"cached"

    signature_store.clear()
    lost = cached.model_copy(update={"tool_calls": [{
        "id": "lost-id", "type": "function", "function": {"name": "one", "arguments": "{}"}}]})
    part = cp._convert_messages_to_contents([lost])[0][0]["parts"][0]
    assert base64.b64decode(part["thoughtSignature"]) == SKIP_VALIDATOR_SENTINEL


def test_single_call_and_proto_default_filtering():
    events = _events(FIXTURES["single_call"])
    calls = [data for kind, data in events if kind == "function_call"]
    assert calls == [{
        "name": "lookup_weather",
        "args": {"city": "Example City"},
        "thought_signature": "c2FuaXRpemVkLXNpZ25hdHVyZQ==",
    }]
    default_events = _events(FIXTURES["proto_defaults"])
    assert default_events == [("text", "ordinary")]


def test_parallel_nonstream_conversion_preserves_text_thought_and_calls():
    response_id = "chatcmpl-sanitized"
    full_text, reasoning, calls = "", "", []
    for kind, data in _events(FIXTURES["parallel_mixed"]):
        if kind == "text":
            full_text += data
        elif kind == "thought":
            reasoning += data
        elif kind == "function_call":
            calls.append(cp._openai_tool_call(response_id, len(calls), data))
    assert full_text == "I will use both results."
    assert reasoning == "checking"
    assert [call["function"]["name"] for call in calls] == ["first_tool", "second_tool"]
    assert calls[0]["id"] != calls[1]["id"]
    assert calls[0]["extra_content"]["google"]["thought_signature"] == "cGFyYWxsZWwtc2lnbmF0dXJl"
    assert "extra_content" not in calls[1]
    assert signature_store.get_record(calls[0]["id"]).state is SignatureState.SIGNED
    assert signature_store.get_record(calls[1]["id"]).state is SignatureState.UNSIGNED_FOLLOWER


def test_nonstreaming_response_returns_openai_message_tool_calls(monkeypatch):
    app_state.set_google_cookie("SAPISID=test; SID=test")
    app_state.set_project_id("example-project")

    class FakeResponse:
        status_code = 200
        text = json.dumps(FIXTURES["parallel_mixed"])

    class FakeClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, url, headers=None, json=None):
            return FakeResponse()

    class FakeRequest:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(cp.httpx, "AsyncClient", FakeClient)
    req = _request(tools=[_function_tool("first_tool"), _function_tool("second_tool")])
    response = asyncio.run(cp.CookieProxyUpstream().chat_completions(req, FakeRequest()))
    payload = json.loads(response.body)
    choice = payload["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "I will use both results."
    assert choice["message"]["reasoning_content"] == "checking"
    assert [c["function"]["name"] for c in choice["message"]["tool_calls"]] == [
        "first_tool", "second_tool"]


def test_streaming_deltas_have_stable_indexes_ids_and_tool_finish(monkeypatch):
    app_state.set_google_cookie("SAPISID=test; SID=test")
    app_state.set_project_id("example-project")

    async def fake_execute(client, headers, body, sampler=None, fallback_body=None):
        for event in _events(FIXTURES["parallel_mixed"]):
            yield event

    monkeypatch.setattr(cp, "_execute_stream_request_generator", fake_execute)
    req = _request(tools=[_function_tool("first_tool"), _function_tool("second_tool")], stream=True)

    class FakeRequest:
        async def is_disconnected(self):
            return False

    async def run():
        response = await cp.CookieProxyUpstream().chat_completions(req, FakeRequest())
        return "".join([chunk async for chunk in response.body_iterator])

    raw = asyncio.run(run())
    chunks = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: {")]
    deltas = [choice["delta"] for chunk in chunks for choice in chunk["choices"]]
    streamed_calls = [call for delta in deltas for call in delta.get("tool_calls", [])]
    assert [call["index"] for call in streamed_calls] == [0, 1]
    assert len({call["id"] for call in streamed_calls}) == 2
    assert [call["function"]["name"] for call in streamed_calls] == ["first_tool", "second_tool"]
    assert any(delta.get("reasoning_content") == "checking" for delta in deltas)
    assert any(delta.get("content") == "I will use both results." for delta in deltas)
    assert any(choice["finish_reason"] == "tool_calls"
               for chunk in chunks for choice in chunk["choices"])


def test_controlled_fallback_runs_once_for_clear_native_schema_error(monkeypatch):
    calls = []

    async def fake_once(client, headers, body, sampler=None):
        calls.append(body["kind"])
        if body["kind"] == "native":
            yield "native_tool_unsupported", "FunctionDeclarations unsupported by this model"
        else:
            yield "text", "fallback ok"
            yield "finish", "STOP"

    monkeypatch.setattr(cp, "_execute_stream_request_generator_once", fake_once)

    async def run():
        return [event async for event in cp._execute_stream_request_generator(
            object(), {}, {"kind": "native"}, fallback_body={"kind": "fallback"})]

    assert asyncio.run(run()) == [("text", "fallback ok"), ("finish", "STOP")]
    assert calls == ["native", "fallback"]
    assert cp._native_tool_error("Invalid argument: functionCallingConfig is not supported")
    assert not cp._native_tool_error("429 quota exhausted")
