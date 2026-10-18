import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.background import BackgroundTask

import api_helpers
from api_errors import UpstreamError, error_payload, error_sse, upstream_error, prepare_stream_response, error_code_for_status
from auth import get_api_key
from models import OpenAIRequest
from request_lifecycle import DisconnectMiddleware
from resumable import ResumableBuffer, ResumableStore, pump_stream
from routes import chat_api
from upstreams import cookie_proxy


def sse_payloads(frames):
    text = ''.join(frame.decode() if isinstance(frame, bytes) else frame for frame in frames)
    return [json.loads(line[5:]) for line in text.splitlines()
            if line.startswith('data:') and line[5:].strip() != '[DONE]']


@pytest.fixture
def studio(monkeypatch):
    monkeypatch.setattr(cookie_proxy, '_get_cookie_string', lambda: 'TEST_COOKIE')
    monkeypatch.setattr(cookie_proxy, '_get_project_id', lambda: 'TEST_PROJECT')
    monkeypatch.setattr(cookie_proxy, 'build_headers', lambda _: {'cookie': 'TEST_COOKIE'})
    monkeypatch.setattr(cookie_proxy, 'get_proxy_client_args', lambda: {'trust_env': False})
    monkeypatch.setattr(cookie_proxy, 'get_retry_settings', lambda: (0, 0))
    monkeypatch.setattr(cookie_proxy, 'build_batch_graphql_body_async', AsyncMock(return_value={'variables': {}}))
    state = SimpleNamespace(payload={}, status=200, calls=0)
    client_class = httpx.AsyncClient

    def handler(request):
        state.calls += 1
        return httpx.Response(state.status, json=state.payload)

    def factory(**kwargs):
        return client_class(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cookie_proxy.httpx, 'AsyncClient', factory)
    state.request = OpenAIRequest(model='gemini-4-argon', messages=[{'role': 'user', 'content': 'hello'}])
    state.connection = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

    async def call(stream=False, image=False):
        state.request.stream = stream
        if image:
            state.request.model = 'gemini-3.6-flash-image'
        response = await cookie_proxy.CookieProxyUpstream().chat_completions(state.request, state.connection)
        if stream:
            return [frame async for frame in response.body_iterator]
        return response

    state.call = call
    return state


@pytest.mark.parametrize('code,status', [(3, 400), (7, 403), (8, 429), (16, 401), (503, 503)])
@pytest.mark.parametrize('stream', [False, True])
def test_studio_graphql_errors_never_become_assistant_content(studio, code, status, stream):
    studio.payload = {'results': [{'errors': [{'code': code, 'message': 'upstream failed'}]}]}
    result = asyncio.run(studio.call(stream=stream))
    if stream:
        payloads = sse_payloads(result)
        error = next(p['error'] for p in payloads if 'error' in p)
        assert all('choices' not in p for p in payloads)
    else:
        assert result.status_code == status
        payload = json.loads(result.body)
        assert 'choices' not in payload
        error = payload['error']
    assert error['code'] == error_code_for_status(status)
    assert error['message'] == 'upstream failed'
    assert studio.calls == 1


