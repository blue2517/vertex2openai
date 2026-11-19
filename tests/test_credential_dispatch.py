import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from google import genai
from google.genai import types

import api_helpers
import credential_dispatch
import network_proxy as proxy
import runtime_state
from auth import get_api_key
from cookie_credentials import cookie_credential_manager
from express_key_manager import ExpressKeyManager
from models import OpenAIRequest
from request_lifecycle import DisconnectMiddleware, active_jobs, finished_jobs
from resumable import ResumableStore
from routes import chat_api
from test_cookie_credentials import login, record as cookie_record
from upstreams import cookie_proxy as cp
from upstreams import express_sdk


def _sse_bytes(*chunks: dict) -> list[bytes]:
    return [f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode("utf-8") for c in chunks]


def _studio_frame(parts=None, finish=None) -> bytes:
    cand = {"content": {"parts": parts or []}}
    if finish:
        cand["finishReason"] = finish
    return json.dumps({"results": [{"data": {"candidates": [cand]}}]}, ensure_ascii=False).encode("utf-8")


STUDIO_UI = b'{"results":[{"data":{"ui":{"streamGenerateContentV2":[]}}}]}'
STUDIO_429_200 = b'{"results":[{"errors":[{"message":"Resource exhausted","extensions":{"code":"RESOURCE_EXHAUSTED"}}]}]}'


def _collect_sse_text(frames: list) -> str:
    out = []
    for frame in frames:
        text = frame.decode("utf-8") if isinstance(frame, bytes) else str(frame)
        for line in text.splitlines():
            if line.startswith("data:") and line[5:].strip() != "[DONE]":
                payload = json.loads(line[5:])
                for ch in payload.get("choices", []):
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        out.append(delta["content"])
    return "".join(out)


# ============================================================
# 1. Settings, validation, persistence & UI smoke tests
# ============================================================

def test_dispatch_settings_defaults_and_persistence(client):
    login(client)
    initial = client.get("/api/settings").json()
    assert initial["credential_failover_enabled"] is False
    assert initial["credential_failover_max"] == 3
    assert initial["credential_race_enabled"] is False
    assert initial["credential_race_concurrency"] == 2
    assert initial["credential_race_delay_seconds"] == 2.0
    assert initial["credential_race_win_mode"] == "first_output"
    assert initial["credential_race_stream_only"] is False

    patch = {
        "credential_failover_enabled": True,
        "credential_failover_max": 5,
        "credential_race_enabled": True,
        "credential_race_concurrency": 4,
        "credential_race_delay_seconds": 3.5,
        "credential_race_win_mode": "first_packet",
        "credential_race_stream_only": True,
    }
    resp = client.post("/api/settings", json=patch)
    assert resp.status_code == 200
    saved = resp.json()
    for key, expected in patch.items():
        assert saved[key] == expected
    reloaded = client.get("/api/settings").json()
    for key, expected in patch.items():
        assert reloaded[key] == expected


@pytest.mark.parametrize("key,bad", [
    ("credential_failover_enabled", "yes"),
    ("credential_failover_enabled", 1),
    ("credential_failover_max", -1),
    ("credential_failover_max", 51),
    ("credential_failover_max", 1.5),
    ("credential_failover_max", True),
    ("credential_failover_max", "3"),
    ("credential_race_enabled", 0),
    ("credential_race_concurrency", 1),
    ("credential_race_concurrency", 21),
    ("credential_race_concurrency", 2.5),
    ("credential_race_concurrency", False),
    ("credential_race_delay_seconds", -0.5),
    ("credential_race_delay_seconds", 60.5),
    ("credential_race_delay_seconds", True),
    ("credential_race_delay_seconds", "2"),
    ("credential_race_win_mode", "raw_bytes"),
    ("credential_race_win_mode", 1),
    ("credential_race_stream_only", "false"),
])
def test_invalid_dispatch_settings_reject_entire_patch(client, key, bad):
    login(client)
    before = client.get("/api/settings").json()
    resp = client.post("/api/settings", json={key: bad, "retry_max": 42})
    assert resp.status_code == 400
    assert client.get("/api/settings").json() == before


def test_corrupted_persisted_dispatch_settings_fallback_to_defaults(isolated_state):
    isolated_state._state["settings"] = {
        "credential_failover_enabled": "bad",
        "credential_failover_max": 999,
        "credential_race_enabled": 123,
        "credential_race_concurrency": 0,
        "credential_race_delay_seconds": float("nan"),
        "credential_race_win_mode": "unknown",
        "credential_race_stream_only": "no",
    }
    cfg = isolated_state.get_dispatch_settings()
    assert cfg == {
        "credential_failover_enabled": False,
        "credential_failover_max": 3,
        "credential_race_enabled": False,
        "credential_race_concurrency": 2,
        "credential_race_delay_seconds": 2.0,
        "credential_race_win_mode": "first_output",
        "credential_race_stream_only": False,
    }


