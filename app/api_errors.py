"""API failures are protocol errors, never assistant message content."""

import ast
import codecs
from contextlib import suppress
import json
import re

import httpx
import anyio
from fastapi.responses import JSONResponse, StreamingResponse


_STATUS_NAMES = {
    "INVALID_ARGUMENT": 400, "FAILED_PRECONDITION": 400,
    "UNAUTHENTICATED": 401, "PERMISSION_DENIED": 403, "NOT_FOUND": 404,
    "RESOURCE_EXHAUSTED": 429, "INTERNAL": 500, "UNAVAILABLE": 503,
    "DEADLINE_EXCEEDED": 504,
}
_GRPC_STATUS = {3: 400, 4: 504, 5: 404, 7: 403, 8: 429, 9: 400, 13: 500, 14: 503, 16: 401}
_ERROR_CODES = {
    400: "invalid_request_error", 401: "authentication_error", 403: "permission_denied",
    404: "not_found", 408: "request_timeout", 409: "conflict",
    422: "unprocessable_entity", 429: "rate_limit_exceeded", 499: "request_cancelled",
    500: "server_error", 502: "bad_gateway", 503: "service_unavailable", 504: "upstream_timeout",
}


def error_code_for_status(status):
    return _ERROR_CODES.get(status, "upstream_error")


