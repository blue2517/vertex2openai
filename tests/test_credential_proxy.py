import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.responses import StreamingResponse

import config
import network_proxy as proxy
import runtime_state
from express_key_manager import ExpressKeyManager
from models import OpenAIRequest
from test_cookie_credentials import record, login
from upstreams import express_sdk


def test_binding_validation_direct_and_global_inheritance():
    proxy.save_proxy_config('custom', 'http://GLOBAL:8080')
    ident = proxy.add_proxy_entry('Account', 'socks5://USER:PRIVATE_PASSWORD@ACCOUNT:1080')['pool'][0]['id']
    assert proxy.get_credential_proxy_args({}) == {'trust_env': False, 'proxy': 'http://GLOBAL:8080'}
    assert proxy.get_credential_proxy_args({'proxy_mode': 'direct'}) == {'trust_env': False}
    assert proxy.get_credential_proxy_args({'proxy_mode': 'pool', 'proxy_id': ident}) == {'trust_env': False, 'proxy': 'socks5://USER:PRIVATE_PASSWORD@ACCOUNT:1080'}
    assert proxy.get_proxy_client_args()['proxy'] == 'http://GLOBAL:8080'


@pytest.mark.parametrize('kind', ['cookie', 'key'])
def test_bound_proxy_delete_protection_includes_disabled_credentials(kind):
    ident = proxy.add_proxy_entry('Account', 'http://ACCOUNT:8080')['pool'][0]['id']
    state = runtime_state.app_state
    if kind == 'cookie':
        state.set_cookie_credentials([record(enabled=False, proxy_mode='pool', proxy_id=ident)])
    else:
        state.set_express_keys([{'key': 'PRIVATE_KEY', 'enabled': False, 'proxy_mode': 'pool', 'proxy_id': ident}])
    assert proxy.get_proxy_status()['bound_credentials'] == {ident: 1}
    with pytest.raises(ValueError):
        proxy.delete_proxy_entry(ident)
    if kind == 'cookie':
        state.set_cookie_credentials([])
    else:
        state.set_express_keys([])
    proxy.delete_proxy_entry(ident)
    assert proxy.get_proxy_status()['pool'] == []


def test_binding_is_persisted_and_kept_when_key_project_is_updated():
    ident = proxy.add_proxy_entry('Account', 'http://ACCOUNT:8080')['pool'][0]['id']
    state = runtime_state.app_state
    state.set_express_keys([{'key': 'PRIVATE_KEY', 'proxy_mode': 'pool', 'proxy_id': ident}])
    state.update_express_key_project('PRIVATE_KEY', 'new-project')
    loaded = runtime_state.AppState().get_express_keys()[0]
    assert loaded['proxy_mode'] == 'pool' and loaded['proxy_id'] == ident
    assert loaded['project_id'] == 'new-project'


@pytest.mark.parametrize('kind', ['cookie', 'key'])
def test_invalid_proxy_binding_rejected_at_save(client, kind):
    login(client)
    path = '/api/cookie-credentials' if kind == 'cookie' else '/api/express-keys'
    payload = {'credentials': [record(proxy_mode='pool', proxy_id='missing')]} if kind == 'cookie' else {'keys': [{'key': 'PRIVATE_KEY', 'proxy_mode': 'pool', 'proxy_id': 'missing'}]}
    response = client.post(path, json=payload)
    assert response.status_code == 400
    assert 'PRIVATE_' not in response.text


def test_key_probe_uses_bound_proxy_not_global_and_hides_proxy_errors(monkeypatch):
    import express_key_probe
    ident = proxy.add_proxy_entry('Account', 'http://USER:PRIVATE_PASSWORD@ACCOUNT:8080')['pool'][0]['id']
    runtime_state.app_state.set_express_keys([{'key': 'PRIVATE_KEY', 'proxy_mode': 'pool', 'proxy_id': ident}])
    proxy.save_proxy_config('direct')
    seen = []
    real_client = httpx.AsyncClient
    def make_client(**kwargs):
        seen.append(kwargs.copy())
        kwargs.pop('proxy', None)
        def handler(request):
            return httpx.Response(404, text='projects/123456789/locations/global/publishers/google/models/fixture was not found')
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, 'AsyncClient', make_client)
    project, _ = asyncio.run(express_key_probe.probe_project_id('PRIVATE_KEY'))
    assert project == '123456789'
    assert seen[0]['proxy'] == 'http://USER:PRIVATE_PASSWORD@ACCOUNT:8080'
    def fail(**kwargs):
        raise httpx.ProxyError('PRIVATE_PASSWORD')
    monkeypatch.setattr(httpx, 'AsyncClient', fail)
    project, message = asyncio.run(express_key_probe.probe_project_id('PRIVATE_KEY'))
    assert project is None and 'PRIVATE_PASSWORD' not in message


