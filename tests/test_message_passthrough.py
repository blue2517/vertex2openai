import asyncio
import json
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.responses import JSONResponse, StreamingResponse

import config
import main
import model_capabilities as mc
from message_processing import create_gemini_prompt
from models import OpenAIRequest
from upstreams import cookie_proxy, express_sdk


RETIRED_KEYS = {'prefill_mode', 'prefill_suppress_thinking', 'prefill_instruction',
                'inject_prefill', 'inject_prefill_for_image'}


@pytest.fixture
def legacy_settings(isolated_state, request):
    mode = getattr(request, 'param', 'smart')
    retired = {'prefill_mode': mode, 'prefill_suppress_thinking': True,
               'prefill_instruction': 'SERVER_REWRITE', 'inject_prefill': 'SERVER_ASSISTANT',
               'inject_prefill_for_image': True}
    isolated_state._state['settings'] = {
        **retired,
        'native_thinking_mode': 'request', 'thinking_g3_level': 'low',
        'inject_system_instruction': '', 'express_location': '',
        'model_overrides': {'gemini-4-argon': retired, 'gemini-3.6-flash-image': retired},
    }
    return isolated_state


def caller_request(tail, stream, image):
    messages = [{'role': 'system', 'content': 'CALLER_SYSTEM'}, {'role': 'user', 'content': 'CALLER_USER'}]
    if tail == 'assistant':
        messages.append({'role': 'assistant', 'content': '  <thinking>\n  1. '})
    elif tail == 'assistant_parts':
        messages.append({'role': 'assistant', 'content': [{'type': 'text', 'text': '  <thinking>\n  1. '}]})
    return OpenAIRequest(model='gemini-3.6-flash-image' if image else 'gemini-4-argon',
                         stream=stream, reasoning_effort='high', messages=messages)


@pytest.mark.parametrize('legacy_settings', ['smart', 'minimal', 'keep_turn', 'off'], indirect=True)
@pytest.mark.parametrize('tail', ['user', 'assistant', 'assistant_parts'])
@pytest.mark.parametrize('stream,image', [(False, False), (True, False), (True, True)])
def test_studio_preserves_caller_messages_with_legacy_prefill_settings(
    monkeypatch, legacy_settings, tail, stream, image, capsys,
):
    monkeypatch.setattr(cookie_proxy, '_get_cookie_string', lambda: 'TEST_COOKIE')
    monkeypatch.setattr(cookie_proxy, '_get_project_id', lambda: 'TEST_PROJECT')
    monkeypatch.setattr(cookie_proxy, 'build_headers', lambda _: {'cookie': 'TEST_COOKIE'})
    monkeypatch.setattr(cookie_proxy, 'get_proxy_client_args', lambda: {'trust_env': False})
    monkeypatch.setattr(cookie_proxy, 'get_retry_settings', lambda: (0, 0))
    captured = []
    client_class = httpx.AsyncClient

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'results': [{'data': {'candidates': [{'content': {'parts': [{'text': 'actual reply'}]}, 'finishReason': 'STOP'}]}}]})

    monkeypatch.setattr(cookie_proxy.httpx, 'AsyncClient', lambda **kwargs: client_class(**kwargs, transport=httpx.MockTransport(handler)))
    request = caller_request(tail, stream, image)
    original = request.model_dump()

    async def scenario():
        response = await cookie_proxy.CookieProxyUpstream().chat_completions(
            request, SimpleNamespace(is_disconnected=AsyncMock(return_value=False)))
        if isinstance(response, StreamingResponse):
            return [frame async for frame in response.body_iterator]
        assert response.status_code == 200

    asyncio.run(scenario())
    assert request.model_dump() == original
    assert len(captured) == 1
    variables = captured[0]['variables']
    contents = variables['contents']
    assert [part['role'] for part in contents] == (['user', 'model'] if tail != 'user' else ['user'])
    assert contents[0]['parts'] == [{'text': 'CALLER_USER'}]
    if tail != 'user':
        assert contents[-1]['parts'] == [{'text': '  <thinking>\n  1. '}]
    assert variables['generationConfig']['thinkingConfig'] == {'thinkingLevel': 'HIGH', 'includeThoughts': True}
    assert 'SERVER_' not in json.dumps(variables)
    assert '预填充' not in capsys.readouterr().out