@pytest.mark.parametrize('stream,image', [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize('parts,feedback,status', [
    ([], {}, 502),
    ([{'text': 'private reasoning', 'thought': True}], {}, 502),
    ([], {'blockReason': 'SAFETY'}, 400),
])
def test_studio_empty_or_blocked_response_is_error(studio, stream, image, parts, feedback, status):
    studio.payload = {'results': [{'data': {'candidates': [{'content': {'parts': parts}}], 'promptFeedback': feedback}}]}
    result = asyncio.run(studio.call(stream=stream, image=image))
    if stream:
        payloads = sse_payloads(result)
        error = next(p['error'] for p in payloads if 'error' in p)
        assert not any(c.get('delta', {}).get('content') for p in payloads for c in p.get('choices', []))
        assert error['code'] in ('content_filter', 'empty_response')
    else:
        assert result.status_code == status
        assert 'choices' not in json.loads(result.body)


def test_studio_error_after_partial_text_is_separate_event(studio, monkeypatch):
    async def events(*args, **kwargs):
        yield 'text', 'actual model text'
        yield 'api_error', UpstreamError(503, 'upstream failed')

    monkeypatch.setattr(cookie_proxy, '_execute_stream_request_generator', events)
    payloads = sse_payloads(asyncio.run(studio.call(stream=True)))
    content = ''.join(c['delta'].get('content') or '' for p in payloads for c in p.get('choices', []))
    assert content == 'actual model text'
    assert payloads[-1]['error']['code'] == error_code_for_status(503)
    assert not any(c.get('finish_reason') == 'stop' for p in payloads for c in p.get('choices', []))


@pytest.mark.parametrize('status', [401, 403, 429, 503])
def test_studio_image_http_error_preserves_status(studio, status):
    studio.status = status
    studio.payload = {'error': {'message': 'upstream failed'}}
    payloads = sse_payloads(asyncio.run(studio.call(stream=True, image=True)))
    assert payloads == [error_payload(UpstreamError(status, 'upstream failed'))]


@pytest.mark.parametrize('split', [False, True])
def test_error_stream_is_not_cached_as_success(split):
    async def scenario():
        buf = ResumableBuffer('test', kind='stream')
        frame = error_sse(UpstreamError(429, 'upstream failed'))

        async def frames():
            if split:
                yield frame[:20]
                yield frame[20:]
            else:
                yield frame
            yield 'data: [DONE]\n\n'

        await pump_stream(buf, frames())
        assert buf.status == 'error'
        assert buf.status_code == 429
        assert buf.error == 'upstream failed'
        assert sum('[DONE]' in frame for frame in buf.events) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize('fake', [False, True])
@pytest.mark.parametrize('partial', [False, True])
def test_express_stream_errors_preserve_status_without_content(monkeypatch, isolated_state, fake, partial):
    isolated_state.update_settings({'fake_streaming': fake, 'fake_streaming_interval': 0})
    monkeypatch.setattr(api_helpers, 'get_retry_settings', lambda: (0, 0))
    error = UpstreamError(429, 'upstream failed')

    async def chunks():
        if partial:
            yield SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text='actual text', thought=False)]))])
        raise error

    models = SimpleNamespace(generate_content=AsyncMock(side_effect=error),
                             generate_content_stream=AsyncMock(return_value=chunks()))
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    request = OpenAIRequest(model='gemini-4-argon', stream=True, messages=[{'role': 'user', 'content': 'hello'}])

    async def scenario():
        response = await api_helpers.execute_gemini_call(client, request.model, lambda _: [], {}, request)
        return [frame async for frame in response.body_iterator]

    payloads = sse_payloads(asyncio.run(scenario()))
    assert payloads[-1]['error']['code'] == error_code_for_status(429)
    content = ''.join(c['delta'].get('content') or '' for p in payloads for c in p.get('choices', []))
    assert content == ('actual text' if partial and not fake else '')


@pytest.mark.parametrize('buffered', [False, True])
@pytest.mark.parametrize('status', [400, 401, 403, 429, 502, 503, 504])
def test_http_route_and_buffer_preserve_error_status(monkeypatch, isolated_state, buffered, status):
    isolated_state.update_settings({'resumable_enabled': buffered})
    app = FastAPI()
    app.include_router(chat_api.router)
    app.add_middleware(DisconnectMiddleware)
    app.dependency_overrides[get_api_key] = lambda: 'test-key'
    store = ResumableStore()
    monkeypatch.setattr(chat_api, 'resumable_store', store)

    async def fail(*args):
        raise UpstreamError(status, 'upstream failed')

    monkeypatch.setattr(chat_api, '_call_upstream', fail)
    with TestClient(app) as client:
        response = client.post('/v1/chat/completions', json={'model': 'gemini-4-argon', 'messages': [{'role': 'user', 'content': 'hello'}]})
    assert response.status_code == status
    assert 'choices' not in response.json()
    assert response.json()['error']['code'] == error_code_for_status(status)
    assert response.headers['x-should-retry'] == 'false'
    assert response.headers['cache-control'] == 'no-store'
    if buffered:
        assert next(iter(store._buffers.values())).status == 'error'


@pytest.mark.parametrize('error,status', [(httpx.ReadTimeout(''), 504), (httpx.RemoteProtocolError('private details'), 502)])
def test_transport_errors_have_short_messages(error, status):
    error = upstream_error(error)
    payload = error_payload(error)
    assert error.status_code == status
    assert payload['error']['message']
    assert 'private details' not in payload['error']['message']
    assert 'choices' not in payload