def test_express_selected_key_uses_own_proxy_for_sdk_and_fallback(monkeypatch):
    ident = proxy.add_proxy_entry('Account', 'http://ACCOUNT:8080')['pool'][0]['id']
    state = runtime_state.app_state
    state.set_express_keys([{'key': 'PRIVATE_KEY', 'project_id': 'key-project', 'proxy_mode': 'pool', 'proxy_id': ident}])
    seen = []
    def make_client(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace()
    async def execute(*args, **kwargs):
        assert await asyncio.to_thread(proxy.get_proxy_client_args) == {'trust_env': False, 'proxy': 'http://ACCOUNT:8080'}
        proxy.edit_proxy_entry(ident, 'updated', 'http://CHANGED:8080')
        kwargs['fallback_client_factory']()
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(express_sdk.genai, 'Client', make_client)
    monkeypatch.setattr(express_sdk, 'execute_gemini_call', execute)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(express_key_manager=ExpressKeyManager())))
    req = OpenAIRequest(model='gemini-3.8-flash', messages=[{'role': 'user', 'content': 'test'}])
    assert asyncio.run(express_sdk.ExpressSDKUpstream().chat_completions(req, request)).status_code == 200
    assert len(seen) == 2 and all(x['api_key'] == 'PRIVATE_KEY' for x in seen)
    assert all(x['http_options'].async_client_args == {'trust_env': False, 'proxy': 'http://ACCOUNT:8080'} for x in seen)
    assert proxy.get_proxy_client_args() == {'trust_env': True}


def test_concurrent_stream_and_thread_proxy_scopes_do_not_leak():
    first = proxy.add_proxy_entry('A', 'http://FIRST:8080')['pool'][0]['id']
    second = proxy.add_proxy_entry('B', 'http://SECOND:8080')['pool'][1]['id']

    @proxy.credential_proxy_scope
    async def prepare(ident):
        proxy.bind_credential_proxy({'proxy_mode': 'pool', 'proxy_id': ident})
        expected = proxy.get_proxy_client_args()
        await asyncio.sleep(0)
        assert proxy.get_proxy_client_args() == expected
        async def body():
            for _ in range(2):
                await asyncio.sleep(0)
                assert await asyncio.to_thread(proxy.get_proxy_client_args) == expected
                yield json.dumps(expected)
        return StreamingResponse(body())

    async def run():
        responses = await asyncio.gather(prepare(first), prepare(second))
        assert proxy.get_proxy_client_args() == {'trust_env': True}
        async def consume(resp):
            return [chunk async for chunk in resp.body_iterator]
        output = await asyncio.gather(*map(consume, responses))
        assert all('FIRST' in chunk for chunk in output[0])
        assert all('SECOND' in chunk for chunk in output[1])
        assert proxy.get_proxy_client_args() == {'trust_env': True}
    asyncio.run(run())


def test_proxy_scope_resets_after_exception_and_stream_close():
    @proxy.credential_proxy_scope
    async def fail():
        proxy.bind_credential_proxy({'proxy_mode': 'direct'})
        raise RuntimeError('fixture')

    closed = []
    @proxy.credential_proxy_scope
    async def prepare():
        proxy.bind_credential_proxy({'proxy_mode': 'direct'})
        async def body():
            try:
                yield 'first'
                yield 'second'
            finally:
                closed.append(proxy.get_proxy_client_args())
        return StreamingResponse(body())

    async def run():
        with pytest.raises(RuntimeError):
            await fail()
        assert proxy.get_proxy_client_args() == {'trust_env': True}
        response = await prepare()
        assert await anext(response.body_iterator) == 'first'
        assert proxy.get_proxy_client_args() == {'trust_env': True}
        await asyncio.create_task(response.body_iterator.aclose())
        assert closed == [{'trust_env': False}]
        assert proxy.get_proxy_client_args() == {'trust_env': True}
    asyncio.run(run())
