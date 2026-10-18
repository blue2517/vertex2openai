import asyncio
import json

import httpx
import pytest

import config
import network_proxy as proxy
import runtime_state


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080", "https://HOST:443", "socks5://USER:PASS@HOST:1080",
    "socks5h://HOST:1080", "http://USER:p%40ss%23word@[::1]:8080", "http://HOST",
])
def test_valid_proxy_urls(url):
    assert proxy.validate_proxy_url(url) == url


@pytest.mark.parametrize("url", [
    "", "file:///etc/passwd", "ftp://HOST:21", "http://", "socks5://HOST",
    "http://HOST:0", "http://HOST:65536", "http://HOST:not-a-port",
    "http://HOST/path", "http://HOST?token=secret", "http://HOST#secret",
    "http://USER:password\n@HOST:80", "http://HOST/a secret", "http://[invalid:80",
])
def test_invalid_proxy_urls_do_not_echo_input(url):
    with pytest.raises(ValueError) as exc:
        proxy.validate_proxy_url(url)
    assert "password" not in str(exc.value)
    assert "secret" not in str(exc.value)


def test_mask_hides_all_credentials_and_handles_ipv6():
    assert proxy.mask_proxy_url("socks5://private-user:private-pass@[::1]:1080") == "socks5://***:***@[::1]:1080"
    assert proxy.mask_proxy_url("http://USER:p%40ss@HOST:8080") == "http://***:***@host:8080"
    assert proxy.mask_proxy_url(None) == ""


def test_default_follows_environment_without_persisting_secret(monkeypatch):
    monkeypatch.setattr(config, "PROXY_URL", "socks5://env-user:env-pass@HOST:1080")
    assert proxy.get_proxy_client_args() == {"trust_env": True, "proxy": config.PROXY_URL}
    status = proxy.get_proxy_status()
    assert status["mode"] == "environment"
    assert "env-pass" not in json.dumps(status)
    assert "env-user" not in json.dumps(status)
    assert "network_proxy" not in runtime_state.app_state._state


def test_persistence_precedence_direct_and_blank_preservation(monkeypatch):
    monkeypatch.setattr(config, "PROXY_URL", "http://env-user:env-pass@ENV:8000")
    custom = "socks5h://custom-user:custom-pass@CUSTOM:1080"
    proxy.save_proxy_config("custom", custom)
    assert runtime_state.AppState().get_network_proxy() == {"mode": "custom", "url": custom, "pool": [], "active_id": ""}
    assert proxy.get_proxy_client_args() == {"trust_env": False, "proxy": custom}
    proxy.save_proxy_config("direct")
    assert proxy.get_proxy_client_args() == {"trust_env": False}
    proxy.save_proxy_config("custom", "")
    assert proxy.get_proxy_client_args()["proxy"] == custom
    proxy.save_proxy_config("environment")
    assert proxy.get_proxy_client_args()["proxy"] == config.PROXY_URL


def test_first_custom_blank_can_reuse_environment(monkeypatch):
    monkeypatch.setattr(config, "PROXY_URL", "socks5://ENV:1080")
    proxy.save_proxy_config("custom")
    assert proxy.get_proxy_client_args()["proxy"] == config.PROXY_URL


def test_invalid_save_and_failed_disk_write_keep_previous_config(monkeypatch):
    proxy.save_proxy_config("custom", "http://PREVIOUS:8080")
    previous = runtime_state.app_state.get_network_proxy()
    with pytest.raises(ValueError):
        proxy.save_proxy_config("custom", "ftp://HOST:21")
    assert runtime_state.app_state.get_network_proxy() == previous
    monkeypatch.setattr(runtime_state.app_state, "_save", lambda: False)
    with pytest.raises(OSError):
        proxy.save_proxy_config("direct")
    assert runtime_state.app_state.get_network_proxy() == previous


