import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

import config
import network_proxy as proxy
import runtime_state


def add(name='A', url='socks5://USER:PRIVATE_PASSWORD@FIRST:1080'):
    return proxy.add_proxy_entry(name, url)['pool'][-1]['id']


def login(client):
    assert client.post('/api/login', json={'password': 'proxy-test-password'}).status_code == 200


def test_pool_crud_selection_persistence_and_secret_masking():
    first = add()
    second = add('B', 'http://SECOND:8080')
    assert proxy.get_proxy_client_args() == {'trust_env': True}
    status = proxy.save_proxy_config('pool', proxy_id=first)
    assert status['mode'] == 'pool' and status['pool'][0]['active'] is True
    assert 'PRIVATE_PASSWORD' not in json.dumps(status)
    assert proxy.get_proxy_client_args()['proxy'] == 'socks5://USER:PRIVATE_PASSWORD@FIRST:1080'
    proxy.save_proxy_config('pool', proxy_id=second)
    assert proxy.get_proxy_client_args()['proxy'] == 'http://SECOND:8080'
    proxy.edit_proxy_entry(second, 'renamed')
    assert proxy.get_proxy_client_args()['proxy'] == 'http://SECOND:8080'
    proxy.edit_proxy_entry(second, 'renamed', 'socks5h://THIRD:1080')
    assert proxy.get_proxy_client_args() == {'trust_env': False, 'proxy': 'socks5h://THIRD:1080'}
    loaded = runtime_state.AppState().get_network_proxy()
    assert loaded['active_id'] == second and len(loaded['pool']) == 2
    proxy.delete_proxy_entry(first)
    assert len(proxy.get_proxy_status()['pool']) == 1
    with pytest.raises(ValueError):
        proxy.delete_proxy_entry(second)
    proxy.save_proxy_config('direct')
    proxy.delete_proxy_entry(second)
    assert proxy.get_proxy_client_args() == {'trust_env': False}
    assert proxy.get_proxy_status()['pool'] == []


def test_legacy_custom_is_migrated_without_changing_effective_proxy():
    secret = 'socks5://USER:PRIVATE_PASSWORD@LEGACY:1080'
    runtime_state.app_state._state['network_proxy'] = {'mode': 'custom', 'url': secret}
    original = runtime_state.app_state._state['network_proxy'].copy()
    status = proxy.get_proxy_status()
    assert status['mode'] == 'custom'
    assert status['pool'] == [{'id': 'legacy-custom', 'name': '原自定义代理', 'url_masked': 'socks5://***:***@legacy:1080', 'active': False}]
    assert runtime_state.app_state._state['network_proxy'] == original
    assert proxy.get_proxy_client_args()['proxy'] == secret
    proxy.save_proxy_config('pool', proxy_id='legacy-custom')
    assert runtime_state.AppState().get_network_proxy()['pool'][0]['url'] == secret
    assert proxy.get_proxy_client_args()['proxy'] == secret


def test_import_environment_proxy_without_exposing_credentials(monkeypatch):
    monkeypatch.setattr(config, 'PROXY_URL', 'http://USER:PRIVATE_PASSWORD@ENV:8080')
    status = proxy.add_proxy_entry('environment copy', use_current=True)
    assert status['mode'] == 'environment' and status['current_in_pool'] is True
    assert 'PRIVATE_PASSWORD' not in json.dumps(status)
    proxy.save_proxy_config('pool', proxy_id=status['pool'][0]['id'])
    assert proxy.get_proxy_client_args()['proxy'] == config.PROXY_URL
    with pytest.raises(ValueError):
        proxy.add_proxy_entry('duplicate', use_current=True)


@pytest.mark.parametrize('name', ['', ' ', 'x' * 81, 'name\nsecret'])
def test_invalid_entry_name_does_not_modify_state(name):
    before = runtime_state.app_state.get_network_proxy()
    with pytest.raises(ValueError):
        proxy.add_proxy_entry(name, 'http://HOST:8080')
    assert runtime_state.app_state.get_network_proxy() == before


def test_duplicates_missing_entries_and_empty_pool_fail_without_changing_config():
    first = add()
    second = add('B', 'http://SECOND:8080')
    before = runtime_state.app_state.get_network_proxy()
    actions = [
        lambda: proxy.add_proxy_entry('duplicate', 'socks5://USER:PRIVATE_PASSWORD@FIRST:1080'),
        lambda: proxy.edit_proxy_entry(second, 'duplicate', 'socks5://USER:PRIVATE_PASSWORD@FIRST:1080'),
        lambda: proxy.edit_proxy_entry('missing', 'name'),
        lambda: proxy.delete_proxy_entry('missing'),
        lambda: proxy.save_proxy_config('pool', proxy_id='missing'),
    ]
    for action in actions:
        with pytest.raises(ValueError):
            action()
        assert runtime_state.app_state.get_network_proxy() == before


def test_empty_pool_cannot_activate_or_import_direct():
    with pytest.raises(proxy.ProxyEntryNotFound):
        proxy.save_proxy_config('pool')
    with pytest.raises(ValueError):
        proxy.add_proxy_entry('current', use_current=True)


def test_pool_limit(monkeypatch):
    monkeypatch.setattr(proxy, 'MAX_PROXY_POOL_SIZE', 2)
    add()
    add('B', 'http://SECOND:8080')
    with pytest.raises(ValueError):
        add('C', 'http://THIRD:8080')
    assert len(proxy.get_proxy_status()['pool']) == 2


