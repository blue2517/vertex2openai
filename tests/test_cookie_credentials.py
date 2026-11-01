import asyncio
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import config
import network_proxy as proxy
import runtime_state
from cookie_credentials import CookieCredentialManager
from models import OpenAIRequest
from upstreams import cookie_proxy as cp


def record(index=1, **kwargs):
    return {"id": f"cookie-{index}", "name": f"Account {index}",
            "cookie": f"SAPISID=PRIVATE_COOKIE_{index}; SID=PRIVATE_SESSION_{index}",
            "project_id": f"project-{index}", "enabled": True, **kwargs}


def login(client):
    assert client.post('/api/login', json={'password': 'proxy-test-password'}).status_code == 200


def test_migration_dashboard_pair_beats_old_service_environment():
    state = runtime_state.app_state
    state.set_google_cookie(record()['cookie'])
    state.set_project_id('new-dashboard-project')
    state.import_cookie_credentials_once(record(2)['cookie'], 'old-environment-project')
    loaded = state.get_cookie_credentials()
    assert len(loaded) == 1
    assert loaded[0]['cookie'] == record()['cookie']
    assert loaded[0]['project_id'] == 'new-dashboard-project'
    assert runtime_state.AppState().get_cookie_credentials() == loaded
    state.import_cookie_credentials_once(record(3)['cookie'], 'another-project')
    assert state.get_cookie_credentials() == loaded


def test_environment_initial_import_then_empty_list_stays_empty_after_restart():
    state = runtime_state.app_state
    state.import_cookie_credentials_once(record()['cookie'], 'environment-project')
    assert state.get_cookie_credentials()[0]['project_id'] == 'environment-project'
    state.set_cookie_credentials([])
    loaded = runtime_state.AppState()
    loaded.import_cookie_credentials_once(record()['cookie'], 'environment-project')
    assert loaded.get_cookie_credentials() == []
    assert loaded.cookie_credentials_initialized()
    assert loaded.get_google_cookie() == '' and loaded.get_project_id() == ''


def test_round_robin_random_disabled_and_defensive_copies(monkeypatch):
    state = runtime_state.app_state
    state.set_cookie_credentials([record(1), record(2, enabled=False), record(3)])
    state.update_settings({'roundrobin': True})
    manager = CookieCredentialManager()
    assert [manager.get_record()['id'] for _ in range(5)] == ['cookie-1', 'cookie-3', 'cookie-1', 'cookie-3', 'cookie-1']
    snapshot = manager.get_record()
    snapshot['project_id'] = 'tampered'
    assert state.get_cookie_credentials()[2]['project_id'] == 'project-3'
    state.update_settings({'roundrobin': False})
    monkeypatch.setattr('cookie_credentials.random.randrange', lambda length: length - 1)
    assert manager.get_record()['id'] == 'cookie-3'
    state.set_cookie_credentials([record(1, enabled=False)])
    assert manager.get_record() is None


def test_round_robin_selection_is_thread_safe():
    state = runtime_state.app_state
    state.set_cookie_credentials([record(1), record(2), record(3)])
    state.update_settings({'roundrobin': True})
    manager = CookieCredentialManager()
    with ThreadPoolExecutor(max_workers=8) as pool:
        selected = list(pool.map(lambda _: manager.get_record()['id'], range(90)))
    assert Counter(selected) == {'cookie-1': 30, 'cookie-2': 30, 'cookie-3': 30}


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_credentials_api_requires_login(client, method):
    assert client.request(method, '/api/cookie-credentials', json={'credentials': []}).status_code == 401


def test_api_masks_secrets_preserves_blank_cookie_and_reloads(client):
    login(client)
    response = client.post('/api/cookie-credentials', json={'credentials': [record(), record(2)]})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert 'PRIVATE_COOKIE' not in response.text and 'PRIVATE_SESSION' not in response.text
    rows = response.json()['credentials']
    assert all('cookie' not in r and r['cookie_configured'] for r in rows)
    rows[0].update(cookie='', project_id='new-project', enabled=False, proxy_mode='direct')
    response = client.post('/api/cookie-credentials', json={'credentials': rows})
    assert response.status_code == 200 and response.json()['enabled'] == 1
    saved = runtime_state.AppState().get_cookie_credentials()
    assert saved[0]['cookie'] == record()['cookie'] and saved[0]['project_id'] == 'new-project'
    assert saved[0]['proxy_mode'] == 'direct'
    for path in ['/api/cookie-credentials', '/api/settings', '/api/settings/runtime', '/api/proxy']:
        result = client.get(path)
        assert result.status_code == 200
        assert 'PRIVATE_COOKIE' not in result.text and 'PRIVATE_SESSION' not in result.text
    assert client.get('/api/settings/runtime').json()['google_project_id'] == 'new-project'