@pytest.mark.parametrize('stream', [False, True])
def test_studio_success_content_is_unchanged(studio, stream):
    studio.payload = {'results': [{'data': {'candidates': [{'content': {'parts': [{'text': 'actual model text'}]}, 'finishReason': 'STOP'}]}}]}
    result = asyncio.run(studio.call(stream=stream))
    if stream:
        payloads = sse_payloads(result)
        assert not any('error' in p for p in payloads)
        assert ''.join(c['delta'].get('content') or '' for p in payloads for c in p.get('choices', [])) == 'actual model text'
    else:
        assert result.status_code == 200
        assert json.loads(result.body)['choices'][0]['message']['content'] == 'actual model text'


@pytest.mark.parametrize('stream,fake', [(False, False), (True, False), (True, True)])
def test_express_empty_response_is_an_error(monkeypatch, isolated_state, stream, fake):
    isolated_state.update_settings({'fake_streaming': fake, 'fake_streaming_interval': 0})
    monkeypatch.setattr(api_helpers, 'get_retry_settings', lambda: (0, 0))
    empty = SimpleNamespace(candidates=[])

    async def chunks():
        yield empty

    models = SimpleNamespace(generate_content=AsyncMock(return_value=empty),
                             generate_content_stream=AsyncMock(return_value=chunks()))
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    request = OpenAIRequest(model='gemini-4-argon', stream=stream, messages=[{'role': 'user', 'content': 'hello'}])

    async def scenario():
        response = await api_helpers.execute_gemini_call(client, request.model, lambda _: [], {}, request)
        return sse_payloads([frame async for frame in response.body_iterator])

    if stream:
        assert asyncio.run(scenario())[-1]['error']['code'] == 'empty_response'
    else:
        with pytest.raises(UpstreamError) as exc:
            asyncio.run(scenario())
        assert exc.value.status_code == 502


@pytest.mark.parametrize('case,status', [('authentication', 401), ('validation', 422), ('not_found', 404)])
def test_v1_authentication_and_validation_use_error_envelope(client, monkeypatch, case, status):
    import auth
    monkeypatch.setattr(auth, 'API_KEY', 'test-secret')
    if case == 'not_found':
        response = client.get('/v1/missing')
    else:
        response = client.post('/v1/chat/completions', json={'model': 'gemini-4-argon'},
                               headers={'Authorization': 'Bearer test-secret'} if case == 'validation' else {})
    assert response.status_code == status
    assert set(response.json()) == {'error'}
    assert response.json()['error']['code'] == error_code_for_status(status)
    assert 'test-secret' not in response.text


@pytest.mark.parametrize('stream,fake', [(False, False), (True, False), (True, True)])
def test_express_success_and_unspecified_block_reason_are_preserved(monkeypatch, isolated_state, stream, fake):
    isolated_state.update_settings({'fake_streaming': fake, 'fake_streaming_interval': 0})
    raw = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text='actual text', thought=False)]), finish_reason='STOP')],
                          prompt_feedback=SimpleNamespace(block_reason='BLOCKED_REASON_UNSPECIFIED'))

    async def chunks():
        yield raw

    models = SimpleNamespace(generate_content=AsyncMock(return_value=raw),
                             generate_content_stream=AsyncMock(return_value=chunks()))
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    request = OpenAIRequest(model='gemini-4-argon', stream=stream, messages=[{'role': 'user', 'content': 'hello'}])

    async def scenario():
        response = await api_helpers.execute_gemini_call(client, request.model, lambda _: [], {}, request)
        if not stream:
            assert response.status_code == 200
            assert json.loads(response.body)['choices'][0]['message']['content'] == 'actual text'
        else:
            payloads = sse_payloads([frame async for frame in response.body_iterator])
            assert not any('error' in p for p in payloads)
            assert ''.join(c['delta'].get('content') or '' for p in payloads for c in p.get('choices', [])) == 'actual text'

    asyncio.run(scenario())