def test_failed_pool_write_rolls_back(monkeypatch):
    first = add()
    proxy.save_proxy_config('pool', proxy_id=first)
    previous = runtime_state.app_state.get_network_proxy()
    monkeypatch.setattr(runtime_state.app_state, '_save', lambda: False)
    for action in [lambda: add('B', 'http://SECOND:8080'), lambda: proxy.edit_proxy_entry(first, 'new'), lambda: proxy.save_proxy_config('direct')]:
        with pytest.raises(OSError):
            action()
        assert runtime_state.app_state.get_network_proxy() == previous


def test_pool_operations_keep_legacy_custom_and_other_modes():
    proxy.save_proxy_config('custom', 'http://ORIGINAL:8080')
    first = add()
    proxy.save_proxy_config('pool', proxy_id=first)
    proxy.save_proxy_config('custom')
    assert proxy.get_proxy_client_args()['proxy'] == 'http://ORIGINAL:8080'
    assert len(proxy.get_proxy_status()['pool']) == 1
    proxy.save_proxy_config('direct')
    assert len(proxy.get_proxy_status()['pool']) == 1


def test_concurrent_adds_preserve_all_entries():
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda i: proxy.add_proxy_entry(f'node {i}', f'http://HOST{i}:8080'), range(12)))
    state = runtime_state.AppState().get_network_proxy()
    assert len(state['pool']) == 12
    assert len({item['id'] for item in state['pool']}) == 12


def test_testing_inactive_profile_does_not_switch_or_save(monkeypatch):
    first = add()
    second = add('B', 'http://SECOND:8080')
    proxy.save_proxy_config('pool', proxy_id=first)
    before = runtime_state.app_state.get_network_proxy()
    seen = []
    real_client = httpx.AsyncClient

    def make_client(**kwargs):
        seen.append(kwargs.copy())
        kwargs.pop('proxy', None)
        return real_client(transport=httpx.MockTransport(lambda req: httpx.Response(204)), **kwargs)

    monkeypatch.setattr(httpx, 'AsyncClient', make_client)
    result = asyncio.run(proxy.test_proxy_connection('pool', proxy_id=second))
    assert result['ok'] is True and seen[0]['proxy'] == 'http://SECOND:8080'
    assert runtime_state.app_state.get_network_proxy() == before


@pytest.mark.parametrize('method,path,payload', [
    ('POST', '/api/proxy/pool', {'name': 'A', 'proxy_url': 'http://HOST:8080'}),
    ('POST', '/api/proxy/pool/current', {'name': 'A'}),
    ('PUT', '/api/proxy/pool/missing', {'name': 'A'}),
    ('DELETE', '/api/proxy/pool/missing', None),
])
def test_pool_api_authentication(client, method, path, payload):
    assert client.request(method, path, json=payload).status_code == 401


def test_pool_api_crud_and_active_delete_protection(client):
    login(client)
    response = client.post('/api/proxy/pool', json={'name': '<script>test</script>', 'proxy_url': 'http://USER:PRIVATE_PASSWORD@HOST:8080'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert 'PRIVATE_PASSWORD' not in response.text
    entry = response.json()['pool'][0]
    assert entry['name'] == '<script>test</script>'
    ident = entry['id']
    assert client.post('/api/proxy', json={'mode': 'pool', 'proxy_id': ident}).status_code == 200
    assert client.delete('/api/proxy/pool/' + ident).status_code == 400
    assert client.put('/api/proxy/pool/' + ident, json={'name': 'renamed', 'proxy_url': ''}).status_code == 200
    assert proxy.get_proxy_client_args()['proxy'] == 'http://USER:PRIVATE_PASSWORD@HOST:8080'
    assert client.post('/api/proxy', json={'mode': 'direct'}).status_code == 200
    assert client.delete('/api/proxy/pool/' + ident).status_code == 200
    assert client.get('/api/proxy').json()['pool'] == []
    assert client.delete('/api/proxy/pool/missing').status_code == 404
    assert client.post('/api/proxy', json={'mode': 'pool', 'proxy_id': 'missing'}).status_code == 404
    assert client.post('/api/proxy/test', json={'mode': 'pool', 'proxy_id': 'missing'}).status_code == 404


def test_import_current_api(client, monkeypatch):
    login(client)
    monkeypatch.setattr(config, 'PROXY_URL', 'socks5://USER:PRIVATE_PASSWORD@ENV:1080')
    response = client.post('/api/proxy/pool/current', json={'name': 'env copy'})
    assert response.status_code == 200
    assert len(response.json()['pool']) == 1
    assert 'PRIVATE_PASSWORD' not in response.text
    assert response.json()['mode'] == 'environment'


def test_pool_test_api_passes_selected_id_without_activating(client, monkeypatch):
    login(client)
    first = add()
    second = add('B', 'http://SECOND:8080')
    proxy.save_proxy_config('pool', proxy_id=first)
    before = runtime_state.app_state.get_network_proxy()

    async def probe(mode, url=None, proxy_id=None):
        assert mode == 'pool' and proxy_id == second and url is None
        return {'ok': True, 'http_status': 204, 'elapsed_ms': 1, 'message': 'connected', 'proxy_url_masked': 'http://second:8080'}

    monkeypatch.setattr(proxy, 'test_proxy_connection', probe)
    response = client.post('/api/proxy/test', json={'mode': 'pool', 'proxy_id': second})
    assert response.status_code == 200 and response.json()['ok'] is True
    assert runtime_state.app_state.get_network_proxy() == before
