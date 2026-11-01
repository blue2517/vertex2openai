"""共享的动态出网代理：环境变量兼容、控制台覆盖及显式直连。"""

import asyncio
import time
import uuid
from contextvars import ContextVar
from functools import wraps
from urllib.parse import urlsplit

import httpx
from starlette.responses import StreamingResponse

import config as app_config
from runtime_state import app_state

PROXY_MODES = ("environment", "custom", "direct", "pool")
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
PROXY_TEST_URL = "https://www.gstatic.com/generate_204"
MAX_PROXY_POOL_SIZE = 100
_request_proxy_args = ContextVar("credential_proxy_args", default=None)


class ProxyEntryNotFound(ValueError):
    pass


def validate_proxy_url(value: str) -> str:
    url = value.strip()
    if not url or len(url) > 4096 or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("请输入完整代理地址；认证信息中的特殊字符请使用 URL 编码。")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in PROXY_SCHEMES or not parsed.hostname:
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if parsed.scheme.startswith("socks") and port is None:
            raise ValueError
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError
        httpx.Proxy(url)
    except (ValueError, httpx.InvalidURL):
        # 错误文本只描述格式，绝不包含原始 URL / 用户名 / 密码。
        raise ValueError("代理格式应为 http(s)://主机:端口 或 socks5(h)://主机:端口。") from None
    return url


def mask_proxy_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in PROXY_SCHEMES or not parsed.hostname:
            return "（代理地址格式异常）"
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        auth = "***:***@" if parsed.username is not None or parsed.password is not None else ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{auth}{host}{port}"
    except ValueError:
        return "（代理地址格式异常）"


def _environment_proxy() -> str:
    return (app_config.PROXY_URL or "").strip()


def _effective_url(conf: dict) -> str | None:
    if conf["mode"] == "direct":
        return None
    if conf["mode"] == "custom":
        return conf["url"] or None
    if conf["mode"] == "pool":
        entry = next((item for item in conf["pool"] if item["id"] == conf["active_id"]), None)
        if entry is None:
            raise ValueError("当前代理池条目已失效，请重新选择。")
        return entry["url"]
    return _environment_proxy() or None


def get_proxy_client_args() -> dict:
    """每次创建客户端时取一致快照；显式直连/自定义不受 HTTP_PROXY 等干扰。"""
    snapshot = _request_proxy_args.get()
    if snapshot is not None:
        return dict(snapshot)
    return _global_proxy_client_args()


def _global_proxy_client_args() -> dict:
    conf = app_state.get_network_proxy()
    args = {"trust_env": conf["mode"] == "environment"}
    url = _effective_url(conf)
    if url:
        args["proxy"] = url
    return args


def normalize_credential_proxy(record: dict) -> dict:
    mode = record.get("proxy_mode", "inherit") or "inherit"
    ident = record.get("proxy_id", "") or ""
    if mode not in ("inherit", "direct", "pool") or not isinstance(ident, str):
        raise ValueError("凭证代理请选择跟随全局、直接连接或代理池。")
    if mode == "pool":
        entry = next((p for p in app_state.get_network_proxy()["pool"] if p["id"] == ident), None)
        if entry is None:
            raise ProxyEntryNotFound("凭证绑定的代理已失效，请重新选择代理池节点。")
        validate_proxy_url(entry["url"])
    return {"proxy_mode": mode, "proxy_id": ident if mode == "pool" else ""}


def get_credential_proxy_args(record: dict | None = None) -> dict:
    binding = normalize_credential_proxy(record or {})
    if binding["proxy_mode"] == "inherit":
        return _global_proxy_client_args()
    if binding["proxy_mode"] == "direct":
        return {"trust_env": False}
    # Resolve once, then keep the URL snapshot for the full request lifetime.
    conf = app_state.get_network_proxy()
    entry = next((p for p in conf["pool"] if p["id"] == binding["proxy_id"]), None)
    if entry is None:
        raise ProxyEntryNotFound("凭证绑定的代理已失效，请重新选择代理池节点。")
    return {"trust_env": False, "proxy": entry["url"]}