@pytest.mark.parametrize('buffered', [False, True])
@pytest.mark.parametrize('image', [False, True])
@pytest.mark.parametrize('http_status,code,status', [(401, 401, 401), (403, 403, 403), (200, 7, 403), (200, 16, 401)])
def test_studio_auth_error_is_one_upstream_call_and_real_http_error(
    studio, monkeypatch, isolated_state, buffered, image, http_status, code, status, capsys,
):
    isolated_state._state['use_web_proxy'] = True
    isolated_state.update_settings({'resumable_enabled': buffered})
    studio.status = http_status
    studio.payload = {'error': {'code': code, 'message': 'Permission denied; quota; try again later; invalid functionDeclarations'}}
    monkeypatch.setattr(cookie_proxy, 'get_retry_settings', lambda: (5, 0))
    retry = Mock()
    monkeypatch.setattr(cookie_proxy.stats, 'add_retry', retry)
    app = FastAPI()
    app.include_router(chat_api.router)
    app.add_middleware(DisconnectMiddleware)
    app.dependency_overrides[get_api_key] = lambda: 'test-key'
    monkeypatch.setattr(chat_api, 'resumable_store', ResumableStore())
    body = {'model': 'gemini-3.6-flash-image' if image else 'gemini-4-argon', 'stream': True,
            'messages': [{'role': 'user', 'content': 'hello'}],
            'tools': [{'type': 'function', 'function': {'name': 'emit', 'parameters': {'type': 'object'}}}]}
    with TestClient(app) as client:
        response = client.post('/v1/chat/completions', json=body)
    assert response.status_code == status
    assert response.headers['content-type'].startswith('application/json')
    assert response.json()['error']['code'] == error_code_for_status(status)
    assert response.headers['x-should-retry'] == 'false'
    assert 'choices' not in response.json()
    assert studio.calls == 1
    retry.assert_not_called()
    logs = capsys.readouterr().out
    assert '项目层面' not in logs
    assert 'Cookie 通常' not in logs
    assert '重取 Cookie' not in logs


@pytest.mark.parametrize('status', [401, 403, 429, 503, 504])
@pytest.mark.parametrize('split', [False, True])
def test_stream_bootstrap_returns_error_before_committing_200(status, split):
    async def scenario():
        closed = False

        async def frames():
            nonlocal closed
            try:
                yield ': keep-alive\n\n'
                yield 'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
                yield 'data: {"choices":[{"delta":{"content":""}}]}\n\n'
                frame = error_sse(UpstreamError(status, '认证失败')).replace('\n', '\r\n').encode()
                if split:
                    for i in range(0, len(frame), 3):
                        yield frame[i:i+3]
                else:
                    yield frame
                yield 'data: [DONE]\n\n'
            finally:
                closed = True

        response = await prepare_stream_response(StreamingResponse(frames(), media_type='text/event-stream'))
        assert response.status_code == status
        assert json.loads(response.body)['error']['message'] == '认证失败'
        assert closed

    asyncio.run(scenario())


@pytest.mark.parametrize('status', [400, 401, 403, 404, 422, 429, 499, 500, 502, 503, 504])
def test_error_codes_are_strings_and_survive_sse_round_trip(status):
    original = UpstreamError(status, 'upstream failed')
    payload = error_payload(original)
    assert isinstance(payload['error']['code'], str)
    assert payload['error']['param'] is None
    parsed = upstream_error(payload)
    assert parsed.status_code == status
    assert parsed.code == payload['error']['code']


@pytest.mark.parametrize('http_status,code,status', [(401, 401, 401), (403, 403, 403), (200, 7, 403), (200, 8, 429)])
def test_nonstream_errors_log_a_correlated_terminal_status(studio, capsys, http_status, code, status):
    studio.status = http_status
    studio.payload = {'error': {'code': code, 'message': 'upstream failed'}}
    response = asyncio.run(studio.call())
    assert response.status_code == status
    assert response.headers['x-should-retry'] == 'false'
    assert json.loads(response.body)['error']['code'] == error_code_for_status(status)
    logs = capsys.readouterr().out.splitlines()
    started = next(line for line in logs if line.startswith('→ [Studio]'))
    request_id = started.split('请求=', 1)[1].split(' |', 1)[0]
    terminal = [line for line in logs if '本次请求已结束' in line]
    assert len(terminal) == 1
    assert f'请求={request_id}' in terminal[0]
    assert f'HTTP {status}' in terminal[0]
    assert studio.calls == 1