def fake_async_client(monkeypatch, handler, seen):
    real_client = httpx.AsyncClient

    def make_client(**kwargs):
        seen.append(kwargs.copy())
        kwargs.pop("proxy", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", make_client)


def test_connection_probe_is_fixed_credential_free_and_does_not_save(monkeypatch):
    seen = []

    def handler(request):
        assert str(request.url) == proxy.PROXY_TEST_URL
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(204)

    fake_async_client(monkeypatch, handler, seen)
    result = asyncio.run(proxy.test_proxy_connection("custom", "socks5://USER:SECRET@HOST:1080"))
    assert result["ok"] is True and result["http_status"] == 204
    assert seen[0]["proxy"] == "socks5://USER:SECRET@HOST:1080"
    assert seen[0]["trust_env"] is False
    assert "SECRET" not in json.dumps(result)
    assert runtime_state.app_state.get_network_proxy()["mode"] == "environment"


@pytest.mark.parametrize("status", [200, 302, 403, 500])
def test_non_204_probe_responses_are_failures(monkeypatch, status):
    fake_async_client(monkeypatch, lambda request: httpx.Response(status), [])
    result = asyncio.run(proxy.test_proxy_connection("direct"))
    assert result["ok"] is False and result["http_status"] == status


def test_probe_failure_hides_exception_credentials(monkeypatch):
    def handler(request):
        raise httpx.ProxyError("socks5://USER:SECRET@HOST:1080 failed")

    fake_async_client(monkeypatch, handler, [])
    result = asyncio.run(proxy.test_proxy_connection("custom", "socks5://USER:SECRET@HOST:1080"))
    assert result["ok"] is False
    assert "SECRET" not in json.dumps(result)


def test_all_shared_http_clients_read_live_proxy(monkeypatch):
    import express_key_probe
    from http_options import get_http_options

    proxy.save_proxy_config("custom", "socks5://FIRST:1080")
    assert get_http_options().client_args["proxy"] == "socks5://FIRST:1080"
    assert get_http_options().async_client_args["proxy"] == "socks5://FIRST:1080"
    assert express_key_probe._client_args()["proxy"] == "socks5://FIRST:1080"
    proxy.save_proxy_config("direct")
    assert get_http_options().client_args == {"trust_env": False}
    assert "proxy" not in express_key_probe._client_args()


def test_model_loader_uses_live_proxy(monkeypatch):
    import model_loader

    seen = []
    fake_async_client(monkeypatch, lambda request: httpx.Response(200, json={"models": ["gemini-4-argon"]}), seen)
    proxy.save_proxy_config("custom", "http://CUSTOM:8080")
    assert asyncio.run(model_loader.fetch_and_parse_models_config())["models"] == ["gemini-4-argon"]
    assert seen[0]["proxy"] == "http://CUSTOM:8080"
    assert seen[0]["trust_env"] is False


def test_remote_image_download_switches_proxy_and_direct(monkeypatch):
    import message_processing

    seen = []
    real_client = httpx.Client

    def make_client(**kwargs):
        seen.append(kwargs.copy())
        kwargs.pop('proxy', None)
        return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'image', headers={'content-type': 'image/png'})), **kwargs)

    monkeypatch.setattr(httpx, 'Client', make_client)
    monkeypatch.setattr(message_processing, '_is_blocked_host', lambda host: False)
    proxy.save_proxy_config('custom', 'http://CUSTOM:8080')
    assert message_processing.fetch_remote_image('https://images.example.test/a.png') == (b'image', 'image/png')
    assert seen[-1]['proxy'] == 'http://CUSTOM:8080'
    proxy.save_proxy_config('direct')
    assert message_processing.fetch_remote_image('https://images.example.test/a.png') == (b'image', 'image/png')
    assert 'proxy' not in seen[-1] and seen[-1]['trust_env'] is False


def test_cookie_channel_uses_live_proxy(monkeypatch):
    from models import OpenAIRequest
    from upstreams import cookie_proxy

    class Request:
        async def is_disconnected(self):
            return False

    seen = []
    monkeypatch.setattr(cookie_proxy, '_get_cookie_string', lambda: 'SAPISID=fake-fixture')
    monkeypatch.setattr(cookie_proxy, '_get_project_id', lambda: 'fixture-project')
    monkeypatch.setattr(config, 'SSL_CERT_FILE', '/fixture/custom-ca.pem')
    fake_async_client(monkeypatch, lambda request: httpx.Response(400, text='mock upstream response'), seen)
    payload = OpenAIRequest(model='gemini-3.6-flash', messages=[{'role': 'user', 'content': 'test'}])
    proxy.save_proxy_config('custom', 'socks5://CUSTOM:1080')
    response = asyncio.run(cookie_proxy.CookieProxyUpstream().chat_completions(payload, Request()))
    assert response.status_code == 400
    assert seen[-1]['proxy'] == 'socks5://CUSTOM:1080' and seen[-1]['trust_env'] is False
    assert seen[-1]['verify'] == '/fixture/custom-ca.pem'
    proxy.save_proxy_config('direct')
    response = asyncio.run(cookie_proxy.CookieProxyUpstream().chat_completions(payload, Request()))
    assert response.status_code == 400
    assert 'proxy' not in seen[-1] and seen[-1]['trust_env'] is False


def test_express_fallback_keeps_request_proxy_snapshot(monkeypatch):
    from types import SimpleNamespace
    from fastapi.responses import JSONResponse
    from express_key_manager import ExpressKeyManager
    from models import OpenAIRequest
    from upstreams import express_sdk

    runtime_state.app_state.set_express_keys([{'key': 'fake-fixture', 'project_id': 'fixture-project'}])
    seen = []

    def make_client(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace()

    async def execute(*args, **kwargs):
        proxy.save_proxy_config('direct')
        kwargs['fallback_client_factory']()
        return JSONResponse({'ok': True})

    monkeypatch.setattr(express_sdk.genai, 'Client', make_client)
    monkeypatch.setattr(express_sdk, 'execute_gemini_call', execute)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(express_key_manager=ExpressKeyManager())))
    payload = OpenAIRequest(model='gemini-3.6-flash', messages=[{'role': 'user', 'content': 'test'}])
    proxy.save_proxy_config('custom', 'socks5://ORIGINAL:1080')
    response = asyncio.run(express_sdk.ExpressSDKUpstream().chat_completions(payload, request))
    assert response.status_code == 200
    assert len(seen) == 2
    assert all(item['http_options'].client_args['proxy'] == 'socks5://ORIGINAL:1080' for item in seen)
    assert proxy.get_proxy_client_args() == {'trust_env': False}
