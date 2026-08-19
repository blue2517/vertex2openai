# -*- coding: utf-8 -*-
"""Focused coverage for Google's generateContent thought-signature rules."""
import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from google.genai import types

from api_helpers import ToolCallIndexer, convert_chunk_to_openai, _chunk_openai_response_dict_for_sse
from message_processing import create_gemini_prompt, process_gemini_response_to_openai_dict
from models import OpenAIMessage
from signature_store import (
    SKIP_VALIDATOR_SENTINEL,
    SignatureState,
    signature_store,
)


def _candidate(parts):
    return SimpleNamespace(
        content=types.Content(role="model", parts=parts),
        finish_reason=None,
        safety_ratings=[],
        text=None,
    )


def _response(parts):
    return SimpleNamespace(candidates=[_candidate(parts)])


def _fc(name, call_id, sig=None, **args):
    return types.Part(
        function_call=types.FunctionCall(name=name, id=call_id, args=args),
        thought_signature=sig,
    )


def _assistant_from(response_dict):
    return OpenAIMessage.model_validate(response_dict["choices"][0]["message"])


def _sig(message_or_call):
    encoded = message_or_call["extra_content"]["google"]["thought_signature"]
    return base64.b64decode(encoded)


def test_single_function_call_round_trips_via_explicit_extra_content_after_store_clear():
    out = process_gemini_response_to_openai_dict(
        _response([_fc("lookup", "call-1", b"single-signature", q="x")]), "gemini-3.7-flash")
    payload = out["choices"][0]["message"]
    assert _sig(payload["tool_calls"][0]) == b"single-signature"

    signature_store.clear()
    prompt = create_gemini_prompt([_assistant_from(out)], "gemini-3.7-flash")
    part = prompt[0].parts[0]
    assert part.function_call.name == "lookup"
    assert part.thought_signature == b"single-signature"


def test_parallel_calls_replay_fc1_signed_followers_explicitly_unsigned():
    out = process_gemini_response_to_openai_dict(_response([
        _fc("one", "fc-1", b"parallel-signature", n=1),
        _fc("two", "fc-2", None, n=2),
        _fc("three", "fc-3", None, n=3),
    ]), "gemini-3.7-flash")
    calls = out["choices"][0]["message"]["tool_calls"]
    assert _sig(calls[0]) == b"parallel-signature"
    assert "extra_content" not in calls[1]
    assert "extra_content" not in calls[2]

    signature_store.clear()
    replay = create_gemini_prompt([_assistant_from(out)], "gemini-3.7-flash")[0].parts
    assert [p.function_call.name for p in replay] == ["one", "two", "three"]
    assert [p.thought_signature for p in replay] == [b"parallel-signature", None, None]


def test_sequential_steps_preserve_every_required_signature():
    messages = [
        OpenAIMessage(role="user", content="do both"),
        OpenAIMessage(role="assistant", tool_calls=[{
            "id": "step-1", "type": "function",
            "function": {"name": "first", "arguments": "{}"},
            "extra_content": {"google": {"thought_signature": base64.b64encode(b"sig-a").decode()}},
        }]),
        OpenAIMessage(role="tool", name="first", tool_call_id="step-1", content='{"ok":true}'),
        OpenAIMessage(role="assistant", tool_calls=[{
            "id": "step-2", "type": "function",
            "function": {"name": "second", "arguments": "{}"},
            "extra_content": {"google": {"thought_signature": base64.b64encode(b"sig-b").decode()}},
        }]),
        OpenAIMessage(role="tool", name="second", tool_call_id="step-2", content='{"ok":true}'),
    ]
    signature_store.clear()
    contents = create_gemini_prompt(messages, "gemini-3.7-flash")
    function_calls = [p for c in contents for p in c.parts if p.function_call]
    assert [p.thought_signature for p in function_calls] == [b"sig-a", b"sig-b"]