def bind_credential_proxy(record: dict | None = None) -> None:
    _request_proxy_args.set(get_credential_proxy_args(record))


def credential_proxy_scope(func):
    """Isolate concurrent jobs, worker threads and deferred streaming iterators."""
    @wraps(func)
    async def wrapped(*args, **kwargs):
        token = _request_proxy_args.set(None)
        try:
            response = await func(*args, **kwargs)
            snapshot = _request_proxy_args.get()
        finally:
            _request_proxy_args.reset(token)
        if isinstance(response, StreamingResponse) and snapshot is not None:
            original = response.body_iterator

            async def stream():
                try:
                    while True:
                        stream_token = _request_proxy_args.set(snapshot)
                        try:
                            chunk = await anext(original)
                        except StopAsyncIteration:
                            return
                        finally:
                            _request_proxy_args.reset(stream_token)
                        # Never keep a context token across a yield: callers can
                        # consume or close the iterator from different tasks.
                        yield chunk
                finally:
                    stream_token = _request_proxy_args.set(snapshot)
                    try:
                        if hasattr(original, "aclose"):
                            await original.aclose()
                    finally:
                        _request_proxy_args.reset(stream_token)
            response.body_iterator = stream()
        return response
    return wrapped


def credential_proxy_bindings() -> dict:
    counts = {}
    for record in app_state.get_express_keys() + app_state.get_cookie_credentials():
        if record.get("proxy_mode") == "pool" and record.get("proxy_id"):
            ident = record["proxy_id"]
            counts[ident] = counts.get(ident, 0) + 1
    return counts


def get_proxy_status() -> dict:
    conf = app_state.get_network_proxy()
    effective = _effective_url(conf)
    return {
        "mode": conf["mode"],
        "enabled": bool(effective),
        "effective_url_masked": mask_proxy_url(effective),
        "custom_url_masked": mask_proxy_url(conf["url"]),
        "environment_url_masked": mask_proxy_url(_environment_proxy()),
        "custom_configured": bool(conf["url"]),
        "active_id": conf["active_id"],
        "current_in_pool": bool(effective and any(item["url"] == effective for item in conf["pool"])),
        "bound_credentials": credential_proxy_bindings(),
        "pool": [{"id": item["id"], "name": item["name"], "url_masked": mask_proxy_url(item["url"]),
                  "active": conf["mode"] == "pool" and item["id"] == conf["active_id"]}
                 for item in conf["pool"]],
    }


def resolve_proxy_config(mode: str, url: str | None = None, proxy_id: str | None = None,
                         *, saved: dict | None = None) -> dict:
    if mode not in PROXY_MODES:
        raise ValueError("请选择跟随环境变量、自定义代理、代理池或直接连接。")
    saved = saved if saved is not None else app_state.get_network_proxy()
    if mode == "custom":
        # 输入框留空保持已保存地址；首次从环境配置切换时可沿用原代理。
        candidate = (url or "").strip() or saved["url"] or _environment_proxy()
        custom = validate_proxy_url(candidate)
    else:
        custom = saved["url"]
    if mode == "environment" and _environment_proxy():
        validate_proxy_url(_environment_proxy())
    active_id = saved["active_id"]
    if mode == "pool":
        active_id = proxy_id or active_id
        entry = next((item for item in saved["pool"] if item["id"] == active_id), None)
        if entry is None:
            raise ProxyEntryNotFound("请选择代理池中的有效代理。")
        validate_proxy_url(entry["url"])
    return {**saved, "mode": mode, "url": custom, "active_id": active_id}


def save_proxy_config(mode: str, url: str | None = None, proxy_id: str | None = None) -> dict:
    app_state.update_network_proxy(lambda saved: resolve_proxy_config(mode, url, proxy_id, saved=saved))
    return get_proxy_status()


def _entry_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise ValueError("代理名称需为 1–80 个字符。")
    return name


