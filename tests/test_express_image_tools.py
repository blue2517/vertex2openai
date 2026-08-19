# -*- coding: utf-8 -*-
"""Express 生图模型的工具选择不能偷偷启用搜索。"""
from api_helpers import create_generation_config
import model_capabilities as mc
from models import OpenAIRequest


def _request(**updates):
    data = {
        "model": "gemini-3.1-flash-image",
        "messages": [{"role": "user", "content": "draw a cat"}],
    }
    data.update(updates)
    return OpenAIRequest(**data)


def _tool(name):
    return {"type": "function", "function": {
        "name": name, "description": "test", "parameters": {"type": "object", "properties": {}}}}


def test_plain_image_request_has_no_tools():
    assert "tools" not in create_generation_config(_request())


def test_image_tool_choice_none_disables_all_tools():
    config = create_generation_config(_request(
        tools=[_tool("google_search"), _tool("custom_helper")],
        tool_choice="none",
    ))
    assert "tools" not in config


def test_image_explicit_search_declaration_maps_to_google_search():
    config = create_generation_config(_request(tools=[_tool("google_search")]))
    assert config["tools"] == [{"google_search": {}}]


def test_image_custom_function_is_suppressed_without_enabling_search():
    config = create_generation_config(_request(tools=[_tool("custom_helper")]))
    assert "tools" not in config


def test_express_required_tool_choice_maps_to_any():
    request = OpenAIRequest(
        model="gemini-3.5-flash",
        messages=[{"role": "user", "content": "call it"}],
        tools=[_tool("custom_helper")],
        tool_choice="required",
    )
    config = create_generation_config(request)
    assert config["tool_config"] == {
        "function_calling_config": {"mode": "ANY"}}


def test_flash_image_keeps_official_extended_ratio_but_pro_does_not():
    assert "9:21" in mc.get_profile("gemini-3.1-flash-image")["image_aspect_ratios"]
    assert "9:21" not in mc.get_profile("gemini-3-pro-image")["image_aspect_ratios"]