def test_dispatch_settings_are_global_only(isolated_state):
    isolated_state.update_settings({"credential_failover_enabled": True, "credential_failover_max": 4})
    ov = isolated_state.set_model_override("gemini-4-argon", {
        "credential_failover_enabled": False,
        "credential_failover_max": 0,
        "credential_race_enabled": True,
        "thinking_g3_level": "low",
    })
    assert "credential_failover_enabled" not in ov
    assert "credential_failover_max" not in ov
    assert "credential_race_enabled" not in ov


def test_dashboard_ui_exposes_dispatch_controls(client):
    login(client)
    html = client.get("/").text
    for elem_id in (
        "credential_failover_enabled",
        "credential_failover_max",
        "credential_race_enabled",
        "credential_race_concurrency",
        "credential_race_delay_seconds",
        "credential_race_win_mode",
        "credential_race_stream_only",
    ):
        assert f'id="{elem_id}"' in html
        assert f"'{elem_id}'" in html or f"{elem_id}:" in html


# ============================================================
# 2. Express channel: Failover, Racing, Mode B, Proxy Isolation
# ============================================================

def _setup_express_pool(isolated_state, specs):
    """specs: list of (key, note, proxy_url)"""
    keys = []
    for key, note, proxy_url in specs:
        pid = proxy.add_proxy_entry(f"Proxy-{note}", proxy_url)["pool"][-1]["id"]
        keys.append({"key": key, "note": note, "project_id": "", "enabled": True, "proxy_mode": "pool", "proxy_id": pid})
    isolated_state.set_express_keys(keys)
    manager = ExpressKeyManager()
    return manager


def _make_express_env(monkeypatch, isolated_state, specs, handler_fn):
    manager = _setup_express_pool(isolated_state, specs)
    isolated_state.update_settings({
        "retry_max": 0,
        "retry_backoff_seconds": 0,
        "roundrobin": True,
        "express_location": "",
    })
    seen_clients = []
    real_client_cls = genai.Client

    def fake_genai_client(*, vertexai=True, api_key="", http_options=None):
        observed_proxy = None
        if http_options and http_options.async_client_args:
            observed_proxy = http_options.async_client_args.get("proxy")
        closed_flag = {"closed": False}

        async def _transport_handler(request):
            return await handler_fn(api_key, observed_proxy, request)

        async_httpx = httpx.AsyncClient(transport=httpx.MockTransport(_transport_handler))
        client = real_client_cls(
            vertexai=vertexai,
            api_key=api_key,
            http_options=types.HttpOptions(httpx_async_client=async_httpx),
        )
        orig_aio_aclose = client.aio.aclose

        async def tracked_aio_aclose():
            closed_flag["closed"] = True
            await orig_aio_aclose()
            await async_httpx.aclose()

        client.aio.aclose = tracked_aio_aclose
        seen_clients.append({"key": api_key, "proxy": observed_proxy, "closed": closed_flag})
        return client

    monkeypatch.setattr(express_sdk.genai, "Client", fake_genai_client)
    base_req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(express_key_manager=manager)),
        is_disconnected=AsyncMock(return_value=False),
        register_upstream_client=lambda c: c,
    )
    return manager, seen_clients, base_req


def test_express_failover_roundrobin_and_strict_proxy_isolation(monkeypatch, isolated_state):
    specs = [
        ("KEY_1", "A", "http://proxy-a:8001"),
        ("KEY_2", "B", "http://proxy-b:8002"),
        ("KEY_3", "C", "http://proxy-c:8003"),
    ]
    attempts = []

    async def handler(api_key, observed_proxy, request):
        attempts.append((api_key, observed_proxy))
        if api_key == "KEY_1":
            return httpx.Response(429, text='{"error":{"code":429,"message":"RESOURCE_EXHAUSTED"}}')
        if api_key == "KEY_2":
            raise httpx.ReadTimeout("read timed out")
        body = _sse_bytes({"candidates": [{"content": {"parts": [{"text": "from-key-3"}]}, "finishReason": "STOP"}]})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"".join(body))

    _, seen_clients, base_req = _make_express_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "credential_failover_enabled": True,
        "credential_failover_max": 2,
        "credential_race_enabled": False,
    })

    async def run():
        req = OpenAIRequest(model="gemini-3.8-flash", stream=True, messages=[{"role": "user", "content": "hi"}])
        resp = await credential_dispatch.dispatch_upstream(
            express_sdk.ExpressSDKUpstream(), req, base_req, is_cookie_channel=False
        )
        assert resp.status_code == 200
        frames = [f async for f in resp.body_iterator]
        return _collect_sse_text(frames)

    text = asyncio.run(run())
    assert text == "from-key-3"
    assert attempts == [
        ("KEY_1", "http://proxy-a:8001"),
        ("KEY_2", "http://proxy-b:8002"),
        ("KEY_3", "http://proxy-c:8003"),
    ]
    # Failed attempts had their clients closed immediately.
    assert seen_clients[0]["closed"]["closed"] is True
    assert seen_clients[1]["closed"]["closed"] is True


