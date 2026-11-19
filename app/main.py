import asyncio
import json
import os
import secrets
import threading
import time
from typing import Literal
from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel

from auth import get_api_key
from express_key_manager import ExpressKeyManager
from routes import models_api, chat_api

from logger import rt_logger, stats
import config
from runtime_state import app_state
import model_capabilities as mc
from model_loader import get_express_models

from cookie_auth import validate_cookie
from cookie_credentials import cookie_credential_manager
import express_key_probe
from resumable import resumable_store
import network_proxy
from request_lifecycle import DisconnectMiddleware, shutdown_jobs
from api_errors import create_openai_error_response, error_type_for_status
from token_counting import warm_tokenizer
from service_control import service_control
from usage_stats import (
    usage_ledger,
    express_credential_id,
    mask_express_key,
    format_express_label,
    format_cookie_label,
    format_proxy_label,
)

express_key_manager = ExpressKeyManager()


async def _probe_express_projects_on_startup() -> None:
    """启动后异步探测尚无 Project ID 的 Express Key（失败不影响服务）。"""
    try:
        pending = [r["key"] for r in app_state.get_express_keys() if not r.get("project_id")]
        if not pending:
            return
        print(f"🔎 [密钥探测] 启动探测：{len(pending)} 个 Key 尚无 Project ID，正在用不存在的模型名调 generateContent 探测。")
        await express_key_probe.probe_all(keys=pending)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"⚠️ [密钥探测] 启动探测失败（不影响服务）：{type(e).__name__} - {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from model_loader import refresh_models_config_cache
    # Validate the bundled vocabulary once; no tokenizer network downloads at runtime.
    await asyncio.to_thread(warm_tokenizer)
    print("🚀 [服务启动] agentplatform2api 适配器已启动（Express API Key / Cookie 直连 双通道）。")

    # S-1：默认口令 + 公开托管 + 明文 Cookie 是很危险的组合，必须让人看见。
    if config.API_KEY == DEFAULT_API_KEY:
        public_host = any(os.environ.get(k) for k in ("SPACE_ID", "SPACE_HOST", "HF_SPACE_ID"))
        if public_host and os.environ.get("ALLOW_DEFAULT_KEY", "").lower() not in ("1", "true", "yes"):
            raise RuntimeError(
                "检测到公开托管环境（HuggingFace Space 等）且 API_KEY 仍为默认值 123456。\n"
                "该口令同时是控制台登录密码，而控制台可以读写完整的 Google 会话 Cookie。\n"
                "请设置一个强 API_KEY 后重启；确需临时放行可设 ALLOW_DEFAULT_KEY=true。"
            )
        print("🔴 [安全警告] API_KEY 仍是默认值 123456！它既是本代理的 Key，也是控制台登录口令，"
              "请立刻改成强口令。")
    # 多账号 Express Key：环境变量只做**一次性**初始导入，之后以控制台保存的列表为准。
    # （用户在控制台把列表清空并保存后，这里不会再把环境变量里的 Key 复种回来。）
    app_state.import_env_express_keys_once(config.VERTEX_EXPRESS_API_KEY_VAL)
    app_state.import_cookie_credentials_once(config.GOOGLE_COOKIE, config.GOOGLE_PROJECT_ID)
    if express_key_manager.get_total_keys() > 0:
        print(f"✅ [密钥配置] 已加载 {express_key_manager.get_total_keys()} 个 Express API Key。")
    else:
        print("⚠️ [密钥配置] 未配置任何 Express API Key（控制台「通道与凭证」页可随时添加）。"
              "若不启用 Cookie 直连模式，聊天请求将会报错。")
    await refresh_models_config_cache()

    # 启动时自动探测各 Key 归属的 GCP Project ID（location 钉定要用）。
    # 放后台任务里跑：探测走外网，绝不能阻塞服务启动。
    _probe_task = asyncio.create_task(_probe_express_projects_on_startup())
    # 过期缓冲清理器（弱网续传用，见 resumable.py）
    _sweeper_task = asyncio.create_task(resumable_store.sweeper_loop())
    try:
        yield
    finally:
        for _t in (_probe_task, _sweeper_task):
            if not _t.done():
                _t.cancel()
        await shutdown_jobs()
        resumable_store.cancel_all_pumps()