@pytest.mark.parametrize('patch', [
    {'id': 'invalid<script>'}, {'name': 'x' * 81}, {'cookie': ''},
    {'cookie': 'SID=PRIVATE_SESSION_ONLY'}, {'cookie': 'SAPISID=PRIVATE_COOKIE\nSECRET'},
    {'project_id': ''}, {'project_id': 'invalid project'}, {'enabled': 'false'},
    {'proxy_mode': 'custom', 'proxy_url': 'PRIVATE_PASSWORD'},
    {'proxy_mode': 'pool', 'proxy_id': 'missing'},
])
def test_invalid_credentials_rejected_without_echo_or_mutation(client, patch):
    login(client)
    runtime_state.app_state.set_cookie_credentials([record(2)])
    before = runtime_state.app_state.get_cookie_credentials()
    response = client.post('/api/cookie-credentials', json={'credentials': [record(**patch)]})
    assert response.status_code == 400
    assert 'PRIVATE_' not in response.text
    assert runtime_state.app_state.get_cookie_credentials() == before


@pytest.mark.parametrize('payload', [{}, [], {'credentials': {}}, {'credentials': ['PRIVATE_COOKIE']},
                                     {'credentials': [record(), record()]}, {'credentials': [record()] * 101}])
def test_bad_payloads_do_not_replace_existing_credentials(client, payload):
    login(client)
    runtime_state.app_state.set_cookie_credentials([record(2)])
    before = runtime_state.app_state.get_cookie_credentials()
    response = client.post('/api/cookie-credentials', json=payload)
    assert response.status_code == 400
    assert 'PRIVATE_' not in response.text
    assert runtime_state.app_state.get_cookie_credentials() == before


def test_invalid_json_response_is_sanitized(client):
    login(client)
    response = client.post('/api/cookie-credentials', content='{"PRIVATE_COOKIE":broken', headers={'Content-Type': 'application/json'})
    assert response.status_code == 400 and 'PRIVATE_COOKIE' not in response.text


def test_failed_save_rolls_back_memory_and_disk(client, monkeypatch):
    login(client)
    runtime_state.app_state.set_cookie_credentials([record()])
    before = runtime_state.app_state.get_cookie_credentials()
    monkeypatch.setattr(runtime_state.app_state, '_save', lambda: False)
    response = client.post('/api/cookie-credentials', json={'credentials': [record(2)]})
    assert response.status_code == 500
    assert runtime_state.app_state.get_cookie_credentials() == before
    assert runtime_state.AppState().get_cookie_credentials() == before


def test_legacy_endpoint_updates_first_pair_without_removing_other_accounts(client):
    login(client)
    runtime_state.app_state.set_cookie_credentials([record(), record(2)])
    response = client.post('/api/cookie', json={'cookie': '', 'project_id': 'updated-project'})
    assert response.status_code == 200
    records = runtime_state.app_state.get_cookie_credentials()
    assert records[0]['cookie'] == record()['cookie'] and records[0]['project_id'] == 'updated-project'
    assert records[1]['id'] == 'cookie-2'


def test_legacy_endpoint_blank_cookie_preserves_migrated_environment(client, monkeypatch):
    login(client)
    monkeypatch.setattr(config, 'GOOGLE_COOKIE', record()['cookie'])
    monkeypatch.setattr(config, 'GOOGLE_PROJECT_ID', 'environment-project')
    response = client.post('/api/cookie', json={'cookie': '', 'project_id': 'updated-project'})
    assert response.status_code == 200
    assert runtime_state.app_state.get_cookie_credentials()[0]['cookie'] == record()['cookie']


def test_dashboard_precedence_even_before_migration(monkeypatch):
    runtime_state.app_state.set_google_cookie(record()['cookie'])
    runtime_state.app_state.set_project_id('dashboard-project')
    monkeypatch.setattr(config, 'GOOGLE_COOKIE', record(2)['cookie'])
    monkeypatch.setattr(config, 'GOOGLE_PROJECT_ID', 'old-environment-project')
    assert cp._get_cookie_string() == record()['cookie']
    assert cp._get_project_id() == 'dashboard-project'


def test_disabled_or_empty_managed_pool_never_falls_back_to_environment(monkeypatch):
    monkeypatch.setattr(config, 'GOOGLE_COOKIE', record()['cookie'])
    monkeypatch.setattr(config, 'GOOGLE_PROJECT_ID', 'environment-project')
    async def run():
        req = OpenAIRequest(model='gemini-3.8-flash', messages=[{'role': 'user', 'content': 'test'}])
        for records in [[], [record(enabled=False)]]:
            runtime_state.app_state.set_cookie_credentials(records)
            response = await cp.CookieProxyUpstream().chat_completions(req, SimpleNamespace())
            assert response.status_code == 401
    asyncio.run(run())


