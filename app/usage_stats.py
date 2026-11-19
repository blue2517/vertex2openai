"""Persistent per-credential usage ledger, success rates, 200-minute status strip, and call history."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import runtime_state

RECENT_BLOCK_COUNT = 20
RECENT_BLOCK_DURATION_MS = 10 * 60 * 1000  # 20 x 10min = 200 minutes
MAX_RETAINED_EVENTS = 5000
TRIM_THRESHOLD_LINES = 6000


def express_credential_id(key: str) -> str:
    """Stable non-secret identifier for an Express API Key."""
    clean = str(key or "").strip()
    if not clean:
        return "exp_unknown"
    return "exp_" + hashlib.sha256(clean.encode("utf-8")).hexdigest()[:12]


def mask_express_key(key: str) -> str:
    clean = str(key or "").strip()
    if len(clean) <= 8:
        return "***"
    return f"{clean[:4]}...{clean[-4:]}"


def format_express_label(record: Optional[dict]) -> str:
    if not record:
        return "Express"
    note = str(record.get("note") or "").strip()
    if note:
        return note
    idx = record.get("index")
    if isinstance(idx, int):
        return f"Key #{idx + 1}"
    key = str(record.get("key") or "").strip()
    if key:
        try:
            keys = runtime_state.app_state.get_express_keys()
            for i, item in enumerate(keys):
                if item.get("key") == key:
                    item_note = str(item.get("note") or "").strip()
                    return item_note or f"Key #{i + 1}"
        except Exception:
            pass
    project = str(record.get("project_id") or "").strip()
    if project:
        return f"Key({project})"
    return f"Key({mask_express_key(key)})" if key else "Express"


def format_cookie_label(record: Optional[dict]) -> str:
    if not record:
        return "Cookie"
    name = str(record.get("name") or "").strip()
    if name:
        return name
    ident = str(record.get("id") or "").strip()
    return f"Cookie #{ident[:6]}" if ident else "Cookie"


def format_proxy_label(record: Optional[dict]) -> str:
    """Return human-readable proxy label for logs and UI without leaking credentials."""
    try:
        net = runtime_state.app_state.get_network_proxy()
    except Exception:
        net = {"mode": "environment", "url": "", "pool": [], "active_id": ""}
    pool_map = {
        str(p.get("id") or ""): str(p.get("name") or "代理池")
        for p in (net.get("pool") or [])
        if isinstance(p, dict)
    }

    mode = str((record or {}).get("proxy_mode") or "inherit").strip()
    proxy_id = str((record or {}).get("proxy_id") or "").strip()
    if mode == "direct":
        return "直连"
    if mode == "pool":
        return pool_map.get(proxy_id, "代理池(失效)")

    global_mode = str(net.get("mode") or "environment")
    if global_mode == "direct":
        return "直连(全局)"
    if global_mode == "pool":
        name = pool_map.get(str(net.get("active_id") or ""), "代理池")
        return f"{name}(全局)"
    if global_mode == "custom":
        return "自定义代理(全局)"
    return "环境代理" if os.environ.get("PROXY_URL") or net.get("url") else "直连"


def _empty_totals() -> Dict[str, Any]:
    return {
        "total_requests": 0,
        "success_calls": 0,
        "failure_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "total_latency_ms": 0,
        "latency_samples": 0,
        "total_ttft_ms": 0,
        "ttft_samples": 0,
        "last_seen_ms": None,
        "last_status": None,
        "last_error": "",
        "last_model": "",
        "channel": "",
        "cred_label": "",
        "proxy_label": "",
    }


def _enrich_totals(raw: Dict[str, Any]) -> Dict[str, Any]:
    out = _empty_totals()
    if isinstance(raw, dict):
        for k in out:
            if k in raw and raw[k] is not None:
                out[k] = raw[k]
    total_req = int(out["total_requests"] or 0)
    succ = int(out["success_calls"] or 0)
    out["success_rate"] = round((succ / total_req) * 100.0, 1) if total_req > 0 else None
    lat_n = int(out["latency_samples"] or 0)
    out["avg_latency_ms"] = int(round(int(out["total_latency_ms"] or 0) / lat_n)) if lat_n > 0 else None
    ttft_n = int(out["ttft_samples"] or 0)
    out["avg_ttft_ms"] = int(round(int(out["total_ttft_ms"] or 0) / ttft_n)) if ttft_n > 0 else None
    return out


def _atomic_write_json(path: str, payload: Any) -> None:
    target_dir = os.path.dirname(path) or "."
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".usage-", suffix=".tmp", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class CredentialUsageLedger:
    """Thread-safe persistent usage ledger stored alongside web_state.json."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event_lines_since_trim = 0

    def _state_dir(self) -> str:
        return os.path.dirname(runtime_state.STATE_FILE) or "."

    def _totals_path(self) -> str:
        return os.path.join(self._state_dir(), "usage_totals.json")

    def _events_path(self) -> str:
        return os.path.join(self._state_dir(), "usage_events.jsonl")

    def _load_totals_locked(self) -> Dict[str, Any]:
        path = self._totals_path()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {
                        "global": data.get("global") if isinstance(data.get("global"), dict) else _empty_totals(),
                        "credentials": data.get("credentials") if isinstance(data.get("credentials"), dict) else {},
                    }
            except Exception:
                pass
        return {"global": _empty_totals(), "credentials": {}}

    def _load_events_locked(self, cred_id: Optional[str] = None) -> List[Dict[str, Any]]:
        path = self._events_path()
        if not os.path.isfile(path):
            return []
        events: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if cred_id is not None and obj.get("cred_id") != cred_id:
                        continue
                    events.append(obj)
        except Exception:
            return []
        return events

    def _maybe_trim_events_locked(self) -> None:
        self._event_lines_since_trim += 1
        if self._event_lines_since_trim < 200:
            return
        self._event_lines_since_trim = 0
        path = self._events_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) <= TRIM_THRESHOLD_LINES:
                return
            kept = lines[-MAX_RETAINED_EVENTS:]
            target_dir = os.path.dirname(path) or "."
            fd, tmp_path = tempfile.mkstemp(prefix=".usage-ev-", suffix=".tmp", dir=target_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except Exception:
            pass

    @staticmethod
    def _apply_to_totals(
        totals: Dict[str, Any],
        *,
        success: bool,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        latency_ms: int,
        ttft_ms: Optional[int],
        now_ms: int,
        status_code: int,
        error: str,
        model: str,
        channel: str,
        cred_label: str,
        proxy_label: str,
    ) -> None:
        totals["total_requests"] = int(totals.get("total_requests") or 0) + 1
        if success:
            totals["success_calls"] = int(totals.get("success_calls") or 0) + 1
        else:
            totals["failure_calls"] = int(totals.get("failure_calls") or 0) + 1
        totals["prompt_tokens"] = int(totals.get("prompt_tokens") or 0) + prompt_tokens
        totals["completion_tokens"] = int(totals.get("completion_tokens") or 0) + completion_tokens
        totals["total_tokens"] = int(totals.get("total_tokens") or 0) + total_tokens
        if latency_ms >= 0:
            totals["total_latency_ms"] = int(totals.get("total_latency_ms") or 0) + latency_ms
            totals["latency_samples"] = int(totals.get("latency_samples") or 0) + 1
        if ttft_ms is not None and ttft_ms >= 0:
            totals["total_ttft_ms"] = int(totals.get("total_ttft_ms") or 0) + ttft_ms
            totals["ttft_samples"] = int(totals.get("ttft_samples") or 0) + 1
        totals["last_seen_ms"] = now_ms
        totals["last_status"] = int(status_code)
        totals["last_error"] = "" if success else (error or "")[:240]
        totals["last_model"] = model
        if channel:
            totals["channel"] = channel
        if cred_label:
            totals["cred_label"] = cred_label
        if proxy_label:
            totals["proxy_label"] = proxy_label

    def record_attempt(
        self,
        *,
        channel: str,
        cred_id: str,
        cred_label: str,
        proxy_label: str = "",
        request_model: str = "",
        upstream_model: str = "",
        mode: str = "stream",
        status_code: int = 200,
        success: bool = True,
        latency_ms: int = 0,
        ttft_ms: Optional[int] = None,
        first_packet_ms: Optional[int] = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error: str = "",
        cancelled: bool = False,
        ts_ms: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Persist a completed credential attempt. Cancelled race losers (499) are ignored."""
        if cancelled or int(status_code) == 499:
            return None
        if not cred_id:
            cred_id = f"{channel}_default"

        now_ms = int(ts_ms if ts_ms is not None else time.time() * 1000)
        p_toks = max(0, int(prompt_tokens or 0))
        c_toks = max(0, int(completion_tokens or 0))
        t_toks = p_toks + c_toks
        clamped_latency = max(0, int(latency_ms or 0))
        norm_ttft = None if ttft_ms is None else min(clamped_latency, max(0, int(ttft_ms)))
        norm_fp = None if first_packet_ms is None else min(clamped_latency, max(0, int(first_packet_ms)))
        req_model = str(request_model or upstream_model or "").strip()
        up_model = str(upstream_model or req_model).strip()
        clean_error = "" if success else str(error or "").strip()[:240]

        event = {
            "ts_ms": now_ms,
            "ts_iso": datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channel": channel,
            "cred_id": cred_id,
            "cred_label": cred_label,
            "proxy_label": proxy_label,
            "request_model": req_model,
            "upstream_model": up_model,
            "mode": mode,
            "status_code": int(status_code),
            "success": bool(success),
            "latency_ms": clamped_latency,
            "ttft_ms": norm_ttft,
            "first_packet_ms": norm_fp,
            "prompt_tokens": p_toks,
            "completion_tokens": c_toks,
            "total_tokens": t_toks,
            "error": clean_error,
        }

        with self._lock:
            try:
                state_dir = self._state_dir()
                os.makedirs(state_dir, exist_ok=True)
                ev_path = self._events_path()
                with open(ev_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
                try:
                    os.chmod(ev_path, 0o600)
                except OSError:
                    pass

                data = self._load_totals_locked()
                cred_totals = data["credentials"].get(cred_id)
                if not isinstance(cred_totals, dict):
                    cred_totals = _empty_totals()
                self._apply_to_totals(
                    cred_totals,
                    success=bool(success),
                    prompt_tokens=p_toks,
                    completion_tokens=c_toks,
                    total_tokens=t_toks,
                    latency_ms=clamped_latency,
                    ttft_ms=norm_ttft,
                    now_ms=now_ms,
                    status_code=int(status_code),
                    error=clean_error,
                    model=up_model,
                    channel=channel,
                    cred_label=cred_label,
                    proxy_label=proxy_label,
                )
                data["credentials"][cred_id] = cred_totals

                global_totals = data["global"]
                self._apply_to_totals(
                    global_totals,
                    success=bool(success),
                    prompt_tokens=p_toks,
                    completion_tokens=c_toks,
                    total_tokens=t_toks,
                    latency_ms=clamped_latency,
                    ttft_ms=norm_ttft,
                    now_ms=now_ms,
                    status_code=int(status_code),
                    error=clean_error,
                    model=up_model,
                    channel=channel,
                    cred_label=cred_label,
                    proxy_label=proxy_label,
                )
                data["global"] = global_totals
                _atomic_write_json(self._totals_path(), data)
                self._maybe_trim_events_locked()
            except Exception:
                pass
        return event

    @staticmethod
    def build_recent_status(events: List[Dict[str, Any]], now_ms: Optional[int] = None) -> Dict[str, Any]:
        """20 10-minute status blocks covering the last 200 minutes."""
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        window_start = now_ms - RECENT_BLOCK_COUNT * RECENT_BLOCK_DURATION_MS
        buckets: List[Dict[str, Any]] = []
        for idx in range(RECENT_BLOCK_COUNT):
            b_start = window_start + idx * RECENT_BLOCK_DURATION_MS
            b_end = b_start + RECENT_BLOCK_DURATION_MS
            buckets.append({
                "start_ms": b_start,
                "end_ms": b_end,
                "success": 0,
                "failure": 0,
                "rate": -1.0,
                "state": "idle",
            })

        total_success = 0
        total_failure = 0
        for ev in events:
            ts_ms = int(ev.get("ts_ms") or 0)
            if ts_ms < window_start or ts_ms > now_ms:
                continue
            idx = min(RECENT_BLOCK_COUNT - 1, max(0, (ts_ms - window_start) // RECENT_BLOCK_DURATION_MS))
            if ev.get("success"):
                buckets[idx]["success"] += 1
                total_success += 1
            else:
                buckets[idx]["failure"] += 1
                total_failure += 1

        for b in buckets:
            tot = b["success"] + b["failure"]
            if tot > 0:
                b["rate"] = round(b["success"] / tot, 4)
                if b["failure"] == 0:
                    b["state"] = "success"
                elif b["success"] == 0:
                    b["state"] = "failure"
                else:
                    b["state"] = "mixed"

        total = total_success + total_failure
        success_rate = round((total_success / total) * 100.0, 1) if total > 0 else 100.0
        return {
            "blocks": buckets,
            "total_success": total_success,
            "total_failure": total_failure,
            "success_rate": success_rate,
            "has_data": total > 0,
        }

    def get_credential_summary(self, cred_id: str, recent_limit: int = 50) -> Dict[str, Any]:
        with self._lock:
            data = self._load_totals_locked()
            events = self._load_events_locked(cred_id=cred_id)
        totals = _enrich_totals(data["credentials"].get(cred_id) or {})
        recent_status = self.build_recent_status(events)
        recent_events = list(reversed(events[-recent_limit:])) if recent_limit > 0 else []
        return {
            "cred_id": cred_id,
            "history": totals,
            "recent_status": recent_status,
            "recent_events": recent_events,
        }

    def get_overview_summary(self, recent_limit: int = 40) -> Dict[str, Any]:
        with self._lock:
            data = self._load_totals_locked()
            events = self._load_events_locked()

        events_by_cred: Dict[str, List[Dict[str, Any]]] = {}
        for ev in events:
            cid = str(ev.get("cred_id") or "")
            if cid:
                events_by_cred.setdefault(cid, []).append(ev)

        now_ms = int(time.time() * 1000)
        cred_summaries: Dict[str, Dict[str, Any]] = {}
        all_ids = set(data["credentials"].keys()) | set(events_by_cred.keys())
        for cid in all_ids:
            ev_list = events_by_cred.get(cid, [])
            cred_summaries[cid] = {
                "cred_id": cid,
                "history": _enrich_totals(data["credentials"].get(cid) or {}),
                "recent_status": self.build_recent_status(ev_list, now_ms=now_ms),
            }

        return {
            "global": _enrich_totals(data["global"]),
            "recent_status": self.build_recent_status(events, now_ms=now_ms),
            "credentials": cred_summaries,
            "recent_events": list(reversed(events[-recent_limit:])) if recent_limit > 0 else [],
        }

    def reset_credential(self, cred_id: str) -> None:
        with self._lock:
            data = self._load_totals_locked()
            data["credentials"].pop(cred_id, None)
            _atomic_write_json(self._totals_path(), data)
            events = self._load_events_locked()
            kept = [ev for ev in events if ev.get("cred_id") != cred_id]
            ev_path = self._events_path()
            if kept:
                target_dir = os.path.dirname(ev_path) or "."
                fd, tmp_path = tempfile.mkstemp(prefix=".usage-ev-", suffix=".tmp", dir=target_dir)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        for ev in kept:
                            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                    os.replace(tmp_path, ev_path)
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
            elif os.path.exists(ev_path):
                try:
                    os.remove(ev_path)
                except OSError:
                    pass

    def reset_all(self) -> None:
        with self._lock:
            for p in (self._totals_path(), self._events_path()):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass


usage_ledger = CredentialUsageLedger()