def test_cache_loss_uses_sentinel_only_for_required_first_call():
    signature_store.clear()
    assistant = OpenAIMessage(role="assistant", tool_calls=[
        {"id": "lost-1", "type": "function", "function": {"name": "one", "arguments": "{}"}},
        {"id": "lost-2", "type": "function", "function": {"name": "two", "arguments": "{}"}},
        {"id": "lost-3", "type": "function", "function": {"name": "three", "arguments": "{}"}},
    ])
    parts = create_gemini_prompt([assistant], "gemini-3.7-flash")[0].parts
    assert [p.thought_signature for p in parts] == [SKIP_VALIDATOR_SENTINEL, None, None]


def test_signature_store_distinguishes_signed_unsigned_follower_and_unknown():
    process_gemini_response_to_openai_dict(_response([
        _fc("one", "state-1", b"sig"),
        _fc("two", "state-2", None),
    ]), "gemini-3.7-flash")
    assert signature_store.get_record("state-1").state is SignatureState.SIGNED
    assert signature_store.get_record("state-2").state is SignatureState.UNSIGNED_FOLLOWER
    signature_store.put_unknown("state-3")
    assert signature_store.get_record("state-3").state is SignatureState.UNKNOWN


@pytest.mark.parametrize("thought,kind", [(False, "text"), (True, "thought")])
def test_ordinary_part_signature_returns_to_original_part_kind(thought, kind):
    original = types.Part(text="reason" if thought else "answer", thought=thought,
                          thought_signature=b"ordinary-signature")
    out = process_gemini_response_to_openai_dict(_response([original]), "gemini-3.7-flash")
    payload = out["choices"][0]["message"]
    assert payload["extra_content"]["google"]["thought_signature_part"] == kind
    replay = create_gemini_prompt([OpenAIMessage.model_validate(payload)], "gemini-3.7-flash")[0].parts
    signed = next(p for p in replay if p.thought_signature)
    assert bool(signed.thought) is thought
    assert signed.thought_signature == b"ordinary-signature"


def test_mixed_text_thought_and_tool_call_are_all_preserved_in_part_order():
    parts = [
        types.Part(text="visible"),
        _fc("act", "mixed-call", b"fc-signature", x=1),
        types.Part(text="thinking", thought=True, thought_signature=b"thought-signature"),
    ]
    out = process_gemini_response_to_openai_dict(_response(parts), "gemini-3.7-flash")
    payload = out["choices"][0]["message"]
    assert payload["content"] == "visible"
    assert payload["reasoning_content"] == "thinking"
    assert len(payload["tool_calls"]) == 1

    signature_store.clear()
    replay = create_gemini_prompt([OpenAIMessage.model_validate(payload)], "gemini-3.7-flash")[0].parts
    assert replay[0].text == "visible"
    assert replay[1].function_call.name == "act"
    assert replay[1].thought_signature == b"fc-signature"
    assert replay[2].thought is True
    assert replay[2].thought_signature == b"thought-signature"


def test_signature_only_stream_delta_is_emitted_and_round_trips():
    chunk = _response([types.Part(text="", thought_signature=b"stream-signature")])
    sse = convert_chunk_to_openai(chunk, "gemini-3.7-flash", "resp-1",
                                  indexer=ToolCallIndexer())
    delta = json.loads(sse.removeprefix("data: ").strip())["choices"][0]["delta"]
    assert _sig(delta) == b"stream-signature"
    assert delta["extra_content"]["google"]["thought_signature_part"] == "signature_only"

    message = OpenAIMessage(role="assistant", content=None, extra_content=delta["extra_content"])
    replay = create_gemini_prompt([message], "gemini-3.7-flash")[0].parts
    assert len(replay) == 1
    assert replay[0].text == ""
    assert replay[0].thought_signature == b"stream-signature"