def test_express_failover_does_not_retry_400_content_filter(monkeypatch, isolated_state):
    specs = [("KEY_1", "A", "http://proxy-a:8001"), ("KEY_2", "B", "http://proxy-b:8002")]
    attempts = []

    async def handler(api_key, observed_proxy, request):
        attempts.append(api_key)
        return httpx.Response(400, text='{"error":{"code":400,"message":"Invalid argument"}}')

    _, _, base_req = _make_express_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "credential_failover_enabled": True,
        "credential_failover_max": 3,
        "credential_race_enabled": False,
    })

    req = OpenAIRequest(model="gemini-3.8-flash", stream=True, messages=[{"role": "user", "content": "hi"}])
    resp = asyncio.run(credential_dispatch.dispatch_upstream(
        express_sdk.ExpressSDKUpstream(), req, base_req, is_cookie_channel=False
    ))
    assert resp.status_code == 400
    assert attempts == ["KEY_1"]


def test_express_race_first_output_cancels_losers_and_isolates_proxies(monkeypatch, isolated_state):
    specs = [
        ("KEY_1", "A", "http://proxy-a:8001"),
        ("KEY_2", "B", "http://proxy-b:8002"),
        ("KEY_3", "C", "http://proxy-c:8003"),
    ]
    started = {}
    cancelled = []
    observed_pairs = {}
    all_started = asyncio.Event()

    async def handler(api_key, observed_proxy, request):
        observed_pairs[api_key] = observed_proxy
        started[api_key] = True
        if len(started) == 3:
            all_started.set()
        if api_key in ("KEY_1", "KEY_3"):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(api_key)
                raise
        await asyncio.wait_for(all_started.wait(), timeout=2.0)
        body = _sse_bytes({"candidates": [{"content": {"parts": [{"text": "winner-key-2"}]}, "finishReason": "STOP"}]})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"".join(body))

    _, seen_clients, base_req = _make_express_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "credential_failover_enabled": False,
        "credential_race_enabled": True,
        "credential_race_concurrency": 3,
        "credential_race_delay_seconds": 0.0,
        "credential_race_win_mode": "first_output",
    })

    async def run():
        req = OpenAIRequest(model="gemini-3.8-flash", stream=True, messages=[{"role": "user", "content": "hi"}])
        resp = await credential_dispatch.dispatch_upstream(
            express_sdk.ExpressSDKUpstream(), req, base_req, is_cookie_channel=False
        )
        assert resp.status_code == 200
        return _collect_sse_text([f async for f in resp.body_iterator])

    assert asyncio.run(run()) == "winner-key-2"
    assert observed_pairs == {
        "KEY_1": "http://proxy-a:8001",
        "KEY_2": "http://proxy-b:8002",
        "KEY_3": "http://proxy-c:8003",
    }
    assert set(cancelled) == {"KEY_1", "KEY_3"}
    by_key = {c["key"]: c["closed"]["closed"] for c in seen_clients}
    assert by_key["KEY_1"] is True
    assert by_key["KEY_3"] is True


