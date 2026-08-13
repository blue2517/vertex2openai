# -*- coding: utf-8 -*-
"""Cookie(Studio) 通道工具流量自动降级（疑问1）。

RikkaHub 这类前端只要模型卡勾了工具能力，每条请求都带 tools 声明，
旧实现只看 tools 非空就 400，Studio 通道等于不可用。
"""
import asyncio
import json

from models import OpenAIRequest, OpenAIMessage
from runtime_state import app_state
import upstreams.cookie_proxy as cp


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


# ---------- 分类器 ----------

def test_declared_only_is_not_history():
    tt = cp.classify_tool_traffic([OpenAIMessage(role="user", content="hi")], [_tool("get_weather")])
    assert tt["declared"] is True and tt["history"] is False
    assert tt["custom_names"] == ["get_weather"]
    assert tt["builtin_search"] is False


def test_history_tool_traffic_detected():
    msgs = [
        OpenAIMessage(role="user", content="hi"),
        OpenAIMessage(role="assistant", content=None,
                      tool_calls=[{"id": "1", "type": "function",
                                   "function": {"name": "f", "arguments": "{}"}}]),
        OpenAIMessage(role="tool", name="f", tool_call_id="1", content="42"),
    ]
    tt = cp.classify_tool_traffic(msgs, None)
    assert tt["history"] is True and tt["declared"] is False


def test_builtin_search_recognized():
    for nm in ["google_search", "googleSearch", "web_search", "search-web"]:
        tt = cp.classify_tool_traffic([], [_tool(nm)])
        assert tt["builtin_search"] is True, nm
        assert tt["custom_names"] == []


def test_no_tools_is_clean():
    tt = cp.classify_tool_traffic([OpenAIMessage(role="user", content="hi")], None)
    assert tt == {"declared": False, "history": False, "builtin_search": False, "custom_names": []}


def test_legacy_wrapper_still_true():
    assert cp.has_tool_traffic([], [_tool("x")]) is True
    assert cp.has_tool_traffic([OpenAIMessage(role="user", content="hi")], None) is False


# ---------- 工具往返渲染成可读文本 ----------

def test_tool_history_rendered_as_text():
    msgs = [
        OpenAIMessage(role="user", content="北京天气?"),
        OpenAIMessage(role="assistant", content=None,
                      tool_calls=[{"id": "1", "type": "function",
                                   "function": {"name": "get_weather",
                                                "arguments": '{"city":"北京"}'}}]),
        OpenAIMessage(role="tool", name="get_weather", tool_call_id="1", content="晴 30C"),
    ]
    contents, _ = cp._convert_messages_to_contents(msgs)
    blob = json.dumps(contents, ensure_ascii=False)
    assert "请求调用工具" in blob and "get_weather" in blob
    assert "工具执行结果" in blob and "晴 30C" in blob
    # 角色序列必须仍然交替合法（user → model → user）
    assert [c["role"] for c in contents] == ["user", "model", "user"]


def test_plain_text_of_variants():
    assert cp._plain_text_of("abc") == "abc"
    assert cp._plain_text_of([{"type": "text", "text": "x"}]) == "x"
    assert cp._plain_text_of(None) == ""


# ---------- 降级映射进载荷 ----------

def test_force_search_enables_googlesearch():
    req = OpenAIRequest(model="gemini-3.7-flash",
                        messages=[OpenAIMessage(role="user", content="hi")])
    body = cp._build_batch_graphql_body("proj", "gemini-3.7-flash", req, force_search=True)
    assert body["variables"]["tools"] == [{"googleSearch": {}}]
    body2 = cp._build_batch_graphql_body("proj", "gemini-3.7-flash", req, force_search=False)
    assert "tools" not in body2["variables"]


# ---------- 入口策略（端到端） ----------

class _FakeReq:
    async def is_disconnected(self):
        return False


def _setup_auth():
    app_state.set_google_cookie("SAPISID=abc; SID=x")
    app_state.set_project_id("p")


def _call(req):
    _setup_auth()
    return asyncio.run(cp.CookieProxyUpstream().chat_completions(req, _FakeReq()))


def test_reject_policy_returns_400():
    app_state.update_settings({"cookie_tool_policy": "reject"})
    req = OpenAIRequest(model="gemini-3.7-flash", stream=False,
                        messages=[OpenAIMessage(role="user", content="hi")],
                        tools=[_tool("get_weather")])
    resp = _call(req)
    assert resp.status_code == 400
    assert "不支持函数调用" in json.loads(resp.body)["error"]["message"]


def test_degrade_policy_drops_tools_and_proceeds(monkeypatch):
    """默认降级：不再 400，工具声明被丢弃，请求照常发出。"""
    app_state.update_settings({"cookie_tool_policy": "degrade"})
    captured = {}

    async def fake_build(project_id, model_name, request, prefill_active=False, force_search=False):
        captured["tools"] = request.tools
        captured["force_search"] = force_search
        return {"variables": {"contents": [], "generationConfig": {}}}

    async def fake_exec(client, headers, body, sampler=None):
        yield ("text", "正常回复")
        yield ("finish", "STOP")

    monkeypatch.setattr(cp, "build_batch_graphql_body_async", fake_build)
    monkeypatch.setattr(cp, "_execute_stream_request_generator", fake_exec)

    req = OpenAIRequest(model="gemini-3.7-flash", stream=True,
                        messages=[OpenAIMessage(role="user", content="hi")],
                        tools=[_tool("get_weather")], tool_choice="auto")

    async def run():
        resp = await cp.CookieProxyUpstream().chat_completions(req, _FakeReq())
        return [c async for c in resp.body_iterator]

    _setup_auth()
    chunks = asyncio.run(run())
    assert captured["tools"] is None            # 声明已丢弃
    assert captured["force_search"] is False
    body = "".join(chunks)
    assert "正常回复" in body and "[DONE]" in body


def test_degrade_maps_search_tool(monkeypatch):
    app_state.update_settings({"cookie_tool_policy": "degrade"})
    captured = {}

    async def fake_build(project_id, model_name, request, prefill_active=False, force_search=False):
        captured["force_search"] = force_search
        return {"variables": {"contents": [], "generationConfig": {}}}

    async def fake_exec(client, headers, body, sampler=None):
        yield ("text", "ok")
        yield ("finish", "STOP")

    monkeypatch.setattr(cp, "build_batch_graphql_body_async", fake_build)
    monkeypatch.setattr(cp, "_execute_stream_request_generator", fake_exec)

    req = OpenAIRequest(model="gemini-3.7-flash", stream=True,
                        messages=[OpenAIMessage(role="user", content="hi")],
                        tools=[_tool("google_search")])

    async def run():
        resp = await cp.CookieProxyUpstream().chat_completions(req, _FakeReq())
        return [c async for c in resp.body_iterator]

    _setup_auth()
    asyncio.run(run())
    assert captured["force_search"] is True     # 搜索类工具映射为内建 googleSearch