def add_proxy_entry(name: str, url: str | None = None, *, use_current: bool = False) -> dict:
    name = _entry_name(name)

    def update(conf):
        candidate = _effective_url(conf) if use_current else url
        if not candidate:
            raise ValueError("请填写代理地址，或先配置可用的当前代理。")
        candidate = validate_proxy_url(candidate)
        if any(item["url"] == candidate for item in conf["pool"]):
            raise ValueError("该代理已在列表中，可直接使用或编辑。")
        if len(conf["pool"]) >= MAX_PROXY_POOL_SIZE:
            raise ValueError(f"代理池最多保存 {MAX_PROXY_POOL_SIZE} 个代理。")
        entry = {"id": uuid.uuid4().hex, "name": name, "url": candidate}
        return {**conf, "pool": [*conf["pool"], entry], "active_id": conf["active_id"] or entry["id"]}

    app_state.update_network_proxy(update)
    return get_proxy_status()


def edit_proxy_entry(proxy_id: str, name: str, url: str | None = None) -> dict:
    name = _entry_name(name)

    def update(conf):
        entry = next((item for item in conf["pool"] if item["id"] == proxy_id), None)
        if entry is None:
            raise ProxyEntryNotFound("代理条目不存在，请刷新列表。")
        candidate = validate_proxy_url((url or "").strip() or entry["url"])
        if any(item["id"] != proxy_id and item["url"] == candidate for item in conf["pool"]):
            raise ValueError("该代理地址已在列表中。")
        entries = [{**item, "name": name, "url": candidate} if item["id"] == proxy_id else item
                   for item in conf["pool"]]
        return {**conf, "pool": entries}

    app_state.update_network_proxy(update)
    return get_proxy_status()


def delete_proxy_entry(proxy_id: str) -> dict:
    def update(conf):
        if not any(item["id"] == proxy_id for item in conf["pool"]):
            raise ProxyEntryNotFound("代理条目不存在，请刷新列表。")
        if conf["mode"] == "pool" and conf["active_id"] == proxy_id:
            raise ValueError("请先切换到其他代理或连接方式，再删除正在使用的代理。")
        if credential_proxy_bindings().get(proxy_id):
            raise ValueError("此代理仍被凭证绑定，请先在 Cookie / Key 列表解除绑定并保存。")
        entries = [item for item in conf["pool"] if item["id"] != proxy_id]
        selected = conf["active_id"]
        if selected == proxy_id:
            selected = entries[0]["id"] if entries else ""
        return {**conf, "pool": entries, "active_id": selected}

    app_state.update_network_proxy(update)
    return get_proxy_status()


async def test_proxy_connection(mode: str, url: str | None = None, proxy_id: str | None = None) -> dict:
    conf = resolve_proxy_config(mode, url, proxy_id)
    args = {"trust_env": mode == "environment", "timeout": httpx.Timeout(8.0, connect=5.0)}
    proxy = _effective_url(conf)
    if proxy:
        args["proxy"] = proxy
    if app_config.SSL_CERT_FILE:
        args["verify"] = app_config.SSL_CERT_FILE
    started = time.monotonic()
    try:
        # 固定 Google 公共网络探测端点，不接收任意目标 URL，不附带 API Key / Cookie。
        async with httpx.AsyncClient(**args) as client:
            response = await asyncio.wait_for(client.get(PROXY_TEST_URL), timeout=10.0)
        ok = response.status_code == 204
        message = "Google 网络连通。" if ok else f"Google 探测返回 HTTP {response.status_code}。"
        status = response.status_code
    except (httpx.HTTPError, OSError, ValueError, asyncio.TimeoutError):
        # 部分库异常包含代理认证信息，避免直接回传异常字符串或打印日志。
        ok, status = False, None
        message = "连接测试失败，请检查代理地址、认证信息和网络连通性。"
    return {"ok": ok, "http_status": status, "elapsed_ms": round((time.monotonic() - started) * 1000),
            "message": message, "proxy_url_masked": mask_proxy_url(proxy)}
