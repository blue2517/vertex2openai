# -*- coding: utf-8 -*-
"""控制台前端静态审计。

控制台是单文件内联 HTML+JS，没有构建步骤也没有 lint，因此很容易出现
「加了 ? 按钮却忘了写说明块」「删了输入框但 JS 还在读它」这类只有点开才发现的问题
（都真实发生过）。这里把这些不变量固化成测试。
"""
import re

import main


HTML = main.DASHBOARD_HTML
SCRIPTS = "\n".join(re.findall(r"<script>(.*?)</script>", HTML, re.DOTALL))
# 去掉 <script> 段，剩下的才是标记部分（避免把 JS 里的字符串当成 DOM）
MARKUP = re.sub(r"<script>.*?</script>", "", HTML, flags=re.DOTALL)
ELEMENT_IDS = set(re.findall(r'id="([^"]+)"', MARKUP))


def test_every_help_toggle_has_a_help_box():
    """每个 ? 按钮都必须有对应的说明块，否则点了没反应。"""
    referenced = set(re.findall(r"hlp\(this,\s*'([^']+)'\)", HTML))
    assert referenced, "没找到任何帮助按钮，选择器可能改了"
    missing = sorted(referenced - ELEMENT_IDS)
    assert not missing, f"这些 ? 按钮没有对应的说明块：{missing}"


def test_every_help_box_is_reachable():
    """反向检查：写了说明块却没有按钮能打开它，属于死代码。"""
    boxes = set(re.findall(r'id="(h_[^"]+)"[^>]*class="helpbox"', MARKUP))
    referenced = set(re.findall(r"hlp\(this,\s*'([^']+)'\)", HTML))
    orphans = sorted(boxes - referenced)
    assert not orphans, f"这些说明块没有任何按钮能打开：{orphans}"


def _js_referenced_ids() -> set:
    """JS 里所有"按 id 取元素"的写法。

    不能只看 $('x')：控制台还通过 numOrNull('x') / numOr('x', d) 这类辅助函数，
    以及 [...].forEach(k => setV(k, s[k])) 的数组按 id 批量读写——
    漏掉这些的话，删掉一个输入框而 JS 仍在读它，审计会放过去（真踩过）。
    """
    ids = set(re.findall(r"\$\('([^']+)'\)", SCRIPTS))
    ids |= set(re.findall(r"numOrNull\('([^']+)'\)", SCRIPTS))
    ids |= set(re.findall(r"numOr\('([^']+)'", SCRIPTS))
    # setV 批量列表：['a','b',...].forEach(k=>setV(k,...))
    for arr in re.findall(r"\[([^\]]*?)\]\.forEach\(k=>setV\(", SCRIPTS):
        ids |= set(re.findall(r"'([^']+)'", arr))
    return ids


def test_js_only_touches_existing_elements():
    """JS 按 id 读写的元素必须真的存在（删控件时最容易漏改 JS）。"""
    missing = sorted(_js_referenced_ids() - ELEMENT_IDS)
    assert not missing, f"JS 引用了不存在的元素：{missing}"


def test_settings_keys_in_js_exist_in_backend():
    """控制台读写的设置键必须都是后端认识的键，否则静默丢设置。"""
    import config as app_config
    known = set(app_config.DEFAULT_SETTINGS)
    # 全局保存与按模型保存两处 patch 里出现的 key: 形式
    patch_keys = set(re.findall(r"^\s*([a-z_]+):\$\('", SCRIPTS, re.MULTILINE))
    unknown = sorted(patch_keys - known)
    assert not unknown, f"控制台在保存后端不认识的键：{unknown}"


def test_no_removed_project_id_field():
    """标准模式 Project ID 输入框已按需求删除，统一用「通道与凭证」里的那个。"""
    assert "express_project_id" not in HTML


def test_native_thinking_note_is_model_agnostic():
    """原生思考说明对所有模型都要成立，不能写死某个具体型号。"""
    note = re.search(r"🎭.*?</p>", HTML, re.DOTALL)
    assert note, "没找到原生思考的说明段落"
    body = note.group(0)
    for hardcoded in ["3.6-flash", "3.7-flash", "2.5-pro"]:
        assert hardcoded not in body, f"说明里写死了具体型号：{hardcoded}"


def test_help_boxes_wrap_long_tokens_on_mobile():
    """说明里会出现很长的资源路径/URL，必须允许断行，否则手机上会撑破卡片。"""
    assert "overflow-wrap:anywhere" in HTML
    assert re.search(r"\.helpbox[^{]*\{[^}]*", HTML)


def test_sampling_inputs_stack_on_narrow_screens():
    """三个采样输入在窄屏要能竖排，否则挤成一团。"""
    assert "grid-cols-1 sm:grid-cols-3" in HTML


def test_selects_do_not_use_fixed_width():
    """下拉用 max-width 而不是固定 width，窄屏才不会溢出。"""
    for sid in ["cookie_tool_policy", "express_location", "sampling_policy"]:
        m = re.search(rf'<select id="{sid}"[^>]*>', HTML)
        assert m, f"没找到下拉 {sid}"
        assert "width:" not in m.group(0) or "max-width" in m.group(0), \
            f"{sid} 用了固定宽度，窄屏可能溢出：{m.group(0)}"


def test_location_select_has_global_and_regions():
    m = re.search(r'<select id="express_location".*?</select>', HTML, re.DOTALL)
    assert m
    block = m.group(0)
    assert '<option value="global"' in block
    assert '<option value=""' in block          # 保留“后端自选”旧行为
    regions = re.findall(r'<option value="([a-z]+-[a-z]+\d+)"', block)
    assert len(regions) >= 20, f"区域太少：{len(regions)}"