def test_express_race_first_packet_cancels_losers_before_first_output(monkeypatch, isolated_state):
    specs = [
        ("KEY_1", "A", "http://proxy-a:8001"),
        ("KEY_2", "B", "http://proxy-b:8002"),
    ]
    k2_cancelled = asyncio.Event()
    k2_started = asyncio.Event()

    class StagedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            # First packet: empty text delta (HTTP 200 body packet, NOT yet first output!)
            yield _sse_bytes({"candidates": [{"content": {"parts": [{"text": ""}]}}]})[0]
            # Wait until K2 has been cancelled by first_packet win before emitting actual text!
            await asyncio.wait_for(k2_cancelled.wait(), timeout=2.0)
            yield _sse_bytes({"candidates": [{"content": {"parts": [{"text": "after-k2-cancelled"}]}, "finishReason": "STOP"}]})[0]

    async def handler(api_key, observed_proxy, request):
        if api_key == "KEY_2":
            k2_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                k2_cancelled.set()
                raise
        await asyncio.wait_for(k2_started.wait(), timeout=2.0)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=StagedBody())

    _, _, base_req = _make_express_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "credential_failover_enabled": False,
        "credential_race_enabled": True,
        "credential_race_concurrency": 2,
        "credential_race_delay_seconds": 0.0,
        "credential_race_win_mode": "first_packet",
    })

    async def run():
        req = OpenAIRequest(model="gemini-3.8-flash", stream=True, messages=[{"role": "user", "content": "hi"}])
        resp = await credential_dispatch.dispatch_upstream(
            express_sdk.ExpressSDKUpstream(), req, base_req, is_cookie_channel=False
        )
        assert resp.status_code == 200
        return _collect_sse_text([f async for f in resp.body_iterator])

    assert asyncio.run(run()) == "after-k2-cancelled"
    assert k2_cancelled.is_set()


def test_express_mode_b_refills_dead_slot_immediately_while_sibling_hangs(monkeypatch, isolated_state):
    specs = [
        ("KEY_1", "A", "http://proxy-a:8001"),
        ("KEY_2", "B", "http://proxy-b:8002"),
        ("KEY_3", "C", "http://proxy-c:8003"),
        ("KEY_4", "D", "http://proxy-d:8004"),
    ]
    k2_cancelled = False
    order = []

    async def handler(api_key, observed_proxy, request):
        nonlocal k2_cancelled
        order.append((api_key, observed_proxy))
        if api_key == "KEY_2":
            # Simulates long PVP queue hang — must NOT block KEY_1's slot from refilling KEY_3 -> KEY_4!
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                k2_cancelled = True
                raise
        if api_key in ("KEY_1", "KEY_3"):
            return httpx.Response(429, text='{"error":{"code":429,"message":"RESOURCE_EXHAUSTED"}}')
        body = _sse_bytes({"candidates": [{"content": {"parts": [{"text": "refill-winner-key-4"}]}, "finishReason": "STOP"}]})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"".join(body))

    _, _, base_req = _make_express_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "credential_failover_enabled": True,
        "credential_failover_max": 2,
        "credential_race_enabled": True,
        "credential_race_concurrency": 2,
        "credential_race_delay_seconds": 0.0,
        "credential_race_win_mode": "first_output",
    })

    async def run():
        req = OpenAIRequest(model="gemini-3.8-flash", stream=True, messages=[{"role": "user", "content": "hi"}])
        resp = await credential_dispatch.dispatch_upstream(
            express_sdk.ExpressSDKUpstream(), req, base_req, is_cookie_channel=False
        )
        assert resp.status_code == 200
        return _collect_sse_text([f async for f in resp.body_iterator])

    assert asyncio.run(run()) == "refill-winner-key-4"
    assert k2_cancelled is True
    assert ("KEY_1", "http://proxy-a:8001") in order
    assert ("KEY_2", "http://proxy-b:8002") in order
    assert ("KEY_3", "http://proxy-c:8003") in order
    assert ("KEY_4", "http://proxy-d:8004") in order