@pytest.mark.parametrize('legacy_settings', ['smart', 'minimal', 'keep_turn', 'off'], indirect=True)
@pytest.mark.parametrize('tail', ['user', 'assistant', 'assistant_parts'])
@pytest.mark.parametrize('stream,image', [(False, False), (True, False), (True, True)])
def test_express_preserves_caller_messages_with_legacy_prefill_settings(
    monkeypatch, legacy_settings, tail, stream, image, capsys,
):
    monkeypatch.setattr(express_sdk.genai, 'Client', Mock(return_value=SimpleNamespace()))
    monkeypatch.setattr(express_sdk, '_log_resolved_endpoint', lambda _: None)
    monkeypatch.setattr(express_sdk, 'get_http_options', lambda: {})
    execute = AsyncMock(return_value=JSONResponse(content={'ok': True}))
    monkeypatch.setattr(express_sdk, 'execute_gemini_call', execute)
    manager = SimpleNamespace(get_total_keys=lambda: 1,
                              get_express_record=lambda: {'key': 'TEST_KEY', 'project_id': 'TEST_PROJECT'})
    connection = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(express_key_manager=manager)))
    request = caller_request(tail, stream, image)
    original = request.model_dump()
    asyncio.run(express_sdk.ExpressSDKUpstream().chat_completions(request, connection))
    assert request.model_dump() == original
    args = execute.await_args.args
    assert args[4] is request
    assert args[4].model_dump() == original
    contents = args[2](request.messages)
    assert [part.role for part in contents] == (['user', 'model'] if tail != 'user' else ['user'])
    if tail != 'user':
        assert contents[-1].parts[0].text == '  <thinking>\n  1. '
    assert args[3]['thinking_config']['include_thoughts'] is True
    assert args[3]['thinking_config']['thinking_level'] == 'high'
    assert 'SERVER_' not in str(args[3])
    assert '预填充' not in capsys.readouterr().out


def test_retired_settings_are_ignored_by_defaults_and_ui(legacy_settings):
    assert RETIRED_KEYS.isdisjoint(config.DEFAULT_SETTINGS)
    assert RETIRED_KEYS.isdisjoint(config.PER_MODEL_KEYS)
    assert RETIRED_KEYS.isdisjoint(legacy_settings.get_settings())
    assert RETIRED_KEYS.isdisjoint(legacy_settings.get_effective_settings('gemini-4-argon'))
    assert all(key not in main.DASHBOARD_HTML for key in RETIRED_KEYS)
    assert '[预填充兼容]' not in main.DASHBOARD_HTML


def test_empty_messages_do_not_get_a_synthetic_user_instruction():
    assert create_gemini_prompt([]) == []
    assert create_gemini_prompt([SimpleNamespace(role='system', content='CALLER_SYSTEM', tool_calls=None)]) == []


def test_native_thinking_only_follows_explicit_parameters(legacy_settings):
    for tail in ('user', 'assistant'):
        request = caller_request(tail, False, False)
        settings = legacy_settings.get_effective_settings(request.model)
        assert mc.resolve_thinking(request.model, request, settings)['include_thoughts'] is True
        assert mc.resolve_thinking(request.model, request, settings)['level'] == 'high'


def test_dashboard_javascript_remains_valid_after_control_removal():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is needed for JavaScript syntax validation')
    scripts = re.findall(r'<script\b[^>]*>(.*?)</script>', main.DASHBOARD_HTML, re.S)
    for source in scripts:
        result = subprocess.run([node, '--check'], input=source, text=True, encoding='utf-8', capture_output=True)
        assert result.returncode == 0, result.stderr
