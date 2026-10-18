import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    import config
    import runtime_state

    monkeypatch.setattr(runtime_state, "STATE_FILE", str(tmp_path / "web_state.json"))
    monkeypatch.setattr(runtime_state.app_state, "_state", {"use_web_proxy": False})
    monkeypatch.setattr(config, "PROXY_URL", None)
    monkeypatch.setattr(config, "SSL_CERT_FILE", None)
    monkeypatch.setattr(config, "API_KEY", "proxy-test-password")
    yield runtime_state.app_state


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import main

    monkeypatch.setattr(main, "_sessions", {})
    monkeypatch.setattr(main, "_login_failures", {})
    # 不进入 lifespan，测试只使用本地模拟网络，不拉取线上模型或探测真实 Key。
    result = TestClient(main.app, raise_server_exceptions=True)
    try:
        yield result
    finally:
        result.close()