def test_race_stream_only_flag_controls_nonstream_racing(monkeypatch, isolated_state):
    specs = [
        ("KEY_1", "A", "http://proxy-a:8001"),
        ("KEY_2", "B", "http://proxy-b:8002"),
    ]
    seen_keys = []

    async def handler(api_key, observed_proxy, request):
        seen_keys.append(api_key)
        if api_key == "KEY_1":
            await asyncio.sleep(0.05)
        payload = {
            "candidates": [{"content": {"role": "model", "parts": [{"text": f"ok-{api_key}"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 2, "totalTokenCount": 4},
        }
        return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(payload).encode())

    _, _, base_req = _make_express_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "credential_failover_enabled": False,
        "credential_race_enabled": True,
        "credential_race_concurrency": 2,
        "credential_race_delay_seconds": 0.0,
        "credential_race_stream_only": True,
    })

    req = OpenAIRequest(model="gemini-3.8-flash", stream=False, messages=[{"role": "user", "content": "hi"}])
    resp = asyncio.run(credential_dispatch.dispatch_upstream(
        express_sdk.ExpressSDKUpstream(), req, base_req, is_cookie_channel=False
    ))
    assert resp.status_code == 200
    assert seen_keys == ["KEY_1"]  # stream_only=True skipped racing for non-stream

    # Now turn stream_only OFF -> both keys race on non-stream!
    seen_keys.clear()
    isolated_state.update_settings({"credential_race_stream_only": False})
    resp2 = asyncio.run(credential_dispatch.dispatch_upstream(
        express_sdk.ExpressSDKUpstream(), req, base_req, is_cookie_channel=False
    ))
    assert resp2.status_code == 200
    assert set(seen_keys) == {"KEY_1", "KEY_2"}


def test_random_delay_window_applies_to_secondary_slots(monkeypatch, isolated_state):
    specs = [
        ("KEY_1", "A", "http://proxy-a:8001"),
        ("KEY_2", "B", "http://proxy-b:8002"),
        ("KEY_3", "C", "http://proxy-c:8003"),
    ]
    uniform_calls = []
    sleep_calls = []

    monkeypatch.setattr(credential_dispatch.random, "uniform", lambda a, b: (uniform_calls.append((a, b)) or 0.0))
    orig_sleep = credential_dispatch.asyncio.sleep

    async def spy_sleep(sec):
        sleep_calls.append(sec)
        await orig_sleep(0)

    monkeypatch.setattr(credential_dispatch.asyncio, "sleep", spy_sleep)

    async def handler(api_key, observed_proxy, request):
        body = _sse_bytes({"candidates": [{"content": {"parts": [{"text": "fast"}]}, "finishReason": "STOP"}]})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"".join(body))

    _, _, base_req = _make_express_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "credential_failover_enabled": False,
        "credential_race_enabled": True,
        "credential_race_concurrency": 3,
        "credential_race_delay_seconds": 4.5,
    })

    req = OpenAIRequest(model="gemini-3.8-flash", stream=True, messages=[{"role": "user", "content": "hi"}])
    resp = asyncio.run(credential_dispatch.dispatch_upstream(
        express_sdk.ExpressSDKUpstream(), req, base_req, is_cookie_channel=False
    ))
    assert resp.status_code == 200
    # Slot 0 starts immediately; slots 1 & 2 draw random delay in [0.0, 4.5]
    assert uniform_calls == [(0.0, 4.5), (0.0, 4.5)]


# ============================================================
# 3. Cookie / Studio channel: Failover, Safe First-Packet, Mode B, Proxy Isolation
# ============================================================

def _make_cookie_env(monkeypatch, isolated_state, specs, handler_fn):
    """specs: list of (ident, name, project_id, proxy_url)"""
    creds = []
    for ident, name, project_id, proxy_url in specs:
        pid = proxy.add_proxy_entry(f"CookieProxy-{name}", proxy_url)["pool"][-1]["id"]
        creds.append(cookie_record(
            id=ident,
            name=name,
            cookie=f"SAPISID=SECRET_{ident}; SID=SID_{ident}",
            project_id=project_id,
            enabled=True,
            proxy_mode="pool",
            proxy_id=pid,
        ))
    isolated_state.set_cookie_credentials(creds)
    cookie_credential_manager.reset()
    isolated_state.update_settings({
        "retry_max": 0,
        "retry_backoff_seconds": 0,
        "roundrobin": True,
    })
    monkeypatch.setattr(cp, "build_headers", lambda c: {"x-test-cookie": c})
    monkeypatch.setattr(cp, "build_batch_graphql_body_async", AsyncMock(return_value={"variables": {}}))

    orig_async_client = httpx.AsyncClient
    created = []

    def fake_async_client(**kwargs):
        observed_proxy = kwargs.pop("proxy", None)
        closed_flag = {"closed": False}

        async def _transport(request):
            cookie_val = request.headers.get("x-test-cookie", "")
            ident = next((i for i, _, _, _ in specs if f"SECRET_{i}" in cookie_val), "unknown")
            return await handler_fn(ident, observed_proxy, request)

        client = orig_async_client(**kwargs, transport=httpx.MockTransport(_transport))
        orig_aclose = client.aclose

        async def tracked_aclose():
            closed_flag["closed"] = True
            await orig_aclose()

        client.aclose = tracked_aclose
        created.append({"proxy": observed_proxy, "closed": closed_flag})
        return client

    monkeypatch.setattr(cp.httpx, "AsyncClient", fake_async_client)
    base_req = SimpleNamespace(
        is_disconnected=AsyncMock(return_value=False),
        register_upstream_client=lambda c: c,
    )
    return created, base_req


