import asyncio
import json
import uuid
import hmac

import anyio
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from auth import get_api_key
from models import OpenAIRequest
from runtime_state import app_state
from upstreams.express_sdk import ExpressSDKUpstream
from upstreams.cookie_proxy import CookieProxyUpstream
from api_helpers import extract_upstream_error, create_openai_error_response
from api_errors import error_response, prepare_stream_response, terminal_error_response
from request_lifecycle import (
    ClientDisconnected, GenerationJob, JobRequest, ManagedStreamingResponse,
    active_jobs, cancel_and_wait, while_connected, finished_job, owner_digest,
)
from resumable import (
    resumable_store, compute_stream_id, is_enabled as resumable_enabled,
    pump_stream, sse_from_buffer, frame_with_id, DONE_FRAME,
)

router = APIRouter()
express_upstream = ExpressSDKUpstream()
cookie_upstream = CookieProxyUpstream()


async def _call_upstream(request_obj: OpenAIRequest, fastapi_request: Request):
    upstream = cookie_upstream if app_state.is_web_proxy_enabled() else express_upstream
    return await upstream.chat_completions(request_obj, fastapi_request)


def _request_fingerprint(request_obj: OpenAIRequest) -> str:
    return request_obj.model_dump_json()


def _resume_start_seq(request: Request) -> int:
    try:
        value = (request.headers.get("last-event-id") or "").strip()
        return max(0, int(value or request.query_params.get("resume_from", 0)))
    except (TypeError, ValueError):
        return 0


def _resume_headers(request_id: str, replay: bool = False) -> dict:
    headers = {"X-Resume-Id": request_id, "X-Request-Id": request_id, "Cache-Control": "no-store"}
    if replay:
        headers["X-Resumable-Replay"] = "1"
    return headers


def _cancel_response(message="调用方已断开连接，请求已取消。"):
    return JSONResponse(status_code=499, content=create_openai_error_response(
        499, message, "request_cancelled"))


def _exception_response(error, model):
    code, message = extract_upstream_error(error)
    print(f"❌ [路由兜底] 模型 {model} | HTTP {code} | {message[:200]}")
    return error_response(error)


def _response_payload(response):
    body = getattr(response, "body", b"") or b""
    try:
        return json.loads(body) if body else {}
    except (ValueError, TypeError):
        return {"_raw": body.decode("utf-8", "replace")[:4000]}


async def _finish_json(buf, response):
    status = response.status_code
    payload = _response_payload(response)
    await buf.finish("done" if status < 400 else "error",
                     error=payload.get("error", {}).get("message") if status >= 400 else None,
                     payload=payload, status_code=status)


