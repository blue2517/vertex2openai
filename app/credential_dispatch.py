"""Cross-credential auto-retry (Mode B refill) and concurrent racing dispatcher.

Both Express API Key and Cookie direct upstreams share this scheduler:
- Each attempt runs in its own asyncio.Task with its own AttemptRequest and
  @credential_proxy_scope ContextVar, keeping every credential strictly isolated
  to its own bound proxy.
- When concurrent racing is enabled, up to k distinct credentials start within
  [0, t] seconds (slot 0 immediately, remaining slots randomly jittered in [0, t]).
  The first attempt to win (first_output or safe first_packet) keeps streaming
  while all other attempts are cancelled and their underlying HTTP/SDK clients
  are closed immediately.
- When cross-credential failover is enabled, any slot that terminates with a
  failover-eligible failure before producing client output immediately refills
  with the next credential according to the existing round-robin / random rule,
  up to credential_failover_max refills (Mode B).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import anyio
from fastapi.responses import JSONResponse, StreamingResponse

import config as app_config
import model_capabilities as mc
from api_errors import UpstreamError, error_response, prepare_stream_response
from api_helpers import get_retry_settings
from cookie_credentials import cookie_credential_manager
from express_key_manager import ExpressKeyManager
from logger import stats
from models import OpenAIRequest
from request_lifecycle import cancel_and_wait
from runtime_state import app_state
from schema_validation import SchemaValidationError, validate_request_schemas
from upstreams.express_sdk import _normalize_model_name

FAILOVER_ELIGIBLE_STATUSES = frozenset({401, 403, 408, 429, 500, 502, 503, 504})
CREDENTIAL_BAD_STATUSES = frozenset({401, 403})


class AttemptRequest:
    """Per-attempt request proxy isolating cancellation, client resources and first-packet signal."""

    def __init__(
        self,
        base_request: Any,
        *,
        attempt_id: str,
        on_first_packet: Optional[Callable[[], None]] = None,
        suppress_error_stats: bool = True,
    ):
        self._base = base_request
        self.attempt_id = attempt_id
        self.cancelled = asyncio.Event()
        self.resources: list[Any] = []
        self._on_first_packet = on_first_packet
        self._first_packet_notified = False
        self.suppress_attempt_error_stats = suppress_error_stats

    async def is_disconnected(self) -> bool:
        if self.cancelled.is_set():
            return True
        checker = getattr(self._base, "is_disconnected", None)
        if checker is None:
            return False
        try:
            return bool(await checker())
        except Exception:
            return False

    def register_upstream_client(self, client: Any) -> Any:
        if all(client is not other for other in self.resources):
            self.resources.append(client)
        return client

    def notify_first_packet(self) -> None:
        if self._first_packet_notified or self.cancelled.is_set():
            return
        self._first_packet_notified = True
        if self._on_first_packet is not None:
            self._on_first_packet()

    def transfer_resources_to_parent(self) -> None:
        parent_register = getattr(self._base, "register_upstream_client", None)
        if parent_register is not None:
            for client in self.resources:
                parent_register(client)
        self.resources.clear()

    async def close_resources(self) -> None:
        resources, self.resources = list(self.resources), []
        with anyio.CancelScope(shield=True):
            for client in reversed(resources):
                for target, method in (
                    (getattr(client, "aio", None), "aclose"),
                    (client, "aclose"),
                    (client, "close"),
                ):
                    close = getattr(target, method, None)
                    if close is not None:
                        with suppress(Exception):
                            result = close()
                            if inspect.isawaitable(result):
                                await result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


@dataclass(eq=False)
class _Slot:
    slot_no: int
    credential: Optional[dict]
    cred_id: Optional[str]
    cred_label: str
    attempt_req: AttemptRequest
    first_packet_event: asyncio.Event = field(default_factory=asyncio.Event)
    packet_waiter: Optional[asyncio.Task] = None
    cleaned: bool = False

    async def cleanup(self) -> None:
        if self.cleaned:
            return
        self.cleaned = True
        self.attempt_req.cancelled.set()
        await self.attempt_req.close_resources()


def _response_payload(response: Any) -> dict:
    body = getattr(response, "body", b"") or b""
    try:
        parsed = json.loads(body) if body else {}
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _is_failover_eligible(response: Any) -> bool:
    if response is None:
        return True
    status = getattr(response, "status_code", 500)
    return status in FAILOVER_ELIGIBLE_STATUSES


def _is_credential_bad(response: Any) -> bool:
    if response is None:
        return False
    status = getattr(response, "status_code", 500)
    if status in CREDENTIAL_BAD_STATUSES:
        return True
    err = _response_payload(response).get("error")
    if isinstance(err, dict):
        if err.get("type") == "configuration_error" or err.get("code") == "configuration_error":
            return True
    return False


async def _parent_disconnected(request: Any) -> bool:
    checker = getattr(request, "is_disconnected", None)
    if checker is None:
        return False
    try:
        return bool(await checker())
    except Exception:
        return False


def _is_true_stream(request_obj: OpenAIRequest, is_cookie_channel: bool) -> bool:
    if not request_obj.stream:
        return False
    base_model = request_obj.model or ""
    if not is_cookie_channel:
        base_model, _, _ = _normalize_model_name(base_model)
    elif base_model.endswith("-search"):
        base_model = base_model[:-len("-search")]
    if mc.get_profile(base_model)["is_image"]:
        return False
    if not is_cookie_channel and app_state.get_setting("fake_streaming", app_config.FAKE_STREAMING_ENABLED):
        return False
    return True


def _get_express_manager(fastapi_request: Any) -> ExpressKeyManager:
    app = getattr(fastapi_request, "app", None)
    state = getattr(app, "state", None)
    manager = getattr(state, "express_key_manager", None)
    if manager is not None:
        return manager
    return ExpressKeyManager()


def _credential_id(record: Optional[dict], is_cookie_channel: bool) -> Optional[str]:
    if not record:
        return None
    return record.get("id") if is_cookie_channel else record.get("key")


def _credential_label(record: Optional[dict], is_cookie_channel: bool) -> str:
    if not record:
        return "默认凭证"
    if is_cookie_channel:
        return record.get("name") or str(record.get("id", ""))[:8] or "Cookie 凭证"
    note = (record.get("note") or "").strip()
    if note:
        return note
    idx = record.get("index")
    return f"Key #{idx + 1}" if isinstance(idx, int) else "Express Key"


async def _close_response_stream(response: Any) -> None:
    if isinstance(response, StreamingResponse):
        close_iter = getattr(response.body_iterator, "aclose", None)
        if close_iter is not None:
            with anyio.CancelScope(shield=True):
                with suppress(Exception):
                    await close_iter()


async def _cancel_slot(task: asyncio.Task, slot: _Slot) -> None:
    slot.attempt_req.cancelled.set()
    if slot.packet_waiter is not None:
        await cancel_and_wait(slot.packet_waiter)
        slot.packet_waiter = None
    if not task.done():
        await cancel_and_wait(task)
    else:
        with suppress(Exception):
            await _close_response_stream(task.result())
    await slot.cleanup()


async def _cancel_other_slots(
    active_slots: dict[asyncio.Task, _Slot],
    packet_waiters: dict[asyncio.Task, _Slot],
    *,
    exclude_slot: Optional[_Slot] = None,
) -> int:
    # Cancel all pending packet waiters first.
    waiters = list(packet_waiters.keys())
    packet_waiters.clear()
    for wt in waiters:
        with suppress(Exception):
            if not wt.done():
                wt.cancel()
    if waiters:
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*waiters, return_exceptions=True)

    to_cancel: list[tuple[asyncio.Task, _Slot]] = []
    for task, slot in list(active_slots.items()):
        slot.packet_waiter = None
        if exclude_slot is not None and slot is exclude_slot:
            continue
        active_slots.pop(task, None)
        to_cancel.append((task, slot))

    if to_cancel:
        with anyio.CancelScope(shield=True):
            await asyncio.gather(*(_cancel_slot(task, slot) for task, slot in to_cancel), return_exceptions=True)
    return len(to_cancel)


async def dispatch_upstream(
    upstream: Any,
    request_obj: OpenAIRequest,
    fastapi_request: Any,
    *,
    is_cookie_channel: bool,
) -> Any:
    """Dispatch a request using configured cross-credential failover and/or racing."""
    settings = app_state.get_dispatch_settings()
    failover_enabled = settings["credential_failover_enabled"]
    failover_max = settings["credential_failover_max"] if failover_enabled else 0
    race_enabled = settings["credential_race_enabled"]
    race_concurrency = settings["credential_race_concurrency"]
    race_delay = settings["credential_race_delay_seconds"]
    race_win_mode = settings["credential_race_win_mode"]
    race_stream_only = settings["credential_race_stream_only"]

    use_race = race_enabled and (not race_stream_only or _is_true_stream(request_obj, is_cookie_channel))

    # Fast path: both features disabled -> exact original single-credential path.
    if not failover_enabled and not use_race:
        return await upstream.chat_completions(request_obj, fastapi_request)

    # Validate request-level errors once before spawning retries or racers.
    try:
        validate_request_schemas(request_obj)
    except SchemaValidationError as exc:
        return error_response(UpstreamError(400, str(exc), "invalid_request_error"))

    if not is_cookie_channel:
        _, _, model_error = _normalize_model_name(request_obj.model)
        if model_error:
            print(f"❌ [模型名称] {model_error} 收到的模型名：{request_obj.model}")
            return error_response(UpstreamError(400, model_error, "invalid_request_error"))

    channel_name = "Studio" if is_cookie_channel else "Express"
    express_manager = None if is_cookie_channel else _get_express_manager(fastapi_request)

    if is_cookie_channel:
        app_state.import_cookie_credentials_once(app_config.GOOGLE_COOKIE, app_config.GOOGLE_PROJECT_ID)
        enabled_records = cookie_credential_manager.get_enabled_records()
    else:
        enabled_records = express_manager.get_records()

    available_count = len(enabled_records)
    if available_count == 0:
        # Let the upstream emit its standard 401/400/legacy fallback behavior.
        return await upstream.chat_completions(request_obj, fastapi_request)

    initial_slots = min(race_concurrency, available_count) if (use_race and available_count >= 2) else 1
    refills_remaining = failover_max
    total_refill_budget = failover_max

    # If neither racing (>1) nor failover (>0) actually applies (e.g., race enabled with only 1 credential
    # and failover disabled), run single attempt directly.
    if initial_slots <= 1 and refills_remaining <= 0:
        return await upstream.chat_completions(request_obj, fastapi_request)

    _, backoff_sec = get_retry_settings()

    active_slots: dict[asyncio.Task, _Slot] = {}
    packet_waiters: dict[asyncio.Task, _Slot] = {}
    in_flight_ids: set[str] = set()
    tried_ids: set[str] = set()
    bad_ids: set[str] = set()
    last_failed_at: dict[str, float] = {}
    claimed_slot: Optional[_Slot] = None
    winner_slot: Optional[_Slot] = None
    winner_response: Any = None
    last_error_response: Any = None
    slot_counter = 0
    total_attempts = 0

    def _draw_record(exclude: set[str]) -> Optional[dict]:
        if is_cookie_channel:
            return cookie_credential_manager.get_record(exclude_ids=exclude)
        return express_manager.get_express_record(exclude_keys=exclude)

    def _select_next_credential(*, is_initial: bool) -> Optional[dict]:
        if is_initial:
            return _draw_record(in_flight_ids | bad_ids)
        # Mode B refill: prefer untried credentials first (following round-robin / random rule),
        # then wrap around to previously tried transient-error credentials not currently in-flight.
        rec = _draw_record(in_flight_ids | tried_ids | bad_ids)
        if rec is not None:
            return rec
        return _draw_record(in_flight_ids | bad_ids)

    def _refill_delay(cred_id: Optional[str]) -> float:
        if cred_id is None or cred_id not in last_failed_at:
            return 0.0
        elapsed = time.monotonic() - last_failed_at[cred_id]
        if elapsed < 1.0 and backoff_sec > 0:
            return min(backoff_sec, 1.0)
        return 0.0

    def _spawn_slot(credential: dict, *, delay: float, reason: str) -> _Slot:
        nonlocal slot_counter, total_attempts
        slot_counter += 1
        total_attempts += 1
        cred_id = _credential_id(credential, is_cookie_channel)
        cred_label = _credential_label(credential, is_cookie_channel)
        if cred_id is not None:
            in_flight_ids.add(cred_id)
            tried_ids.add(cred_id)

        first_packet_event = asyncio.Event()
        attempt_req = AttemptRequest(
            fastapi_request,
            attempt_id=f"{slot_counter}-{uuid.uuid4().hex[:6]}",
            on_first_packet=first_packet_event.set,
            suppress_error_stats=True,
        )
        slot = _Slot(
            slot_no=slot_counter,
            credential=credential,
            cred_id=cred_id,
            cred_label=cred_label,
            attempt_req=attempt_req,
            first_packet_event=first_packet_event,
        )

        if initial_slots > 1:
            delay_hint = f"延迟 {delay:.2f}s" if delay > 0 else "立即发起"
            print(f"🚀 [凭证调度] {channel_name} | 槽位 #{slot.slot_no} 凭证={cred_label}（{delay_hint}）")

        async def _worker() -> Any:
            if delay > 0:
                await asyncio.sleep(delay)
            if await attempt_req.is_disconnected():
                return error_response(UpstreamError(499, "请求已取消。", "request_cancelled"))
            raw_response = await upstream.chat_completions(
                request_obj, attempt_req, credential=credential
            )
            return await prepare_stream_response(raw_response)

        task = asyncio.create_task(_worker(), name=f"dispatch:{channel_name}:{slot.slot_no}")
        active_slots[task] = slot
        return slot

    if initial_slots > 1:
        win_label = "安全首包" if race_win_mode == "first_packet" else "首字"
        print(
            f"🏁 [凭证竞速] {channel_name} | 模型={request_obj.model} | 启动 {initial_slots} 路并发"
            f"（窗口 0–{race_delay:g}s，胜出={win_label}，补位上限={total_refill_budget}）"
        )

    try:
        # Launch initial slot(s): slot 0 starts immediately; remaining slots jitter randomly in [0, t].
        for i in range(initial_slots):
            cred = _select_next_credential(is_initial=True)
            if cred is None:
                break
            delay = 0.0 if (i == 0 or race_delay <= 0) else random.uniform(0.0, race_delay)
            _spawn_slot(cred, delay=delay, reason="initial")

        while active_slots:
            if await _parent_disconnected(fastapi_request):
                return error_response(UpstreamError(499, "调用方已断开连接，请求已取消。", "request_cancelled"))

            wait_set: set[asyncio.Task] = set(active_slots.keys())
            if race_win_mode == "first_packet" and claimed_slot is None and len(active_slots) > 1:
                for task, slot in active_slots.items():
                    if slot.packet_waiter is None:
                        slot.packet_waiter = asyncio.create_task(slot.first_packet_event.wait())
                        packet_waiters[slot.packet_waiter] = slot
                    wait_set.add(slot.packet_waiter)

            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

            completed_tasks = [t for t in done if t in active_slots]
            triggered_slots: list[_Slot] = []
            for t in done:
                if t in packet_waiters:
                    slot = packet_waiters.pop(t)
                    if slot.packet_waiter is t:
                        slot.packet_waiter = None
                    if not t.cancelled():
                        with suppress(Exception):
                            t.result()
                            triggered_slots.append(slot)

            # 1. Process completed worker tasks first (a task finishing with status < 400 wins immediately).
            for task in completed_tasks:
                slot = active_slots.pop(task, None)
                if slot is None:
                    continue
                if slot.packet_waiter is not None:
                    packet_waiters.pop(slot.packet_waiter, None)
                    await cancel_and_wait(slot.packet_waiter)
                    slot.packet_waiter = None
                if slot.cred_id is not None:
                    in_flight_ids.discard(slot.cred_id)

                try:
                    resp = task.result()
                except asyncio.CancelledError:
                    await slot.cleanup()
                    continue
                except Exception as exc:
                    resp = error_response(exc)

                if resp is not None and getattr(resp, "status_code", 500) < 400:
                    winner_slot = slot
                    winner_response = resp
                    slot.attempt_req.suppress_attempt_error_stats = False
                    slot.attempt_req.transfer_resources_to_parent()
                    cancelled_others = await _cancel_other_slots(
                        active_slots, packet_waiters, exclude_slot=slot
                    )
                    if cancelled_others > 0:
                        print(
                            f"🏆 [竞速胜出] {channel_name} | 槽位 #{slot.slot_no} 凭证={slot.cred_label}"
                            f" 率先输出首字，已终止其余 {cancelled_others} 路连接"
                        )
                    break

                # Attempt failed before valid output.
                await slot.cleanup()
                if slot is claimed_slot:
                    claimed_slot = None
                if resp is not None and getattr(resp, "status_code", 500) != 499:
                    last_error_response = resp
                elif last_error_response is None:
                    last_error_response = resp

                if slot.cred_id is not None:
                    last_failed_at[slot.cred_id] = time.monotonic()
                    if _is_credential_bad(resp):
                        bad_ids.add(slot.cred_id)

                status_code = getattr(resp, "status_code", 500) if resp is not None else 500

                # Mode B: refill the freed slot immediately when failover is enabled and eligible.
                if (
                    claimed_slot is None
                    and refills_remaining > 0
                    and _is_failover_eligible(resp)
                    and not await _parent_disconnected(fastapi_request)
                ):
                    next_cred = _select_next_credential(is_initial=False)
                    if next_cred is not None:
                        refills_remaining -= 1
                        stats.add_retry()
                        used = total_refill_budget - refills_remaining
                        next_label = _credential_label(next_cred, is_cookie_channel)
                        next_id = _credential_id(next_cred, is_cookie_channel)
                        delay = _refill_delay(next_id)
                        if initial_slots > 1:
                            print(
                                f"🔄 [跨凭证重试] {channel_name} | 槽位 #{slot.slot_no} 凭证={slot.cred_label}"
                                f" 终止（HTTP {status_code}），立即补位凭证={next_label}"
                                f"（第 {used}/{total_refill_budget} 次补位）"
                            )
                        else:
                            print(
                                f"🔄 [跨凭证重试] {channel_name} | 凭证={slot.cred_label}"
                                f" 终止（HTTP {status_code}），切换至凭证={next_label}"
                                f"（第 {used}/{total_refill_budget} 次跨凭证重试）"
                            )
                        _spawn_slot(next_cred, delay=delay, reason="failover")

            if winner_response is not None:
                return winner_response

            # 2. In first_packet mode, the first active slot with a safe first packet claims the win
            #    and cancels all other concurrent slots immediately.
            if race_win_mode == "first_packet" and claimed_slot is None and triggered_slots:
                active_set = set(active_slots.values())
                for candidate in triggered_slots:
                    if candidate in active_set:
                        claimed_slot = candidate
                        cancelled_others = await _cancel_other_slots(
                            active_slots, packet_waiters, exclude_slot=claimed_slot
                        )
                        if cancelled_others > 0:
                            print(
                                f"📦 [竞速胜出] {channel_name} | 槽位 #{claimed_slot.slot_no}"
                                f" 凭证={claimed_slot.cred_label} 率先收到安全首包，已终止其余 {cancelled_others} 路连接"
                            )
                        break

        if last_error_response is not None:
            status_code = getattr(last_error_response, "status_code", 502)
            print(
                f"❌ [凭证调度] {channel_name} | 所有尝试均失败（共尝试 {total_attempts} 次），返回 HTTP {status_code}"
            )
            return last_error_response

        return error_response(UpstreamError(502, "所有凭证尝试均未返回有效响应。"))
    finally:
        if winner_slot is None:
            with anyio.CancelScope(shield=True):
                await _cancel_other_slots(active_slots, packet_waiters, exclude_slot=None)