def test_cookie_failover_skips_http200_429_and_401_with_proxy_isolation(monkeypatch, isolated_state):
    specs = [
        ("c1", "Cookie-1", "proj-1", "http://cookie-p1:8001"),
        ("c2", "Cookie-2", "proj-2", "http://cookie-p2:8002"),
        ("c3", "Cookie-3", "proj-3", "http://cookie-p3:8003"),
    ]
    seen = []
    errors_mock, retries_mock = Mock(), Mock()
    monkeypatch.setattr(cp.stats, "add_error", errors_mock)
    monkeypatch.setattr(credential_dispatch.stats, "add_retry", retries_mock)

    async def handler(ident, observed_proxy, request):
        seen.append((ident, observed_proxy))
        if ident == "c1":
            return httpx.Response(200, content=STUDIO_429_200)
        if ident == "c2":
            return httpx.Response(401, text="unauthenticated")
        return httpx.Response(200, content=_studio_frame([{"text": "from-cookie-3"}], finish="STOP"))

    _make_cookie_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "credential_failover_enabled": True,
        "credential_failover_max": 2,
        "credential_race_enabled": False,
    })

    async def run():
        req = OpenAIRequest(model="gemini-3.8-flash", stream=True, messages=[{"role": "user", "content": "hi"}])
        resp = await credential_dispatch.dispatch_upstream(
            cp.CookieProxyUpstream(), req, SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
            is_cookie_channel=True,
        )
        assert resp.status_code == 200
        return _collect_sse_text([f async for f in resp.body_iterator])

    assert asyncio.run(run()) == "from-cookie-3"
    assert seen == [
        ("c1", "http://cookie-p1:8001"),
        ("c2", "http://cookie-p2:8002"),
        ("c3", "http://cookie-p3:8003"),
    ]
    errors_mock.assert_not_called()
    assert retries_mock.call_count == 2


def test_cookie_race_first_packet_ignores_http200_429_and_wins_on_safe_ui_packet(monkeypatch, isolated_state):
    specs = [
        ("c1", "Cookie-1", "proj-1", "http://cookie-p1:8001"),
        ("c2", "Cookie-2", "proj-2", "http://cookie-p2:8002"),
        ("c3", "Cookie-3", "proj-3", "http://cookie-p3:8003"),
    ]
    c3_cancelled = asyncio.Event()
    c3_started = asyncio.Event()
    observed = {}

    class C2Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            # Wait briefly so C1's HTTP-200-wrapped 429 arrives FIRST (must NOT false-win!).
            await asyncio.sleep(0.03)
            yield STUDIO_UI
            # Must cancel C3 on safe first packet BEFORE real text arrives!
            await asyncio.wait_for(c3_cancelled.wait(), timeout=2.0)
            yield _studio_frame([{"text": "safe-first-packet-win"}], finish="STOP")

    async def handler(ident, observed_proxy, request):
        observed[ident] = observed_proxy
        if ident == "c1":
            # Immediate HTTP 200 wrapped 429 error — must NEVER trigger safe first_packet!
            return httpx.Response(200, content=STUDIO_429_200)
        if ident == "c3":
            c3_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                c3_cancelled.set()
                raise
        await asyncio.wait_for(c3_started.wait(), timeout=2.0)
        return httpx.Response(200, stream=C2Stream())

    _, base_req = _make_cookie_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "credential_failover_enabled": False,
        "credential_race_enabled": True,
        "credential_race_concurrency": 3,
        "credential_race_delay_seconds": 0.0,
        "credential_race_win_mode": "first_packet",
    })

    async def run():
        req = OpenAIRequest(model="gemini-3.8-flash", stream=True, messages=[{"role": "user", "content": "hi"}])
        resp = await credential_dispatch.dispatch_upstream(
            cp.CookieProxyUpstream(), req, base_req, is_cookie_channel=True
        )
        assert resp.status_code == 200
        return _collect_sse_text([f async for f in resp.body_iterator])

    assert asyncio.run(run()) == "safe-first-packet-win"
    assert c3_cancelled.is_set()
    assert observed == {
        "c1": "http://cookie-p1:8001",
        "c2": "http://cookie-p2:8002",
        "c3": "http://cookie-p3:8003",
    }