app = FastAPI(title="agentplatform2api", lifespan=lifespan)


@app.exception_handler(StarletteHTTPException)
async def api_http_error(request: Request, error: StarletteHTTPException):
    if not request.url.path.startswith("/v1/"):
        return await http_exception_handler(request, error)
    return JSONResponse(status_code=error.status_code, headers=error.headers,
                        content=create_openai_error_response(
                            error.status_code, str(error.detail), error_type_for_status(error.status_code)))


@app.exception_handler(RequestValidationError)
async def api_validation_error(request: Request, error: RequestValidationError):
    if not request.url.path.startswith("/v1/"):
        return await request_validation_exception_handler(request, error)
    details = error.errors()[0] if error.errors() else {}
    payload = create_openai_error_response(422, "请求参数无效：" + details.get("msg", "格式错误"), "invalid_request_error")
    payload["error"]["param"] = ".".join(str(part) for part in details.get("loc", ()) if part != "body") or None
    return JSONResponse(status_code=422, content=payload)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # 本服务使用 Bearer / Basic 鉴权，不依赖浏览器 Cookie；
    # 关闭 allow_credentials 以符合 CORS 规范（通配符 + 凭证不合法）。
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.express_key_manager = express_key_manager


@app.middleware("http")
async def stats_tracker_middleware(request: Request, call_next):
    if "chat/completions" in request.url.path:
        stats.increment_total()
        try:
            response = await call_next(request)
            if response.status_code >= 400 and response.status_code != 499:
                stats.add_error()
            return response
        except Exception as e:
            stats.add_error()
            raise e
    return await call_next(request)


# ====== 仅密码登录（Cookie 会话，免输账号）======
AUTH_COOKIE = "ap_session"
DEFAULT_API_KEY = "123456"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30

# P2-2：会话 token 改为随机值存内存，不再用 sha256(常量 + API_KEY) 这种确定值。
# 确定值意味着同一个 API_KEY 永远对应同一个 cookie，无法单独失效某个会话。
_sessions: dict = {}          # token -> 过期时间戳
_sessions_lock = threading.Lock()

# 登录失败计数：{ip: [失败次数, 最近失败时间]}，指数退避
_login_failures: dict = {}
_login_lock = threading.Lock()
LOGIN_LOCK_BASE_SECONDS = 2
LOGIN_LOCK_MAX_SECONDS = 300


def _issue_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _sessions_lock:
        for t, exp in list(_sessions.items()):     # 顺手清理过期会话
            if exp < now:
                _sessions.pop(t, None)
        _sessions[token] = now + SESSION_TTL_SECONDS
    return token


def _revoke_session(token: str) -> None:
    with _sessions_lock:
        _sessions.pop(token, None)


def _session_tokens(request: Request) -> list[str]:
    tokens: list[str] = []
    raw = request.headers.get("cookie", "")
    for chunk in raw.split(";"):
        if "=" not in chunk:
            continue
        name, val = chunk.split("=", 1)
        if name.strip() == AUTH_COOKIE:
            token = val.strip().strip('"')
            if token and token not in tokens:
                tokens.append(token)
    fallback = request.cookies.get(AUTH_COOKIE, "")
    if fallback and fallback not in tokens:
        tokens.append(fallback)
    return tokens


def _is_authed(request: Request) -> bool:
    tokens = _session_tokens(request)
    if not tokens:
        return False
    now = time.time()
    with _sessions_lock:
        for token in tokens:
            exp = _sessions.get(token)
            if exp is None:
                continue
            if exp < now:
                _sessions.pop(token, None)
                continue
            return True
    return False


def _login_retry_after(ip: str) -> int:
    """该 IP 还需等待多少秒才能再次尝试登录（0 = 可以尝试）。"""
    with _login_lock:
        rec = _login_failures.get(ip)
        if not rec:
            return 0
        count, last = rec
        if count < 3:
            return 0
        wait = min(LOGIN_LOCK_BASE_SECONDS * (2 ** (count - 3)), LOGIN_LOCK_MAX_SECONDS)
        remain = int(last + wait - time.time())
        return max(0, remain)


