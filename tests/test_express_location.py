# -*- coding: utf-8 -*-
"""标准（Express）模式的 location 钉定，以及项目级错误的分类。

实测结论（2026-08-14，真机）：
- 只发裸模型名 → 走 express 端点格式，location 由后端路由；
  gemini-2.5-pro 因此被路由到 asia-southeast1 并 404。
- 改发 projects/{project}/locations/global/publishers/google/models/{model}
  → 同一个 Key、同一个模型 200 正常出文。
"""
import config as app_config
from runtime_state import app_state
from upstreams.express_sdk import resolve_express_model_path
import upstreams.cookie_proxy as cp


# ---------- 模型路径解析 ----------

def test_empty_location_keeps_bare_model():
    """留空 = 旧行为，必须原样发裸模型名（不引入回归）。"""
    assert resolve_express_model_path("gemini-2.5-pro", {}) == "gemini-2.5-pro"
    assert resolve_express_model_path("gemini-2.5-pro", {"express_location": ""}) == "gemini-2.5-pro"


def test_location_pinned_builds_full_path(monkeypatch):
    monkeypatch.setattr(app_config, "GOOGLE_PROJECT_ID", "proj-a", raising=False)
    got = resolve_express_model_path("gemini-2.5-pro", {"express_location": "global"})
    assert got == "projects/proj-a/locations/global/publishers/google/models/gemini-2.5-pro"


def test_region_location_supported(monkeypatch):
    monkeypatch.setattr(app_config, "GOOGLE_PROJECT_ID", "proj-a", raising=False)
    got = resolve_express_model_path("gemini-3.7-flash", {"express_location": "us-central1"})
    assert got == "projects/proj-a/locations/us-central1/publishers/google/models/gemini-3.7-flash"


def test_project_id_comes_from_credentials_page(monkeypatch):
    """项目 ID 只取「通道与凭证」页保存的那个（不再单独配一份）。"""
    monkeypatch.setattr(app_config, "GOOGLE_PROJECT_ID", None, raising=False)
    app_state.set_project_id("proj-from-state")
    got = resolve_express_model_path("gemini-2.5-pro", {"express_location": "global"})
    assert got == "projects/proj-from-state/locations/global/publishers/google/models/gemini-2.5-pro"


def test_project_id_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(app_config, "GOOGLE_PROJECT_ID", "proj-from-env", raising=False)
    got = resolve_express_model_path("gemini-2.5-pro", {"express_location": "global"})
    assert got == "projects/proj-from-env/locations/global/publishers/google/models/gemini-2.5-pro"


def test_no_project_id_degrades_safely(monkeypatch, capsys):
    """有 location 但拿不到项目 ID → 退回裸模型名，绝不拼出半截路径。"""
    monkeypatch.setattr(app_config, "GOOGLE_PROJECT_ID", None, raising=False)
    got = resolve_express_model_path("gemini-2.5-pro", {"express_location": "global"})
    assert got == "gemini-2.5-pro"
    assert "Project ID" in capsys.readouterr().out


def test_client_supplied_full_path_respected():
    full = "projects/x/locations/global/publishers/google/models/gemini-3.7-flash"
    assert resolve_express_model_path(full, {"express_location": "us-central1"}) == full
    assert resolve_express_model_path("publishers/google/models/m",
                                      {"express_location": "global"}) == "publishers/google/models/m"


# ---------- 项目级错误 vs Cookie 失效 ----------

def test_billing_error_is_project_not_cookie():
    msg = "This API method requires billing to be enabled. Please enable billing on project #tribal-x"
    assert cp._is_project_error(msg) is True


def test_permission_denied_on_project_is_project_error():
    """点名了具体项目资源 → 按项目问题给指引，而不是让人反复重取 Cookie。"""
    msg = ("Permission 'aiplatform.endpoints.predict' denied on resource "
           "'//aiplatform.googleapis.com/projects/some-proj/locations/global/publishers/google/models/x'")
    assert cp._is_project_error(msg) is True


def test_pure_session_errors_stay_cookie():
    for msg in ["Unauthenticated request", "login required", "session expired"]:
        assert cp._is_project_error(msg) is False
        assert cp._is_cookie_expired_error(msg) is True


def test_retryable_not_confused_with_project():
    assert cp._is_project_error("Resource has been exhausted (e.g. check quota).") is False
    assert cp._is_retryable_error("Resource has been exhausted (e.g. check quota).") is True


# ---------- 默认值与钉定失败自动回退 ----------

def test_default_is_global():
    """默认 global：多数模型只在 global 提供，让后端自选会偶发 404。"""
    assert app_config.DEFAULT_SETTINGS["express_location"] == "global"


def test_no_separate_project_id_setting():
    """项目 ID 不再有独立设置项——统一用「通道与凭证」里的那个。"""
    assert "express_project_id" not in app_config.DEFAULT_SETTINGS
    assert "express_project_id" not in app_config.PER_MODEL_KEYS


def test_default_settings_pin_when_project_available(monkeypatch):
    monkeypatch.setattr(app_config, "GOOGLE_PROJECT_ID", "proj-x", raising=False)
    got = resolve_express_model_path("gemini-2.5-pro", app_state.get_settings())
    assert got == "projects/proj-x/locations/global/publishers/google/models/gemini-2.5-pro"


def test_default_settings_no_project_stays_bare(monkeypatch):
    """没有项目 ID 时，默认 global 也不能拼出半截路径，必须退回裸模型名。"""
    monkeypatch.setattr(app_config, "GOOGLE_PROJECT_ID", None, raising=False)
    assert resolve_express_model_path("gemini-2.5-pro", app_state.get_settings()) == "gemini-2.5-pro"


def test_pin_failure_detection():
    from api_helpers import is_location_pin_failure
    # 项目/区域没有这个模型
    assert is_location_pin_failure(
        "404 NOT_FOUND. Publisher model `projects/p/locations/us-west1/publishers/google/"
        "models/gemini-2.5-pro` was not found") is True
    # 项目没开计费
    assert is_location_pin_failure(
        "403 PERMISSION_DENIED. This API method requires billing to be enabled") is True
    # 与钉定无关的错误不能误判（否则会把正常重试逻辑带偏）
    assert is_location_pin_failure("429 RESOURCE_EXHAUSTED quota") is False
    assert is_location_pin_failure("503 Service Unavailable") is False
    assert is_location_pin_failure("Requests ending with a model turn are not supported.") is False