def test_cookie_race_first_output_waits_for_real_text_over_ui_only_stall(monkeypatch, isolated_state):
    specs = [
        ("c1", "Cookie-1", "proj-1", "http://cookie-p1:8001"),
        ("c2", "Cookie-2", "proj-2", "http://cookie-p2:8002"),
    ]
    c1_cancelled = asyncio.Event()
    c1_sent_ui = asyncio.Event()

    class C1StalledAfterUI(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield STUDIO_UI
            c1_sent_ui.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                c1_cancelled.set()
                raise

    async def handler(ident, observed_proxy, request):
        if ident == "c1":
            return httpx.Response(200, stream=C1StalledAfterUI())
        await asyncio.wait_for(c1_sent_ui.wait(), timeout=2.0)
        return httpx.Response(200, content=_studio_frame([{"text": "c2-real-text"}], finish="STOP"))

    _, base_req = _make_cookie_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "credential_failover_enabled": False,
        "credential_race_enabled": True,
        "credential_race_concurrency": 2,
        "credential_race_delay_seconds": 0.0,
        "credential_race_win_mode": "first_output",
    })

    async def run():
        req = OpenAIRequest(model="gemini-3.8-flash", stream=True, messages=[{"role": "user", "content": "hi"}])
        resp = await credential_dispatch.dispatch_upstream(
            cp.CookieProxyUpstream(), req, base_req, is_cookie_channel=True
        )
        assert resp.status_code == 200
        return _collect_sse_text([f async for f in resp.body_iterator])

    assert asyncio.run(run()) == "c2-real-text"
    assert c1_cancelled.is_set()


# ============================================================
# 4. Client disconnect cancels ALL active racing tasks cleanly
# ============================================================

def test_client_disconnect_cancels_all_racing_slots_and_closes_clients(monkeypatch, isolated_state):
    from test_request_lifecycle import Connection

    active_jobs.clear()
    finished_jobs.clear()
    specs = [
        ("KEY_1", "A", "http://proxy-a:8001"),
        ("KEY_2", "B", "http://proxy-b:8002"),
        ("KEY_3", "C", "http://proxy-c:8003"),
    ]
    all_entered = asyncio.Event()
    entered = set()
    cancelled = set()

    async def handler(api_key, observed_proxy, request):
        entered.add(api_key)
        if len(entered) == 3:
            all_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.add(api_key)
            raise

    manager, seen_clients, _ = _make_express_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.enable_web_proxy(False)
    isolated_state.update_settings({
        "resumable_enabled": False,
        "credential_failover_enabled": True,
        "credential_failover_max": 2,
        "credential_race_enabled": True,
        "credential_race_concurrency": 3,
        "credential_race_delay_seconds": 0.0,
    })

    app = FastAPI()
    app.state.express_key_manager = manager

    async def test_key(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise HTTPException(401)
        return authorization[7:]

    app.dependency_overrides[get_api_key] = test_key
    app.include_router(chat_api.router)
    app.add_middleware(DisconnectMiddleware)
    monkeypatch.setattr(chat_api, "resumable_store", ResumableStore())

    async def scenario():
        conn = Connection(app, body={"model": "gemini-3.8-flash", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        await asyncio.wait_for(all_entered.wait(), timeout=2.0)
        await conn.disconnect()
        assert cancelled == {"KEY_1", "KEY_2", "KEY_3"}
        assert all(c["closed"]["closed"] for c in seen_clients)
        assert not active_jobs

    asyncio.run(scenario())


def test_cookie_mode_b_refills_dead_slot_immediately_while_sibling_hangs(monkeypatch, isolated_state):
    specs = [
        ("c1", "Cookie-1", "proj-1", "http://cookie-p1:8001"),
        ("c2", "Cookie-2", "proj-2", "http://cookie-p2:8002"),
        ("c3", "Cookie-3", "proj-3", "http://cookie-p3:8003"),
    ]
    c2_cancelled = False
    seen = []

    async def handler(ident, observed_proxy, request):
        nonlocal c2_cancelled
        seen.append((ident, observed_proxy))
        if ident == "c2":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                c2_cancelled = True
                raise
        if ident == "c1":
            return httpx.Response(200, content=STUDIO_429_200)
        return httpx.Response(200, content=_studio_frame([{"text": "cookie-mode-b-c3"}], finish="STOP"))

    _, base_req = _make_cookie_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "credential_failover_enabled": True,
        "credential_failover_max": 2,
        "credential_race_enabled": True,
        "credential_race_concurrency": 2,
        "credential_race_delay_seconds": 0.0,
        "credential_race_win_mode": "first_output",
    })

    async def run():
        req = OpenAIRequest(model="gemini-3.8-flash", stream=True, messages=[{"role": "user", "content": "hi"}])
        resp = await credential_dispatch.dispatch_upstream(
            cp.CookieProxyUpstream(), req, base_req, is_cookie_channel=True
        )
        assert resp.status_code == 200
        return _collect_sse_text([f async for f in resp.body_iterator])

    assert asyncio.run(run()) == "cookie-mode-b-c3"
    assert c2_cancelled is True
    assert ("c1", "http://cookie-p1:8001") in seen
    assert ("c2", "http://cookie-p2:8002") in seen
    assert ("c3", "http://cookie-p3:8003") in seen


def test_failover_exhaustion_and_random_deduplication(monkeypatch, isolated_state):
    specs = [
        ("KEY_1", "A", "http://proxy-a:8001"),
        ("KEY_2", "B", "http://proxy-b:8002"),
        ("KEY_3", "C", "http://proxy-c:8003"),
    ]
    tried = []

    async def handler(api_key, observed_proxy, request):
        tried.append(api_key)
        return httpx.Response(429, text='{"error":{"code":429,"message":"RESOURCE_EXHAUSTED"}}')

    _, _, base_req = _make_express_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "roundrobin": False,
        "credential_failover_enabled": True,
        "credential_failover_max": 2,
        "credential_race_enabled": False,
    })

    req = OpenAIRequest(model="gemini-3.8-flash", stream=True, messages=[{"role": "user", "content": "hi"}])
    resp = asyncio.run(credential_dispatch.dispatch_upstream(
        express_sdk.ExpressSDKUpstream(), req, base_req, is_cookie_channel=False
    ))
    assert resp.status_code == 429
    # 1 initial + 2 retries = 3 attempts, all 3 distinct credentials tried without repeating in random mode.
    assert len(tried) == 3
    assert set(tried) == {"KEY_1", "KEY_2", "KEY_3"}


