import json
import time
import math
import uuid
import asyncio
import httpx
import re
from typing import List, Dict, Any, Callable, Optional

from fastapi.responses import JSONResponse, StreamingResponse
from google.genai import types

from models import OpenAIRequest, OpenAIMessage
from message_processing import (
    convert_to_openai_format,
    extract_reasoning_by_tags,
    _create_safety_ratings_html,
)
import config as app_config
from config import VERTEX_REASONING_TAG

import model_capabilities as mc
from runtime_state import app_state

# 引入报错重试统计器
from logger import stats
from request_lifecycle import cancel_and_wait
from api_errors import (
    TIMEOUT_EXCEPTIONS,
    UpstreamError,
    create_openai_error_response,
    extract_upstream_error,
    error_sse,
)

try:
    import httpx2
except ImportError:  # pragma: no cover - optional in environments without httpx2
    httpx2 = None


def _safety_score_enabled() -> bool:
    try:
        return bool(app_state.get_setting("safety_score", app_config.SAFETY_SCORE))
    except Exception:
        return bool(app_config.SAFETY_SCORE)



def get_retry_settings() -> tuple[int, float]:
    """读取重试配置。

    **语义（全项目统一）**：`retry_max` 是「失败后的重试次数」，
    总请求次数 = retry_max + 1。所以循环一律写 `range(retry_max + 1)`。
    旧实现在 Express 通道写成 `range(retry_max)`，retry_max=0 时一次请求都不发（P0-2）。
    """
    try:
        retry_max = int(app_state.get_setting("retry_max", app_config.DEFAULT_SETTINGS["retry_max"]))
    except (TypeError, ValueError):
        retry_max = app_config.DEFAULT_SETTINGS["retry_max"]
    try:
        backoff = float(app_state.get_setting(
            "retry_backoff_seconds", app_config.DEFAULT_SETTINGS["retry_backoff_seconds"]))
    except (TypeError, ValueError):
        backoff = float(app_config.DEFAULT_SETTINGS["retry_backoff_seconds"])
    return max(0, min(50, retry_max)), max(0.0, min(120.0, backoff))


class UpstreamTrace:
    """Per-generation diagnostics, without headers, prompt text or session values."""

    def __init__(
        self,
        channel: str,
        model: str,
        response_id: str,
        started: float,
        unit_label: str = "JSON",
        monotonic: Optional[Callable[[], float]] = None,
    ):
        self.channel = channel
        self.label = f"{model} | 请求={response_id.rsplit('-', 1)[-1]}"
        self.started = started
        self.unit_label = unit_label
        self._monotonic = monotonic or time.monotonic
        self.attempts = 0
        self.has_headers = False
        self.received_bytes = 0
        self.objects = 0
        self.kinds: set[str] = set()
        self.first_packet: Optional[float] = None
        self.first_output: Optional[float] = None

    def begin_attempt(self):
        self.attempts += 1
        self.has_headers = False
        self.received_bytes = 0
        self.objects = 0
        self.kinds.clear()

    def received_headers(self):
        self.has_headers = True

    def received_packet(self):
        if self.first_packet is None:
            self.first_packet = self._monotonic() - self.started
            print(f"📦 [{self.channel}] {self.label} | 首包耗时 {self.first_packet:.2f}s")

    def observe_event(self, kind: str):
        labels = {"text": "正文", "thought": "思考", "image": "图片", "function_call": "工具调用"}
        if kind not in labels:
            return
        self.kinds.add(kind)
        elapsed = self._monotonic() - self.started
        if self.first_output is None:
            self.first_output = elapsed
            print(f"⚡ [{self.channel}] {self.label} | 首字耗时 {elapsed:.2f}s（{labels[kind]}）")

    def summary(self) -> str:
        packet = "未到达" if self.first_packet is None else f"{self.first_packet:.2f}s"
        first = "未到达" if self.first_output is None else f"{self.first_output:.2f}s"
        return (f"上游请求={self.attempts} | 响应头={'已收' if self.has_headers else '未收'}"
                f" | 本次接收={self.received_bytes}B/{self.objects}{self.unit_label}"
                f" | 思考={'有' if 'thought' in self.kinds else '无'}"
                f" | 正文={'有' if 'text' in self.kinds else '无'}"
                f" | 工具={'有' if 'function_call' in self.kinds else '无'}"
                f" | 首包={packet} | 首字={first}"
                f" | 总耗时 {self._monotonic() - self.started:.2f}s")

    def transport_failure(self, error: Exception):
        stage = "读取响应体" if self.has_headers else "等待响应头"
        print(f"⚠️ [{self.channel}] {self.label} | 异常={type(error).__name__}"
              f" | 阶段={stage} | {self.summary()}")


class _FirstPacketStreamMixin:
    """Observe the first non-empty body read before decompression/UTF-8 decoding."""

    def __init__(self, stream: Any, trace: UpstreamTrace, count_bytes: bool = False):
        self.stream = stream
        self.trace = trace
        self.count_bytes = count_bytes

    async def __aiter__(self):
        async for chunk in self.stream:
            if chunk:
                self.trace.received_packet()
                if self.count_bytes:
                    self.trace.received_bytes += len(chunk)
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class FirstPacketByteStream(_FirstPacketStreamMixin, httpx.AsyncByteStream):
    pass


if httpx2 is not None:
    class _Httpx2FirstPacketByteStream(_FirstPacketStreamMixin, httpx2.AsyncByteStream):
        pass
else:  # pragma: no cover
    _Httpx2FirstPacketByteStream = FirstPacketByteStream


def _wrap_first_packet_stream(stream: Any, trace: UpstreamTrace, count_bytes: bool = False) -> Any:
    if httpx2 is not None and isinstance(stream, httpx2.AsyncByteStream):
        return _Httpx2FirstPacketByteStream(stream, trace, count_bytes=count_bytes)
    return FirstPacketByteStream(stream, trace, count_bytes=count_bytes)


class _ObservingTransportMixin:
    """Wrap an httpx/httpx2 async transport to enforce timeouts and record first-packet timing."""

    def __init__(self, inner: Any, trace: UpstreamTrace, timeout: Optional[httpx.Timeout] = None):
        self.inner = inner
        self.trace = trace
        self.timeout = timeout

    async def handle_async_request(self, request: Any) -> Any:
        if self.timeout is not None:
            existing = request.extensions.get("timeout")
            if not existing or all(v is None for v in existing.values()):
                request.extensions["timeout"] = self.timeout.as_dict()
        response = await self.inner.handle_async_request(request)
        self.trace.received_headers()
        if response.status_code == 200:
            if response.is_stream_consumed:
                if response.content:
                    self.trace.received_packet()
                    self.trace.received_bytes += len(response.content)
            elif response.stream is not None:
                response.stream = _wrap_first_packet_stream(response.stream, self.trace, count_bytes=True)
        return response

    async def aclose(self):
        await self.inner.aclose()


class _ObservingAsyncTransport(_ObservingTransportMixin, httpx.AsyncBaseTransport):
    pass


if httpx2 is not None:
    class _Httpx2ObservingAsyncTransport(_ObservingTransportMixin, httpx2.AsyncBaseTransport):
        pass
else:  # pragma: no cover
    _Httpx2ObservingAsyncTransport = _ObservingAsyncTransport


