# -*- coding: utf-8 -*-
"""思维链守卫（疑问3）：让模型别跳过预设思维链直接写正文。"""
from message_processing import (
    apply_prefill_compat,
    build_cot_guard,
    detect_unclosed_tag,
)
from models import OpenAIMessage


# ---------- 未闭合标签识别 ----------

def test_detect_unclosed_basic_any_tag_name():
    """通用性：不预设任何具体标签名，各式各样的自定义标签都要能识别。"""
    cases = {
        "<thinking>": "thinking",
        "<CoT>": "CoT",
        "<Chain_of_Thought>": "Chain_of_Thought",
        "准备开始\n<plan_1>": "plan_1",
        "<analysis~>": "analysis~",
        "<my.step:2>": "my.step:2",
        "<a-b_c~>": "a-b_c~",
    }
    for text, expect in cases.items():
        assert detect_unclosed_tag(text) == expect, text


def test_detect_ignores_closed_tags():
    assert detect_unclosed_tag("<a>x</a>") is None
    assert detect_unclosed_tag("<a>x</a><b>") == "b"          # 取最后一个未闭合的
    assert detect_unclosed_tag("<outer><inner>x</inner>") == "outer"


def test_detect_no_tag_cases():
    assert detect_unclosed_tag("") is None
    assert detect_unclosed_tag("普通句子，没有标签。") is None
    assert detect_unclosed_tag(None) is None


def test_guard_text_mentions_tag_and_order():
    for tag in ["thinking", "CoT", "plan_1", "分析~"]:
        g = build_cot_guard(tag)
        assert f"<{tag}>" in g and f"</{tag}>" in g
        assert "先" in g and "正文" in g


# ---------- 接入两种预填充模式 ----------

def _msgs(prefill):
    return [OpenAIMessage(role="user", content="继续剧情"),
            OpenAIMessage(role="assistant", content=prefill)]


# 典型形态：一段完整句子 + 一个未闭合的思维链开标签（标签名任取，这里用中性示例）
PREFILL = "好的，我先梳理一下。\n<thinking>"


def test_smart_appends_guard_after_prefill():
    new_msgs, prefill, active = apply_prefill_compat(_msgs(PREFILL), "smart", cot_guard=True)
    assert active and prefill == PREFILL
    tail = new_msgs[-1].content
    assert "格式硬性要求" in tail
    # 守卫必须排在预填充文本之后：模型最后读到的就是这条要求
    assert tail.index(PREFILL) < tail.index("格式硬性要求")
    assert "</thinking>" in tail


def test_keep_turn_appends_guard_to_nudge():
    new_msgs, prefill, active = apply_prefill_compat(_msgs(PREFILL), "keep_turn", cot_guard=True)
    assert active and prefill == PREFILL
    assert new_msgs[-2].role == "assistant"          # 预填充仍留在 model 轮次
    assert new_msgs[-1].role == "user"
    assert "</thinking>" in new_msgs[-1].content


def test_guard_off_by_default_flag():
    new_msgs, _, _ = apply_prefill_compat(_msgs(PREFILL), "smart", cot_guard=False)
    assert "格式硬性要求" not in new_msgs[-1].content


def test_guard_noop_without_unclosed_tag():
    """预填充只是普通句子（无未闭合标签）→ 守卫不应插话。"""
    new_msgs, _, _ = apply_prefill_compat(_msgs("客官里边请，"), "smart", cot_guard=True)
    assert "格式硬性要求" not in new_msgs[-1].content


def test_guard_respects_custom_template():
    new_msgs, _, _ = apply_prefill_compat(_msgs(PREFILL), "smart", cot_guard=True,
                                          instruction_template="【自定义】接着写：")
    tail = new_msgs[-1].content
    assert "【自定义】接着写：" in tail and "格式硬性要求" in tail


def test_guard_not_applied_when_model_last_allowed():
    """2.5 系原生透传（消息不改），没有可插守卫的位置，行为保持不变。"""
    msgs = _msgs(PREFILL)
    new_msgs, prefill, active = apply_prefill_compat(msgs, "smart", allow_model_last=True,
                                                     cot_guard=True)
    assert new_msgs is msgs and prefill == PREFILL and active is True


def test_off_and_minimal_modes_unaffected():
    out = apply_prefill_compat(_msgs(PREFILL), "off", cot_guard=True)
    assert out[0] is not None and out[2] is False
    new_msgs, prefill, active = apply_prefill_compat(_msgs(PREFILL), "minimal", cot_guard=True)
    assert active is True and prefill == ""          # minimal 不拼回预填充
    assert "格式硬性要求" not in new_msgs[-1].content