@pytest.mark.parametrize('buffered', [False, True])
def test_error_after_real_output_stays_an_independent_sse_event(monkeypatch, isolated_state, buffered):
    isolated_state.update_settings({'resumable_enabled': buffered})
    app = FastAPI()
    app.include_router(chat_api.router)
    app.add_middleware(DisconnectMiddleware)
    app.dependency_overrides[get_api_key] = lambda: 'test-key'
    monkeypatch.setattr(chat_api, 'resumable_store', ResumableStore())
    finished = Mock()
    real = 'data: {"choices":[{"delta":{"content":"actual text"}}]}\n\n'
    packet = (real + error_sse(UpstreamError(403, '权限失败'))).encode()
    cut = packet.index('权限'.encode()) + 1

    async def upstream(*args):
        async def frames():
            yield ': keep-alive\n\n'
            yield packet[:cut]
            yield packet[cut:]
            yield 'data: [DONE]\n\n'
        return StreamingResponse(frames(), media_type='text/event-stream', background=BackgroundTask(finished))

    monkeypatch.setattr(chat_api, '_call_upstream', upstream)
    with TestClient(app) as client:
        response = client.post('/v1/chat/completions', json={'model': 'gemini-4-argon', 'stream': True,
                               'messages': [{'role': 'user', 'content': 'hello'}]})
    assert response.status_code == 200
    payloads = sse_payloads([response.text])
    assert payloads[-1]['error']['code'] == error_code_for_status(403)
    assert payloads[-1]['error']['message'] == '权限失败'
    assert ''.join(c['delta'].get('content') or '' for p in payloads for c in p.get('choices', [])) == 'actual text'
    finished.assert_called_once()


@pytest.mark.parametrize('buffered', [False, True])
def test_early_stream_error_closes_iterator_and_background_once(monkeypatch, isolated_state, buffered):
    isolated_state.update_settings({'resumable_enabled': buffered})
    app = FastAPI()
    app.include_router(chat_api.router)
    app.add_middleware(DisconnectMiddleware)
    app.dependency_overrides[get_api_key] = lambda: 'test-key'
    monkeypatch.setattr(chat_api, 'resumable_store', ResumableStore())
    finished, closed = Mock(), Mock()

    async def upstream(*args):
        async def frames():
            try:
                yield ': keep-alive\n\n'
                yield error_sse(UpstreamError(403, 'denied'))
                await asyncio.Event().wait()
            finally:
                closed()
        return StreamingResponse(frames(), media_type='text/event-stream', background=BackgroundTask(finished))

    monkeypatch.setattr(chat_api, '_call_upstream', upstream)
    with TestClient(app) as client:
        response = client.post('/v1/chat/completions', json={'model': 'gemini-4-argon', 'stream': True,
                               'messages': [{'role': 'user', 'content': 'hello'}]})
    assert response.status_code == 403
    closed.assert_called_once()
    finished.assert_called_once()


@pytest.mark.parametrize('mode', ['json', 'real_stream', 'fake_stream'])
def test_express_auth_error_never_retries_or_changes_model_route(monkeypatch, isolated_state, mode):
    isolated_state.update_settings({'fake_streaming': mode == 'fake_stream', 'fake_streaming_interval': 0})
    monkeypatch.setattr(api_helpers, 'get_retry_settings', lambda: (5, 0))
    error = UpstreamError(403, 'Permission denied; requires billing; quota; try again later')
    models = SimpleNamespace(generate_content=AsyncMock(side_effect=error),
                             generate_content_stream=AsyncMock(side_effect=error))
    fallback = Mock()
    retry = Mock()
    monkeypatch.setattr(api_helpers.stats, 'add_retry', retry)
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    request = OpenAIRequest(model='gemini-4-argon', stream=mode != 'json', messages=[{'role': 'user', 'content': 'hello'}])

    async def scenario():
        response = await api_helpers.execute_gemini_call(
            client, 'projects/X/locations/global/publishers/google/models/gemini-4-argon',
            lambda _: [], {}, request, fallback_model=request.model, fallback_client_factory=fallback)
        response = await prepare_stream_response(response)
        assert response.status_code == 403

    if mode == 'json':
        with pytest.raises(UpstreamError) as exc:
            asyncio.run(scenario())
        assert exc.value.status_code == 403
    else:
        asyncio.run(scenario())
    if mode == 'real_stream':
        models.generate_content_stream.assert_awaited_once()
    else:
        models.generate_content.assert_awaited_once()
    fallback.assert_not_called()
    retry.assert_not_called()
