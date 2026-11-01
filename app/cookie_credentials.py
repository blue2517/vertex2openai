"""Cookie credential validation and thread-safe random / round-robin selection."""

import random
import re
import threading
import uuid
from urllib.parse import parse_qs, urlsplit

import config
from cookie_auth import validate_cookie
from runtime_state import app_state

MAX_COOKIE_CREDENTIALS = 100


def normalize_cookie_records(records: list, previous: list) -> list:
    from network_proxy import normalize_credential_proxy
    if not isinstance(records, list) or len(records) > MAX_COOKIE_CREDENTIALS:
        raise ValueError(f"Cookie 凭证列表应为数组，最多 {MAX_COOKIE_CREDENTIALS} 条。")
    old = {r["id"]: r for r in previous}
    clean, ids, pairs = [], set(), set()
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("每条 Cookie 凭证应为对象。")
        ident = str(item.get("id") or uuid.uuid4().hex).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident) or ident in ids:
            raise ValueError("Cookie 凭证 ID 无效或重复，请刷新列表。")
        ids.add(ident)
        name = str(item.get("name") or "Cookie 凭证").strip()
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError("凭证名称需为 1–80 个字符。")
        cookie = str(item.get("cookie") or "").strip() or old.get(ident, {}).get("cookie", "")
        if len(cookie) > 65536 or "\n" in cookie or "\r" in cookie or not validate_cookie(cookie)["valid"]:
            raise ValueError("请为每条凭证填写完整 Cookie（含 SAPISID）；已有凭证留空保留原值。")
        project = str(item.get("project_id") or "").strip()
        if project.startswith("https://"):
            project = parse_qs(urlsplit(project).query).get("project", [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", project):
            raise ValueError("请为每条 Cookie 凭证填写所属的 Project ID。")
        if (cookie, project) in pairs:
            raise ValueError("同一 Cookie 和项目已存在，请合并重复条目。")
        pairs.add((cookie, project))
        if "enabled" in item and not isinstance(item["enabled"], bool):
            raise ValueError("enabled 应为布尔值。")
        clean.append({"id": ident, "name": name, "cookie": cookie, "project_id": project,
                      "enabled": item.get("enabled", True), **normalize_credential_proxy(item)})
    return clean


class CookieCredentialManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._index = 0

    def get_record(self):
        with self._lock:
            records = [r for r in app_state.get_cookie_credentials() if r.get("enabled", True)]
            if not records:
                return None
            if app_state.get_setting("roundrobin", config.ROUNDROBIN):
                index = self._index % len(records)
                self._index = (index + 1) % len(records)
            else:
                index = random.randrange(len(records))
            return records[index]

    def reset(self):
        with self._lock:
            self._index = 0


cookie_credential_manager = CookieCredentialManager()