@pytest.mark.parametrize('stream', [False, True])
def test_cookie_request_retries_keep_credential_project_and_proxy_snapshot(monkeypatch, stream):
    state = runtime_state.app_state
    ident = proxy.add_proxy_entry('Account exit', 'http://PRIVATE_PROXY:PRIVATE_PASSWORD@HOST:8080')['pool'][0]['id']
    state.set_cookie_credentials([record(proxy_mode='pool', proxy_id=ident)])
    state.update_settings({'roundrobin': True})
    seen, bodies, cookies = [], [], []
    real_client = httpx.AsyncClient

    def handler(request):
        cookies.append(request.headers['cookie'])
        # Simulate account edits and proxy edits while the request is in flight.
        state.set_cookie_credentials([record(2, proxy_mode='direct')])
        proxy.edit_proxy_entry(ident, 'edited', 'http://CHANGED:8080')
        return httpx.Response(503 if len(cookies) == 1 else 418, text='fixture error')

    def make_client(**kwargs):
        seen.append(kwargs.copy())
        kwargs.pop('proxy', None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    async def build(project, *args, **kwargs):
        bodies.append(project)
        assert await asyncio.to_thread(proxy.get_proxy_client_args) == {'trust_env': False, 'proxy': 'http://PRIVATE_PROXY:PRIVATE_PASSWORD@HOST:8080'}
        return {'variables': {}}

    monkeypatch.setattr(cp, 'build_batch_graphql_body_async', build)
    monkeypatch.setattr(cp, 'get_retry_settings', lambda: (1, 0))
    monkeypatch.setattr(httpx, 'AsyncClient', make_client)

    async def run():
        req = OpenAIRequest(model='gemini-3.8-flash', messages=[{'role': 'user', 'content': 'test'}], stream=stream)
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        response = await cp.CookieProxyUpstream().chat_completions(req, request)
        assert 'proxy' not in proxy.get_proxy_client_args()
        if stream:
            output = ''.join([chunk async for chunk in response.body_iterator])
            assert '[DONE]' in output and 'fixture error' in output
        else:
            assert response.status_code == 418
        assert 'proxy' not in proxy.get_proxy_client_args()
    asyncio.run(run())
    assert bodies == ['project-1', 'project-1']
    assert cookies == [record()['cookie'], record()['cookie']]
    assert all(r['proxy'] == 'http://PRIVATE_PROXY:PRIVATE_PASSWORD@HOST:8080' for r in seen)


@pytest.mark.parametrize('stream', [False, True])
def test_two_cookie_accounts_route_with_their_own_project_and_proxy(monkeypatch, stream):
    state = runtime_state.app_state
    ident = proxy.add_proxy_entry('First exit', 'http://FIRST:8080')['pool'][0]['id']
    state.set_cookie_credentials([record(1, proxy_mode='pool', proxy_id=ident), record(2, proxy_mode='direct')])
    state.update_settings({'roundrobin': True})
    cp.cookie_credential_manager.reset()
    monkeypatch.setattr(config, 'GOOGLE_COOKIE', 'SAPISID=OLD_ENVIRONMENT; SID=OLD_SESSION')
    monkeypatch.setattr(config, 'GOOGLE_PROJECT_ID', 'old-environment-project')
    sent, proxies = [], []
    real_client = httpx.AsyncClient
    def handler(request):
        sent.append((request.headers['cookie'], json.loads(request.content)['variables']['project']))
        return httpx.Response(200, json={'results': [{'data': {'candidates': [{'content': {'parts': [{'text': 'OK'}]}, 'finishReason': 'STOP'}]}}]})
    def make_client(**kwargs):
        proxies.append({k: kwargs[k] for k in ['proxy', 'trust_env'] if k in kwargs})
        kwargs.pop('proxy', None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    async def build(project, *args, **kwargs):
        return {'variables': {'project': project}}
    monkeypatch.setattr(cp, 'build_batch_graphql_body_async', build)
    monkeypatch.setattr(httpx, 'AsyncClient', make_client)
    async def run():
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        for _ in range(2):
            req = OpenAIRequest(model='gemini-3.8-flash', messages=[{'role': 'user', 'content': 'test'}], stream=stream)
            response = await cp.CookieProxyUpstream().chat_completions(req, request)
            output = ''.join([chunk async for chunk in response.body_iterator]) if stream else response.body.decode()
            assert 'OK' in output
    asyncio.run(run())
    assert sent == [(record(1)['cookie'], 'project-1'), (record(2)['cookie'], 'project-2')]
    assert proxies == [{'proxy': 'http://FIRST:8080', 'trust_env': False}, {'trust_env': False}]


def test_image_fake_stream_collector_retries_use_captured_cookie(monkeypatch):
    cookies = []
    real_client = httpx.AsyncClient
    def handler(request):
        cookies.append(request.headers['cookie'])
        runtime_state.app_state.set_google_cookie(record(2)['cookie'])
        return httpx.Response(503 if len(cookies) == 1 else 418, text='fixture')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(cp, 'build_batch_graphql_body_async', AsyncMock(return_value={'variables': {}}))
    async def run():
        req = OpenAIRequest(model='gemini-3.6-flash-image', messages=[{'role': 'user', 'content': 'test'}])
        return await cp._collect_full_response('project-1', req.model, req, cp.build_headers(record()['cookie']),
            {'trust_env': False}, 1, 0, SimpleNamespace(is_disconnected=AsyncMock(return_value=False)), cookie_str=record()['cookie'])
    assert asyncio.run(run())['kind'] == 'error'
    assert cookies == [record()['cookie'], record()['cookie']]