def test_function_responses_are_unsigned_user_parts_grouped_without_fresh_user_merge():
    # Populate a signed fallback to prove it is not copied onto FunctionResponse.
    signature_store.put("result-1", b"must-not-attach")
    messages = [
        OpenAIMessage(role="tool", name="one", tool_call_id="result-1", content="1"),
        OpenAIMessage(role="tool", name="two", tool_call_id="result-2", content="2"),
        OpenAIMessage(role="user", content="new turn"),
    ]
    contents = create_gemini_prompt(messages, "gemini-3.7-flash")
    assert len(contents) == 2
    assert contents[0].role == "user"
    assert len(contents[0].parts) == 2
    assert all(p.function_response is not None for p in contents[0].parts)
    assert all(p.thought_signature is None for p in contents[0].parts)
    assert contents[1].role == "user"
    assert contents[1].parts[0].text == "new turn"


def test_tool_result_without_name_recovers_function_name_from_call_id():
    messages = [
        OpenAIMessage(role="assistant", tool_calls=[{
            "id": "call-standard", "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
            "extra_content": {"google": {
                "thought_signature": base64.b64encode(b"lookup-sig").decode()}},
        }]),
        OpenAIMessage(role="tool", tool_call_id="call-standard", content='{"ok":true}'),
    ]
    contents = create_gemini_prompt(messages, "gemini-3.5-flash")
    response_part = contents[1].parts[0]
    assert response_part.function_response is not None
    assert response_part.function_response.name == "lookup"
    assert response_part.thought_signature is None


def test_multiple_ordinary_parts_replay_without_aggregate_duplication():
    parts = [
        types.Part(text="A", thought_signature=b"sig-a"),
        types.Part(text="B", thought=True, thought_signature=b"sig-b"),
        types.Part(text="C", thought_signature=b"sig-c"),
        types.Part(text="", thought_signature=b"sig-final"),
    ]
    out = process_gemini_response_to_openai_dict(_response(parts), "gemini-3.7-flash")
    payload = out["choices"][0]["message"]
    assert payload["content"] == "AC"
    assert payload["reasoning_content"] == "B"
    assert len(payload["extra_content"]["google"]["ordinary_parts"]) == 4

    replay = create_gemini_prompt([OpenAIMessage.model_validate(payload)], "gemini-3.7-flash")[0].parts
    assert [(p.text, bool(p.thought), p.thought_signature) for p in replay] == [
        ("A", False, b"sig-a"),
        ("B", True, b"sig-b"),
        ("C", False, b"sig-c"),
        ("", False, b"sig-final"),
    ]


def test_stream_chunk_preserves_all_ordinary_signatures_not_only_last():
    chunk = _response([
        types.Part(text="one", thought_signature=b"first"),
        types.Part(text="two", thought=True, thought_signature=b"last"),
    ])
    sse = convert_chunk_to_openai(chunk, "gemini-3.7-flash", "resp",
                                  indexer=ToolCallIndexer())
    delta = json.loads(sse.removeprefix("data: ").strip())["choices"][0]["delta"]
    metadata = delta["extra_content"]["google"]["ordinary_parts"]
    assert [base64.b64decode(item["thought_signature"]) for item in metadata] == [b"first", b"last"]