async def _cancel_buffer(buf, message):
    if buf.done:
        return
    payload = create_openai_error_response(499, message, "request_cancelled")
    if buf.kind == "stream":
        await buf.append(frame_with_id(buf.next_seq, f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"))
        await buf.append(frame_with_id(buf.next_seq, DONE_FRAME))
    await buf.finish("cancelled", error=message, payload=payload, status_code=499)


async def _begin_buffered_job(request, request_obj, api_key):
    request_id, _ = compute_stream_id(request.headers, api_key, _request_fingerprint(request_obj))
    buf, is_new = await resumable_store.create_if_absent(
        request_id, model=request_obj.model, kind="stream" if request_obj.stream else "json",
        reset_delivered=True)
    if not is_new:
        return buf, buf.job, buf.job.attach(), False

    grace = app_state.get_disconnect_grace_seconds()
    job = GenerationJob(request_id, api_key, grace, lambda message: _cancel_buffer(buf, message))
    buf.job = job
    token = job.attach()

    async def work():
        response = None
        try:
            try:
                response = await _call_upstream(request_obj, JobRequest(request, job))
                response = await prepare_stream_response(response)
            except Exception as error:
                response = _exception_response(error, request_obj.model)
            job.response = response
            if isinstance(response, StreamingResponse):
                job.ready.set()
                await pump_stream(buf, response.body_iterator)
            else:
                await _finish_json(buf, response)
                job.ready.set()
        finally:
            job.produced.set()
            if response is not None and response.background is not None:
                background, response.background = response.background, None
                with anyio.CancelScope(shield=True):
                    await background()

    resumable_store.register_pump(request_id, job.start(work))
    return buf, job, token, True


async def _resumable_stream(request: Request, request_obj: OpenAIRequest, api_key: str):
    buf, job, token, is_new = await _begin_buffered_job(request, request_obj, api_key)
    transferred = False
    try:
        await while_connected(job.ready.wait(), request)
        if not isinstance(job.response, StreamingResponse):
            response = job.response or _cancel_response(job.reason)
            return JSONResponse(status_code=response.status_code, content=_response_payload(response),
                                headers=_resume_headers(buf.id, not is_new))
        response = ManagedStreamingResponse(
            sse_from_buffer(buf, _resume_start_seq(request)), request, job, token,
            media_type="text/event-stream", headers=_resume_headers(buf.id, not is_new))
        transferred = True
        return response
    except ClientDisconnected:
        return _cancel_response()
    finally:
        if not transferred:
            await job.release(token)


async def _resumable_json(request: Request, request_obj: OpenAIRequest, api_key: str):
    buf, job, token, is_new = await _begin_buffered_job(request, request_obj, api_key)
    wait_timeout = 600.0
    if app_state.is_web_proxy_enabled():
        wait_timeout = max(wait_timeout, app_state.get_studio_read_timeout_seconds() + 90.0)
    try:
        if buf.payload is None:
            await while_connected(buf.wait_for(2 ** 62, wait_timeout), request)
        if buf.payload is None:
            return JSONResponse(status_code=504, content=create_openai_error_response(
                504, "上游响应超时，缓冲未完成。", "timeout"),
                headers=_resume_headers(buf.id, not is_new))
        buf.mark_delivered()
        return JSONResponse(status_code=buf.status_code, content=buf.payload,
                            headers=_resume_headers(buf.id, not is_new))
    except ClientDisconnected:
        return _cancel_response()
    finally:
        await job.release(token)


async def _direct_frames(job, queue):
    while True:
        if not queue.empty():
            yield queue.get_nowait()
            continue
        if job.done.is_set():
            if job.cancelled.is_set():
                error = create_openai_error_response(499, job.reason, "request_cancelled")
                yield f"data: {json.dumps(error, ensure_ascii=False)}\n\n"
                yield DONE_FRAME
            return
        item = asyncio.create_task(queue.get())
        finished = asyncio.create_task(job.done.wait())
        try:
            await asyncio.wait((item, finished), return_when=asyncio.FIRST_COMPLETED)
            if item.done():
                yield item.result()
        finally:
            await cancel_and_wait(item, finished)


async def _direct_response(request, request_obj, api_key):
    # No response history when resumable mode is off; one pending chunk supplies backpressure.
    job = GenerationJob(f"direct-{uuid.uuid4().hex}", api_key)
    token = job.attach()
    queue = asyncio.Queue(maxsize=1)

    async def work():
        response = None
        try:
            response = await _call_upstream(request_obj, JobRequest(request, job))
            response = await prepare_stream_response(response)
            job.response = response
            job.ready.set()
            if isinstance(response, StreamingResponse):
                try:
                    async for frame in response.body_iterator:
                        await queue.put(frame)
                finally:
                    close = getattr(response.body_iterator, "aclose", None)
                    if close is not None:
                        with anyio.CancelScope(shield=True):
                            await close()
        except Exception as error:
            error_response = _exception_response(error, request_obj.model)
            if job.ready.is_set():
                await queue.put(f"data: {json.dumps(_response_payload(error_response))}\n\n")
                await queue.put(DONE_FRAME)
            else:
                job.response = error_response
        finally:
            job.produced.set()
            if response is not None and response.background is not None:
                background, response.background = response.background, None
                with anyio.CancelScope(shield=True):
                    await background()

    job.start(work)
    transferred = False
    try:
        await while_connected(job.ready.wait(), request)
        response = job.response or _cancel_response(job.reason)
        response.headers["X-Request-Id"] = job.id
        if not isinstance(response, StreamingResponse):
            return response
        managed = ManagedStreamingResponse(
            _direct_frames(job, queue), request, job, token,
            status_code=response.status_code, headers=dict(response.headers),
            media_type=response.media_type)
        transferred = True
        return managed
    except ClientDisconnected:
        return _cancel_response()
    finally:
        if not transferred:
            await job.release(token)


@router.post("/v1/chat/completions")
async def chat_completions(fastapi_request: Request, request: OpenAIRequest,
                           api_key: str = Depends(get_api_key)):
    """Both upstreams share connection monitoring, task ownership and cancellation."""
    try:
        if resumable_enabled():
            if request.stream:
                response = await _resumable_stream(fastapi_request, request, api_key)
            else:
                response = await _resumable_json(fastapi_request, request, api_key)
        else:
            response = await _direct_response(fastapi_request, request, api_key)
        return terminal_error_response(response)
    except Exception as error:
        return _exception_response(error, request.model)


@router.delete("/v1/requests/{request_id}")
@router.delete("/v1/resumable/{request_id}")
async def cancel_request(request_id: str, api_key: str = Depends(get_api_key)):
    job = active_jobs.get(request_id)
    if job is None:
        record = finished_job(request_id)
        if record is not None and hmac.compare_digest(record["owner"], owner_digest(api_key)):
            return JSONResponse(content={"id": request_id,
                "status": "cancelled" if record["cancelled"] else "done",
                "cancelled": record["cancelled"]})
        buf = await resumable_store.get(request_id)
        job = buf.job if buf is not None else None
    if job is None or not job.owned_by(api_key):
        return JSONResponse(status_code=404, content=create_openai_error_response(
            404, "未找到该调用方的请求。", "not_found"))
    cancelled = await job.cancel()
    return JSONResponse(content={
        "id": request_id, "status": "cancelled" if cancelled else "done", "cancelled": cancelled,
    })


@router.post("/v1/chat/completions/cancel")
async def cancel_by_request(fastapi_request: Request, request: OpenAIRequest,
                            api_key: str = Depends(get_api_key)):
    """Cancel a buffered request using its original body and idempotency headers."""
    request_id, _ = compute_stream_id(fastapi_request.headers, api_key, _request_fingerprint(request))
    return await cancel_request(request_id, api_key)


@router.get("/v1/resumable/{stream_id}")
async def resumable_poll(stream_id: str, fastapi_request: Request,
                         api_key: str = Depends(get_api_key),
                         from_: int = Query(0, alias="from"), limit: int = Query(200),
                         wait: float = Query(0.0)):
    buf = await resumable_store.get(stream_id)
    if buf is None or buf.job is None or not buf.job.owned_by(api_key):
        return JSONResponse(status_code=404, content=create_openai_error_response(
            404, "未找到该调用方的响应缓冲。", "not_found"))
    job = buf.job
    token = job.attach()
    try:
        limit = max(1, min(limit, 500))
        if wait > 0 and not buf.done:
            await while_connected(buf.wait_for(max(0, from_), min(wait, 25.0)), fastapi_request)
        start = max(0, from_)
        events = buf.snapshot(start, limit)
        end = start + len(events)
        if buf.done and end >= buf.next_seq and start <= buf.next_seq:
            buf.mark_delivered()
        return JSONResponse(content={
            "id": stream_id, "model": buf.model, "kind": buf.kind, "status": buf.status,
            "error": buf.error,
            "events": [{"seq": start + i, "data": frame} for i, frame in enumerate(events)],
            "next": end, "done": buf.done and end >= buf.next_seq,
        })
    except ClientDisconnected:
        return _cancel_response()
    finally:
        await job.release(token)