def _http_status(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 400 <= value <= 599 else _GRPC_STATUS.get(value)
    if isinstance(value, str):
        if value.isdigit():
            return _http_status(int(value))
        return (_STATUS_NAMES.get(value.upper())
                or next((status for status, code in _ERROR_CODES.items() if value == code), None))
    return None


def error_type_for_status(status):
    return {400: "invalid_request_error", 401: "authentication_error",
            403: "permission_error", 404: "not_found_error", 429: "rate_limit_error",
            504: "timeout"}.get(status, "upstream_error")


class UpstreamError(Exception):
    def __init__(self, status_code, message, error_type=None, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type or error_type_for_status(status_code)
        self.code = code if isinstance(code, str) and not code.isdigit() else error_code_for_status(status_code)


def upstream_error(value, status_code=None, error_type=None):
    if isinstance(value, UpstreamError) and status_code is None and error_type is None:
        return value
    status = _http_status(status_code)
    code = None
    if isinstance(value, httpx.TimeoutException):
        return UpstreamError(status or 504, "上游请求超时。", error_type)
    if isinstance(value, httpx.RequestError):
        return UpstreamError(status or 502, "上游连接中断。", error_type)
    if isinstance(value, httpx.HTTPStatusError):
        status = status or value.response.status_code
        try:
            value = value.response.json()
        except ValueError:
            value = value.response.text
    elif isinstance(value, Exception):
        status = status or _http_status(getattr(value, "status_code", None)) or _http_status(getattr(value, "code", None))
        error_type = error_type or getattr(value, "error_type", None)
        value = getattr(value, "message", None) or str(value) or type(value).__name__
    if isinstance(value, str):
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(value)
            except (ValueError, SyntaxError, TypeError):
                continue
            if isinstance(parsed, dict):
                value = parsed
                break
    if isinstance(value, dict):
        status = status or _http_status(value.get("status_code"))
        detail = value.get("error", value)
        if not isinstance(detail, dict):
            detail = {"message": detail}
        extensions = detail.get("extensions") or {}
        if not isinstance(extensions, dict):
            extensions = {}
        status = (status or _http_status(detail.get("code")) or _http_status(detail.get("status"))
                  or _http_status(extensions.get("code")))
        error_type = error_type or detail.get("type")
        status = status or {"invalid_request_error": 400, "authentication_error": 401,
                           "permission_error": 403, "rate_limit_error": 429, "timeout": 504}.get(error_type)
        if isinstance(detail.get("code"), str) and not _http_status(detail["code"]):
            code = detail["code"]
        value = detail.get("message") or "上游请求失败。"
    message = str(value).strip() or "上游请求失败。"
    if status is None:
        upper = message.upper()
        status = next((code for name, code in _STATUS_NAMES.items() if name in upper), None)
        if status is None:
            match = re.search(r"\b(?:HTTP\s*)?([45]\d\d)\b", message)
            status = int(match.group(1)) if match else None
        low = message.lower()
        if status is None:
            if any(word in low for word in ("resource exhausted", "rate limit", "quota", "too many requests")):
                status = 429
            elif any(word in low for word in ("unauthenticated", "invalid credentials", "login required", "session expired")):
                status = 401
            elif any(word in low for word in ("permission", "denied", "not authorized")):
                status = 403
            elif "not found" in low:
                status = 404
            elif "invalid" in low:
                status = 400
            elif "timeout" in low or "timed out" in low:
                status = 504
    return UpstreamError(status or 502, message, error_type, code)


def create_openai_error_response(status_code, message, error_type, code=None):
    message = re.sub(r"([?&]key=)[^&\s'\"]+", r"\1***HIDDEN_API_KEY***", str(message))
    message = re.sub(r"Bearer\s+[^\s'\"]+", "Bearer ***", message, flags=re.I)
    message = " ".join(message.split())[:512]
    return {"error": {"message": message, "type": error_type,
                      "code": (code if isinstance(code, str) and not code.isdigit()
                               else error_code_for_status(status_code)), "param": None}}


def error_payload(error):
    error = upstream_error(error)
    return create_openai_error_response(error.status_code, str(error), error.error_type, error.code)


def error_response(error):
    error = upstream_error(error)
    return terminal_error_response(JSONResponse(status_code=error.status_code, content=error_payload(error)))


def terminal_error_response(response):
    """Our retry budget is over; SDKs supporting this hint must not start another one.

    This is an SDK compatibility header, not a replacement HTTP status or an
    HTTP-standard guarantee that an arbitrary gateway will not retry.
    """
    if response.status_code >= 400:
        response.headers["x-should-retry"] = "false"
        response.headers["Cache-Control"] = "no-store"
    return response


def error_sse(error):
    return f"data: {json.dumps(error_payload(error), ensure_ascii=False)}\n\n"


def extract_upstream_error(error):
    error = upstream_error(error)
    return error.status_code, str(error)


async def prepare_stream_response(response):
    """Do not commit HTTP 200 for heartbeats or role-only stream preambles.

    An error before actual output is an HTTP error. Once real output starts,
    the existing iterator and its independent SSE error events are preserved.
    """
    if not isinstance(response, StreamingResponse):
        return response
    iterator = response.body_iterator
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    preamble = []
    transferred = False

    async def close():
        with anyio.CancelScope(shield=True):
            closer = getattr(iterator, "aclose", None)
            if closer is not None:
                with suppress(Exception):
                    await closer()

    def failed(error):
        result = error_response(error)
        for key, value in response.headers.items():
            if key.lower() not in {"content-type", "content-length", "content-encoding", "transfer-encoding"}:
                result.headers[key] = value
        result.background = response.background
        return result

    try:
        async for chunk in iterator:
            text = decoder.decode(chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8"))
            pending = (pending + text).replace("\r\n", "\n")
            while "\n\n" in pending:
                frame, pending = pending.split("\n\n", 1)
                data = "\n".join(line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:"))
                if not data:
                    continue
                if data == "[DONE]":
                    return failed(UpstreamError(502, "上游未返回有效回复。", code="empty_response"))
                try:
                    payload = json.loads(data)
                except ValueError:
                    return failed(UpstreamError(502, "上游流式响应格式错误。"))
                if not isinstance(payload, dict):
                    return failed(UpstreamError(502, "上游流式响应格式错误。"))
                if "error" in payload:
                    return failed(upstream_error(payload))
                if "choices" in payload:
                    deltas = [choice.get("delta") or {} for choice in payload["choices"]]
                    has_output = any(delta.get("content") or delta.get("reasoning_content")
                                     or delta.get("tool_calls") or delta.get("function_call") for delta in deltas)
                    if not has_output:
                        if any(delta.get("role") for delta in deltas) and len(preamble) < 64:
                            preamble.append(frame + "\n\n")
                        continue

                prefix = preamble + [frame + "\n\n"]
                if pending:
                    prefix.append(pending)
                async def replay():
                    try:
                        for initial in prefix:
                            yield initial
                        async for remaining in iterator:
                            decoded = decoder.decode(remaining if isinstance(remaining, bytes) else str(remaining).encode("utf-8"))
                            if decoded:
                                yield decoded
                        tail = decoder.decode(b"", final=True)
                        if tail:
                            yield tail
                    finally:
                        await close()

                response.body_iterator = replay()
                transferred = True
                return response
        return failed(UpstreamError(502, "上游未返回有效回复。", code="empty_response"))
    except Exception as error:
        return failed(error)
    finally:
        if not transferred:
            await close()


class SSEErrorScanner:
    """Recognize error envelopes, including frames split across iterator chunks."""

    def __init__(self):
        self.pending = ""

    def feed(self, chunk):
        self.pending = (self.pending + chunk).replace("\r\n", "\n")
        while "\n\n" in self.pending:
            frame, self.pending = self.pending.split("\n\n", 1)
            data = "\n".join(line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:"))
            if not data or data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except ValueError:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                return upstream_error(payload)
        if len(self.pending) > 65536:
            self.pending = ""
        return None