def _wrap_observing_transport(inner: Any, trace: UpstreamTrace, timeout: Optional[httpx.Timeout] = None) -> Any:
    if isinstance(inner, _ObservingTransportMixin):
        inner.trace = trace
        if timeout is not None:
            inner.timeout = timeout
        return inner
    if httpx2 is not None and isinstance(inner, httpx2.AsyncBaseTransport):
        return _Httpx2ObservingAsyncTransport(inner, trace, timeout=timeout)
    return _ObservingAsyncTransport(inner, trace, timeout=timeout)


def _wrap_httpx_instance(httpx_client: Any, trace: UpstreamTrace, timeout: Optional[httpx.Timeout]) -> None:
    transport = getattr(httpx_client, "_transport", None)
    if transport is not None:
        httpx_client._transport = _wrap_observing_transport(transport, trace, timeout=timeout)
    mounts = getattr(httpx_client, "_mounts", None)
    if isinstance(mounts, dict):
        for pattern, mounted in list(mounts.items()):
            if mounted is not None:
                mounts[pattern] = _wrap_observing_transport(mounted, trace, timeout=timeout)


def attach_express_trace(
    client: Any,
    trace: Optional[UpstreamTrace],
    read_timeout_seconds: Optional[float] = None,
) -> Any:
    """Attach HTTP header, first-packet observation, and timeouts to a google-genai client."""
    if client is None or trace is None:
        return client
    api_client = getattr(client, "_api_client", None)
    httpx_client = getattr(api_client, "_async_httpx_client", None)
    if httpx_client is None:
        return client
    timeout = (
        httpx.Timeout(120.0, connect=30.0, read=float(read_timeout_seconds))
        if read_timeout_seconds is not None
        else None
    )
    if getattr(httpx_client, "_per_loop", False) and hasattr(httpx_client, "_client_for_running_loop"):
        httpx_client._express_trace_config = (trace, timeout)
        if not getattr(httpx_client, "_express_loop_hooked", False):
            orig_for_loop = httpx_client._client_for_running_loop

            def _traced_client_for_running_loop():
                inner = orig_for_loop()
                cfg_trace, cfg_timeout = httpx_client._express_trace_config
                _wrap_httpx_instance(inner, cfg_trace, cfg_timeout)
                return inner

            httpx_client._client_for_running_loop = _traced_client_for_running_loop
            httpx_client._express_loop_hooked = True
        loop_clients = getattr(httpx_client, "_loop_clients", None)
        if isinstance(loop_clients, dict):
            for inner in list(loop_clients.values()):
                _wrap_httpx_instance(inner, trace, timeout)
    else:
        _wrap_httpx_instance(httpx_client, trace, timeout)
    return client


