import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
from google import genai
from google.genai import types

import api_helpers
import main
from models import OpenAIRequest
from upstreams import cookie_proxy, express_sdk
from usage_stats import (
    RECENT_BLOCK_COUNT,
    RECENT_BLOCK_DURATION_MS,
    express_credential_id,
    usage_ledger,
)


def _login(client):
    r = client.post("/api/login", json={"password": "proxy-test-password"})
    assert r.status_code == 200


def test_usage_ledger_persistence_recent_blocks_and_reset(isolated_state):
    now_ms = int(time.time() * 1000)
    cid_a = express_credential_id("KEY_ALPHA_12345678")
    cid_b = "cookie_beta_01"

    # Two successes and one failure for cid_a
    usage_ledger.record_attempt(
        channel="express",
        cred_id=cid_a,
        cred_label="Key #1",
        proxy_label="美国A",
        request_model="gemini-4-carbon",
        upstream_model="gemini-4-carbon",
        mode="stream",
        status_code=200,
        success=True,
        latency_ms=3000,
        ttft_ms=1000,
        prompt_tokens=100,
        completion_tokens=50,
        ts_ms=now_ms - 60_000,
    )
    usage_ledger.record_attempt(
        channel="express",
        cred_id=cid_a,
        cred_label="Key #1",
        proxy_label="美国A",
        request_model="gemini-4-carbon",
        upstream_model="gemini-4-carbon",
        mode="stream",
        status_code=429,
        success=False,
        latency_ms=1500,
        error="RESOURCE_EXHAUSTED",
        ts_ms=now_ms - 30_000,
    )
    # Cancelled race loser (499) must NOT penalize totals or success rate
    assert usage_ledger.record_attempt(
        channel="express",
        cred_id=cid_a,
        cred_label="Key #1",
        status_code=499,
        success=False,
        cancelled=True,
        ts_ms=now_ms - 20_000,
    ) is None

    # One success for cid_b in an older bucket
    usage_ledger.record_attempt(
        channel="cookie",
        cred_id=cid_b,
        cred_label="Cookie主力",
        proxy_label="直连",
        request_model="gemini-4-argon-search",
        upstream_model="gemini-4-argon",
        mode="stream",
        status_code=200,
        success=True,
        latency_ms=2000,
        ttft_ms=800,
        prompt_tokens=200,
        completion_tokens=100,
        ts_ms=now_ms - 3 * RECENT_BLOCK_DURATION_MS,
    )

    summary_a = usage_ledger.get_credential_summary(cid_a, recent_limit=10)
    assert summary_a["history"]["total_requests"] == 2
    assert summary_a["history"]["success_calls"] == 1
    assert summary_a["history"]["failure_calls"] == 1
    assert summary_a["history"]["success_rate"] == 50.0
    assert summary_a["history"]["total_tokens"] == 150
    assert summary_a["history"]["avg_latency_ms"] == 2250
    assert summary_a["history"]["avg_ttft_ms"] == 1000
    assert summary_a["history"]["last_error"] == "RESOURCE_EXHAUSTED"
    assert len(summary_a["recent_status"]["blocks"]) == RECENT_BLOCK_COUNT
    assert summary_a["recent_status"]["has_data"] is True
    assert summary_a["recent_status"]["blocks"][-1]["state"] == "mixed"
    assert len(summary_a["recent_events"]) == 2

    # Reset only cid_a, cid_b stays intact
    usage_ledger.reset_credential(cid_a)
    assert usage_ledger.get_credential_summary(cid_a)["history"]["total_requests"] == 0
    assert usage_ledger.get_credential_summary(cid_b)["history"]["total_requests"] == 1

    # Reset all
    usage_ledger.reset_all()
    assert usage_ledger.get_credential_summary(cid_b)["history"]["total_requests"] == 0


