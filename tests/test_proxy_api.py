import pytest

import network_proxy as proxy
import runtime_state


def login(client):
    response = client.post('/api/login', json={'password': 'proxy-test-password'})
    assert response.status_code == 200


@pytest.mark.parametrize('method,path', [
    ('GET', '/api/proxy'), ('POST', '/api/proxy'), ('POST', '/api/proxy/test'),
])
def test_proxy_endpoints_require_console_login(client, method, path):
    response = client.request(method, path, json={'mode': 'direct'})
    assert response.status_code == 401


def test_save_get_blank_input_and_other_settings_hide_credentials(client):
    login(client)
    secret = 'socks5://PRIVATE_USER:PRIVATE_PASSWORD@HOST:1080'
    response = client.post('/api/proxy', json={'mode': 'custom', 'proxy_url': secret})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.json()['effective_url_masked'] == 'socks5://***:***@host:1080'
    for path in ['/api/proxy', '/api/settings', '/api/settings/runtime']:
        output = client.get(path)
        assert output.status_code == 200
        assert 'PRIVATE_USER' not in output.text and 'PRIVATE_PASSWORD' not in output.text
    response = client.post('/api/proxy', json={'mode': 'custom', 'proxy_url': ''})
    assert response.status_code == 200
    assert runtime_state.app_state.get_network_proxy()['url'] == secret
    assert runtime_state.AppState().get_network_proxy()['url'] == secret
    assert client.post('/api/proxy', json={'mode': 'direct'}).status_code == 200
    assert proxy.get_proxy_client_args() == {'trust_env': False}


@pytest.mark.parametrize('url', ['socks5://HOST', 'http://USER:PRIVATE_PASSWORD@HOST:bad', 'file:///etc/passwd'])
def test_bad_proxy_is_rejected_without_modifying_state(client, url):
    login(client)
    response = client.post('/api/proxy', json={'mode': 'custom', 'proxy_url': url})
    assert response.status_code == 400
    assert 'PRIVATE_PASSWORD' not in response.text
    assert runtime_state.app_state.get_network_proxy()['mode'] == 'environment'


def test_proxy_test_endpoint_does_not_save(client, monkeypatch):
    login(client)

    async def probe(mode, url, proxy_id=None):
        assert mode == 'custom' and url == 'http://USER:PRIVATE_PASSWORD@HOST:8080'
        return {'ok': True, 'message': 'connected', 'http_status': 204, 'elapsed_ms': 5, 'proxy_url_masked': 'http://***:***@host:8080'}

    monkeypatch.setattr(proxy, 'test_proxy_connection', probe)
    response = client.post('/api/proxy/test', json={'mode': 'custom', 'proxy_url': 'http://USER:PRIVATE_PASSWORD@HOST:8080'})
    assert response.status_code == 200
    assert response.json()['ok'] is True
    assert 'PRIVATE_PASSWORD' not in response.text
    assert runtime_state.app_state.get_network_proxy()['mode'] == 'environment'


def test_failed_persistence_returns_500_and_preserves_previous_state(client, monkeypatch):
    login(client)
    proxy.save_proxy_config('custom', 'http://PREVIOUS:8080')
    monkeypatch.setattr(runtime_state.app_state, '_save', lambda: False)
    response = client.post('/api/proxy', json={'mode': 'direct'})
    assert response.status_code == 500
    assert proxy.get_proxy_client_args()['proxy'] == 'http://PREVIOUS:8080'


def test_dashboard_contains_proxy_controls_only_after_login(client):
    assert 'id="proxy-mode"' not in client.get('/').text
    login(client)
    html = client.get('/').text
    for element in ['proxy-mode', 'proxy-url', 'proxy-test', 'proxy-save', 'proxy-result']:
        assert f'id="{element}"' in html
    assert 'loadNetworkProxy()' in html