def _observe_gemini_response(trace: Optional[UpstreamTrace], resp: Any) -> None:
    """Record semantic output kinds (thought/text/image/function_call) on an Express trace."""
    if trace is None or resp is None:
        return
    candidates = getattr(resp, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        if parts:
            for part in parts:
                if getattr(part, "function_call", None) is not None:
                    trace.observe_event("function_call")
                elif getattr(part, "inline_data", None) is not None or getattr(part, "file_data", None) is not None:
                    trace.observe_event("image")
                else:
                    text = getattr(part, "text", None)
                    if isinstance(text, str) and text:
                        trace.observe_event("thought" if getattr(part, "thought", None) is True else "text")
        else:
            cand_text = getattr(candidate, "text", None)
            if isinstance(cand_text, str) and cand_text:
                trace.observe_event("text")
            elif content is not None:
                content_text = getattr(content, "text", None)
                if isinstance(content_text, str) and content_text:
                    trace.observe_event("text")
    if not candidates:
        resp_text = getattr(resp, "text", None)
        if isinstance(resp_text, str) and resp_text:
            trace.observe_event("text")

class StreamingReasoningProcessor:
    def __init__(self, tag_name: str = VERTEX_REASONING_TAG):
        self.tag_name = tag_name
        self.open_tag = f"<{tag_name}>"
        self.close_tag = f"</{tag_name}>"
        self.tag_buffer = ""
        self.inside_tag = False
        self._reasoning_chunks = []
        self.partial_tag_buffer = "" 

    def process_chunk(self, content: str) -> tuple[str, str]:
        if self.partial_tag_buffer:
            content = self.partial_tag_buffer + content
            self.partial_tag_buffer = ""
        self.tag_buffer += content
        
        processed_content_chunks = []
        current_reasoning_chunks = []
        
        while self.tag_buffer:
            if not self.inside_tag:
                open_pos = self.tag_buffer.find(self.open_tag)
                if open_pos == -1:
                    partial_match = False
                    for i in range(1, min(len(self.open_tag), len(self.tag_buffer) + 1)):
                        if self.tag_buffer[-i:] == self.open_tag[:i]:
                            partial_match = True
                            if len(self.tag_buffer) > i:
                                processed_content_chunks.append(self.tag_buffer[:-i])
                                self.partial_tag_buffer = self.tag_buffer[-i:]
                            else: 
                                self.partial_tag_buffer = self.tag_buffer
                            self.tag_buffer = ""
                            break
                    if not partial_match:
                        processed_content_chunks.append(self.tag_buffer)
                        self.tag_buffer = ""
                    break
                else:
                    processed_content_chunks.append(self.tag_buffer[:open_pos])
                    self.tag_buffer = self.tag_buffer[open_pos + len(self.open_tag):]
                    self.inside_tag = True
            else: 
                close_pos = self.tag_buffer.find(self.close_tag)
                if close_pos == -1:
                    partial_match = False
                    for i in range(1, min(len(self.close_tag), len(self.tag_buffer) + 1)):
                        if self.tag_buffer[-i:] == self.close_tag[:i]:
                            partial_match = True
                            if len(self.tag_buffer) > i:
                                new_reasoning = self.tag_buffer[:-i]
                                self._reasoning_chunks.append(new_reasoning)
                                if new_reasoning: current_reasoning_chunks.append(new_reasoning)
                                self.partial_tag_buffer = self.tag_buffer[-i:]
                            else: 
                                self.partial_tag_buffer = self.tag_buffer
                            self.tag_buffer = ""
                            break
                    if not partial_match:
                        if self.tag_buffer:
                            self._reasoning_chunks.append(self.tag_buffer)
                            current_reasoning_chunks.append(self.tag_buffer)
                            self.tag_buffer = ""
                    break
                else:
                    final_reasoning_chunk = self.tag_buffer[:close_pos]
                    self._reasoning_chunks.append(final_reasoning_chunk)
                    if final_reasoning_chunk: current_reasoning_chunks.append(final_reasoning_chunk)
                    
                    self.tag_buffer = self.tag_buffer[close_pos + len(self.close_tag):]
                    self.inside_tag = False
                    
        return "".join(processed_content_chunks), "".join(current_reasoning_chunks)
    
    def flush_remaining(self) -> tuple[str, str]:
        remaining_content_chunks = []
        if self.partial_tag_buffer:
            remaining_content_chunks.append(self.partial_tag_buffer)
            self.partial_tag_buffer = ""
            
        if not self.inside_tag:
            if self.tag_buffer: remaining_content_chunks.append(self.tag_buffer)
        else:
            if self.tag_buffer: self._reasoning_chunks.append(self.tag_buffer)
            self.inside_tag = False
            
        remaining_content = "".join(remaining_content_chunks)
        remaining_reasoning = "".join(self._reasoning_chunks)
        
        self.tag_buffer = ""
        self._reasoning_chunks.clear()
        
        return remaining_content, remaining_reasoning
    

def is_retryable_exception(e):
    if isinstance(e, TIMEOUT_EXCEPTIONS):
        return False
    if extract_upstream_error(e)[0] in (401, 403):
        return False
    error_str = str(e).lower()
    if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in [429, 503, 502]:
        return True
    if hasattr(e, "code") and e.code in [429, 503, 502]:
        return True
    if "429" in error_str or "503" in error_str or "too many requests" in error_str or "quota" in error_str:
        return True
    return False

def create_generation_config(request: OpenAIRequest) -> Dict[str, Any]:
    config: Dict[str, Any] = {}
    
    system_texts = []
    for msg in request.messages:
        if msg.role == "system" and msg.content:
            if isinstance(msg.content, str):
                system_texts.append(msg.content)
            elif isinstance(msg.content, list):
                for part in msg.content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        system_texts.append(part.get("text", ""))
                    elif hasattr(part, "text") and isinstance(part.text, str):
                        system_texts.append(part.text)
                        
    # P1-9：旧代码无条件剥离生图模型的 system_instruction，且没有注释理由。
    # 官方并未禁止生图模型使用系统指令，但为避免直接改变既有行为，
    # 改成控制台开关 image_system_instruction（默认关，保持旧行为）。
    if system_texts:
        # 生图判定统一走能力档案（"nano-banana" 家族名字里不带 image，裸 "image" in 会漏判）
        _is_image_name = mc.get_profile(request.model)["is_image"]
        _allow_sys = (not _is_image_name) or bool(
            app_state.get_setting("image_system_instruction", False))
        if _allow_sys:
            config["system_instruction"] = "\n".join(system_texts)
    
    if request.temperature is not None: config["temperature"] = request.temperature
    if request.max_tokens is not None: 
        config["max_output_tokens"] = request.max_tokens
    elif getattr(request, "max_completion_tokens", None) is not None:
        config["max_output_tokens"] = request.max_completion_tokens
        
    if request.top_p is not None: config["top_p"] = request.top_p
    if request.top_k is not None: config["top_k"] = request.top_k
    # P1-3：OpenAI 允许 stop 为字符串，统一规范成数组
    if request.stop is not None:
        config["stop_sequences"] = [request.stop] if isinstance(request.stop, str) else list(request.stop)
    if request.seed is not None: config["seed"] = request.seed
    if request.n is not None: config["candidate_count"] = request.n
    
    # 第三轮：惩罚参数默认剥离 —— 前端仍可传这两个字段，但反代不发给 Google
    # （新版 Gemini 收到非零 presence_penalty / frequency_penalty 直接 400）。
    # 这里不再下发；model_capabilities.sanitize_sampling 按档案做最终兜底剥离。
    for _pk in ("presence_penalty", "frequency_penalty"):
        _pv = getattr(request, _pk, None)
        if _pv:  # 只有非零值才值得提示（0 不会触发 400）
            if app_state.get_setting("debug_outbound", False):
                print(f"🔎 [出站调试] 已剥离 {_pk}={_pv}：新版 Gemini 不接受非零惩罚参数，该值不会发给 Google。")
        
    # P1-3：OpenAI 语义 logprobs=bool + top_logprobs=int；Gemini 语义 response_logprobs=bool + logprobs=int。
    _logprobs = getattr(request, "logprobs", None)
    _top_logprobs = getattr(request, "top_logprobs", None)
    if getattr(request, "response_logprobs", None) is not None:
        config["response_logprobs"] = request.response_logprobs
    if isinstance(_logprobs, bool):
        if _logprobs:
            config["response_logprobs"] = True
            config["logprobs"] = _top_logprobs if isinstance(_top_logprobs, int) else 1
    elif isinstance(_logprobs, int):
        config["logprobs"] = _logprobs
    elif isinstance(_top_logprobs, int):
        config["logprobs"] = _top_logprobs

    if getattr(request, "response_format", None) is not None:
        fmt = request.response_format
        fmt_type = fmt.get("type", "") if isinstance(fmt, dict) else getattr(fmt, "type", "")
        if fmt_type == "json_object":
            config["response_mime_type"] = "application/json"
        elif fmt_type == "json_schema":
            # OpenAI 结构化输出：{"type":"json_schema","json_schema":{"name":...,"schema":{...}}}
            config["response_mime_type"] = "application/json"
            json_schema_obj = fmt.get("json_schema") if isinstance(fmt, dict) else getattr(fmt, "json_schema", None)
            schema = None
            if isinstance(json_schema_obj, dict):
                schema = json_schema_obj.get("schema")
            if isinstance(schema, dict):
                schema = {k: v for k, v in schema.items() if k != "$schema"}
                config["response_schema"] = schema
    
    # 官方 2026 最新基准配置
    safety_threshold = "BLOCK_NONE"
    
    config["safety_settings"] = [
        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold=safety_threshold),
        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold=safety_threshold),
        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold=safety_threshold),
        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold=safety_threshold),
        types.SafetySetting(category="HARM_CATEGORY_JAILBREAK", threshold=safety_threshold)
    ]
    
    tools_list = []
    if request.tools:
        function_declarations = []
        for tool in request.tools:
            if tool.get("type") == "function":
                func_data = tool.get("function")
                if func_data:
                    declaration = {
                        "name": func_data.get("name"),
                        "description": func_data.get("description"),
                    }
                    parameters = func_data.get("parameters")
                    if isinstance(parameters, dict) and "$schema" in parameters:
                        parameters = parameters.copy()
                        del parameters["$schema"]
                    if parameters is not None:
                        declaration["parameters"] = parameters
                    declaration = {k: v for k, v in declaration.items() if v is not None}
                    if declaration.get("name"): 
                        function_declarations.append(declaration)
        if function_declarations:
            tools_list.append({"function_declarations": function_declarations})

    # 读取控制台设置与模型能力档案（优先级：单次请求 > 模型专属 > 全局 > 内置默认）
    settings = app_state.get_effective_settings(request.model)
    # 控制台的「采样参数处理」可覆盖版本自动判定（新模型版本号更小却已废弃采样时用）
    profile = mc.apply_sampling_policy(mc.get_profile(request.model), settings)
    is_image_model = profile["is_image"]

    if is_image_model:
        config["response_modalities"] = ["TEXT", "IMAGE"]

        # Nano Banana 家族专属（用户决策 2026-10-13）：
        # - 追加 4 个图像安全分类，阈值全 OFF（官方示例基准）
        # - media_resolution=HIGH：提升输入多媒体的理解精细度（非输出尺寸）
        # 旧生图模型维持现状，不加。
        if "nano-banana" in (request.model or "").lower():
            config["safety_settings"].extend([
                types.SafetySetting(category="HARM_CATEGORY_IMAGE_HATE", threshold="OFF"),
                types.SafetySetting(category="HARM_CATEGORY_IMAGE_DANGEROUS_CONTENT", threshold="OFF"),
                types.SafetySetting(category="HARM_CATEGORY_IMAGE_HARASSMENT", threshold="OFF"),
                types.SafetySetting(category="HARM_CATEGORY_IMAGE_SEXUALLY_EXPLICIT", threshold="OFF"),
            ])
            config["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_HIGH

        # 宽高比：两通道共用解析（请求额外字段 > OpenAI size 映射 > 提示词 > 控制台默认，按模型白名单校验）
        target_ar = mc.resolve_aspect_ratio(request.model, request, settings)

        # 分辨率：请求 > 控制台默认，按模型白名单校验并回退
        image_size = mc.resolve_image_size(request.model, request, settings)
        image_config_args = {"image_size": image_size}
        if target_ar:
            image_config_args["aspect_ratio"] = target_ar

        config["image_config"] = types.ImageConfig(**image_config_args)
        # 生图模型不支持自定义函数调用；只有客户端明确声明搜索工具时才保留
        # google_search。普通生图请求与 tool_choice=none 都不得偷偷启用搜索。
        _search_names = {"google_search", "googlesearch", "web_search", "websearch", "search"}
        _declared_search = any(
            str(((tool.get("function") or {}).get("name") or "")).lower() in _search_names
            for tool in (request.tools or []) if isinstance(tool, dict)
        )
        _tools_disabled = isinstance(request.tool_choice, str) and request.tool_choice.lower() == "none"
        tools_list = ([{"google_search": {}}]
                      if _declared_search and not _tools_disabled and profile.get("supports_search")
                      else [])

        # 生图不支持的键（采样类由 sanitize 统一剥离，这里清理其余）
        for key in ["response_mime_type", "response_schema", "response_logprobs", "logprobs"]:
            config.pop(key, None)
    else:
        # 文本/多模态：客户端未显式传采样值时，应用控制台默认（仅注入该模型支持的键）
        if config.get("temperature") is None and settings.get("default_temperature") is not None:
            config["temperature"] = settings["default_temperature"]
        if config.get("top_p") is None and settings.get("default_top_p") is not None:
            config["top_p"] = settings["default_top_p"]
        if config.get("max_output_tokens") is None and settings.get("default_max_tokens") is not None:
            config["max_output_tokens"] = settings["default_max_tokens"]

    # 按模型家族剥离不支持的采样参数（例如 Gemini 3.x 弃用 temperature/top_p/top_k、不支持 candidate_count）
    mc.sanitize_sampling(config, profile)

    if tools_list:
        config["tools"] = tools_list

    tool_config = None
    if request.tool_choice and not is_image_model:
        choice = request.tool_choice
        mode = None
        allowed_functions = None
        if isinstance(choice, str):
            if choice == "none": mode = "NONE"
            elif choice == "auto": mode = "AUTO"
            elif choice == "required": mode = "ANY"
        elif isinstance(choice, dict) and choice.get("type") == "function":
            func_name = choice.get("function", {}).get("name")
            if func_name:
                mode = "ANY"
                allowed_functions = [func_name]
        if mode:
            config_dict = {"mode": mode}
            if allowed_functions: config_dict["allowed_function_names"] = allowed_functions
            tool_config = {"function_calling_config": config_dict}
    
    if tool_config: config["tool_config"] = tool_config
        
    return config


def _extract_usage(resp: Any) -> tuple[int, int, int]:
    """从 SDK 响应里取 (prompt, completion, total) token 数。"""
    um = getattr(resp, "usage_metadata", None)
    if not um:
        return 0, 0, 0
    p_tk = getattr(um, "prompt_token_count", 0) or 0
    c_tk = getattr(um, "candidates_token_count", 0) or 0
    t_tk = getattr(um, "total_token_count", None) or (p_tk + c_tk)
    return p_tk, c_tk, t_tk


def _record_usage(resp: Any) -> dict:
    """记录 token 用量，并打印上游实际返回的流量等级。"""
    usage_metadata = getattr(resp, "usage_metadata", None)
    traffic_type = getattr(usage_metadata, "traffic_type", None) if usage_metadata else None
    if traffic_type:
        value = getattr(traffic_type, "value", None) or str(traffic_type)
        print(f"🚦 [流量等级] 上游实际 traffic_type={value}")

    p_tk, c_tk, t_tk = _extract_usage(resp)
    if p_tk or c_tk:
        stats.add_tokens(p_tk, c_tk)
        print(f"💰 [算力消耗统计] 提示词: {p_tk} | 思考与生成: {c_tk} | 总计: {t_tk} Tokens")
    return {"prompt_tokens": p_tk, "completion_tokens": c_tk, "total_tokens": t_tk}


def wants_usage(request_obj: Any) -> bool:
    """客户端是否通过 stream_options.include_usage 要求在流末尾附带用量（P1-8）。"""
    opts = getattr(request_obj, "stream_options", None)
    if opts is None and getattr(request_obj, "model_extra", None):
        opts = request_obj.model_extra.get("stream_options")
    if isinstance(opts, dict):
        return bool(opts.get("include_usage"))
    return False


def make_usage_chunk(response_id: str, model: str, usage: dict) -> str:
    """OpenAI 风格的用量尾块（choices 为空，仅携带 usage）。"""
    chunk = {"id": response_id, "object": "chat.completion.chunk", "created": int(time.time()),
             "model": model, "choices": [], "usage": usage}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _gemini_prompt_blocked(response: Any) -> bool:
    reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
    return bool(reason) and not str(getattr(reason, "name", reason)).upper().endswith("UNSPECIFIED")


def _empty_gemini_error(response: Any) -> UpstreamError:
    filtered = _gemini_prompt_blocked(response) or any(
        str(getattr(getattr(candidate, "finish_reason", None), "name", getattr(candidate, "finish_reason", ""))).upper()
        in {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}
        for candidate in (getattr(response, "candidates", None) or [])
    )
    return UpstreamError(400 if filtered else 502,
                         "上游拦截了请求。" if filtered else "上游未返回有效回复。",
                         code="content_filter" if filtered else "empty_response")


def is_gemini_response_valid(response: Any) -> bool:
    if response is None: return False
    if hasattr(response, "candidates") and response.candidates:
        for cand in response.candidates:
            if hasattr(cand, "content") and hasattr(cand.content, "parts") and cand.content.parts:
                for part in cand.content.parts:
                    if getattr(part, "thought", False): continue
                    if getattr(part, "function_call", None) is not None: return True
                    if getattr(part, "inline_data", None) is not None: return True
                    if getattr(part, "file_data", None) is not None: return True
                    if hasattr(part, "text") and isinstance(getattr(part, "text", None), str) and getattr(part, "text", "").strip(): return True
            elif isinstance(getattr(cand, "text", None), str) and cand.text.strip(): return True
    elif isinstance(getattr(response, "text", None), str) and response.text.strip(): return True
    return False


class ToolCallIndexer:
    """一次流式响应内，为 tool_calls 分配稳定递增的 index（P0-3）。

    OpenAI 客户端按 delta.tool_calls[].index 累积函数调用。旧实现把 index 硬编码成 0，
    多个并行调用会被前端合并成一个；同时旧实现遇到第一个 function_call 就 break，
    直接丢掉其余并行调用——而官方要求并行调用必须完整按
    FC1,FC2,FR1,FR2 的顺序回传，缺一个就 400。

    序号必须由调用方持有：并行调用可能分布在不同 chunk 里。
    每次重试都要新建一个实例（重试会重发整轮，序号需要归零）。
    """

    def __init__(self):
        self._next: Dict[int, int] = {}
        self._seen_tool_calls: set[int] = set()

    def next_index(self, candidate_index: int = 0) -> int:
        i = self._next.get(candidate_index, 0)
        self._next[candidate_index] = i + 1
        self._seen_tool_calls.add(candidate_index)
        return i

    def record_tool_call(self, candidate_index: int, tool_index: int) -> None:
        self._seen_tool_calls.add(candidate_index)

    def has_tool_calls(self, candidate_index: int = 0) -> bool:
        return candidate_index in self._seen_tool_calls


def convert_chunk_to_openai(chunk: Any, model_name: str, response_id: str, candidate_index: int = 0,
                            indexer: Optional[ToolCallIndexer] = None) -> str:
    from message_processing import (
        parse_gemini_response_for_reasoning_and_content,
        build_tool_call_id,
    )
    from signature_store import SignatureState
    delta_payload = {}
    openai_finish_reason = None

    if hasattr(chunk, "candidates") and chunk.candidates and len(chunk.candidates) > candidate_index:
        candidate = chunk.candidates[candidate_index]
        raw_gemini_finish_reason = getattr(candidate, "finish_reason", None)
        if raw_gemini_finish_reason:
            if hasattr(raw_gemini_finish_reason, "name"): raw_gemini_finish_reason_str = raw_gemini_finish_reason.name.upper()
            else: raw_gemini_finish_reason_str = str(raw_gemini_finish_reason).upper()

            if raw_gemini_finish_reason_str == "STOP": openai_finish_reason = "stop"
            elif raw_gemini_finish_reason_str == "MAX_TOKENS": openai_finish_reason = "length"
            elif raw_gemini_finish_reason_str == "SAFETY": openai_finish_reason = "content_filter"
            elif raw_gemini_finish_reason_str in ["TOOL_CODE", "FUNCTION_CALL"]: openai_finish_reason = "tool_calls"

        tool_call_deltas = []
        if hasattr(candidate, "content") and hasattr(candidate.content, "parts") and candidate.content.parts:
            for part in candidate.content.parts:
                fc = getattr(part, "function_call", None)
                if fc is None:
                    continue
                tc_index = indexer.next_index(candidate_index) if indexer else len(tool_call_deltas)
                tc = {
                    "index": tc_index,
                    "id": build_tool_call_id(
                        fc, part,
                        missing_state=(SignatureState.UNSIGNED_FOLLOWER
                                       if tc_index > 0 else SignatureState.UNKNOWN)),
                    "type": "function",
                    "function": {
                        "name": fc.name,
                        "arguments": json.dumps(fc.args) if fc.args is not None else "",
                    },
                }
                if indexer:
                    indexer.record_tool_call(candidate_index, tc_index)
                tool_call_deltas.append(tc)

        # Google often emits the functionCall in one chunk and a final STOP in a
        # later empty chunk. Once this candidate has produced any tool call, the
        # OpenAI finish reason for that turn must remain tool_calls.
        if (openai_finish_reason == "stop" and indexer
                and indexer.has_tool_calls(candidate_index)):
            openai_finish_reason = "tool_calls"

        if tool_call_deltas:
            delta_payload["tool_calls"] = tool_call_deltas

        reasoning_text, normal_text = parse_gemini_response_for_reasoning_and_content(candidate)

        # Only append safety ratings to the final chunk.
        if (openai_finish_reason and _safety_score_enabled()
                and hasattr(candidate, "safety_ratings") and candidate.safety_ratings):
            normal_text += _create_safety_ratings_html(candidate.safety_ratings)

        if reasoning_text:
            delta_payload["reasoning_content"] = reasoning_text
        if normal_text:
            delta_payload["content"] = normal_text
        elif tool_call_deltas:
            delta_payload["content"] = None
        elif not reasoning_text and openai_finish_reason is None:
            delta_payload["content"] = ""

    if not delta_payload and openai_finish_reason is None:
        delta_payload["content"] = ""

    chunk_data = {
        "id": response_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model_name,
        "choices": [{"index": candidate_index, "delta": delta_payload, "finish_reason": openai_finish_reason}]
    }
    return f"data: {json.dumps(chunk_data)}\n\n"

def create_final_chunk(model: str, response_id: str, candidate_count: int = 1) -> str:
    choices = [{"index": i, "delta": {}, "finish_reason": "stop"} for i in range(candidate_count)]
    final_chunk_data = {"id": response_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model, "choices": choices}
    return f"data: {json.dumps(final_chunk_data)}\n\n"

async def _chunk_openai_response_dict_for_sse(
    openai_response_dict: Dict[str, Any],
    response_id_override: Optional[str] = None, 
    model_name_override: Optional[str] = None
):
    resp_id = response_id_override or openai_response_dict.get("id", f"chatcmpl-fakestream-{int(time.time())}")
    model_name = model_name_override or openai_response_dict.get("model", "unknown")
    created_time = openai_response_dict.get("created", int(time.time()))
    
    choices = openai_response_dict.get("choices", [])
    if not choices:
        yield error_sse(UpstreamError(502, "上游未返回有效回复。", code="empty_response"))
        yield "data: [DONE]\n\n"
        return

    for choice_idx, choice in enumerate(choices): 
        message = choice.get("message", {})
        final_finish_reason = choice.get("finish_reason", "stop")

        def _sse(delta):
            payload = {"id": resp_id, "object": "chat.completion.chunk", "created": created_time,
                       "model": model_name, "choices": [{"index": choice_idx, "delta": delta,
                                                           "finish_reason": None}]}
            return f"data: {json.dumps(payload)}\n\n"

        # A response may legitimately mix thoughts, visible text, tool calls and
        # message-level signature metadata in the same assistant turn. Emit each
        # independently instead of the old mutually-exclusive tool/text branches.
        if message.get("extra_content") is not None:
            yield _sse({"extra_content": message["extra_content"]})

        reasoning_content = message.get("reasoning_content", "")
        actual_content = message.get("content")
        if reasoning_content:
            yield _sse({"reasoning_content": reasoning_content})
            await asyncio.sleep(0.01)

        if actual_content is not None:
            content_to_chunk = actual_content
            # Keep generated images whole; chunk ordinary text for fake-stream UX.
            if "![Image](data:image/" in content_to_chunk:
                chunk_size = max(1, len(content_to_chunk))
            else:
                chunk_size = max(1, math.ceil(len(content_to_chunk) / 10)) if content_to_chunk else 1
            if not content_to_chunk and not reasoning_content:
                yield _sse({"content": ""})
            else:
                for i in range(0, len(content_to_chunk), chunk_size):
                    yield _sse({"content": content_to_chunk[i:i + chunk_size]})
                    if len(content_to_chunk) > chunk_size:
                        await asyncio.sleep(0.01)

        for tc_item_idx, tool_call_item in enumerate(message.get("tool_calls") or []):
            tool_delta = {
                "index": tc_item_idx,
                "id": tool_call_item["id"],
                "type": "function",
                "function": {"name": tool_call_item["function"]["name"], "arguments": ""},
            }
            if tool_call_item.get("extra_content") is not None:
                tool_delta["extra_content"] = tool_call_item["extra_content"]
            yield _sse({"tool_calls": [tool_delta]})
            await asyncio.sleep(0.01)
            yield _sse({"tool_calls": [{
                "index": tc_item_idx,
                "id": tool_call_item["id"],
                "function": {"arguments": tool_call_item["function"]["arguments"]},
            }]})
            await asyncio.sleep(0.01)
        
        yield f"data: {json.dumps({'id': resp_id, 'object': 'chat.completion.chunk', 'created': created_time, 'model': model_name, 'choices': [{'index': choice_idx, 'delta': {}, 'finish_reason': final_finish_reason}]})}\n\n"

    yield "data: [DONE]\n\n"

async def gemini_fake_stream_generator(
    gemini_client_instance: Any,
    model_for_api_call: str,
    prompt_for_api_call: List[types.Content],
    gen_config_dict_for_api_call: Dict[str, Any],
    request_obj: OpenAIRequest,
    is_auto_attempt: bool,
    fastapi_request: Optional[Any] = None,
    trace: Optional[UpstreamTrace] = None,
    read_timeout_seconds: Optional[float] = None,
):
    if trace is None:
        trace = UpstreamTrace("Express", model_for_api_call, uuid.uuid4().hex[:8], time.monotonic(), unit_label="块")
    if read_timeout_seconds is None:
        read_timeout_seconds = app_state.get_express_nonstream_read_timeout_seconds()
    attach_express_trace(gemini_client_instance, trace, read_timeout_seconds=read_timeout_seconds)
    is_image_request = mc.get_profile(request_obj.model)["is_image"]
    print(f"🌊 [假流式] 已开始调用 Gemini 模型 {model_for_api_call}，客户端请求模型名为 {request_obj.model}。")

    # P1-6：不再使用 tenacity 的硬编码 20 次，改为与真流式/非流式一致的手写退避，
    # 读取控制台的 retry_max / retry_backoff_seconds，并在等待期间检测客户端断开。
    max_retries, backoff_sec = get_retry_settings()

    async def _client_gone() -> bool:
        if fastapi_request is None:
            return False
        try:
            return await fastapi_request.is_disconnected()
        except Exception:
            return False

    outer_keep_alive_interval = app_state.get_setting(
        "fake_streaming_interval", app_config.FAKE_STREAMING_INTERVAL_SECONDS)

    api_call_task = None
    raw_gemini_response = None
    last_error = None

    try:
        for attempt in range(max_retries + 1):
            if await _client_gone():
                print(f"ℹ️ [客户端断开] 假流式请求前检测到客户端已断开，停止调用模型 {model_for_api_call}。")
                return

            trace.begin_attempt()
            api_call_task = asyncio.create_task(
                gemini_client_instance.aio.models.generate_content(
                    model=model_for_api_call,
                    contents=prompt_for_api_call,
                    config=gen_config_dict_for_api_call,
                )
            )

            # 等待期间持续吐 keep-alive，避免前端因长时间无字节而超时
            while not api_call_task.done():
                if outer_keep_alive_interval > 0:
                    keep_alive_data = {"id": "chatcmpl-keepalive", "object": "chat.completion.chunk",
                                       "created": int(time.time()), "model": request_obj.model,
                                       "choices": [{"delta": {"content": ""}, "index": 0, "finish_reason": None}]}
                    yield f"data: {json.dumps(keep_alive_data)}\n\n"
                    await asyncio.sleep(outer_keep_alive_interval)
                else:
                    await asyncio.sleep(0.2)
                if await _client_gone():
                    print("ℹ️ [客户端断开] 假流式等待期间客户端已断开，正在取消上游任务。")
                    api_call_task.cancel()
                    return

            try:
                raw_gemini_response = await api_call_task
                break
            except asyncio.CancelledError:
                raise
            except Exception as e_call:
                last_error = e_call
                if not isinstance(e_call, UpstreamError) and not hasattr(e_call, "code"):
                    trace.transport_failure(e_call)
                if is_retryable_exception(e_call) and attempt < max_retries:
                    stats.add_retry()
                    _, err_msg = extract_upstream_error(e_call)
                    print(f"⚠️ [Express] {trace.label} | 首段输出前暂时失败：{err_msg}"
                          f" | {backoff_sec}s 后进行第 {attempt + 2}/{max_retries + 1} 次尝试 | {trace.summary()}")
                    waited = 0.0
                    while waited < backoff_sec:
                        step = min(max(0.5, outer_keep_alive_interval or 1.0), backoff_sec - waited)
                        await asyncio.sleep(step)
                        waited += step
                        if await _client_gone():
                            print("ℹ️ [客户端断开] 假流式退避期间客户端已断开，停止重试。")
                            return
                        if outer_keep_alive_interval > 0:
                            keep_alive_data = {"id": "chatcmpl-keepalive", "object": "chat.completion.chunk",
                                               "created": int(time.time()), "model": request_obj.model,
                                               "choices": [{"delta": {"content": ""}, "index": 0, "finish_reason": None}]}
                            yield f"data: {json.dumps(keep_alive_data)}\n\n"
                    continue
                raise

        if raw_gemini_response is None:
            raise last_error or ValueError("上游未返回任何响应（重试已耗尽）。")

        trace.objects += 1
        _observe_gemini_response(trace, raw_gemini_response)
        _record_usage(raw_gemini_response)

        openai_response_dict = convert_to_openai_format(raw_gemini_response, request_obj.model)

        if _gemini_prompt_blocked(raw_gemini_response):
            print(f"❌ [Express] {trace.label} | 上游拦截了请求 | {trace.summary()}")
            raise UpstreamError(400, "上游拦截了请求。", code="content_filter")

        if not is_gemini_response_valid(raw_gemini_response):
            desc = "只返回了思考、未返回正文" if "thought" in trace.kinds else "未返回任何内容"
            print(f"❌ [Express] {trace.label} | 上游{desc} | {trace.summary()}")
            raise _empty_gemini_error(raw_gemini_response)

        async for chunk_sse in _chunk_openai_response_dict_for_sse(
            openai_response_dict=openai_response_dict
        ):
            yield chunk_sse

        done_label = "生图假流式完成" if is_image_request else "假流式完成"
        print(f"✅ [Express] {trace.label} | {done_label} | {trace.summary()}")

    except asyncio.CancelledError:
        raise
    except Exception as e_outer_gemini:
        err_code, err_msg = extract_upstream_error(e_outer_gemini)
        icon = "🔑" if err_code in (401, 403) else "❌"
        print(f"{icon} [Express] {trace.label} | HTTP {err_code} | {err_msg} | {trace.summary()}")
        if not is_auto_attempt:
            yield error_sse(e_outer_gemini)
            yield "data: [DONE]\n\n"
        if is_auto_attempt: raise
    finally:
        if "api_call_task" in locals():
            await cancel_and_wait(api_call_task)
            
def is_location_pin_failure(err: Any) -> bool:
    """错误是否像"钉定的 projects/locations 路径不对"（而非模型或网络本身的问题）。

    典型两种：
      404 `Publisher model projects/X/locations/Y/... was not found`  → 该项目/区域没有这个模型
      403 `This API method requires billing to be enabled ... project #X` → 项目没开计费/无权
    命中时可以把模型名退回**裸模型名**再试一次，让后端自行路由——
    这样即使用户填的 Project ID 与 API Key 不属于同一项目，也只是回到旧行为，不会更糟。
    """
    e = str(err).lower()
    if "requires billing" in e or "billing to be enabled" in e:
        return True
    if ("not found" in e or "404" in e) and "publisher model" in e:
        return True
    if "permission_denied" in e and "projects/" in e:
        return True
    return False


async def execute_gemini_call(
    current_client: Any,
    model_to_call: str,
    prompt_func: Callable[[List[OpenAIMessage]], List[types.Content]],
    gen_config_dict: Dict[str, Any],
    request_obj: OpenAIRequest,
    is_auto_attempt: bool = False,
    fastapi_request: Optional[Any] = None,
    fallback_model: Optional[str] = None,
    fallback_client_factory: Optional[Callable[[], Any]] = None,
    trace: Optional[UpstreamTrace] = None,
):
    fallback_client = None
    if trace is None:
        trace = UpstreamTrace("Express", model_to_call, uuid.uuid4().hex[:8], time.monotonic(), unit_label="块")

    is_image_request = mc.get_profile(request_obj.model)["is_image"]
    use_fake_stream = bool(
        request_obj.stream and (
            app_state.get_setting("fake_streaming", app_config.FAKE_STREAMING_ENABLED) or is_image_request
        )
    )
    read_timeout_seconds = (
        app_state.get_express_stream_read_timeout_seconds()
        if (request_obj.stream and not use_fake_stream)
        else app_state.get_express_nonstream_read_timeout_seconds()
    )
    attach_express_trace(current_client, trace, read_timeout_seconds=read_timeout_seconds)

    def _get_fallback_client():
        nonlocal fallback_client
        if fallback_client_factory is None:
            return current_client
        if fallback_client is None:
            fallback_client = attach_express_trace(
                fallback_client_factory(), trace, read_timeout_seconds=read_timeout_seconds,
            )
        return fallback_client

    # P1-2：prompt 构建内部有远程图片下载与 PIL 压缩（同步阻塞），
    # 放到线程里执行，避免卡住整个事件循环。
    actual_prompt_for_call = await asyncio.to_thread(prompt_func, request_obj.messages)

    if request_obj.stream:
        mode_name = "生图假流式" if is_image_request else ("假流式" if use_fake_stream else "流式")
    else:
        mode_name = "非流式"
    print(f"→ [Express] {trace.label} | {len(request_obj.messages)} 条消息 | {mode_name}")

    async def _client_gone() -> bool:
        """检测客户端是否已断开连接（用于在重试前及时止损）。"""
        if fastapi_request is None:
            return False
        try:
            return await fastapi_request.is_disconnected()
        except Exception:
            return False

    if request_obj.stream:
        if use_fake_stream:
            if is_image_request:
                print("🖼️ [生图保护] 图片模型请求已自动切换为假流式输出，以避免上游流式限制。")
            return StreamingResponse(
                gemini_fake_stream_generator(
                    current_client, model_to_call, actual_prompt_for_call,
                    gen_config_dict, request_obj, is_auto_attempt,
                    fastapi_request=fastapi_request,
                    trace=trace,
                    read_timeout_seconds=read_timeout_seconds,
                ), media_type="text/event-stream"
            )
        else: # True Streaming
            response_id_for_stream = f"chatcmpl-realstream-{int(time.time())}"
            async def _gemini_real_stream_generator_inner():
                # 钉定失败要在这里改写模型名与客户端，必须声明 nonlocal。
                nonlocal model_to_call, current_client
                max_retries, backoff_sec = get_retry_settings()
                has_yielded = False  # 是否已向客户端输出过内容
                got_output = False
                # 立即吐一个 SSE 心跳，尽快建立连接（429 重试期间也保活，防前端超时中断）
                yield ": keep-alive\n\n"
                # 总尝试次数 = retry_max + 1，retry_max=0 时仍会请求一次
                for attempt in range(max_retries + 1):
                    # 客户端断开则停止重试，避免无谓的上游调用
                    if await _client_gone():
                        print(f"ℹ️ [客户端断开] 真流式请求前检测到客户端已断开，停止调用模型 {model_to_call}。")
                        return
                    stream_gen_obj = None
                    trace.begin_attempt()
                    try:
                        stream_gen_obj = await current_client.aio.models.generate_content_stream(
                            model=model_to_call,
                            contents=actual_prompt_for_call,
                            config=gen_config_dict
                        )

                        # 每次 attempt 都重置 tool_calls 序号（重试会重发整轮）
                        tool_indexer = ToolCallIndexer()

                        final_p_tk, final_c_tk, final_t_tk = 0, 0, 0
                        last_chunk = None

                        async for chunk_item_call in stream_gen_obj:
                            last_chunk = chunk_item_call
                            trace.objects += 1
                            if _gemini_prompt_blocked(chunk_item_call):
                                raise UpstreamError(400, "上游拦截了请求。", code="content_filter")
                            _observe_gemini_response(trace, chunk_item_call)
                            got_output = got_output or is_gemini_response_valid(chunk_item_call)
                            if getattr(chunk_item_call, "usage_metadata", None):
                                final_p_tk, final_c_tk, final_t_tk = _extract_usage(chunk_item_call)

                            # 支持 n>1：按候选序号逐个输出
                            num_candidates = len(chunk_item_call.candidates) if getattr(chunk_item_call, "candidates", None) else 1
                            for ci in range(num_candidates):
                                has_yielded = True
                                sse_chunk = convert_chunk_to_openai(
                                    chunk_item_call, request_obj.model, response_id_for_stream, ci,
                                    indexer=tool_indexer)
                                yield sse_chunk

                        if not got_output:
                            desc = "只返回了思考、未返回正文" if "thought" in trace.kinds else "未返回任何内容"
                            print(f"❌ [Express] {trace.label} | 上游{desc} | {trace.summary()}")
                            raise _empty_gemini_error(last_chunk)

                        if final_p_tk > 0 or final_c_tk > 0:
                            stats.add_tokens(final_p_tk, final_c_tk)
                            print(f"💰 [算力消耗统计] 提示词: {final_p_tk} | 思考与生成: {final_c_tk} | 总计: {final_t_tk} Tokens")

                        # P1-8：Express 真流式此前从不发 usage 块，客户端只能显示 0
                        if wants_usage(request_obj):
                            yield make_usage_chunk(response_id_for_stream, request_obj.model, {
                                "prompt_tokens": final_p_tk,
                                "completion_tokens": final_c_tk,
                                "total_tokens": final_t_tk,
                            })

                        yield "data: [DONE]\n\n"
                        print(f"✅ [Express] {trace.label} | 流式传输顺利完毕 | {trace.summary()}")
                        return

                    except asyncio.CancelledError:
                        raise
                    except Exception as e_stream_call:
                        if not isinstance(e_stream_call, UpstreamError) and not hasattr(e_stream_call, "code"):
                            trace.transport_failure(e_stream_call)
                        err_code, err_msg = extract_upstream_error(e_stream_call)
                        error_str = str(e_stream_call).lower()
                        is_retryable = (
                            not isinstance(e_stream_call, TIMEOUT_EXCEPTIONS)
                            and (
                                "429" in error_str or "503" in error_str or "too many requests" in error_str
                                or "quota" in error_str or "resource exhausted" in error_str
                            )
                        )
                        if err_code in (401, 403):
                            is_retryable = False

                        # location 钉定路径不对 → 退回裸模型名再试一次（仅在还没输出内容时）
                        if (fallback_model and model_to_call != fallback_model
                                and err_code not in (401, 403)
                                and not has_yielded and is_location_pin_failure(e_stream_call)):
                            print(f"↩️ [上游端点] 钉定路径调用失败（{str(e_stream_call)[:80]}），"
                                  f"已退回默认路由 {fallback_model} 重试一次。"
                                  "如持续出现，请确认「通道与凭证」里的 Project ID 属于该 API Key 且已开启计费，"
                                  "或把「标准模式 location」设为“默认（后端自选）”。")
                            model_to_call = fallback_model
                            current_client = _get_fallback_client()
                            print("ℹ️ [流量等级] 回退默认路由已使用普通请求。")
                            continue

                        # 关键修复：只有在“尚未向客户端输出任何内容”时才重试；
                        # 否则重试会导致整段答案重复输出（前半段 + 完整重发）。
                        if is_retryable and not has_yielded and attempt < max_retries:
                            # F-5：退避时长改读控制台的 retry_backoff_seconds，
                            # 原先硬编码 2**(attempt%4)，控制台设置对真流式完全没作用。
                            wait_time = backoff_sec
                            stats.add_retry() # 核心：手动重试计入大盘
                            print(f"⚠️ [Express] {trace.label} | 首段输出前暂时失败：{err_msg}"
                                  f" | {wait_time}s 后进行第 {attempt + 2}/{max_retries + 1} 次尝试 | {trace.summary()}")
                            if await _client_gone():
                                print(f"ℹ️ [客户端断开] 重试前检测到客户端已断开，停止调用模型 {model_to_call}。")
                                return
                            # 退避等待期间持续吐 SSE 心跳，保活前端连接
                            _waited = 0.0
                            while _waited < wait_time:
                                await asyncio.sleep(min(3.0, wait_time - _waited))
                                _waited += 3.0
                                if await _client_gone():
                                    print(f"ℹ️ [客户端断开] 重试等待期间检测到客户端已断开，停止调用模型 {model_to_call}。")
                                    return
                                yield ": keep-alive\n\n"
                            continue

                        if err_code in (401, 403):
                            print(f"🔑 [Express] {trace.label} | HTTP {err_code} | 认证/权限失败，本次请求已结束。")
                        elif has_yielded:
                            print(f"❌ [Express] {trace.label} | 上游输出中断：{err_msg} | {trace.summary()}")
                        elif not isinstance(e_stream_call, UpstreamError) or e_stream_call.code != "empty_response":
                            err_prefix = "重试耗尽" if is_retryable else "请求终止"
                            print(f"❌ [Express] {trace.label} | {err_prefix}：{err_msg} | {trace.summary()}")
                        if is_auto_attempt:
                            raise e_stream_call
                        # 错误独立于 delta.content；已输出正文也必须报告失败。
                        yield error_sse(e_stream_call)
                        yield "data: [DONE]\n\n"
                        return
                    finally:
                        close = getattr(stream_gen_obj, "aclose", None)
                        if close is not None:
                            await close()

            return StreamingResponse(_gemini_real_stream_generator_inner(), media_type="text/event-stream")
    else: # Non-streaming
        # 手动退避重试循环（替代 tenacity），以便在每次重试前检测客户端断开
        max_retries, backoff_sec = get_retry_settings()
        response_obj_call = None
        # 总尝试次数 = retry_max + 1，retry_max=0 时仍会请求一次
        for attempt in range(max_retries + 1):
            if await _client_gone():
                print(f"ℹ️ [客户端断开] 非流式请求前检测到客户端已断开，停止调用模型 {model_to_call}。")
                return JSONResponse(
                    status_code=499,
                    content=create_openai_error_response(499, "客户端已断开连接，请求已取消。", "client_closed_request"),
                )
            trace.begin_attempt()
            try:
                response_obj_call = await current_client.aio.models.generate_content(
                    model=model_to_call,
                    contents=actual_prompt_for_call,
                    config=gen_config_dict,
                )
                break
            except asyncio.CancelledError:
                raise
            except Exception as e_call:
                if not isinstance(e_call, UpstreamError) and not hasattr(e_call, "code"):
                    trace.transport_failure(e_call)
                err_code, err_msg = extract_upstream_error(e_call)
                if (fallback_model and model_to_call != fallback_model
                        and err_code not in (401, 403)
                        and is_location_pin_failure(e_call)):
                    print(f"↩️ [上游端点] 钉定路径调用失败（{str(e_call)[:80]}），"
                          f"已退回默认路由 {fallback_model} 重试一次。"
                          "如持续出现，请确认「通道与凭证」里的 Project ID 属于该 API Key 且已开启计费，"
                          "或把「标准模式 location」设为“默认（后端自选）”。")
                    model_to_call = fallback_model
                    current_client = _get_fallback_client()
                    print("ℹ️ [流量等级] 回退默认路由已使用普通请求。")
                    continue
                if is_retryable_exception(e_call) and attempt < max_retries:
                    stats.add_retry()
                    wait_time = backoff_sec   # F-5：同上，统一用控制台配置的退避
                    print(f"⚠️ [Express] {trace.label} | 上游暂时失败：{err_msg}"
                          f" | {wait_time}s 后进行第 {attempt + 2}/{max_retries + 1} 次尝试 | {trace.summary()}")
                    await asyncio.sleep(wait_time)
                    continue
                icon = "🔑" if err_code in (401, 403) else "❌"
                print(f"{icon} [Express] {trace.label} | HTTP {err_code} | {err_msg} | {trace.summary()}")
                if isinstance(e_call, TIMEOUT_EXCEPTIONS):
                    raise UpstreamError(504, err_msg, "timeout") from e_call
                raise

        # 兜底：绝不让 None 流到下游的有效性检查里变成一条误导性的“无有效内容”
        if response_obj_call is None:
            print(f"❌ [Express] {trace.label} | HTTP 502 | 上游未返回任何响应（重试已耗尽）。| {trace.summary()}")
            return JSONResponse(
                status_code=502,
                content=create_openai_error_response(
                    502, "上游未返回任何响应（重试已耗尽）。", "upstream_error"),
            )

        trace.objects += 1
        _observe_gemini_response(trace, response_obj_call)

        if _gemini_prompt_blocked(response_obj_call):
            block_msg = f"Agent Platform 安全策略拦截了请求：{response_obj_call.prompt_feedback.block_reason}"
            if hasattr(response_obj_call.prompt_feedback,"block_reason_message") and \
               response_obj_call.prompt_feedback.block_reason_message:
                block_msg+=f"（{response_obj_call.prompt_feedback.block_reason_message}）"
            print(f"❌ [Express] {trace.label} | {block_msg} | {trace.summary()}")
            raise UpstreamError(400, "上游拦截了请求。", code="content_filter")

        if not is_gemini_response_valid(response_obj_call):
            desc = "只返回了思考、未返回正文" if "thought" in trace.kinds else "未返回任何内容"
            print(f"❌ [Express] {trace.label} | 上游{desc} | {trace.summary()}")
            raise _empty_gemini_error(response_obj_call)

        _record_usage(response_obj_call)
        print(f"✅ [Express] {trace.label} | 非流式完成 | {trace.summary()}")

        openai_response_content = convert_to_openai_format(response_obj_call, request_obj.model)
        return JSONResponse(content=openai_response_content)