def test_credentials_overview_and_history_apis(client, isolated_state):
    assert client.get("/api/credentials/overview").status_code == 401
    _login(client)

    isolated_state.set_express_keys([
        {"key": "AQ_SECRET_KEY_0001", "note": "主力Key", "project_id": "123456789", "enabled": True, "proxy_mode": "direct"},
    ])
    cid = express_credential_id("AQ_SECRET_KEY_0001")
    usage_ledger.record_attempt(
        channel="express",
        cred_id=cid,
        cred_label="主力Key",
        proxy_label="直连",
        request_model="gemini-4-carbon",
        upstream_model="gemini-4-carbon",
        status_code=200,
        success=True,
        latency_ms=1200,
        ttft_ms=400,
        prompt_tokens=80,
        completion_tokens=40,
    )

    resp = client.get("/api/credentials/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert "AQ_SECRET_KEY_0001" not in resp.text
    assert data["global"]["total_requests"] == 1
    assert data["global"]["success_rate"] == 100.0
    assert len(data["credentials"]) == 1
    card = data["credentials"][0]
    assert card["cred_id"] == cid
    assert card["label"] == "主力Key"
    assert card["proxy_label"] == "直连"
    assert card["history"]["total_requests"] == 1
    assert card["history"]["success_rate"] == 100.0

    hist = client.get(f"/api/credentials/{cid}/history")
    assert hist.status_code == 200
    assert len(hist.json()["recent_events"]) == 1

    r_reset = client.post(f"/api/credentials/{cid}/stats/reset")
    assert r_reset.status_code == 200
    assert client.get(f"/api/credentials/{cid}/history").json()["history"]["total_requests"] == 0

    assert client.post("/api/stats/reset").status_code == 200


def test_express_clean_logs_and_no_safety_html_tampering(monkeypatch, isolated_state, capsys):
    isolated_state._state["settings"] = {"safety_score": True, "inject_system_instruction": "INJECTED_PROMPT"}
    isolated_state.set_express_keys([
        {"key": "KEY_CLEAN_LOG_9999", "note": "", "project_id": "412211867678", "enabled": True, "proxy_mode": "direct"},
    ])
    body = json.dumps({
        "candidates": [{
            "content": {"role": "model", "parts": [{"text": "clean reply"}]},
            "finishReason": "STOP",
            "safetyRatings": [{"category": "HARM_CATEGORY_HATE_SPEECH", "probability": "NEGLIGIBLE", "probabilityScore": 0.01}],
        }],
        "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 8, "totalTokenCount": 20},
    }).encode("utf-8")

    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body)

    monkeypatch.setattr(
        express_sdk,
        "get_http_options",
        lambda: types.HttpOptions(httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))),
    )
    manager = SimpleNamespace(
        get_total_keys=lambda: 1,
        get_express_record=lambda: {"key": "KEY_CLEAN_LOG_9999", "project_id": "412211867678", "index": 0, "proxy_mode": "direct"},
    )
    conn = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(express_key_manager=manager)),
        is_disconnected=AsyncMock(return_value=False),
    )
    req = OpenAIRequest(model="gemini-4-carbon", stream=False, messages=[{"role": "user", "content": "hi"}])

    resp = asyncio.run(express_sdk.ExpressSDKUpstream().chat_completions(req, conn))
    payload = json.loads(resp.body)
    assert payload["choices"][0]["message"]["content"] == "clean reply"
    assert "<details" not in payload["choices"][0]["message"]["content"]

    logs = capsys.readouterr().out
    assert "凭证=Key #1" in logs
    assert "代理=直连" in logs
    assert "gemini-4-carbon" in logs
    assert "消息转换" not in logs
    assert "使用官方 Gemini Express Mode SDK" not in logs
    assert "已钉定 location" not in logs
    assert "算力消耗统计" not in logs

    cid = express_credential_id("KEY_CLEAN_LOG_9999")
    summary = usage_ledger.get_credential_summary(cid)
    assert summary["history"]["total_requests"] == 1
    assert summary["history"]["success_calls"] == 1
    assert summary["history"]["total_tokens"] == 20


def test_dashboard_dark_style_and_no_prose_walls():
    html = main.DASHBOARD_HTML
    assert "--bg: #0b1020;" in html
    assert "--panel: #121a2e;" in html
    assert "cdn.tailwindcss.com" not in html
    assert "chart.js" not in html
    assert "helpbox" not in html
    assert "inject_system_instruction" not in html
    assert "safety_score" not in html
    assert "输出附加安全分" not in html
    assert "附加 system 指令" not in html
    assert "recent-bars" in html
    assert "detailModal" in html