def test_express_fake_stream_round_trip_preserves_mixed_content_calls_and_signatures():
    response = process_gemini_response_to_openai_dict(_response([
        types.Part(text="visible"),
        _fc("act", "fake-call", b"tool-sig", value=1),
        types.Part(text="reason", thought=True, thought_signature=b"message-sig"),
    ]), "gemini-3.7-flash")

    async def collect():
        return [chunk async for chunk in _chunk_openai_response_dict_for_sse(response)]

    chunks = [json.loads(item[6:]) for item in asyncio.run(collect()) if item.startswith("data: {")]
    message = {"role": "assistant", "content": "", "reasoning_content": "", "tool_calls": []}
    calls = {}
    for chunk in chunks:
        choice = chunk["choices"][0]
        delta = choice["delta"]
        message["content"] += delta.get("content", "")
        message["reasoning_content"] += delta.get("reasoning_content", "")
        if "extra_content" in delta:
            message["extra_content"] = delta["extra_content"]
        for tc in delta.get("tool_calls", []):
            call = calls.setdefault(tc["index"], {
                "id": tc.get("id"), "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if tc.get("id"):
                call["id"] = tc["id"]
            if tc.get("extra_content"):
                call["extra_content"] = tc["extra_content"]
            function = tc.get("function") or {}
            if function.get("name"):
                call["function"]["name"] = function["name"]
            call["function"]["arguments"] += function.get("arguments", "")
    message["tool_calls"] = [calls[i] for i in sorted(calls)]

    assert message["content"] == "visible"
    assert message["reasoning_content"] == "reason"
    assert _sig(message["tool_calls"][0]) == b"tool-sig"
    assert _sig(message) == b"message-sig"

    replay = create_gemini_prompt([OpenAIMessage.model_validate(message)], "gemini-3.7-flash")[0].parts
    assert replay[0].text == "visible"
    assert replay[1].function_call.name == "act"
    assert replay[1].thought_signature == b"tool-sig"
    assert replay[2].text == "reason" and replay[2].thought is True
    assert replay[2].thought_signature == b"message-sig"


def test_true_stream_cross_chunk_topology_is_cumulative_on_final_delta():
    indexer = ToolCallIndexer()
    thought_chunk = convert_chunk_to_openai(
        _response([types.Part(text="plan", thought=True, thought_signature=b"thought-sig")]),
        "gemini-3.5-flash", "resp", indexer=indexer,
    )
    call_chunk = convert_chunk_to_openai(
        _response([_fc("act", "cross-call", b"call-sig", value=1)]),
        "gemini-3.5-flash", "resp", indexer=indexer,
    )
    final_candidate = _candidate([])
    final_candidate.finish_reason = "STOP"
    final_chunk = convert_chunk_to_openai(
        SimpleNamespace(candidates=[final_candidate]),
        "gemini-3.5-flash", "resp", indexer=indexer,
    )

    deltas = [
        json.loads(item.removeprefix("data: ").strip())["choices"][0]["delta"]
        for item in (thought_chunk, call_chunk, final_chunk)
    ]
    # Standard SSE aggregation replaces unknown extension objects; the last delta
    # must therefore contain the complete metadata, not only the final chunk.
    final_google = deltas[-1]["extra_content"]["google"]
    assert [item["type"] for item in final_google["part_order"]] == ["ordinary", "tool_call"]
    assert base64.b64decode(final_google["ordinary_parts"][0]["thought_signature"]) == b"thought-sig"

    call = deltas[1]["tool_calls"][0]
    message = OpenAIMessage(
        role="assistant",
        content=None,
        reasoning_content="plan",
        tool_calls=[call],
        extra_content=deltas[-1]["extra_content"],
    )
    replay = create_gemini_prompt([message], "gemini-3.5-flash")[0].parts
    assert replay[0].thought is True and replay[0].thought_signature == b"thought-sig"
    assert replay[1].function_call.name == "act"
    assert replay[1].thought_signature == b"call-sig"


def test_true_stream_final_stop_becomes_tool_calls_after_prior_function_call():
    indexer = ToolCallIndexer()
    first = convert_chunk_to_openai(
        _response([_fc("act", "stream-call", b"stream-tool-sig", value=1)]),
        "gemini-3.5-flash", "resp", indexer=indexer,
    )
    first_choice = json.loads(first.removeprefix("data: ").strip())["choices"][0]
    assert first_choice["delta"]["tool_calls"]

    final_candidate = _candidate([])
    final_candidate.finish_reason = "STOP"
    final = convert_chunk_to_openai(
        SimpleNamespace(candidates=[final_candidate]),
        "gemini-3.5-flash", "resp", indexer=indexer,
    )
    final_choice = json.loads(final.removeprefix("data: ").strip())["choices"][0]
    assert final_choice["finish_reason"] == "tool_calls"
