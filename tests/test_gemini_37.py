# -*- coding: utf-8 -*-
"""gemini-3.7-flash 接入：能力档案与思考档位。

依据 intro_gemini_3_7_flash.ipynb（核对于 2026-08-14）：
默认思考档位 medium；档位表只列 LOW / MEDIUM / HIGH（未提及 MINIMAL）；
thinking_level 不可与 legacy thinking_budget 同时出现（否则 400）。
"""
import json
from pathlib import Path

import model_capabilities as mc
from models import OpenAIRequest, OpenAIMessage


def _req(model="gemini-3.7-flash", **kw):
    return OpenAIRequest(model=model, messages=[OpenAIMessage(role="user", content="hi")], **kw)


def test_37_in_model_list():
    data = json.loads((Path(__file__).resolve().parent.parent / "vertexModels.json").read_text("utf-8"))
    assert "gemini-3.7-flash" in data["models"]


def test_37_profile_matches_official_doc():
    p = mc.get_profile("gemini-3.7-flash")
    assert p["family"] == "g3" and p["thinking_kind"] == "level"
    assert p["default_level"] == "medium"                      # 官方：默认 medium
    assert mc.sort_levels(p["thinking_levels"]) == ["low", "medium", "high"]
    assert "minimal" not in p["thinking_levels"]               # 官方档位表未提供 MINIMAL
    # 3.6 起采样弃用 → 3.7 也应剥离
    assert "temperature" not in p["allowed_sampling"]
    assert "top_p" not in p["allowed_sampling"]
    assert "candidate_count" not in p["allowed_sampling"]
    assert p["requires_user_last_turn"] is True


def test_36_keeps_minimal_unchanged():
    """3.6-flash 的 MINIMAL 已真机验证可用，不能被这次改动波及。"""
    p = mc.get_profile("gemini-3.6-flash")
    assert "minimal" in p["thinking_levels"]
    assert p["default_level"] == "medium"


def test_37_off_mode_floors_to_low_not_minimal():
    """关闭原生思考：3.7 没有 minimal，应就近取 low 而不是发不支持的枚举。"""
    t = mc.resolve_thinking("gemini-3.7-flash", _req(reasoning_effort="xhigh"),
                            {"native_thinking_mode": "off"})
    assert t == {"mode": "level", "level": "low", "include_thoughts": False}


def test_37_console_level_clamped():
    t = mc.resolve_thinking("gemini-3.7-flash", _req(),
                            {"native_thinking_mode": "console", "thinking_g3_level": "minimal"})
    assert t["level"] == "low"          # minimal 不合法 → 就近向上取 low
    t2 = mc.resolve_thinking("gemini-3.7-flash", _req(),
                             {"native_thinking_mode": "console", "thinking_g3_level": "high"})
    assert t2["level"] == "high"


def test_37_never_sends_budget_with_level():
    """官方：thinking_level 与 thinking_budget 混用会 400 —— 3.x 只能走 level 分支。"""
    t = mc.resolve_thinking("gemini-3.7-flash", _req(), {})
    assert t["mode"] == "level" and "budget" not in t


def test_future_model_forward_safe_no_minimal():
    p = mc.get_profile("gemini-4.1-flash")
    assert "minimal" not in p["thinking_levels"]    # 未知新型号不冒 400 风险
    assert "temperature" not in p["allowed_sampling"]