def test_race_caps_concurrency_to_available_credentials(monkeypatch, isolated_state):
    specs = [
        ("KEY_1", "A", "http://proxy-a:8001"),
        ("KEY_2", "B", "http://proxy-b:8002"),
    ]
    seen = []

    async def handler(api_key, observed_proxy, request):
        seen.append(api_key)
        if api_key == "KEY_1":
            await asyncio.Event().wait()
        body = _sse_bytes({"candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"".join(body))

    _, _, base_req = _make_express_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "credential_failover_enabled": False,
        "credential_race_enabled": True,
        "credential_race_concurrency": 6,
        "credential_race_delay_seconds": 0.0,
    })

    async def run():
        req = OpenAIRequest(model="gemini-3.8-flash", stream=True, messages=[{"role": "user", "content": "hi"}])
        resp = await credential_dispatch.dispatch_upstream(
            express_sdk.ExpressSDKUpstream(), req, base_req, is_cookie_channel=False
        )
        assert resp.status_code == 200
        return _collect_sse_text([f async for f in resp.body_iterator])

    assert asyncio.run(run()) == "ok"
    assert sorted(seen) == ["KEY_1", "KEY_2"]


def test_first_packet_claimed_winner_fails_before_output_recovers_via_failover(monkeypatch, isolated_state):
    specs = [
        ("c1", "Cookie-1", "proj-1", "http://cookie-p1:8001"),
        ("c2", "Cookie-2", "proj-2", "http://cookie-p2:8002"),
        ("c3", "Cookie-3", "proj-3", "http://cookie-p3:8003"),
    ]
    c2_cancelled = asyncio.Event()
    c2_started = asyncio.Event()

    class C1DropsAfterUI(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield STUDIO_UI
            await asyncio.wait_for(c2_cancelled.wait(), timeout=2.0)
            raise httpx.RemoteProtocolError("peer closed before text")

    async def handler(ident, observed_proxy, request):
        if ident == "c2":
            c2_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                c2_cancelled.set()
                raise
        if ident == "c1":
            await asyncio.wait_for(c2_started.wait(), timeout=2.0)
            return httpx.Response(200, stream=C1DropsAfterUI())
        return httpx.Response(200, content=_studio_frame([{"text": "recovered-on-c3"}], finish="STOP"))

    _, base_req = _make_cookie_env(monkeypatch, isolated_state, specs, handler)
    isolated_state.update_settings({
        "credential_failover_enabled": True,
        "credential_failover_max": 1,
        "credential_race_enabled": True,
        "credential_race_concurrency": 2,
        "credential_race_delay_seconds": 0.0,
        "credential_race_win_mode": "first_packet",
    })

    async def run():
        req = OpenAIRequest(model="gemini-3.8-flash", stream=True, messages=[{"role": "user", "content": "hi"}])
        resp = await credential_dispatch.dispatch_upstream(
            cp.CookieProxyUpstream(), req, base_req, is_cookie_channel=True
        )
        assert resp.status_code == 200
        return _collect_sse_text([f async for f in resp.body_iterator])

    assert asyncio.run(run()) == "recovered-on-c3"
    assert c2_cancelled.is_set()