def _record_login_failure(ip: str) -> None:
    with _login_lock:
        count, _ = _login_failures.get(ip, (0, 0.0))
        _login_failures[ip] = (count + 1, time.time())


def _clear_login_failure(ip: str) -> None:
    with _login_lock:
        _login_failures.pop(ip, None)


def mask_cookie(cookie_str: str) -> str:
    """S-1：控制台只回显掩码，不再把完整 Google 会话 Cookie 明文吐回前端。"""
    if not cookie_str:
        return ""
    names = []
    for seg in cookie_str.split(";"):
        name = seg.strip().split("=", 1)[0].strip()
        if name:
            names.append(name)
    return f"已配置（共 {len(names)} 个 cookie 字段，{len(cookie_str)} 字符）"


async def require_auth(request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="未登录")
    return True


LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 · vertex2openai 控制台</title>
<style>
  :root{--bg:#0b1020;--panel:#121a2e;--border:#273454;--text:#e6ecf8;--muted:#8f9bb8;--primary:#4f7cff;--primary-hover:#3d69eb;--danger:#ef4444;--danger-bg:rgba(239,68,68,.14)}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
  .login-card{width:100%;max-width:400px;background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:28px 24px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
  .login-card h1{margin:0 0 6px;font-size:20px;font-weight:700;display:flex;align-items:center;gap:10px}
  .login-card p{margin:0 0 18px;font-size:13px;color:var(--muted)}
  .field{margin-bottom:14px}
  .field label{display:block;font-size:12.5px;color:var(--muted);margin-bottom:6px}
  input{width:100%;background:#0c1222;color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-size:14px;outline:none}
  input:focus{border-color:var(--primary)}
  button{width:100%;background:var(--primary);color:#fff;border:1px solid var(--primary);border-radius:8px;padding:10px;font-size:13.5px;font-weight:600;cursor:pointer;transition:.15s}
  button:hover{background:var(--primary-hover);border-color:var(--primary-hover)}
  .err{margin-top:12px;min-height:18px;font-size:12.5px;color:#fca5a5}
</style>
</head><body>
  <div class="login-card">
    <h1>
      <svg width="26" height="26" viewBox="0 0 40 40" fill="none"><rect width="40" height="40" rx="10" fill="#4f7cff"/><path d="M11 16 H27" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/><path d="M23 12 L28.5 16 L23 20" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M29 24 H13" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-opacity=".9"/><path d="M17 20 L11.5 24 L17 28" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" stroke-opacity=".9"/></svg>
      vertex2openai 控制台
    </h1>
    <p>请输入访问密码（API_KEY）</p>
    <form id="f" onsubmit="return doLogin(event)">
      <div class="field">
        <label for="pw">管理密码</label>
        <input id="pw" type="password" placeholder="输入密码" autofocus autocomplete="current-password">
      </div>
      <button type="submit">登录控制台</button>
    </form>
    <div id="err" class="err"></div>
  </div>
<script>
const BASE_PATH = (typeof location !== 'undefined' && location.pathname)
  ? location.pathname.replace(/\/+$/, '')
  : '';
const appUrl = path => (typeof path === 'string' && path.startsWith('/') && !path.startsWith('//') && BASE_PATH && !path.startsWith(BASE_PATH + '/'))
  ? BASE_PATH + path
  : path;
async function doLogin(e){
  e.preventDefault();
  const pw=document.getElementById('pw').value;
  const r=await fetch(appUrl('/api/login'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
  if(r.ok){ location.href=appUrl('/'); } else { document.getElementById('err').textContent='密码错误，请重试'; }
  return false;
}
</script>
</body></html>
"""


from dashboard_html import DASHBOARD_HTML


@app.get("/", response_class=HTMLResponse)
async def dashboard_ui(request: Request):
    if _is_authed(request):
        return HTMLResponse(DASHBOARD_HTML)
    return HTMLResponse(LOGIN_HTML)


class LoginBody(BaseModel):
    password: str


def _cookie_path(request: Request) -> str:
    prefix = (request.headers.get("x-forwarded-prefix") or "").strip()
    if prefix.startswith("/") and not prefix.startswith("//"):
        cleaned = "/" + prefix.strip("/")
        return cleaned if cleaned != "/" else "/"
    return "/"


@app.post("/api/login")
async def login(body: LoginBody, request: Request):
    ip = request.client.host if request.client else "unknown"

    # P2-2：失败三次后指数退避，避免对口令（同时也是 API Key）无限爆破
    remain = _login_retry_after(ip)
    if remain > 0:
        return JSONResponse(status_code=429,
                            content={"error": f"尝试过于频繁，请 {remain} 秒后再试"})

    if config.API_KEY and secrets.compare_digest(body.password, config.API_KEY):
        _clear_login_failure(ip)
        resp = JSONResponse(content={"ok": True})
        cookie_path = _cookie_path(request)
        is_secure = (request.url.scheme == "https"
                     or request.headers.get("x-forwarded-proto", "") == "https")
        if cookie_path != "/":
            resp.delete_cookie(
                AUTH_COOKIE, path="/",
                httponly=True, samesite="lax", secure=is_secure,
            )
        resp.set_cookie(
            AUTH_COOKIE, _issue_session(),
            httponly=True, samesite="lax", max_age=SESSION_TTL_SECONDS,
            path=cookie_path,
            # 反代后 request.url.scheme 可能是 http，这里同时看 x-forwarded-proto
            secure=is_secure,
        )
        return resp

    _record_login_failure(ip)
    print(f"🔐 [登录失败] 来自 {ip} 的密码尝试失败。")
    return JSONResponse(status_code=401, content={"error": "密码错误"})


@app.post("/api/logout")
async def logout(request: Request):
    for token in _session_tokens(request):
        _revoke_session(token)
    resp = JSONResponse(content={"ok": True})
    cookie_path = _cookie_path(request)
    is_secure = (request.url.scheme == "https"
                 or request.headers.get("x-forwarded-proto", "") == "https")
    resp.delete_cookie(
        AUTH_COOKIE, path=cookie_path,
        httponly=True, samesite="lax", secure=is_secure,
    )
    if cookie_path != "/":
        resp.delete_cookie(
            AUTH_COOKIE, path="/",
            httponly=True, samesite="lax", secure=is_secure,
        )
    return resp


@app.get("/api/stats")
async def get_stats_api(_auth: bool = Depends(require_auth)):
    return JSONResponse(content=stats.get_json_stats())


def _build_credentials_overview() -> dict:
    overview = usage_ledger.get_overview_summary(recent_limit=40)
    cred_stats = overview.get("credentials") or {}
    empty_recent = usage_ledger.build_recent_status([])
    cards = []

    app_state.import_env_express_keys_once(config.VERTEX_EXPRESS_API_KEY_VAL)
    for idx, rec in enumerate(app_state.get_express_keys()):
        cid = express_credential_id(rec.get("key", ""))
        st = cred_stats.get(cid) or {}
        label = format_express_label(dict(rec, index=idx))
        cards.append({
            "cred_id": cid,
            "channel": "express",
            "label": label,
            "subtitle": mask_express_key(rec.get("key", "")),
            "project_id": rec.get("project_id", ""),
            "project_source": rec.get("project_source", ""),
            "enabled": bool(rec.get("enabled", True)),
            "proxy_mode": rec.get("proxy_mode", "inherit"),
            "proxy_id": rec.get("proxy_id", ""),
            "proxy_label": format_proxy_label(rec),
            "history": st.get("history") or {},
            "recent_status": st.get("recent_status") or empty_recent,
        })

    app_state.import_cookie_credentials_once(config.GOOGLE_COOKIE, config.GOOGLE_PROJECT_ID)
    for rec in app_state.get_cookie_credentials():
        cid = str(rec.get("id") or "")
        st = cred_stats.get(cid) or {}
        cards.append({
            "cred_id": cid,
            "channel": "cookie",
            "label": format_cookie_label(rec),
            "subtitle": rec.get("project_id", "") or cid[:8],
            "project_id": rec.get("project_id", ""),
            "enabled": bool(rec.get("enabled", True)),
            "proxy_mode": rec.get("proxy_mode", "inherit"),
            "proxy_id": rec.get("proxy_id", ""),
            "proxy_label": format_proxy_label(rec),
            "history": st.get("history") or {},
            "recent_status": st.get("recent_status") or empty_recent,
        })

    sess = stats.get_json_stats()
    return {
        "uptime": sess.get("uptime", 0),
        "use_web_proxy": app_state.is_web_proxy_enabled(),
        "roundrobin": bool(app_state.get_setting("roundrobin", config.ROUNDROBIN)),
        "dispatch": app_state.get_dispatch_settings(),
        "session": sess,
        "global": overview.get("global") or {},
        "recent_status": overview.get("recent_status") or empty_recent,
        "credentials": cards,
        "recent_events": overview.get("recent_events") or [],
    }


@app.get("/api/credentials/overview")
async def get_credentials_overview_api(_auth: bool = Depends(require_auth)):
    return JSONResponse(content=_build_credentials_overview(), headers={"Cache-Control": "no-store"})


@app.get("/api/credentials/{cred_id}/history")
async def get_credential_history_api(cred_id: str, _auth: bool = Depends(require_auth)):
    return JSONResponse(
        content=usage_ledger.get_credential_summary(cred_id, recent_limit=80),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/credentials/{cred_id}/stats/reset")
async def reset_credential_stats_api(cred_id: str, _auth: bool = Depends(require_auth)):
    usage_ledger.reset_credential(cred_id)
    return JSONResponse(content={"ok": True, "cred_id": cred_id}, headers={"Cache-Control": "no-store"})


@app.post("/api/stats/reset")
async def reset_all_stats_api(_auth: bool = Depends(require_auth)):
    usage_ledger.reset_all()
    return JSONResponse(content={"ok": True}, headers={"Cache-Control": "no-store"})


@app.get("/api/service/status")
async def service_status_api():
    # Restart clears login sessions. Expose only an opaque boot ID and generic
    # restart state so the old page can distinguish the new, ready process.
    return JSONResponse(content=service_control.status(), headers={"Cache-Control": "no-store"})


@app.post("/api/service/restart", status_code=202)
async def restart_service_api(request: Request, _auth: bool = Depends(require_auth)):
    # Custom header + same-origin fetch: cross-site forms cannot trigger a restart.
    # CORS deliberately does not allow credentials, so cross-site JS cannot either.
    if request.headers.get("X-Requested-With") != "service-restart":
        raise HTTPException(status_code=403, detail="请使用控制台的重启按钮。")
    try:
        service_control.request_restart()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse(status_code=202, content=service_control.status(),
                        headers={"Cache-Control": "no-store"})


# ==========================================
# 设置与通道控制
# ==========================================
class ModeSetting(BaseModel):
    mode: str


class NetworkProxySetting(BaseModel):
    mode: Literal["environment", "custom", "direct", "pool"]
    proxy_url: str | None = None
    proxy_id: str | None = None


class ProxyPoolEntrySetting(BaseModel):
    name: str
    proxy_url: str | None = None


class ProxyPoolCurrentSetting(BaseModel):
    name: str = "当前代理"


def _proxy_action_response(action, *args, **kwargs):
    try:
        status = action(*args, **kwargs)
    except network_proxy.ProxyEntryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except OSError:
        raise HTTPException(status_code=500, detail="代理配置保存失败，请检查状态目录权限和剩余空间。") from None
    return JSONResponse(content=status, headers={"Cache-Control": "no-store"})


@app.get("/api/proxy")
async def get_network_proxy_api(_auth: bool = Depends(require_auth)):
    return JSONResponse(content=network_proxy.get_proxy_status(), headers={"Cache-Control": "no-store"})


@app.post("/api/proxy")
async def save_network_proxy_api(body: NetworkProxySetting, _auth: bool = Depends(require_auth)):
    return _proxy_action_response(network_proxy.save_proxy_config, body.mode, body.proxy_url, body.proxy_id)


@app.post("/api/proxy/pool")
async def add_proxy_pool_entry_api(body: ProxyPoolEntrySetting, _auth: bool = Depends(require_auth)):
    return _proxy_action_response(network_proxy.add_proxy_entry, body.name, body.proxy_url)


@app.post("/api/proxy/pool/current")
async def import_current_proxy_api(body: ProxyPoolCurrentSetting, _auth: bool = Depends(require_auth)):
    return _proxy_action_response(network_proxy.add_proxy_entry, body.name, use_current=True)


@app.put("/api/proxy/pool/{proxy_id}")
async def edit_proxy_pool_entry_api(proxy_id: str, body: ProxyPoolEntrySetting, _auth: bool = Depends(require_auth)):
    return _proxy_action_response(network_proxy.edit_proxy_entry, proxy_id, body.name, body.proxy_url)


@app.delete("/api/proxy/pool/{proxy_id}")
async def delete_proxy_pool_entry_api(proxy_id: str, _auth: bool = Depends(require_auth)):
    return _proxy_action_response(network_proxy.delete_proxy_entry, proxy_id)


@app.post("/api/proxy/test")
async def test_network_proxy_api(body: NetworkProxySetting, _auth: bool = Depends(require_auth)):
    try:
        result = await network_proxy.test_proxy_connection(body.mode, body.proxy_url, body.proxy_id)
    except network_proxy.ProxyEntryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return JSONResponse(content=result, headers={"Cache-Control": "no-store"})


@app.get("/api/settings/runtime")
async def get_runtime_settings(_auth: bool = Depends(require_auth)):
    app_state.import_cookie_credentials_once(config.GOOGLE_COOKIE, config.GOOGLE_PROJECT_ID)
    records = app_state.get_cookie_credentials()
    first = records[0] if records else {}
    cookie = first.get("cookie", "")
    return JSONResponse(content={
        "use_web_proxy": app_state.is_web_proxy_enabled(),
        # S-1：只回显掩码。完整 Cookie 等价于该 Google 账号的完整访问权，
        # 没有任何理由让它出现在前端 JS / 浏览器缓存 / 截图里。
        "google_cookie": mask_cookie(cookie),
        "google_cookie_configured": bool(cookie),
        "google_project_id": first.get("project_id", ""),
        "cookie_credentials_count": len(records),
    })


@app.post("/api/settings/mode")
async def set_settings_mode(setting: ModeSetting, _auth: bool = Depends(require_auth)):
    app_state.enable_web_proxy(setting.mode == "web_proxy")
    return JSONResponse(content={"status": "success"})


@app.get("/api/settings")
async def get_settings_api(_auth: bool = Depends(require_auth)):
    return JSONResponse(content=app_state.get_settings())


@app.post("/api/settings")
async def update_settings_api(request: Request, _auth: bool = Depends(require_auth)):
    try:
        patch = await request.json()
    except Exception:
        patch = {}
    if not isinstance(patch, dict):
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON 对象。"})
    try:
        updated = app_state.update_settings(patch)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return JSONResponse(content=updated)


@app.get("/api/capabilities")
async def get_capabilities_api(_auth: bool = Depends(require_auth)):
    try:
        models = await get_express_models()
    except Exception:
        models = []
    # 能力摘要要反映该模型生效的采样策略，否则控制台提示与实际下发不一致
    caps = {m: mc.capabilities_summary(m, app_state.get_effective_settings(m)) for m in models}
    # 附带各模型是否已有专属参数覆盖，供前端标示
    overrides = app_state.get_model_overrides()
    return JSONResponse(content={"models": models, "capabilities": caps, "overrides": overrides})


# ==========================================
# 按模型参数覆盖（per-model overrides）
# ==========================================
@app.get("/api/model-overrides")
async def list_model_overrides(_auth: bool = Depends(require_auth)):
    return JSONResponse(content=app_state.get_model_overrides())


@app.post("/api/model-overrides/{model_name}")
async def save_model_override(model_name: str, request: Request, _auth: bool = Depends(require_auth)):
    try:
        patch = await request.json()
    except Exception:
        patch = {}
    if not isinstance(patch, dict):
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON 对象。"})
    saved = app_state.set_model_override(model_name, patch)
    return JSONResponse(content={"status": "success", "model": model_name, "override": saved})


@app.delete("/api/model-overrides/{model_name}")
async def delete_model_override(model_name: str, _auth: bool = Depends(require_auth)):
    ok = app_state.clear_model_override(model_name)
    return JSONResponse(content={"status": "success" if ok else "not_found", "model": model_name})


# ==========================================
# Express API Key 列表（多账号，热编辑）
# ==========================================
class ExpressKeysBody(BaseModel):
    keys: list = []


class ProbeBody(BaseModel):
    key: str = ""             # 指定单个 Key 探测
    all: bool = False         # 探测全部
    force: bool = False       # 忽略「已有 Project ID / 人工填写」的跳过规则


class KeyProjectBody(BaseModel):
    key: str
    project_id: str = ""


def _express_keys_payload() -> dict:
    """控制台按用户要求**明文**返回 Key（控制台本身有密码保护）；日志里仍打掩码。"""
    records = app_state.get_express_keys()
    return {
        "keys": records,
        "total": len(records),
        "enabled": sum(1 for r in records if r.get("enabled", True)),
        "roundrobin": bool(app_state.get_setting("roundrobin", config.ROUNDROBIN)),
        "env_import_done": True,
    }


@app.get("/api/express-keys")
async def list_express_keys(_auth: bool = Depends(require_auth)):
    return JSONResponse(content=_express_keys_payload(), headers={"Cache-Control": "no-store"})


@app.post("/api/express-keys")
async def save_express_keys(body: ExpressKeysBody, _auth: bool = Depends(require_auth)):
    """整表覆盖保存（增删改都走这里），立即热生效，无需重启。

    保存空列表是合法操作：之后即使环境变量 VERTEX_EXPRESS_API_KEY 仍有值，
    启动时也不会再把 Key 复种回来（见 runtime_state.import_env_express_keys_once）。
    """
    if not isinstance(body.keys, list):
        return JSONResponse(status_code=400, content={"error": "keys 必须是数组。"})
    try:
        for record in body.keys:
            if not isinstance(record, (dict, str)):
                raise ValueError("每条 Key 凭证应为对象或字符串。")
            network_proxy.normalize_credential_proxy(record if isinstance(record, dict) else {})
        saved = app_state.set_express_keys(body.keys)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except OSError:
        return JSONResponse(status_code=500, content={"error": "凭证保存失败，请检查状态目录。"})
    express_key_manager.refresh_keys()
    # 新加进来、还没有 Project ID 的 Key 立刻在后台探一次
    pending = [r["key"] for r in saved if not r.get("project_id")]
    if pending:
        asyncio.create_task(express_key_probe.probe_all(keys=pending))
    payload = _express_keys_payload()
    payload["status"] = "success"
    return JSONResponse(content=payload)


@app.post("/api/express-keys/probe")
async def probe_express_keys(body: ProbeBody, _auth: bool = Depends(require_auth)):
    """手动触发探测：指定单个 Key，或 all=true 探测全部。"""
    if body.all:
        results = await express_key_probe.probe_all(force=body.force)
    else:
        key = (body.key or "").strip()
        if not key:
            return JSONResponse(status_code=400, content={"error": "请提供 key，或设 all=true。"})
        results = [await express_key_probe.probe_and_store(key, force=body.force)]
    payload = _express_keys_payload()
    payload["results"] = results
    return JSONResponse(content=payload)


@app.post("/api/express-keys/project")
async def set_express_key_project(body: KeyProjectBody, _auth: bool = Depends(require_auth)):
    """人工覆盖某个 Key 的 Project ID（来源标记为 manual，自动探测不会再覆盖它）。"""
    ok = app_state.update_express_key_project(
        body.key, body.project_id, source=("manual" if body.project_id.strip() else ""))
    if not ok:
        return JSONResponse(status_code=404, content={"error": "该 Key 不在列表中。"})
    payload = _express_keys_payload()
    payload["status"] = "success"
    return JSONResponse(content=payload)


class CookieSetting(BaseModel):
    cookie: str = ""          # 留空 = 保持现有 Cookie
    project_id: str = ""


def _cookie_credentials_payload() -> dict:
    app_state.import_cookie_credentials_once(config.GOOGLE_COOKIE, config.GOOGLE_PROJECT_ID)
    records = app_state.get_cookie_credentials()
    return {"credentials": [{k: v for k, v in rec.items() if k != "cookie"} |
                            {"cookie_configured": bool(rec.get("cookie")),
                             "cookie_masked": mask_cookie(rec.get("cookie", ""))} for rec in records],
            "total": len(records), "enabled": sum(bool(r.get("enabled", True)) for r in records),
            "roundrobin": bool(app_state.get_setting("roundrobin", config.ROUNDROBIN))}


@app.get("/api/cookie-credentials")
async def list_cookie_credentials(_auth: bool = Depends(require_auth)):
    return JSONResponse(content=_cookie_credentials_payload(), headers={"Cache-Control": "no-store"})


@app.post("/api/cookie-credentials")
async def save_cookie_credentials(request: Request, _auth: bool = Depends(require_auth)):
    try:
        body = await request.json()
        if not isinstance(body, dict) or "credentials" not in body:
            raise ValueError("请提供 credentials 数组。")
        app_state.import_cookie_credentials_once(config.GOOGLE_COOKIE, config.GOOGLE_PROJECT_ID)
        app_state.set_cookie_credentials(body["credentials"])
    except ValueError as exc:
        # json decoder exceptions may include submitted content; use a fixed message.
        message = "请求体应为有效 JSON。" if isinstance(exc, json.JSONDecodeError) else str(exc)
        return JSONResponse(status_code=400, content={"error": message}, headers={"Cache-Control": "no-store"})
    except OSError:
        return JSONResponse(status_code=500, content={"error": "凭证保存失败，请检查状态目录。"})
    cookie_credential_manager.reset()
    return JSONResponse(content={"status": "success", **_cookie_credentials_payload()},
                        headers={"Cache-Control": "no-store"})


@app.post("/api/cookie")
async def set_google_cookie(setting: CookieSetting, _auth: bool = Depends(require_auth)):
    """保存 Cookie 与 Project ID。

    S-1：cookie 传空字符串表示「保持现有 Cookie 不变，只更新 Project ID」，
    这样前端就不需要为了改 Project ID 而把完整 Cookie 再取回来一次。
    """
    new_cookie = (setting.cookie or "").strip()
    project_id = (setting.project_id or "").strip()

    if new_cookie:
        validation = validate_cookie(new_cookie)
        if not validation["valid"]:
            return JSONResponse(status_code=400, content={"error": validation["message"]})
        message = validation["message"]
    else:
        message = "✅ 已保留原有 Cookie，仅更新 Project ID。"

    try:
        app_state.import_cookie_credentials_once(config.GOOGLE_COOKIE, config.GOOGLE_PROJECT_ID)
        records = app_state.get_cookie_credentials()
        record = dict(records[0]) if records else {"name": "原 Cookie 凭证"}
        if not new_cookie and not record.get("cookie"):
            return JSONResponse(status_code=400, content={"error": "尚未配置 Cookie，请粘贴完整的 Google Cookie。"})
        if new_cookie:
            record["cookie"] = new_cookie
        if project_id:
            record["project_id"] = project_id
        app_state.set_cookie_credentials([record, *records[1:]])
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except OSError:
        return JSONResponse(status_code=500, content={"error": "凭证保存失败，请检查状态目录。"})
    cookie_credential_manager.reset()
    return JSONResponse(content={"status": "success", "message": message})


@app.get("/stream-logs")
async def stream_logs_endpoint(request: Request, _auth: bool = Depends(require_auth)):
    def _event_chunks(msg: str):
        # SSE 规范写法：多行消息必须拆成多个 `data:` 行，浏览器会用换行拼回 e.data。
        # 之前是 yield f"data: {msg}\n\n"，msg 里的裸换行会导致换行后的每一行被浏览器
        # 当成未知字段直接丢弃——多行诊断、traceback 在控制台永远只剩第一行，
        # 只能去 Docker 日志看完整版（2026-10-04 修复）。
        for line in msg.splitlines() or [""]:
            yield f"data: {line}\n"
        yield "\n"

    async def log_generator():
        q = rt_logger.subscribe()
        try:
            for msg in rt_logger.snapshot_history():
                for chunk in _event_chunks(msg):
                    yield chunk
            while not service_control.status()["restarting"]:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=1.0)
                    for chunk in _event_chunks(msg):
                        yield chunk
                except asyncio.TimeoutError:
                    yield ": keep-alive heartbeat\n\n"
        finally:
            rt_logger.unsubscribe(q)
    return StreamingResponse(log_generator(), media_type="text/event-stream")


app.include_router(models_api.router)
app.include_router(chat_api.router)
app.add_middleware(DisconnectMiddleware)
