import pytest

import runtime_state


def login(client):
    assert client.post('/api/login', json={'password': 'proxy-test-password'}).status_code == 200


def test_disconnect_defaults_and_persistence(client):
    login(client)
    settings = client.get('/api/settings').json()
    assert settings['continue_on_disconnect'] is False
    assert settings['disconnect_grace_seconds'] == 5
    assert runtime_state.app_state.get_disconnect_grace_seconds() == 0
    response = client.post('/api/settings', json={
        'continue_on_disconnect': True, 'disconnect_grace_seconds': 12,
    })
    assert response.status_code == 200
    assert runtime_state.AppState().get_disconnect_grace_seconds() == 12


@pytest.mark.parametrize('seconds', [0, 5, 300, 12.0])
def test_valid_reconnection_grace(client, seconds):
    login(client)
    response = client.post('/api/settings', json={
        'continue_on_disconnect': True, 'disconnect_grace_seconds': seconds,
    })
    assert response.status_code == 200
    assert type(response.json()['disconnect_grace_seconds']) is int
    assert runtime_state.app_state.get_disconnect_grace_seconds() == seconds


@pytest.mark.parametrize('patch', [
    {'disconnect_grace_seconds': value} for value in [-1, 301, 0.5, True, None, '5', [], {}]
] + [
    {'continue_on_disconnect': value} for value in [0, 1, 'true', None, []]
])
def test_invalid_disconnect_settings_are_rejected_atomically(client, patch):
    login(client)
    before = client.get('/api/settings').json()
    response = client.post('/api/settings', json={**patch, 'retry_max': 42})
    assert response.status_code == 400
    assert client.get('/api/settings').json() == before


def test_disconnect_settings_are_global_and_bad_persisted_values_fall_back(isolated_state):
    isolated_state._state['settings'] = {'continue_on_disconnect': 'true', 'disconnect_grace_seconds': 5}
    assert isolated_state.get_disconnect_grace_seconds() == 0
    isolated_state._state['settings'] = {'continue_on_disconnect': True, 'disconnect_grace_seconds': -1}
    assert isolated_state.get_disconnect_grace_seconds() == 5
    result = isolated_state.set_model_override('gemini-4-argon', {
        'continue_on_disconnect': True, 'disconnect_grace_seconds': 100,
    })
    assert result == {}


@pytest.mark.parametrize('method,path', [
    ('DELETE', '/v1/requests/TARGET'), ('DELETE', '/v1/resumable/TARGET'),
    ('POST', '/v1/chat/completions/cancel'), ('GET', '/v1/resumable/TARGET'),
])
def test_cancel_and_poll_endpoints_require_api_auth(client, method, path):
    assert client.request(method, path, json={
        'model': 'gemini-4-argon', 'messages': [{'role': 'user', 'content': 'hello'}],
    }).status_code == 401
